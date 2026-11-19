"""Auth request/response models."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

Role = Literal["viewer", "operator", "admin"]


class UserRegister(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=10, max_length=72)

    @field_validator("full_name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) < 2:
            raise ValueError("full_name must contain at least 2 characters")
        return cleaned

    @field_validator("password")
    @classmethod
    def _password_strength(cls, value: str) -> str:
        # Length is the only rule that reliably helps. Composition rules
        # ("one symbol, one digit") push people towards Password1! and are
        # actively harmful, so this checks length and rejects the obvious.
        if len(value.encode("utf-8")) > 72:
            raise ValueError("Password must be at most 72 bytes when UTF-8 encoded")
        if value.lower() in {"password12", "1234567890", "qwertyuiop"}:
            raise ValueError("That password is too common")
        return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=72)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    full_name: str
    role: Role
    is_active: bool
    created_at: dt.datetime
    last_login_at: dt.datetime | None = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class RoleUpdate(BaseModel):
    role: Role
