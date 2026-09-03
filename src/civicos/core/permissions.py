"""Role based access control.

Permissions are plain ``resource:action`` strings. Roles map to a permission
set; a trailing ``*`` grants every action on a resource. Keeping the matrix in
one module means an auditor can read the entire authorisation model on a single
screen, and tests can assert against it directly.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Who someone is inside a municipality."""

    SUPER_ADMIN = "super_admin"
    """Platform operator. Crosses tenant boundaries; used for onboarding."""

    TENANT_ADMIN = "tenant_admin"
    """Municipal administrator - owns configuration for one municipality."""

    DEPARTMENT_HEAD = "department_head"
    """Runs a department (Sanitation, Water, Roads...)."""

    SUPERVISOR = "supervisor"
    """Assigns and verifies field work within a department or ward."""

    FIELD_AGENT = "field_agent"
    """Crew member who executes work orders in the field."""

    CALL_CENTER = "call_center"
    """Helpline operator who lodges complaints on behalf of citizens."""

    ANALYST = "analyst"
    """Read-only access to operational data and analytics."""

    REPRESENTATIVE = "representative"
    """Elected councillor / UC chairman: ward oversight, no operational writes."""

    CITIZEN = "citizen"
    """Registered resident."""

    SERVICE_ACCOUNT = "service_account"
    """Machine client authenticating with an API key."""


class Resource(StrEnum):
    TENANT = "tenants"
    USER = "users"
    DEPARTMENT = "departments"
    ISSUE = "issues"
    WORK_ORDER = "workorders"
    ASSET = "assets"
    SERVICE_REQUEST = "servicerequests"
    DOCUMENT = "documents"
    ANNOUNCEMENT = "announcements"
    ALERT = "alerts"
    SURVEY = "surveys"
    BUDGET = "budget"
    ANALYTICS = "analytics"
    AI = "ai"
    AUDIT = "audit"
    CATEGORY = "categories"
    SLA = "sla"


def perm(resource: Resource | str, action: str) -> str:
    return f"{resource}:{action}"


# Common bundles -------------------------------------------------------------

_ALL = "*"

_READ_OPS = frozenset(
    {
        perm(Resource.ISSUE, "read"),
        perm(Resource.WORK_ORDER, "read"),
        perm(Resource.ASSET, "read"),
        perm(Resource.SERVICE_REQUEST, "read"),
        perm(Resource.DOCUMENT, "read"),
        perm(Resource.ANNOUNCEMENT, "read"),
        perm(Resource.CATEGORY, "read"),
        perm(Resource.DEPARTMENT, "read"),
    }
)

_CITIZEN_PERMISSIONS = frozenset(
    {
        perm(Resource.ISSUE, "create"),
        perm(Resource.ISSUE, "read_own"),
        perm(Resource.ISSUE, "comment"),
        perm(Resource.ISSUE, "rate"),
        perm(Resource.SERVICE_REQUEST, "create"),
        perm(Resource.SERVICE_REQUEST, "read_own"),
        perm(Resource.ANNOUNCEMENT, "read"),
        perm(Resource.DOCUMENT, "read"),
        perm(Resource.SURVEY, "respond"),
        perm(Resource.AI, "chat"),
        perm(Resource.AI, "vision"),
    }
)

_FIELD_AGENT_PERMISSIONS = _CITIZEN_PERMISSIONS | {
    perm(Resource.ISSUE, "read"),
    perm(Resource.WORK_ORDER, "read"),
    perm(Resource.WORK_ORDER, "update_assigned"),
    perm(Resource.WORK_ORDER, "complete"),
    perm(Resource.ASSET, "read"),
    perm(Resource.ASSET, "inspect"),
}

_CALL_CENTER_PERMISSIONS = _READ_OPS | {
    perm(Resource.ISSUE, "create"),
    perm(Resource.ISSUE, "update"),
    perm(Resource.ISSUE, "comment"),
    perm(Resource.SERVICE_REQUEST, "create"),
    perm(Resource.SERVICE_REQUEST, "update"),
    perm(Resource.AI, "chat"),
    perm(Resource.AI, "vision"),
    perm(Resource.AI, "translate"),
}

