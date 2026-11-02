"""Thin client for the local ComfyUI HTTP API."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Optional

from .config import settings


class ComfyError(RuntimeError):
    pass


# 远端 ComfyUI 生成的文件先下到这里,再由上层（pipeline / preview）搬到 storage。
_CACHE_DIR = Path(tempfile.gettempdir()) / "fitflick_comfy"


def _req(path: str, payload: dict | None = None, timeout: int = 60):
    url = settings.comfy_api.rstrip("/") + path
    if payload is None:
        req = urllib.request.Request(url, method="GET")
    else:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body) if body else {}


def system_stats() -> dict:
    return _req("/system_stats")


def is_up() -> bool:
    try:
        system_stats()
        return True
    except Exception:
        return False


def submit(workflow: dict) -> str:
    try:
        res = _req("/prompt", {"prompt": workflow}, timeout=120)
    except urllib.error.HTTPError as e:
        raise ComfyError(f"ComfyUI 拒绝工作流: {e.read().decode()[:800]}") from e
    except Exception as e:
        raise ComfyError(f"无法连接 ComfyUI({settings.comfy_api}): {e}") from e
    pid = res.get("prompt_id")
    if not pid:
        raise ComfyError(f"ComfyUI 未返回 prompt_id: {res}")
    return pid


def status(prompt_id: str) -> dict:
    try:
        hist = _req(f"/history/{prompt_id}")
    except Exception:
        return {}
    return hist.get(prompt_id, {})


def _in_queue(prompt_id: str) -> bool:
    """这一炉还在 ComfyUI 的队列里(正在跑或等着)?"""
    try:
        q = _req("/queue", timeout=10)
    except Exception:
        return True  # 查不到就别乱判"丢了"
    for item in (q.get("queue_running") or []) + (q.get("queue_pending") or []):
        if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
            return True
    return False


def wait(prompt_id: str, timeout: int | None = None, on_tick=None) -> None:
    """Block until the prompt finishes (or raises)."""
    timeout = timeout or settings.shot_timeout
    t0 = time.time()
    misses = 0
    while time.time() - t0 < timeout:
        st = status(prompt_id)
        if st:
            state = st.get("status", {})
            if state.get("completed"):
                return
            if state.get("status_str") == "error":
                raise ComfyError(f"ComfyUI 执行失败: {json.dumps(state)[:600]}")
        else:
            # 既不在历史、又不在队列 = 这一炉已经不在 ComfyUI 手里了(它重启了 / 被中断了)。
            # 不判这一条的话,这里会一直空转到 30 分钟超时 —— 渲染线程跟着卡住,
            # 后面排队的所有任务都不会有人接手(界面就永远停在"排队中")。实测踩过。
            if not _in_queue(prompt_id):
                misses += 1
                if misses >= 3:  # 连着 3 次(约 15 秒)都找不到,基本可以确定丢了
                    raise ComfyError(
                        f"任务在 ComfyUI 里已丢失(可能重启或被中断): {prompt_id}"
                    )
            else:
                misses = 0
        if on_tick:
            on_tick(int(time.time() - t0))
        time.sleep(5)
    raise ComfyError(f"镜头生成超时({timeout}s)")


def interrupt() -> None:
    try:
        _req("/interrupt", {}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def queue_state() -> dict:
    """ComfyUI 当前队列:queue_running / queue_pending。"""
    return _req("/queue", timeout=10)


def queue_delete(prompt_ids: list[str]) -> None:
    """从 ComfyUI 队列里删掉还没开始跑的条目。"""
    _req("/queue", {"delete": list(prompt_ids)}, timeout=10)


def free_memory() -> None:
    """让 ComfyUI 卸载已缓存的模型、释放显存和内存。

    必须做:预览图用的是 SDXL,它一旦留在显存里,接着跑 MiniMax H3 的视频渲染就会
    因为显存不够而在读模型分片时报 "HostBuffer.read_file_slice failed"。
    """
    try:
        _req("/free", {"unload_models": True, "free_memory": True}, timeout=60)
    except Exception:
        pass


def upload_image(local: Path) -> str:
    """把本地图片传给 ComfyUI 的 input 目录,返回工作流里能直接引用的文件名。

    后端和 ComfyUI 不在同一台机器时,工作流里不能写本机绝对路径 ——
    远端会因为「Invalid file path」直接拒绝整条工作流。
    """
    local = Path(local)
    blob = local.read_bytes()
    # 文件名带内容哈希:不同目录下同名图(比如各项目的锚图)不会在 ComfyUI 的
    # input 目录里互相覆盖;同一张图重复上传也只是覆盖同名同内容的文件。
    name = hashlib.md5(blob).hexdigest()[:10] + "_" + local.name
    boundary = "----fitflick" + uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + blob
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        settings.comfy_api.rstrip("/") + "/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        raise ComfyError(f"上传图片到 ComfyUI 失败({local.name}): {exc}") from exc
    name = res.get("name") or local.name
    sub = res.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


def _history(limit: int = 200) -> dict:
    try:
        return _req(f"/history?max_items={limit}", timeout=30)
    except Exception:
        return {}


def _match_output(prefix: str, suffixes: tuple[str, ...]) -> Optional[dict]:
    """在 ComfyUI 历史里找 filename 以 `prefix_` 开头、后缀匹配的最新一条输出。"""
    hits = []
    for item in _history().values():
        for node_out in (item.get("outputs") or {}).values():
            for group in ("gifs", "videos", "images"):
                for f in node_out.get(group) or []:
                    name = (f.get("filename") or "").lower()
                    if name.startswith(prefix.lower() + "_") and name.endswith(
                        suffixes
                    ):
                        hits.append(f)
    if not hits:
        return None
    # VHS 的计数器是固定 5 位补零,按文件名字典序排就是生成顺序
    hits.sort(key=lambda f: f.get("filename") or "")
    return hits[-1]


def _download(hit: dict) -> Optional[Path]:
    """按 /view 把远端输出拉到本地缓存目录。"""
    try:
        url = (
            settings.comfy_api.rstrip("/")
            + "/view?filename="
            + urllib.parse.quote(hit.get("filename") or "")
            + "&subfolder="
            + urllib.parse.quote(hit.get("subfolder") or "")
            + "&type="
            + urllib.parse.quote(hit.get("type") or "output")
        )
        with urllib.request.urlopen(url, timeout=300) as r:
            data = r.read()
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        dst = _CACHE_DIR / uuid.uuid4().hex[:8] / (hit.get("filename") or "out.bin")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        return dst
    except Exception:
        return None


def newest_output(prefix: str) -> Optional[Path]:
    """Newest file for a filename_prefix. VHS_VideoCombine bumps the trailing counter when a
    file with the same prefix exists, so never hard-code _00001."""
    hits = glob.glob(os.path.join(str(settings.comfy_output_dir), prefix + "_*.mp4"))
    if hits:
        hits.sort(key=os.path.getmtime)
        return Path(hits[-1])
    # 输出目录在本机不存在(ComfyUI 在另一台机器上)时,走 HTTP 从远端取
    hit = _match_output(prefix, (".mp4", ".webm", ".mov"))
    return _download(hit) if hit else None


def newest_image(prefix: str) -> Optional[Path]:
    """SaveImage 存下来的那张图(同样别硬编码 _00001)。"""
    hits = glob.glob(os.path.join(str(settings.comfy_output_dir), prefix + "_*.png"))
    if hits:
        hits.sort(key=os.path.getmtime)
        return Path(hits[-1])
    hit = _match_output(prefix, (".png", ".jpg", ".jpeg", ".webp"))
    return _download(hit) if hit else None
