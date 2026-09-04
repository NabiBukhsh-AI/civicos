# Architecture

## Shape of the system

CivicOS is a modular monolith. One deployable unit, strict internal layering,
no distributed system where a municipality does not need one. A town of 300,000
people generates a few thousand reports a month; that is a workload one
container handles comfortably, and splitting it into services would buy
operational cost rather than capability.

```
  clients ─▶ API layer ─▶ services ─▶ repositories ─▶ database
                 │            │
                 │            └─▶ AI layer ─▶ providers (or offline rules)
                 │            └─▶ integrations (storage, notifications)
                 └─▶ core (config, auth, geo, i18n, telemetry)
```

### Layer rules

| Layer | May import | Must not |
|---|---|---|
| `core/` | stdlib, third-party | anything from `civicos.*` except `core` |
| `db/` | `core` | `domain`, `services`, `api` |
| `domain/` | `core`, `db` | `services`, `api`, `ai` |
| `repositories/` | `core`, `db`, `domain` | `services`, `api` |
| `ai/` | `core`, `domain` (for the usage ledger) | `api`, `services` |
| `services/` | everything below | `api` |
| `api/` | everything | — |

The one deliberate exception: `ai/usage.py` writes to the `AIUsage` table, so
the AI layer touches `domain`. Metering belongs next to the calls it meters.

---

## Multi-tenancy

A *tenant* is one municipality. Isolation rests on three mechanisms, in order
of importance:

**1. Structural.** Every tenant-owned table carries `tenant_id` with an index,
supplied by `TenantMixin`. `TenantRepository` takes the tenant id in its
constructor and applies it to every query it builds. There is no way to
construct a repository without naming a tenant.

**2. Resolution precedence.** `TenantSlugMiddleware` extracts a slug hint from
the header, subdomain or query string, but the `get_tenant` dependency prefers
the JWT's `tid` claim when one is present. A crafted `X-Tenant` header cannot
move an authenticated caller to another municipality — there is a test for
exactly this.

**3. Cross-tenant checks.** `get_optional_actor` rejects a token whose user
belongs to a different tenant than the one resolved, unless the user is a
platform administrator.

Configure resolution per deployment:

```bash
CIVICOS_TENANCY__RESOLUTION_ORDER=jwt,subdomain,header,default
CIVICOS_TENANCY__BASE_DOMAIN=civicos.gov     # northtown.civicos.gov
```

### Why one shared database

Schema-per-tenant and database-per-tenant both push operational cost onto the
municipality: N migrations, N backups, N connection pools. A shared schema with
enforced `tenant_id` scoping keeps operations proportional to the deployment
rather than to the number of towns. If a specific tenant later needs physical
isolation (a legal requirement, say), it gets its own deployment — the code is
identical.

---

## Data model

47 tables in nine bounded contexts.

### Tenancy
`municipalities` → `admin_units` (self-referential: zone → union council →
ward) → `departments` → `issue_categories` → `sla_policies`, plus
`representatives` for the "who represents me" directory.

Categories carry `keywords` and `ai_hints`, which feed both the rule-based
router and the triage prompt. Adding local vocabulary is an INSERT, not a
deployment.

### Identity
`users` (staff and residents in one table — a resident later hired as an
inspector keeps their reporting history), `user_sessions` (server-side so
"sign out everywhere" works), `api_keys`, `one_time_codes`.

Either an email or a phone number identifies a user, both unique per tenant.
Phone-first identity matters: many residents have no email address at all.

### Issues
`issues` is the centre of the system. It keeps three parallel views of the same
complaint, deliberately not merged:

- what the reporter said — `description`, `description_original`
- what the AI concluded — the `ai_*` columns
- what the municipality decided — `category_id`, `priority`, `status`

Around it: `issue_attachments` (with EXIF-derived location and capture time as
first-class columns), `issue_events` (append-only timeline), `issue_comments`
(public replies and internal notes), `issue_followers` (the "me too" signal).

### Work, assets, services
`work_orders` + `work_order_updates` + `material_usage` + `crews`;
`assets` + `asset_inspections`; `service_types` + `service_applications` +
`application_documents` + `application_events` + `service_schedules`.

Work orders are decoupled from issues on purpose: one complaint can need three
jobs, one job can close several complaints, and preventive maintenance produces
jobs with no complaint behind them.

### Knowledge, engagement, budget, operations
`documents` + `document_chunks` + `conversations` + `conversation_messages` +
`ai_usage`; `announcements` + `emergency_alerts` + `surveys` + `feedback`;
`budget_periods` + `budget_lines` + `development_projects` + `expenditures`;
`audit_logs` + `notifications` + `daily_metrics` + `saved_views` +
`webhook_endpoints`.

### Portability decisions

