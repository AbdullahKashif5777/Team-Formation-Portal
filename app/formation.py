from __future__ import annotations

import threading
import time
from datetime import datetime

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import models

_CACHE_LOCK = threading.Lock()
_FORMATION_CACHE: dict[int, tuple[float, bool]] = {}
_FORMATION_TTL_S = 5.0


def _compute_is_formation_open(section_id: int) -> bool:
    """
    Guardrail for team operations.

    Rules:
    - If both formation_start and formation_end are NULL -> open.
    - If only start is set -> open when now >= start.
    - If only end is set -> open when now <= end.
    - If both are set -> open when start <= now <= end.
    """
    if not section_id:
        return False

    db: Session = SessionLocal()
    try:
        section = db.query(models.Section).filter(models.Section.id == int(section_id)).first()
        if not section:
            return False

        start = getattr(section, "formation_start", None)
        end = getattr(section, "formation_end", None)
        if start is None and end is None:
            return True

        now = datetime.now()
        if start is not None and now < start:
            return False
        if end is not None and now > end:
            return False
        return True
    finally:
        db.close()


def is_formation_open(section_id: int) -> bool:
    """Same semantics as before; results memoized briefly to reduce repeated DB hits."""
    sid = int(section_id)
    now = time.monotonic()
    with _CACHE_LOCK:
        hit = _FORMATION_CACHE.get(sid)
        if hit is not None:
            ts, val = hit
            if now - ts < _FORMATION_TTL_S:
                return val

    result = _compute_is_formation_open(sid)
    with _CACHE_LOCK:
        _FORMATION_CACHE[sid] = (now, result)
    return result
