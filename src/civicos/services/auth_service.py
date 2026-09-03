"""Authentication: registration, sign-in, sessions, API keys, phone OTP.

Two identity styles coexist because municipal reality demands it: staff sign in
with an email and password, while many residents have no email address at all
and authenticate with a phone number and a one-time code.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Sequence

import structlog
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.clock import utcnow
from civicos.core.config import get_settings
from civicos.core.context import Actor
from civicos.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from civicos.core.permissions import Role, is_staff, permissions_for
from civicos.core.security import (
    api_key_prefix,
    create_token,
    decode_token,
    generate_api_key,
    generate_numeric_code,
    hash_api_key,
    hash_password,
    validate_password_strength,
    verify_password,
)
from civicos.domain.enums import AuditAction, UserStatus, VerificationMethod
from civicos.domain.identity import ApiKey, OneTimeCode, User, UserSession
from civicos.domain.tenancy import Municipality
from civicos.services import audit_service

logger = structlog.get_logger(__name__)

OTP_PURPOSE_LOGIN = "login"
OTP_PURPOSE_VERIFY = "verify"
OTP_PURPOSE_RESET = "password_reset"
MAX_OTP_ATTEMPTS = 5


@dataclass(slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 0


@dataclass(slots=True)
class AuthResult:
    user: User
    tokens: TokenPair
    session_id: uuid.UUID


# ------------------------------------------------------------- registration --


async def register(
    session: AsyncSession,
    tenant: Municipality,
    *,
    full_name: str,
    email: str | None = None,
    phone: str | None = None,
    password: str | None = None,
    role: Role = Role.CITIZEN,
    language: str | None = None,
    department_id: uuid.UUID | None = None,
    admin_unit_id: uuid.UUID | None = None,
    designation: str | None = None,
    created_by_staff: bool = False,
) -> User:
    """Create an account. Residents may register with a phone number alone."""
    email = (email or "").strip().lower() or None
    phone = normalise_phone(phone)
    if not email and not phone:
        raise ValidationError(
            "An email address or a phone number is required.", code="contact_required"
        )
    if role is not Role.CITIZEN and not created_by_staff:
        raise PermissionDeniedError("Staff accounts must be created by an administrator.")
    if is_staff(role) and not password:
        raise ValidationError("Staff accounts require a password.", code="password_required")

    if await _find_by_identifier(session, tenant.id, email or phone or "") is not None:
        raise ConflictError(
            "An account with those details already exists.", code="user_exists"
        )

    password_hash = None
    if password:
        validate_password_strength(password)
        password_hash = hash_password(password)

    user = User(
        tenant_id=tenant.id,
        email=email,
        phone=phone,
        password_hash=password_hash,
        full_name=full_name.strip(),
        role=role,
        status=UserStatus.ACTIVE if password_hash else UserStatus.INVITED,
        language=language or tenant.default_language,
        department_id=department_id,
        admin_unit_id=admin_unit_id,
        designation=designation,
        password_changed_at=utcnow() if password_hash else None,
    )
    session.add(user)
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="user",
        entity_id=user.id,
        entity_label=user.identifier,
        summary=f"Account created with role '{role.value}'",
        tenant_id=tenant.id,
    )
    return user


# ------------------------------------------------------------------ sign-in --


async def authenticate(
    session: AsyncSession,
    tenant: Municipality,
    identifier: str,
    password: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthResult:
    """Password sign-in with lockout after repeated failures."""
    settings = get_settings()
    user = await _find_by_identifier(session, tenant.id, identifier)

    if user is None or not user.password_hash:
        # Uniform failure: never reveal whether an account exists.
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED,
            entity_type="user",
            entity_label=identifier,
            summary="Sign-in failed: unknown account",
            tenant_id=tenant.id,
            succeeded=False,
            ip_address=ip_address,
        )
        raise AuthenticationError("Those sign-in details are not correct.", code="bad_credentials")

    if user.is_locked:
        raise AuthenticationError(
            "This account is temporarily locked after repeated failed attempts.",
            code="account_locked",
        )
    if not user.is_active:
        raise AuthenticationError("This account is not active.", code="account_inactive")

    if not verify_password(password, user.password_hash):
        user.failed_login_count += 1
        if user.failed_login_count >= settings.security.max_failed_logins:
            user.locked_until = utcnow() + timedelta(minutes=settings.security.lockout_minutes)
            logger.warning("account_locked", user_id=str(user.id))
        await audit_service.record(
            session,
            action=AuditAction.LOGIN_FAILED,
            entity_type="user",
            entity_id=user.id,
            entity_label=user.identifier,
            summary=f"Sign-in failed (attempt {user.failed_login_count})",
            tenant_id=tenant.id,
            succeeded=False,
            ip_address=ip_address,
        )
        await session.flush()
        raise AuthenticationError("Those sign-in details are not correct.", code="bad_credentials")

    return await _establish_session(
        session, tenant, user, user_agent=user_agent, ip_address=ip_address
    )


async def request_otp(
    session: AsyncSession,
    tenant: Municipality,
    target: str,
    *,
    purpose: str = OTP_PURPOSE_LOGIN,
) -> tuple[OneTimeCode, str]:
    """Issue a one-time code. Returns the row and the plaintext code.

    The caller sends the plaintext over SMS; only its hash is stored, so a
    database leak cannot be replayed into account access.
    """
    settings = get_settings()
    target = normalise_phone(target) or target.strip().lower()

    # Invalidate any outstanding codes for this target so only one is live.
    await session.execute(
        update(OneTimeCode)
        .where(
            OneTimeCode.tenant_id == tenant.id,
            OneTimeCode.target == target,
            OneTimeCode.purpose == purpose,
            OneTimeCode.consumed_at.is_(None),
        )
        .values(consumed_at=utcnow())
    )

    code = generate_numeric_code(6)
    user = await _find_by_identifier(session, tenant.id, target)
    entry = OneTimeCode(
        tenant_id=tenant.id,
        target=target,
        purpose=purpose,
        code_hash=_hash_code(code, target),
        expires_at=utcnow() + timedelta(minutes=10),
        user_id=user.id if user else None,
    )
    session.add(entry)
    await session.flush()
    logger.info("otp_issued", purpose=purpose, has_account=user is not None)
    return entry, code


async def verify_otp(
    session: AsyncSession,
    tenant: Municipality,
    target: str,
    code: str,
    *,
    purpose: str = OTP_PURPOSE_LOGIN,
    full_name: str | None = None,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> AuthResult:
    """Consume a one-time code, creating the resident's account if needed."""
    target = normalise_phone(target) or target.strip().lower()
    entry = await session.scalar(
        select(OneTimeCode)
        .where(
            OneTimeCode.tenant_id == tenant.id,
            OneTimeCode.target == target,
            OneTimeCode.purpose == purpose,
            OneTimeCode.consumed_at.is_(None),
        )
        .order_by(OneTimeCode.created_at.desc())
        .limit(1)
    )
    if entry is None:
        raise AuthenticationError("No verification code is pending.", code="otp_not_found")
    if entry.expires_at < utcnow():
        raise AuthenticationError("That code has expired.", code="otp_expired")

    entry.attempts += 1
    if entry.attempts > MAX_OTP_ATTEMPTS:
        entry.consumed_at = utcnow()
        await session.flush()
        raise AuthenticationError("Too many incorrect attempts.", code="otp_attempts_exceeded")

    if entry.code_hash != _hash_code(code, target):
        await session.flush()
        raise AuthenticationError("That code is not correct.", code="otp_invalid")

    entry.consumed_at = utcnow()
    user = await _find_by_identifier(session, tenant.id, target)
    if user is None:
        is_phone = "@" not in target
        user = User(
            tenant_id=tenant.id,
            phone=target if is_phone else None,
            email=None if is_phone else target,
            full_name=(full_name or "Resident").strip(),
            role=Role.CITIZEN,
            status=UserStatus.ACTIVE,
            language=tenant.default_language,
            is_verified=True,
            verification_method=VerificationMethod.PHONE_OTP
            if is_phone
            else VerificationMethod.EMAIL,
        )
        session.add(user)
        await session.flush()
    elif not user.is_verified:
        user.is_verified = True
        user.verification_method = VerificationMethod.PHONE_OTP

    return await _establish_session(
        session, tenant, user, user_agent=user_agent, ip_address=ip_address
    )


