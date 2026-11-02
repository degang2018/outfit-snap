"""画布「三视图」节点:平铺服装 / 穿着图 -> 正面、侧面、背面三视图。

走本机 ComfyUI 的 Qwen-Image-Edit-2511 GGUF 工作流。生成一张 2:1 的三视图
(也可按需裁成三张),结果写回画布节点的 custom_nodes 里。

任务状态放在进程内存里(本机单进程 dev server),出图后再同步到 canvas_state,
前端轮询 /canvas/threeview/status 即可。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from PIL import Image

from . import comfy_client
from .config import STORAGE_DIR, settings
from .db import SessionLocal
from .models import Project

QWEN_UNET = os.getenv("QWEN_EDIT_UNET", "qwen-image-edit-2511-Q5_K_S.gguf")
QWEN_CLIP = os.getenv("QWEN_EDIT_CLIP", "qwen_2.5_vl_7b_fp8_scaled.safetensors")
QWEN_VAE = os.getenv("QWEN_EDIT_VAE", "qwen_image_vae.safetensors")
OUT_DIR = STORAGE_DIR / "canvas" / "threeview"

# (project_id, node_id) -> {status, message, outputs, error}
JOBS: dict[tuple[int, str], dict] = {}
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 提示词
def _common_tail(mannequin: bool, full_body: bool = False, head: bool = False) -> str:
    tail = (
        "生成同一件（套）服装的三视图：左边正面、中间侧面、右边背面；"
        "三个视图并排、比例一致、间距均匀、脚底在同一条水平线上。"
        "纯白背景，柔和均匀的棚拍光线，无阴影、无文字、无水印、无多余道具。"
        "三张视图必须是同一件服装：版型、长度、颜色、图案、印花位置和面料纹理完全一致，"
        "不要改设计、不要加图里没有的衣服或配饰。"
        "每件衣服的领口、门襟、袖型、下摆都要完整保留，不要把两件衣服合并成一件，"
        "也不要只画出袖子或半件。图案（格纹的格距、颜色、线条粗细、蕾丝花纹）"
        "必须和参考图完全一致，不要重画、简化或换配色。"
        "按部位穿对：内搭穿在上身、下装穿在下半身、外套穿在最外层、鞋穿在脚上；"
        "参考图里的每一件都要出现在画面里，一件都不能少。"
    )
    if mannequin and full_body:
        if head:
            tail += (
                "使用白色全身人台：人台有头部和脖子（头部是光滑的白色、没有五官，不要眼睛、鼻子、嘴，不要头发），"
                "有双臂、双腿和双脚，直立站立，从头顶到脚底完整入画，不要裁掉头、脚和鞋子。"
                "如果输入图里有鞋，把鞋穿在人台双脚上，鞋里直接是人台的脚，鞋口保持参考图的样子；"
                "鞋的款式、颜色和朝向在三个视图里保持一致（正面朝前、侧面朝侧、背面朝后）。"
                "不要真人模特，脸部保持空白。"
            )
        else:
            tail += (
                "使用白色无脸全身人台，人台有双腿和双脚、直立站立，从肩颈到脚底完整入画，"
                "不要裁掉脚部和鞋子。如果输入图里有鞋，把鞋穿在人台双脚上，"
                "鞋里直接是人台的脚，鞋口保持参考图的样子；"
                "鞋的款式、颜色和朝向在三个视图里保持一致（正面朝前、侧面朝侧、背面朝后）。"
                "不要真人模特、不要露脸。"
            )
    elif mannequin:
        tail += (
            "使用白色无脸半身服装人台：人台只有肩颈到胯部的躯干，"
            "下面是一根细支杆和圆形底座，画面到裙摆下缘为止。"
            "不要真人模特、不要露脸。"
        )
    return tail


def _person_tail() -> str:
    return (
        "生成同一个人的全身三视图：左边正面、中间侧面、右边背面；"
        "三个视图并排、比例一致、间距均匀、脚底在同一条水平线上。"
        "三张里必须是同一个人：脸、五官、发型、肤色、身材比例完全一致；三张里也是同一套衣服。"
        "每件衣服的领口、门襟、袖型、下摆都要完整保留，不要把两件衣服合并成一件；"
        "图案（格纹的格距、颜色、线条粗细、蕾丝花纹）必须和参考图完全一致，不要重画或换配色。"
        "按部位穿对：内搭穿在上身、下装穿在下半身、外套穿在最外层、鞋穿在脚上；"
        "每一件都要出现在画面里，一件都不能少。"
        "全身从头到脚完整入画，头顶留少量空间，双腿、双脚和鞋子都要完整可见，不要裁掉腿或脚。"
        "纯白背景，柔和均匀的棚拍光线，无阴影、无文字、无水印、无多余道具。"
        "不要添加图里没有的衣物或配饰。"
    )


def _garment_labels(urls: list[str]) -> list[str]:
    """按素材库里的 ai_name / ai_kind 给每件衣服起个名字,拼图时告诉模型每个格子是什么。"""
    from .db import SessionLocal
    from .models import Asset

    out: list[str] = []
    with SessionLocal() as db:
        for url in urls:
            rel = url[len("/media/"):] if isinstance(url, str) and url.startswith("/media/") else ""
            asset = db.query(Asset).filter(Asset.rel_path == rel).first() if rel else None
            if asset is None:
                out.append("一件服装")
                continue
            name = (asset.ai_name or "").strip() or "一件服装"
            kind = (asset.ai_kind or "").strip()
            out.append(f"{name}·{kind}" if kind else name)
    return out


def build_prompt(opts: dict, n_imgs: int, sheet: int = 0,
                 sheet_items: list[str] | None = None) -> str:
    """按「来源 / 主体 / 是否每件单独」拼一段中文提示词。

    sheet > 0 表示传入的是一张把多件单品拼在一起的参考图(Qwen 单次最多 3 张)。
    opts["mode"] == "person" 是「人物穿着三视图」：图1=人物,后面=服装。
    """
    source = opts.get("source") or "garment"
    mannequin = (opts.get("subject") or "mannequin") == "mannequin"
    full_body = mannequin and bool(opts.get("full_body"))
    head = full_body and opts.get("head", True) is not False
    if full_body and head:
        form = "白色全身人台（有头部、脖子、双臂、双腿和双脚，头部没有五官）"
    elif full_body:
        form = "白色无脸全身人台（有双腿和双脚）"
    else:
        form = "白色无脸半身人台（只有躯干，下面是一根支杆）"
    n = max(1, min(3, n_imgs))
    labels = list(sheet_items or [])

    def _pic(idx: int, label_i: int) -> str:
        label = labels[label_i] if 0 <= label_i < len(labels) else ""
        return f"图{idx}（{label}）" if label else f"图{idx}"

    pics = "、".join(_pic(i + 1, i) for i in range(n))
    extra = (opts.get("prompt") or "").strip()
    if opts.get("mode") == "person":
        if sheet:
            items = "、".join(sheet_items or [])
            layout = (f"图2是一张拼图，从左到右、从上到下依次是：{items}。" if items
                      else f"图2这张拼图里是同一套穿搭的全部服装（共 {sheet} 件）。")
            wear = "让她把这些单品全部穿上，一件都不要遗漏，也不要把几件合并成一件；"
        else:
            layout = ""
            wear = ("让她穿上" + "、".join(_pic(i + 2, i) for i in range(max(1, n - 1)))
                    + " 里的服装，一件都不要遗漏，也不要把几件合并成一件；")
        head = (
            "以图1中的人物为准：脸、五官、发型、肤色和身材比例保持完全一致，"
            "不要换脸、不要换发型、不要改变身材比例；"
            + layout + wear
            + "保留每件衣服的版型、长度、颜色、图案和面料纹理；"
            "不要添加图里没有的衣物或配饰。"
        )
        return head + _person_tail() + (("\n补充要求：" + extra) if extra else "")
    if sheet and mannequin:
        items = "、".join(sheet_items or [])
        layout = f"图1是一张拼图，从左到右、从上到下依次是：{items}。" if items else \
            f"图1是一张拼图，里面是同一套穿搭的全部单品（共 {sheet} 件，含上装、下装和鞋）。"
        head = (
            layout +
            f"把这些单品全部原样穿在{form}上，一件都不要遗漏，也不要把几件合并成一件；"
            "保留每件的版型、长度、颜色、图案和面料纹理；不要添加图里没有的衣物或配饰。"
        )
    elif source == "worn":
        if mannequin:
            head = (
                f"把{pics}里人物身上的整套服装原样穿到{form}上，"
                "保留服装的版型、长度、颜色、图案和面料纹理；"
                "不要保留人物本人，也不要添加图里没有的衣物或配饰。"
            )
        else:
            head = (
                f"以{pics}中的人物和服装为准，生成这个人的三视图；"
                "人物必须是同一个人：脸、发型、肤色、身材、服装细节在三张里完全一致。"
            )
    else:
        if mannequin:
            head = (
                f"把{pics}里的服装原样穿在{form}上，"
                "保留版型、长度、颜色、图案和面料纹理；不要添加图里没有的衣物或配饰。"
            )
        else:
            head = (
                f"以{pics}里的服装为准，让同一个真人模特穿上这套服装，生成她的三视图；"
                "人物的脸、发型、身材、服装细节在三张里完全一致。"
            )
    extra = (opts.get("prompt") or "").strip()
    return head + _common_tail(mannequin, full_body, head) + (("\n补充要求：" + extra) if extra else "")


# ---------------------------------------------------------------- ComfyUI 交互
def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = "----ovs" + uuid.uuid4().hex
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


def _workflow(names: list[str], prompt: str, seed: int, prefix: str) -> dict:
    wf = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": QWEN_UNET}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": QWEN_CLIP, "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": QWEN_VAE}},
        "4": {"class_type": "LoadImage", "inputs": {"image": names[0]}},
        "5": {"class_type": "ImagePadForOutpaint", "inputs": {
            "image": ["4", 0], "left": 320, "top": 0, "right": 320,
            "bottom": 0, "feathering": 24}},
        "6": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["5", 0]}},
        "7": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "image1": ["6", 0], "prompt": prompt}},
        "8": {"class_type": "TextEncodeQwenImageEditPlus", "inputs": {
            "clip": ["2", 0], "vae": ["3", 0], "image1": ["6", 0], "prompt": ""}},
        "9": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["7", 0], "reference_latents_method": "index_timestep_zero"}},
        "10": {"class_type": "FluxKontextMultiReferenceLatentMethod", "inputs": {
            "conditioning": ["8", 0], "reference_latents_method": "index_timestep_zero"}},
        "11": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["1", 0], "shift": 3.1}},
        "12": {"class_type": "CFGNorm", "inputs": {"model": ["11", 0], "strength": 1.0}},
        "13": {"class_type": "VAEEncode", "inputs": {"pixels": ["6", 0], "vae": ["3", 0]}},
        "14": {"class_type": "KSampler", "inputs": {
            "model": ["12", 0], "positive": ["9", 0], "negative": ["10", 0],
            "latent_image": ["13", 0], "seed": seed, "steps": 20, "cfg": 4.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "15": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["3", 0]}},
        "16": {"class_type": "SaveImage", "inputs": {
            "images": ["15", 0], "filename_prefix": prefix}},
    }
    # 第二、三张参考图(可选)
    for i, name in enumerate(names[1:3], start=2):
        nid = str(20 + i)
        wf[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        wf["7"]["inputs"][f"image{i}"] = [nid, 0]
        wf["8"]["inputs"][f"image{i}"] = [nid, 0]
    return wf


def _local_url_to_path(url: str) -> Path | None:
    if isinstance(url, str) and url.startswith("/media/"):
        p = STORAGE_DIR / url[len("/media/"):]
        return p if p.exists() else None
    return None


def _save_output(filename: str, subfolder: str) -> Path | None:
    src = Path(settings.comfy_output_dir) / subfolder / filename
    if not src.exists():
        # 输出目录可能被改成别的路径,再走 HTTP 兜底
        try:
            url = (settings.comfy_api.rstrip("/") + "/view?filename=" + urllib.parse.quote(filename)
                   + "&subfolder=" + urllib.parse.quote(subfolder) + "&type=output")
            data = urllib.request.urlopen(url, timeout=120).read()
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            dst = OUT_DIR / f"{uuid.uuid4().hex}.png"
            dst.write_bytes(data)
            return dst
        except Exception:
            return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dst = OUT_DIR / f"{uuid.uuid4().hex}{src.suffix or '.png'}"
    shutil.copyfile(src, dst)
    return dst


def _split_sheet(path: Path) -> list[Path]:
    out = []
    with Image.open(path) as im:
        w, h = im.size
        step = w // 3
        for i in range(3):
            box = (i * step, 0, (i + 1) * step if i < 2 else w, h)
            p = OUT_DIR / f"{uuid.uuid4().hex}.png"
            im.crop(box).save(p, "PNG")
            out.append(p)
    return out


MAX_IMAGES = 3      # Qwen 单次只吃 3 张参考图;超过 3 张会串味,前端也限制在 3 张


def _stitch(paths: list[Path]) -> Path:
    """把多件单品的图拼成一张白底参考图(2 列,从左到右、从上到下)。

    ComfyUI 的 TextEncodeQwenImageEditPlus 只有 image1..image3 三个入口,
    超过 3 件时先拼成一张再当 image1 传进去。
    """
    cell, gap = 512, 16
    cols = 2
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new(
        "RGB",
        (cols * cell + (cols - 1) * gap, rows * cell + (rows - 1) * gap),
        (255, 255, 255),
    )
    for i, p in enumerate(paths):
        with Image.open(p) as im:
            im = im.convert("RGBA")
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            im = Image.alpha_composite(bg, im).convert("RGB")
        im.thumbnail((cell - 40, cell - 40), Image.LANCZOS)
        r, c = divmod(i, cols)
        sheet.paste(im, (
            c * (cell + gap) + (cell - im.width) // 2,
            r * (cell + gap) + (cell - im.height) // 2,
        ))
    tmp = OUT_DIR / "_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    dst = tmp / f"sheet_{uuid.uuid4().hex}.png"
    sheet.save(dst, "PNG")
    return dst


def _set_node(project_id: int, node_id: str, **fields) -> None:
    with SessionLocal() as db:
        p = db.get(Project, project_id)
        if p is None:
            return
        state = dict(p.canvas_state or {})
        nodes = [dict(n) for n in (state.get("custom_nodes") or [])]
        for n in nodes:
            if n.get("id") == node_id:
                n.update(fields)
        state["custom_nodes"] = nodes
        p.canvas_state = state
        db.commit()


def _run(project_id: int, node_id: str, sources: list[str], opts: dict) -> None:
    key = (project_id, node_id)
    with _LOCK:
        try:
            per_piece = bool(opts.get("per_piece")) and len(sources) > 1 and opts.get("mode") != "person"
            if opts.get("mode") == "person":
                batches = [sources[:MAX_IMAGES]]
            else:
                batches = [[s] for s in sources] if per_piece else [sources[:MAX_IMAGES]]
            total = len(batches)
            outputs: list[str] = []
            for bi, batch in enumerate(batches, start=1):
                JOBS[key] = {"status": "running", "message": f"正在生成第 {bi}/{total} 张…",
                             "outputs": outputs, "error": ""}
                _set_node(project_id, node_id, status="running",
                          message=f"正在生成第 {bi}/{total} 张…")
                names = []
                sheet_n = 0
                sheet_items: list[str] = []
                if opts.get("mode") == "person":
                    # 图1 = 人物,图2/图3 = 服装;服装超过 2 件时拼成一张
                    person_p = _local_url_to_path(batch[0])
                    if person_p is None:
                        raise RuntimeError(f"找不到人物图: {batch[0]}")
                    names = [_upload(person_p)]
                    gpaths = []
                    for url in batch[1:]:
                        p = _local_url_to_path(url)
                        if p is None:
                            raise RuntimeError(f"找不到输入图: {url}")
                        gpaths.append(p)
                    if len(gpaths) > 2:
                        sheet = _stitch(gpaths)
                        try:
                            names.append(_upload(sheet))
                            sheet_n = len(gpaths)
                            sheet_items = _garment_labels(batch[1:])
                        finally:
                            try:
                                sheet.unlink()
                            except OSError:
                                pass
                    else:
                        for p in gpaths:
                            names.append(_upload(p))
                        sheet_items = _garment_labels(batch[1:])
                elif len(batch) > 3:
                    paths = []
                    for url in batch:
                        p = _local_url_to_path(url)
                        if p is None:
                            raise RuntimeError(f"找不到输入图: {url}")
                        paths.append(p)
                    sheet = _stitch(paths)
                    try:
                        names = [_upload(sheet)]
                        sheet_n = len(paths)
                        sheet_items = _garment_labels(batch)
                    finally:
                        try:
                            sheet.unlink()
                        except OSError:
                            pass
                else:
                    for url in batch:
                        p = _local_url_to_path(url)
                        if p is None:
                            raise RuntimeError(f"找不到输入图: {url}")
                        names.append(_upload(p))
                    sheet_items = _garment_labels(batch)
                prompt = build_prompt(opts, len(names), sheet_n, sheet_items)
                seed = int(time.time() * 1000) % (2 ** 31 - 1)
                prefix = f"threeview_{node_id[:8]}_{bi}"
                pid = comfy_client.submit(_workflow(names, prompt, seed, prefix))
                comfy_client.wait(pid, timeout=1800)
                st = comfy_client.status(pid)
                files = []
                for node in (st.get("outputs") or {}).values():
                    for img in node.get("images", []):
                        p = _save_output(img["filename"], img.get("subfolder") or "")
                        if p is not None:
                            files.append(p)
                if not files:
                    raise RuntimeError("ComfyUI 没有返回图片")
                if opts.get("split"):
                    for f in files:
                        for part in _split_sheet(f):
                            outputs.append(f"/media/{part.relative_to(STORAGE_DIR).as_posix()}")
                else:
                    for f in files:
                        outputs.append(f"/media/{f.relative_to(STORAGE_DIR).as_posix()}")
            _set_node(project_id, node_id, status="done", message="", outputs=outputs, error="")
            JOBS[key] = {"status": "done", "message": "", "outputs": outputs, "error": ""}
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:500]
            _set_node(project_id, node_id, status="failed", message="", error=msg)
            JOBS[key] = {"status": "failed", "message": "", "outputs": [], "error": msg}
        finally:
            comfy_client.free_memory()


def start(project_id: int, node: dict) -> dict:
    node_id = str(node.get("id") or "")
    opts = dict(node.get("opts") or {})
    if opts.get("mode") == "person":
        # 人物穿着三视图:图1 必须是人物的照片,后面才是要穿的衣服
        person = str(opts.get("person") or "")
        garments = [s for s in (node.get("images") or []) if isinstance(s, str) and s]
        if not person:
            raise ValueError("还没有人物照片：先上传一张人物的照片")
        if not garments:
            raise ValueError("还没有要穿的服装：至少上传一件衣服")
        if len(garments) > 2:
            raise ValueError("人物三视图最多 2 件服装（人物占 1 张，共 3 张参考图）")
        sources = [person] + garments
    else:
        sources = [s for s in (node.get("images") or []) if isinstance(s, str) and s]
        if not sources:
            raise ValueError("三视图节点还没有输入图：先上传或从左边连线接一张")
        if len(sources) > MAX_IMAGES:
            raise ValueError(f"三视图最多 {MAX_IMAGES} 张参考图，请先删掉多余的")
    if not comfy_client.is_up():
        raise ValueError("ComfyUI 没启动（127.0.0.1:8188）")
    if (JOBS.get((project_id, node_id)) or {}).get("status") in ("queued", "running"):
        raise ValueError("这个三视图节点正在生成，等它跑完再点")
    JOBS[(project_id, node_id)] = {
        "status": "queued", "message": "排队中…", "outputs": [], "error": "",
    }
    _set_node(project_id, node_id, status="queued", message="排队中…", error="", outputs=[])
    threading.Thread(target=_run, args=(project_id, node_id, sources, opts), daemon=True).start()
    return JOBS[(project_id, node_id)]


def status(project_id: int, node_id: str) -> dict:
    job = JOBS.get((project_id, node_id))
    if job:
        return job
    return {"status": "", "message": "", "outputs": [], "error": ""}
