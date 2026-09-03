"""Domain vocabulary.

These enums are the shared language between the API, the workflow engine, the
AI layer and the analytics queries. They are ``StrEnum`` so they serialise to
readable JSON and store as readable VARCHAR - a DBA can read the issues table
without a lookup table.
"""

from __future__ import annotations

from enum import StrEnum


# --------------------------------------------------------------- tenancy -----


class MunicipalityTier(StrEnum):
    """Administrative level.

    The default vocabulary covers the common local-government tiers; a
    deployment that uses different words maps them in its tenant config
    (see docs/architecture.md).
    """

    METROPOLITAN = "metropolitan"
    DISTRICT = "district"
    TOWN = "town"
    MUNICIPAL_COMMITTEE = "municipal_committee"
    UNION_COUNCIL = "union_council"
    CANTONMENT = "cantonment"
    OTHER = "other"


class AdminUnitType(StrEnum):
    """Sub-divisions inside a municipality."""

    ZONE = "zone"
    UNION_COUNCIL = "union_council"
    WARD = "ward"
    NEIGHBOURHOOD = "neighbourhood"
    SECTOR = "sector"
    BLOCK = "block"


class TenantStatus(StrEnum):
    ACTIVE = "active"
    ONBOARDING = "onboarding"
    SUSPENDED = "suspended"
    ARCHIVED = "archived"


# -------------------------------------------------------------- identity -----


class UserStatus(StrEnum):
    ACTIVE = "active"
    INVITED = "invited"
    SUSPENDED = "suspended"
    DEACTIVATED = "deactivated"


class VerificationMethod(StrEnum):
    NONE = "none"
    PHONE_OTP = "phone_otp"
    EMAIL = "email"
    NADRA = "nadra"
    STAFF_VOUCHED = "staff_vouched"


# ---------------------------------------------------------------- issues -----


class IssueStatus(StrEnum):
    """Lifecycle of a civic complaint."""

    SUBMITTED = "submitted"
    TRIAGED = "triaged"
    ACKNOWLEDGED = "acknowledged"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    RESOLVED = "resolved"
    VERIFIED = "verified"
    CLOSED = "closed"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    REOPENED = "reopened"

    @property
    def is_open(self) -> bool:
        return self in _OPEN_ISSUE_STATUSES

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_ISSUE_STATUSES


_OPEN_ISSUE_STATUSES = frozenset(
    {
        IssueStatus.SUBMITTED,
        IssueStatus.TRIAGED,
        IssueStatus.ACKNOWLEDGED,
        IssueStatus.ASSIGNED,
        IssueStatus.IN_PROGRESS,
        IssueStatus.ON_HOLD,
        IssueStatus.REOPENED,
    }
)

_TERMINAL_ISSUE_STATUSES = frozenset(
    {
        IssueStatus.CLOSED,
        IssueStatus.REJECTED,
        IssueStatus.DUPLICATE,
    }
)

