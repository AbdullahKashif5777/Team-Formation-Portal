from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.responses import Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import time
import logging

from app.database import create_tables
from app.core.database import log_db_healthcheck
from app.routers import auth, teams, ws, admin, roster
from app.routers import roster_sheet
from app.core.config import env_csv, load_env
from app.config import settings as app_settings

load_env()


def _cors_allow_origins_and_credentials() -> tuple[list[str], bool]:
    """
    ALLOWED_ORIGINS: comma-separated origins, or * for any origin.
    Wildcard * cannot be used with allow_credentials=True (CORS / Starlette rules),
    so * implies allow_credentials=False.
    If unset, falls back to localhost defaults plus CORS_ORIGIN / CORS_ORIGINS.
    """
    raw = (os.getenv("ALLOWED_ORIGINS") or "").strip()
    if raw == "*":
        return ["*"], False
    if raw:
        # Browsers send Origin without a trailing slash; strip so env mistakes like https://host/ still match.
        origins = list(
            dict.fromkeys(
                o.strip().rstrip("/")
                for o in raw.split(",")
                if o.strip().rstrip("/")
            )
        )
        return origins, True
    origins = list(
        dict.fromkeys(
            [
                "http://localhost:8000",
                "http://127.0.0.1:8000",
                *(env_csv("CORS_ORIGINS") or []),
                *(
                    [os.getenv("CORS_ORIGIN").strip()]
                    if (os.getenv("CORS_ORIGIN") or "").strip()
                    else []
                ),
            ]
        )
    )
    return origins, True


_cors_origins, _cors_credentials = _cors_allow_origins_and_credentials()

app = FastAPI(title="UMT Team Formation Portal", version="1.0.0", docs_url="/api/docs")

logger = logging.getLogger("uvicorn.error")


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    start = time.perf_counter()
    try:
        return await call_next(request)
    finally:
        ms = int((time.perf_counter() - start) * 1000)
        threshold_ms = int(os.getenv("SLOW_REQUEST_MS") or 800)
        if ms >= threshold_ms:
            logger.info("SLOW REQUEST %s %s took %sms", request.method, request.url.path, ms)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Fail closed: never crash the process on unexpected errors.
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth.router)
app.include_router(teams.router)
app.include_router(teams.member_router)
app.include_router(ws.router)
app.include_router(admin.router)
app.include_router(roster.router)
app.include_router(roster_sheet.router)

# Serve static frontend files
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Avoid browsers/CDNs serving an old index.html that references broken script paths after a deploy.
_HTML_NO_CACHE = {"Cache-Control": "no-store, max-age=0, must-revalidate"}


def _portal_html(filename: str) -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, filename), headers=dict(_HTML_NO_CACHE))


@app.on_event("startup")
def startup():
    create_tables()
    log_db_healthcheck()
    if app_settings.smtp_configured:
        logger.info(
            "Outbound email (SMTP) enabled via %s:%s",
            app_settings.SMTP_HOST,
            app_settings.SMTP_PORT,
        )
    else:
        logger.warning(
            "Outbound email disabled: set SMTP_USER and SMTP_PASSWORD on the server to send mail."
        )


@app.get("/", include_in_schema=False)
def root():
    return _portal_html("index.html")


@app.get("/lead", include_in_schema=False)
def lead_page():
    return _portal_html("lead.html")


@app.get("/member", include_in_schema=False)
def member_page():
    return _portal_html("member.html")


@app.get("/select-portal", include_in_schema=False)
def select_portal_page():
    return _portal_html("select-portal.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return _portal_html("admin.html")


@app.get("/roster", include_in_schema=False)
def roster_page():
    return _portal_html("roster.html")


@app.get("/config.js", include_in_schema=False)
def root_config_js():
    """Netlify serves static/ at site root; FastAPI only mounted /static — expose root aliases."""
    return FileResponse(
        os.path.join(STATIC_DIR, "config.js"),
        media_type="application/javascript",
    )


@app.get("/api-runtime.js", include_in_schema=False)
def root_api_runtime_js():
    return FileResponse(
        os.path.join(STATIC_DIR, "api-runtime.js"),
        media_type="application/javascript",
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    fp = os.path.join(STATIC_DIR, "favicon.ico")
    if os.path.exists(fp):
        return FileResponse(fp)
    return Response(status_code=204)


@app.get("/api/portal-features", include_in_schema=False)
def portal_features():
    """
    Deployment smoke check: curl this URL after deploy. 404 means an old image is still running.
    Bump marker when changing portal shell wiring (HTML + static JS paths).
    """
    return {
        "marker": "portal-shell-2026-05-09",
        "scripts_primary": "/static/config.js",
        "root_aliases": ["/config.js", "/api-runtime.js"],
        "html_cache": "meta+FileResponse-no-store",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
