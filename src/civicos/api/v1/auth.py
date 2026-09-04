"""Authentication endpoints."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status

from civicos.api.deps import (
    ActorDep,
    CurrentUserDep,
    SessionDep,
    TenantDep,
    client_ip,
    require_permission,
    user_agent,
)
from civicos.core.permissions import Resource, perm
from civicos.domain.enums import AuditAction
from civicos.schemas.auth import (
    ApiKeyCreatedOut,
    ApiKeyCreateRequest,
    ApiKeyOut,
    AuthenticatedUser,
    LoginRequest,
    OTPRequest,
    OTPVerifyRequest,
    PasswordChangeRequest,
    RefreshRequest,
    RegisterRequest,
    SessionOut,
    StaffCreateRequest,
    TokenResponse,
    UserOut,
    UserUpdateRequest,
)
from civicos.schemas.common import Message
from civicos.services import audit_service, auth_service
from civicos.services.notification_service import Recipient, notify

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: Request,
    payload: RegisterRequest,
    session: SessionDep,
    tenant: TenantDep,
) -> TokenResponse:
    """Self-registration for residents."""
    await auth_service.register(
        session,
        tenant,
        full_name=payload.full_name,
        email=str(payload.email) if payload.email else None,
        phone=payload.phone,
        password=payload.password,
        language=payload.language,
    )
    if payload.password:
        result = await auth_service.authenticate(
            session,
            tenant,
            str(payload.email or payload.phone),
            payload.password,
            user_agent=user_agent(request),
            ip_address=client_ip(request),
        )
        await session.commit()
        return TokenResponse(**asdict(result.tokens))

    # No password: the account exists but must be claimed with an OTP.
    await session.commit()
    return TokenResponse(access_token="", refresh_token="", expires_in=0)


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    payload: LoginRequest,
    session: SessionDep,
    tenant: TenantDep,
) -> TokenResponse:
    result = await auth_service.authenticate(
        session,
        tenant,
        payload.identifier,
        payload.password,
        user_agent=user_agent(request),
        ip_address=client_ip(request),
    )
    await session.commit()
    return TokenResponse(**asdict(result.tokens))


@router.post("/otp/request", response_model=Message)
async def request_otp(payload: OTPRequest, session: SessionDep, tenant: TenantDep) -> Message:
    """Send a one-time code.

    The response is deliberately identical whether or not an account exists, so
    the endpoint cannot be used to enumerate residents.
    """
    entry, code = await auth_service.request_otp(
        session, tenant, payload.target, purpose=payload.purpose
    )
    recipient = (
        Recipient(phone=entry.target) if "@" not in entry.target else Recipient(email=entry.target)
    )
    await notify(
        session,
        tenant.id,
        [recipient],
        template_key="auth.otp",
        body=f"Your {tenant.name} verification code is {code}. It expires in 10 minutes.",
        subject="Verification code",
    )
    await session.commit()
    return Message(message="If that contact is valid, a verification code has been sent.")


@router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(
    request: Request,
    payload: OTPVerifyRequest,
    session: SessionDep,
    tenant: TenantDep,
) -> TokenResponse:
    result = await auth_service.verify_otp(
        session,
        tenant,
        payload.target,
        payload.code,
        purpose=payload.purpose,
        full_name=payload.full_name,
        user_agent=user_agent(request),
        ip_address=client_ip(request),
    )
    await session.commit()
    return TokenResponse(**asdict(result.tokens))


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, session: SessionDep, tenant: TenantDep) -> TokenResponse:
    result = await auth_service.refresh_tokens(session, tenant, payload.refresh_token)
    await session.commit()
    return TokenResponse(**asdict(result.tokens))


@router.post("/logout", response_model=Message)
async def logout(payload: RefreshRequest, session: SessionDep, tenant: TenantDep) -> Message:
    revoked = await auth_service.sign_out(session, payload.refresh_token)
    await session.commit()
    return Message(message="Signed out.", detail=f"{revoked} session(s) revoked.")


@router.post("/logout-all", response_model=Message)
async def logout_everywhere(
    session: SessionDep, tenant: TenantDep, user: CurrentUserDep
) -> Message:
    revoked = await auth_service.sign_out(session, user_id=user.id)
    await session.commit()
    return Message(message="Signed out on all devices.", detail=f"{revoked} session(s) revoked.")


@router.get("/me", response_model=AuthenticatedUser)
async def me(tenant: TenantDep, actor: ActorDep, user: CurrentUserDep) -> AuthenticatedUser:
    return AuthenticatedUser(
        user=UserOut.model_validate(user),
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        permissions=sorted(actor.permissions),
        is_superadmin=actor.is_superadmin,
    )


@router.patch("/me", response_model=UserOut)
async def update_me(
    payload: UserUpdateRequest, session: SessionDep, tenant: TenantDep, user: CurrentUserDep
) -> UserOut:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await session.commit()
    return UserOut.model_validate(user)


@router.post("/me/password", response_model=Message)
async def change_password(
    payload: PasswordChangeRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> Message:
    await auth_service.change_password(
        session, user, payload.current_password, payload.new_password
    )
    await audit_service.record(
        session,
        action=AuditAction.UPDATE,
        entity_type="user",
        entity_id=user.id,
        entity_label=user.identifier,
        summary="Password changed",
        tenant_id=tenant.id,
    )
    await session.commit()
    return Message(
        message="Password changed.",
        detail="You have been signed out on all other devices.",
    )


@router.get("/me/sessions", response_model=list[SessionOut])
async def my_sessions(
    session: SessionDep, tenant: TenantDep, user: CurrentUserDep
) -> list[SessionOut]:
    rows = await auth_service.list_sessions(session, user.id)
    return [SessionOut.model_validate(row) for row in rows]


@router.post(
    "/staff",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.USER, "create")))],
)
async def create_staff(
    payload: StaffCreateRequest, session: SessionDep, tenant: TenantDep
) -> UserOut:
    """Create a staff account. Administrators only."""
    user = await auth_service.register(
        session,
        tenant,
        full_name=payload.full_name,
        email=str(payload.email) if payload.email else None,
        phone=payload.phone,
        password=payload.password,
        role=payload.role,
        language=payload.language,
        department_id=payload.department_id,
        admin_unit_id=payload.admin_unit_id,
        designation=payload.designation,
        created_by_staff=True,
    )
    await session.commit()
    return UserOut.model_validate(user)


@router.post(
    "/api-keys",
    response_model=ApiKeyCreatedOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_permission(perm(Resource.USER, "create")))],
)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    session: SessionDep,
    tenant: TenantDep,
    user: CurrentUserDep,
) -> ApiKeyCreatedOut:
    """Mint an API key for a machine client.

    The plaintext key is shown exactly once - it is not recoverable afterwards.
    """
    api_key, plaintext = await auth_service.issue_api_key(
        session,
        tenant,
        name=payload.name,
        scopes=payload.scopes,
        expires_in_days=payload.expires_in_days,
        created_by_user_id=user.id,
    )
    await session.commit()
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(api_key).model_dump(), api_key=plaintext)


@router.delete(
    "/api-keys/{key_id}",
    response_model=Message,
    dependencies=[Depends(require_permission(perm(Resource.USER, "delete")))],
)
async def revoke_api_key(
    key_id: Annotated[uuid.UUID, ...], session: SessionDep, tenant: TenantDep
) -> Message:
    from civicos.domain.identity import ApiKey

    api_key = await session.get(ApiKey, key_id)
    if api_key is None or api_key.tenant_id != tenant.id:
        from civicos.core.errors import NotFoundError

        raise NotFoundError("API key not found.", code="api_key_not_found")
    await auth_service.revoke_api_key(session, api_key)
    await session.commit()
    return Message(message="API key revoked.")
