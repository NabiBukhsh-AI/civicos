"""ASGI middleware: request context, access logs, metrics, headers, throttling."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from civicos.core import context
from civicos.core.config import Settings, get_settings
from civicos.core.i18n import normalise_language
from civicos.core.rate_limit import get_rate_limiter
from civicos.core.telemetry import HTTP_LATENCY, HTTP_REQUESTS, normalise_path

logger = structlog.get_logger(__name__)

RequestHandler = Callable[[Request], Awaitable[Response]]

REQUEST_ID_HEADER = "X-Request-ID"
_EXEMPT_PATHS = frozenset({"/health", "/health/live", "/health/ready", "/metrics"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind context vars, log and time every request."""

    def __init__(self, app: ASGIApp, settings: Settings | None = None) -> None:
        super().__init__(app)
        self.settings = settings or get_settings()

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or context.new_request_id()
        request.state.request_id = request_id
        context.set_request_id(request_id)
        context.set_actor(None)
        context.set_tenant(None, None)

        language = normalise_language(request.headers.get("Accept-Language"))
        context.set_language(language)
        request.state.language = language

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration = time.perf_counter() - started
            route = request.scope.get("route")
            path = normalise_path(request.url.path, getattr(route, "path", None))
            if path not in _EXEMPT_PATHS:
                HTTP_REQUESTS.labels(
                    method=request.method, path=path, status=str(status_code)
                ).inc()
                HTTP_LATENCY.labels(method=request.method, path=path).observe(duration)
                log = logger.bind(
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=round(duration * 1000, 2),
                    client=request.client.host if request.client else None,
                )
                if duration * 1000 > self.settings.observability.slow_request_ms:
                    log.warning("http_request_slow")
                else:
                    log.info("http_request")
            # Each request runs in its own contextvars copy, so clearing here is
            # belt-and-braces rather than strictly required.
            context.set_request_id(None)
            structlog.contextvars.clear_contextvars()


class ResponseHeadersMiddleware(BaseHTTPMiddleware):
    """Echo the request id and apply conservative security headers."""

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        response = await call_next(request)
        response.headers.setdefault(REQUEST_ID_HEADER, getattr(request.state, "request_id", ""))
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(self), microphone=(), camera=(self)"
        )
        if get_settings().is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class TenantSlugMiddleware(BaseHTTPMiddleware):
    """Work out *which* municipality a request is for, without touching the DB.

    The resolved slug is stashed on ``request.state``; the API dependency layer
    turns it into a validated tenant row. Keeping the DB out of middleware means
    health checks stay cheap and the ordering of dependencies stays explicit.
    """

    def __init__(self, app: ASGIApp, settings: Settings | None = None) -> None:
        super().__init__(app)
        self.settings = settings or get_settings()

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        request.state.tenant_slug_hint = self._resolve(request)
        return await call_next(request)

    def _resolve(self, request: Request) -> str | None:
        tenancy = self.settings.tenancy
        for strategy in tenancy.resolution_order:
            if strategy == "header":
                value = request.headers.get(tenancy.header_name)
                if value:
                    return value.strip().lower()
            elif strategy == "subdomain":
                host = (request.headers.get("host") or "").split(":")[0]
                if tenancy.base_domain and host.endswith(tenancy.base_domain):
                    sub = host[: -len(tenancy.base_domain)].strip(".")
                    if sub and sub not in {"www", "api"}:
                        return sub.lower()
            elif strategy == "query":
                value = request.query_params.get("tenant")
                if value:
                    return value.strip().lower()
            elif strategy == "default":
                return tenancy.default_tenant_slug
            # "jwt" is handled by the auth dependency, which has the decoded token.
        return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Coarse per-client throttle applied before routing."""

    def __init__(self, app: ASGIApp, settings: Settings | None = None) -> None:
        super().__init__(app)
        self.settings = settings or get_settings()

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        limits = self.settings.rate_limit
        if not limits.enabled or request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        identity = self._identity(request)
        limit = (
            limits.default_per_minute
            if request.headers.get("Authorization")
            else limits.anonymous_per_minute
        )
        limiter = await get_rate_limiter()
        result = await limiter.hit(f"http:{identity}", limit=limit, window_seconds=60)
        if not result.allowed:
            logger.info("rate_limited", identity=identity, path=request.url.path)
            return JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": "Too many requests. Please slow down.",
                        "details": {"retry_after_seconds": result.retry_after_seconds},
                        "request_id": getattr(request.state, "request_id", None),
                    }
                },
                headers={"Retry-After": str(result.retry_after_seconds)},
            )
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        return response

    @staticmethod
    def _identity(request: Request) -> str:
        auth = request.headers.get("Authorization")
        if auth:
            # Hash-free but stable: the token tail is enough to separate clients.
            return f"tok:{auth[-24:]}"
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return f"ip:{forwarded.split(',')[0].strip()}"
        return f"ip:{request.client.host if request.client else 'unknown'}"


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject oversized uploads early, before they are buffered."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        super().__init__(app)
        self.max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        content_length = request.headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "payload_too_large",
                        "message": (
                            f"Request body exceeds the {self.max_bytes // (1024 * 1024)} MB limit."
                        ),
                        "details": {"max_bytes": self.max_bytes},
                        "request_id": getattr(request.state, "request_id", None),
                    }
                },
            )
        return await call_next(request)
