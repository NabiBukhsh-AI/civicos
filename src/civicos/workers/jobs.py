"""Scheduled jobs.

Deliberately plain async functions rather than a framework: they can be run by
cron (via ``civicos ops ...``), by ARQ/Celery, by a Kubernetes CronJob or in a
test, with no adaptation. Each job is idempotent and scoped per tenant.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import structlog
from sqlalchemy import select

from civicos.core.clock import utcnow
from civicos.core.context import SYSTEM_ACTOR, use_context
from civicos.core.telemetry import BACKGROUND_JOBS, OPEN_ISSUES
from civicos.db.session import session_scope
from civicos.domain.enums import IssueStatus, SLAStage, TenantStatus
from civicos.domain.tenancy import Municipality

logger = structlog.get_logger(__name__)


async def _active_tenants(session: Any, slug: str | None = None) -> list[Municipality]:
    statement = select(Municipality).where(
        Municipality.deleted_at.is_(None), Municipality.status == TenantStatus.ACTIVE
    )
    if slug:
        statement = statement.where(Municipality.slug == slug)
    return list((await session.scalars(statement)).all())


async def run_sla_sweep(tenant_slug: str | None = None) -> dict[str, int]:
    """Re-evaluate SLA states across open issues and escalate new breaches.

    This is the job that turns a written service target into an operational
    one: without it, a missed deadline is only discovered when a resident
    complains again.
    """
    from civicos.domain.tenancy import Department
    from civicos.repositories.issues import IssueRepository
    from civicos.services import issue_service, sla_service

    checked = breached = escalated = 0

    async with session_scope() as session:
        for tenant in await _active_tenants(session, tenant_slug):
            with use_context(actor=SYSTEM_ACTOR, tenant_id=tenant.id, tenant_slug=tenant.slug):
                repository = IssueRepository(session, tenant.id)
                issues = await repository.due_for_sla_check()
                checked += len(issues)

                for issue in issues:
                    stages = await sla_service.refresh(session, issue)
                    if not stages:
                        continue
                    breached += len(stages)

                    department = (
                        await session.get(Department, issue.department_id)
                        if issue.department_id
                        else None
                    )
                    should_escalate = (
                        SLAStage.RESOLUTION in stages
                        and department is not None
                        and department.escalation_chain
                    )
                    if should_escalate:
                        await issue_service.escalate(
                            session,
                            tenant,
                            issue,
                            reason=(
                                f"Automatic escalation: the resolution deadline for "
                                f"{issue.reference} has passed."
                            ),
                        )
                        escalated += 1

                open_count = await repository.count(
                    repository.query().where(
                        repository.model.status.in_([s for s in IssueStatus if s.is_open])
                    )
                )
                OPEN_ISSUES.labels(tenant=tenant.slug).set(open_count)

    BACKGROUND_JOBS.labels(job="sla_sweep", outcome="ok").inc()
    logger.info("sla_sweep_complete", checked=checked, breached=breached, escalated=escalated)
    return {"checked": checked, "breached": breached, "escalated": escalated}


async def run_daily_rollup(days: int = 1) -> dict[str, int]:
    """Recompute daily analytics rollups. Run nightly."""
    from civicos.services import analytics_service

    tenant_count = 0
    async with session_scope() as session:
        tenants = await _active_tenants(session)
        tenant_count = len(tenants)
        for tenant in tenants:
            for offset in range(1, days + 1):
                target = (utcnow() - timedelta(days=offset)).date()
                await analytics_service.rollup_day(session, tenant.id, target)

    BACKGROUND_JOBS.labels(job="daily_rollup", outcome="ok").inc()
    logger.info("daily_rollup_complete", tenants=tenant_count, days=days)
    return {"tenants": tenant_count, "days": days}


async def run_notification_retry(limit: int = 200) -> dict[str, int]:
    """Re-attempt failed notification deliveries."""
    from civicos.services import notification_service

    async with session_scope() as session:
        delivered = await notification_service.retry_failed(session, limit=limit)

    BACKGROUND_JOBS.labels(job="notification_retry", outcome="ok").inc()
    return {"delivered": delivered}


async def run_reindex(tenant_slug: str) -> dict[str, int]:
    """Re-embed a municipality's whole corpus (e.g. after a model change)."""
    from civicos.ai.rag.ingest import ingest_document
    from civicos.domain.enums import DocumentStatus
    from civicos.domain.knowledge import Document
    from civicos.integrations.storage import get_storage
    from civicos.services.auth_service import get_tenant_by_slug

    reindexed = failed = 0
    storage = get_storage()

    async with session_scope() as session:
        tenant = await get_tenant_by_slug(session, tenant_slug)
        documents = (
            await session.scalars(
                select(Document).where(
                    Document.tenant_id == tenant.id,
                    Document.deleted_at.is_(None),
                    Document.storage_key.is_not(None),
                    Document.status.in_([DocumentStatus.INDEXED, DocumentStatus.FAILED]),
                )
            )
        ).all()

        for document in documents:
            try:
                data = await storage.load(document.storage_key)  # type: ignore[arg-type]
                result = await ingest_document(session, document, data)
                if result.succeeded:
                    reindexed += 1
                else:
                    failed += 1
            except Exception as exc:
                failed += 1
                logger.warning("reindex_failed", document_id=str(document.id), error=str(exc))

    BACKGROUND_JOBS.labels(job="reindex", outcome="ok").inc()
    return {"reindexed": reindexed, "failed": failed}


