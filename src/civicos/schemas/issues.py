"""Issue (civic report) API contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from civicos.domain.enums import (
    IssueStatus,
    Priority,
    ReportChannel,
    Severity,
    SLAState,
    Visibility,
)
from civicos.schemas.common import (
    APIModel,
    AttachmentOut,
    Coordinates,
    InputModel,
    TimelineEntry,
)


class IssueCreateRequest(InputModel):
    """What a resident, call-centre operator or field app submits."""

    description: str = Field(
        min_length=10,
        max_length=8000,
        description="What is wrong, in the reporter's own words.",
        examples=["The drain outside the community centre has been overflowing for a week."],
    )
    title: str | None = Field(
        default=None,
        max_length=255,
        description="Optional. Derived from the description when omitted.",
    )
    category_slug: str | None = Field(
        default=None,
        max_length=64,
        description="Optional. AI triage proposes one when omitted.",
    )
    priority: Priority | None = Field(
        default=None, description="Staff-only override; ignored for public submissions."
    )
    channel: ReportChannel = ReportChannel.WEB
    location: Coordinates | None = None
    address: str | None = Field(default=None, max_length=500)
    landmark: str | None = Field(default=None, max_length=255)
    language: str | None = Field(default=None, max_length=8)

    reporter_name: str | None = Field(default=None, max_length=160)
    reporter_phone: str | None = Field(default=None, max_length=32)
    reporter_email: str | None = Field(default=None, max_length=160)
    is_anonymous: bool = False
    contact_consent: bool = Field(
        default=True, description="False means: record it, but do not contact me."
    )
    tags: list[str] = Field(default_factory=list, max_length=10)
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _anonymous_has_no_contact(self) -> IssueCreateRequest:
        if self.is_anonymous and (self.reporter_phone or self.reporter_email):
            # An "anonymous" report carrying a phone number is a contradiction
            # the reporter did not intend; drop the identifying fields.
            self.reporter_phone = None
            self.reporter_email = None
            self.reporter_name = None
        return self


class IssueUpdateRequest(InputModel):
    title: str | None = Field(default=None, max_length=255)
    category_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    priority: Priority | None = None
    severity: Severity | None = None
    admin_unit_id: uuid.UUID | None = None
    address: str | None = Field(default=None, max_length=500)
    landmark: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = Field(default=None, max_length=10)
    visibility: Visibility | None = None
    estimated_cost: float | None = Field(default=None, ge=0)


class IssueTransitionRequest(InputModel):
    status: IssueStatus
    note: str | None = Field(default=None, max_length=2000)
    resolution_note: str | None = Field(default=None, max_length=2000)
    rejection_reason: str | None = Field(default=None, max_length=1000)


class IssueAssignRequest(InputModel):
    assignee_id: uuid.UUID
    note: str | None = Field(default=None, max_length=1000)


class IssueMergeRequest(InputModel):
    parent_issue_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=500)


class IssueCommentRequest(InputModel):
    body: str = Field(min_length=1, max_length=4000)
    visibility: Visibility = Visibility.PUBLIC
    is_official: bool = False


class IssueRatingRequest(InputModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=1000)


class IssueEscalateRequest(InputModel):
    reason: str = Field(min_length=3, max_length=1000)


class CategoryRef(APIModel):
    id: uuid.UUID
    slug: str
    name: str
    icon: str | None = None
    colour: str | None = None


class DepartmentRef(APIModel):
    id: uuid.UUID
    code: str
    name: str


class AssigneeRef(APIModel):
    id: uuid.UUID
    full_name: str
    designation: str | None = None


class AITriageOut(APIModel):
    """The model's opinion, always reported separately from the decision."""

    category_slug: str | None = None
    confidence: float | None = None
    priority: Priority | None = None
    severity: Severity | None = None
    summary: str | None = None
    model: str | None = None
    triaged_at: datetime | None = None
    analysis: dict[str, Any] = Field(default_factory=dict)


class SLAOut(APIModel):
    response_due_at: datetime | None = None
    resolution_due_at: datetime | None = None
    first_response_at: datetime | None = None
    resolved_at: datetime | None = None
    response_state: SLAState
    resolution_state: SLAState
    escalation_level: int


class IssueSummary(APIModel):
    """List-view shape - deliberately small, no timeline, no attachments."""

    id: uuid.UUID
    reference: str
    title: str
    status: IssueStatus
    priority: Priority
    severity: Severity | None = None
    channel: ReportChannel
    category: CategoryRef | None = None
    department: DepartmentRef | None = None
    assignee: AssigneeRef | None = None
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    confirmations: int
    duplicate_count: int
    is_flagged: bool
    sla_resolution_state: SLAState
    resolution_due_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class IssueDetail(IssueSummary):
    description: str
    language: str
    landmark: str | None = None
    admin_unit_id: uuid.UUID | None = None
    tags: list[str] = Field(default_factory=list)
    visibility: Visibility
    reporter_name: str | None = None
    is_anonymous: bool
    duplicate_of_id: uuid.UUID | None = None
    resolution_note: str | None = None
    rejection_reason: str | None = None
    satisfaction_rating: int | None = None
    estimated_cost: float | None = None
    actual_cost: float | None = None
    reopened_count: int
    view_count: int
    sla: SLAOut | None = None
    ai: AITriageOut | None = None
    attachments: list[AttachmentOut] = Field(default_factory=list)
    timeline: list[TimelineEntry] = Field(default_factory=list)
    comments: list[IssueCommentOut] = Field(default_factory=list)


class IssueCommentOut(APIModel):
    id: uuid.UUID
    body: str
    author_label: str
    author_id: uuid.UUID | None = None
    visibility: Visibility
    is_official: bool
    created_at: datetime


class DuplicateCandidateOut(APIModel):
    issue_id: uuid.UUID
    reference: str
    title: str
    status: str
    distance_meters: float
    score: float
    reason: str


class IssueCreatedOut(APIModel):
    """Intake response: the record, plus what the pipeline decided about it."""

    issue: IssueDetail
    created: bool
    merged_into: str | None = Field(
        default=None, description="Reference of the report this was merged into."
    )
    duplicate_candidates: list[DuplicateCandidateOut] = Field(default_factory=list)
    ai_applied: bool = False
    routing_reason: str = ""
    warnings: list[str] = Field(default_factory=list)


class PublicIssueOut(APIModel):
    """Redacted shape for the open-data feed and the public map.

    No reporter identity, no internal notes, no free-text description - only
    what a municipality can publish without exposing a resident.
    """

    reference: str
    title: str
    status: IssueStatus
    category: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    admin_unit: str | None = None
    confirmations: int
    created_at: datetime
    resolved_at: datetime | None = None


class NearbyIssueOut(PublicIssueOut):
    distance_meters: float


IssueDetail.model_rebuild()