#: Allowed state machine. Anything not listed here is rejected by the workflow.
ISSUE_TRANSITIONS: dict[IssueStatus, frozenset[IssueStatus]] = {
    IssueStatus.SUBMITTED: frozenset(
        {
            IssueStatus.TRIAGED,
            IssueStatus.ACKNOWLEDGED,
            IssueStatus.ASSIGNED,
            IssueStatus.REJECTED,
            IssueStatus.DUPLICATE,
        }
    ),
    IssueStatus.TRIAGED: frozenset(
        {
            IssueStatus.ACKNOWLEDGED,
            IssueStatus.ASSIGNED,
            IssueStatus.REJECTED,
            IssueStatus.DUPLICATE,
        }
    ),
    IssueStatus.ACKNOWLEDGED: frozenset(
        {IssueStatus.ASSIGNED, IssueStatus.REJECTED, IssueStatus.DUPLICATE}
    ),
    IssueStatus.ASSIGNED: frozenset(
        {
            IssueStatus.IN_PROGRESS,
            IssueStatus.ON_HOLD,
            IssueStatus.RESOLVED,
            IssueStatus.REJECTED,
            IssueStatus.DUPLICATE,
        }
    ),
    IssueStatus.IN_PROGRESS: frozenset(
        {IssueStatus.RESOLVED, IssueStatus.ON_HOLD, IssueStatus.ASSIGNED}
    ),
    IssueStatus.ON_HOLD: frozenset(
        {IssueStatus.IN_PROGRESS, IssueStatus.ASSIGNED, IssueStatus.REJECTED}
    ),
    IssueStatus.RESOLVED: frozenset(
        {IssueStatus.VERIFIED, IssueStatus.REOPENED, IssueStatus.CLOSED}
    ),
    IssueStatus.VERIFIED: frozenset({IssueStatus.CLOSED, IssueStatus.REOPENED}),
    IssueStatus.REOPENED: frozenset(
        {IssueStatus.ASSIGNED, IssueStatus.IN_PROGRESS, IssueStatus.REJECTED}
    ),
    IssueStatus.CLOSED: frozenset({IssueStatus.REOPENED}),
    IssueStatus.REJECTED: frozenset({IssueStatus.REOPENED}),
    IssueStatus.DUPLICATE: frozenset({IssueStatus.REOPENED}),
}


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    EMERGENCY = "emergency"

    @property
    def weight(self) -> int:
        return _PRIORITY_WEIGHTS[self]


_PRIORITY_WEIGHTS = {
    Priority.LOW: 1,
    Priority.NORMAL: 2,
    Priority.HIGH: 3,
    Priority.URGENT: 4,
    Priority.EMERGENCY: 5,
}


class Severity(StrEnum):
    """How bad the physical condition is, as judged from evidence."""

    MINOR = "minor"
    MODERATE = "moderate"
    MAJOR = "major"
    CRITICAL = "critical"


class ReportChannel(StrEnum):
    """Where a report came from - drives notification routing and analytics."""

    WEB = "web"
    MOBILE = "mobile"
    WHATSAPP = "whatsapp"
    SMS = "sms"
    HELPLINE = "helpline"
    WALK_IN = "walk_in"
    EMAIL = "email"
    FIELD_INSPECTION = "field_inspection"
    SENSOR = "sensor"
    SOCIAL_MEDIA = "social_media"
    API = "api"


class IssueEventType(StrEnum):
    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    ASSIGNED = "assigned"
    COMMENTED = "commented"
    ATTACHMENT_ADDED = "attachment_added"
    PRIORITY_CHANGED = "priority_changed"
    CATEGORY_CHANGED = "category_changed"
    ESCALATED = "escalated"
    MERGED = "merged"
    SLA_BREACHED = "sla_breached"
    AI_TRIAGED = "ai_triaged"
    REOPENED = "reopened"
    RATED = "rated"
    NOTIFIED = "notified"


class SLAStage(StrEnum):
    RESPONSE = "response"
    RESOLUTION = "resolution"


class SLAState(StrEnum):
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BREACHED = "breached"
    MET = "met"
    NOT_APPLICABLE = "not_applicable"


# ----------------------------------------------------------- work orders -----


class WorkOrderStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    VERIFIED = "verified"
    CANCELLED = "cancelled"


WORK_ORDER_TRANSITIONS: dict[WorkOrderStatus, frozenset[WorkOrderStatus]] = {
    WorkOrderStatus.DRAFT: frozenset(
        {WorkOrderStatus.SCHEDULED, WorkOrderStatus.DISPATCHED, WorkOrderStatus.CANCELLED}
    ),
    WorkOrderStatus.SCHEDULED: frozenset(
        {WorkOrderStatus.DISPATCHED, WorkOrderStatus.CANCELLED, WorkOrderStatus.BLOCKED}
    ),
    WorkOrderStatus.DISPATCHED: frozenset(
        {WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED}
    ),
    WorkOrderStatus.IN_PROGRESS: frozenset(
        {WorkOrderStatus.COMPLETED, WorkOrderStatus.BLOCKED, WorkOrderStatus.CANCELLED}
    ),
    WorkOrderStatus.BLOCKED: frozenset(
        {WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.SCHEDULED, WorkOrderStatus.CANCELLED}
    ),
    WorkOrderStatus.COMPLETED: frozenset({WorkOrderStatus.VERIFIED, WorkOrderStatus.IN_PROGRESS}),
    WorkOrderStatus.VERIFIED: frozenset(),
    WorkOrderStatus.CANCELLED: frozenset(),
}


