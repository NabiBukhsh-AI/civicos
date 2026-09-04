"""Request-scoped context.

Context variables let deep layers (repositories, audit log, AI usage metering)
know which municipality and which actor a unit of work belongs to without
threading arguments through every call site.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

_request_id: ContextVar[str | None] = ContextVar("civicos_request_id", default=None)
_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("civicos_tenant_id", default=None)
_tenant_slug: ContextVar[str | None] = ContextVar("civicos_tenant_slug", default=None)
_actor: ContextVar[Actor | None] = ContextVar("civicos_actor", default=None)
_language: ContextVar[str] = ContextVar("civicos_language", default="en")


@dataclass(slots=True)
class Actor:
    """Whoever is performing the current unit of work."""

    id: uuid.UUID | None = None
    kind: str = "anonymous"  # user | service | system | anonymous
    email: str | None = None
    display_name: str | None = None
    role: str | None = None
    tenant_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    permissions: frozenset[str] = field(default_factory=frozenset)
    is_superadmin: bool = False

    @property
    def is_authenticated(self) -> bool:
        return self.kind in {"user", "service"} and self.id is not None

    def has_permission(self, permission: str) -> bool:
        if self.is_superadmin:
            return True
        if permission in self.permissions:
            return True
        # Wildcard support: "issues:*" grants "issues:update".
        resource = permission.split(":", 1)[0]
        return f"{resource}:*" in self.permissions

    def describe(self) -> dict[str, Any]:
        return {
            "actor_id": str(self.id) if self.id else None,
            "actor_kind": self.kind,
            "actor_role": self.role,
        }


SYSTEM_ACTOR = Actor(kind="system", display_name="system", is_superadmin=True)
ANONYMOUS_ACTOR = Actor(kind="anonymous", display_name="anonymous")


def new_request_id() -> str:
    return uuid.uuid4().hex


def set_request_id(value: str | None) -> Token[str | None]:
    return _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def set_tenant(tenant_id: uuid.UUID | None, slug: str | None = None) -> None:
    _tenant_id.set(tenant_id)
    _tenant_slug.set(slug)


def get_tenant_id() -> uuid.UUID | None:
    return _tenant_id.get()


def get_tenant_slug() -> str | None:
    return _tenant_slug.get()


def set_actor(actor: Actor | None) -> Token[Actor | None]:
    return _actor.set(actor)


def get_actor() -> Actor:
    return _actor.get() or ANONYMOUS_ACTOR


def set_language(language: str) -> None:
    _language.set(language)


def get_language() -> str:
    return _language.get()


@contextmanager
def use_context(
    *,
    actor: Actor | None = None,
    tenant_id: uuid.UUID | None = None,
    tenant_slug: str | None = None,
    request_id: str | None = None,
    language: str | None = None,
) -> Iterator[None]:
    """Temporarily bind a context (used by workers, CLI commands and tests)."""
    tokens: list[tuple[ContextVar[Any], Token[Any]]] = []
    if actor is not None:
        tokens.append((_actor, _actor.set(actor)))
    if tenant_id is not None:
        tokens.append((_tenant_id, _tenant_id.set(tenant_id)))
    if tenant_slug is not None:
        tokens.append((_tenant_slug, _tenant_slug.set(tenant_slug)))
    if request_id is not None:
        tokens.append((_request_id, _request_id.set(request_id)))
    if language is not None:
        tokens.append((_language, _language.set(language)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