async def refresh_tokens(
    session: AsyncSession, tenant: Municipality, refresh_token: str
) -> AuthResult:
    """Rotate a refresh token, revoking the old session.

    Rotation means a stolen refresh token is usable at most once, and the
    legitimate holder's next refresh fails loudly instead of silently sharing
    the session.
    """
    claims = decode_token(refresh_token, expected_type="refresh")
    token_hash = _hash_token(refresh_token)

    user_session = await session.scalar(
        select(UserSession).where(UserSession.refresh_token_hash == token_hash)
    )
    if user_session is None or not user_session.is_active:
        raise AuthenticationError("That session is no longer valid.", code="session_invalid")

    user = await session.get(User, uuid.UUID(claims.subject))
    if user is None or user.tenant_id != tenant.id or not user.is_active:
        raise AuthenticationError("That session is no longer valid.", code="session_invalid")

    user_session.revoked_at = utcnow()
    user_session.revoked_reason = "rotated"
    return await _establish_session(
        session,
        tenant,
        user,
        user_agent=user_session.user_agent,
        ip_address=user_session.ip_address,
    )


async def sign_out(
    session: AsyncSession, refresh_token: str | None = None, *, user_id: uuid.UUID | None = None
) -> int:
    """Revoke one session, or every session for a user."""
    now = utcnow()
    statement = update(UserSession).where(UserSession.revoked_at.is_(None))
    if refresh_token:
        statement = statement.where(
            UserSession.refresh_token_hash == _hash_token(refresh_token)
        )
    elif user_id:
        statement = statement.where(UserSession.user_id == user_id)
    else:
        return 0
    result = await session.execute(
        statement.values(revoked_at=now, revoked_reason="signed_out")
    )
    return int(result.rowcount or 0)