class WorkOrderType(StrEnum):
    CORRECTIVE = "corrective"
    PREVENTIVE = "preventive"
    INSPECTION = "inspection"
    EMERGENCY = "emergency"
    PROJECT = "project"


class CrewShift(StrEnum):
    MORNING = "morning"
    EVENING = "evening"
    NIGHT = "night"
    ON_CALL = "on_call"


# ---------------------------------------------------------------- assets -----


class AssetType(StrEnum):
    STREETLIGHT = "streetlight"
    ROAD_SEGMENT = "road_segment"
    DRAIN = "drain"
    SEWER_LINE = "sewer_line"
    WATER_LINE = "water_line"
    WATER_PUMP = "water_pump"
    OVERHEAD_TANK = "overhead_tank"
    PARK = "park"
    PLAYGROUND = "playground"
    PUBLIC_TOILET = "public_toilet"
    WASTE_BIN = "waste_bin"
    TRANSFER_STATION = "transfer_station"
    SCHOOL = "school"
    DISPENSARY = "dispensary"
    COMMUNITY_CENTRE = "community_centre"
    GRAVEYARD = "graveyard"
    BUILDING = "building"
    VEHICLE = "vehicle"
    SIGNAGE = "signage"
    CCTV = "cctv"
    OTHER = "other"


