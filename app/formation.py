from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app import models

_CACHE_LOCK = threading.Lock()
_FORMATION_CACHE: dict[int, tuple[float, bool]] = {}
_FORMATION_TTL_S = 5.0


def _dt_utc_z(dt: datetime | None) -> str | None:
    """Serialize stored-naive-UTC datetimes as ISO 8601 with Z."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


FormationReason = Literal[
    "open",
    "no_section",
    "invalid_section_id",
    "before_start",
    "after_end",
]


def formation_window_debug(section_id: int) -> dict:
    """
    Returns server-side evidence for formation window checks.

    Useful to debug time window mismatches in production without DB access.
    """
    try:
        sid = int(section_id)
    except Exception:
        return {
            "section_id": section_id,
            "now_utc": _dt_utc_z(datetime.utcnow()),
            "formation_start_utc": None,
            "formation_end_utc": None,
            "is_open": False,
            "reason": "invalid_section_id",
        }

    if not sid:
        return {
            "section_id": sid,
            "now_utc": _dt_utc_z(datetime.utcnow()),
            "formation_start_utc": None,
            "formation_end_utc": None,
            "is_open": False,
            "reason": "invalid_section_id",
        }

    db: Session = SessionLocal()
    try:
        section = db.query(models.Section).filter(models.Section.id == sid).first()
        if not section:
            return {
                "section_id": sid,
                "now_utc": _dt_utc_z(datetime.utcnow()),
                "formation_start_utc": None,
                "formation_end_utc": None,
                "is_open": False,
                "reason": "no_section",
            }

        start = getattr(section, "formation_start", None)
        end = getattr(section, "formation_end", None)

        if start is None and end is None:
            return {
                "section_id": sid,
                "now_utc": _dt_utc_z(datetime.utcnow()),
                "formation_start_utc": None,
                "formation_end_utc": None,
                "is_open": True,
                "reason": "open",
            }

        # Stored formation_start/formation_end are treated as UTC (naive UTC in DB).
        now = datetime.utcnow()
        if start is not None and now < start:
            return {
                "section_id": sid,
                "now_utc": _dt_utc_z(now),
                "formation_start_utc": _dt_utc_z(start),
                "formation_end_utc": _dt_utc_z(end),
                "is_open": False,
                "reason": "before_start",
            }
        if end is not None and now > end:
            return {
                "section_id": sid,
                "now_utc": _dt_utc_z(now),
                "formation_start_utc": _dt_utc_z(start),
                "formation_end_utc": _dt_utc_z(end),
                "is_open": False,
                "reason": "after_end",
            }
        return {
            "section_id": sid,
            "now_utc": _dt_utc_z(now),
            "formation_start_utc": _dt_utc_z(start),
            "formation_end_utc": _dt_utc_z(end),
            "is_open": True,
            "reason": "open",
        }
    finally:
        db.close()


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

        # Stored formation_start/formation_end are treated as UTC (naive UTC in DB).
        now = datetime.utcnow()
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
