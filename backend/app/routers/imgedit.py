"""图片编辑:多图 + 一句话 → FLUX3 Klein 指令编辑;支持涂抹 mask 局部编辑。

业界对齐(Nano Banana / Seedream 4.0 玩法):
- 多图参考:用户传 N 张图,文字里用「图1」「图2」指代,后端转成官方 token <ref_image_N>
- 指令编辑:中/英文都行(Qwen3-VL 文本编码器)
- 局部编辑:涂抹 mask → 提示词约束只改涂抹区 → 结果与原图按羽化 mask 混合,
  未涂区域像素级保持原样(避免整幅重绘跑偏)
"""
from __future__ import annotations

import asyncio
import io
import json
import math
import os
import random
import re
import shutil
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from PIL import Image, ImageFilter

from .. import comfy_client
from ..config import STORAGE_DIR, settings
from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/imgedit", tags=["imgedit"])

ALLOWED_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/bmp"}
MAX_UPLOAD_MB = 30
MAX_IMAGES = 6          # 参考图再多会串味,界业界同类产品也限制在个位数

FLUX3_UNET = "flux3-image-klein-int8-convrot.safetensors"
FLUX3_CLIP = "qwen3vl_4b_int8_convrot.safetensors"
FLUX3_VAE = "flux2-vae.safetensors"

# 出图像素量:2.0 -> 3.0(用户 2026-10-22 要求,衣服纹理像素多约 50%,单张慢约 40%)
IMGEDIT_MEGAPIXELS = float(os.getenv("IMGEDIT_MEGAPIXELS", "3.0"))
# 参考图按原始分辨率进模型(不再统一压到出图像素量);只对超大图做安全降采样,防显存爆
REF_MAX_SHORT_SIDE = int(os.getenv("IMGEDIT_REF_MAX_SHORT", "2048"))
# 太小的参考图(底图常常只有几百像素)补到 2MP 再编码 —— 不然参考 token 太粗,脸和身材会被"重新想象"
REF_MIN_MEGAPIXELS = float(os.getenv("IMGEDIT_REF_MIN_MP", "2.0"))


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----imgedit" + uuid.uuid4().hex
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


