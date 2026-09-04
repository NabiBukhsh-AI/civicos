"""Storage backends and the factory that picks one."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import structlog

from civicos.core.config import get_settings
from civicos.core.errors import ConfigurationError, NotFoundError
from civicos.integrations.storage.base import (
    StorageBackend,
    StoredFile,
    build_key,
    checksum_bytes,
    sanitize_filename,
    sniff_content_type,
    validate_upload,
    verify_declared_type,
)

logger = structlog.get_logger(__name__)

__all__ = [
    "LocalStorage",
    "S3Storage",
    "StorageBackend",
    "StoredFile",
    "get_storage",
    "reset_storage",
    "sanitize_filename",
    "sniff_content_type",
    "validate_upload",
    "verify_declared_type",
]


class LocalStorage(StorageBackend):
    """Filesystem storage. The default, and enough for a single-server town."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        candidate = (self.root / key).resolve()
        # Refuse anything that escapes the storage root via "..".
        if not candidate.is_relative_to(self.root):
            raise ConfigurationError("Invalid storage key.", code="invalid_storage_key")
        return candidate

    async def save(
        self,
        data: bytes,
        *,
        tenant_id: uuid.UUID,
        filename: str,
        content_type: str,
        folder: str = "uploads",
    ) -> StoredFile:
        digest = checksum_bytes(data)
        key = build_key(tenant_id, folder, filename, digest)
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)
        return StoredFile(
            key=key,
            filename=sanitize_filename(filename),
            content_type=content_type,
            size_bytes=len(data),
            checksum=digest,
        )

    async def load(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise NotFoundError("Stored file not found.", code="file_not_found")
        return await asyncio.to_thread(path.read_bytes)

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        if not path.exists():
            return False
        await asyncio.to_thread(path.unlink)
        return True

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def health(self) -> bool:
        return self.root.exists() and self.root.is_dir()


class S3Storage(StorageBackend):
    """S3-compatible object storage (AWS S3, MinIO, R2, Wasabi)."""

    def __init__(
        self,
        bucket: str,
        *,
        region: str | None = None,
        endpoint_url: str | None = None,
        public_base_url: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self.endpoint_url = endpoint_url
        self.public_base_url = public_base_url
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigurationError(
                "S3 storage requires boto3. Install it with: pip install 'civicos[storage]'",
                code="boto3_missing",
            ) from exc
        self._client = boto3.client("s3", region_name=self.region, endpoint_url=self.endpoint_url)
        return self._client

    async def save(
        self,
        data: bytes,
        *,
        tenant_id: uuid.UUID,
        filename: str,
        content_type: str,
        folder: str = "uploads",
    ) -> StoredFile:
        digest = checksum_bytes(data)
        key = build_key(tenant_id, folder, filename, digest)
        client = self._get_client()

        # boto3 is synchronous; keep the event loop free.
        await asyncio.to_thread(
            client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
            Metadata={"tenant": str(tenant_id), "checksum": digest},
        )
        return StoredFile(
            key=key,
            filename=sanitize_filename(filename),
            content_type=content_type,
            size_bytes=len(data),
            checksum=digest,
            public_url=f"{self.public_base_url.rstrip('/')}/{key}"
            if self.public_base_url
            else None,
        )

    async def load(self, key: str) -> bytes:
        client = self._get_client()
        try:
            response = await asyncio.to_thread(client.get_object, Bucket=self.bucket, Key=key)
        except Exception as exc:
            raise NotFoundError("Stored file not found.", code="file_not_found") from exc
        return await asyncio.to_thread(response["Body"].read)

    async def delete(self, key: str) -> bool:
        client = self._get_client()
        await asyncio.to_thread(client.delete_object, Bucket=self.bucket, Key=key)
        return True

    async def exists(self, key: str) -> bool:
        client = self._get_client()
        try:
            await asyncio.to_thread(client.head_object, Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    async def url_for(self, key: str, expires_seconds: int = 3600) -> str | None:
        if self.public_base_url:
            return f"{self.public_base_url.rstrip('/')}/{key}"
        client = self._get_client()
        return await asyncio.to_thread(
            client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    async def health(self) -> bool:
        try:
            client = self._get_client()
            await asyncio.to_thread(client.head_bucket, Bucket=self.bucket)
            return True
        except Exception:
            return False


_storage: StorageBackend | None = None


def get_storage() -> StorageBackend:
    """Return the configured backend, building it on first use."""
    global _storage
    if _storage is not None:
        return _storage

    settings = get_settings()
    if settings.storage.backend == "s3":
        if not settings.storage.s3_bucket:
            raise ConfigurationError(
                "CIVICOS_STORAGE__S3_BUCKET must be set when backend is 's3'.",
                code="s3_bucket_missing",
            )
        _storage = S3Storage(
            settings.storage.s3_bucket,
            region=settings.storage.s3_region,
            endpoint_url=settings.storage.s3_endpoint_url,
            public_base_url=settings.storage.s3_public_base_url,
        )
    else:
        _storage = LocalStorage(settings.storage.local_path)

    logger.info("storage_ready", backend=type(_storage).__name__)
    return _storage


def reset_storage() -> None:
    global _storage
    _storage = None
