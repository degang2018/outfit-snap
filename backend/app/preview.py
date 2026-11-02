"""按镜头快速出一张预览图(SDXL 纯文生图),提前看构图/景别,不用等整片跑完。

一次任务只跑一个项目:逐镜头串行出图,进度放内存里给前端轮询。
出的图落到 storage/previews/,路径写进 project_shots.options["preview_rel_path"],
所以刷新页面也能看到,不用再跑一遍。
"""
from __future__ import annotations

import random
import re
import shutil
import threading

from . import comfy_client, comfy_workflow, llm
from .camera import apply_en as apply_camera_en
from .config import STORAGE_DIR, settings
from .db import SessionLocal
from .models import ProjectShot

_LOCK = threading.Lock()
_JOBS: dict[int, dict] = {}

_IDLE = {"status": "idle", "total": 0, "done": 0, "current": "", "error": ""}

_SCENE_RE = re.compile(r"<Scene \d+> 是「([^」]+)」")
_STOPS = ("overall_soundscape:", "non_diegetic_music:", "retention_analysis:")

# 景别关键词 -> 给 SDXL 用的短词。按顺序匹配(「全景」要在「近景」之前判)
_FRAME_WORDS = (("全景", "全身"), ("半身", "半身"), ("中景", "中景"),
                ("近景", "近景"), ("特写", "近景"))


def _action_text(prompt: str) -> str:
    """从 H3 的六段结构里取出 detailed_description 那一段(就是画面描述)。"""
    text = prompt or ""
    marker = "detailed_description:"
    i = text.find(marker)
    if i < 0:
        return " ".join(text.split())
    seg = text[i + len(marker):]
    for stop in _STOPS:
        j = seg.find(stop)
        if j >= 0:
            seg = seg[:j]
    return " ".join(seg.split())


def _scene_of(prompt: str) -> str:
    return "、".join(dict.fromkeys(_SCENE_RE.findall(prompt or "")))


# SDXL 用的 CLIP 其实**不吃中文**(实测:写「纯白背景」会给粉色、写「中景」会给全身),
# 所以真正拿去出图的是下面这条英文提示词 —— 让 LLM 把这镜的中文描述压成一句英文。
_EN_SYSTEM = (
    "You write ONE short English prompt for SDXL, a photorealistic still-image model. "
    "The input is a Chinese description of a single shot from a fashion outfit video.\n"
    "Output ONLY the prompt: no quotes, no labels, no explanation, one single line.\n"
    "Rules:\n"
    "1. START with the shot size, putting it first, choosing exactly one of these three: "
    "'full body shot' / 'cowboy shot, head to mid-thigh' / 'half body shot, head to waist'. "
    "NEVER go tighter than half body: the whole head AND the upper body (so the garment is "
    "visible) must always be in frame - even for a necklace, collarbone or lipstick shot, "
    "still frame at least head-to-waist.\n"
    "2. Then the subject and her single action, briefly: 'a young asian woman' plus what she is "
    "wearing plus her pose and action.\n"
    "3. Then the background and the lighting - use the scene given in the input, do not invent a different one.\n"
    "4. End with: photorealistic, fashion photo, sharp focus, natural skin texture.\n"
    "5. English only, under 40 words - keep it short, the model only reads the beginning.\n"
    "6. The whole head must stay inside the frame - never crop the top of the head.\n"
    "7. Never mention text, watermark, logo, or any second person "
    "(a single hand coming in from the edge is fine)."
)

_FRAME_EN = {"全身": "full body shot", "半身": "half body shot, head to waist",
             "中景": "cowboy shot, head to mid-thigh", "近景": "half body shot, head to waist"}


def _english_prompt(title: str, action: str, scene: str) -> str | None:
    """让 LLM 把这一镜的中文描述压成一句 SDXL 吃得下的英文。拿不到就返回 None。"""
    if not settings.llm_enabled:
        return None
    user = f"镜头标题:{title or ''}\n场景:{scene or '(没写)'}\n画面描述:{(action or '')[:500]}"
    try:
        out = llm.chat_text(_EN_SYSTEM, user, temperature=0.4)
    except Exception:  # noqa: BLE001
        return None
    if not out:
        return None
    return " ".join(out.split())[:600]


def _fallback_en(prompt: str) -> str:
    """LLM 不可用时的兜底:至少把景别和影棚说清楚。"""
    seg = _action_text(prompt)
    frame = next((v for k, v in _FRAME_WORDS if k in seg), "")
    return (f"{_FRAME_EN.get(frame, 'half body shot, head to waist')}, a young asian woman, "
            f"pure white studio background, soft even lighting, "
            f"photorealistic fashion photo, sharp focus, natural skin texture")


def _run(project_id: int, shot_ids: list[int]) -> None:
    db = SessionLocal()
    # 先在干净的显存上开始:如果之前跑过视频渲染,那个大模型还占着卡,SDXL 会挤不进去
    comfy_client.free_memory()
    try:
        job = _JOBS[project_id]
        for n, shot_id in enumerate(shot_ids, start=1):
            shot = db.get(ProjectShot, shot_id)
            if shot is None:
                continue
            job["current"] = shot.title or shot.key
            prompt = (_english_prompt(shot.title, _action_text(shot.prompt), _scene_of(shot.prompt))
                      or _fallback_en(shot.prompt))
            if prompt:
                # 预览图也跟着多角度机位走,不然调整完角度看不到构图变化
                prompt = apply_camera_en(prompt, (shot.options or {}).get("camera"))
            if not prompt:
                job["done"] = n
                continue

            prefix = f"preview_p{project_id}_s{shot.idx}"
            wf = comfy_workflow.build_preview_workflow(
                prompt=prompt, seed=random.randint(1, 2**31 - 1), prefix=prefix,
            )
            pid = comfy_client.submit(wf)
            comfy_client.wait(pid, timeout=settings.preview_timeout)

            src = comfy_client.newest_image(prefix)
            if src is not None:
                rel = f"previews/{src.name}"
                (STORAGE_DIR / "previews").mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, STORAGE_DIR / rel)
                shot.options = {**(shot.options or {}), "preview_rel_path": rel}
                db.commit()
            job["done"] = n
        job["status"] = "done"
        job["current"] = ""
    except Exception as exc:  # noqa: BLE001
        job = _JOBS.get(project_id)
        if job is not None:
            job["status"] = "failed"
            job["error"] = str(exc)[:400]
    finally:
        # 用完就把 SDXL 从显存里赶走,否则紧接着的视频渲染会显存不足
        comfy_client.free_memory()
        db.close()


def start(project_id: int, shot_ids: list[int]) -> dict:
    with _LOCK:
        cur = _JOBS.get(project_id)
        if cur is not None and cur["status"] == "running":
            return dict(cur)
        _JOBS[project_id] = {"status": "running", "total": len(shot_ids),
                             "done": 0, "current": "", "error": ""}
        job = dict(_JOBS[project_id])
    threading.Thread(target=_run, args=(project_id, list(shot_ids)), daemon=True).start()
    return job


def status(project_id: int) -> dict:
    job = _JOBS.get(project_id)
    return dict(job) if job is not None else dict(_IDLE)


def any_running() -> int | None:
    """有预览图任务在跑就返回它的项目 id(渲染前用它挡住,别和 SDXL 抢显存)。"""
    with _LOCK:
        return next((pid for pid, job in _JOBS.items() if job["status"] == "running"), None)
