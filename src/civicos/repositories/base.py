"""Tenant-scoped repository base.

The single most important invariant in a multi-tenant civic system is that no
query ever crosses a municipality boundary. Rather than trusting every call
site to remember a ``WHERE tenant_id = ...``, all reads and writes for
tenant-owned tables go through this class, which takes the tenant id in its
constructor and applies it unconditionally.
"""

from __future__ import annotations

import uuid
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from civicos.core.errors import NotFoundError
from civicos.core.pagination import PageParams
from civicos.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class TenantRepository(Generic[ModelT]):
    """CRUD + query helpers bound to one tenant."""

    model: type[ModelT]

    def __init__(self, session: AsyncSession, tenant_id: uuid.UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    # -- query building -------------------------------------------------------

    def query(self, *, include_deleted: bool = False) -> Select[Any]:
        statement = select(self.model).where(self.model.tenant_id == self.tenant_id)  # type: ignore[attr-defined]
        if not include_deleted and hasattr(self.model, "deleted_at"):
            statement = statement.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return statement

    def count_query(self, *, include_deleted: bool = False) -> Select[Any]:
        statement = select(func.count()).select_from(self.model).where(
            self.model.tenant_id == self.tenant_id  # type: ignore[attr-defined]
        )
        if not include_deleted and hasattr(self.model, "deleted_at"):
            statement = statement.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return statement

    # -- reads ----------------------------------------------------------------

    async def get(self, entity_id: uuid.UUID, *, include_deleted: bool = False) -> ModelT | None:
        return await self.session.scalar(
            self.query(include_deleted=include_deleted).where(self.model.id == entity_id)  # type: ignore[attr-defined]
        )

    async def get_or_404(self, entity_id: uuid.UUID) -> ModelT:
        entity = await self.get(entity_id)
        if entity is None:
            raise NotFoundError(
                f"{self.model.__name__} not found.",
                code=f"{self.model.__tablename__.rstrip('s')}_not_found",  # type: ignore[attr-defined]
                details={"id": str(entity_id)},
            )
        return entity

    async def list(
        self,
        statement: Select[Any] | None = None,
        *,
        page: PageParams | None = None,
    ) -> Sequence[ModelT]:
        statement = statement if statement is not None else self.query()
        if page is not None:
            statement = statement.offset(page.offset).limit(page.limit)
        return (await self.session.scalars(statement)).unique().all()

    async def count(self, statement: Select[Any] | None = None) -> int:
        if statement is None:
            statement = self.count_query()
        else:
            statement = select(func.count()).select_from(statement.subquery())
        return int(await self.session.scalar(statement) or 0)

    async def paginate(
        self, statement: Select[Any], page: PageParams
    ) -> tuple[Sequence[ModelT], int]:
        """Return one page plus the total, in that order."""
        total = await self.count(statement)
        items = await self.list(statement, page=page)
        return items, total

    async def exists(self, **filters: Any) -> bool:
        statement = self.query()
        for field, value in filters.items():
            statement = statement.where(getattr(self.model, field) == value)
        return await self.session.scalar(statement.limit(1)) is not None

    # -- writes ---------------------------------------------------------------

    async def add(self, entity: ModelT, *, flush: bool = True) -> ModelT:
        if getattr(entity, "tenant_id", None) is None:
            entity.tenant_id = self.tenant_id  # type: ignore[attr-defined]
        self.session.add(entity)
        if flush:
            await self.session.flush()
        return entity

    async def soft_delete(self, entity: ModelT) -> ModelT:
        from civicos.core.clock import utcnow  # noqa: PLC0415

        if not hasattr(entity, "deleted_at"):
            raise TypeError(f"{type(entity).__name__} does not support soft deletion")
        entity.deleted_at = utcnow()  # type: ignore[attr-defined]
        await self.session.flush()
        return entity

    async def hard_delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()


def apply_sort(
    statement: Select[Any],
    model: type[Any],
    sort_by: str | None,
    descending: bool,
    *,
    allowed: set[str],
    default: str = "created_at",
) -> Select[Any]:
    """Apply an allow-listed sort, so a query parameter cannot order by anything."""
    field = sort_by if sort_by in allowed else default
    column = getattr(model, field, None)
    if column is None:
        column = getattr(model, default)
    return statement.order_by(column.desc() if descending else column.asc())
