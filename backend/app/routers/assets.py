"""素材库:参考图上传,按 人物/道具/场景 分组,可在镜头提示词里引用。

A shot references an image either through its ordered `refs` list ("asset:<id>") or by
dropping the image straight into the prompt text, which inserts a `<Picture N>` token and
appends the asset to `refs` so it is actually fed to ComfyUI.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from PIL import Image
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import quick_start
from ..config import STORAGE_DIR
from ..db import get_db
from ..deps import get_current_user
from ..models import CAT_CHARACTER, CAT_PROP, CAT_SCENE, CATEGORIES, Asset, User
from ..schemas import AssetOut, AssetPatchIn, AssetRecognizeIn
from ..serializers import asset_out

router = APIRouter(prefix="/assets", tags=["assets"])

ALLOWED_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp"}
MAX_UPLOAD_MB = 20

# 一次读图认几件。视觉模型一次能看多张(它按「图 N 是哪一件」逐张回报),批量比逐张快很多。
RECOGNIZE_BATCH = 8


def recognize_assets(db: Session, assets: list[Asset], force: bool = False) -> int:
    """把「这张图是哪件衣服」认出来,写回 asset.ai_name / ai_desc —— **这是缓存**。

    已经认过的直接跳过(force=True 才重认):同一张图只读一次图,以后每次生成都用缓存,
    不再调视觉模型(以前是每生成一次就把所有图重读一遍)。用户手改过 ai_name / ai_desc 的,
    传 force 才会被覆盖。只认「道具」分类 —— 人物图 / 场景图喂给"服装买手"会把话说歪。
    """
    # 人物图 / 场景图不认(喂给"服装买手"会把话说歪);其余(道具 / 整套参考图 / 实拍参考图…)都认。
    # 以前这里写死 `category == prop`,结果 category='garment' 的整套参考图被静默跳过、
    # 永远认不出来(实测:一张被标成 garment 的整套拼图,件名一直是空)。
    skip = (CAT_CHARACTER, CAT_SCENE)
    todo = [a for a in assets
            if a.category not in skip and a.rel_path
            and (force or not (a.ai_name or "").strip())]
    done = 0
    for i in range(0, len(todo), RECOGNIZE_BATCH):
        chunk = todo[i:i + RECOGNIZE_BATCH]
        paths = [str(STORAGE_DIR / a.rel_path) for a in chunk]
        data, _engine, _detail = quick_start.describe_garment(paths, want_pieces=True)
        if not data:
            continue
        # 一张图可能认出一件以上(实拍拼图):按图号分组,第一件当"这张图叫它",其余存进 ai_more
        by_index: dict[int, list[dict]] = {}
        for p in (data.get("pieces") or []):
            by_index.setdefault(p.get("index"), []).append(p)
        for n, asset in enumerate(chunk, start=1):
            found = by_index.get(n) or []
            first = found[0] if found else {}
            name = str(first.get("part") or "").strip()[:80]
            desc = str(first.get("desc") or "").strip()[:120]
            kind = str(first.get("slot") or "").strip()[:20]
            length = str(first.get("len") or "").strip()[:10]
            if not desc and len(chunk) == 1:
                # 单张时整体描述就是这一件的描述,用它兜底
                desc = str(data.get("description") or "").strip()[:120]
            more: list[dict] = []
            for x in found[1:]:
                m = {"name": str(x.get("part") or "").strip()[:80],
                     "desc": str(x.get("desc") or "").strip()[:120],
                     "kind": str(x.get("slot") or "").strip()[:20],
                     "len": str(x.get("len") or "").strip()[:10]}
                if m["name"] or m["kind"]:
                    more.append(m)
            if name:
                asset.ai_name = name
            if desc:
                asset.ai_desc = desc
            if kind:
                asset.ai_kind = kind
            if length:
                asset.ai_len = length
            asset.ai_more = json.dumps(more, ensure_ascii=False) if more else ""
            if name or desc or kind or length or more:
                done += 1
    db.commit()
    return done


def _visible(db: Session, user: User) -> list[Asset]:
    """自己的素材 + 内置素材(内置素材所有账号都能用,但只能读)。"""
    return (
        db.query(Asset)
        .filter(or_(Asset.user_id == user.id, Asset.is_system.is_(True)))
        .order_by(Asset.is_system.desc(), Asset.id.desc())
        .all()
    )


def _editable(db: Session, user: User, asset_id: int) -> Asset:
    asset = db.get(Asset, asset_id)
    if asset is None or (asset.user_id != user.id and not asset.is_system):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在")
    if asset.is_system:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "内置素材不可修改")
    return asset


@router.post("", response_model=AssetOut, status_code=status.HTTP_201_CREATED)
async def upload(
    file: UploadFile = File(...),
    category: str = Form(CAT_PROP),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssetOut:
    if (file.content_type or "") not in ALLOWED_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只支持 PNG / JPG / WEBP / BMP 图片")
    if category not in CATEGORIES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "分类必须是 人物 / 道具 / 场景")

    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"图片不能超过 {MAX_UPLOAD_MB}MB")

    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    name = f"{uuid.uuid4().hex}{ext}"
    rel = f"assets/{user.id}/{name}"
    dest = STORAGE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    try:
        with Image.open(dest) as im:
            width, height = im.size
    except Exception as exc:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法解析该图片") from exc

    asset = Asset(
        user_id=user.id, filename=file.filename or name, rel_path=rel,
        category=category, mime=file.content_type or "image/png",
        width=width, height=height,
    )
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset_out(asset)


@router.get("", response_model=list[AssetOut])
def list_assets(
    category: str | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AssetOut]:
    rows = _visible(db, user)
    if category in CATEGORIES:
        rows = [a for a in rows if a.category == category]
    return [asset_out(a) for a in rows]


@router.post("/recognize", response_model=list[AssetOut])
def recognize(
    payload: AssetRecognizeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[AssetOut]:
    """把选中的素材读图认件一遍,结果缓存在素材上(ai_name / ai_desc),返回更新后的素材。

    已认过的不会重认(除非 force=True)—— 这就是缓存:生成时直接用,不再调视觉模型。
    """
    want = set(payload.ids or [])
    rows = [a for a in _visible(db, user) if a.id in want]
    recognize_assets(db, rows, force=payload.force)
    return [asset_out(a) for a in rows]


@router.get("/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> AssetOut:
    """Any signed-in user may read an asset by id - template shots reference the built-in
    lookbook images, which belong to the system account rather than to the current user."""
    asset = db.get(Asset, asset_id)
    if asset is None or (asset.user_id != user.id and not asset.is_system):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "图片不存在")
    return asset_out(asset)


@router.patch("/{asset_id}", response_model=AssetOut)
def update_asset(
    asset_id: int,
    payload: AssetPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AssetOut:
    """素材管理里的「保存」:改分类 / 改名。"""
    asset = _editable(db, user, asset_id)
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        if value is not None:
            setattr(asset, field, value)
    db.commit()
    db.refresh(asset)
    return asset_out(asset)


@router.delete("/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_asset(asset_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    asset = _editable(db, user, asset_id)
    (STORAGE_DIR / asset.rel_path).unlink(missing_ok=True)
    db.delete(asset)
    db.commit()
