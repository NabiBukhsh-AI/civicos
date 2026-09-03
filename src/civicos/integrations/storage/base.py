"""File storage abstraction.

Local disk for a single-server town deployment; S3-compatible object storage
(AWS, MinIO, Cloudflare R2) for anything larger. Keys are tenant-prefixed so a
misconfigured bucket policy cannot expose one municipality's evidence photos to
another.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from civicos.core.clock import utcnow
from civicos.core.errors import PayloadTooLargeError, UnsupportedMediaError

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(slots=True)
class StoredFile:
    key: str
    filename: str
    content_type: str
    size_bytes: int
    checksum: str
    public_url: str | None = None
    stored_at: datetime | None = None


class StorageBackend(ABC):
    """Content-addressed-ish blob storage."""

    @abstractmethod
    async def save(
        self,
        data: bytes,
        *,
        tenant_id: uuid.UUID,
        filename: str,
        content_type: str,
        folder: str = "uploads",
    ) -> StoredFile: ...

    @abstractmethod
    async def load(self, key: str) -> bytes: ...

    @abstractmethod
    async def delete(self, key: str) -> bool: ...

    @abstractmethod
    async def exists(self, key: str) -> bool: ...

    async def url_for(self, key: str, expires_seconds: int = 3600) -> str | None:
        """A URL a browser can fetch, if the backend can produce one."""
        return None

    async def health(self) -> bool:
        return True


def build_key(
    tenant_id: uuid.UUID, folder: str, filename: str, checksum: str
) -> str:
    """``<tenant>/<folder>/<yyyy>/<mm>/<hash8>-<safe-name>``.

    Date-partitioned so a year's uploads can be lifecycled or archived as a
    unit, and hash-prefixed so two residents uploading ``IMG_0001.jpg`` on the
    same day do not collide.
    """
    now = utcnow()
    safe = sanitize_filename(filename)
    return f"{tenant_id}/{folder}/{now:%Y/%m}/{checksum[:8]}-{safe}"


def sanitize_filename(filename: str, max_length: int = 80) -> str:
    """Strip path components and anything that could confuse a filesystem."""
    base = (filename or "file").replace("\\", "/").split("/")[-1]
    base = _SAFE_NAME.sub("_", base).strip("._") or "file"
    if len(base) <= max_length:
        return base
    stem, _, extension = base.rpartition(".")
    if extension and len(extension) <= 8:
        return f"{stem[: max_length - len(extension) - 1]}.{extension}"
    return base[:max_length]


def checksum_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_upload(
    data: bytes,
    content_type: str,
    *,
    allowed_types: list[str],
    max_bytes: int,
    label: str = "file",
) -> None:
    """Reject an upload before it reaches storage."""
    if not data:
        raise UnsupportedMediaError(f"The uploaded {label} is empty.", code="empty_upload")
    if len(data) > max_bytes:
        raise PayloadTooLargeError(
            f"The {label} exceeds the {max_bytes // (1024 * 1024)} MB limit.",
            details={"size_bytes": len(data), "maximum": max_bytes},
        )
    normalised = (content_type or "").split(";")[0].strip().lower()
    if allowed_types and normalised not in allowed_types:
        raise UnsupportedMediaError(
            f"'{normalised or 'unknown'}' is not an accepted {label} type.",
            details={"allowed": allowed_types},
        )


#: Magic-number prefixes. A browser-supplied content type is a hint, not a fact.
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),  # also docx/xlsx
)


def sniff_content_type(data: bytes, fallback: str = "application/octet-stream") -> str:
    """Detect the real media type from the file's own bytes."""
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data[0:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return fallback


def verify_declared_type(data: bytes, declared: str) -> str:
    """Return the trustworthy content type, preferring the sniffed one.

    A ``.jpg`` that is really a zip archive is either a mistake or an attack;
    either way the stored metadata should describe what the bytes actually are.
    """
    sniffed = sniff_content_type(data, fallback="")
    if not sniffed:
        return (declared or "application/octet-stream").split(";")[0].strip().lower()
    if sniffed == "application/zip" and declared.endswith("wordprocessingml.document"):
        return declared  # docx is a zip container
    return sniffed
