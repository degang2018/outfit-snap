"""衣服分割:上传一张平铺商品图 → 按背景切成若干件 → 每件单独下载。

原图字节原样留档;切出来的每件都是原像素 1:1(无损 PNG),不做缩放 / 有损压缩。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from PIL import Image

from ..config import STORAGE_DIR
from ..deps import get_current_user
from ..garment_split import split_image
from ..models import User

router = APIRouter(prefix="/split", tags=["split"])

ALLOWED_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp"}
MAX_UPLOAD_MB = 30


def _safe_stem(name: str | None) -> str:
    """文件名可以是中文/带空格,但落到 URL 上必须是纯 ASCII,否则下载链接会坏。"""
    stem = Path(name or "image").stem
    keep = "".join(c for c in stem if c.isascii() and (c.isalnum() or c in "-_"))
    return keep[:40] or "image"


@router.post("")
async def split(
    file: UploadFile = File(...),
    parts: int = Form(0),
    tolerance: int = Form(20),
    transparent: bool = Form(False),
    user: User = Depends(get_current_user),
) -> dict:
    if (file.content_type or "") not in ALLOWED_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只支持 PNG / JPG / WEBP / BMP 图片")

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件是空的")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"图片不能超过 {MAX_UPLOAD_MB}MB")

    uid = uuid.uuid4().hex
    rel_dir = f"splits/{user.id}/{uid}"
    dest = STORAGE_DIR / rel_dir
    dest.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    orig_name = f"original{ext}"
    # 原图:字节原样落盘 —— 后面所有裁切都从它读,它自己永不被改写
    (dest / orig_name).write_bytes(data)

    try:
        with Image.open(dest / orig_name) as im:
            im.load()
            width, height = im.size
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法解析该图片") from exc

    try:
        pieces = split_image(
            dest / orig_name,
            dest,
            _safe_stem(file.filename),
            parts=max(0, int(parts)),
            tolerance=max(1, min(120, int(tolerance))),
            transparent=bool(transparent),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    base = f"/media/{rel_dir}"
    for p in pieces:
        p["url"] = f"{base}/{p['filename']}"
    return {
        "original": {
            "url": f"{base}/{orig_name}",
            "filename": file.filename or orig_name,
            "width": width,
            "height": height,
        },
        "parts": pieces,
    }
