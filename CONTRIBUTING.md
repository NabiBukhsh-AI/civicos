# Contributing

Thanks for helping. CivicOS is used by public bodies, so the bar is a little
higher than usual in two specific places: **anything touching resident data**
and **anything touching the AI layer**. Everything else is ordinary.

## Setup

```bash
make dev-install
make seed
make test
```

The suite runs against in-memory SQLite with the offline AI provider. No
network, no API key, no containers. If a change makes tests require any of
those, that is a design problem, not a test problem.

## Before opening a pull request

```bash
make check     # lint, types, tests
```

## Layering

Imports must flow one way. `core` depends on nothing internal; `api` may depend
on everything. In particular:

- Business rules live in `services/`, never in a router.
- Tenant-scoped queries go through a repository, never raw session queries in a
  service or router.
- Prompts live in `ai/prompts.py`, never inline at a call site.
- Providers own transport only — no prompts, no persistence, no business rules.

## Rules that are not negotiable

**Tenant scoping.** Any query against a tenant-owned table goes through
`TenantRepository` or carries an explicit `tenant_id` filter. A PR that adds an
unscoped query will be asked to change.

**Resident data.** Report text passes through `redact_pii` before it is stored,
published or sent to a model. Do not add a path that bypasses it. The
deterministic redactor is applied over model output too — a regex match is
never overridden by model judgement.

**AI is advisory.** Model output goes into `ai_*` columns and is applied only
above the tenant's confidence threshold. Do not let a model write directly into
an operational column.

**Offline must work.** Every AI capability needs a deterministic fallback that
produces a valid result. Add it in the same PR, and test it.

**Records are archived, not deleted.** Use soft deletion. Consequential changes
get an audit entry and, for issues, a timeline event.

**Errors are typed.** Raise a `CivicOSError` subclass with a stable `code` and
actionable `details`. Do not raise bare `HTTPException`.

## Adding things

| Adding | Where |
|---|---|
| Endpoint | `api/v1/<area>.py`, schemas in `schemas/`, logic in `services/` |
| Model | `domain/<context>.py`, re-export in `domain/__init__.py`, generate a migration |
| AI capability | Schema in `ai/schemas.py`, prompt in `ai/prompts.py`, module in `ai/`, offline path in `providers/heuristic.py` |
| Provider | `ai/providers/<vendor>.py` implementing `LLMProvider`, register in `registry.py` |
| Notification channel | `integrations/notifications/`, wire into `get_dispatcher` |
| Scheduled job | Async function in `workers/jobs.py`, CLI command in `cli.py` |
| Language | A dict in `core/i18n.CATALOGUE` |

## Migrations

```bash
make migration m="add asset warranty fields"
```

Review the generated file — autogenerate is a starting point, not an answer.
Check that the downgrade actually works, and confirm it applies on both SQLite
and PostgreSQL.

## Tests

Write the test that would have caught the bug. Name it after the behaviour, not
the function:

```python
def test_illegal_transition_is_refused(): ...
def test_tenant_scoping_does_not_leak_records(): ...
def test_anonymous_flag_strips_contact_details(): ...
```

Assert on properties rather than model prose. `assert result.category_slug in
valid_slugs` is a test; `assert result.summary == "..."` is a hostage.

## Style

Ruff for linting and formatting, 100-column lines, type hints on public
functions.

Comments explain *why*, not *what*. The codebase's convention is to record the
reasoning that would otherwise be lost:

```python
# Merging wrongly buries a resident's report silently, while a duplicate in the
# queue is merely untidy - so the thresholds are asymmetric on purpose.
```

Docstrings on a module say what it is for and what decision shaped it.

## Reviewing

A useful review of this codebase asks:

- Is this tenant-scoped?
- Does it work with AI disabled?
- What happens when the model returns nonsense?
- Is resident data redacted on every path it can take?
- Does a state change leave an audit trail?
- Will this migration roll back?

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
