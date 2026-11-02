"""Auth: guest-first JWT, e-mail binding, login."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import BindEmailIn, LoginIn, TokenOut, UserOut
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _token(user: User) -> TokenOut:
    return TokenOut(access_token=create_access_token(user.id), user=UserOut.model_validate(user))


@router.post("/guest", response_model=TokenOut)
def create_guest(db: Session = Depends(get_db)) -> TokenOut:
    """Default entry: every new visitor gets a throw-away guest account + token."""
    guests = db.query(User).filter(User.is_guest.is_(True)).count()
    user = User(display_name=f"游客{guests + 1:04d}", is_guest=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return _token(user)


@router.post("/bind", response_model=TokenOut)
def bind_email(
    payload: BindEmailIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TokenOut:
    """Guest -> full user. The guest's projects stay attached to the same row."""
    if not user.is_guest:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该账号已经绑定邮箱")
    if db.query(User).filter(User.email == payload.email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "该邮箱已被注册")
    user.email = payload.email
    user.password_hash = hash_password(payload.password)
    user.is_guest = False
    if payload.display_name:
        user.display_name = payload.display_name
    db.commit()
    db.refresh(user)
    return _token(user)


@router.post("/login", response_model=TokenOut)
def login(payload: LoginIn, db: Session = Depends(get_db)) -> TokenOut:
    email = payload.email.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "邮箱或密码不正确")
    return _token(user)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(user)
