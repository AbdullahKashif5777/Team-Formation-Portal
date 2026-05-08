"""
Compatibility shim.

Configuration plumbing has moved to `app.core.config`. Keep `settings` here so
existing imports (`from app.config import settings`) continue to work.
"""

from app.core.config import settings  # noqa: F401
