"""局部修理:在涂抹区域里用 Qwen-Image-Edit-2511 重绘,区域外保持不动。

前端传一张白底笔刷 mask(白色=要重绘),这里把原图和 mask 一起送进 ComfyUI:
VAEEncode -> SetLatentNoiseMask -> KSampler,潜空间里只更新 mask 区域。
"""
from __future__ import annotations

import io
import os
import time
import uuid
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

from . import comfy_client
from .config import STORAGE_DIR, settings
from .threeview import QWEN_CLIP, QWEN_UNET, QWEN_VAE, _upload

OUT_DIR = STORAGE_DIR / "canvas" / "inpaint"
TMP_DIR = OUT_DIR / "_tmp"

DEFAULT_PROMPT = (
    "只修改涂抹区域：去掉区域里的物体或瑕疵，用周围的背景、布料或皮肤自然补全，"
    "边缘和光影要接得上；涂抹区域以外的像素保持完全不变。"
)


def _prepare(src: Path, mask_bytes: bytes) -> tuple[Path, Path, Image.Image, Image.Image]:
    """返回 (送 ComfyUI 的图, 送 ComfyUI 的 mask, 原尺寸图, 原尺寸 mask)。"""
    with Image.open(src) as im:
        orig = ImageOps.exif_transpose(im).convert("RGB")
    rgb = orig.copy()
    try:
        mask = Image.open(io.BytesIO(mask_bytes)).convert("L")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"mask 图片无法解析: {exc}") from exc

    w, h = rgb.size
    # 统一缩放到 16 的倍数,Qwen 的 VAE 按 8 倍下采样,尺寸对齐更稳
    tw = max(64, int(w // 16 * 16))
    th = max(64, int(h // 16 * 16))
    max_side = 1280
    if max(tw, th) > max_side:
        scale = max_side / max(tw, th)
        tw = max(64, int(tw * scale) // 16 * 16)
        th = max(64, int(th * scale) // 16 * 16)
    rgb = rgb.resize((tw, th), Image.LANCZOS)
    mask = mask.resize((tw, th), Image.NEAREST)

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex
    img_p = TMP_DIR / f"{stem}_img.png"
    mask_p = TMP_DIR / f"{stem}_mask.png"
    rgb.save(img_p, "PNG")
    mask.save(mask_p, "PNG")
    return img_p, mask_p, orig, mask


def _workflow(img_name: str, mask_name: str, prompt: str, seed: int, steps: int, prefix: str) -> dict:
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": QWEN_UNET}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
        "4": {"class_type": "LoadImage", "inputs": {"image": img_name}},
        "5": {"class_type": "LoadImageMask", "inputs": {"image": mask_name, "channel": "red"}},
        "6": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "image1": ["4", 0], "prompt": prompt}},
        "7": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "image1": ["4", 0], "prompt": ""}},
        "8": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["6", 0], "reference_latents_method": "index_timestep_zero"}},
        "9": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["7", 0], "reference_latents_method": "index_timestep_zero"}},
        "10": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.1}},
        "11": {"class_type": "CFGNorm", "inputs": {"model": ["10", 0], "strength": 1.0}},
        "12": {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["3", 0]}},
        "13": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["12", 0], "mask": ["5", 0]}},
        "14": {"class_type": "KSampler", "inputs": {
            "model": ["11", 0], "positive": ["8", 0], "negative": ["9", 0],
            "latent_image": ["13", 0], "seed": seed, "steps": steps, "cfg": 4.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}},
        "16": {"class_type": "SaveImage", "inputs": {"images": ["15", 0], "filename_prefix": prefix}},
    }


def run(image_url: str, mask_bytes: bytes, prompt: str = "", steps: int = 24) -> str:
    if not image_url.startswith("/media/"):
        raise ValueError("只支持修理存储里的图片")
    src = STORAGE_DIR / image_url[len("/media/"):]
    if not src.exists():
        raise ValueError("找不到要修理的原图")
    if not mask_bytes:
        raise ValueError("没有收到涂抹区域")
    if not comfy_client.is_up():
        raise ValueError("ComfyUI 没启动（127.0.0.1:8188）")

    img_p, mask_p, orig, mask = _prepare(src, mask_bytes)
    try:
        img_name = _upload(img_p)
        mask_name = _upload(mask_p)
        steps = max(8, min(40, int(steps or 24)))
        text = (prompt or "").strip() or DEFAULT_PROMPT
        if "涂抹区域以外" not in text:
            text += " 涂抹区域以外的像素保持完全不变。"
        seed = int(time.time() * 1000) % (2 ** 31 - 1)
        prefix = "inpaint_" + uuid.uuid4().hex[:8]
        pid = comfy_client.submit(_workflow(img_name, mask_name, text, seed, steps, prefix))
        comfy_client.wait(pid, timeout=1800)
        st = comfy_client.status(pid)
        files = []
        for node in (st.get("outputs") or {}).values():
            for img in node.get("images", []):
                p = Path(settings.comfy_output_dir) / (img.get("subfolder") or "") / img["filename"]
                if p.exists():
                    files.append(p)
        if not files:
            raise RuntimeError("ComfyUI 没有返回图片")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        dst = OUT_DIR / f"{uuid.uuid4().hex}.png"
        # 只把涂抹区域换成生成结果,其余像素用原图直接盖回去 —— 保证「区域外完全不动」。
        with Image.open(files[0]) as im:
            edited = im.convert("RGB")
        if edited.size != orig.size:
            edited = edited.resize(orig.size, Image.LANCZOS)
        if mask.size != orig.size:
            mask = mask.resize(orig.size, Image.NEAREST)
        cut = mask.filter(ImageFilter.GaussianBlur(1.2))
        Image.composite(edited, orig, cut).save(dst, "PNG")
        return f"/media/{dst.relative_to(STORAGE_DIR).as_posix()}"
    finally:
        for p in (img_p, mask_p):
            try:
                os.remove(p)
            except OSError:
                pass
        comfy_client.free_memory()
