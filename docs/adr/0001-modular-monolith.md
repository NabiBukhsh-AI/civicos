# ADR 0001: A modular monolith, not microservices

**Status:** accepted · **Date:** 2026-09-04

## Context

CivicOS serves municipalities. A town of 300,000 residents files a few thousand
reports a month — roughly one every twenty minutes at peak. The operators are
usually a small IT department, sometimes a single administrator, occasionally a
contractor who visits twice a year.

## Decision

Build a modular monolith: one deployable unit, strict internal layering,
enforced import direction.

## Rationale

The workload does not require distribution. What it does require is that a
municipality can run, upgrade, back up and restore the system without a
platform team. Every service boundary added would be a network call to debug, a
deployment to coordinate and a failure mode to explain — paid for continuously,
in exchange for scaling headroom that will not be needed.

Layering gives most of the benefit that service boundaries are usually reached
for. The AI layer knows nothing about HTTP; repositories know nothing about
services; providers know nothing about municipal rules. Those seams are where a
future split would happen, and they are already clean.

## Consequences

- One image, one migration path, one backup.
- Scaling is horizontal replicas plus a read replica, which covers the
  realistic range.
- A tenant needing physical isolation gets its own deployment of the same code.
- If one component ever becomes genuinely independent — document ingestion is
  the likeliest — the layering means extracting it is mechanical.
