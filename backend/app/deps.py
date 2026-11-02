"""Shared FastAPI dependencies (auth)."""
from __future__ import annotations

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import User
from .security import decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="登录状态无效或已过期,请重新进入",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    cred: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    if cred is None or not cred.credentials:
        raise _UNAUTHORIZED
    user_id = decode_access_token(cred.credentials)
    if user_id is None:
        raise _UNAUTHORIZED
    user = db.get(User, user_id)
    if user is None:
        raise _UNAUTHORIZED
    return user
