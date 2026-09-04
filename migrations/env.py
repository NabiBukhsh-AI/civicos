"""Alembic environment.

Reads the database URL from application settings rather than alembic.ini, so
migrations always target the same database the application does, and enables
``render_as_batch`` so the same migration scripts apply on SQLite (which cannot
ALTER columns in place) as on PostgreSQL.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from civicos.core.config import get_settings
from civicos.db.base import Base

# Importing the domain package registers every table on Base.metadata.
import civicos.domain  # noqa: F401  isort: skip

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database.url)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Leave objects Alembic does not own alone.

    pgvector's ANN indexes are created by hand in a data migration (their
    parameters depend on corpus size), so autogenerate must not try to drop
    them the next time it runs.
    """
    if type_ == "index" and name and name.endswith("_ann"):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection."""
    context.configure(
        url=settings.database.url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        render_as_batch=settings.database.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        render_as_batch=settings.database.is_sqlite,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