_SUPERVISOR_PERMISSIONS = _CALL_CENTER_PERMISSIONS | {
    perm(Resource.ISSUE, "assign"),
    perm(Resource.ISSUE, "transition"),
    perm(Resource.ISSUE, "merge"),
    perm(Resource.WORK_ORDER, "create"),
    perm(Resource.WORK_ORDER, "update"),
    perm(Resource.WORK_ORDER, "assign"),
    perm(Resource.WORK_ORDER, "close"),
    perm(Resource.ASSET, "update"),
    perm(Resource.ANALYTICS, "read"),
    perm(Resource.AI, "triage"),
    perm(Resource.AI, "brief"),
}

_DEPARTMENT_HEAD_PERMISSIONS = _SUPERVISOR_PERMISSIONS | {
    perm(Resource.ISSUE, "reject"),
    perm(Resource.ISSUE, "escalate"),
    perm(Resource.ASSET, "create"),
    perm(Resource.ASSET, "delete"),
    perm(Resource.SERVICE_REQUEST, "decide"),
    perm(Resource.DOCUMENT, "create"),
    perm(Resource.ANNOUNCEMENT, "create"),
    perm(Resource.SURVEY, "read"),
    perm(Resource.BUDGET, "read"),
    perm(Resource.USER, "read"),
    perm(Resource.SLA, "read"),
}

_ANALYST_PERMISSIONS = _READ_OPS | {
    perm(Resource.ANALYTICS, "read"),
    perm(Resource.ANALYTICS, "export"),
    perm(Resource.BUDGET, "read"),
    perm(Resource.SURVEY, "read"),
    perm(Resource.SLA, "read"),
    perm(Resource.AI, "chat"),
    perm(Resource.AI, "brief"),
}

_REPRESENTATIVE_PERMISSIONS = _ANALYST_PERMISSIONS | {
    perm(Resource.ISSUE, "comment"),
    perm(Resource.ISSUE, "escalate"),
    perm(Resource.ANNOUNCEMENT, "create"),
    perm(Resource.ALERT, "create"),
    perm(Resource.SURVEY, "create"),
}

_TENANT_ADMIN_PERMISSIONS = frozenset(
    {
        perm(resource, _ALL)
        for resource in Resource
        if resource is not Resource.TENANT
    }
) | {
    perm(Resource.TENANT, "read"),
    perm(Resource.TENANT, "update"),
}

ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.SUPER_ADMIN: frozenset({perm(resource, _ALL) for resource in Resource}),
    Role.TENANT_ADMIN: frozenset(_TENANT_ADMIN_PERMISSIONS),
    Role.DEPARTMENT_HEAD: frozenset(_DEPARTMENT_HEAD_PERMISSIONS),
    Role.SUPERVISOR: frozenset(_SUPERVISOR_PERMISSIONS),
    Role.FIELD_AGENT: frozenset(_FIELD_AGENT_PERMISSIONS),
    Role.CALL_CENTER: frozenset(_CALL_CENTER_PERMISSIONS),
    Role.ANALYST: frozenset(_ANALYST_PERMISSIONS),
    Role.REPRESENTATIVE: frozenset(_REPRESENTATIVE_PERMISSIONS),
    Role.CITIZEN: frozenset(_CITIZEN_PERMISSIONS),
    Role.SERVICE_ACCOUNT: frozenset(
        _READ_OPS
        | {
            perm(Resource.ISSUE, "create"),
            perm(Resource.AI, "chat"),
            perm(Resource.AI, "vision"),
        }
    ),
}

#: Roles that operate the municipality (as opposed to residents / machines).
STAFF_ROLES: frozenset[Role] = frozenset(
    {
        Role.SUPER_ADMIN,
        Role.TENANT_ADMIN,
        Role.DEPARTMENT_HEAD,
        Role.SUPERVISOR,
        Role.FIELD_AGENT,
        Role.CALL_CENTER,
        Role.ANALYST,
    }
)


def permissions_for(role: Role | str) -> frozenset[str]:
    """Return the permission set granted by a role (empty for unknown roles)."""
    try:
        return ROLE_PERMISSIONS[Role(role)]
    except (ValueError, KeyError):
        return frozenset()


def is_staff(role: Role | str) -> bool:
    try:
        return Role(role) in STAFF_ROLES
    except ValueError:
        return False
