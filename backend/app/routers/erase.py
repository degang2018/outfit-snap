"""图片橡皮擦:上传原图 + 一张「擦除区域」的 mask,后端按 mask 无损抠图后返回。

原图字节原样留档;结果 PNG 里**没擦到的像素与原图完全一致**(1:1 复制,不缩放不重编码)。
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from ..config import STORAGE_DIR
from ..deps import get_current_user
from ..image_eraser import erase
from ..models import User

router = APIRouter(prefix="/erase", tags=["erase"])

ALLOWED_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp"}
MAX_IMAGE_MB = 30
MAX_MASK_MB = 40
MODES = {"transparent", "white"}


def _safe_stem(name: str | None) -> str:
    """落到 URL 上的文件名必须是纯 ASCII,否则下载链接会坏。"""
    stem = Path(name or "image").stem
    keep = "".join(c for c in stem if c.isascii() and (c.isalnum() or c in "-_"))
    return keep[:40] or "image"


@router.post("")
async def erase_image(
    file: UploadFile = File(...),
    mask: UploadFile = File(...),
    mode: str = Form("transparent"),
    user: User = Depends(get_current_user),
) -> dict:
    if (file.content_type or "") not in ALLOWED_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只支持 PNG / JPG / WEBP / BMP 图片")
    if (mask.content_type or "") not in ALLOWED_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "mask 必须是 PNG / JPG / WEBP / BMP 图片")
    if mode not in MODES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "擦除方式只能是 transparent(透明)或 white(填白)")

    data = await file.read()
    mask_data = await mask.read()
    if not data or not mask_data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件是空的")
    if len(data) > MAX_IMAGE_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"图片不能超过 {MAX_IMAGE_MB}MB")
    if len(mask_data) > MAX_MASK_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"擦除区域数据不能超过 {MAX_MASK_MB}MB")

    uid = uuid.uuid4().hex
    rel_dir = f"erases/{user.id}/{uid}"
    dest = STORAGE_DIR / rel_dir
    dest.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    orig_name = f"original{ext}"
    # 原图:字节原样落盘 —— 之后所有处理都从它读,它自己永不被改写
    (dest / orig_name).write_bytes(data)
    (dest / "mask.png").write_bytes(mask_data)

    stem = _safe_stem(file.filename)
    out_name = f"{stem}_erased.png"
    try:
        width, height = erase(dest / orig_name, dest / "mask.png", dest / out_name, mode=mode)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"无法处理该图片:{exc}") from exc

    base = f"/media/{rel_dir}"
    return {
        "original": {
            "url": f"{base}/{orig_name}",
            "filename": file.filename or orig_name,
            "width": width,
            "height": height,
        },
        "erased": {
            "url": f"{base}/{out_name}",
            "filename": out_name,
            "width": width,
            "height": height,
            "mode": mode,
        },
    }
