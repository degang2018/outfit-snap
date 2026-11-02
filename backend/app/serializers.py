"""ORM -> API payload helpers (keeps the routers thin)."""
from __future__ import annotations

from .config import settings
from .models import (
    ST_ACTIVE,
    ST_CANCELED,
    ST_DONE,
    ST_DRAFT,
    ST_FAILED,
    Asset,
    Project,
    ProjectShot,
    RenderJob,
    TemplateShot,
)
from .schemas import AssetOut, ProjectBrief, ProjectOut, RenderJobOut, ShotOut, TemplateShotOut
from .thumb import thumb_rel_for


def media_url(rel_path: str | None) -> str | None:
    return f"/media/{rel_path}" if rel_path else None


def _json_list(raw: str | None) -> list:
    """DB 里存的是 JSON 文本(ai_more),坏了就当空列表,别让接口 500。"""
    if not raw:
        return []
    try:
        import json

        out = json.loads(raw)
        return out if isinstance(out, list) else []
    except Exception:  # noqa: BLE001
        return []


def asset_out(asset: Asset) -> AssetOut:
    return AssetOut(id=asset.id, filename=asset.filename, url=media_url(asset.rel_path) or "",
                    category=asset.category, width=asset.width, height=asset.height,
                    is_system=asset.is_system,
                    ai_name=asset.ai_name or "", ai_desc=asset.ai_desc or "",
                    ai_kind=asset.ai_kind or "", ai_len=asset.ai_len or "",
                    ai_close=asset.ai_close or "",
                    ai_more=_json_list(asset.ai_more))


def template_shot_out(shot: TemplateShot) -> TemplateShotOut:
    return TemplateShotOut(
        idx=shot.idx, key=shot.key, title=shot.title or shot.key, mode=shot.mode,
        duration_frames=shot.duration_frames, prompt=shot.prompt,
        refs=list(shot.refs or []), options=dict(shot.options or {}),
    )


def shot_out(shot: ProjectShot) -> ShotOut:
    options = dict(shot.options or {})
    # 静态图分镜(piece_style='static' 的主链):clip_rel_path 直接是 .png,
    # 没有缩略图视频那套,thumb_url 直接用原图(前端 <img> 自己缩放)。
    _clip = shot.clip_rel_path or ""
    _is_image = _clip.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    return ShotOut(
        id=shot.id, idx=shot.idx, key=shot.key, title=shot.title or shot.key, mode=shot.mode,
        duration_frames=shot.duration_frames, prompt=shot.prompt, refs=list(shot.refs or []),
        options=options, enabled=shot.enabled, status=shot.status,
        clip_url=media_url(shot.clip_rel_path),
        thumb_url=media_url(shot.clip_rel_path) if _is_image else media_url(thumb_rel_for(shot.clip_rel_path)),
        candidates=[u for c in (shot.candidates or []) if c and (u := media_url(c))],
        # 静态图候选本身就是 PNG:没有 thumbs/*.jpg,不能给前端一个 404 的缩略图地址
        candidate_thumbs=[(u if c.lower().endswith((".png", ".jpg", ".jpeg", ".webp")) else None)
                          if c and (u := media_url(c)) else None
                          for c in (shot.candidates or [])],
        preview_url=media_url(options.get("preview_rel_path")),
    )


def job_out(job: RenderJob | None) -> RenderJobOut | None:
    if job is None:
        return None
    return RenderJobOut(
        id=job.id, status=job.status, progress=job.progress, current_shot=job.current_shot,
        message=job.message, error=job.error, shot_id=job.shot_id, out_url=media_url(job.out_rel_path),
        created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at,
    )


def total_frames(shots) -> int:
    """成片总帧数。

    「只产状态图」的镜头(按件排镜里的展示镜)不进成片(见 pipeline.run_job),
    这里也要排除 —— 否则界面报的时长会比成片实际长一截。
    """
    return sum(s.duration_frames for s in shots
               if getattr(s, "enabled", True)
               and not (getattr(s, "options", None) or {}).get("state_only"))


def project_status(project: Project) -> str:
    """项目对外展示的真实状态。

    数据库里的 status 只在真正提交渲染时才有意义,新建的项目一直是 queued,
    直接拿它当「排队中」会误导用户,所以这里按「有没有在跑的任务 / 有没有成片」重新判定。
    """
    active = next((j for j in (project.jobs or []) if j.status in ST_ACTIVE), None)
    if active is not None:
        return active.status                      # 排队中 / 生成中
    if project.final_rel_path:
        return ST_DONE                            # 有片子才算完成
    if project.status in (ST_FAILED, ST_CANCELED):
        return project.status
    return ST_DRAFT


def project_out(project: Project, with_shots: bool = True) -> ProjectOut:
    frames = total_frames(project.shots)
    fps = project.fps or settings.fps
    return ProjectOut(
        id=project.id, title=project.title, status=project_status(project),
        template_id=project.template_id,
        out_width=project.out_width, out_height=project.out_height, fps=fps,
        total_frames=frames, duration_seconds=round(frames / fps, 2),
        final_url=media_url(project.final_rel_path),
        bg_ref=project.bg_ref, bg_desc=project.bg_desc,
        canvas_state=dict(project.canvas_state or {}),
        created_at=project.created_at, updated_at=project.updated_at,
        shots=[shot_out(s) for s in project.shots] if with_shots else [],
        latest_job=job_out(project.jobs[0] if project.jobs else None),
    )


def project_brief(project: Project) -> ProjectBrief:
    frames = total_frames(project.shots)
    fps = project.fps or settings.fps
    return ProjectBrief(
        id=project.id, title=project.title, status=project_status(project),
        out_width=project.out_width, out_height=project.out_height, fps=fps,
        total_frames=frames, duration_seconds=round(frames / fps, 2),
        final_url=media_url(project.final_rel_path),
        created_at=project.created_at, updated_at=project.updated_at,
    )
