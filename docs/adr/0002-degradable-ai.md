# ADR 0002: The platform must work with AI disabled

**Status:** accepted · **Date:** 2026-09-04

## Context

Municipalities procure slowly. A town may want the reporting and workflow
system a year before it can approve an AI vendor, or may be unable to send
resident data to a hosted model at all. Budgets are fixed and annual; an
unbounded per-call cost is not something a public body can sign.

## Decision

AI is an enhancement layer with a deterministic floor underneath it. Every
capability has a rule-based fallback that produces a valid result, and the
default configuration uses it.

## Rationale

Making AI mandatory would put the platform behind a procurement decision it
does not need. It would also mean that a vendor outage, an expired key or an
exhausted budget stops a municipality accepting complaints — an unacceptable
failure mode for a service residents rely on.

The offline provider is therefore built as a real component: keyword rules over
a curated taxonomy for triage, hashing-trick embeddings for similarity, honest
messaging for the assistant. It is exercised by the entire test suite, so it
cannot rot.

## Consequences

- Tests run with no network, no key and no containers, so contributors run them.
- Exhausting the AI budget degrades rather than errors.
- A provider outage degrades rather than errors.
- Every AI capability costs slightly more to build, because the fallback ships
  in the same change.
- A town can adopt the platform first and AI later, with no migration.
