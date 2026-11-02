"""把生成出来的低分辨率片段超分到交付分辨率(ESRGAN)。

复用本机 ComfyUI 已装的 upscale 模型,走这条链:
    VHS_LoadVideoPath -> UpscaleModelLoader -> ImageUpscaleWithModel
    -> ImageScale(交付尺寸) -> ImageBlend(混回 Lanczos) -> VHS_VideoCombine

为什么不塞进生成工作流里: 超分放在采样之后单独跑,不和视频模型同时占显存,
炸了也只是退回原片,不会把整条镜头判失败。

**按帧分块**(2026-10-20): ImageUpscaleWithModel 会把整批帧一次性变成 4 倍大的张量。
576x1024 的 144 帧 → 2304x4096x3x4B ≈ 16.3GB,32GB 内存的机器直接
`DefaultCPUAllocator: not enough memory`。现在按内存预算把片段切成若干段,
一段段超分再拼回去 —— 峰值只有一块,画质和一次性超分一样。
"""
from __future__ import annotations

import uuid
from fractions import Fraction
from pathlib import Path

import av

from . import comfy_client
from .config import settings
from .threeview import _upload


def _comfy_input_dir() -> Path:
    """ComfyUI 的 input 目录(和 output 同级),VHS_LoadVideoPath 只认这下面的文件。"""
    return Path(settings.comfy_output_dir).parent / "input"


def _probe(src: Path) -> tuple[int, int]:
    """读原片的(总帧数, 帧率)。读不到帧数就解码数一遍。"""
    with av.open(str(src)) as c:
        s = c.streams.video[0]
        fps = int(round(float(s.average_rate or settings.fps) or settings.fps))
        if s.frames:
            return int(s.frames), fps
        return sum(1 for _ in c.decode(s)), fps


def _chunk_frames(width: int, height: int) -> int:
    """一段最多几帧:按 4 倍放大后的单帧张量大小,卡在 ~2.5GB 以内。"""
    if settings.upscale_chunk > 0:
        return settings.upscale_chunk
    per_frame = (int(width) * 4) * (int(height) * 4) * 3 * 4      # float32
    return max(1, int((2.5 * 1024 ** 3) // max(1, per_frame)))


def _workflow(in_path: Path, width: int, height: int, fps: int,
              cap: int, skip: int, prefix: str) -> dict:
    return {
        "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": str(in_path), "force_rate": 0,
            "custom_width": 0, "custom_height": 0,
            "frame_load_cap": int(cap), "skip_first_frames": int(skip),
            "select_every_nth": 1}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {
            "model_name": settings.upscale_model}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {
            "upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "ImageScale", "inputs": {
            "image": ["3", 0], "upscale_method": "lanczos",
            "width": int(width), "height": int(height), "crop": "disabled"}},
        # 原片直接 Lanczos 放大一份,再按 upscale_blend 混回去:
        # ESRGAN 单跑会明显加锐(皮肤发塑料、边缘白边),混一点普通放大能压住。
        "6": {"class_type": "ImageScale", "inputs": {
            "image": ["1", 0], "upscale_method": "lanczos",
            "width": int(width), "height": int(height), "crop": "disabled"}},
        "7": {"class_type": "ImageBlend", "inputs": {
            "image1": ["4", 0], "image2": ["6", 0],
            "blend_factor": float(settings.upscale_blend), "blend_mode": "normal"}},
        "5": {"class_type": "VHS_VideoCombine", "inputs": {
            "images": ["7", 0], "frame_rate": int(fps), "loop_count": 0,
            "filename_prefix": prefix, "format": "video/h264-mp4",
            "pingpong": False, "save_output": True}},
    }


def _concat(parts: list[Path], dst: Path, fps: int) -> None:
    """把几段超分好的 mp4 按顺序拼成一条(逐帧重编码,帧数和时长不变)。

    每帧必须显式给 pts(单位 1/fps) —— 直接沿用解码出来的 pts,编码器会按两套
    time_base 叠乘,时间戳乱掉(实测 144 帧拼出来变成 3456fps、时长 0.04 秒)。
    """
    with av.open(str(parts[0])) as c0:
        first = c0.streams.video[0]
        w, h = first.width, first.height
    tb = Fraction(1, int(fps))
    with av.open(str(dst), "w") as out:
        st = out.add_stream("libx264", rate=int(fps))
        st.width, st.height = w, h
        st.pix_fmt = "yuv420p"
        st.options = {"crf": "18", "preset": "veryfast"}
        n = 0
        for p in parts:
            with av.open(str(p)) as c:
                for fr in c.decode(video=0):
                    fr.pts = n
                    fr.time_base = tb
                    n += 1
                    for pkt in st.encode(fr):
                        out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)


def upscale_clip(src: Path, dst: Path, width: int, height: int, fps: int) -> bool:
    """把 src 超分到 width×height 写到 dst;失败返回 False(调用方退回原片)。"""
    if not src.exists() or not comfy_client.is_up():
        return False

    # 通过 ComfyUI 的 /upload/image 把视频交给它自己落到 input 目录:
    # 后端进程没有写 ComfyUI 目录的权限,自己 copy 会 Permission denied。
    try:
        name = _upload(src)
    except Exception:  # noqa: BLE001
        return False
    in_path = _comfy_input_dir() / name

    total, src_fps = _probe(src)
    if total <= 0:
        return False
    out_fps = int(fps) or src_fps

    # 生成片段是这种尺寸;按它算分块(交付尺寸更大,但超分前只有生成尺寸这么大)
    with av.open(str(src)) as c:
        s = c.streams.video[0]
        gw, gh = s.width, s.height
    chunk = max(1, min(total, _chunk_frames(gw, gh)))

    parts: list[Path] = []
    start = 0
    while start < total:
        cap = min(chunk, total - start)
        prefix = f"ovs_up_{uuid.uuid4().hex[:10]}"
        pid = comfy_client.submit(_workflow(in_path, width, height, out_fps,
                                            cap, start, prefix))
        comfy_client.wait(pid, timeout=1800)
        out_file = comfy_client.newest_output(prefix)
        if out_file is None:
            return False
        parts.append(out_file)
        start += cap

    dst.parent.mkdir(parents=True, exist_ok=True)
    if len(parts) == 1:
        dst.write_bytes(parts[0].read_bytes())
    else:
        _concat(parts, dst, out_fps)
    return True