async def run_asset_inspection_sweep() -> dict[str, int]:
    """Raise preventive work orders for assets whose inspection is due.

    Preventive maintenance is the cheapest maintenance there is; this is what
    turns an asset register from a spreadsheet into an operational schedule.
    """
    from civicos.domain.assets import Asset
    from civicos.domain.enums import Priority, WorkOrderType
    from civicos.domain.workorders import WorkOrder, WorkOrderStatus
    from civicos.services import workorder_service

    raised = 0
    async with session_scope() as session:
        for tenant in await _active_tenants(session):
            with use_context(actor=SYSTEM_ACTOR, tenant_id=tenant.id, tenant_slug=tenant.slug):
                due = (
                    await session.scalars(
                        select(Asset).where(
                            Asset.tenant_id == tenant.id,
                            Asset.deleted_at.is_(None),
                            Asset.next_inspection_due.is_not(None),
                            Asset.next_inspection_due <= utcnow(),
                        )
                    )
                ).all()

                for asset in due:
                    # Do not stack duplicate inspection orders on one asset.
                    existing = await session.scalar(
                        select(WorkOrder).where(
                            WorkOrder.tenant_id == tenant.id,
                            WorkOrder.asset_id == asset.id,
                            WorkOrder.order_type == WorkOrderType.INSPECTION,
                            WorkOrder.status.not_in(
                                [
                                    WorkOrderStatus.COMPLETED,
                                    WorkOrderStatus.VERIFIED,
                                    WorkOrderStatus.CANCELLED,
                                ]
                            ),
                        )
                    )
                    if existing is not None:
                        continue

                    await workorder_service.create_work_order(
                        session,
                        tenant,
                        workorder_service.WorkOrderDraft(
                            title=f"Scheduled inspection: {asset.name}",
                            instructions=(
                                f"Routine inspection of {asset.code}. Record the condition "
                                "and photograph any defects."
                            ),
                            order_type=WorkOrderType.INSPECTION,
                            priority=Priority.LOW,
                            asset_id=asset.id,
                            department_id=asset.department_id,
                            admin_unit_id=asset.admin_unit_id,
                            latitude=asset.latitude,
                            longitude=asset.longitude,
                            address=asset.address,
                            due_at=utcnow() + timedelta(days=14),
                        ),
                    )
                    raised += 1

    BACKGROUND_JOBS.labels(job="asset_inspection_sweep", outcome="ok").inc()
    logger.info("asset_inspection_sweep_complete", raised=raised)
    return {"raised": raised}


async def run_schedule_reliability() -> dict[str, int]:
    """Recompute how reliably recurring service rounds actually happen.

    Publishing a schedule is only meaningful if missed rounds are visible, so
    the reliability score is derived from completion records, not asserted.
    """
    from civicos.domain.services import ServiceSchedule

    updated = 0
    async with session_scope() as session:
        for tenant in await _active_tenants(session):
            schedules = (
                await session.scalars(
                    select(ServiceSchedule).where(
                        ServiceSchedule.tenant_id == tenant.id,
                        ServiceSchedule.is_active.is_(True),
                    )
                )
            ).all()
            for schedule in schedules:
                expected = _expected_interval_hours(schedule.frequency)
                if schedule.last_completed_at is None:
                    schedule.reliability_score = 0.0
                else:
                    from civicos.core.clock import ensure_utc

                    elapsed = (
                        utcnow() - ensure_utc(schedule.last_completed_at)
                    ).total_seconds() / 3600
                    schedule.reliability_score = round(
                        max(0.0, min(1.0, expected / max(elapsed, expected))), 3
                    )
                    schedule.next_due_at = ensure_utc(schedule.last_completed_at) + timedelta(
                        hours=expected
                    )
                updated += 1

    BACKGROUND_JOBS.labels(job="schedule_reliability", outcome="ok").inc()
    return {"updated": updated}


def _expected_interval_hours(frequency: Any) -> float:
    return {
        "daily": 24.0,
        "alternate_days": 48.0,
        "twice_weekly": 84.0,
        "weekly": 168.0,
        "fortnightly": 336.0,
        "monthly": 720.0,
        "on_demand": 720.0,
    }.get(str(frequency), 168.0)


async def run_all_scheduled() -> dict[str, Any]:
    """Convenience entry point for a single scheduler tick."""
    return {
        "sla": await run_sla_sweep(),
        "notifications": await run_notification_retry(),
        "inspections": await run_asset_inspection_sweep(),
        "schedules": await run_schedule_reliability(),
    }
