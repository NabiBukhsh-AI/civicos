"""Authentication and user-management contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator, model_validator

from civicos.core.permissions import Role
from civicos.schemas.common import APIModel, InputModel


class LoginRequest(InputModel):
    identifier: str = Field(
        min_length=3,
        max_length=160,
        description="Email address or phone number.",
        examples=["clerk@municipality.gov"],
    )
    password: str = Field(min_length=1, max_length=256)


class OTPRequest(InputModel):
    target: str = Field(min_length=6, max_length=160, description="Phone number or email.")
    purpose: str = Field(default="login", pattern="^(login|verify|password_reset)$")


class OTPVerifyRequest(InputModel):
    target: str = Field(min_length=6, max_length=160)
    code: str = Field(min_length=4, max_length=8, pattern=r"^\d+$")
    purpose: str = Field(default="login", pattern="^(login|verify|password_reset)$")
    full_name: str | None = Field(default=None, max_length=160)


class RefreshRequest(InputModel):
    refresh_token: str = Field(min_length=16)


class RegisterRequest(InputModel):
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    password: str | None = Field(default=None, min_length=8, max_length=256)
    language: str | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def _require_contact(self) -> RegisterRequest:
        if not self.email and not self.phone:
            raise ValueError("Provide an email address or a phone number.")
        return self


class StaffCreateRequest(RegisterRequest):
    """Administrators create staff accounts; residents self-register."""

    role: Role = Role.FIELD_AGENT
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    designation: str | None = Field(default=None, max_length=120)
    employee_number: str | None = Field(default=None, max_length=48)

    @field_validator("role")
    @classmethod
    def _reject_superadmin(cls, value: Role) -> Role:
        if value is Role.SUPER_ADMIN:
            raise ValueError("Platform administrators cannot be created through this endpoint.")
        return value


class PasswordChangeRequest(InputModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class TokenResponse(APIModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(APIModel):
    id: uuid.UUID
    full_name: str
    local_name: str | None = None
    email: str | None = None
    phone: str | None = None
    role: Role
    status: str
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    designation: str | None = None
    language: str
    is_verified: bool
    avatar_url: str | None = None
    trust_score: int
    reports_submitted: int
    last_login_at: datetime | None = None
    created_at: datetime


class UserUpdateRequest(InputModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    local_name: str | None = Field(default=None, max_length=160)
    language: str | None = Field(default=None, max_length=8)
    address: str | None = Field(default=None, max_length=500)
    notification_channels: list[str] | None = None


class StaffUpdateRequest(UserUpdateRequest):
    role: Role | None = None
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    designation: str | None = Field(default=None, max_length=120)
    status: str | None = Field(default=None, pattern="^(active|suspended|deactivated|invited)$")


class AuthenticatedUser(APIModel):
    """The ``/auth/me`` payload: identity plus resolved capabilities."""

    user: UserOut
    tenant_id: uuid.UUID
    tenant_slug: str
    permissions: list[str]
    is_superadmin: bool


class SessionOut(APIModel):
    id: uuid.UUID
    device_label: str | None = None
    user_agent: str | None = None
    ip_address: str | None = None
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime


class ApiKeyCreateRequest(InputModel):
    name: str = Field(min_length=2, max_length=120)
    scopes: list[str] = Field(default_factory=list, max_length=40)
    expires_in_days: int | None = Field(default=365, ge=1, le=3650)


class ApiKeyOut(APIModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    request_count: int


class ApiKeyCreatedOut(ApiKeyOut):
    """Returned once, at creation. The plaintext is never retrievable again."""

    api_key: str
