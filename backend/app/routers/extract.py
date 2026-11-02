"""提取衣服:上传一张真人穿着照 → FLUX3 Klein 把身上某件衣服抽成白底平铺商品图。

原图字节原样留档;产出图走 ComfyUI(8188) 本地生成,PNG 落盘后可预览 / 下载。
提示词一律正向描述(「只呈现这件衣服、平铺、白底」),不用否定枚举 —— 负面词会反噬。
被原图遮住的部位(比如腰头)是模型按可见部分合理补全的,前端要给用户提示。
"""
from __future__ import annotations

import asyncio
import json
import random
import shutil
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from PIL import Image

from .. import comfy_client
from ..config import STORAGE_DIR, settings
from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/extract", tags=["extract"])

ALLOWED_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp"}
MAX_UPLOAD_MB = 30

FLUX3_UNET = "flux3-image-klein-int8-convrot.safetensors"
FLUX3_CLIP = "qwen3vl_4b_int8_convrot.safetensors"
FLUX3_VAE = "flux2-vae.safetensors"

# 按区域正向描述:只写「要什么」,不写「不要什么」
_PROMPTS = {
    "top": (
        "Take the upper-body garment worn by the person in <ref_image_0> and present only "
        "that garment by itself, as a single product for sale in a store: the whole garment "
        "neatly laid out flat, front view, sleeves spread naturally, centered on a pure "
        "white background, nothing else in the frame. Match the garment's color, pattern, "
        "fabric texture, collar, cuffs, fit and length exactly. Photorealistic, sharp and "
        "detailed, soft even lighting."
    ),
    "bottom": (
        "Take the lower-body garment worn by the person in <ref_image_0> and present only "
        "that garment by itself, as a single product for sale in a store: the whole garment "
        "neatly laid out flat, front view, centered on a pure white background, nothing "
        "else in the frame. Match the garment's color, pattern, fabric texture, waistband, "
        "fit and length exactly. Photorealistic, sharp and detailed, soft even lighting."
    ),
}


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----extract" + uuid.uuid4().hex
    lines: list[bytes] = []
    for k, v in fields.items():
        lines.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    for k, (fname, data) in files.items():
        lines.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{fname}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
        )
    lines.append(f"--{boundary}--\r\n".encode())
    return b"".join(lines), f"multipart/form-data; boundary={boundary}"


def _upload(local: Path) -> str:
    """把本地图片传进 ComfyUI 的 input 目录,返回 LoadImage 可用的名字。"""
    data, ctype = _multipart({}, {"image": (local.name, local.read_bytes())})
    req = urllib.request.Request(
        settings.comfy_api.rstrip("/") + "/upload/image",
        data=data, headers={"Content-Type": ctype}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.loads(r.read().decode())
    name = res.get("name") or local.name
    sub = res.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


def _workflow(image_name: str, prompt: str, seed: int, prefix: str) -> dict:
    """FLUX3 Klein 单参考图提取工作流(与 tryon_flux3.py 同款接线)。

    坑:ReferenceMultiLatent 的 autogrow 键必须带组前缀「images.image0」;
    DynamicCombo 平铺成「resolution」+「resolution.megapixels」。
    """
    return {
        "U": {"class_type": "UNETLoader", "inputs": {
            "unet_name": FLUX3_UNET, "weight_dtype": "default"}},
        "C": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": FLUX3_CLIP, "type": "flux3", "device": "default"}},
        "V": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX3_VAE}},
        "L": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "P": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["C", 0]}},
        "N": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["P", 0]}},
        "R": {"class_type": "ReferenceMultiLatent", "inputs": {
            "positive": ["P", 0], "negative": ["N", 0], "vae": ["V", 0],
            "resolution": "resize", "resolution.megapixels": 2.0,
            "images.image0": ["L", 0]}},
        "K": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 20, "cfg": 1.0, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0,
            "model": ["U", 0], "positive": ["R", 0], "negative": ["R", 1],
            "latent_image": ["R", 2]}},
        "D": {"class_type": "VAEDecode", "inputs": {"samples": ["K", 0], "vae": ["V", 0]}},
        "S": {"class_type": "SaveImage", "inputs": {"images": ["D", 0], "filename_prefix": prefix}},
    }


