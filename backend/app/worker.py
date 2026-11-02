"""Background render worker + watchdog.

两个守护线程:

* ``render-worker``   —— 取排队中的任务,一个一个跑。
* ``render-watchdog`` —— 保证渲染线程活着,并清理僵尸任务。

为什么要有看门狗:一个 running 的任务只活在渲染线程里。进程重启、线程挂掉、
或者线程卡死在某个调用里,数据库里的那一行就永远停在 ``running`` ——没人再推进它,
于是永远显示「生成中」,而且会把整个项目锁住(再点生成直接 409)。
看门狗靠心跳发现这种任务,自动收尾并把项目释放出来。
"""
from __future__ import annotations

import threading
import traceback
from datetime import datetime, timedelta

from . import comfy_client as comfy
from .config import settings
from .db import SessionLocal
from .models import ST_ACTIVE, ST_FAILED, ST_QUEUED, ST_RUNNING, ProjectShot, RenderJob, utcnow
from .pipeline import run_job

_stop = threading.Event()
_thread: threading.Thread | None = None
_supervisor: threading.Thread | None = None


def _naive(dt: datetime | None) -> datetime | None:
    """SQLite 取回来的是 naive 时间,统一成 naive 再比较。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _kill(db, job: RenderJob, message: str, error: str | None = None) -> None:
    """给一个再也不会被推进的任务收尾:标记失败 + 释放它占着的分镜。"""
    job.status = ST_FAILED
    job.message = message
    job.error = job.error or (error or message)
    job.finished_at = utcnow()
    if job.shot_id:
        shot = db.get(ProjectShot, job.shot_id)
        if shot is not None:
            shot.status = ST_FAILED
    elif job.current_shot:
        # 整片任务:当时正在生成的那个分镜还没出片,放回「未生成」
        (
            db.query(ProjectShot)
            .filter(ProjectShot.project_id == job.project_id, ProjectShot.key == job.current_shot)
            .update({ProjectShot.status: ST_QUEUED})
        )


def reap_orphans() -> int:
    """服务重启后,上次中断的 queued/running 任务再也没人会推进,直接标记为失败。"""
    db = SessionLocal()
    try:
        orphans = db.query(RenderJob).filter(RenderJob.status.in_(ST_ACTIVE)).all()
        for job in orphans:
            _kill(db, job, "服务重启,任务已中断", "渲染过程中服务重启,任务被中断,请重新生成")
        db.commit()
        return len(orphans)
    finally:
        db.close()


def reap_stalled() -> list[int]:
    """worker 名义上还在,但心跳早就停了 —— 任务卡死,自动清掉。

    心跳由 ``pipeline._beat()`` 在等 ComfyUI 的间隙持续刷新,所以只要任务正常推进
    就不会被误杀;真正卡住(或线程已经没了)的任务才会命中。
    """
    cutoff = _naive(utcnow()) - timedelta(seconds=settings.job_idle_timeout)
    db = SessionLocal()
    try:
        stalled = [
            job
            for job in db.query(RenderJob).filter(RenderJob.status == ST_RUNNING).all()
            if (_naive(job.heartbeat_at or job.started_at or job.created_at) or cutoff) < cutoff
        ]
        if not stalled:
            return []

        comfy.interrupt()          # 顺手让 ComfyUI 别再算这一炉了
        mins = max(1, settings.job_idle_timeout // 60)
        for job in stalled:
            _kill(
                db,
                job,
                f"超过 {mins} 分钟没有进展,已自动终止",
                "任务长时间没有心跳(生成卡住或服务异常),已自动终止并释放项目,请重新生成",
            )
        db.commit()
        return [job.id for job in stalled]
    finally:
        db.close()


def _loop() -> None:
    while not _stop.is_set():
        job_id = None
        db = SessionLocal()
        try:
            job = (
                db.query(RenderJob)
                .filter(RenderJob.status == ST_QUEUED)
                .order_by(RenderJob.id.asc())
                .first()
            )
            if job is not None:
                job_id = job.id
        except Exception:
            traceback.print_exc()
        finally:
            db.close()

        if job_id is None:
            _stop.wait(settings.worker_poll_seconds)
            continue

        db = SessionLocal()
        try:
            run_job(db, job_id)
        except Exception:
            traceback.print_exc()
        finally:
            db.close()


def _ensure_worker() -> None:
    """渲染线程不在了就重新拉起 —— 否则任务会一直排队,永远没人接手。"""
    global _thread
    if _stop.is_set() or not settings.worker_enabled:
        return
    if _thread is not None and _thread.is_alive():
        return
    if _thread is not None:
        print("[worker] 渲染线程已退出,重新拉起", flush=True)
    _thread = threading.Thread(target=_loop, name="render-worker", daemon=True)
    _thread.start()


def _supervise() -> None:
    print(
        f"[worker] 看门狗已启动:每 {settings.watchdog_interval:.0f}s 检查一次,"
        f"running 任务超过 {max(1, settings.job_idle_timeout // 60)} 分钟没心跳就自动清理",
        flush=True,
    )
    while not _stop.is_set():
        _stop.wait(settings.watchdog_interval)
        if _stop.is_set():
            return
        try:
            dead = reap_stalled()
            if dead:
                print(f"[worker] 自动清理了卡死的任务 {dead}", flush=True)
        except Exception:
            traceback.print_exc()
        try:
            _ensure_worker()
        except Exception:
            traceback.print_exc()


def start() -> None:
    global _supervisor
    if not settings.worker_enabled:
        return
    _ensure_worker()
    if _supervisor is None or not _supervisor.is_alive():
        _supervisor = threading.Thread(target=_supervise, name="render-watchdog", daemon=True)
        _supervisor.start()


def stop() -> None:
    _stop.set()
