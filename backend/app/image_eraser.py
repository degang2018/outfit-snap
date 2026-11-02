"""图片橡皮擦:前端把「擦掉的地方」涂成一张 mask,后端按 mask 抠掉。

为什么放到后端做:
  * 能保证「没擦到的像素一个都不动」—— 直接复制原图解码后的像素值,不做任何颜色/缩放处理;
  * 输出 PNG(无损),不会像 JPEG 那样产生压缩损失;
  * 原图另存一份**字节完全相同**的副本(见 router),随时能回到起点。

mask 约定:白色(或高亮)  = 擦掉;黑色 = 保留。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageOps


def erase(src: Path, mask_path: Path, out_path: Path, mode: str = "transparent") -> tuple[int, int]:
    """按 mask 把 src 抠一遍写到 out_path,返回 (宽, 高)。

    mode='transparent' 擦成透明(PNG 带 alpha);mode='white' 擦成白色。
    没被涂到的像素:两条路径下都**原样保留**(见下面整型运算,alpha=255 时结果恒等于原值)。
    """
    with Image.open(src) as im:
        # 手机拍的照片常带 EXIF 旋转,浏览器显示时已经转正了 —— 这里跟着转,
        # 否则后端读到的像素方向和用户看到的、以及 mask 的方向就对不上了。
        rgb = ImageOps.exif_transpose(im).convert("RGB")
    with Image.open(mask_path) as m:
        # 先把 mask 压到黑底上再取灰度:这样"透明背景+白色笔刷"的 mask 也能正确解读,
        # 不透明的黑白 mask 结果也完全一样。
        mask = m.convert("RGBA")
    if mask.size != rgb.size:
        raise ValueError(f"mask 尺寸 {mask.size[0]}×{mask.size[1]} 与原图 {rgb.size[0]}×{rgb.size[1]} 不一致")

    flat = Image.alpha_composite(Image.new("RGBA", mask.size, (0, 0, 0, 255)), mask).convert("L")

    a = np.asarray(rgb, dtype=np.uint16)
    cut = np.asarray(flat, dtype=np.uint16)                 # 255 = 擦掉
    keep = 255 - cut                                        # 255 = 完全保留
    ku = keep[..., None]

    if mode == "white":
        out = ((a * ku + 255 * (255 - ku)) // 255).astype(np.uint8)
        img = Image.fromarray(out, "RGB")
    else:
        img = Image.fromarray(np.dstack([a.astype(np.uint8), keep.astype(np.uint8)]), "RGBA")

    img.save(out_path)      # 后缀 .png → 无损
    return rgb.size
