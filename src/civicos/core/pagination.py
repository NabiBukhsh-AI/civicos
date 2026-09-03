"""Pagination primitives shared by every list endpoint."""

from __future__ import annotations

from typing import Annotated, Generic, Sequence, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200


class PageParams(BaseModel):
    """Offset pagination parameters, bound as a FastAPI dependency."""

    page: int = Field(default=1, ge=1, description="1-indexed page number")
    page_size: int = Field(
        default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Items per page"
    )

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    @property
    def limit(self) -> int:
        return self.page_size


def page_params(
    page: Annotated[int, Query(ge=1, description="1-indexed page number")] = 1,
    page_size: Annotated[
        int, Query(ge=1, le=MAX_PAGE_SIZE, description="Items per page")
    ] = DEFAULT_PAGE_SIZE,
) -> PageParams:
    return PageParams(page=page, page_size=page_size)


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    total_pages: int
    has_next: bool
    has_previous: bool


class Page(BaseModel, Generic[T]):
    """A page of results plus navigation metadata."""

    items: list[T]
    meta: PageMeta

    @classmethod
    def build(cls, items: Sequence[T], total: int, params: PageParams) -> "Page[T]":
        total_pages = max(1, -(-total // params.page_size))  # ceil division
        return cls(
            items=list(items),
            meta=PageMeta(
                page=params.page,
                page_size=params.page_size,
                total=total,
                total_pages=total_pages,
                has_next=params.page < total_pages,
                has_previous=params.page > 1,
            ),
        )


class SortParams(BaseModel):
    sort_by: str | None = None
    descending: bool = True


def sort_params(
    sort_by: Annotated[str | None, Query(description="Field to sort by")] = None,
    order: Annotated[str, Query(pattern="^(asc|desc)$")] = "desc",
) -> SortParams:
    return SortParams(sort_by=sort_by, descending=order == "desc")
