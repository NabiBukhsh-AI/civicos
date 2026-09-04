"""Service-level agreements: deadlines, risk states, breaches and escalation.

An SLA is the promise a municipality makes when it accepts a complaint, so the
engine is deliberately explicit rather than clever:

* policies resolve most-specific-first (category+priority > category > priority
  > tenant default);
* deadlines are computed once at triage and stored on the issue, so a later
  policy edit cannot silently re-date historic commitments;
* non-emergency clocks tick in working hours only, emergency clocks run 24/7.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.clock import add_business_minutes, ensure_utc, utcnow
from civicos.core.telemetry import SLA_BREACHES
from civicos.domain.enums import IssueEventType, Priority, SLAStage, SLAState
from civicos.domain.issues import Issue, IssueEvent
from civicos.domain.tenancy import Department, Municipality, SLAPolicy

logger = structlog.get_logger(__name__)

#: Used when a tenant has configured no policy at all.
DEFAULT_RESPONSE_MINUTES = 240
DEFAULT_RESOLUTION_MINUTES = 4320  # 3 days
DEFAULT_WARNING_THRESHOLD = 0.75


@dataclass(slots=True)
class SLATargets:
    policy_id: uuid.UUID | None
    response_due_at: datetime
    resolution_due_at: datetime
    warning_threshold: float
    business_hours_only: bool


async def resolve_policy(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    category_id: uuid.UUID | None,
    priority: Priority,
) -> SLAPolicy | None:
    """Pick the most specific active policy that matches."""
    policies = (
        await session.scalars(
            select(SLAPolicy).where(
                SLAPolicy.tenant_id == tenant_id,
                SLAPolicy.is_active.is_(True),
            )
        )
    ).all()

    candidates = [
        policy
        for policy in policies
        if (policy.category_id is None or policy.category_id == category_id)
        and (policy.priority is None or policy.priority == priority)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda policy: policy.specificity)


async def compute_targets(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    category_id: uuid.UUID | None,
    priority: Priority,
    department: Department | None = None,
    municipality: Municipality | None = None,
    start: datetime | None = None,
) -> SLATargets:
    """Compute the response and resolution deadlines for an issue."""
    start = start or utcnow()
    policy = await resolve_policy(session, tenant_id, category_id=category_id, priority=priority)

    response_minutes = policy.response_minutes if policy else DEFAULT_RESPONSE_MINUTES
    resolution_minutes = policy.resolution_minutes if policy else DEFAULT_RESOLUTION_MINUTES
    warning = policy.warning_threshold if policy else DEFAULT_WARNING_THRESHOLD
    business_hours = policy.business_hours_only if policy else True

    # An emergency clock never pauses for the weekend.
    if priority is Priority.EMERGENCY:
        business_hours = False

    timezone = municipality.timezone if municipality else None
    if business_hours:
        workday_start, workday_end, weekend_days = _working_hours(department)
        response_due = add_business_minutes(
            start,
            response_minutes,
            timezone=timezone,
            workday_start=workday_start,
            workday_end=workday_end,
            weekend_days=weekend_days,
        )
        resolution_due = add_business_minutes(
            start,
            resolution_minutes,
            timezone=timezone,
            workday_start=workday_start,
            workday_end=workday_end,
            weekend_days=weekend_days,
        )
    else:
        response_due = start + timedelta(minutes=response_minutes)
        resolution_due = start + timedelta(minutes=resolution_minutes)

    return SLATargets(
        policy_id=policy.id if policy else None,
        response_due_at=response_due,
        resolution_due_at=resolution_due,
        warning_threshold=warning,
        business_hours_only=business_hours,
    )


def apply_targets(issue: Issue, targets: SLATargets) -> None:
    issue.sla_policy_id = targets.policy_id
    issue.response_due_at = targets.response_due_at
    issue.resolution_due_at = targets.resolution_due_at
    issue.sla_response_state = SLAState.ON_TRACK
    issue.sla_resolution_state = SLAState.ON_TRACK


def evaluate(issue: Issue, *, now: datetime | None = None) -> dict[str, SLAState]:
    """Recompute both SLA states for one issue without writing anything."""
    now = now or utcnow()
    return {
        "response": _stage_state(
            due_at=issue.response_due_at,
            met_at=issue.first_response_at,
            created_at=issue.created_at,
            current=issue.sla_response_state,
            now=now,
        ),
        "resolution": _stage_state(
            due_at=issue.resolution_due_at,
            met_at=issue.resolved_at,
            created_at=issue.created_at,
            current=issue.sla_resolution_state,
            now=now,
        ),
    }


def _stage_state(
    *,
    due_at: datetime | None,
    met_at: datetime | None,
    created_at: datetime,
    current: SLAState,
    now: datetime,
) -> SLAState:
    if due_at is None:
        return SLAState.NOT_APPLICABLE
    due_at = ensure_utc(due_at)

    if met_at is not None:
        # Once a stage is done its outcome is fixed - met or missed, forever.
        if current is SLAState.BREACHED:
            return SLAState.BREACHED
        return SLAState.MET if ensure_utc(met_at) <= due_at else SLAState.BREACHED
    if now > due_at:
        return SLAState.BREACHED

    total = (due_at - ensure_utc(created_at)).total_seconds()
    elapsed = (now - ensure_utc(created_at)).total_seconds()
    if total > 0 and elapsed / total >= DEFAULT_WARNING_THRESHOLD:
        return SLAState.AT_RISK
    return SLAState.ON_TRACK


async def refresh(
    session: AsyncSession, issue: Issue, *, now: datetime | None = None
) -> list[SLAStage]:
    """Update stored SLA states, recording an event for each new breach."""
    now = now or utcnow()
    states = evaluate(issue, now=now)
    newly_breached: list[SLAStage] = []

    if issue.sla_response_state is not states["response"]:
        if states["response"] is SLAState.BREACHED:
            newly_breached.append(SLAStage.RESPONSE)
        issue.sla_response_state = states["response"]

    if issue.sla_resolution_state is not states["resolution"]:
        if states["resolution"] is SLAState.BREACHED:
            newly_breached.append(SLAStage.RESOLUTION)
        issue.sla_resolution_state = states["resolution"]

    for stage in newly_breached:
        # Read the label only if the relationship is already loaded: this runs
        # during intake too, where `issue` was just constructed in memory and a
        # lazy load would raise on an async session.
        category = _category_label(issue)
        SLA_BREACHES.labels(tenant=str(issue.tenant_id), stage=str(stage), category=category).inc()
        session.add(
            IssueEvent(
                tenant_id=issue.tenant_id,
                issue_id=issue.id,
                event_type=IssueEventType.SLA_BREACHED,
                actor_label="system",
                to_value=str(stage),
                note=f"{stage.value.title()} deadline missed.",
                is_public=False,
                payload={
                    "stage": str(stage),
                    "due_at": (
                        issue.response_due_at.isoformat()
                        if stage is SLAStage.RESPONSE and issue.response_due_at
                        else issue.resolution_due_at.isoformat()
                        if issue.resolution_due_at
                        else None
                    ),
                },
            )
        )
        logger.info(
            "sla_breached",
            issue=issue.reference,
            stage=str(stage),
            department=str(issue.department_id),
        )

    if newly_breached:
        await session.flush()
    return newly_breached


def escalation_targets(department: Department | None, level: int) -> list[uuid.UUID]:
    """Who to notify at a given escalation level.

    The chain is department-configured; level 1 is the first name on it,
    level 2 the second, and so on. Running off the end simply means everyone
    already listed has been told.
    """
    if department is None or not department.escalation_chain:
        return []
    chain = [uuid.UUID(str(item)) for item in department.escalation_chain if item]
    index = max(level - 1, 0)
    return chain[index : index + 1] if index < len(chain) else []


def time_remaining(issue: Issue, stage: SLAStage) -> timedelta | None:
    due = issue.response_due_at if stage is SLAStage.RESPONSE else issue.resolution_due_at
    if due is None:
        return None
    return ensure_utc(due) - utcnow()


def compliance_rate(issues: Sequence[Issue], stage: SLAStage) -> float:
    """Share of issues that met the given stage, ignoring ones still running."""
    attribute = "sla_response_state" if stage is SLAStage.RESPONSE else "sla_resolution_state"
    decided = [
        getattr(issue, attribute)
        for issue in issues
        if getattr(issue, attribute) in {SLAState.MET, SLAState.BREACHED}
    ]
    if not decided:
        return 1.0
    met = sum(1 for state in decided if state is SLAState.MET)
    return round(met / len(decided), 4)


def _category_label(issue: Issue) -> str:
    """Category slug for metrics, without triggering a lazy load."""
    from sqlalchemy import inspect as sa_inspect

    if "category" in sa_inspect(issue).unloaded:
        return "uncategorised"
    return issue.category.slug if issue.category else "uncategorised"


def _working_hours(
    department: Department | None,
) -> tuple[time, time, frozenset[int]]:
    """Read a department's working hours, falling back to a 9-5 week."""
    default_start, default_end = time(9, 0), time(17, 0)
    default_weekend = frozenset({6})
    if department is None or not department.working_hours:
        return default_start, default_end, default_weekend

    config = department.working_hours
    try:
        start_parts = str(config.get("start", "09:00")).split(":")
        end_parts = str(config.get("end", "17:00")).split(":")
        start = time(int(start_parts[0]), int(start_parts[1]))
        end = time(int(end_parts[0]), int(end_parts[1]))
    except (ValueError, IndexError):
        start, end = default_start, default_end

    weekend = config.get("weekend_days")
    weekend_days = (
        frozenset(int(day) for day in weekend)
        if isinstance(weekend, list) and weekend
        else default_weekend
    )
    return start, end, weekend_days
