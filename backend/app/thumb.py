"""视频片段的首帧缩略图。

分镜画布上如果有几十个视频节点,浏览器同时加载几十个 <video> 会非常卡。
这里给每个片段生成一张 JPEG 首帧,前端默认只显示图片 + 播放按钮,
点播放时才加载那一个视频。

缩略图按片段文件名缓存到 storage/thumbs/<stem>.jpg:
* 新片段在 write_clip() 时顺手生成;
* 老片段第一次被请求时按需生成(main.py 的 /media/thumbs/ 路由)。
"""
from __future__ import annotations

from pathlib import Path

import av
from PIL import Image

from .config import STORAGE_DIR

THUMB_DIR = STORAGE_DIR / "thumbs"
_SAFE = set("0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-")


def thumb_rel_for(clip_rel: str | None) -> str | None:
    if not clip_rel:
        return None
    return f"thumbs/{Path(clip_rel).stem}.jpg"


def thumb_path_for(clip: str | Path) -> Path:
    return THUMB_DIR / (Path(clip).stem + ".jpg")


def write_thumb(clip_path: str | Path, frame: Image.Image) -> None:
    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        im = frame.convert("RGB")
        im.thumbnail((480, 480), getattr(Image, "Resampling", Image).LANCZOS)
        im.save(thumb_path_for(clip_path), "JPEG", quality=85, optimize=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[thumb] 生成失败 {clip_path}: {exc}", flush=True)


def ensure_thumb(name: str) -> Path | None:
    """按 /media/thumbs/<stem>.jpg 的请求按需生成;找不到片段返回 None。"""
    if not name.endswith(".jpg"):
        return None
    stem = name[:-4]
    if not stem or any(c not in _SAFE for c in stem):
        return None
    out = THUMB_DIR / name
    if out.exists():
        return out
    for sub in ("clips", "renders"):
        clip = STORAGE_DIR / sub / f"{stem}.mp4"
        if not clip.exists():
            continue
        try:
            with av.open(str(clip)) as container:
                stream = container.streams.video[0]
                for frame in container.decode(stream):
                    write_thumb(clip, frame.to_image())
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"[thumb] 解码失败 {clip}: {exc}", flush=True)
        if out.exists():
            return out
        break
    return None
