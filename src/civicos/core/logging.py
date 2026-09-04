"""Structured logging.

Logs are emitted as JSON in every non-local environment so they can be shipped
straight into Loki/CloudWatch/ELK, and every line automatically carries the
request id, tenant and actor from :mod:`civicos.core.context`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from civicos.core.config import Settings, get_settings
from civicos.core.context import get_actor, get_request_id, get_tenant_slug


def _bind_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach ambient request context to every log line."""
    if request_id := get_request_id():
        event_dict.setdefault("request_id", request_id)
    if tenant := get_tenant_slug():
        event_dict.setdefault("tenant", tenant)
    actor = get_actor()
    if actor.is_authenticated:
        event_dict.setdefault("actor_id", str(actor.id))
        event_dict.setdefault("actor_role", actor.role)
    return event_dict


_SENSITIVE_KEYS = {
    "password",
    "new_password",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "secret_key",
    "api_key",
    "authorization",
    "cnic",
    "id_number",
}


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Never let credentials or national ID numbers reach the log sink."""
    for key in list(event_dict):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***redacted***"
    return event_dict


def configure_logging(settings: Settings | None = None) -> None:
    """Configure structlog + stdlib logging. Safe to call more than once."""
    settings = settings or get_settings()
    level = getattr(logging, settings.observability.log_level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _bind_context,
        _redact,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.observability.log_format == "console":
        renderer: Any = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Route through stdlib logging so uvicorn/SQLAlchemy records land in the
        # same stream and handlers, and `add_logger_name` has a name to read.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    # Uvicorn duplicates access logs that our middleware already emits.
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    for noisy in ("httpx", "httpcore", "asyncio", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
