"""Application factory and ASGI entry point.

Run with::

    uvicorn civicos.main:app --reload
    civicos serve            # equivalent, via the CLI
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import ORJSONResponse

from civicos import __version__
from civicos.api.router import api_router
from civicos.api.v1 import health
from civicos.core.config import Settings, get_settings
from civicos.core.errors import register_exception_handlers
from civicos.core.logging import configure_logging
from civicos.core.middleware import (
    MaxBodySizeMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    ResponseHeadersMiddleware,
    TenantSlugMiddleware,
)
from civicos.core.telemetry import set_build_info, setup_tracing

logger = structlog.get_logger(__name__)

DESCRIPTION = """
**CivicOS** is a multi-tenant municipal operations platform.

One deployment serves any number of municipalities. Every request is scoped to a
municipality (resolved from the JWT, the `X-Tenant` header or the subdomain),
and every tenant configures its own departments, complaint taxonomy, service
targets and languages.

### What it covers

* **Reporting** - citizens report problems from the web, a mobile app, WhatsApp,
  SMS or a helpline. AI triage proposes a category, priority and department;
  duplicate detection merges the forty reports a single burst main produces.
* **Operations** - SLA-tracked issue workflow, work orders and crew dispatch,
  an asset registry with inspections.
* **Services** - permits, licences and certificates with configurable forms and
  approval steps.
* **Engagement** - announcements, geo-targeted emergency alerts, surveys,
  multilingual notifications.
* **Transparency** - a public portal, an open-data feed, published budgets and
  development projects.
* **Assistance** - a document assistant that answers from the municipality's own
  bylaws and notices, with citations, and says so when it does not know.

### Notes for integrators

* Errors always use the envelope `{"error": {"code", "message", "details"}}`.
  Branch on `code`, never on prose.
* AI output is advisory. Model suggestions are stored separately from municipal
  decisions and are always attributable.
* The platform degrades rather than fails: with no AI provider configured,
  triage, search and deduplication fall back to deterministic rules.
"""

TAGS_METADATA = [
    {"name": "Public Portal", "description": "Unauthenticated, redacted, cacheable."},
    {"name": "Authentication", "description": "Password and phone-OTP sign-in, sessions, API keys."},
    {"name": "Issues", "description": "Citizen reports and their full lifecycle."},
    {"name": "AI Assistant", "description": "Grounded Q&A, vision analysis, triage preview, briefings."},
    {"name": "Knowledge Base", "description": "Document upload, indexing and semantic search."},
    {"name": "Work Orders", "description": "Field work: dispatch, progress, completion."},
    {"name": "Assets", "description": "Municipal asset registry and inspections."},
    {"name": "Citizen Services", "description": "Permits, licences and certificates."},
    {"name": "Engagement", "description": "Announcements, alerts, surveys, notifications."},
    {"name": "Analytics", "description": "Dashboards, hotspots, SLA compliance, exports."},
    {"name": "Administration", "description": "Municipality configuration and audit."},
    {"name": "Health", "description": "Liveness, readiness and metrics."},
]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start-up and shutdown.

    Deliberately does *not* create tables or seed data - schema changes are
    applied with Alembic, explicitly, by an operator who chose to.
    """
    settings = get_settings()
    configure_logging(settings)
    set_build_info(__version__)

    from civicos.db.session import ping  # noqa: PLC0415

    logger.info(
        "starting",
        version=__version__,
        environment=str(settings.environment),
        ai_provider=settings.ai.provider,
        database=settings.database.url.split("://")[0],
    )
    if not await ping():
        logger.error("database_unreachable_at_startup")

    yield

    from civicos.ai.registry import reset_providers  # noqa: PLC0415
    from civicos.core.rate_limit import reset_rate_limiter  # noqa: PLC0415
    from civicos.db.session import dispose_engine  # noqa: PLC0415

    await reset_providers()
    await reset_rate_limiter()
    await dispose_engine()
    logger.info("stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=f"{settings.project_name} API",
        description=DESCRIPTION,
        version=__version__,
        openapi_tags=TAGS_METADATA,
        default_response_class=ORJSONResponse,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
        root_path=settings.root_path,
        lifespan=lifespan,
        contact={"name": "CivicOS", "url": "https://github.com/NabiBukhsh-AI/civicos"},
        license_info={"name": "MIT", "url": "https://opensource.org/licenses/MIT"},
    )

    # Middleware runs bottom-up, so this list reads in reverse order of
    # execution: context is established first, compression applied last.
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allow_origins,
        allow_origin_regex=settings.cors.allow_origin_regex,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=settings.cors.allow_methods,
        allow_headers=settings.cors.allow_headers,
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining"],
    )
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.storage.max_upload_bytes)
    app.add_middleware(RateLimitMiddleware, settings=settings)
    app.add_middleware(TenantSlugMiddleware, settings=settings)
    app.add_middleware(ResponseHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware, settings=settings)

    register_exception_handlers(app)
    setup_tracing(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": settings.project_name,
            "tagline": settings.project_tagline,
            "version": __version__,
            "api": settings.api_v1_prefix,
            "docs": "/docs" if settings.docs_enabled else "disabled",
            "health": "/health",
        }

    return app


app = create_app()


def main() -> None:  # pragma: no cover - console entry point
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "civicos.main:app",
        host="0.0.0.0",  # noqa: S104 - containers bind all interfaces
        port=8000,
        reload=settings.environment.value == "local",
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