def _save_output(filename: str, subfolder: str, dest_dir: Path) -> Path | None:
    src = Path(settings.comfy_output_dir) / subfolder / filename
    dst = dest_dir / f"extracted_{uuid.uuid4().hex[:8]}.png"
    if src.exists():
        shutil.copyfile(src, dst)
        return dst
    # 输出目录可能被改成别的路径,走 HTTP 兜底
    try:
        url = (settings.comfy_api.rstrip("/") + "/view?filename=" + urllib.parse.quote(filename)
               + "&subfolder=" + urllib.parse.quote(subfolder) + "&type=output")
        dst.write_bytes(urllib.request.urlopen(url, timeout=120).read())
        return dst
    except Exception:
        return None


def _generate(orig: Path, prompt: str, uid: str, dest: Path) -> Path:
    """同步阻塞的 ComfyUI 生成段(上传 → 跑工作流 → 等结果 → 落盘)。

    整段必须丢到线程池里跑:直接在 async 接口里调会让事件循环冻住 20~50 秒,
    期间所有 /media 图片请求全部挂起,前端结果图就会报「加载失败」。
    """
    try:
        name = _upload(orig)
        seed = random.randint(0, 2 ** 31 - 1)
        pid = comfy_client.submit(_workflow(name, prompt, seed, f"extract_{uid[:8]}"))
        comfy_client.wait(pid, timeout=600)
        st = comfy_client.status(pid)
        for node in (st.get("outputs") or {}).values():
            for img in node.get("images", []):
                out = _save_output(img["filename"], img.get("subfolder") or "", dest)
                if out is not None:
                    return out
        raise RuntimeError("ComfyUI 没有返回图片")
    finally:
        comfy_client.free_memory()


@router.post("")
async def extract(
    file: UploadFile = File(...),
    slot: str = Form("top"),
    desc: str = Form(""),
    user: User = Depends(get_current_user),
) -> dict:
    if (file.content_type or "") not in ALLOWED_MIME:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "只支持 PNG / JPG / WEBP / BMP 图片")
    if slot not in _PROMPTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "slot 只支持 top / bottom")

    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "文件是空的")
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"图片不能超过 {MAX_UPLOAD_MB}MB")

    uid = uuid.uuid4().hex
    rel_dir = f"extracts/{user.id}/{uid}"
    dest = STORAGE_DIR / rel_dir
    dest.mkdir(parents=True, exist_ok=True)

    ext = Path(file.filename or "img.png").suffix.lower() or ".png"
    orig_name = f"original{ext}"
    (dest / orig_name).write_bytes(data)

    try:
        with Image.open(dest / orig_name) as im:
            im.load()
            width, height = im.size
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "无法解析该图片") from exc

    if not comfy_client.is_up():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ComfyUI 没启动(8188)")

    try:
        # 用户给的文字描述能把模型锚到具体那件衣服上(实测:只说「上装」时项链被一起留下,
        # 点名「绿色粗针织毛衣」后配饰就被正确留在人身上)
        prompt = _PROMPTS[slot]
        desc = (desc or "").strip()
        if desc:
            prompt += f" The garment is: {desc}."
        out_path = await asyncio.to_thread(_generate, dest / orig_name, prompt, uid, dest)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"提取失败:{str(exc)[:300]}") from exc

    with Image.open(out_path) as im:
        ow, oh = im.size

    base = f"/media/{rel_dir}"
    return {
        "original": {"url": f"{base}/{orig_name}", "width": width, "height": height},
        "result": {
            "url": f"{base}/{out_path.name}",
            "filename": f"extract_{slot}_{uid[:8]}.png",
            "width": ow,
            "height": oh,
        },
    }
