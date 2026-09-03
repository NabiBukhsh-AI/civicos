"""Domain models.

Importing this package registers every table on ``Base.metadata``, which is
what Alembic autogenerate and ``create_all`` rely on. Keep the re-exports in
sync when adding a model.
"""

from civicos.db.base import Base
from civicos.domain.assets import Asset, AssetInspection
from civicos.domain.budget import (
    BudgetLine,
    BudgetPeriod,
    DevelopmentProject,
    Expenditure,
)
from civicos.domain.engagement import (
    Announcement,
    EmergencyAlert,
    Feedback,
    Survey,
    SurveyQuestion,
    SurveyResponse,
)
from civicos.domain.identity import ApiKey, OneTimeCode, User, UserSession
from civicos.domain.issues import (
    Issue,
    IssueAttachment,
    IssueComment,
    IssueEvent,
    IssueFollower,
)
from civicos.domain.knowledge import (
    AIUsage,
    Conversation,
    ConversationMessage,
    Document,
    DocumentChunk,
)
from civicos.domain.operations import (
    AuditLog,
    DailyMetric,
    Notification,
    SavedView,
    WebhookEndpoint,
)
from civicos.domain.services import (
    ApplicationDocument,
    ApplicationEvent,
    ServiceApplication,
    ServiceSchedule,
    ServiceType,
)
from civicos.domain.tenancy import (
    AdminUnit,
    Department,
    IssueCategory,
    Municipality,
    Representative,
    SLAPolicy,
)
from civicos.domain.workorders import (
    Crew,
    CrewMember,
    MaterialUsage,
    WorkOrder,
    WorkOrderUpdate,
)

__all__ = [
    "AIUsage",
    "AdminUnit",
    "Announcement",
    "ApiKey",
    "ApplicationDocument",
    "ApplicationEvent",
    "Asset",
    "AssetInspection",
    "AuditLog",
    "Base",
    "BudgetLine",
    "BudgetPeriod",
    "Conversation",
    "ConversationMessage",
    "Crew",
    "CrewMember",
    "DailyMetric",
    "Department",
    "DevelopmentProject",
    "Document",
    "DocumentChunk",
    "EmergencyAlert",
    "Expenditure",
    "Feedback",
    "Issue",
    "IssueAttachment",
    "IssueCategory",
    "IssueComment",
    "IssueEvent",
    "IssueFollower",
    "MaterialUsage",
    "Municipality",
    "Notification",
    "OneTimeCode",
    "Representative",
    "SLAPolicy",
    "SavedView",
    "ServiceApplication",
    "ServiceSchedule",
    "ServiceType",
    "Survey",
    "SurveyQuestion",
    "SurveyResponse",
    "User",
    "UserSession",
    "WebhookEndpoint",
    "WorkOrder",
    "WorkOrderUpdate",
]
