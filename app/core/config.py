from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def load_env() -> None:
    """
    Local-dev convenience: load `.env` into process env.

    Values are still sourced via `os.getenv` everywhere else.
    """
    load_dotenv(override=False)


def env_str(name: str, *, default: str | None = None, required: bool = False) -> str | None:
    v = os.getenv(name)
    if v is None or v == "":
        if required and default is None:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    return v


def env_int(name: str, *, default: int | None = None, required: bool = False) -> int | None:
    raw = env_str(name, default=None, required=required)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"Invalid int env var {name}={raw!r}") from e


def env_csv(name: str) -> list[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


@dataclass(frozen=True)
class Settings:
    @property
    def SECRET_KEY(self) -> str:
        return env_str("SECRET_KEY", required=True)  # type: ignore[return-value]

    @property
    def ALGORITHM(self) -> str:
        return env_str("ALGORITHM", default="HS256") or "HS256"

    @property
    def ACCESS_TOKEN_EXPIRE_MINUTES(self) -> int:
        return env_int("ACCESS_TOKEN_EXPIRE_MINUTES", default=1440) or 1440

    @property
    def DATABASE_URL(self) -> str:
        return env_str("DATABASE_URL", required=True)  # type: ignore[return-value]

    # Email
    @property
    def SMTP_USER(self) -> str:
        return env_str("SMTP_USER", default="") or ""

    @property
    def SMTP_PASSWORD(self) -> str:
        return env_str("SMTP_PASSWORD", default="") or ""

    @property
    def SMTP_HOST(self) -> str:
        return env_str("SMTP_HOST", default="smtp.gmail.com") or "smtp.gmail.com"

    @property
    def SMTP_PORT(self) -> int:
        return env_int("SMTP_PORT", default=587) or 587

    @property
    def smtp_configured(self) -> bool:
        """Both user and app password must be set for outbound mail."""
        u = (self.SMTP_USER or "").strip()
        p = (self.SMTP_PASSWORD or "").strip()
        return bool(u and p)

    @property
    def EMAIL_FROM(self) -> str:
        return env_str("EMAIL_FROM", default="UMT Team Portal <noreply@umt.edu.pk>") or "UMT Team Portal <noreply@umt.edu.pk>"

    @property
    def PUBLIC_BASE_URL(self) -> str:
        return env_str("PUBLIC_BASE_URL", default="") or ""

    # Google Sheets (optional)
    @property
    def GOOGLE_SERVICE_ACCOUNT_JSON(self) -> str:
        return env_str("GOOGLE_SERVICE_ACCOUNT_JSON", default="") or ""

    # Admin email list (for notifications)
    @property
    def ADMIN_EMAILS(self) -> str:
        return env_str("ADMIN_EMAILS", default="") or ""

    @property
    def admin_emails_list(self) -> list[str]:
        return env_csv("ADMIN_EMAILS")

    # Retired; kept empty to preserve runtime expectations.
    @property
    def lead_emails_list(self) -> list[str]:
        return []


settings = Settings()

