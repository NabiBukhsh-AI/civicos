# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-09-04

Complete rewrite. The project moves from a single-tenant FastAPI service with
two endpoints to a multi-tenant municipal operations platform.

### Added

- **Multi-tenancy.** Any number of municipalities per deployment, resolved from
  the JWT, an `X-Tenant` header or a subdomain. Each configures its own
  departments, taxonomy, SLA targets, languages and branding.
- **Issue lifecycle.** Eleven-state workflow with an enforced transition table,
  append-only timeline, public and internal comments, corroboration counts,
  satisfaction ratings and escalation.
- **AI triage.** Category, priority, severity, department and title proposed per
  report with a confidence score; applied only above the tenant's threshold, and
  always recorded separately from the municipality's own decision.
- **Duplicate detection.** Escalating checks — fingerprint, geo plus lexical,
  semantic, then model adjudication for borderline pairs only.
- **SLA engine.** Most-specific-first policy resolution, working-hours aware,
  with at-risk warnings, breach recording and department escalation chains.
- **Work orders and crews.** Dispatch by skill, coverage area and capacity;
  offline-tolerant field updates; checklists, materials, sign-off.
- **Asset registry.** Inspections, condition history, QR scan-to-report, and
  automatic preventive work orders when an inspection falls due.
- **Citizen services.** Permits, licences, NOCs and certificates with
  configurable forms, document requirements and approval steps.
- **Engagement.** Announcements, geo-targeted emergency alerts, surveys,
  feedback with sentiment, and multi-channel notifications.
- **Transparency.** Public portal, GeoJSON open-data feed, published budgets and
  development projects, reference-code tracking without an account.
- **Knowledge base.** Structure-aware chunking, hybrid retrieval, permission-
  filtered by audience, with reconciled citations.
- **Analytics.** Dashboards, trends, hotspot detection, department scorecards,
  SLA compliance, AI spend, streaming CSV export, generated briefings.
- **Provider-agnostic AI.** Anthropic, Google and OpenAI adapters plus an
  offline heuristic provider with hashing-trick embeddings.
- **Operations.** CLI for onboarding, ingestion and scheduled jobs; Prometheus
  metrics; structured JSON logs with PII redaction; audit trail.
- 133 tests running with no network, no API key and no containers.

### Changed

- Google Gemini is now one of several supported providers rather than the only
  one, and the default configuration needs no provider at all.
- The single on-disk FAISS index is replaced by a tenant-scoped, permission-
  aware corpus with a pgvector or portable backend chosen automatically.
- Image analysis returns a validated structured assessment instead of prose,
  with EXIF location and capture time treated as evidence.
- Configuration moved from ad-hoc environment reads to nested, validated
  settings that refuse unsafe production values.

### Removed

- The `api/`, `models/` and `routes/` package layout, superseded by
  `src/civicos/` with enforced layering.
- `create_embeddings.py`, superseded by `civicos docs ingest`.
- The requirement for a Google API key to start the application.
