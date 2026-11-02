"""玩法(Playbook):可复用的拍摄结构 + 参数。

用户可以把「打底穿搭 / 自定义动作 / 场景 / 镜头数 / 时长」存成一个玩法,
以后换任何衣服直接套用 —— 剧本每次按这件衣服现场生成,不是写死的镜头。
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import Playbook, User
from ..schemas import PlaybookIn, PlaybookOut, PlaybookPatchIn

router = APIRouter(prefix="/playbooks", tags=["playbooks"])


def _own(playbook_id: int, user: User, db: Session) -> Playbook:
    row = db.get(Playbook, playbook_id)
    if row is None or row.is_system or row.owner_id != user.id:
        # 内置玩法所有人可见但不可改;别人的玩法当作不存在
        raise HTTPException(status.HTTP_404_NOT_FOUND, "玩法不存在,或不属于你")
    return row


@router.get("", response_model=list[PlaybookOut])
def list_playbooks(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[PlaybookOut]:
    """内置玩法 + 我自己保存的玩法。"""
    rows = (
        db.query(Playbook)
        .filter(or_(Playbook.is_system.is_(True), Playbook.owner_id == user.id))
        .order_by(Playbook.is_system.desc(), Playbook.id)
        .all()
    )
    return [PlaybookOut.model_validate(r) for r in rows]


@router.post("", response_model=PlaybookOut, status_code=status.HTTP_201_CREATED)
def create_playbook(
    payload: PlaybookIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaybookOut:
    """把当前这套参数存成「我的玩法」。"""
    row = Playbook(
        owner_id=user.id,
        is_system=False,
        key="u" + uuid.uuid4().hex[:12],
        title=payload.title,
        description=payload.description,
        structure=payload.structure,
        params=dict(payload.params or {}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return PlaybookOut.model_validate(row)


@router.patch("/{playbook_id}", response_model=PlaybookOut)
def update_playbook(
    playbook_id: int,
    payload: PlaybookPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PlaybookOut:
    row = _own(playbook_id, user, db)
    data = payload.model_dump(exclude_unset=True)
    if data.get("title") is not None:
        row.title = data["title"]
    if data.get("description") is not None:
        row.description = data["description"]
    if data.get("params") is not None:
        row.params = dict(data["params"])
    db.commit()
    db.refresh(row)
    return PlaybookOut.model_validate(row)


@router.delete("/{playbook_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_playbook(
    playbook_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> None:
    row = _own(playbook_id, user, db)
    db.delete(row)
    db.commit()