async def change_password(
    session: AsyncSession, user: User, current_password: str, new_password: str
) -> User:
    if not user.password_hash or not verify_password(current_password, user.password_hash):
        raise AuthenticationError("The current password is not correct.", code="bad_password")
    validate_password_strength(new_password)
    user.password_hash = hash_password(new_password)
    user.password_changed_at = utcnow()
    user.failed_login_count = 0
    user.locked_until = None
    # Any other device holding a session was authorised under the old secret.
    await sign_out(session, user_id=user.id)
    await session.flush()
    return user


# ----------------------------------------------------------------- api keys --


async def issue_api_key(
    session: AsyncSession,
    tenant: Municipality,
    *,
    name: str,
    scopes: list[str] | None = None,
    expires_in_days: int | None = 365,
    created_by_user_id: uuid.UUID | None = None,
) -> tuple[ApiKey, str]:
    """Mint an API key. The plaintext is returned once and never stored."""
    plaintext, prefix, key_hash = generate_api_key()
    api_key = ApiKey(
        tenant_id=tenant.id,
        name=name,
        key_prefix=prefix,
        key_hash=key_hash,
        scopes=scopes or [],
        created_by_user_id=created_by_user_id,
        expires_at=utcnow() + timedelta(days=expires_in_days) if expires_in_days else None,
    )
    session.add(api_key)
    await session.flush()
    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="api_key",
        entity_id=api_key.id,
        entity_label=name,
        summary=f"API key '{name}' issued",
        tenant_id=tenant.id,
    )
    return api_key, plaintext


async def resolve_api_key(session: AsyncSession, plaintext: str) -> ApiKey | None:
    """Look a key up by its prefix, then verify the hash in constant time."""
    prefix = api_key_prefix(plaintext)
    if not prefix:
        return None
    api_key = await session.scalar(select(ApiKey).where(ApiKey.key_prefix == prefix))
    if api_key is None or not api_key.is_active:
        return None
    if not _constant_time_equals(api_key.key_hash, hash_api_key(plaintext)):
        return None
    api_key.last_used_at = utcnow()
    api_key.request_count += 1
    return api_key


async def revoke_api_key(session: AsyncSession, api_key: ApiKey) -> ApiKey:
    api_key.revoked_at = utcnow()
    await session.flush()
    return api_key


# ------------------------------------------------------------------- actors --


def actor_from_user(user: User) -> Actor:
    """Build the request-scoped actor for an authenticated user."""
    permissions = set(permissions_for(user.role))
    permissions.update(str(item) for item in (user.extra_permissions or []))
    return Actor(
        id=user.id,
        kind="user",
        email=user.email,
        display_name=user.display(),
        role=str(user.role),
        tenant_id=user.tenant_id,
        department_id=user.department_id,
        permissions=frozenset(permissions),
        is_superadmin=user.is_superadmin or user.role is Role.SUPER_ADMIN,
    )


