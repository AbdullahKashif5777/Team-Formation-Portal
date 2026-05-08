"""
Compatibility shim.

Database plumbing has moved to `app.core.database`. Keep re-exports here so the
rest of the codebase can continue importing from `app.database`.
"""

from app.core.database import (  # noqa: F401
    Base,
    DATABASE_URL,
    SessionLocal,
    create_tables,
    engine,
    get_db,
)
