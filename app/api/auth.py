"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import (
    create_access_token,
    get_current_user,
    hash_password,
    require_admin,
    verify_password,
)
from app.core.types import utcnow
from app.models.user import User
from app.schemas.auth import RoleUpdate, TokenResponse, UserLogin, UserOut, UserRegister

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account",
)
def register(
    payload: UserRegister,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    """Register a new user.

    The first account created on a fresh install becomes an admin, so a new
    deployment is usable without a seed script or a manual SQL update. Every
    later registration is a viewer.
    """
    existing = db.scalars(select(User).where(User.email == payload.email.lower())).first()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with that email already exists",
        )

    is_first_user = db.scalar(select(User.id).limit(1)) is None

    user = User(
        email=payload.email.lower(),
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role="admin" if is_first_user else "viewer",
        last_login_at=utcnow(),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(str(user.id), extra_claims={"role": user.role})
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse, summary="Log in")
def login(
    payload: UserLogin,
    db: Annotated[Session, Depends(get_db)],
) -> TokenResponse:
    user = db.scalars(select(User).where(User.email == payload.email.lower())).first()

    # Same response for "no such user" and "wrong password": telling an attacker
    # which emails exist is a free account-enumeration oracle.
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )

    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)

    token = create_access_token(str(user.id), extra_claims={"role": user.role})
    return TokenResponse(
        access_token=token,
        expires_in=settings.access_token_ttl_minutes * 60,
        user=UserOut.model_validate(user),
    )


@router.get("/me", response_model=UserOut, summary="Current user")
def me(user: Annotated[User, Depends(get_current_user)]) -> UserOut:
    return UserOut.model_validate(user)


@router.get("/users", response_model=list[UserOut], summary="List users (admin)")
def list_users(
    _admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> list[UserOut]:
    users = db.scalars(select(User).order_by(User.id)).all()
    return [UserOut.model_validate(u) for u in users]


@router.patch(
    "/users/{user_id}/role",
    response_model=UserOut,
    summary="Change a user's role (admin)",
)
def update_role(
    user_id: int,
    payload: RoleUpdate,
    admin: Annotated[User, Depends(require_admin)],
    db: Annotated[Session, Depends(get_db)],
) -> UserOut:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if target.id == admin.id and payload.role != "admin":
        # Locking yourself out of the only admin account is unrecoverable
        # without database access, so refuse it rather than allow it.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You cannot remove your own admin role",
        )
    target.role = payload.role
    db.commit()
    db.refresh(target)
    return UserOut.model_validate(target)
