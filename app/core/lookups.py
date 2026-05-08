from __future__ import annotations

from functools import lru_cache

from sqlalchemy import text

from app.core.database import SessionLocal


@lru_cache(maxsize=256)
def course_name_by_id(course_id: int) -> str:
    """
    Small global cache for static-ish lookup data.

    Safe for read-heavy endpoints that repeatedly need course names and where
    occasional staleness is acceptable (admin edits are rare).
    """
    if not course_id:
        return ""

    db = SessionLocal()
    try:
        row = db.execute(text("SELECT name FROM courses WHERE id = :id"), {"id": int(course_id)}).first()
        return str(row[0]) if row and row[0] is not None else ""
    finally:
        db.close()


def clear_course_cache() -> None:
    course_name_by_id.cache_clear()

