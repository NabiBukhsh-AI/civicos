"""Shared API schema primitives."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from civicos.core.geo import is_valid_coordinate

T = TypeVar("T")


class APIModel(BaseModel):
    """Base for response models read straight off ORM objects."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class InputModel(BaseModel):
    """Base for request bodies. Unknown keys are rejected, not ignored.

    Silently dropping an unrecognised field is how a client ends up believing
    it set a priority that never arrived.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class Message(BaseModel):
    message: str
    detail: str | None = None


class IdResponse(BaseModel):
    id: uuid.UUID
    reference: str | None = None


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


class Page(BaseModel, Generic[T]):
    items: list[T]
    meta: PageMeta


class Coordinates(InputModel):
    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)
    accuracy_meters: float | None = Field(default=None, ge=0.0)

    @field_validator("longitude")
    @classmethod
    def _reject_null_island(cls, value: float, info: Any) -> float:
        latitude = info.data.get("latitude")
        if latitude is not None and not is_valid_coordinate(latitude, value):
            raise ValueError("Coordinates are not usable (null island or out of range).")
        return value


class GeoPoint(APIModel):
    latitude: float | None = None
    longitude: float | None = None
    address: str | None = None
    landmark: str | None = None
    admin_unit_id: uuid.UUID | None = None
    admin_unit_name: str | None = None


class ActorSummary(APIModel):
    id: uuid.UUID | None = None
    name: str | None = None
    role: str | None = None


class TimelineEntry(APIModel):
    id: uuid.UUID
    event_type: str
    actor_label: str
    from_value: str | None = None
    to_value: str | None = None
    note: str | None = None
    is_public: bool = True
    created_at: datetime


class AttachmentOut(APIModel):
    id: uuid.UUID
    kind: str
    stage: str
    filename: str
    content_type: str
    size_bytes: int
    url: str | None = Field(default=None, alias="public_url")
    width: int | None = None
    height: int | None = None
    captured_at: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    ai_caption: str | None = None
    created_at: datetime


class HealthStatus(BaseModel):
    status: str
    version: str
    environment: str
    checks: dict[str, Any] = Field(default_factory=dict)
