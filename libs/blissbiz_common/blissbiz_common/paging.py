"""Pagination and LIKE helpers shared by list endpoints (docs/05 conventions: page_size <= 100)."""

from fastapi import Query
from pydantic import BaseModel


class Page(BaseModel):
    page: int
    page_size: int
    total: int


def like_pattern(text: str) -> str:
    """Escape % and _ so user text is matched literally (use with ``escape='\\\\'``)."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def page_query(default_size: int = 20, max_size: int = 100):
    def dependency(
        page: int = Query(1, ge=1, le=100000),
        page_size: int = Query(default_size, ge=1, le=max_size),
    ) -> tuple[int, int]:
        return page, page_size

    return dependency
