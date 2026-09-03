"""FastAPI dependencies: tenant resolution, authentication, authorisation.

Ordering matters and is made explicit here: resolve the municipality, then the
actor, then check the actor belongs to that municipality, then check the
permission. Every protected endpoint composes these rather than re-implementing
the checks.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Callable

import structlog
from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core import context
from civicos.core.config import Settings, get_settings
from civicos.core.context import Actor
from civicos.core.errors import (
    AuthenticationError,
    PermissionDeniedError,
    RateLimitedError,
    TenantResolutionError,
)
from civicos.core.i18n import normalise_language
from civicos.core.pagination import PageParams, page_params
from civicos.core.permissions import Role, is_staff
from civicos.core.rate_limit import get_rate_limiter
from civicos.core.security import decode_token
from civicos.db.session import get_session
from civicos.domain.identity import User
from civicos.domain.tenancy import Municipality
from civicos.services import auth_service

logger = structlog.get_logger(__name__)

bearer_scheme = HTTPBearer(auto_error=False, description="JWT access token")

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PageDep = Annotated[PageParams, Depends(page_params)]


async def get_tenant(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
) -> Municipality:
    """Resolve the municipality this request belongs to.

    A token's tenant claim wins over a header, so a signed-in user cannot be
    tricked into acting against another municipality by a crafted header.
    """
    slug: str | None = None

    if credentials and "jwt" in settings.tenancy.resolution_order:
        try:
            claims = decode_token(credentials.credentials, expected_type="access")
            if claims.tenant_id:
                tenant = await session.get(Municipality, uuid.UUID(claims.tenant_id))
                if tenant is not None:
                    _bind_tenant(request, tenant)
                    return tenant
        except (AuthenticationError, ValueError):
            pass  # fall through to the other strategies; auth is checked later

    slug = getattr(request.state, "tenant_slug_hint", None)
    if not slug:
        raise TenantResolutionError(
            "Specify the municipality with the "
            f"'{settings.tenancy.header_name}' header.",
            details={"header": settings.tenancy.header_name},
        )

    tenant = await auth_service.get_tenant_by_slug(session, slug)
    _bind_tenant(request, tenant)
    return tenant


def _bind_tenant(request: Request, tenant: Municipality) -> None:
    request.state.tenant = tenant
    context.set_tenant(tenant.id, tenant.slug)
    context.set_language(
        normalise_language(
            getattr(request.state, "language", None) or tenant.default_language
        )
    )


TenantDep = Annotated[Municipality, Depends(get_tenant)]


async def get_optional_actor(
    request: Request,
    session: SessionDep,
    tenant: TenantDep,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(bearer_scheme)
    ] = None,
    api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> Actor:
    """Identify the caller if possible; anonymous is a valid answer here.

    Public endpoints use this so an authenticated resident gets a personalised
    response while an anonymous one still gets a working page.
    """
    if credentials:
        claims = decode_token(credentials.credentials, expected_type="access")
        user = await auth_service.load_user(session, uuid.UUID(claims.subject))
        if user.tenant_id != tenant.id and not user.is_superadmin:
            raise PermissionDeniedError(
                "This account belongs to a different municipality.",
                code="tenant_mismatch",
            )
        actor = auth_service.actor_from_user(user)
        request.state.user = user
        context.set_actor(actor)
        context.set_language(user.language)
        return actor

    if api_key:
        key = await auth_service.resolve_api_key(session, api_key)
        if key is None:
            raise AuthenticationError("That API key is not valid.", code="api_key_invalid")
        if key.tenant_id != tenant.id:
            raise PermissionDeniedError(
                "That API key belongs to a different municipality.", code="tenant_mismatch"
            )
        actor = auth_service.actor_from_api_key(key)
        context.set_actor(actor)
        return actor

    context.set_actor(context.ANONYMOUS_ACTOR)
    return context.ANONYMOUS_ACTOR


OptionalActorDep = Annotated[Actor, Depends(get_optional_actor)]


async def get_actor(actor: OptionalActorDep) -> Actor:
    """Require an authenticated caller."""
    if not actor.is_authenticated:
        raise AuthenticationError(
            "Sign in to perform this action.", code="authentication_required"
        )
    return actor


ActorDep = Annotated[Actor, Depends(get_actor)]


async def get_current_user(request: Request, actor: ActorDep) -> User:
    """The authenticated :class:`User` row (rejects machine clients)."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise AuthenticationError(
            "This endpoint requires a user account, not an API key.",
            code="user_account_required",
        )
    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]


