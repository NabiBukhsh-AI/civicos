"""Application configuration.

Every knob is environment driven so the same container image can serve any
municipality. Nested settings use a double-underscore delimiter::

    CIVICOS_AI__PROVIDER=anthropic
    CIVICOS_DATABASE__URL=postgresql+asyncpg://user:pass@host/civicos
"""

from __future__ import annotations

import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class DatabaseSettings(BaseModel):
    """Database connection settings.

    SQLite (aiosqlite) works out of the box for local development and CI;
    PostgreSQL + pgvector is the supported production target.
    """

    url: str = "sqlite+aiosqlite:///./civicos.db"
    echo: bool = False
    pool_size: int = 10
    max_overflow: int = 20
    pool_recycle_seconds: int = 1800
    pool_pre_ping: bool = True

    @property
    def is_sqlite(self) -> bool:
        return self.url.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.url.startswith("postgresql")


class SecuritySettings(BaseModel):
    secret_key: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    refresh_token_ttl_days: int = 14
    password_min_length: int = 10
    bcrypt_rounds: int = 12
    # Public civic reporting works without an account when enabled.
    allow_anonymous_reports: bool = True
    max_failed_logins: int = 5
    lockout_minutes: int = 15


class CorsSettings(BaseModel):
    allow_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    allow_origin_regex: str | None = None
    allow_credentials: bool = True
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])

    @field_validator("allow_origins", "allow_methods", "allow_headers", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


class AISettings(BaseModel):
    """LLM / embedding provider configuration.

    ``provider`` selects the default chat backend. Individual capabilities can
    override it (for example run vision on Gemini while chat runs on Claude).
    """

    provider: Literal["anthropic", "google", "openai", "mock"] = "mock"
    chat_model: str = "claude-opus-5"
    fast_model: str = "claude-haiku-4-5"

    vision_provider: Literal["anthropic", "google", "openai", "mock", "default"] = "default"
    vision_model: str | None = None

    embedding_provider: Literal["google", "openai", "mock", "default"] = "default"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536

    anthropic_api_key: str | None = None
    google_api_key: str | None = None
    openai_api_key: str | None = None

    max_output_tokens: int = 2048
    temperature: float = 0.2
    request_timeout_seconds: int = 60
    max_retries: int = 3

    # Cost and abuse guardrails.
    daily_token_budget: int = 2_000_000
    max_images_per_request: int = 8
    max_image_bytes: int = 12 * 1024 * 1024

    @property
    def resolved_vision_provider(self) -> str:
        return self.provider if self.vision_provider == "default" else self.vision_provider

    @property
    def resolved_embedding_provider(self) -> str:
        if self.embedding_provider != "default":
            return self.embedding_provider
        # Anthropic does not serve an embedding endpoint; fall back sensibly.
        return "mock" if self.provider == "anthropic" else self.provider


class RAGSettings(BaseModel):
    chunk_size: int = 1200
    chunk_overlap: int = 180
    top_k: int = 6
    candidate_multiplier: int = 4
    min_similarity: float = 0.15
    max_context_chars: int = 12_000
    vector_backend: Literal["auto", "pgvector", "numpy"] = "auto"
    keyword_weight: float = 0.35


class StorageSettings(BaseModel):
    backend: Literal["local", "s3"] = "local"
    local_path: Path = REPO_ROOT / "var" / "uploads"
    max_upload_bytes: int = 25 * 1024 * 1024
    allowed_image_types: list[str] = Field(
        default_factory=lambda: ["image/jpeg", "image/png", "image/webp"]
    )
    allowed_document_types: list[str] = Field(
        default_factory=lambda: [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
            "text/markdown",
            "text/csv",
        ]
    )
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_endpoint_url: str | None = None
    s3_public_base_url: str | None = None


class NotificationSettings(BaseModel):
    default_channels: list[str] = Field(default_factory=lambda: ["in_app"])
    sms_backend: Literal["console", "twilio", "webhook", "disabled"] = "console"
    email_backend: Literal["console", "smtp", "disabled"] = "console"
    whatsapp_backend: Literal["console", "meta", "disabled"] = "disabled"
    push_backend: Literal["console", "fcm", "disabled"] = "disabled"

    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    webhook_url: str | None = None

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "no-reply@civicos.local"
    smtp_use_tls: bool = True

    meta_phone_number_id: str | None = None
    meta_access_token: str | None = None
    fcm_server_key: str | None = None


class RedisSettings(BaseModel):
    url: str | None = None
    cache_ttl_seconds: int = 300
    queue_name: str = "civicos:jobs"


class RateLimitSettings(BaseModel):
    enabled: bool = True
    default_per_minute: int = 120
    anonymous_per_minute: int = 30
    ai_per_minute: int = 20


class ObservabilitySettings(BaseModel):
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    metrics_enabled: bool = True
    tracing_enabled: bool = False
    otlp_endpoint: str | None = None
    slow_request_ms: int = 1500


class TenancySettings(BaseModel):
    """How an incoming request is mapped to a municipality."""

    default_tenant_slug: str = "default"
    resolution_order: list[Literal["jwt", "header", "subdomain", "query", "default"]] = Field(
        default_factory=lambda: ["jwt", "header", "subdomain", "default"]
    )
    header_name: str = "X-Tenant"
    base_domain: str | None = None
    allow_cross_tenant_superadmin: bool = True


class GeoSettings(BaseModel):
    """Fallbacks used when a tenant has not configured its own map bounds."""

    default_latitude: float = 0.0
    default_longitude: float = 0.0
    default_zoom: int = 13
    duplicate_radius_meters: int = 120
    duplicate_time_window_hours: int = 168
    duplicate_similarity_threshold: float = 0.82
    hotspot_cell_meters: int = 250
    geocoder: Literal["none", "nominatim"] = "none"
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    nominatim_user_agent: str = "civicos/2.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CIVICOS_",
        env_nested_delimiter="__",
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    project_name: str = "CivicOS"
    project_tagline: str = "AI-native municipal operations platform"
    environment: Environment = Environment.LOCAL
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    docs_enabled: bool = True
    root_path: str = ""
    supported_languages: list[str] = Field(default_factory=lambda: ["en"])
    default_language: str = "en"
    timezone: str = "UTC"

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    cors: CorsSettings = Field(default_factory=CorsSettings)
    ai: AISettings = Field(default_factory=AISettings)
    rag: RAGSettings = Field(default_factory=RAGSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    tenancy: TenancySettings = Field(default_factory=TenancySettings)
    geo: GeoSettings = Field(default_factory=GeoSettings)

    @field_validator("supported_languages", mode="before")
    @classmethod
    def _split_languages(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> Settings:
        if self.environment is Environment.PRODUCTION:
            if self.debug:
                raise ValueError("debug must be disabled in production")
            if "*" in self.cors.allow_origins:
                raise ValueError(
                    "cors.allow_origins must not contain '*' in production; list explicit origins"
                )
            if len(self.security.secret_key) < 32:
                raise ValueError("security.secret_key must be at least 32 characters in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def is_testing(self) -> bool:
        return self.environment is Environment.TEST


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache (used by tests that mutate the environment)."""
    get_settings.cache_clear()


settings = get_settings()