class AssetCondition(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    CRITICAL = "critical"
    DECOMMISSIONED = "decommissioned"


# -------------------------------------------------------------- services -----


class ServiceApplicationStatus(StrEnum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    INFO_REQUIRED = "info_required"
    INSPECTION_SCHEDULED = "inspection_scheduled"
    PAYMENT_PENDING = "payment_pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    ISSUED = "issued"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


SERVICE_TRANSITIONS: dict[ServiceApplicationStatus, frozenset[ServiceApplicationStatus]] = {
    ServiceApplicationStatus.DRAFT: frozenset(
        {ServiceApplicationStatus.SUBMITTED, ServiceApplicationStatus.CANCELLED}
    ),
    ServiceApplicationStatus.SUBMITTED: frozenset(
        {
            ServiceApplicationStatus.UNDER_REVIEW,
            ServiceApplicationStatus.INFO_REQUIRED,
            ServiceApplicationStatus.REJECTED,
            ServiceApplicationStatus.CANCELLED,
        }
    ),
    ServiceApplicationStatus.UNDER_REVIEW: frozenset(
        {
            ServiceApplicationStatus.INFO_REQUIRED,
            ServiceApplicationStatus.INSPECTION_SCHEDULED,
            ServiceApplicationStatus.PAYMENT_PENDING,
            ServiceApplicationStatus.APPROVED,
            ServiceApplicationStatus.REJECTED,
        }
    ),
    ServiceApplicationStatus.INFO_REQUIRED: frozenset(
        {
            ServiceApplicationStatus.UNDER_REVIEW,
            ServiceApplicationStatus.CANCELLED,
            ServiceApplicationStatus.REJECTED,
        }
    ),
    ServiceApplicationStatus.INSPECTION_SCHEDULED: frozenset(
        {
            ServiceApplicationStatus.UNDER_REVIEW,
            ServiceApplicationStatus.PAYMENT_PENDING,
            ServiceApplicationStatus.APPROVED,
            ServiceApplicationStatus.REJECTED,
        }
    ),
    ServiceApplicationStatus.PAYMENT_PENDING: frozenset(
        {ServiceApplicationStatus.APPROVED, ServiceApplicationStatus.CANCELLED}
    ),
    ServiceApplicationStatus.APPROVED: frozenset({ServiceApplicationStatus.ISSUED}),
    ServiceApplicationStatus.ISSUED: frozenset(
        {ServiceApplicationStatus.EXPIRED, ServiceApplicationStatus.CANCELLED}
    ),
    ServiceApplicationStatus.REJECTED: frozenset({ServiceApplicationStatus.UNDER_REVIEW}),
    ServiceApplicationStatus.EXPIRED: frozenset(),
    ServiceApplicationStatus.CANCELLED: frozenset(),
}


class ScheduleFrequency(StrEnum):
    """Cadence of a recurring municipal service (waste pickup, water supply)."""

    DAILY = "daily"
    ALTERNATE_DAYS = "alternate_days"
    WEEKLY = "weekly"
    TWICE_WEEKLY = "twice_weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"
    ON_DEMAND = "on_demand"


# ------------------------------------------------------------- knowledge -----


class DocumentType(StrEnum):
    BYLAW = "bylaw"
    POLICY = "policy"
    BUDGET = "budget"
    TENDER = "tender"
    NOTICE = "notice"
    MINUTES = "minutes"
    REPORT = "report"
    FORM = "form"
    SOP = "sop"
    CONTRACT = "contract"
    MAP = "map"
    OTHER = "other"


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    ARCHIVED = "archived"


class Visibility(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


# ------------------------------------------------------------ engagement -----


class AnnouncementType(StrEnum):
    NOTICE = "notice"
    NEWS = "news"
    EVENT = "event"
    TENDER = "tender"
    OUTAGE = "outage"
    ROAD_CLOSURE = "road_closure"
    VACANCY = "vacancy"


class AlertSeverity(StrEnum):
    INFO = "info"
    ADVISORY = "advisory"
    WATCH = "watch"
    WARNING = "warning"
    EMERGENCY = "emergency"


class AlertCategory(StrEnum):
    FLOOD = "flood"
    HEATWAVE = "heatwave"
    RAIN = "rain"
    WATER_SUPPLY = "water_supply"
    POWER = "power"
    DISEASE_OUTBREAK = "disease_outbreak"
    FIRE = "fire"
    BUILDING_COLLAPSE = "building_collapse"
    SECURITY = "security"
    TRAFFIC = "traffic"
    OTHER = "other"


class SurveyStatus(StrEnum):
    DRAFT = "draft"
    OPEN = "open"
    CLOSED = "closed"
    ARCHIVED = "archived"


class QuestionType(StrEnum):
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    RATING = "rating"
    TEXT = "text"
    YES_NO = "yes_no"
    NUMBER = "number"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    MIXED = "mixed"


# ---------------------------------------------------------------- budget -----


class BudgetCategory(StrEnum):
    DEVELOPMENT = "development"
    OPERATIONS = "operations"
    SALARIES = "salaries"
    MAINTENANCE = "maintenance"
    EMERGENCY = "emergency"
    GRANT = "grant"
    OTHER = "other"


class ProjectStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    TENDERED = "tendered"
    AWARDED = "awarded"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"


# --------------------------------------------------------- notifications -----


class NotificationChannel(StrEnum):
    IN_APP = "in_app"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    PUSH = "push"
    VOICE = "voice"
    WEBHOOK = "webhook"


class NotificationStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


# -------------------------------------------------------------------- AI -----


class AICapability(StrEnum):
    CHAT = "chat"
    RAG = "rag"
    TRIAGE = "triage"
    VISION = "vision"
    SUMMARY = "summary"
    TRANSLATION = "translation"
    EMBEDDING = "embedding"
    BRIEFING = "briefing"
    DUPLICATE_CHECK = "duplicate_check"
    MODERATION = "moderation"


class ConversationRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class AuditAction(StrEnum):
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    EXPORT = "export"
    ASSIGN = "assign"
    TRANSITION = "transition"
    CONFIGURE = "configure"
    AI_INVOKE = "ai_invoke"
