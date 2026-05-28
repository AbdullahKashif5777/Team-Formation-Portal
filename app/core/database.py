from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import DeclarativeBase, sessionmaker
import logging
import time

load_dotenv(override=False)

logger = logging.getLogger("uvicorn.error")


def _env_flag(name: str) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _normalize_database_url(url: str) -> str:
    u = (url or "").strip()
    if not u:
        raise RuntimeError("DATABASE_URL is not set.")

    # Common provider formats:
    # - postgres://... (legacy)
    # - postgresql://... (no explicit driver)
    # Prefer psycopg2 driver for SQLAlchemy sync engine.
    if u.startswith("postgres://"):
        u = "postgresql+psycopg2://" + u[len("postgres://") :]
    elif u.startswith("postgresql://"):
        u = "postgresql+psycopg2://" + u[len("postgresql://") :]
    return u


def _enforce_sslmode_require(url: str) -> str:
    """
    Neon requires TLS; ensure sslmode=require for Postgres URLs unless already set.
    """
    if not url.startswith("postgresql+psycopg2://"):
        return url

    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    if not q.get("sslmode"):
        q["sslmode"] = "require"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), parts.fragment))


def _apply_neon_pooler_suffix(url: str) -> str:
    """
    Neon may provide a pooler hostname (or a separate pooler URL). If the
    hostname does not already use a `-pooler` variant, opportunistically swap
    in the `-pooler` host.
    """
    if not url.startswith("postgresql+psycopg2://"):
        return url

    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host or host.endswith("-pooler") or "-pooler." in host:
        return url

    pooler_host = f"{host}-pooler"

    userinfo = ""
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo += f":{parts.password}"
        userinfo += "@"
    port = f":{parts.port}" if parts.port else ""
    new_netloc = f"{userinfo}{pooler_host}{port}"
    return urlunsplit((parts.scheme, new_netloc, parts.path, parts.query, parts.fragment))


def _resolve_database_url() -> str:
    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError("Missing required environment variable: DATABASE_URL")

    # Allow Neon-provided pooler URL override.
    pooler_override = (os.getenv("NEON_POOLER_URL") or "").strip()
    if pooler_override:
        raw = pooler_override

    normalized = _normalize_database_url(raw)
    normalized = _enforce_sslmode_require(normalized)

    # If enabled, prefer the `-pooler` hostname variant when possible.
    if _env_flag("NEON_USE_POOLER_SUFFIX"):
        normalized = _apply_neon_pooler_suffix(normalized)

    return normalized


DATABASE_URL = _resolve_database_url()

_engine_kwargs: dict = {"pool_pre_ping": True, "pool_recycle": 300}
if DATABASE_URL.startswith("postgresql"):
    _engine_kwargs["connect_args"] = {"sslmode": "require"}

# Default: local SQLAlchemy pooling sized for spikes.
# If using Neon's sidecar pooler (or any external pool), enable NullPool to
# avoid double-pooling and keep the app lightweight.
if _env_flag("DB_USE_NULLPOOL"):
    _engine_kwargs["poolclass"] = NullPool
else:
    _engine_kwargs["pool_size"] = int(os.getenv("DB_POOL_SIZE") or 20)
    _engine_kwargs["max_overflow"] = int(os.getenv("DB_MAX_OVERFLOW") or 100)