| Concern | Decision | Why |
|---|---|---|
| Primary keys | UUIDv4 | Offline field apps can create records and sync later; public URLs do not leak complaint volume. |
| Enums | VARCHAR via `StringEnum` | Municipal taxonomies change often; a native enum needs a migration per value. |
| JSON | `JSON` with a `JSONB` variant | Same code on SQLite and PostgreSQL. |
| Timestamps | `UTCDateTime` | Always returns UTC-aware, so SQLite and PostgreSQL behave identically in SLA maths. |
| Geospatial | `latitude`/`longitude` + geohash | No PostGIS requirement. Haversine and bounding boxes cover proximity, clustering and hotspots. |
| Vectors | `Vector` type | Real `vector(N)` on pgvector, JSON elsewhere, chosen automatically. |
| Deletion | Soft, everywhere | Municipal records are legal records. |

---

## Request lifecycle

Middleware is registered so it executes outermost-first:

1. `RequestContextMiddleware` — request id, language, context vars, access log, metrics
2. `ResponseHeadersMiddleware` — request id echo, security headers
3. `TenantSlugMiddleware` — slug hint, no database access
4. `RateLimitMiddleware` — coarse per-client throttle
5. `MaxBodySizeMiddleware` — reject oversized uploads before buffering
6. CORS, GZip

Then dependencies resolve in a fixed order: **tenant → actor → permission**.
`ai_rate_limit` adds a tighter throttle on endpoints that spend money.

---

## Intake pipeline

The ordering here is the most important design decision in the codebase.

```
validate ─▶ moderate ─▶ PERSIST ─▶ embed ─▶ AI triage ─▶ dedupe
                                                            │
                          notify ◀─ SLA ◀─ route ◀──────────┘
```

Everything after `PERSIST` is enrichment. If the model times out, the network
drops or the AI budget is exhausted, the report is already saved with a
reference code the resident can quote. Degrading is always preferable to
losing a complaint.

### Duplicate detection

Escalating cost, stopping at the first confident answer:

1. **Fingerprint** — byte-identical resubmission within 24 hours (a double tap on a flaky connection)
2. **Geo + lexical** — same category, within `duplicate_radius_meters`, overlapping wording
3. **Semantic** — embedding cosine similarity, with a threshold that adapts to the embedding backend
4. **Model adjudication** — only for the borderline top candidate, only when a real provider is configured

Thresholds are asymmetric on purpose. Merging wrongly buries a resident's
report silently; leaving a duplicate in the queue is merely untidy. So we merge
only on strong evidence and flag the middle band for a human.

### SLA engine

Policies resolve most-specific-first: (category, priority) → (category) →
(priority) → tenant default. Deadlines are computed once at triage and stored
on the issue, so editing a policy cannot silently re-date commitments already
made. Non-emergency clocks tick in working hours only; emergency clocks run
24/7.

---

## Extension points

| To add… | Do this | Not this |
|---|---|---|
| A complaint category | `POST /admin/categories` with keywords and AI hints | Edit an enum |
| A permit or licence | `POST` a `ServiceType` with a form schema | Write a new endpoint |
| An AI vendor | Implement `LLMProvider`, `register_provider(...)` | Touch any service |
| A notification channel | Implement `Channel`, add it in the dispatcher factory | Touch the notification service |
| A storage backend | Implement `StorageBackend` | Touch the attachment service |
| A scheduled job | Add an async function in `workers/jobs.py`, expose a CLI command | Add a framework |
| A language | Add a dict to `core/i18n.CATALOGUE` | Anything else |

---

## Scaling path

The platform is designed to grow without rewriting:

| Load | Change |
|---|---|
| Pilot | SQLite, local storage, offline AI. One process. |
| One town in production | PostgreSQL, S3-compatible storage, a real AI provider, a worker container. |
| Several towns | Add replicas behind a load balancer; add Redis so rate limits and the job queue are shared. |
| Large corpus | Enable pgvector and add an HNSW index (`ix_document_chunks_embedding_ann`, excluded from autogenerate). |
| Multi-year history | The nightly `daily_metrics` rollup already backs the dashboards; archive old issues without losing the series. |

Read replicas are the natural next step: the public portal and analytics are
read-only and cacheable, and would move first.

---

## What is deliberately absent

- **A frontend.** The API is the product; a municipality's web and mobile
  clients differ. OpenAPI at `/openapi.json` generates typed clients.
- **A workflow engine.** Municipal workflows are simple state machines and
  benefit from being readable in one file (`domain/enums.py`).
- **A message broker.** Jobs are idempotent async functions. Cron is enough
  until it is not, and then ARQ slots in without changing them.
- **PostGIS.** Adds an operational dependency for capability the platform does
  not currently need. The columns are ready if it does.