def actor_from_api_key(api_key: ApiKey) -> Actor:
    permissions = set(permissions_for(Role.SERVICE_ACCOUNT))
    permissions.update(str(scope) for scope in (api_key.scopes or []))
    return Actor(
        id=api_key.id,
        kind="service",
        display_name=api_key.name,
        role=str(Role.SERVICE_ACCOUNT),
        tenant_id=api_key.tenant_id,
        permissions=frozenset(permissions),
    )


async def load_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("This account is no longer active.", code="account_inactive")
    return user


async def list_sessions(session: AsyncSession, user_id: uuid.UUID) -> Sequence[UserSession]:
    return (
        await session.scalars(
            select(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .order_by(UserSession.created_at.desc())
        )
    ).all()


# ---------------------------------------------------------------- internals --


async def _establish_session(
    session: AsyncSession,
    tenant: Municipality,
    user: User,
    *,
    user_agent: str | None,
    ip_address: str | None,
) -> AuthResult:
    settings = get_settings()
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = utcnow()

    session_id = uuid.uuid4()
    access_token = create_token(
        user.id,
        token_type="access",
        tenant_id=tenant.id,
        role=str(user.role),
        session_id=str(session_id),
    )
    refresh_token = create_token(
        user.id,
        token_type="refresh",
        tenant_id=tenant.id,
        role=str(user.role),
        session_id=str(session_id),
    )

    session.add(
        UserSession(
            id=session_id,
            user_id=user.id,
            refresh_token_hash=_hash_token(refresh_token),
            expires_at=utcnow() + timedelta(days=settings.security.refresh_token_ttl_days),
            user_agent=user_agent[:255] if user_agent else None,
            ip_address=ip_address,
            last_used_at=utcnow(),
        )
    )
    await session.flush()

    await audit_service.record(
        session,
        action=AuditAction.LOGIN,
        entity_type="user",
        entity_id=user.id,
        entity_label=user.identifier,
        summary="Signed in",
        tenant_id=tenant.id,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return AuthResult(
        user=user,
        session_id=session_id,
        tokens=TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=settings.security.access_token_ttl_minutes * 60,
        ),
    )


async def _find_by_identifier(
    session: AsyncSession, tenant_id: uuid.UUID, identifier: str
) -> User | None:
    identifier = identifier.strip()
    if not identifier:
        return None
    phone = normalise_phone(identifier)
    return await session.scalar(
        select(User).where(
            User.tenant_id == tenant_id,
            User.deleted_at.is_(None),
            or_(
                User.email == identifier.lower(),
                User.phone == (phone or identifier),
            ),
        )
    )


def normalise_phone(value: str | None) -> str | None:
    """Reduce a phone number to digits (plus a leading ``+``).

    Residents type numbers in half a dozen formats; storing one canonical form
    is what makes "have I seen this person before" answerable.
    """
    if not value:
        return None
    cleaned = "".join(char for char in value if char.isdigit() or char == "+")
    if not cleaned or not any(char.isdigit() for char in cleaned):
        return None
    if cleaned.startswith("+"):
        return "+" + "".join(char for char in cleaned[1:] if char.isdigit())
    return cleaned


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _hash_code(code: str, target: str) -> str:
    """Salt the OTP with its target so codes are not interchangeable."""
    settings = get_settings()
    return hashlib.sha256(
        f"{settings.security.secret_key}:{target}:{code}".encode()
    ).hexdigest()


def _constant_time_equals(left: str, right: str) -> bool:
    import hmac  # noqa: PLC0415

    return hmac.compare_digest(left, right)


async def ensure_tenant_member(user: User, tenant_id: uuid.UUID) -> None:
    if user.tenant_id != tenant_id and not user.is_superadmin:
        raise PermissionDeniedError("This account belongs to a different municipality.")


async def get_tenant_by_slug(session: AsyncSession, slug: str) -> Municipality:
    tenant = await session.scalar(
        select(Municipality).where(
            Municipality.slug == slug.lower(), Municipality.deleted_at.is_(None)
        )
    )
    if tenant is None:
        raise NotFoundError(
            f"No municipality is configured with the identifier '{slug}'.",
            code="tenant_not_found",
        )
    return tenant
