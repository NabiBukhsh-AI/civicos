# CivicOS

**An AI-native, multi-tenant municipal operations platform.**

One deployment serves any number of municipalities — a town, a district
council, a municipal committee, a cantonment board. Each one configures its own
departments, complaint taxonomy, service targets, languages and branding
without a code change.

[![CI](https://github.com/NabiBukhsh-AI/civicos/actions/workflows/ci.yml/badge.svg)](https://github.com/NabiBukhsh-AI/civicos/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## What it does

A resident photographs an overflowing drain and taps *send*. Ninety seconds
later a supervisor in the right department has a categorised, prioritised,
geo-located job with a deadline attached — and the thirty-nine other people who
report the same drain get folded into one record instead of thirty-nine.

That loop is the product. Everything else supports it.

| Area | Capability |
|---|---|
| **Reporting** | Web, mobile, WhatsApp, SMS, helpline, walk-in, field inspection, sensor and API intake. Anonymous reporting supported. Reference codes readable over the phone. |
| **AI triage** | Category, priority, severity, department and a neutral title proposed per report, with confidence. Low confidence routes to a human queue rather than guessing. |
| **Deduplication** | Escalating checks — fingerprint, geo + lexical, semantic, then model adjudication only for borderline pairs. Merged reports become corroboration, which raises priority. |
| **Vision** | Site photographs assessed into a structured report: condition, hazards, recommended actions, crew-hours estimate. EXIF location and capture time extracted as evidence. |
| **SLA engine** | Response and resolution targets resolved most-specific-first, working-hours aware, with at-risk warnings, breach recording and escalation up a configurable chain. |
| **Field operations** | Work orders, crews with skills and coverage areas, offline-tolerant progress updates, checklists, materials, sign-off, before/after verification. |
| **Assets** | Registry with inspections, condition history, QR codes for scan-to-report, and automatic preventive work orders when an inspection falls due. |
| **Citizen services** | Permits, licences, NOCs and certificates with configurable forms, document requirements and approval steps. |
| **Engagement** | Announcements, geo-targeted emergency alerts, surveys, feedback with sentiment, multilingual notifications over SMS/WhatsApp/email/push/in-app. |
| **Transparency** | Public portal, GeoJSON open-data feed, published budgets and development projects, reference-code tracking with no account. |
| **Knowledge** | Document corpus with structure-aware chunking, hybrid retrieval and an assistant that cites its sources — and says so when the corpus does not answer the question. |
| **Analytics** | Dashboards, daily trend, hotspot detection, per-department scorecards, SLA compliance, AI spend, streaming CSV export, and generated executive briefings. |

---

## Design principles

These are the decisions that shaped the codebase; they are worth reading before
changing it.

**The report is saved before anything clever happens to it.** Moderation, AI
triage, duplicate detection and routing all run against an already-persisted
record. A resident's complaint is never lost because a model timed out.

**AI is advisory, and always attributable.** What the model proposed lives in
`ai_*` columns; what the municipality decided lives in the operational columns.
A supervisor can always see that the model said "Sanitation / high" and that a
human moved it. Every call is metered into a ledger with its cost.

**The platform degrades, it does not fail.** With no AI provider configured,
triage falls back to keyword rules, embeddings to a hashing trick, and the
assistant to an honest "not configured". Every feature keeps working, with less
intelligence. Exhausting the AI budget degrades the same way rather than
returning errors. A town can run this before it has procured anything.

**Tenant isolation is structural.** Every tenant-owned table carries
`tenant_id`, and all access goes through a repository bound to one municipality
at construction time. A token's tenant claim beats a request header, so a
crafted header cannot move an authenticated caller sideways.

**Municipal records are archived, never deleted.** Soft deletes throughout, an
append-only issue timeline, and an audit log capturing who changed what, when
and from what value.

**Portable by default.** SQLite runs the entire platform, including retrieval.
PostgreSQL with pgvector is the production target and is detected
automatically. There is no step where a town needs a GIS department, a vector
database or a Kubernetes cluster to get started.

---

## Quick start

```bash
git clone https://github.com/NabiBukhsh-AI/civicos.git
cd civicos

python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

cp .env.example .env          # works as-is; change the secret key before deploying

civicos db create-all --yes   # or: civicos db upgrade
civicos db seed               # demo municipality, taxonomy, SLAs, sample assets
civicos serve --reload
```

Open <http://localhost:8000/docs>. Send `X-Tenant: demo` with every request.

File a report — no account needed:

```bash
curl -X POST http://localhost:8000/api/v1/issues \
  -H 'Content-Type: application/json' -H 'X-Tenant: demo' \
  -d '{
    "description": "The drain outside the community centre has been overflowing for two weeks.",
    "location": {"latitude": 12.9716, "longitude": 77.5946},
    "address": "Near the community centre"
  }'
```

The response contains the reference code, the category and priority the system
chose, why it routed the report where it did, the SLA deadline it has committed
to, and any near-duplicates it found.

### With Docker

```bash
make docker-up     # PostgreSQL + pgvector, Redis, API, worker; migrated and seeded
```

### Enabling AI

The platform runs fully without it. To turn on model intelligence:

```bash
CIVICOS_AI__PROVIDER=anthropic
CIVICOS_AI__ANTHROPIC_API_KEY=sk-ant-...
```

`google` and `openai` are equally supported, and capabilities can be split
across vendors — chat on one, vision on another, embeddings on a third.

---

## Onboarding a municipality

```bash
civicos tenant create northtown "North Town Municipal Committee" \
    --timezone Asia/Karachi --language en --currency PKR

civicos tenant create-admin northtown --email admin@northtown.gov
civicos docs ingest northtown ./bylaws --visibility public
```

`tenant create` seeds a working default configuration: eight departments, a
sixteen-category complaint taxonomy with keyword hints, eleven SLA policies and
a nine-service catalogue. All of it is then editable through the admin API.

Teaching the classifier local vocabulary is a data change, not a deployment:

```bash
curl -X POST .../api/v1/admin/categories -H 'X-Tenant: northtown' \
  -d '{"slug": "drainage", "name": "Drainage",
       "keywords": ["nala", "gutter", "storm drain"],
       "ai_hints": "Local usage: residents say nala for an open drain."}'
```

---

## Architecture

```
                 ┌──────────────────────────────────────────────┐
  web / mobile   │  API layer      routers, dependencies,        │
  WhatsApp / SMS │                 tenant → auth → permission    │
  helpline       └───────────────────────┬──────────────────────┘
  sensors                                │
                 ┌─────────────────────── ▼ ────────────────────┐
                 │  Services       issue lifecycle, SLA engine,  │
                 │                 dedupe, routing, work orders, │
                 │                 notifications, analytics      │
                 └──────────┬─────────────────────┬─────────────┘
                            │                     │
        ┌─────────────────── ▼ ──────┐   ┌──────── ▼ ─────────────────┐
        │  AI layer                  │   │  Repositories              │
        │  provider-agnostic:        │   │  tenant-scoped queries      │
        │  Anthropic / Google /      │   │                             │
        │  OpenAI / offline rules    │   │  SQLAlchemy 2.0 async       │
        │  triage · vision · RAG     │   │  SQLite or PostgreSQL       │
        └────────────────────────────┘   └─────────────────────────────┘
```

| Layer | Location | Responsibility |
|---|---|---|
| `core/` | config, security, permissions, geo, i18n, clock, telemetry | Cross-cutting primitives. No domain knowledge. |
| `domain/` | 47 SQLAlchemy models | The data model and its vocabulary. |
| `repositories/` | tenant-bound query builders | Data access. Cannot be constructed without a tenant. |
| `services/` | issue, SLA, dedupe, routing, work order, auth, analytics… | Business rules and orchestration. |
| `ai/` | providers, prompts, schemas, RAG, usage ledger | Everything model-facing, behind one interface. |
| `api/` | v1 routers + dependencies | HTTP contracts only. |
| `integrations/` | storage, notifications, EXIF | Outside world. |

Full detail: [docs/architecture.md](docs/architecture.md).

---

## Configuration

Every setting is an environment variable, so one image serves every
environment. Nested groups use a double underscore:

```bash
CIVICOS_DATABASE__URL=postgresql+asyncpg://user:pass@host/civicos
CIVICOS_AI__PROVIDER=anthropic
CIVICOS_RAG__TOP_K=8
CIVICOS_TENANCY__RESOLUTION_ORDER=jwt,subdomain,header
```

See [.env.example](.env.example) for the annotated set. Production refuses to
start with `debug` on, a wildcard CORS origin, or a short secret key.

---

## Operations

```bash
civicos ops sla-sweep            # re-evaluate deadlines, record breaches, escalate
civicos ops rollup               # nightly analytics aggregation
civicos ops retry-notifications  # re-attempt failed deliveries
civicos check                    # validate config and dependencies pre-deploy
```

Observability is built in: `/health/live` and `/health/ready` are separate so a
database blip does not restart a healthy container; `/metrics` exposes
Prometheus counters for intake volume, transitions, SLA breaches, AI tokens and
cost; logs are structured JSON with request id, tenant and actor on every line,
and credentials and national ID numbers redacted before they reach the sink.

---

## Development

```bash
make dev-install
make test          # 133 tests, no network, no API key, no containers
make lint
make check         # everything CI runs
```

The suite runs against in-memory SQLite with the offline AI provider. If tests
needed a container to run, contributors would stop running them.

---

## Security and privacy

- Phone numbers, national ID numbers and email addresses are stripped from
  report text before storage, publication or transmission to any model; the
  verbatim original is retained on the case file.
- Anonymous reporting is genuinely anonymous — marking a report anonymous drops
  the contact fields rather than merely hiding them.
- Refresh tokens rotate on use; a replayed token invalidates the session.
- Passwords are bcrypt-hashed with SHA-256 pre-hashing so long passphrases are
  not silently truncated. API keys are stored only as HMAC digests.
- Sign-in responses are identical whether or not an account exists.
- Retrieval is permission-aware: an internal SOP cannot surface in a
  citizen-facing answer.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

---

## Project history

CivicOS began as **Municipal AI Assistant**, a small FastAPI service with two
endpoints: a PDF chatbot over a single on-disk FAISS index, and an image
analyser that returned a paragraph of prose. This is a ground-up rewrite that
keeps the original insight — that a municipality's own documents and its
residents' photographs are the two most useful data sources it has — and builds
an operations platform around it.

What became of the original two features:

| Then | Now |
|---|---|
| One global FAISS index on disk | Tenant-scoped, permission-aware corpus with hybrid retrieval and pgvector/portable backends |
| Prose answers, no sources | Validated structured answers with reconciled citations and an explicit "not in the documents" |
| Image → paragraph | Image → structured assessment, EXIF evidence, location consistency check, before/after comparison |
| Single tenant, hard-coded | Any number of municipalities, each self-configured |
| One vendor | Provider-agnostic with an offline fallback |

---

## Documentation

- [Architecture](docs/architecture.md) — layers, data model, tenancy, extension points
- [AI design](docs/ai.md) — providers, prompts, structured output, guardrails, cost
- [Deployment](docs/deployment.md) — production checklist, scaling, backups
- [API guide](docs/api.md) — conventions, auth, errors, pagination
- [Contributing](CONTRIBUTING.md)

## License

MIT — see [LICENSE](LICENSE).
