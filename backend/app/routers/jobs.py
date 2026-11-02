"""Render job polling / cancel, plus ComfyUI health."""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import comfy_client as comfy
from .. import llm
from ..config import GEN_PRESETS, REF_SIZE_TIERS, STORAGE_DIR, apply_speed, settings
from ..db import get_db
from ..deps import get_current_user
from ..models import (
    ST_ACTIVE,
    ST_CANCELED,
    ST_DONE,
    ST_FAILED,
    ST_QUEUED,
    ST_RUNNING,
    Project,
    ProjectShot,
    RenderJob,
    User,
    utcnow,
)
from ..schemas import RenderJobOut, SpeedSettingIn
from ..serializers import job_out

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=RenderJobOut)
def get_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RenderJobOut:
    job = db.get(RenderJob, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    return job_out(job)


@router.post("/jobs/{job_id}/cancel", response_model=RenderJobOut)
def cancel_job(job_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> RenderJobOut:
    """终止生成:中断 ComfyUI 当前任务,并把状态落到「已终止」。
    正在跑的 worker 会看到这个状态,不再把结果写成失败或成功。"""
    job = db.get(RenderJob, job_id)
    if job is None or job.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在")
    if job.status not in ST_ACTIVE:
        return job_out(job)

    if job.status == ST_RUNNING:
        comfy.interrupt()

    job.status = ST_CANCELED
    job.message = "已终止"
    job.error = ""
    job.finished_at = utcnow()
    if job.shot_id:
        shot = db.get(ProjectShot, job.shot_id)
        if shot is not None:
            shot.status = ST_CANCELED
    elif job.current_shot:
        # 整片任务被终止时,当时正在生成的那个分镜还没出片,放回「未生成」,
        # 否则它会一直显示「生成中」。
        (
            db.query(ProjectShot)
            .filter(
                ProjectShot.project_id == job.project_id,
                ProjectShot.key == job.current_shot,
            )
            .update({ProjectShot.status: ST_QUEUED})
        )
    db.commit()
    db.refresh(job)
    return job_out(job)


# 队列里的 workflow 用 filename_prefix = "p{项目id}_s{镜号}" 标识(见 pipeline),
# 靠它把 ComfyUI 的队列项对回我们的项目 / 分镜,顺便也能找到对应的任务记录。
_PREFIX_RE = re.compile(r"^p(\d+)_s(\d+)$")


def _queue_prefix(wf: dict) -> str:
    for node in (wf or {}).values():
        if not isinstance(node, dict):
            continue
        inp = node.get("inputs") or {}
        for key in ("filename_prefix", "filename_prefix_text"):
            val = inp.get(key)
            if isinstance(val, str) and _PREFIX_RE.match(val.strip()):
                return val.strip()
    return ""


def _queue_item(it, state: str, db: Session, user: User) -> dict:
    """把 ComfyUI 的队列项转成前端能显示的 {标题 / 项目 / 分镜 / 任务}。"""
    out = {"prompt_id": "", "state": state, "title": "", "shot_key": "",
           "project_id": None, "job_id": None}
    if not isinstance(it, (list, tuple)) or len(it) < 2:
        return out
    out["prompt_id"] = str(it[1])
    prefix = _queue_prefix(it[2] if len(it) > 2 and isinstance(it[2], dict) else {})
    m = _PREFIX_RE.match(prefix)
    if not m:
        return out
    proj_id, idx = int(m.group(1)), int(m.group(2))
    proj = db.get(Project, proj_id)
    if proj is not None and proj.user_id != user.id:
        return out  # 别人的任务不展示细节
    shot = (
        db.query(ProjectShot)
        .filter(ProjectShot.project_id == proj_id, ProjectShot.idx == idx)
        .first()
    )
    out["project_id"] = proj_id
    out["shot_key"] = f"s{idx:02d}"
    name = (shot.title or "") if shot is not None else ""
    out["title"] = f"{proj.title} · {name}" if (proj and name) else (name or prefix)
    job = (
        db.query(RenderJob)
        .filter(
            RenderJob.project_id == proj_id,
            RenderJob.user_id == user.id,
            RenderJob.status.in_(ST_ACTIVE),
        )
        .order_by(RenderJob.id.desc())
        .first()
    )
    if job is not None and (shot is None or job.shot_id in (None, shot.id)):
        out["job_id"] = job.id
    return out


@router.get("/system/queue")
def get_queue(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> dict:
    """ComfyUI 队列任务(运行中 + 排队中),带项目 / 分镜名,前端可以终止。"""
    try:
        q = comfy.queue_state()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"无法连接 ComfyUI: {e}")
    running = [_queue_item(it, "running", db, user) for it in (q.get("queue_running") or [])]
    pending = [_queue_item(it, "pending", db, user) for it in (q.get("queue_pending") or [])]
    return {"running": running, "pending": pending}


@router.post("/system/queue/cancel")
def cancel_queue_item(
    payload: dict = Body(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """终止队列任务:运行中的直接中断,排队中的从队列删掉;对应的任务记录一并标成已终止。"""
    prompt_id = str((payload or {}).get("prompt_id") or "").strip()
    if not prompt_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "缺少 prompt_id")
    try:
        q = comfy.queue_state()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"无法连接 ComfyUI: {e}")
    rows = (q.get("queue_running") or []) + (q.get("queue_pending") or [])

    def _ids(items):
        return [str(x[1]) for x in items if isinstance(x, (list, tuple)) and len(x) > 1]

    if prompt_id in _ids(q.get("queue_running") or []):
        comfy.interrupt()
    elif prompt_id in _ids(q.get("queue_pending") or []):
        try:
            comfy.queue_delete([prompt_id])
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"删除队列任务失败: {e}")
    else:
        return {"ok": False, "message": "任务已不在队列里"}

    # 顺手把后端任务记录标成已终止 —— 否则 worker 还会继续推进后面的分镜。
    item = next(
        (x for x in rows if isinstance(x, (list, tuple)) and len(x) > 1 and str(x[1]) == prompt_id),
        None,
    )
    job_id = _queue_item(item, "running", db, user).get("job_id")
    job = db.get(RenderJob, job_id) if job_id else None
    if job is not None and job.status in ST_ACTIVE:
        job.status = ST_CANCELED
        job.message = "已终止"
        job.error = ""
        job.finished_at = utcnow()
        if job.shot_id:
            shot = db.get(ProjectShot, job.shot_id)
            if shot is not None:
                shot.status = ST_CANCELED
        elif job.current_shot:
            db.query(ProjectShot).filter(
                ProjectShot.project_id == job.project_id,
                ProjectShot.key == job.current_shot,
            ).update({ProjectShot.status: ST_QUEUED})
        db.commit()
    return {"ok": True}


@router.get("/system/media-url")
def media_url(path: str, user: User = Depends(get_current_user)) -> dict:
    """本地绝对路径(状态图 / 抽帧)→ /media URL。

    前端只认 `asset:` 引用,`file:` 引用(换装链的状态图)原本没有缩略图;
    这里把 storage 目录下的文件映射成可直接访问的 /media 地址。
    """
    try:
        rel = Path(path).resolve().relative_to(STORAGE_DIR.resolve())
    except (ValueError, OSError):
        return {"url": ""}
    return {"url": "/media/" + rel.as_posix()}


@router.get("/system/comfy")
def comfy_health(user: User = Depends(get_current_user)) -> dict:
    up = comfy.is_up()
    detail: dict = {"up": up, "api": comfy.settings.comfy_api}
    if up:
        try:
            stats = comfy.system_stats()
            devices = stats.get("devices", [])
            if devices:
                d = devices[0]
                detail["gpu"] = d.get("name", "")
                detail["vram_free_mb"] = round(d.get("vram_free", 0) / 1024 / 1024)
                detail["vram_total_mb"] = round(d.get("vram_total", 0) / 1024 / 1024)
        except Exception:  # noqa: BLE001
            pass
    return detail


@router.get("/system/llm")
def llm_health(user: User = Depends(get_current_user)) -> dict:
    """剧本切分用的本地 LLM 状态(不可用时前端会自动退回规则切分)。"""
    return {
        "enabled": settings.llm_enabled,
        "up": llm.available(),
        "model": settings.llm_model,
        "api": settings.llm_base_url,
    }


def _speed_state() -> dict:
    """出片速度设置(全局):生成分辨率档位 + 参考图档位。"""
    return {
        "gen_short": min(settings.gen_width, settings.gen_height),
        "gen_width": settings.gen_width,
        "gen_height": settings.gen_height,
        "ref_size": settings.ref_image_size,
        "presets": [{"short": k, "w": w, "h": h} for k, (w, h) in sorted(GEN_PRESETS.items())],
        "ref_tiers": list(REF_SIZE_TIERS),
    }


@router.get("/system/speed")
def get_speed(user: User = Depends(get_current_user)) -> dict:
    return _speed_state()


@router.post("/system/speed")
def set_speed(payload: SpeedSettingIn, user: User = Depends(get_current_user)) -> dict:
    """改出片速度(全局,立即生效并写回 .env,下次启动还在)。

    只影响**之后新生成**的镜头 —— 已经有片段的镜头仍会复用,不会因为改档就重出。
    """
    updates = apply_speed(gen_short=payload.gen_short, ref_size=payload.ref_size)
    return {"updated": updates, **_speed_state()}