def _upload_bytes(name: str, data: bytes) -> str:
    body, ctype = _multipart({}, {"image": (name, data)})
    req = urllib.request.Request(
        settings.comfy_api.rstrip("/") + "/upload/image",
        data=body, headers={"Content-Type": ctype}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.loads(r.read().decode())
    fname = res.get("name") or name
    sub = res.get("subfolder") or ""
    return f"{sub}/{fname}" if sub else fname


_REF_RE = re.compile(r"<ref_image_(\d+)>")


def _tokenize(instruction: str, n_images: int, order: list[int] | None = None) -> str:
    """「图1」「@1」→ <ref_image_slot>。

    order[slot] = 用户上传顺序里的下标 —— 底图要挪到第 0 个槽位时,正文里的
    「图1 / 图2」得跟着换成新的槽位号,否则模型会指着错的图。
    """
    order = order or list(range(n_images))

    def _sub(m: re.Match) -> str:
        user_idx = min(int(m.group(1)), n_images) - 1
        return f"<ref_image_{order.index(user_idx)}>"

    return re.sub(r"(?:图\s*|@)([1-9]\d*)", _sub, instruction)


def _detect_base_index(instruction: str, n_images: int) -> int:
    """谁是底图(结果里保留谁的脸 / 身体 / 机位 / 背景)。

    默认第 1 张。但「把图1的衣服穿到图2身上」这种反向换装,底图必须是图2 ——
    以前身份锁写死「以图1为底、不复制其他图的人」,结果永远停在图1那个人身上,
    图1的衣服根本没有挪到图2的身体上(用户报「没把图1的衣服穿到图2身上」)。
    """
    if n_images < 2:
        return 0
    text = _tokenize(instruction, n_images)
    on_body = re.search(
        r"(<ref_image_\d>)[^。;；\n]{0,40}?(?:穿到|换到|套到|披到|穿在|套在)"
        r"[^。;；\n]{0,20}?(<ref_image_\d>)",
        text,
    )
    if on_body:
        return int(_REF_RE.search(on_body.group(2)).group(1))
    wearing = re.search(
        r"(<ref_image_\d>)[^。;；\n]{0,12}?(?:穿上|换上|套上|披上)"
        r"[^。;；\n]{0,20}?(<ref_image_\d>)",
        text,
    )
    if wearing:
        return int(_REF_RE.search(wearing.group(1)).group(1))
    return 0


def _build_prompt(
    instruction: str,
    n_images: int,
    has_mask: bool,
    order: list[int] | None = None,
) -> str:
    """把用户一句话翻译成 FLUX3 Klein 能听懂的提示词。

    实测坑:只写「让图1穿上图2的衣服」会把图2的脸/发型也搬过来。必须显式锁死
    「底图的脸/身材/姿势/背景不动,只改衣服」。换装场景额外加服装保真约束
    (颜色/花纹/面料/版型/长度和参考图一致)。
    """
    order = order or list(range(n_images))
    # 「图1」「@1」→ <ref_image_slot>(底图已按 order 挪到槽位 0)
    text = _tokenize(instruction, n_images, order)
    if "<ref_image_" not in text:
        if n_images == 1:
            text = f"{text}"
        else:
            refs = ", ".join(f"<ref_image_{i}>" for i in range(n_images))
            text = f"{text} (reference images: {refs})"

    # 换装来源图:正文里提到的第一张非底图;没提到就用第 2 张
    mentioned = [int(x) for x in _REF_RE.findall(text)]
    src_slot = next((r for r in mentioned if r != 0), 1 if n_images > 1 else 0)

    # 通用身份锁:槽位 0 是底图,脸/身材/姿势/背景不动
    identity = (
        f"Base everything on <ref_image_0>. Keep the person's face, hairstyle, "
        f"body proportions, pose, camera angle, lighting and background of "
        f"<ref_image_0> completely unchanged. Do not copy the person from any other "
        f"reference image — only use them for the clothes or objects described. "
        f"It is the exact same person as in <ref_image_0>: identical facial features "
        f"(eye shape, nose, lips, brows, face shape), same hair, same skin tone, same "
        f"height, shoulder width, waist, hips, leg length and limb thickness. "
        f"Never beautify, slim, elongate or reshape the body, never change the age, "
        f"and never redraw the head — only the clothing changes."
    )

    # 底图不是用户传的第 1 张(反向换装):把「谁的身体 + 谁的衣服」再说死一遍
    direction = ""
    if order != list(range(n_images)):
        direction = (
            f" The body shown in the result is exactly the body in <ref_image_0>: keep "
            f"its pose, camera angle, framing, skin tone and background unchanged, and "
            f"dress that body in the clothing taken from <ref_image_{src_slot}>. "
            f"The clothing must sit on that body naturally, at the same place a real "
            f"person wears it; do not replace the body with the person in the other "
            f"reference image, do not change the framing."
        )

    # 换装场景:指令里出现「穿/换/套」且有多图参考 → 加服装保真约束
    is_tryon = bool(re.search(r"穿|换|套上|披上|戴上", instruction)) and n_images >= 2
    garment = ""
    if is_tryon:
        garment = (
            f" The new garment must match <ref_image_{src_slot}> exactly in color, collar, "
            "cuffs, buttons, pockets, fit and length — the same shade and saturation as the "
            "reference, neither lighter nor darker. The number, size and position of the "
            "buttons / zipper / drawstring must match the reference exactly: do not add or "
            "remove any of them. Reproduce the material and its surface "
            "texture faithfully: knit stitches, cables and ribbing, lace openwork, weave, "
            "seams and stitching must stay visible and crisp — do not smooth, blur, flatten "
            "or simplify them. Where the reference fabric is plain, keep it plain and even: "
            "do not invent patterns, prints, embossing, sheen or mottling that the reference "
            "does not have. The garment is worn properly on the person's body, not floating. "
            " Everything else she is already wearing in <ref_image_0> — every existing "
            "garment with its color, pattern, fit, length and details — must stay exactly "
            "as in <ref_image_0>: do not recolor, redesign, restyle or replace any of them. "
            "Keep the skin natural too — no airbrushing, no plastic-looking skin. "
            "Do not add accessories (earrings, jewelry, bag, belt) that the instruction "
            "does not ask for."
        )

    # 局部编辑:只改涂抹区
    local = ""
    if has_mask:
        local = (
            " Edit only the painted/marked region. Every pixel outside that region "
            "must remain identical to the original."
        )

    return f"{identity}{direction} {text}.{garment}{local} Photorealistic, natural colors, realistic fabric texture."


def _target_size(base_path: Path, megapixels: float) -> tuple[int, int]:
    """按底图长宽比算输出画布(与底图同比例,像素量 = megapixels)。"""
    with Image.open(base_path) as im:
        w, h = im.size
    scale = math.sqrt(megapixels * 1024 * 1024 / max(1, w * h))
    out_w = max(64, int(round(w * scale / 8)) * 8)
    out_h = max(64, int(round(h * scale / 8)) * 8)
    return out_w, out_h


def _prepare_ref(data: bytes, path: Path) -> tuple[bytes, str]:
    """参考图进模型前的大小:

    - 大于 2.0MP:按原分辨率(衣服细节不再被压掉)
    - 小于 2.0MP:等比补到 2.0MP(小底图的参考 token 太粗,脸和身材会跑)
    - 短边超过 2048:等比降采样(防显存爆)
    """
    ext = path.suffix.lower() or ".png"
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            w, h = im.size
            mp = w * h / (1024.0 * 1024.0)
            target: tuple[int, int] | None = None
            if min(w, h) > REF_MAX_SHORT_SIDE:
                scale = REF_MAX_SHORT_SIDE / min(w, h)
                target = (round(w * scale), round(h * scale))
            elif mp < REF_MIN_MEGAPIXELS:
                scale = math.sqrt(REF_MIN_MEGAPIXELS / max(mp, 1e-6))
                target = (round(w * scale), round(h * scale))
            if target is None:
                return data, ext
            im2 = im.resize(target, Image.LANCZOS)
    except Exception:  # noqa: BLE001  读不了就原样上传,让 ComfyUI 自己报错
        return data, ext
    buf = io.BytesIO()
    im2.save(buf, "PNG")
    return buf.getvalue(), ".png"


def _workflow(
    image_names: list[str],
    prompt: str,
    seed: int,
    prefix: str,
    out_size: tuple[int, int],
) -> dict:
    """FLUX3 Klein 多参考图编辑工作流(接线同官方模板,坑位同 extract.py)。

    参考图用「keep resolution」按原分辨率编码(细节更保真),出图画布由
    EmptyLatentImage 单独控制 —— 这样两者不再绑死,可以把参考图留全、
    输出仍按指定像素量生成。
    """
    refs = {}
    wf = {
        "U": {"class_type": "UNETLoader", "inputs": {
            "unet_name": FLUX3_UNET, "weight_dtype": "default"}},
        "C": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": FLUX3_CLIP, "type": "flux3", "device": "default"}},
        "V": {"class_type": "VAELoader", "inputs": {"vae_name": FLUX3_VAE}},
        "P": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["C", 0]}},
        "N": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["P", 0]}},
        "E": {"class_type": "EmptyLatentImage", "inputs": {
            "width": out_size[0], "height": out_size[1], "batch_size": 1}},
        "K": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": 20, "cfg": 1.0, "sampler_name": "euler",
            "scheduler": "simple", "denoise": 1.0,
            "model": ["U", 0], "positive": ["R", 0], "negative": ["R", 1],
            "latent_image": ["E", 0]}},
        "D": {"class_type": "VAEDecode", "inputs": {"samples": ["K", 0], "vae": ["V", 0]}},
        "S": {"class_type": "SaveImage", "inputs": {"images": ["D", 0], "filename_prefix": prefix}},
    }
    for i, name in enumerate(image_names):
        wf[f"L{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        refs[f"images.image{i}"] = [f"L{i}", 0]
    wf["R"] = {"class_type": "ReferenceMultiLatent", "inputs": {
        "positive": ["P", 0], "negative": ["N", 0], "vae": ["V", 0],
        "resolution": "keep resolution", **refs}}
    return wf


def _save_output(filename: str, subfolder: str, dest_dir: Path) -> Path | None:
    src = Path(settings.comfy_output_dir) / subfolder / filename
    dst = dest_dir / f"edited_{uuid.uuid4().hex[:8]}.png"
    if src.exists():
        shutil.copyfile(src, dst)
        return dst
    try:
        url = (settings.comfy_api.rstrip("/") + "/view?filename=" + urllib.parse.quote(filename)
               + "&subfolder=" + urllib.parse.quote(subfolder) + "&type=output")
        dst.write_bytes(urllib.request.urlopen(url, timeout=120).read())
        return dst
    except Exception:
        return None


def _blend(orig_path: Path, mask_bytes: bytes, result_path: Path) -> None:
    """结果 = 原图*(1-mask) + 生成图*mask,mask 羽化 12px 过渡。

    未涂区域像素级回到原图 —— 模型整幅重绘导致的色偏 / 细节漂移全被挡在涂抹区外。
    """
    with Image.open(orig_path) as o, Image.open(result_path) as r:
        orig = o.convert("RGB").resize(r.size, Image.LANCZOS)
        res = r.convert("RGB")
    mask = Image.open(io.BytesIO(mask_bytes)).convert("L").resize(res.size, Image.LANCZOS)
    mask = mask.filter(ImageFilter.GaussianBlur(12))
    Image.composite(res, orig, mask).save(result_path, "PNG")


def run_imgedit(image_paths: list[Path], instruction: str, mask_bytes: bytes = b"",
                dest_dir: Path | None = None, prefix: str = "imgedit") -> Path:
    """同步跑 FLUX3 Klein imgedit,返回输出 PNG 路径。HTTP 端点和 pipeline 共用。

    调用方负责:
    - 提前 comfy_client.is_up() 检查 / 自己处理异常
    - 放到线程池里跑(asyncio.to_thread / worker 线程)—— 全程阻塞 20~90s

    mask_bytes 非空时,结果按羽化 mask 和 image_paths[0] 混合,未涂区域像素级回到原图。
    """
    if not image_paths:
        raise ValueError("image_paths 不能为空")
    if dest_dir is None:
        dest_dir = STORAGE_DIR / "imgedits" / "_pipeline"
    dest_dir.mkdir(parents=True, exist_ok=True)

    datas: list[bytes] = []
    names: list[str] = []
    # 底图要占槽位 0(输出尺寸 / 构图跟着它走)。反向换装(把图1的衣服穿到图2身上)
    # 时底图是图2,所以按 order 重新排一遍上传顺序,正文里的 @1/@2 同步换槽位号。
    # 涂抹 mask 时永远是第 1 张当底图(mask 是画在它上面的)。
    n = len(image_paths)
    base = 0 if mask_bytes else _detect_base_index(instruction, n)
    order = [base] + [i for i in range(n) if i != base]
    for slot, user_idx in enumerate(order):
        p = image_paths[user_idx]
        data = p.read_bytes()
        if not data:
            raise ValueError(f"第 {user_idx + 1} 张图是空的: {p}")
        ext = p.suffix.lower() or ".png"
        # 落一份到 dest_dir 方便事后排查 / 复用 _blend 找原图(留原图,不存降采样版)
        (dest_dir / f"src{slot}{ext}").write_bytes(data)
        up_data, up_ext = _prepare_ref(data, p)
        datas.append(up_data)
        names.append(_upload_bytes(f"src{slot}{up_ext}", up_data))

    # 出图画布按底图长宽比 + IMGEDIT_MEGAPIXELS 算;参考图各自按原分辨率编码
    out_size = _target_size(image_paths[order[0]], IMGEDIT_MEGAPIXELS)
    prompt = _build_prompt(instruction, len(names), bool(mask_bytes), order)
    seed = random.randint(0, 2 ** 31 - 1)
    pid = comfy_client.submit(
        _workflow(names, prompt, seed, f"{prefix}_{uuid.uuid4().hex[:8]}", out_size)
    )
    try:
        comfy_client.wait(pid, timeout=600)
        st = comfy_client.status(pid)
        out: Path | None = None
        for node in (st.get("outputs") or {}).values():
            for img in node.get("images", []):
                out = _save_output(img["filename"], img.get("subfolder") or "", dest_dir)
                if out is not None:
                    break
            if out is not None:
                break
        if out is None:
            raise RuntimeError("ComfyUI 没有返回图片")
        if mask_bytes:
            _blend(image_paths[0], mask_bytes, out)
        return out
    finally:
        comfy_client.free_memory()


@router.post("")
async def imgedit(
    files: list[UploadFile] = File(...),
    instruction: str = Form(""),
    mask: UploadFile | None = File(None),
    user: User = Depends(get_current_user),
) -> dict:
    if not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "至少传一张图")
    if len(files) > MAX_IMAGES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"最多 {MAX_IMAGES} 张图")
    instruction = (instruction or "").strip()
    if not instruction:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "写一句要做什么")
    for f in files:
        if (f.content_type or "") not in ALLOWED_MIME:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "只支持 PNG / JPG / WEBP / BMP 图片")

    if not comfy_client.is_up():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ComfyUI 没启动(8188)")

    uid = uuid.uuid4().hex
    rel_dir = f"imgedits/{user.id}/{uid}"
    dest = STORAGE_DIR / rel_dir
    dest.mkdir(parents=True, exist_ok=True)

    # 先把上传的图落盘 —— 之后交给 run_imgedit 走文件路径,不再传 bytes
    src_paths: list[Path] = []
    for i, f in enumerate(files):
        data = await f.read()
        if not data:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"第 {i + 1} 张图是空的")
        if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"图片不能超过 {MAX_UPLOAD_MB}MB")
        ext = Path(f.filename or "img.png").suffix.lower() or ".png"
        p = dest / f"src{i}{ext}"
        p.write_bytes(data)
        src_paths.append(p)

    mask_bytes = await mask.read() if mask is not None and mask.filename else b""

    try:
        out_path = await asyncio.to_thread(
            run_imgedit, src_paths, instruction, mask_bytes, dest, f"imgedit_{uid[:8]}"
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"编辑失败:{str(exc)[:300]}") from exc

    with Image.open(out_path) as im:
        ow, oh = im.size
    base = f"/media/{rel_dir}"
    return {
        "result": {
            "url": f"{base}/{out_path.name}",
            "filename": f"edited_{uid[:8]}.png",
            "width": ow,
            "height": oh,
        },
    }