engine = create_engine(DATABASE_URL, **_engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def log_db_healthcheck(*, timeout_s: float = 2.0) -> None:
    """
    Non-fatal DB connectivity check for startup visibility.

    This helps catch slow/failing DNS resolution and suspended compute nodes early.
    """
    start = time.perf_counter()
    try:
        with engine.connect() as conn:
            if timeout_s and timeout_s > 0:
                conn.exec_driver_sql(f"SET statement_timeout = {int(timeout_s * 1000)}")
            conn.execute(text("SELECT 1"))
        ms = int((time.perf_counter() - start) * 1000)
        logger.info("DB healthcheck OK (%sms)", ms)
    except Exception as e:
        ms = int((time.perf_counter() - start) * 1000)
        logger.warning("DB healthcheck FAILED after %sms: %s", ms, e)


def bootstrap_dual_admins_if_empty() -> None:
    """
    Creates two admin accounts if the User table is empty.

    Env vars (required when DB has no users):
    - ADMIN1_EMAIL, ADMIN1_PASSWORD, ADMIN2_EMAIL, ADMIN2_PASSWORD
    Optional:
    - ADMIN1_NAME, ADMIN2_NAME (default: Admin 1, Admin 2)
    """
    from app import models
    from app.core.security import hash_password

    db = SessionLocal()
    try:
        any_user = db.query(models.User).first()
        if any_user:
            return

        e1 = (os.getenv("ADMIN1_EMAIL") or "").strip().lower()
        e2 = (os.getenv("ADMIN2_EMAIL") or "").strip().lower()
        p1 = (os.getenv("ADMIN1_PASSWORD") or "").strip()
        p2 = (os.getenv("ADMIN2_PASSWORD") or "").strip()
        n1 = (os.getenv("ADMIN1_NAME") or "Admin 1").strip() or "Admin 1"
        n2 = (os.getenv("ADMIN2_NAME") or "Admin 2").strip() or "Admin 2"

        if not all((e1, e2, p1, p2)):
            raise RuntimeError(
                "Database is empty but ADMIN1_EMAIL, ADMIN1_PASSWORD, ADMIN2_EMAIL, and "
                "ADMIN2_PASSWORD must all be set to bootstrap the two admin accounts."
            )
        if e1 == e2:
            raise RuntimeError("ADMIN1_EMAIL and ADMIN2_EMAIL must be different.")

        db.add(
            models.User(
                name=n1,
                email=e1,
                student_id=None,
                password_hash=hash_password(p1),
                role="admin",
                is_active=True,
            )
        )
        db.add(
            models.User(
                name=n2,
                email=e2,
                student_id=None,
                password_hash=hash_password(p2),
                role="admin",
                is_active=True,
            )
        )
        db.commit()
    finally:
        db.close()


def ensure_dual_admins_from_env() -> None:
    """
    When ADMIN1_* and ADMIN2_* are all set, ensure both accounts exist as active admins.

    - Missing user: created with hashed password from env.
    - Existing user: role set to admin, is_active True.
    - If ADMIN_PASSWORD_SYNC is truthy, password_hash is updated from env for those two emails.
    """
    from app import models
    from app.core.security import hash_password

    e1 = (os.getenv("ADMIN1_EMAIL") or "").strip().lower()
    e2 = (os.getenv("ADMIN2_EMAIL") or "").strip().lower()
    p1 = (os.getenv("ADMIN1_PASSWORD") or "").strip()
    p2 = (os.getenv("ADMIN2_PASSWORD") or "").strip()
    n1 = (os.getenv("ADMIN1_NAME") or "Admin 1").strip() or "Admin 1"
    n2 = (os.getenv("ADMIN2_NAME") or "Admin 2").strip() or "Admin 2"

    if not all((e1, e2, p1, p2)):
        return
    if e1 == e2:
        return

    sync_pw = _env_flag("ADMIN_PASSWORD_SYNC")

    db = SessionLocal()
    try:
        for email, password, name in ((e1, p1, n1), (e2, p2, n2)):
            u = db.query(models.User).filter(models.User.email == email).first()
            if not u:
                db.add(
                    models.User(
                        name=name,
                        email=email,
                        student_id=None,
                        password_hash=hash_password(password),
                        role="admin",
                        is_active=True,
                    )
                )
            else:
                u.role = "admin"
                u.is_active = True
                if sync_pw:
                    u.password_hash = hash_password(password)
        db.commit()
    finally:
        db.close()


def _ensure_performance_indexes() -> None:
    """
    Composite indexes for common filter patterns (safe on Postgres + SQLite).
    Column-level indexes are declared on models; these supplement them.
    """
    # IMPORTANT:
    # - Do not create ORM tables here with "generic" SQL; Postgres vs SQLite differ
    #   (e.g. INTEGER PRIMARY KEY is not auto-increment on Postgres).
    # - Base.metadata.create_all() is the source of truth for table DDL.
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_team_memberships_member_status ON team_memberships (member_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_team_memberships_team_status ON team_memberships (team_id, status)",
        "ALTER TABLE viva_sprints ADD COLUMN IF NOT EXISTS batch_key VARCHAR(36)",
        "CREATE INDEX IF NOT EXISTS ix_viva_sprints_batch_key ON viva_sprints (batch_key)",
        "CREATE INDEX IF NOT EXISTS ix_viva_sprints_section_date ON viva_sprints (section_id, slot_date)",
        "ALTER TABLE viva_sprints ADD COLUMN IF NOT EXISTS sprint_number INTEGER",
        "ALTER TABLE viva_sprints ADD COLUMN IF NOT EXISTS is_shared_pool BOOLEAN DEFAULT FALSE",
        "CREATE INDEX IF NOT EXISTS ix_viva_sprints_sprint_number ON viva_sprints (sprint_number)",
        "ALTER TABLE viva_slots ADD COLUMN IF NOT EXISTS note VARCHAR(200)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uix_viva_batch_section ON viva_batch_sections (batch_key, section_id)",
        "CREATE INDEX IF NOT EXISTS ix_viva_batch_sections_batch_key ON viva_batch_sections (batch_key)",
        "CREATE INDEX IF NOT EXISTS ix_viva_batch_sections_section_id ON viva_batch_sections (section_id)",
    ]
    for sql in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:
            pass


def create_tables() -> None:
    from app import models  # noqa: F401 - import to register models

    if _env_flag("DB_PURGE_ON_NEXT_STARTUP"):
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    _ensure_performance_indexes()
    bootstrap_dual_admins_if_empty()
    ensure_dual_admins_from_env()
