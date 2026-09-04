"""Prometheus metrics and optional OpenTelemetry tracing.

Metrics are deliberately opinionated around the questions a municipal CIO asks:
how many reports came in, how fast are we closing them, what is the AI costing,
and which upstream is failing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from civicos.core.config import get_settings

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import FastAPI

logger = structlog.get_logger(__name__)

REGISTRY = CollectorRegistry(auto_describe=True)

HTTP_REQUESTS = Counter(
    "civicos_http_requests_total",
    "HTTP requests processed",
    labelnames=("method", "path", "status"),
    registry=REGISTRY,
)

HTTP_LATENCY = Histogram(
    "civicos_http_request_duration_seconds",
    "HTTP request latency",
    labelnames=("method", "path"),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

ISSUES_CREATED = Counter(
    "civicos_issues_created_total",
    "Civic issues reported",
    labelnames=("tenant", "category", "channel"),
    registry=REGISTRY,
)

ISSUE_TRANSITIONS = Counter(
    "civicos_issue_transitions_total",
    "Issue status transitions",
    labelnames=("tenant", "from_status", "to_status"),
    registry=REGISTRY,
)

SLA_BREACHES = Counter(
    "civicos_sla_breaches_total",
    "SLA deadlines missed",
    labelnames=("tenant", "stage", "category"),
    registry=REGISTRY,
)

AI_REQUESTS = Counter(
    "civicos_ai_requests_total",
    "Calls made to an AI provider",
    labelnames=("provider", "capability", "outcome"),
    registry=REGISTRY,
)

AI_TOKENS = Counter(
    "civicos_ai_tokens_total",
    "Tokens consumed by AI calls",
    labelnames=("provider", "capability", "kind"),
    registry=REGISTRY,
)

AI_LATENCY = Histogram(
    "civicos_ai_request_duration_seconds",
    "AI provider latency",
    labelnames=("provider", "capability"),
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
    registry=REGISTRY,
)

NOTIFICATIONS_SENT = Counter(
    "civicos_notifications_total",
    "Outbound notifications",
    labelnames=("channel", "outcome"),
    registry=REGISTRY,
)

BACKGROUND_JOBS = Counter(
    "civicos_background_jobs_total",
    "Background jobs executed",
    labelnames=("job", "outcome"),
    registry=REGISTRY,
)

OPEN_ISSUES = Gauge(
    "civicos_open_issues",
    "Currently open issues",
    labelnames=("tenant",),
    registry=REGISTRY,
)

BUILD_INFO = Gauge(
    "civicos_build_info",
    "Build metadata",
    labelnames=("version", "environment"),
    registry=REGISTRY,
)


def render_metrics() -> bytes:
    return generate_latest(REGISTRY)


def record_ai_call(
    provider: str,
    capability: str,
    *,
    outcome: str,
    duration_seconds: float | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    AI_REQUESTS.labels(provider=provider, capability=capability, outcome=outcome).inc()
    if duration_seconds is not None:
        AI_LATENCY.labels(provider=provider, capability=capability).observe(duration_seconds)
    if input_tokens:
        AI_TOKENS.labels(provider=provider, capability=capability, kind="input").inc(input_tokens)
    if output_tokens:
        AI_TOKENS.labels(provider=provider, capability=capability, kind="output").inc(output_tokens)


def setup_tracing(app: FastAPI) -> None:
    """Wire OpenTelemetry if enabled and the optional extra is installed."""
    settings = get_settings()
    if not settings.observability.tracing_enabled:
        return
    try:  # pragma: no cover - optional dependency
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import (
            FastAPIInstrumentor,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": settings.project_name.lower(),
                "deployment.environment": str(settings.environment),
            }
        )
        provider = TracerProvider(resource=resource)
        if settings.observability.otlp_endpoint:
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.observability.otlp_endpoint))
            )
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health.*,metrics")
        logger.info("tracing_enabled", endpoint=settings.observability.otlp_endpoint)
    except ImportError:
        logger.warning("tracing_requested_but_otel_not_installed")


def set_build_info(version: str) -> None:
    BUILD_INFO.labels(version=version, environment=str(get_settings().environment)).set(1)


def normalise_path(raw_path: str, route_template: Any | None = None) -> str:
    """Collapse identifiers so metric cardinality stays bounded."""
    if route_template:
        return str(route_template)
    parts = raw_path.split("/")
    cleaned = [
        "{id}" if (len(part) > 20 and any(ch.isdigit() for ch in part)) else part for part in parts
    ]
    return "/".join(cleaned) or "/"
