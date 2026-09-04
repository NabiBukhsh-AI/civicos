# Security policy

CivicOS holds resident complaints, contact details, evidence photographs with
GPS coordinates, and permit applications. A breach exposes real people who
often had no choice about using the service. Please treat findings accordingly.

## Reporting a vulnerability

Do **not** open a public issue. Use GitHub's private vulnerability reporting on
this repository (Security → Report a vulnerability), or contact the maintainer
through their GitHub profile.

Please include: what the issue is, how to reproduce it, what an attacker gains,
and the version or commit you tested.

Expect an acknowledgement within a few days and an assessment within two weeks.
Public disclosure is coordinated after a fix is available. Reporters are
credited unless they prefer otherwise.

## Scope

In scope: authentication and session handling, tenant isolation, authorisation,
injection, resident-data exposure, AI prompt injection with a concrete impact,
storage access control, and the default configuration.

Out of scope: findings that require an already-compromised administrator
account, denial of service by volume, missing headers with no demonstrated
impact, and anything in a deployment's own infrastructure rather than this
codebase.

## What the platform does

**Tenant isolation** — every tenant-owned table carries `tenant_id`; all access
goes through a repository bound to one municipality at construction. A token's
tenant claim beats a request header, so a crafted header cannot move an
authenticated caller sideways. There is a test asserting exactly this.

**Authentication** — bcrypt with SHA-256 pre-hashing so passphrases over 72
bytes are not silently truncated; server-side sessions enabling revocation;
refresh-token rotation, so a replayed token is detected; lockout after repeated
failures; identical responses whether or not an account exists.

**API keys** — stored only as an HMAC-SHA256 digest keyed by the app secret,
looked up by a non-secret prefix, compared in constant time, shown in plaintext
exactly once.

**Resident data** — phone numbers, national ID numbers and email addresses are
stripped from report text before storage, publication or transmission to any
model; the verbatim original is kept on the case file. Anonymous reporting
drops contact fields rather than hiding them. The public feed and open data
exclude reporter identity and free-text descriptions entirely.

**Uploads** — content type verified against the file's own magic numbers,
size-capped before buffering, stored under tenant-prefixed keys, with path
traversal refused by the local backend.

**AI** — prompts carry explicit guardrails; retrieval is permission-filtered so
an internal document cannot surface in a citizen answer; citations are
reconciled against what was actually retrieved, so a fabricated source cannot
be published; output is schema-validated before it is trusted, with a
deterministic fallback when it fails.

**Audit** — every consequential action is recorded with actor, request id, IP,
before/after values and outcome. Credentials and hashes are never written to
the audit trail or the logs.

**Production guards** — the application refuses to start in production with
debug enabled, a wildcard CORS origin, or a secret key under 32 characters.

## Deploying safely

The defaults are safe for evaluation, not for production. At minimum:

- Set a strong `CIVICOS_SECURITY__SECRET_KEY` from a secret manager.
- List explicit CORS origins.
- Terminate TLS and run behind a proxy with `--proxy-headers`.
- Use PostgreSQL and object storage with least-privilege credentials.
- Keep `/docs` disabled on an internet-facing production deployment if your
  threat model calls for it (`CIVICOS_DOCS_ENABLED=false`).
- Run `civicos check` before cutover.

See [docs/deployment.md](docs/deployment.md) for the full checklist.
