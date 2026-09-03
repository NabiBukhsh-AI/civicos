"""Contracts for work orders, crews, assets, services and engagement."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field

from civicos.domain.enums import (
    AlertCategory,
    AlertSeverity,
    AnnouncementType,
    AssetCondition,
    AssetType,
    CrewShift,
    Priority,
    ScheduleFrequency,
    ServiceApplicationStatus,
    SurveyStatus,
    Visibility,
    WorkOrderStatus,
    WorkOrderType,
)
from civicos.schemas.common import APIModel, InputModel

# ------------------------------------------------------------- work orders ---


class ChecklistItem(InputModel):
    label: str = Field(min_length=1, max_length=200)
    done: bool = False
    required: bool = True
    note: str | None = Field(default=None, max_length=500)


class WorkOrderCreateRequest(InputModel):
    title: str = Field(min_length=3, max_length=255)
    instructions: str | None = Field(default=None, max_length=4000)
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
    estimated_hours: float | None = Field(default=None, ge=0, le=2000)
    estimated_cost: float | None = Field(default=None, ge=0)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    address: str | None = Field(default=None, max_length=500)
    checklist: list[ChecklistItem] = Field(default_factory=list, max_length=40)
    requires_verification: bool = True
    auto_dispatch: bool = False


class WorkOrderTransitionRequest(InputModel):
    status: WorkOrderStatus
    note: str | None = Field(default=None, max_length=2000)


class WorkOrderDispatchRequest(InputModel):
    crew_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None


class WorkOrderUpdateRequest(InputModel):
    note: str | None = Field(default=None, max_length=2000)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    recorded_at: datetime | None = Field(
        default=None, description="When the device captured this, for offline sync."
    )
    checklist: list[ChecklistItem] | None = Field(default=None, max_length=40)


class MaterialUsageRequest(InputModel):
    item_name: str = Field(min_length=1, max_length=160)
    item_code: str | None = Field(default=None, max_length=64)
    quantity: float = Field(default=1.0, gt=0)
    unit: str = Field(default="unit", max_length=24)
    unit_cost: float | None = Field(default=None, ge=0)


class WorkOrderCompleteRequest(InputModel):
    note: str = Field(min_length=3, max_length=2000)
    actual_hours: float | None = Field(default=None, ge=0, le=2000)
    actual_cost: float | None = Field(default=None, ge=0)
    materials: list[MaterialUsageRequest] = Field(default_factory=list, max_length=40)
    signoff_name: str | None = Field(default=None, max_length=160)
    asset_condition: AssetCondition | None = None


class WorkOrderUpdateOut(APIModel):
    id: uuid.UUID
    status: WorkOrderStatus | None = None
    note: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    recorded_at: datetime | None = None
    created_at: datetime


class WorkOrderSummary(APIModel):
    id: uuid.UUID
    reference: str
    title: str
    status: WorkOrderStatus
    order_type: WorkOrderType
    priority: Priority
    issue_id: uuid.UUID | None = None
    asset_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    assigned_to_id: uuid.UUID | None = None
    scheduled_for: datetime | None = None
    due_at: datetime | None = None
    created_at: datetime


class WorkOrderDetail(WorkOrderSummary):
    instructions: str | None = None
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    dispatched_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    verified_at: datetime | None = None
    estimated_hours: float | None = None
    actual_hours: float | None = None
    estimated_cost: float | None = None
    actual_cost: float | None = None
    checklist: list[dict[str, Any]] = Field(default_factory=list)
    completion_note: str | None = None
    blocked_reason: str | None = None
    signoff: dict[str, Any] = Field(default_factory=dict)
    updates: list[WorkOrderUpdateOut] = Field(default_factory=list)


class CrewCreateRequest(InputModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=2, max_length=160)
    department_id: uuid.UUID | None = None
    supervisor_id: uuid.UUID | None = None
    shift: CrewShift = CrewShift.MORNING
    coverage_unit_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    skills: list[str] = Field(default_factory=list, max_length=30)
    vehicle_registration: str | None = Field(default=None, max_length=32)
    contact_phone: str | None = Field(default=None, max_length=40)
    capacity_per_day: int = Field(default=8, ge=1, le=100)
    member_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class CrewOut(APIModel):
    id: uuid.UUID
    code: str
    name: str
    department_id: uuid.UUID | None = None
    supervisor_id: uuid.UUID | None = None
    shift: CrewShift
    skills: list[str]
    vehicle_registration: str | None = None
    contact_phone: str | None = None
    capacity_per_day: int
    is_active: bool


# ------------------------------------------------------------------ assets ---


class AssetCreateRequest(InputModel):
    code: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=2, max_length=200)
    asset_type: AssetType
    description: str | None = Field(default=None, max_length=2000)
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    address: str | None = Field(default=None, max_length=500)
    geometry: list[list[float]] = Field(
        default_factory=list, description="Line geometry for roads, drains and mains."
    )
    condition: AssetCondition = AssetCondition.GOOD
    installed_on: date | None = None
    expected_life_years: int | None = Field(default=None, ge=1, le=200)
    purchase_cost: float | None = Field(default=None, ge=0)
    manufacturer: str | None = Field(default=None, max_length=160)
    model: str | None = Field(default=None, max_length=160)
    serial_number: str | None = Field(default=None, max_length=120)
    specifications: dict[str, Any] = Field(default_factory=dict)
    inspection_interval_days: int | None = Field(default=None, ge=1, le=3650)


class AssetOut(APIModel):
    id: uuid.UUID
    code: str
    name: str
    asset_type: AssetType
    description: str | None = None
    department_id: uuid.UUID | None = None
    admin_unit_id: uuid.UUID | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    condition: AssetCondition
    is_operational: bool
    installed_on: date | None = None
    expected_life_years: int | None = None
    specifications: dict[str, Any]
    last_inspected_at: datetime | None = None
    next_inspection_due: datetime | None = None
    open_issue_count: int
    lifetime_maintenance_cost: float
    failure_count: int
    qr_payload: str | None = None
    created_at: datetime


class AssetInspectionRequest(InputModel):
    condition: AssetCondition
    severity: str | None = Field(default=None, max_length=16)
    findings: str | None = Field(default=None, max_length=2000)
    recommended_action: str | None = Field(default=None, max_length=2000)
    estimated_repair_cost: float | None = Field(default=None, ge=0)
    inspected_at: datetime | None = None


class AssetInspectionOut(APIModel):
    id: uuid.UUID
    asset_id: uuid.UUID
    inspected_at: datetime
    condition: AssetCondition
    severity: str | None = None
    findings: str | None = None
    recommended_action: str | None = None
    estimated_repair_cost: float | None = None
    is_ai_assisted: bool
    created_at: datetime


# ---------------------------------------------------------------- services ---


class ServiceTypeOut(APIModel):
    id: uuid.UUID
    slug: str
    name: str
    local_name: str | None = None
    description: str | None = None
    department_id: uuid.UUID | None = None
    form_schema: list[dict[str, Any]]
    required_documents: list[dict[str, Any]]
    fee_amount: float
    fee_description: str | None = None
    processing_days: int
    validity_days: int | None = None
    requires_inspection: bool
    requires_payment: bool
    is_active: bool
    icon: str | None = None


class ServiceApplicationRequest(InputModel):
    service_type_id: uuid.UUID
    applicant_name: str = Field(min_length=2, max_length=200)
    applicant_phone: str | None = Field(default=None, max_length=32)
    applicant_email: str | None = Field(default=None, max_length=160)
    form_data: dict[str, Any] = Field(default_factory=dict)
    premises_address: str | None = Field(default=None, max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    submit: bool = Field(default=True, description="False saves it as a draft.")


class ServiceDecisionRequest(InputModel):
    status: ServiceApplicationStatus
    note: str | None = Field(default=None, max_length=2000)
    info_request: str | None = Field(default=None, max_length=2000)
    certificate_number: str | None = Field(default=None, max_length=64)
    valid_until: date | None = None


class ServiceApplicationOut(APIModel):
    id: uuid.UUID
    reference: str
    service_type_id: uuid.UUID
    status: ServiceApplicationStatus
    applicant_name: str
    applicant_phone: str | None = None
    applicant_email: str | None = None
    form_data: dict[str, Any]
    premises_address: str | None = None
    current_step: int
    assigned_to_id: uuid.UUID | None = None
    submitted_at: datetime | None = None
    due_at: datetime | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    info_request: str | None = None
    fee_amount: float
    fee_paid: bool
    certificate_number: str | None = None
    issued_on: date | None = None
    valid_until: date | None = None
    ai_review: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ServiceScheduleOut(APIModel):
    id: uuid.UUID
    name: str
    service_kind: str
    description: str | None = None
    admin_unit_id: uuid.UUID | None = None
    frequency: ScheduleFrequency
    days_of_week: list[int]
    start_time: str | None = None
    end_time: str | None = None
    last_completed_at: datetime | None = None
    next_due_at: datetime | None = None
    reliability_score: float
    is_active: bool


class ServiceScheduleRequest(InputModel):
    name: str = Field(min_length=2, max_length=200)
    service_kind: str = Field(min_length=2, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    admin_unit_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    crew_id: uuid.UUID | None = None
    frequency: ScheduleFrequency = ScheduleFrequency.DAILY
    days_of_week: list[int] = Field(default_factory=list, max_length=7)
    start_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    end_time: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    effective_from: date | None = None
    effective_to: date | None = None
    visibility: Visibility = Visibility.PUBLIC


# -------------------------------------------------------------- engagement ---


class AnnouncementRequest(InputModel):
    title: str = Field(min_length=3, max_length=300)
    summary: str | None = Field(default=None, max_length=500)
    body: str = Field(min_length=10, max_length=20000)
    announcement_type: AnnouncementType = AnnouncementType.NOTICE
    department_id: uuid.UUID | None = None
    admin_unit_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    visibility: Visibility = Visibility.PUBLIC
    is_pinned: bool = False
    publish_now: bool = True
    expires_at: datetime | None = None
    event_starts_at: datetime | None = None
    event_ends_at: datetime | None = None
    location: str | None = Field(default=None, max_length=300)
    language: str = Field(default="en", max_length=8)
    auto_translate: bool = Field(
        default=False, description="Generate translations for the tenant's languages."
    )


class AnnouncementOut(APIModel):
    id: uuid.UUID
    title: str
    summary: str | None = None
    body: str
    body_translations: dict[str, Any] = Field(default_factory=dict)
    announcement_type: AnnouncementType
    department_id: uuid.UUID | None = None
    admin_unit_ids: list[Any] = Field(default_factory=list)
    visibility: Visibility
    is_pinned: bool
    published_at: datetime | None = None
    expires_at: datetime | None = None
    event_starts_at: datetime | None = None
    event_ends_at: datetime | None = None
    location: str | None = None
    cover_image_url: str | None = None
    view_count: int
    created_at: datetime


class AlertRequest(InputModel):
    title: str = Field(min_length=3, max_length=300)
    message: str = Field(min_length=5, max_length=4000)
    instructions: str | None = Field(default=None, max_length=4000)
    severity: AlertSeverity = AlertSeverity.ADVISORY
    category: AlertCategory = AlertCategory.OTHER
    admin_unit_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    centre_latitude: float | None = Field(default=None, ge=-90, le=90)
    centre_longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: int | None = Field(default=None, ge=50, le=100_000)
    channels: list[str] = Field(default_factory=lambda: ["in_app"], max_length=6)
    expires_at: datetime | None = None
    issue_now: bool = True


class AlertOut(APIModel):
    id: uuid.UUID
    title: str
    message: str
    instructions: str | None = None
    severity: AlertSeverity
    category: AlertCategory
    admin_unit_ids: list[Any] = Field(default_factory=list)
    centre_latitude: float | None = None
    centre_longitude: float | None = None
    radius_meters: int | None = None
    channels: list[Any]
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    cancelled_at: datetime | None = None
    is_active: bool
    recipients_targeted: int
    recipients_delivered: int
    created_at: datetime


class SurveyQuestionRequest(InputModel):
    prompt: str = Field(min_length=3, max_length=1000)
    question_type: str = Field(default="single_choice", max_length=24)
    options: list[str] = Field(default_factory=list, max_length=20)
    is_required: bool = True
    display_order: int = 0


class SurveyRequest(InputModel):
    title: str = Field(min_length=3, max_length=300)
    description: str | None = Field(default=None, max_length=4000)
    admin_unit_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    is_anonymous: bool = True
    requires_verification: bool = False
    questions: list[SurveyQuestionRequest] = Field(min_length=1, max_length=40)


class SurveyQuestionOut(APIModel):
    id: uuid.UUID
    prompt: str
    question_type: str
    options: list[Any]
    is_required: bool
    display_order: int


class SurveyOut(APIModel):
    id: uuid.UUID
    title: str
    description: str | None = None
    status: SurveyStatus
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    is_anonymous: bool
    response_count: int
    ai_summary: str | None = None
    questions: list[SurveyQuestionOut] = Field(default_factory=list)
    created_at: datetime


class SurveyResponseRequest(InputModel):
    answers: dict[str, Any] = Field(description="Keyed by question id.")
    admin_unit_id: uuid.UUID | None = None


class FeedbackRequest(InputModel):
    subject: str | None = Field(default=None, max_length=255)
    body: str = Field(min_length=5, max_length=4000)
    rating: int | None = Field(default=None, ge=1, le=5)
    contact_email: str | None = Field(default=None, max_length=160)


class NotificationOut(APIModel):
    id: uuid.UUID
    channel: str
    status: str
    subject: str | None = None
    body: str
    action_url: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    read_at: datetime | None = None
    created_at: datetime
