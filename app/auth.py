"""
Compatibility shim.

Security plumbing has moved to `app.core.security`. Keep exports here so
existing imports (`from app.auth import ...`) continue to work.
"""

from app.core.security import *  # noqa: F403,F401
