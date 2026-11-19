"""User accounts and roles."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.types import UtcDateTime, utcnow

# viewer   -> read-only dashboards
# operator -> can dispatch, acknowledge alerts, curtail
# admin    -> everything, plus user management
ROLES = ("viewer", "operator", "admin")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), default="viewer", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, default=utcnow, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.email} ({self.role})>"