def require_permission(*permissions: str) -> Callable[..., Any]:
    """Dependency factory: caller must hold *all* the named permissions."""

    async def _check(actor: ActorDep) -> Actor:
        missing = [p for p in permissions if not actor.has_permission(p)]
        if missing:
            logger.info(
                "permission_denied",
                required=list(permissions),
                missing=missing,
                role=actor.role,
            )
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={"required": list(permissions)},
            )
        return actor

    return _check


def require_any_permission(*permissions: str) -> Callable[..., Any]:
    """Caller must hold at least one of the named permissions."""

    async def _check(actor: ActorDep) -> Actor:
        if not any(actor.has_permission(p) for p in permissions):
            raise PermissionDeniedError(
                "You do not have permission to perform this action.",
                details={"required_any": list(permissions)},
            )
        return actor

    return _check


def require_staff() -> Callable[..., Any]:
    async def _check(actor: ActorDep) -> Actor:
        if not (actor.is_superadmin or (actor.role and is_staff(actor.role))):
            raise PermissionDeniedError("This area is restricted to municipal staff.")
        return actor

    return _check


def require_role(*roles: Role) -> Callable[..., Any]:
    allowed = {str(role) for role in roles}

    async def _check(actor: ActorDep) -> Actor:
        if actor.is_superadmin or actor.role in allowed:
            return actor
        raise PermissionDeniedError(
            "Your role does not permit this action.",
            details={"allowed_roles": sorted(allowed)},
        )

    return _check


def require_superadmin() -> Callable[..., Any]:
    async def _check(actor: ActorDep) -> Actor:
        if not actor.is_superadmin:
            raise PermissionDeniedError("Platform administrator access is required.")
        return actor

    return _check


def ai_rate_limit(capability: str = "ai") -> Callable[..., Any]:
    """Tighter throttle for endpoints that spend money on every call."""

    async def _check(
        request: Request, actor: OptionalActorDep, settings: SettingsDep
    ) -> None:
        if not settings.rate_limit.enabled:
            return
        identity = (
            f"user:{actor.id}"
            if actor.is_authenticated
            else f"ip:{request.client.host if request.client else 'unknown'}"
        )
        limiter = await get_rate_limiter()
        result = await limiter.hit(
            f"{capability}:{identity}",
            limit=settings.rate_limit.ai_per_minute,
            window_seconds=60,
        )
        if not result.allowed:
            raise RateLimitedError(
                "Too many AI requests. Please wait a moment.",
                details={"retry_after_seconds": result.retry_after_seconds},
            )

    return _check


async def get_language(
    request: Request,
    tenant: TenantDep,
    lang: Annotated[str | None, Query(description="Override the response language.")] = None,
) -> str:
    """Resolve the response language: query > user > Accept-Language > tenant."""
    if lang:
        resolved = normalise_language(lang)
    else:
        user = getattr(request.state, "user", None)
        resolved = normalise_language(
            (user.language if user else None)
            or getattr(request.state, "language", None)
            or tenant.default_language
        )
    context.set_language(resolved)
    return resolved


LanguageDep = Annotated[str, Depends(get_language)]


def client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    return request.headers.get("User-Agent")


async def scoped_to_department(actor: ActorDep) -> uuid.UUID | None:
    """Department heads and supervisors see their own department by default."""
    if actor.is_superadmin or actor.role in {str(Role.TENANT_ADMIN), str(Role.ANALYST)}:
        return None
    return actor.department_id
