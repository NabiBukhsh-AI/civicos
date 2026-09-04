"""Typed application errors and the FastAPI handlers that render them.

Every error leaves the API in the same envelope so that clients (web, mobile,
IVR gateways) can branch on a stable machine code rather than parsing prose::

    {"error": {"code": "issue_not_found", "message": "...", "details": {...},
               "request_id": "..."}}
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


class CivicOSError(Exception):
    """Base class for every domain error raised inside CivicOS."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
                "request_id": request_id,
            }
        }


class NotFoundError(CivicOSError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource does not exist."


class ConflictError(CivicOSError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class ValidationError(CivicOSError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "The submitted payload is invalid."


class AuthenticationError(CivicOSError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_required"
    message = "Valid credentials are required."


class PermissionDeniedError(CivicOSError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class TenantResolutionError(CivicOSError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "tenant_not_resolved"
    message = "Could not determine which municipality this request belongs to."


class RateLimitedError(CivicOSError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please slow down."


class WorkflowError(CivicOSError):
    """Raised when a state transition is not allowed by the workflow."""

    status_code = status.HTTP_409_CONFLICT
    code = "invalid_transition"
    message = "That status change is not allowed from the current state."


class UnsupportedMediaError(CivicOSError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"
    message = "That file type is not accepted."


class PayloadTooLargeError(CivicOSError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "payload_too_large"
    message = "The uploaded file exceeds the configured size limit."


class AIProviderError(CivicOSError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "ai_provider_error"
    message = "The AI provider could not complete the request."


class AIBudgetExceededError(CivicOSError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "ai_budget_exceeded"
    message = "The AI usage budget for this municipality has been exhausted."


class ConfigurationError(CivicOSError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "configuration_error"
    message = "This capability is not configured on the server."


class ExternalServiceError(CivicOSError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "external_service_error"
    message = "An upstream service failed."


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the CivicOS error envelope to a FastAPI application."""

    @app.exception_handler(CivicOSError)
    async def _civicos_error(request: Request, exc: CivicOSError) -> JSONResponse:
        log = logger.bind(code=exc.code, path=request.url.path)
        if exc.status_code >= 500:
            log.error("request_failed", message=exc.message, details=exc.details)
        else:
            log.info("request_rejected", message=exc.message)
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(_request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "The submitted payload is invalid.",
                    "details": {"fields": jsonable_encoder(exc.errors())},
                    "request_id": _request_id(request),
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: "authentication_required",
            403: "permission_denied",
            404: "not_found",
            405: "method_not_allowed",
            429: "rate_limited",
        }.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": code,
                    "message": str(exc.detail),
                    "details": {},
                    "request_id": _request_id(request),
                }
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", path=request.url.path, error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "An unexpected error occurred.",
                    "details": {},
                    "request_id": _request_id(request),
                }
            },
        )
