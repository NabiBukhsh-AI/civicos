"""Liveness, readiness and metrics.

Split deliberately: ``/health/live`` answers "is the process up" and must never
touch a dependency, while ``/health/ready`` answers "can it serve traffic" and
does. Conflating them causes an orchestrator to restart a healthy container
because a database blipped.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Response, status
from fastapi.responses import PlainTextResponse

from civicos import __version__
from civicos.core.config import get_settings
from civicos.core.telemetry import render_metrics
from civicos.db.session import ping
from civicos.schemas.common import HealthStatus

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthStatus)
async def health() -> HealthStatus:
    """Overall status, safe to expose publicly."""
    settings = get_settings()
    return HealthStatus(
        status="healthy",
        version=__version__,
        environment=str(settings.environment),
        checks={"api": "ok"},
    )


@router.get("/health/live", response_model=dict)
async def liveness() -> dict[str, str]:
    """Process liveness. No dependencies are consulted."""
    return {"status": "alive"}


@router.get("/health/ready", response_model=HealthStatus)
async def readiness(response: Response) -> HealthStatus:
    """Dependency readiness: database, storage and AI providers."""
    from civicos.ai.registry import provider_health
    from civicos.integrations.storage import get_storage

    settings = get_settings()
    checks: dict[str, Any] = {}

    database_ok = await ping()
    checks["database"] = "ok" if database_ok else "unavailable"

    try:
        checks["storage"] = "ok" if await get_storage().health() else "unavailable"
    except Exception as exc:
        checks["storage"] = f"error: {exc}"

    try:
        checks["ai_providers"] = await provider_health()
    except Exception as exc:  # never let a provider probe fail the readiness check
        checks["ai_providers"] = {"error": str(exc)}

    # Only the database is load-bearing: the platform is designed to keep
    # accepting reports with AI and object storage degraded.
    ready = database_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthStatus(
        status="ready" if ready else "not_ready",
        version=__version__,
        environment=str(settings.environment),
        checks=checks,
    )


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    settings = get_settings()
    if not settings.observability.metrics_enabled:
        return PlainTextResponse("metrics disabled", status_code=status.HTTP_404_NOT_FOUND)
    return Response(
        content=render_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
