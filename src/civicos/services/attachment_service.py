"""Attachment handling: validate, store, read EXIF, optionally analyse.

Evidence photographs are the backbone of municipal accountability, so this
pipeline does more than move bytes: it verifies the declared media type against
the file's own magic numbers, extracts capture time and GPS, and - when the
report itself carries no location - promotes the photo's coordinates onto the
issue so a geotagged photo is enough to place a complaint on the map.
"""

from __future__ import annotations

import uuid
from typing import Any, Sequence

import structlog
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.ai.types import ImagePart
from civicos.ai.usage import UsageContext
from civicos.ai.vision import analyse_images
from civicos.core.config import get_settings
from civicos.core.errors import PayloadTooLargeError
from civicos.core.geo import Point, encode_geohash, is_valid_coordinate
from civicos.integrations.exif import extract_metadata, location_consistency
from civicos.integrations.storage import (
    get_storage,
    validate_upload,
    verify_declared_type,
)
from civicos.domain.enums import IssueEventType
from civicos.domain.issues import Issue, IssueAttachment, IssueEvent
from civicos.domain.tenancy import Municipality

logger = structlog.get_logger(__name__)

_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


async def attach_to_issue(
    session: AsyncSession,
    tenant: Municipality,
    issue: Issue,
    files: Sequence[UploadFile],
    *,
    stage: str = "report",
    analyse: bool = False,
    uploader_id: uuid.UUID | None = None,
    work_order_id: uuid.UUID | None = None,
) -> list[IssueAttachment]:
    """Store uploads against an issue and enrich them."""
    settings = get_settings()
    storage = get_storage()
    attachments: list[IssueAttachment] = []
    image_parts: list[ImagePart] = []
    photo_points: list[Point] = []

    for upload in files:
        data = await upload.read()
        content_type = verify_declared_type(data, upload.content_type or "")
        is_image = content_type in _IMAGE_TYPES

        validate_upload(
            data,
            content_type,
            allowed_types=(
                settings.storage.allowed_image_types
                if is_image
                else settings.storage.allowed_document_types
            ),
            max_bytes=settings.storage.max_upload_bytes,
            label="image" if is_image else "document",
        )

        stored = await storage.save(
            data,
            tenant_id=tenant.id,
            filename=upload.filename or "upload",
            content_type=content_type,
            folder=f"issues/{issue.reference}",
        )

        attachment = IssueAttachment(
            tenant_id=tenant.id,
            issue_id=issue.id,
            work_order_id=work_order_id,
            kind="photo" if is_image else "document",
            stage=stage,
            storage_key=stored.key,
            public_url=stored.public_url,
            filename=stored.filename,
            content_type=content_type,
            size_bytes=stored.size_bytes,
            checksum=stored.checksum,
            uploaded_by_id=uploader_id,
        )

        if is_image:
            metadata = extract_metadata(data, stored.filename)
            attachment.captured_at = metadata.captured_at
            attachment.latitude = metadata.latitude
            attachment.longitude = metadata.longitude
            attachment.device = metadata.device
            attachment.width = metadata.width
            attachment.height = metadata.height
            attachment.exif = metadata.to_dict()
            if metadata.point is not None:
                photo_points.append(metadata.point)
            if len(image_parts) < settings.ai.max_images_per_request:
                image_parts.append(
                    ImagePart(data=data, media_type=content_type, label=stored.filename)
                )

        session.add(attachment)
        attachments.append(attachment)

    await session.flush()

    _promote_photo_location(issue, photo_points)
    _record_location_check(issue, photo_points)

    if analyse and image_parts:
        await _analyse_and_store(session, tenant, issue, image_parts, attachments)

    session.add(
        IssueEvent(
            tenant_id=tenant.id,
            issue_id=issue.id,
            event_type=IssueEventType.ATTACHMENT_ADDED,
            actor_id=uploader_id,
            actor_label="uploader",
            note=f"{len(attachments)} file(s) added ({stage}).",
            payload={"count": len(attachments), "stage": stage},
        )
    )
    await session.flush()
    return attachments


def _promote_photo_location(issue: Issue, points: list[Point]) -> None:
    """Use a photo's GPS when the report itself has none.

    A resident who photographs a problem has already told us where it is; making
    them drop a pin as well is friction that loses reports.
    """
    if issue.has_location or not points:
        return
    point = points[0]
    if not is_valid_coordinate(point.latitude, point.longitude):
        return
    issue.latitude = point.latitude
    issue.longitude = point.longitude
    issue.geohash = encode_geohash(point.latitude, point.longitude)
    issue.extra["location_source"] = "photo_exif"
    logger.info("issue_location_from_exif", reference=issue.reference)


def _record_location_check(issue: Issue, points: list[Point]) -> None:
    """Flag when photo GPS disagrees with the reported location."""
    if not points or not issue.has_location:
        return
    reported = Point(issue.latitude, issue.longitude)  # type: ignore[arg-type]
    check = location_consistency(reported, points)
    issue.extra["photo_location_check"] = check
    if check.get("consistent") is False:
        issue.is_flagged = True
        issue.flag_reason = (
            f"Photo location is {check['max_distance_m']} m from the reported position."
        )


async def _analyse_and_store(
    session: AsyncSession,
    tenant: Municipality,
    issue: Issue,
    images: list[ImagePart],
    attachments: list[IssueAttachment],
) -> None:
    """Run vision analysis and record it against the issue and its photos."""
    from civicos.services.routing_service import taxonomy_for_prompt  # noqa: PLC0415

    try:
        taxonomy = await taxonomy_for_prompt(session, tenant.id)
        report = await analyse_images(
            images,
            categories=taxonomy,
            context_note=f"Report: {issue.title}",
            usage=UsageContext.from_request(
                session, tenant_id=tenant.id, entity_type="issue", entity_id=issue.id
            ),
        )
    except Exception as exc:
        logger.warning("attachment_analysis_failed", reference=issue.reference, error=str(exc))
        return

    payload: dict[str, Any] = report.model_dump()
    issue.ai_analysis = {**issue.ai_analysis, "vision": payload}
    if report.hazards:
        issue.extra["hazards"] = report.hazards

    for attachment in attachments:
        if attachment.kind == "photo":
            attachment.ai_caption = report.summary[:1000]
            attachment.ai_analysis = payload

    session.add(
        IssueEvent(
            tenant_id=tenant.id,
            issue_id=issue.id,
            event_type=IssueEventType.AI_TRIAGED,
            actor_label="system",
            note=report.summary[:2000],
            is_public=False,
            payload={"source": "vision", "confidence": report.confidence},
        )
    )
    await session.flush()


async def read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload, refusing anything over the limit."""
    data = await upload.read()
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"'{upload.filename}' exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            details={"size_bytes": len(data)},
        )
    return data
