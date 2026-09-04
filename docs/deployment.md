# Deployment

## Before going live

- [ ] `CIVICOS_SECURITY__SECRET_KEY` set to 48+ random bytes, held in a secret
      manager. Rotating it invalidates every session and API key.
- [ ] `CIVICOS_ENVIRONMENT=production`. The app refuses to start with debug on,
      a wildcard CORS origin, or a short secret key.
- [ ] `CIVICOS_CORS__ALLOW_ORIGINS` listing explicit origins.
- [ ] PostgreSQL, not SQLite. Enable `vector` if you want ANN retrieval.
- [ ] S3-compatible storage if you run more than one replica — local disk is
      not shared between them.
- [ ] `CIVICOS_OBSERVABILITY__LOG_FORMAT=json`.
- [ ] TLS terminated upstream; uvicorn run with `--proxy-headers`.
- [ ] Migrations applied as a job (`civicos db upgrade`), never on container start.
- [ ] Scheduled jobs running — the SLA sweep at least every five minutes.
- [ ] Backups configured, and a restore rehearsed.
- [ ] `civicos check` passing against the production configuration.

## Sizing

| Deployment | Setup |
|---|---|
| Pilot / evaluation | One container, SQLite, local storage, offline AI. Genuinely enough to run a town's intake. |
| One municipality | 2 API replicas (1 vCPU / 1 GB each), PostgreSQL (2 vCPU / 4 GB), object storage, 1 worker. |
| Several municipalities | 3+ replicas behind a load balancer, Redis for shared rate limits and queueing, PostgreSQL with a read replica serving the public portal and analytics. |

## Database

```bash
civicos db upgrade            # apply migrations
civicos db upgrade <revision> # target a specific revision
civicos db downgrade <revision>
```

CI runs `alembic upgrade head`, then `alembic check` — an empty autogenerate
diff proves the migration and the models agree — then `alembic downgrade base`.
Keep it that way: a migration you cannot roll back is a migration you cannot
deploy on a Friday.

On PostgreSQL, enable the extensions once:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

For a large corpus, add an ANN index by hand. Its parameters depend on corpus
size, and `migrations/env.py` excludes `*_ann` indexes from autogenerate so it
will not be dropped underneath you:

```sql
CREATE INDEX ix_document_chunks_embedding_ann
  ON document_chunks USING hnsw (embedding vector_cosine_ops);
```

## Scheduled jobs

| Job | Cadence | Command |
|---|---|---|
| SLA sweep and escalation | every 5 min | `civicos ops sla-sweep` |
| Notification retries | every 15 min | `civicos ops retry-notifications` |
| Analytics rollup | nightly | `civicos ops rollup` |
| Preventive inspections | daily | `run_asset_inspection_sweep` |

The compose stack runs these in a `worker` service; in Kubernetes use CronJobs.
Every job is idempotent, so an overlapping run is harmless.

## Observability

- **`/health/live`** — process only. Use for the liveness probe: a database
  blip must not restart a healthy container.
- **`/health/ready`** — database, storage and AI providers. Use for readiness.
  Only the database is load-bearing; AI and storage degrade rather than fail.
- **`/metrics`** — Prometheus. Intake volume by category and channel, status
  transitions, SLA breaches by stage and department, AI tokens and cost by
  provider and capability, notification outcomes, HTTP latency histograms.
- **Logs** — JSON with `request_id`, `tenant`, `actor_id` and `actor_role` on
  every line. Credentials and national ID numbers are redacted before the sink.

Worth alerting on: readiness failing, SLA breach rate rising, notification
failure rate, AI budget exhaustion, p95 latency.

## Backups

Three things must be backed up together, or a restore produces a system whose
records reference files that no longer exist:

1. the database,
2. the object store — evidence photographs, documents, certificates,
3. the secret key, without which refresh tokens and API keys cannot be verified.

Rehearse a restore into staging. Municipal records carry statutory retention
periods, and an untested backup does not meet them.

## Onboarding a municipality

```bash
civicos tenant create northtown "North Town Municipal Committee" \
    --timezone Asia/Karachi --language en --currency PKR

civicos tenant create-admin northtown --email admin@northtown.gov
civicos docs ingest northtown ./bylaws --visibility public
```

To serve each municipality on its own subdomain, set
`CIVICOS_TENANCY__BASE_DOMAIN` and include `subdomain` in the resolution order.

## Upgrades

1. Read the release notes for migration steps.
2. Apply migrations as a job, before rolling the new image.
3. Roll replicas one at a time — the API is backward-compatible within a major
   version.
4. Watch `/health/ready` and the SLA breach counter.

Rolling back is `civicos db downgrade` plus the previous image, which is why
every migration has a working downgrade.

## Data residency

If a municipality may not send data to a hosted model, either set
`CIVICOS_AI__PROVIDER=mock` — everything keeps working on rules — or point the
OpenAI-compatible provider at a self-hosted gateway:

```bash
CIVICOS_AI__PROVIDER=openai
CIVICOS_AI__OPENAI_API_KEY=local
# base_url is set when registering the provider; see docs/ai.md
```
