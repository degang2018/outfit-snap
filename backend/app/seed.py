"""Seed the built-in "15-shot outfit change" template, including its reference images.

Idempotent: running it again only adds what is missing.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from . import concise, prompt_library as pl
from .config import STORAGE_DIR
from .db import SessionLocal
from .models import CAT_CHARACTER, MODE_REF, Asset, Playbook, Template, TemplateShot, User
from .quick_start import DEFAULT_BASE_OUTFIT

TEMPLATE_KEY = "outfit-change-15shot"

# 内置玩法:只有「结构 + 参数」,剧本每次按这件衣服现场生成,所以换任何衣服都能套
SYSTEM_PLAYBOOKS: list[dict] = [
    {
        "key": "float_overhead",
        "title": "悬浮变装 · 托举式",
        "description": (
            "纯白棚、固定机位零运镜。第 1 镜她只穿打底,双手把套装举到胸口抛起,"
            "套装在半空停一瞬后快速落下、**一件一件按正确顺序自动穿上**"
            "(先内搭打底,再中间层,最后才套外面的开衫,不许先穿外套);"
            "之后全是已穿套装的细节快切镜(补妆/拍照/特写)。"
        ),
        "structure": "float_dress",
        "params": {
            "base_outfit": DEFAULT_BASE_OUTFIT,
            "custom_actions": [],
            "scene": "studio_white",
            "shot_count": 13,
            "total_seconds": 23.7,
        },
    },
    {
        "key": "float_side",
        "title": "悬浮变装 · 侧面飞入",
        "description": (
            "同上是纯白棚固定机位,但套装不是托举,而是**一件件从画面上方落下来、罩到她身上**、"
            "贴身垂顺成型、**一件一件按正确顺序自动穿上**"
            "(先内搭打底,再中间层,最后才套外面的开衫,不许先穿外套);"
            "最后一段让包这类道具也落到她手里。"
        ),
        "structure": "float_dress_side",
        "params": {
            "base_outfit": DEFAULT_BASE_OUTFIT,
            "custom_actions": [],
            "scene": "studio_white",
            "shot_count": 9,
            "total_seconds": 16.0,
        },
    },
]


# where the reference images live on this machine (the ones used for viral_gray_24s.mp4)
SOURCE_IMAGES: dict[str, Path] = {
    "lookbook": Path(r"h:\comfyui\video\人物\Gray Loungewear Lookbook Collage.png"),
    "back": Path(r"h:\comfyui\video\人物\grey_back.png"),
}


def _system_user(db: Session) -> User:
    user = db.query(User).filter(User.display_name == "系统素材").first()
    if user is None:
        user = User(display_name="系统素材", is_guest=False)
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _import_image(db: Session, user: User, key: str) -> Asset | None:
    src = SOURCE_IMAGES.get(key)
    if src is None or not src.exists():
        return None
    existing = (
        db.query(Asset)
        .filter(Asset.user_id == user.id, Asset.filename == f"[sys]{key}{src.suffix}")
        .first()
    )
    if existing:
        # self-heal rows that predate the is_system/category columns
        if not existing.is_system or existing.category != CAT_CHARACTER:
            existing.is_system = True
            existing.category = CAT_CHARACTER
            db.commit()
        return existing

    rel = f"assets/system/{key}{src.suffix}"
    dest = STORAGE_DIR / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)

    width = height = 0
    try:
        from PIL import Image

        with Image.open(dest) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001
        pass

    asset = Asset(user_id=user.id, filename=f"[sys]{key}{src.suffix}", rel_path=rel,
                  category=CAT_CHARACTER, is_system=True, mime="image/png",
                  width=width, height=height)
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


def _seed_playbooks(db: Session) -> None:
    """内置玩法:只补不改(用户改过的内置玩法不会被覆盖)。"""
    for spec in SYSTEM_PLAYBOOKS:
        row = db.query(Playbook).filter(Playbook.key == spec["key"]).first()
        if row is None:
            db.add(Playbook(
                owner_id=None, is_system=True, key=spec["key"], title=spec["title"],
                description=spec["description"], structure=spec["structure"],
                params=dict(spec["params"]),
            ))
        elif not row.is_system:
            row.is_system = True      # 自愈:早期建的行没有 is_system
    db.commit()


def seed(db: Session) -> None:
    _seed_playbooks(db)
    lookbook = _import_image(db, _system_user(db), "lookbook")
    back = _import_image(db, _system_user(db), "back")
    if lookbook is None:
        return

    tpl = db.query(Template).filter(Template.key == TEMPLATE_KEY).first()
    if tpl is None:
        tpl = Template(
            key=TEMPLATE_KEY,
            is_system=True,
            user_id=None,
            title="15 镜头 · AI 变装带货短片",
            description=(
                "9:16 竖屏硬切变装短片:白棚冷光、固定机位零运镜。开头抛起灰套装并飘落自动穿好,"
                "之后依次补妆、转身展示背面、摆拍收尾。总长约 23.7 秒,共 15 个镜头。"
                "参考图默认使用灰莫代尔三件套 lookbook 拼图(含多角度)与背面参考图。"
            ),
            cover_url=f"/media/{lookbook.rel_path}",
            out_width=1080,
            out_height=1920,
            fps=24,
        )
        db.add(tpl)
        db.commit()
        db.refresh(tpl)
    elif not tpl.is_system:
        # self-heal a template row created before the is_system column existed
        tpl.is_system = True
        db.commit()

    if tpl.shots:
        return

    for i, key in enumerate(pl.ORDER, start=1):
        frames, title, options, _action = pl.SHOTS[key]
        refs = [f"asset:{lookbook.id}"]
        if key in pl.BACK_KEYS and back is not None:
            refs.append(f"asset:{back.id}")
        db.add(TemplateShot(
            template_id=tpl.id,
            idx=i,
            key=key,
            title=title,
            mode=MODE_REF,
            duration_frames=frames,
            prompt=concise.rule_condense(pl.compose(key)),
            refs=refs,
            options=options,
        ))
    db.commit()


def run() -> None:
    db = SessionLocal()
    try:
        seed(db)
    finally:
        db.close()
