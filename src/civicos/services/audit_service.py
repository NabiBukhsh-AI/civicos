"""Audit logging.

Municipal decisions are appealable, so the system has to be able to show who
did what and when. Every consequential write goes through :func:`record`,
which captures only the fields that actually changed - enough to reconstruct a
decision without turning the audit table into a second copy of the database.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core import context
from civicos.core.pagination import PageParams
from civicos.domain.enums import AuditAction
from civicos.domain.operations import AuditLog

logger = structlog.get_logger(__name__)

#: Never written to the audit trail, whatever the caller passes.
_REDACTED_FIELDS = frozenset(
    {
        "password",
        "password_hash",
        "refresh_token_hash",
        "key_hash",
        "code_hash",
        "secret",
        "embedding",
        "applicant_id_hash",
    }
)


async def record(
    session: AsyncSession,
    *,
    action: AuditAction,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    entity_label: str | None = None,
    summary: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    tenant_id: uuid.UUID | None = None,
    succeeded: bool = True,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> AuditLog | None:
    """Append one audit entry. Returns ``None`` when there is no tenant context."""
    tenant_id = tenant_id or context.get_tenant_id()
    if tenant_id is None:
        logger.debug("audit_skipped_no_tenant", action=str(action), entity=entity_type)
        return None

    actor = context.get_actor()
    entry = AuditLog(
        tenant_id=tenant_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        actor_id=actor.id,
        actor_label=actor.display_name or actor.email or actor.kind,
        actor_role=actor.role,
        summary=summary,
        before=_sanitize(before),
        after=_sanitize(after),
        request_id=context.get_request_id(),
        ip_address=ip_address,
        user_agent=user_agent[:255] if user_agent else None,
        succeeded=succeeded,
    )
    session.add(entry)
    await session.flush()
    return entry


async def record_change(
    session: AsyncSession,
    *,
    entity_type: str,
    entity_id: uuid.UUID,
    before: dict[str, Any],
    after: dict[str, Any],
    entity_label: str | None = None,
    summary: str | None = None,
) -> AuditLog | None:
    """Record only the fields that actually differ."""
    changed_before, changed_after = diff(before, after)
    if not changed_after:
        return None
    return await record(
        session,
        action=AuditAction.UPDATE,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        summary=summary or f"Updated {', '.join(sorted(changed_after))}",
        before=changed_before,
        after=changed_after,
    )


def diff(
    before: dict[str, Any], after: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Field-level difference between two snapshots."""
    changed_before: dict[str, Any] = {}
    changed_after: dict[str, Any] = {}
    for key, new_value in after.items():
        old_value = before.get(key)
        if _normalise(old_value) != _normalise(new_value):
            changed_before[key] = old_value
            changed_after[key] = new_value
    return _sanitize(changed_before), _sanitize(changed_after)


def snapshot(entity: Any, fields: Sequence[str]) -> dict[str, Any]:
    """Capture named attributes of a model for before/after comparison."""
    return {field: getattr(entity, field, None) for field in fields}


def _sanitize(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        key: "***redacted***" if key in _REDACTED_FIELDS else _normalise(value)
        for key, value in payload.items()
    }


def _normalise(value: Any) -> Any:
    """Coerce to something JSON-serialisable and comparable."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _normalise(v) for k, v in value.items()}
    return str(value)


async def history(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    action: AuditAction | None = None,
    page: PageParams | None = None,
) -> tuple[Sequence[AuditLog], int]:
    """Query the audit trail for the admin console."""
    from sqlalchemy import func  # noqa: PLC0415

    statement = select(AuditLog).where(AuditLog.tenant_id == tenant_id)
    if entity_type:
        statement = statement.where(AuditLog.entity_type == entity_type)
    if entity_id:
        statement = statement.where(AuditLog.entity_id == entity_id)
    if actor_id:
        statement = statement.where(AuditLog.actor_id == actor_id)
    if action:
        statement = statement.where(AuditLog.action == action)

    total = int(
        await session.scalar(select(func.count()).select_from(statement.subquery())) or 0
    )
    statement = statement.order_by(AuditLog.created_at.desc())
    if page:
        statement = statement.offset(page.offset).limit(page.limit)
    rows = (await session.scalars(statement)).all()
    return rows, total
