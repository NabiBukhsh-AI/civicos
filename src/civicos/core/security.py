"""Password hashing, JWT issuance/verification and API-key handling."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from civicos.core.config import get_settings
from civicos.core.errors import AuthenticationError, ValidationError

TokenType = Literal["access", "refresh"]

_BCRYPT_MAX_BYTES = 72
API_KEY_PREFIX = "civ"


# --------------------------------------------------------------- passwords ---


def hash_password(password: str) -> str:
    """Hash a password with bcrypt.

    Passwords longer than bcrypt's 72-byte limit are pre-hashed with SHA-256 so
    that long passphrases are not silently truncated.
    """
    return bcrypt.hashpw(_prepare_password(password), bcrypt.gensalt(get_settings().security.bcrypt_rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(_prepare_password(password), hashed.encode())
    except ValueError:
        return False


def _prepare_password(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raw = base64.b64encode(hashlib.sha256(raw).digest())
    return raw


def validate_password_strength(password: str) -> None:
    """Reject weak passwords before they reach the hasher."""
    settings = get_settings()
    minimum = settings.security.password_min_length
    problems: list[str] = []
    if len(password) < minimum:
        problems.append(f"must be at least {minimum} characters")
    if not any(c.isalpha() for c in password):
        problems.append("must contain a letter")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    if password.lower() in _COMMON_PASSWORDS:
        problems.append("is too common")
    if problems:
        raise ValidationError(
            "Password " + ", ".join(problems) + ".",
            code="weak_password",
            details={"problems": problems},
        )


_COMMON_PASSWORDS = {
    "password",
    "password1",
    "password123",
    "12345678",
    "123456789",
    "qwerty123",
    "administrator",
    "letmein123",
}


# ------------------------------------------------------------------ tokens ---


@dataclass(slots=True)
class TokenClaims:
    subject: str
    token_type: TokenType
    tenant_id: str | None = None
    role: str | None = None
    session_id: str | None = None
    scopes: tuple[str, ...] = ()
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    raw: dict[str, Any] | None = None


def create_token(
    subject: str | uuid.UUID,
    *,
    token_type: TokenType = "access",
    tenant_id: str | uuid.UUID | None = None,
    role: str | None = None,
    session_id: str | None = None,
    scopes: list[str] | None = None,
    expires_delta: timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    if expires_delta is None:
        expires_delta = (
            timedelta(minutes=settings.security.access_token_ttl_minutes)
            if token_type == "access"
            else timedelta(days=settings.security.refresh_token_ttl_days)
        )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
        "jti": uuid.uuid4().hex,
        "iss": settings.project_name.lower(),
    }
    if tenant_id is not None:
        payload["tid"] = str(tenant_id)
    if role:
        payload["role"] = role
    if session_id:
        payload["sid"] = session_id
    if scopes:
        payload["scopes"] = scopes
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.security.secret_key, algorithm=settings.security.algorithm)


def decode_token(token: str, *, expected_type: TokenType | None = None) -> TokenClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.security.secret_key,
            algorithms=[settings.security.algorithm],
            issuer=settings.project_name.lower(),
            options={"require": ["exp", "sub", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired.", code="token_expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("Token is invalid.", code="token_invalid") from exc

    token_type = payload.get("typ")
    if expected_type and token_type != expected_type:
        raise AuthenticationError(
            f"Expected a {expected_type} token.", code="token_wrong_type"
        )

    return TokenClaims(
        subject=payload["sub"],
        token_type=token_type,
        tenant_id=payload.get("tid"),
        role=payload.get("role"),
        session_id=payload.get("sid"),
        scopes=tuple(payload.get("scopes") or ()),
        issued_at=datetime.fromtimestamp(payload["iat"], UTC) if "iat" in payload else None,
        expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        raw=payload,
    )


# ---------------------------------------------------------------- api keys ---


def generate_api_key() -> tuple[str, str, str]:
    """Create an API key.

    Returns ``(plaintext, lookup_prefix, hashed)``. Only the prefix and hash are
    persisted; the plaintext is shown to the operator exactly once.
    """
    secret = secrets.token_urlsafe(32)
    prefix = secrets.token_hex(4)
    plaintext = f"{API_KEY_PREFIX}_{prefix}_{secret}"
    return plaintext, prefix, hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    """Hash an API key with HMAC-SHA256 keyed by the app secret."""
    settings = get_settings()
    return hmac.new(
        settings.security.secret_key.encode(), plaintext.encode(), hashlib.sha256
    ).hexdigest()


def api_key_prefix(plaintext: str) -> str | None:
    parts = plaintext.split("_")
    if len(parts) < 3 or parts[0] != API_KEY_PREFIX:
        return None
    return parts[1]


def constant_time_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


# ------------------------------------------------------------------- misc ----


def generate_reference(prefix: str, length: int = 6) -> str:
    """Human-quotable reference code, e.g. ``ABC-7K2QF4``.

    Uses an unambiguous alphabet (no O/0/I/1) because these codes are read out
    over the phone at helpline desks.
    """
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    body = "".join(secrets.choice(alphabet) for _ in range(length))
    return f"{prefix.upper()}-{body}"


def generate_numeric_code(digits: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(digits))


def mask_contact(value: str | None) -> str | None:
    """Mask a phone number or email for display in public/audit surfaces."""
    if not value:
        return None
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}{'*' * max(len(local) - len(head), 1)}@{domain}"
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{digits[:3]}{'*' * (len(digits) - 6)}{digits[-3:]}"
