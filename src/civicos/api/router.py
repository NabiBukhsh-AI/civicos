"""Aggregates every v1 router behind the configured API prefix."""

from __future__ import annotations

from fastapi import APIRouter

from civicos.api.v1 import (
    admin,
    analytics,
    assets,
    assistant,
    auth,
    documents,
    engagement,
    issues,
    public,
    workorders,
)

api_router = APIRouter()

# Ordering shapes the OpenAPI page: what a resident touches first comes first.
api_router.include_router(public.router)
api_router.include_router(auth.router)
api_router.include_router(issues.router)
api_router.include_router(assistant.router)
api_router.include_router(documents.router)
api_router.include_router(workorders.router)
api_router.include_router(workorders.crews_router)
api_router.include_router(assets.router)
api_router.include_router(assets.services_router)
api_router.include_router(engagement.router)
api_router.include_router(analytics.router)
api_router.include_router(admin.router)

__all__ = ["api_router"]
