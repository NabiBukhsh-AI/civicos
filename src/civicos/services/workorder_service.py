"""Work orders: dispatch, field updates, completion and verification.

Work orders are the bridge between a complaint and a crew with a shovel. They
are separate from issues because the mapping is not one-to-one: one complaint
can need several jobs, one job can close several complaints, and preventive
maintenance produces jobs with no complaint behind them.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.clock import utcnow
from civicos.core.errors import ValidationError, WorkflowError
from civicos.core.security import generate_reference
from civicos.domain.assets import Asset
from civicos.domain.enums import (
    WORK_ORDER_TRANSITIONS,
    AssetCondition,
    AuditAction,
    IssueEventType,
    IssueStatus,
    Priority,
    WorkOrderStatus,
    WorkOrderType,
)
from civicos.domain.identity import User
from civicos.domain.issues import Issue, IssueEvent
from civicos.domain.tenancy import Municipality
from civicos.domain.workorders import Crew, MaterialUsage, WorkOrder, WorkOrderUpdate
from civicos.services import audit_service, routing_service
from civicos.services.notification_service import Recipient, notify

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class WorkOrderDraft:
    title: str
    instructions: str | None = None
    order_type: WorkOrderType = WorkOrderType.CORRECTIVE
    priority: Priority = Priority.NORMAL
    issue_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    assigned_to_id: uuid.UUID | None = None
    scheduled_for: datetime | None = None
    due_at: datetime | None = None
    estimated_hours: float | None = None
    estimated_cost: float | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    checklist: list[dict[str, Any]] = field(default_factory=list)
    requires_verification: bool = True


async def create_work_order(
    session: AsyncSession,
    tenant: Municipality,
    draft: WorkOrderDraft,
    *,
    auto_dispatch: bool = False,
    actor: User | None = None,
) -> WorkOrder:
    """Raise a work order, inheriting location and priority from its issue."""
    issue: Issue | None = None
    if draft.issue_id:
        issue = await session.get(Issue, draft.issue_id)
        if issue is None or issue.tenant_id != tenant.id:
            raise ValidationError("That report does not exist.", code="issue_not_found")

    order = WorkOrder(
        tenant_id=tenant.id,
        reference=generate_reference(f"{(tenant.slug[:2] or 'wo').upper()}W"),
        title=draft.title,
        instructions=draft.instructions,
        order_type=draft.order_type,
        priority=draft.priority
        if issue is None
        else max(draft.priority, issue.priority, key=lambda p: p.weight),
        issue_id=draft.issue_id,
        asset_id=draft.asset_id,
        department_id=draft.department_id or (issue.department_id if issue else None),
        admin_unit_id=draft.admin_unit_id or (issue.admin_unit_id if issue else None),
        crew_id=draft.crew_id,
        assigned_to_id=draft.assigned_to_id,
        latitude=draft.latitude
        if draft.latitude is not None
        else (issue.latitude if issue else None),
        longitude=draft.longitude
        if draft.longitude is not None
        else (issue.longitude if issue else None),
        address=draft.address or (issue.address if issue else None),
        scheduled_for=draft.scheduled_for,
        due_at=draft.due_at or (issue.resolution_due_at if issue else None),
        estimated_hours=draft.estimated_hours,
        estimated_cost=draft.estimated_cost,
        checklist=draft.checklist,
        requires_verification=draft.requires_verification,
    )
    session.add(order)
    await session.flush()

    if order.crew_id is None and order.department_id:
        crew = await routing_service.pick_crew(
            session,
            tenant.id,
            department_id=order.department_id,
            category_slug=issue.category.slug if issue and issue.category else None,
            admin_unit_id=order.admin_unit_id,
            priority=order.priority,
        )
        if crew is not None:
            order.crew_id = crew.id

    if auto_dispatch and order.crew_id:
        await dispatch(session, tenant, order, actor=actor)
    elif order.scheduled_for:
        order.status = WorkOrderStatus.SCHEDULED

    if issue is not None:
        session.add(
            IssueEvent(
                tenant_id=tenant.id,
                issue_id=issue.id,
                event_type=IssueEventType.COMMENTED,
                actor_label=actor.display() if actor else "system",
                note=f"Work order {order.reference} raised.",
                is_public=True,
                payload={"work_order_id": str(order.id)},
            )
        )

    await audit_service.record(
        session,
        action=AuditAction.CREATE,
        entity_type="work_order",
        entity_id=order.id,
        entity_label=order.reference,
        summary=f"Work order {order.reference} created",
        tenant_id=tenant.id,
    )
    await session.flush()
    return order


async def transition(
    session: AsyncSession,
    tenant: Municipality,
    order: WorkOrder,
    new_status: WorkOrderStatus,
    *,
    note: str | None = None,
    actor: User | None = None,
) -> WorkOrder:
    """Move a work order through its state machine."""
    current = order.status
    if new_status is current:
        return order

    allowed = WORK_ORDER_TRANSITIONS.get(current, frozenset())
    if new_status not in allowed:
        raise WorkflowError(
            f"Cannot move a work order from '{current.value}' to '{new_status.value}'.",
            details={
                "from": current.value,
                "to": new_status.value,
                "allowed": sorted(status.value for status in allowed),
            },
        )
    if new_status is WorkOrderStatus.BLOCKED and not note:
        raise ValidationError("Explain what is blocking this job.", code="blocked_reason_required")

    now = utcnow()
    order.status = new_status
    if new_status is WorkOrderStatus.DISPATCHED:
        order.dispatched_at = now
    elif new_status is WorkOrderStatus.IN_PROGRESS:
        order.started_at = order.started_at or now
    elif new_status is WorkOrderStatus.COMPLETED:
        order.completed_at = now
        order.completion_note = note or order.completion_note
        if order.started_at:
            order.actual_hours = round((now - order.started_at).total_seconds() / 3600, 2)
    elif new_status is WorkOrderStatus.VERIFIED:
        order.verified_at = now
    elif new_status is WorkOrderStatus.BLOCKED:
        order.blocked_reason = note

    session.add(
        WorkOrderUpdate(
            tenant_id=tenant.id,
            work_order_id=order.id,
            author_id=actor.id if actor else None,
            status=new_status,
            note=note,
            recorded_at=now,
        )
    )

    if new_status in {WorkOrderStatus.COMPLETED, WorkOrderStatus.VERIFIED}:
        await _propagate_to_issue(session, tenant, order, actor=actor)

    await audit_service.record(
        session,
        action=AuditAction.TRANSITION,
        entity_type="work_order",
        entity_id=order.id,
        entity_label=order.reference,
        summary=f"{order.reference}: {current.value} -> {new_status.value}",
        before={"status": current.value},
        after={"status": new_status.value},
        tenant_id=tenant.id,
    )
    await session.flush()
    return order


async def dispatch(
    session: AsyncSession,
    tenant: Municipality,
    order: WorkOrder,
    *,
    crew_id: uuid.UUID | None = None,
    assignee_id: uuid.UUID | None = None,
    actor: User | None = None,
) -> WorkOrder:
    """Send a job to a crew and tell them about it."""
    if crew_id:
        order.crew_id = crew_id
    if assignee_id:
        order.assigned_to_id = assignee_id
    if not order.crew_id and not order.assigned_to_id:
        raise ValidationError(
            "A work order needs a crew or an assignee before dispatch.",
            code="no_dispatch_target",
        )

    await transition(session, tenant, order, WorkOrderStatus.DISPATCHED, actor=actor)
    await _notify_crew(session, tenant, order)
    return order


async def record_update(
    session: AsyncSession,
    tenant: Municipality,
    order: WorkOrder,
    *,
    note: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    recorded_at: datetime | None = None,
    checklist: list[dict[str, Any]] | None = None,
    actor: User | None = None,
) -> WorkOrderUpdate:
    """Log a field update.

    ``recorded_at`` is when the phone captured it; crews frequently work with no
    signal and sync later, so trusting the server clock would misrepresent when
    the work actually happened.
    """
    if checklist is not None:
        order.checklist = checklist
    if order.status is WorkOrderStatus.DISPATCHED:
        await transition(session, tenant, order, WorkOrderStatus.IN_PROGRESS, actor=actor)

    update = WorkOrderUpdate(
        tenant_id=tenant.id,
        work_order_id=order.id,
        author_id=actor.id if actor else None,
        note=note,
        latitude=latitude,
        longitude=longitude,
        recorded_at=recorded_at or utcnow(),
    )
    session.add(update)
    await session.flush()
    return update


async def complete(
    session: AsyncSession,
    tenant: Municipality,
    order: WorkOrder,
    *,
    note: str,
    actual_hours: float | None = None,
    actual_cost: float | None = None,
    materials: list[dict[str, Any]] | None = None,
    signoff_name: str | None = None,
    asset_condition: AssetCondition | None = None,
    actor: User | None = None,
) -> WorkOrder:
    """Close out a job, recording what it consumed and what it left behind."""
    incomplete = [
        item.get("label")
        for item in (order.checklist or [])
        if isinstance(item, dict) and item.get("required", True) and not item.get("done")
    ]
    if incomplete:
        raise ValidationError(
            "Some required checklist items are not complete.",
            code="checklist_incomplete",
            details={"outstanding": incomplete},
        )

    if actual_hours is not None:
        order.actual_hours = actual_hours
    if actual_cost is not None:
        order.actual_cost = actual_cost
    if signoff_name:
        order.signoff = {"name": signoff_name, "at": utcnow().isoformat()}

    for material in materials or []:
        session.add(
            MaterialUsage(
                tenant_id=tenant.id,
                work_order_id=order.id,
                item_name=str(material.get("item_name", "item")),
                item_code=material.get("item_code"),
                quantity=float(material.get("quantity", 1)),
                unit=str(material.get("unit", "unit")),
                unit_cost=material.get("unit_cost"),
            )
        )

    await transition(session, tenant, order, WorkOrderStatus.COMPLETED, note=note, actor=actor)

    if order.asset_id:
        await _update_asset(session, order, asset_condition)
    return order


async def _update_asset(
    session: AsyncSession, order: WorkOrder, condition: AssetCondition | None
) -> None:
    asset = await session.get(Asset, order.asset_id)
    if asset is None:
        return
    asset.last_serviced_at = utcnow()
    if condition is not None:
        asset.condition = condition
        asset.is_operational = condition not in {
            AssetCondition.CRITICAL,
            AssetCondition.DECOMMISSIONED,
        }
    if order.actual_cost:
        asset.lifetime_maintenance_cost = round(
            asset.lifetime_maintenance_cost + order.actual_cost, 2
        )
    if asset.inspection_interval_days:
        asset.next_inspection_due = utcnow() + timedelta(days=asset.inspection_interval_days)


async def _propagate_to_issue(
    session: AsyncSession,
    tenant: Municipality,
    order: WorkOrder,
    *,
    actor: User | None,
) -> None:
    """Move the originating issue on when its last open job finishes."""
    if not order.issue_id:
        return
    issue = await session.get(Issue, order.issue_id)
    if issue is None or issue.status in {IssueStatus.RESOLVED, IssueStatus.CLOSED}:
        return

    siblings = (
        await session.scalars(
            select(WorkOrder).where(
                WorkOrder.issue_id == issue.id,
                WorkOrder.deleted_at.is_(None),
                WorkOrder.id != order.id,
            )
        )
    ).all()
    if any(sibling.is_open for sibling in siblings):
        return  # other jobs still running; the issue is not finished

    from civicos.services import issue_service

    if issue.status in {IssueStatus.ASSIGNED, IssueStatus.IN_PROGRESS, IssueStatus.ON_HOLD}:
        await issue_service.transition(
            session,
            tenant,
            issue,
            IssueStatus.RESOLVED,
            note=f"Work order {order.reference} completed.",
            resolution_note=order.completion_note,
            actor=actor,
        )


async def _notify_crew(session: AsyncSession, tenant: Municipality, order: WorkOrder) -> None:
    recipients: list[Recipient] = []
    if order.assigned_to_id:
        user = await session.get(User, order.assigned_to_id)
        if user is not None:
            recipients.append(Recipient.for_user(user))
    if order.crew_id:
        crew = await session.get(Crew, order.crew_id)
        if crew is not None:
            for member in crew.members:
                if member.user is not None and member.user.id != order.assigned_to_id:
                    recipients.append(Recipient.for_user(member.user))
    if recipients:
        await notify(
            session,
            tenant.id,
            recipients,
            template_key="workorder.assigned",
            params={"reference": order.reference},
            entity_type="work_order",
            entity_id=order.id,
        )


async def overdue_orders(
    session: AsyncSession, tenant_id: uuid.UUID, limit: int = 200
) -> Sequence[WorkOrder]:
    now = utcnow()
    return (
        await session.scalars(
            select(WorkOrder)
            .where(
                WorkOrder.tenant_id == tenant_id,
                WorkOrder.deleted_at.is_(None),
                WorkOrder.due_at.is_not(None),
                WorkOrder.due_at < now,
                WorkOrder.status.not_in(
                    [
                        WorkOrderStatus.COMPLETED,
                        WorkOrderStatus.VERIFIED,
                        WorkOrderStatus.CANCELLED,
                    ]
                ),
            )
            .order_by(WorkOrder.due_at.asc())
            .limit(limit)
        )
    ).all()


async def crew_workload(
    session: AsyncSession, tenant_id: uuid.UUID, crew_id: uuid.UUID
) -> dict[str, Any]:
    """Snapshot used by the dispatch board."""
    orders = (
        await session.scalars(
            select(WorkOrder).where(
                WorkOrder.tenant_id == tenant_id,
                WorkOrder.crew_id == crew_id,
                WorkOrder.deleted_at.is_(None),
            )
        )
    ).all()
    open_orders = [order for order in orders if order.is_open]
    return {
        "crew_id": str(crew_id),
        "open": len(open_orders),
        "overdue": sum(1 for order in open_orders if order.is_overdue),
        "completed_total": sum(
            1
            for order in orders
            if order.status in {WorkOrderStatus.COMPLETED, WorkOrderStatus.VERIFIED}
        ),
        "estimated_hours_outstanding": round(
            sum(order.estimated_hours or 0.0 for order in open_orders), 1
        ),
    }
