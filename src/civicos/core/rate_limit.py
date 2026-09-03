"""Rate limiting with a pluggable backend.

In-memory sliding windows are fine for a single-process deployment (the common
case for a town running one container). When ``CIVICOS_REDIS__URL`` is set the
Redis backend keeps the limit consistent across replicas.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any

import structlog

from civicos.core.config import get_settings

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RateLimiter(ABC):
    @abstractmethod
    async def hit(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult: ...

    async def close(self) -> None:  # pragma: no cover - optional
        return None


class InMemoryRateLimiter(RateLimiter):
    """Sliding-window counter kept in process memory."""

    def __init__(self, max_keys: int = 50_000) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()
        self._max_keys = max_keys

    async def hit(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - window_seconds
        async with self._lock:
            if len(self._hits) > self._max_keys:
                self._evict(cutoff)
            bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window_seconds - now) + 1)
                return RateLimitResult(False, limit, 0, retry_after)
            bucket.append(now)
            return RateLimitResult(True, limit, limit - len(bucket), 0)

    def _evict(self, cutoff: float) -> None:
        stale = [key for key, bucket in self._hits.items() if not bucket or bucket[-1] < cutoff]
        for key in stale:
            self._hits.pop(key, None)


class RedisRateLimiter(RateLimiter):
    """Sorted-set sliding window evaluated atomically inside Redis."""

    _SCRIPT = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local window = tonumber(ARGV[2])
    local limit = tonumber(ARGV[3])
    local member = ARGV[4]
    redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
    local count = redis.call('ZCARD', key)
    if count >= limit then
      local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
      local retry = window
      if oldest[2] then retry = math.ceil(tonumber(oldest[2]) + window - now) end
      return {0, 0, retry}
    end
    redis.call('ZADD', key, now, member)
    redis.call('EXPIRE', key, window + 1)
    return {1, limit - count - 1, 0}
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._script: Any | None = None

    async def hit(self, key: str, *, limit: int, window_seconds: int = 60) -> RateLimitResult:
        try:
            if self._script is None:
                self._script = self._client.register_script(self._SCRIPT)
            now = time.time()
            allowed, remaining, retry = await self._script(
                keys=[f"civicos:rl:{key}"],
                args=[now, window_seconds, limit, f"{now}:{id(self)}"],
            )
            return RateLimitResult(bool(allowed), limit, int(remaining), int(retry))
        except Exception as exc:  # pragma: no cover - network dependent
            # Fail open: a Redis outage must not take the helpline offline.
            logger.warning("rate_limit_backend_error", error=str(exc))
            return RateLimitResult(True, limit, limit, 0)

    async def close(self) -> None:  # pragma: no cover - network dependent
        await self._client.aclose()


_limiter: RateLimiter | None = None


async def get_rate_limiter() -> RateLimiter:
    """Return the process-wide limiter, building it on first use."""
    global _limiter
    if _limiter is not None:
        return _limiter

    settings = get_settings()
    if settings.redis.url:
        try:
            import redis.asyncio as aioredis  # noqa: PLC0415

            client = aioredis.from_url(settings.redis.url, decode_responses=True)
            await client.ping()
            _limiter = RedisRateLimiter(client)
            logger.info("rate_limiter_ready", backend="redis")
            return _limiter
        except Exception as exc:
            logger.warning("redis_unavailable_using_memory_limiter", error=str(exc))

    _limiter = InMemoryRateLimiter()
    logger.info("rate_limiter_ready", backend="memory")
    return _limiter


async def reset_rate_limiter() -> None:
    """Drop the limiter (used between tests and on shutdown)."""
    global _limiter
    if _limiter is not None:
        await _limiter.close()
    _limiter = None
