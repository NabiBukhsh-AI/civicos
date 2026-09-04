"""Test fixtures.

The whole suite runs against an in-process SQLite database with the offline
heuristic AI provider, so it needs no network, no API key and no services. If
these tests need a container to run, contributors will stop running them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest

os.environ.setdefault("CIVICOS_ENVIRONMENT", "test")
os.environ.setdefault("CIVICOS_DATABASE__URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("CIVICOS_AI__PROVIDER", "mock")
os.environ.setdefault("CIVICOS_RATE_LIMIT__ENABLED", "false")
os.environ.setdefault("CIVICOS_SECURITY__SECRET_KEY", "test-secret-key-at-least-32-chars-long")
os.environ.setdefault("CIVICOS_OBSERVABILITY__LOG_LEVEL", "WARNING")
os.environ.setdefault("CIVICOS_TENANCY__DEFAULT_TENANT_SLUG", "testville")

import httpx
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

import civicos.domain  # noqa: F401  (registers every table)
from civicos.core.config import get_settings
from civicos.core.context import Actor, set_actor, set_tenant
from civicos.db.base import Base
from civicos.db.seed import seed_demo_tenant
from civicos.db.session import get_session
from civicos.domain.tenancy import Municipality
from civicos.main import create_app

TEST_TENANT_SLUG = "testville"
ADMIN_EMAIL = "admin@testville.example"
ADMIN_PASSWORD = "TestPassword123!"


@pytest.fixture
async def engine() -> AsyncIterator[object]:
    """A fresh in-memory database per test.

    ``StaticPool`` keeps every connection pointed at the same in-memory
    database, which is what makes ``:memory:`` usable across a request cycle.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


@pytest.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
async def tenant(session) -> Municipality:
    """A fully configured municipality with departments, taxonomy and SLAs."""
    municipality = await seed_demo_tenant(
        session,
        slug=TEST_TENANT_SLUG,
        name="Testville Municipal Committee",
        admin_email=ADMIN_EMAIL,
        admin_password=ADMIN_PASSWORD,
    )
    await session.commit()
    set_tenant(municipality.id, municipality.slug)
    return municipality


@pytest.fixture
def system_actor() -> Actor:
    actor = Actor(kind="system", display_name="test-suite", is_superadmin=True)
    set_actor(actor)
    return actor


@pytest.fixture
async def client(session_factory, tenant) -> AsyncIterator[httpx.AsyncClient]:
    """An HTTP client bound to the app, sharing the test's database."""
    app = create_app(get_settings())

    async def _override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_session] = _override_session

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={"X-Tenant": TEST_TENANT_SLUG},
    ) as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest.fixture
async def admin_token(client) -> str:
    response = await client.post(
        "/api/v1/auth/login",
        json={"identifier": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture
def auth_headers(admin_token) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture
def sample_report() -> dict[str, object]:
    return {
        "description": (
            "The drain at the corner of Station Road has been overflowing for "
            "two weeks. Sewage is running across the footpath and it smells."
        ),
        "location": {"latitude": 12.9716, "longitude": 77.5946},
        "address": "Corner of Station Road",
        "reporter_name": "Test Resident",
        "reporter_phone": "03001234567",
    }


@pytest.fixture
def unique_slug() -> str:
    return f"t{uuid.uuid4().hex[:8]}"
