# API guide

Base path `/api/v1`. Interactive reference at `/docs`, machine-readable schema
at `/openapi.json` — use it to generate a typed client rather than hand-writing
one.

## Choosing a tenant

Every request is scoped to a municipality. Send the header:

```http
X-Tenant: northtown
```

An authenticated request does not need it — the token carries the tenant, and
the token wins if both are present.

## Authentication

Two flows, because municipal reality needs both.

**Password** — staff:

```bash
curl -X POST $BASE/auth/login -H "X-Tenant: northtown" \
  -d '{"identifier": "clerk@northtown.gov", "password": "..."}'
```

**Phone OTP** — residents, many of whom have no email address:

```bash
curl -X POST $BASE/auth/otp/request -H "X-Tenant: northtown" \
  -d '{"target": "03001234567"}'

curl -X POST $BASE/auth/otp/verify -H "X-Tenant: northtown" \
  -d '{"target": "03001234567", "code": "482913", "full_name": "A Resident"}'
```

Both return `{access_token, refresh_token, expires_in}`. Send
`Authorization: Bearer <access_token>`.

Refresh tokens **rotate**: each refresh revokes the token it consumed.
Replaying an old refresh token fails, which is how a stolen token is contained.

**Machine clients** use `X-API-Key` instead. Mint one with
`civicos tenant api-key <slug>`; the plaintext is shown once and never again.

## Errors

Always the same envelope. Branch on `code`, never on prose:

```json
{
  "error": {
    "code": "invalid_transition",
    "message": "Cannot move a report from 'submitted' to 'closed'.",
    "details": {
      "from": "submitted",
      "to": "closed",
      "allowed": ["triaged", "acknowledged", "assigned", "rejected", "duplicate"]
    },
    "request_id": "7f3c1a9e..."
  }
}
```

`details` is designed to be actionable — an invalid transition tells you which
transitions *are* possible, a validation failure names the offending fields, a
rate limit gives `retry_after_seconds`.

| Status | Common codes |
|---|---|
| 400 | `tenant_not_resolved` |
| 401 | `authentication_required`, `token_expired`, `bad_credentials`, `otp_invalid` |
| 403 | `permission_denied`, `tenant_mismatch` |
| 404 | `not_found`, `issue_not_found`, `tenant_not_found` |
| 409 | `conflict`, `invalid_transition`, `document_duplicate`, `already_responded` |
| 413 | `payload_too_large` |
| 415 | `unsupported_media_type` |
| 422 | `validation_error`, `weak_password`, `incomplete_application` |
| 429 | `rate_limited`, `ai_budget_exceeded` |
| 502 | `ai_provider_error`, `external_service_error` |
| 503 | `configuration_error` |

Quote `request_id` when reporting a problem: it appears on every log line for
that request and in the `X-Request-ID` response header.

## Pagination

```
GET /issues?page=2&page_size=50
```

```json
{
  "items": [],
  "meta": {
    "page": 2, "page_size": 50, "total": 412,
    "total_pages": 9, "has_next": true, "has_previous": true
  }
}
```

`page_size` is capped at 200. Sorting is allow-listed: a `sort_by` outside the
permitted set falls back to `created_at` rather than erroring.

## Request bodies

Unknown fields are **rejected**, not ignored. Silently dropping a field is how
a client ends up believing it set a priority that never arrived.

## Reporting an issue

The one endpoint worth reading in full. Anonymous submission is allowed by
default — the barrier to reporting a broken streetlight should be near zero.

```bash
curl -X POST $BASE/issues \
  -H "X-Tenant: northtown" -H "Content-Type: application/json" -d '{
    "description": "The drain outside the community centre has been overflowing for two weeks.",
    "location": {"latitude": 12.9716, "longitude": 77.5946, "accuracy_meters": 12},
    "address": "Near the community centre",
    "channel": "mobile",
    "reporter_name": "A Resident",
    "reporter_phone": "03001234567",
    "contact_consent": true
  }'
```

The response says what the system did, and why:

```json
{
  "issue": {
    "reference": "NOR-7K2QF4",
    "status": "assigned",
    "category": {"slug": "sewerage", "name": "Sewerage overflow / blockage"},
    "priority": "high",
    "sla": {"resolution_due_at": "2026-09-07T11:00:00Z", "resolution_state": "on_track"},
    "ai": {"confidence": 0.88, "summary": "...", "model": "claude-opus-5"}
  },
  "created": true,
  "merged_into": null,
  "duplicate_candidates": [
    {"reference": "NOR-3P8XZ1", "distance_meters": 42.0, "score": 0.61,
     "reason": "42 m away, lexical 0.55, semantic 0.71"}
  ],
  "ai_applied": true,
  "routing_reason": "located in Ward 3; category 'sewerage' is owned by Water & Sewerage",
  "warnings": ["Personal details were removed from the public description."]
}
```

A non-null `merged_into` means the report was folded into an existing one. The
reference is still valid and the reporter is still notified.

Before submitting, call `GET /issues/nearby` and show what is already reported
there. Preventing a duplicate at the source is far cheaper than merging it
afterwards.

## Tracking without an account

`GET /public/issues/{reference}/status` — the anonymous reporter's lifeline.
No authentication, and no personal data in the response.

## Permissions

Permissions are `resource:action` strings; a trailing `*` grants every action
on a resource. `GET /auth/me` returns the caller's resolved set, which is what
a client should use to decide what to render.

| Role | Can |
|---|---|
| `citizen` | report, track own, comment, confirm, rate, apply for services |
| `field_agent` | the above, plus update and complete assigned work orders |
| `call_center` | lodge and update reports on residents' behalf |
| `supervisor` | assign, transition, merge, dispatch, view analytics |
| `department_head` | the above, plus reject, escalate, decide applications, publish |
| `representative` | ward oversight, announcements, alerts, surveys — no operational writes |
| `analyst` | read-only across operations and analytics |
| `tenant_admin` | everything within one municipality |
| `super_admin` | across municipalities; onboarding |

## Rate limits

Per client per minute: 120 authenticated, 30 anonymous, 20 on AI endpoints
(they cost money). Responses carry `X-RateLimit-Limit` and
`X-RateLimit-Remaining`; a 429 carries `Retry-After`.

## Public and open data

Unauthenticated, redacted and cacheable (`Cache-Control: max-age=120`):
municipality profile, taxonomy, wards, representatives, published issues,
announcements, live alerts, service catalogue, collection schedules, budget,
projects, headline statistics, and `GET /public/open-data/issues.geojson` — a
GeoJSON feed that drops straight into QGIS or a mapping library.

## Webhooks

Register an endpoint to receive events rather than polling. Towns rarely run
only one system, and pushing into an existing ERP or WhatsApp gateway beats
asking it to poll.
