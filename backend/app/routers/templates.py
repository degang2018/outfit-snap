"""模板:系统模板(内置)+ 用户自己保存的模板。

系统模板 is_system=True,所有人可见、只读;
用户模板 user_id=<本人>,只有自己能看/删,可以「把项目另存为模板」得到。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import Project, Template, TemplateShot, User
from ..schemas import TemplateFromProjectIn, TemplateOut
from ..serializers import template_shot_out

router = APIRouter(prefix="/templates", tags=["templates"])


def _out(tpl: Template) -> TemplateOut:
    return TemplateOut(
        id=tpl.id, key=tpl.key, title=tpl.title, description=tpl.description,
        cover_url=tpl.cover_url, out_width=tpl.out_width, out_height=tpl.out_height,
        fps=tpl.fps, is_system=tpl.is_system, owner_id=tpl.user_id,
        shots=[template_shot_out(s) for s in tpl.shots],
    )


def _visible(user: User):
    return or_(Template.is_system.is_(True), Template.user_id == user.id)


@router.get("", response_model=list[TemplateOut])
def list_templates(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[TemplateOut]:
    rows = (
        db.query(Template)
        .filter(_visible(user))
        .order_by(Template.is_system.desc(), Template.id.asc())
        .all()
    )
    return [_out(t) for t in rows]


@router.post("/from-project/{project_id}", response_model=TemplateOut, status_code=status.HTTP_201_CREATED)
def save_project_as_template(
    project_id: int,
    payload: TemplateFromProjectIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TemplateOut:
    """把当前项目(含每个镜头的提示词/参考图/时长/模式)另存为我自己的模板。"""
    project = db.get(Project, project_id)
    if project is None or project.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "项目不存在")
    if not project.shots:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "项目还没有镜头")

    tpl = Template(
        user_id=user.id,
        is_system=False,
        key=f"u{uuid.uuid4().hex[:12]}",
        title=payload.title,
        description=payload.description,
        cover_url="",
        out_width=project.out_width,
        out_height=project.out_height,
        fps=project.fps,
    )
    db.add(tpl)
    db.flush()
    for s in project.shots:
        db.add(TemplateShot(
            template_id=tpl.id,
            idx=s.idx,
            key=s.key,
            title=s.title,
            mode=s.mode,
            duration_frames=s.duration_frames,
            prompt=s.prompt,
            refs=list(s.refs or []),
            options=dict(s.options or {}),
        ))
    db.commit()
    db.refresh(tpl)
    return _out(tpl)


@router.get("/{template_id}", response_model=TemplateOut)
def get_template(template_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> TemplateOut:
    tpl = db.get(Template, template_id)
    if tpl is None or not (tpl.is_system or tpl.user_id == user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    return _out(tpl)


@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_template(template_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> None:
    tpl = db.get(Template, template_id)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    if tpl.is_system:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "系统模板不可删除")
    if tpl.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "模板不存在")
    db.delete(tpl)
    db.commit()
