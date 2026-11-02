"""细节镜头库:内置镜头 + 用户自己的改动(改文案 / 删除 / 新增)。

内置那一套写在 quick_start.SHOT_LIBRARY 里(只读);这里只存用户对它的「覆盖」,
原版永远不动 —— 所以每个用户都能随时恢复默认,也不会影响别人。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import quick_start
from ..db import get_db
from ..deps import get_current_user
from ..models import Project, ProjectShot, ShotIdea, User
from ..schemas import (
    ShotBuiltinPatchIn,
    ShotFromShotIn,
    ShotIdeaIn,
    ShotIdeaPatchIn,
    ShotLibraryOut,
)

router = APIRouter(prefix="/shots", tags=["shots"])

# items 里内置镜头是 SHOT_LIBRARY + EXTRA_SHOT_LIBRARY 拼出来的,
# 这里只取 SHOT_LIBRARY 会让补充镜头(如 my_collar_hands)改 / 禁用时 404。
_BUILTIN_KEYS = {it["key"] for it in quick_start.SHOT_LIBRARY_ALL}


def _override_rows(user: User, db: Session) -> list[ShotIdea]:
    return (
        db.query(ShotIdea)
        .filter(ShotIdea.owner_id == user.id, ShotIdea.source_key.isnot(None))
        .all()
    )


def _custom_rows(user: User, db: Session) -> list[ShotIdea]:
    return (
        db.query(ShotIdea)
        .filter(ShotIdea.owner_id == user.id, ShotIdea.source_key.is_(None))
        .order_by(ShotIdea.id)
        .all()
    )


def user_shot_library(user: User, db: Session) -> dict:
    """合并后的镜头库。别的路由也用这个(quick-options / draft-script)。"""
    overrides = {
        r.source_key: {"label": r.label, "action": r.action, "hidden": r.hidden}
        for r in _override_rows(user, db)
    }
    customs = [
        {"id": c.id, "key": f"my_{c.id}", "label": c.label, "action": c.action,
         "seconds": float(c.seconds or 3.0), "hidden": c.hidden}
        for c in _custom_rows(user, db)
    ]
    library, items = quick_start.build_shot_library(overrides, customs)
    return {
        "library": library,
        "items": items,
        "shot_map": {it["key"]: it["action"] for it in library},
    }


def _out(user: User, db: Session) -> ShotLibraryOut:
    data = user_shot_library(user, db)
    return ShotLibraryOut(library=data["library"], items=data["items"])


def _own_row(shot_id: int, user: User, db: Session) -> ShotIdea:
    row = db.get(ShotIdea, shot_id)
    if row is None or row.owner_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "镜头不存在")
    return row


def _action_from_prompt(prompt: str) -> str:
    """从一镜的提示词里抠出「动作正文」—— 存成通用分镜用。

    提示词结构:detailed_description: 后面是动作正文,再接系统自动追加的机位 / 景别那段
    (以「 镜头:」开头)。存的时候把追加的那段切掉,免得以后再用时被追加两遍。
    """
    marker = "detailed_description:"
    i = prompt.find(marker)
    if i < 0:
        return ""
    body = prompt[i + len(marker):]
    for stop in ("overall_soundscape:", " 镜头:"):
        j = body.find(stop)
        if j >= 0:
            body = body[:j]
    return body.strip()[:2000]


def _override_row(key: str, user: User, db: Session) -> ShotIdea | None:
    return (
        db.query(ShotIdea)
        .filter(ShotIdea.owner_id == user.id, ShotIdea.source_key == key)
        .first()
    )


@router.get("", response_model=ShotLibraryOut)
def list_shots(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> ShotLibraryOut:
    """内置镜头 + 我的改动 + 我加的镜头。"""
    return _out(user, db)


@router.post("", response_model=ShotLibraryOut, status_code=status.HTTP_201_CREATED)
def add_shot(
    payload: ShotIdeaIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotLibraryOut:
    """加一个「我的」镜头。"""
    db.add(ShotIdea(owner_id=user.id, source_key=None,
                    label=payload.label.strip(), action=payload.action.strip(),
                    seconds=float(payload.seconds or 3.0)))
    db.commit()
    return _out(user, db)


@router.patch("/{shot_id}", response_model=ShotLibraryOut)
def edit_shot(
    shot_id: int,
    payload: ShotIdeaPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotLibraryOut:
    """改一个「我的」镜头。"""
    row = _own_row(shot_id, user, db)
    data = payload.model_dump(exclude_unset=True)
    if data.get("label") is not None:
        row.label = data["label"].strip()
    if data.get("action") is not None:
        row.action = data["action"].strip()
    if data.get("seconds") is not None:
        row.seconds = float(data["seconds"])
    if data.get("hidden") is not None:
        row.hidden = bool(data["hidden"])
    db.commit()
    return _out(user, db)


@router.delete("/{shot_id}", response_model=ShotLibraryOut)
def delete_shot(
    shot_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotLibraryOut:
    """删掉一个「我的」镜头。"""
    row = _own_row(shot_id, user, db)
    db.delete(row)
    db.commit()
    return _out(user, db)


@router.post("/from-shot", response_model=ShotLibraryOut, status_code=status.HTTP_201_CREATED)
def save_shot_as_idea(
    payload: ShotFromShotIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotLibraryOut:
    """把某个镜头存成「通用分镜」:标题当 label,提示词里的动作正文当 action。

    存下来之后,在「上传衣服 · 一键出稿」(auto.html) 里就能勾到它。
    """
    shot = db.get(ProjectShot, payload.shot_id)
    project = db.get(Project, shot.project_id) if shot is not None else None
    if shot is None or project is None or project.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "镜头不存在")
    action = _action_from_prompt(shot.prompt or "")
    if not action:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "这一镜里没有可保存的动作正文")
    _fps = project.fps or 24
    _secs = max(0.5, min(10.0, round((shot.duration_frames or 72) / _fps, 1)))
    db.add(ShotIdea(owner_id=user.id, source_key=None,
                    label=(shot.title or "通用分镜").strip()[:60],
                    action=action[:2000], seconds=_secs))
    db.commit()
    return _out(user, db)


@router.put("/builtin/{key}", response_model=ShotLibraryOut)
def edit_builtin(
    key: str,
    payload: ShotBuiltinPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotLibraryOut:
    """改 / 隐藏一个内置镜头(只影响我自己)。"""
    if key not in _BUILTIN_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "内置镜头不存在")
    row = _override_row(key, user, db)
    if row is None:
        row = ShotIdea(owner_id=user.id, source_key=key)
        db.add(row)
    data = payload.model_dump(exclude_unset=True)
    if data.get("label") is not None:
        row.label = data["label"].strip()
    if data.get("action") is not None:
        row.action = data["action"].strip()
    if data.get("hidden") is not None:
        row.hidden = bool(data["hidden"])
    db.commit()
    return _out(user, db)


@router.delete("/builtin/{key}", response_model=ShotLibraryOut)
def reset_builtin(
    key: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShotLibraryOut:
    """把这个内置镜头恢复成默认(丢掉我的改动)。"""
    if key not in _BUILTIN_KEYS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "内置镜头不存在")
    row = _override_row(key, user, db)
    if row is not None:
        db.delete(row)
        db.commit()
    return _out(user, db)
