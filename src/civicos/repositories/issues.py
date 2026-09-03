"""Issue queries: filtering, geospatial lookup and analytics aggregates."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Sequence

from sqlalchemy import Select, String, and_, cast, func, or_, select
from sqlalchemy.orm import selectinload

from civicos.core.clock import utcnow
from civicos.core.geo import Point, bounding_box, haversine_meters
from civicos.core.pagination import PageParams
from civicos.domain.enums import IssueStatus, Priority, ReportChannel, SLAState, Visibility
from civicos.domain.issues import Issue, IssueComment, IssueEvent
from civicos.repositories.base import TenantRepository, apply_sort

SORTABLE_FIELDS = {
    "created_at",
    "updated_at",
    "priority",
    "status",
    "resolution_due_at",
    "confirmations",
    "satisfaction_rating",
}


@dataclass(slots=True)
class IssueFilters:
    """Every filter the issue list endpoint understands."""

    status: list[IssueStatus] = field(default_factory=list)
    exclude_status: list[IssueStatus] = field(default_factory=list)
    priority: list[Priority] = field(default_factory=list)
    channel: list[ReportChannel] = field(default_factory=list)
    category_ids: list[uuid.UUID] = field(default_factory=list)
    department_ids: list[uuid.UUID] = field(default_factory=list)
    admin_unit_ids: list[uuid.UUID] = field(default_factory=list)
    assigned_to_id: uuid.UUID | None = None
    reporter_id: uuid.UUID | None = None
    unassigned: bool = False
    open_only: bool = False
    breached_only: bool = False
    at_risk_only: bool = False
    flagged_only: bool = False
    has_location: bool | None = None
    search: str | None = None
    reference: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    visibility: list[Visibility] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    near: Point | None = None
    radius_meters: int = 1000


class IssueRepository(TenantRepository[Issue]):
    model = Issue

    def build_query(self, filters: IssueFilters) -> Select[Any]:
        statement = self.query()

        if filters.status:
            statement = statement.where(Issue.status.in_(filters.status))
        if filters.exclude_status:
            statement = statement.where(Issue.status.not_in(filters.exclude_status))
        if filters.open_only:
            statement = statement.where(Issue.status.in_(_open_statuses()))
        if filters.priority:
            statement = statement.where(Issue.priority.in_(filters.priority))
        if filters.channel:
            statement = statement.where(Issue.channel.in_(filters.channel))
        if filters.category_ids:
            statement = statement.where(Issue.category_id.in_(filters.category_ids))
        if filters.department_ids:
            statement = statement.where(Issue.department_id.in_(filters.department_ids))
        if filters.admin_unit_ids:
            statement = statement.where(Issue.admin_unit_id.in_(filters.admin_unit_ids))
        if filters.assigned_to_id:
            statement = statement.where(Issue.assigned_to_id == filters.assigned_to_id)
        if filters.unassigned:
            statement = statement.where(Issue.assigned_to_id.is_(None))
        if filters.reporter_id:
            statement = statement.where(Issue.reporter_id == filters.reporter_id)
        if filters.breached_only:
            statement = statement.where(
                or_(
                    Issue.sla_response_state == SLAState.BREACHED,
                    Issue.sla_resolution_state == SLAState.BREACHED,
                )
            )
        if filters.at_risk_only:
            statement = statement.where(
                Issue.sla_resolution_state == SLAState.AT_RISK
            )
        if filters.flagged_only:
            statement = statement.where(Issue.is_flagged.is_(True))
        if filters.has_location is True:
            statement = statement.where(Issue.latitude.is_not(None))
        elif filters.has_location is False:
            statement = statement.where(Issue.latitude.is_(None))
        if filters.visibility:
            statement = statement.where(Issue.visibility.in_(filters.visibility))
        if filters.reference:
            statement = statement.where(Issue.reference == filters.reference.upper())
        if filters.created_after:
            statement = statement.where(Issue.created_at >= filters.created_after)
        if filters.created_before:
            statement = statement.where(Issue.created_at <= filters.created_before)
        if filters.search:
            term = f"%{filters.search.lower()}%"
            statement = statement.where(
                or_(
                    func.lower(Issue.title).like(term),
                    func.lower(Issue.description).like(term),
                    func.lower(Issue.reference).like(term),
                    func.lower(func.coalesce(Issue.address, "")).like(term),
                )
            )
        for tag in filters.tags:
            # Portable containment test: the JSON array is compared as text, which
            # works identically on SQLite JSON and PostgreSQL JSONB.
            statement = statement.where(
                func.lower(cast(Issue.tags, String)).like(f"%{tag.lower()}%")
            )
        if filters.near is not None:
            box = bounding_box(filters.near, filters.radius_meters)
            statement = statement.where(
                and_(
                    Issue.latitude.between(box.min_latitude, box.max_latitude),
                    Issue.longitude.between(box.min_longitude, box.max_longitude),
                )
            )
        return statement

    async def search(
        self,
        filters: IssueFilters,
        page: PageParams,
        *,
        sort_by: str | None = None,
        descending: bool = True,
    ) -> tuple[Sequence[Issue], int]:
        statement = self.build_query(filters)
        total = await self.count(statement)
        statement = apply_sort(
            statement, Issue, sort_by, descending, allowed=SORTABLE_FIELDS
        )
        items = await self.list(statement, page=page)

        # The bounding box is a coarse pre-filter; refine to a true radius.
        if filters.near is not None:
            centre = filters.near
            items = [
                issue
                for issue in items
                if issue.has_location
                and haversine_meters(
                    centre, Point(issue.latitude, issue.longitude)  # type: ignore[arg-type]
                )
                <= filters.radius_meters
            ]
        return items, total

    async def get_with_timeline(self, issue_id: uuid.UUID) -> Issue | None:
        return await self.session.scalar(
            self.query()
            .where(Issue.id == issue_id)
            .options(
                selectinload(Issue.attachments),
                selectinload(Issue.events),
                selectinload(Issue.comments).selectinload(IssueComment.author),
                selectinload(Issue.followers),
            )
        )

    async def get_by_reference(self, reference: str) -> Issue | None:
        return await self.session.scalar(
            self.query().where(Issue.reference == reference.upper())
        )

    async def find_nearby(
        self,
        point: Point,
        radius_meters: int,
        *,
        within_hours: int | None = None,
        category_id: uuid.UUID | None = None,
        exclude_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[tuple[Issue, float]]:
        """Open issues near a point, with exact distances. Drives deduplication."""
        box = bounding_box(point, radius_meters)
        statement = (
            self.query()
            .where(
                Issue.latitude.between(box.min_latitude, box.max_latitude),
                Issue.longitude.between(box.min_longitude, box.max_longitude),
                Issue.status.in_(_open_statuses() + [IssueStatus.RESOLVED]),
            )
            .limit(limit * 4)
        )
        if within_hours:
            statement = statement.where(
                Issue.created_at >= utcnow() - timedelta(hours=within_hours)
            )
        if category_id:
            statement = statement.where(Issue.category_id == category_id)
        if exclude_id:
            statement = statement.where(Issue.id != exclude_id)

        rows = (await self.session.scalars(statement)).unique().all()
        scored = [
            (issue, haversine_meters(point, Point(issue.latitude, issue.longitude)))  # type: ignore[arg-type]
            for issue in rows
            if issue.has_location
        ]
        scored = [pair for pair in scored if pair[1] <= radius_meters]
        scored.sort(key=lambda pair: pair[1])
        return scored[:limit]

    async def find_by_fingerprint(
        self, fingerprint: str, *, within_hours: int = 24
    ) -> Issue | None:
        """Catch a byte-identical resubmission (double-tap on a flaky network)."""
        return await self.session.scalar(
            self.query()
            .where(
                Issue.fingerprint == fingerprint,
                Issue.created_at >= utcnow() - timedelta(hours=within_hours),
            )
            .order_by(Issue.created_at.desc())
            .limit(1)
        )

    async def due_for_sla_check(self, limit: int = 500) -> Sequence[Issue]:
        """Open issues whose response or resolution deadline has passed."""
        now = utcnow()
        return (
            await self.session.scalars(
                self.query()
                .where(
                    Issue.status.in_(_open_statuses()),
                    or_(
                        and_(
                            Issue.response_due_at.is_not(None),
                            Issue.response_due_at < now,
                            Issue.sla_response_state.in_(
                                [SLAState.ON_TRACK, SLAState.AT_RISK]
                            ),
                        ),
                        and_(
                            Issue.resolution_due_at.is_not(None),
                            Issue.resolution_due_at < now,
                            Issue.sla_resolution_state.in_(
                                [SLAState.ON_TRACK, SLAState.AT_RISK]
                            ),
                        ),
                    ),
                )
                .limit(limit)
            )
        ).all()

    async def status_counts(self) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(Issue.status, func.count())
                .where(Issue.tenant_id == self.tenant_id, Issue.deleted_at.is_(None))
                .group_by(Issue.status)
            )
        ).all()
        return {str(status): int(count) for status, count in rows}

    async def counts_by(
        self,
        column: Any,
        *,
        since: datetime | None = None,
        open_only: bool = False,
        limit: int = 12,
    ) -> list[tuple[Any, int]]:
        statement = (
            select(column, func.count())
            .where(Issue.tenant_id == self.tenant_id, Issue.deleted_at.is_(None))
            .group_by(column)
            .order_by(func.count().desc())
            .limit(limit)
        )
        if since:
            statement = statement.where(Issue.created_at >= since)
        if open_only:
            statement = statement.where(Issue.status.in_(_open_statuses()))
        return [(value, int(count)) for value, count in (await self.session.execute(statement)).all()]

    async def resolution_stats(self, since: datetime) -> dict[str, float | int]:
        """Mean first-response and resolution times, plus mean satisfaction.

        Date arithmetic differs between dialects, so the elapsed-hours
        expression is built per dialect rather than in raw SQL.
        """
        dialect = self.session.bind.dialect.name if self.session.bind else "sqlite"
        if dialect == "sqlite":
            resolution_hours = (
                func.julianday(Issue.resolved_at) - func.julianday(Issue.created_at)
            ) * 24.0
            response_hours = (
                func.julianday(Issue.first_response_at) - func.julianday(Issue.created_at)
            ) * 24.0
        else:
            resolution_hours = (
                func.extract("epoch", Issue.resolved_at - Issue.created_at) / 3600.0
            )
            response_hours = (
                func.extract("epoch", Issue.first_response_at - Issue.created_at) / 3600.0
            )

        row = (
            await self.session.execute(
                select(
                    func.count(),
                    func.avg(resolution_hours),
                    func.avg(response_hours),
                    func.avg(Issue.satisfaction_rating),
                ).where(
                    Issue.tenant_id == self.tenant_id,
                    Issue.deleted_at.is_(None),
                    Issue.resolved_at.is_not(None),
                    Issue.resolved_at >= since,
                )
            )
        ).first()

        if row is None:
            return {
                "resolved": 0,
                "avg_resolution_hours": 0.0,
                "avg_first_response_hours": 0.0,
                "avg_rating": 0.0,
            }
        return {
            "resolved": int(row[0] or 0),
            "avg_resolution_hours": round(float(row[1] or 0.0), 2),
            "avg_first_response_hours": round(float(row[2] or 0.0), 2),
            "avg_rating": round(float(row[3] or 0.0), 2),
        }

    async def timeline(self, issue_id: uuid.UUID) -> Sequence[IssueEvent]:
        return (
            await self.session.scalars(
                select(IssueEvent)
                .where(
                    IssueEvent.tenant_id == self.tenant_id,
                    IssueEvent.issue_id == issue_id,
                )
                .order_by(IssueEvent.created_at.asc())
            )
        ).all()


def _open_statuses() -> list[IssueStatus]:
    return [status for status in IssueStatus if status.is_open]
