"""Password hashing, JWT issuing/verification and the auth dependencies.

bcrypt is called directly rather than through passlib: passlib 1.7.4 breaks on
bcrypt >= 4.1 (it reads ``bcrypt.__about__.__version__``, which was removed),
and the usual workaround is pinning an ancient bcrypt forever. Nine lines of
direct bcrypt has no version coupling.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db

# bcrypt silently truncates at 72 bytes. Reject longer passwords explicitly
# instead of letting two different 80-character passwords authenticate for
# each other.
BCRYPT_MAX_BYTES = 72

bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt."""
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        raise ValueError(f"Password must be at most {BCRYPT_MAX_BYTES} bytes when UTF-8 encoded")
    salt = bcrypt.gensalt(rounds=settings.bcrypt_rounds)
    return bcrypt.hashpw(encoded, salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time check of a password against its hash."""
    encoded = password.encode("utf-8")
    if len(encoded) > BCRYPT_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        # Malformed hash in the database: a failed login, never a 500.
        return False


def create_access_token(
    subject: str,
    *,
    expires_delta: dt.timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Issue a signed JWT for a user id."""
    now = dt.datetime.now(dt.UTC)
    expire = now + (expires_delta or dt.timedelta(minutes=settings.access_token_ttl_minutes))
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "type": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify a JWT and return its claims. Raises 401 on any problem."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: Annotated[Session, Depends(get_db)],
):
    """Resolve the bearer token to a live user row.

    The user is re-read from the database on every request rather than trusted
    from the token body, so deactivating an account takes effect immediately
    instead of whenever the token happens to expire.
    """
    from app.models.user import User

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User no longer exists",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated",
        )
    request.state.user_id = user.id
    return user


def require_operator(user=Depends(get_current_user)):
    """Grid operations are privileged: viewers watch, operators act."""
    if user.role not in ("operator", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator role required",
        )
    return user


def require_admin(user=Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user
