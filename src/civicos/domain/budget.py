"""Budget transparency: fiscal periods, allocation lines, projects, expenditure.

Publishing where the money goes - and tying development projects to the wards
they serve and the complaints they close - is one of the strongest trust
mechanisms a municipality has. The model is intentionally simple enough that a
town accounts clerk can maintain it from a spreadsheet import.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from civicos.db.base import (
    Base,
    MetadataMixin,
    SoftDeleteMixin,
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)
from civicos.db.types import Coordinate, MutableJSONList, StringEnum
from civicos.domain.enums import BudgetCategory, ProjectStatus, Visibility


class BudgetPeriod(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A fiscal year (or quarter) that lines and expenditure roll up to."""

    __tablename__ = "budget_periods"
    __table_args__ = (UniqueConstraint("tenant_id", "code", name="uq_budget_periods_tenant_code"),)

    code: Mapped[str] = mapped_column(String(24), nullable=False)
    """e.g. ``FY2025-26``"""
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    total_allocated: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_spent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    currency_code: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    lines: Mapped[list["BudgetLine"]] = relationship(
        back_populates="period", cascade="all, delete-orphan"
    )

    @property
    def utilisation(self) -> float:
        return round(self.total_spent / self.total_allocated, 4) if self.total_allocated else 0.0


class BudgetLine(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, MetadataMixin, Base):
    """One allocation head within a period."""

    __tablename__ = "budget_lines"
    __table_args__ = (
        UniqueConstraint("period_id", "code", name="uq_budget_lines_period_code"),
        Index("ix_budget_lines_tenant_period", "tenant_id", "period_id"),
    )

    period_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("budget_periods.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[BudgetCategory] = mapped_column(
        StringEnum(BudgetCategory), default=BudgetCategory.OPERATIONS, nullable=False
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )

    allocated_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    revised_amount: Mapped[float | None] = mapped_column(Float)
    spent_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    committed_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)

    period: Mapped[BudgetPeriod] = relationship(back_populates="lines")
    expenditures: Mapped[list["Expenditure"]] = relationship(
        back_populates="budget_line", cascade="all, delete-orphan"
    )

    @property
    def effective_allocation(self) -> float:
        return self.revised_amount if self.revised_amount is not None else self.allocated_amount

    @property
    def available(self) -> float:
        return round(self.effective_allocation - self.spent_amount - self.committed_amount, 2)

    @property
    def utilisation(self) -> float:
        total = self.effective_allocation
        return round(self.spent_amount / total, 4) if total else 0.0


class DevelopmentProject(
    UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, SoftDeleteMixin, MetadataMixin, Base
):
    """A capital / development scheme, with the ward it serves and its progress."""

    __tablename__ = "development_projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "code", name="uq_development_projects_tenant_code"),
        Index("ix_development_projects_tenant_status", "tenant_id", "status"),
        Index("ix_development_projects_tenant_unit", "tenant_id", "admin_unit_id"),
    )

    code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProjectStatus] = mapped_column(
        StringEnum(ProjectStatus), default=ProjectStatus.PROPOSED, nullable=False
    )

    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    admin_unit_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("admin_units.id", ondelete="SET NULL")
    )
    budget_line_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("budget_lines.id", ondelete="SET NULL")
    )

    estimated_cost: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    awarded_cost: Mapped[float | None] = mapped_column(Float)
    spent_amount: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    contractor_name: Mapped[str | None] = mapped_column(String(255))
    tender_reference: Mapped[str | None] = mapped_column(String(120))

    planned_start: Mapped[date | None] = mapped_column(Date)
    planned_end: Mapped[date | None] = mapped_column(Date)
    actual_start: Mapped[date | None] = mapped_column(Date)
    actual_end: Mapped[date | None] = mapped_column(Date)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    latitude: Mapped[float | None] = mapped_column(Coordinate)
    longitude: Mapped[float | None] = mapped_column(Coordinate)
    beneficiaries: Mapped[int | None] = mapped_column(Integer)
    #: Issues this scheme is meant to resolve - closes the loop from complaint to capital works.
    linked_issue_ids: Mapped[list[Any]] = mapped_column(
        MutableJSONList, default=list, nullable=False
    )
    photo_keys: Mapped[list[Any]] = mapped_column(MutableJSONList, default=list, nullable=False)
    visibility: Mapped[Visibility] = mapped_column(
        StringEnum(Visibility), default=Visibility.PUBLIC, nullable=False
    )

    @property
    def is_delayed(self) -> bool:
        from civicos.core.clock import local_date

        if self.actual_end or not self.planned_end:
            return False
        return local_date() > self.planned_end


class Expenditure(UUIDPrimaryKeyMixin, TenantMixin, TimestampMixin, Base):
    """A recorded spend against a budget line, optionally tied to a project."""

    __tablename__ = "expenditures"
    __table_args__ = (
        Index("ix_expenditures_tenant_date", "tenant_id", "spent_on"),
        Index("ix_expenditures_line", "budget_line_id"),
    )

    budget_line_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("budget_lines.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("development_projects.id", ondelete="SET NULL")
    )
    work_order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_orders.id", ondelete="SET NULL")
    )

    description: Mapped[str] = mapped_column(String(500), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    spent_on: Mapped[date] = mapped_column(Date, nullable=False)
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    voucher_number: Mapped[str | None] = mapped_column(String(80))
    receipt_key: Mapped[str | None] = mapped_column(String(500))
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    is_published: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    budget_line: Mapped[BudgetLine] = relationship(back_populates="expenditures")
