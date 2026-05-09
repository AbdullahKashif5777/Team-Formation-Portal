# UMT Team Formation Portal

A FastAPI-based portal for UMT team formation with role-based flows (Admin / Team Lead / Member), real-time updates (WebSocket), and optional integrations (SMTP email + Google Sheets roster sync).

**Maintainer:** [AbdullahKashif5777](https://github.com/AbdullahKashif5777)

**Repository**

- Web: [AbdullahKashif5777/Team-Formation-Portal](https://github.com/AbdullahKashif5777/Team-Formation-Portal)
- Clone (SSH): `git clone git@github.com:AbdullahKashif5777/Team-Formation-Portal.git`
- Clone (HTTPS): `git clone https://github.com/AbdullahKashif5777/Team-Formation-Portal.git`

## What’s in this repo

- **Backend**: FastAPI (`app/main.py`)
- **Database**: PostgreSQL via SQLAlchemy (configured by `DATABASE_URL`)
- **Frontend**: static HTML/CSS served from `static/` (pages: `/`, `/admin`, `/lead`, `/member`, `/roster`, `/select-portal`)
- **API docs**: Swagger UI at `/api/docs`

## Local setup (Windows / macOS / Linux)

Create a virtual environment, install dependencies, set env vars, then run the server.

```bash
python -m venv .venv
```

Activate:

- **Windows (PowerShell)**:

```bash
.\.venv\Scripts\Activate.ps1
```

- **macOS/Linux**:

```bash
source .venv/bin/activate
```

Install:

```bash
pip install -r requirements.txt
```

Create your `.env`:

```bash
copy .env.example .env
```

Then edit `.env` and set at least:

- **`DATABASE_URL`**: PostgreSQL connection string
- **`SECRET_KEY`**: JWT signing secret
- **`ADMIN1_*` / `ADMIN2_*`**: required on first boot when the users table is empty

Run the app:

```bash
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open:

- **Portal**: `http://127.0.0.1:8000/`
- **API docs**: `http://127.0.0.1:8000/api/docs`

## Environment variables

All variables are documented in `.env.example`. The key ones are:

### Required

- **`DATABASE_URL`**: Postgres URL (Neon/hosted Postgres supported)
- **`SECRET_KEY`**: JWT secret
- **`ALGORITHM`**: default `HS256`
- **`ACCESS_TOKEN_EXPIRE_MINUTES`**: default `1440`

### Admin bootstrap (first run)

On first startup, if the users table is empty, the app bootstraps **two admin users**.

- **`ADMIN1_EMAIL`**, **`ADMIN1_PASSWORD`**, **`ADMIN1_NAME`**
- **`ADMIN2_EMAIL`**, **`ADMIN2_PASSWORD`**, **`ADMIN2_NAME`**
- **`ADMIN_PASSWORD_SYNC`**: if truthy, refreshes the two admins’ password hashes from env on each startup (useful for testing)

### CORS

- **`ALLOWED_ORIGINS`**: comma-separated origins, or `*` (note: `*` disables credentialed requests)
- Optional legacy fallbacks: **`CORS_ORIGIN`**, **`CORS_ORIGINS`**

### Database safety / pooling

- **`DB_PURGE_ON_NEXT_STARTUP`**: if truthy, drops all tables then recreates schema on next startup (**destructive**)
- **`DB_USE_NULLPOOL`**, **`DB_POOL_SIZE`**, **`DB_MAX_OVERFLOW`**
- **`NEON_POOLER_URL`**, **`NEON_USE_POOLER_SUFFIX`**: optional Neon pooler controls

## Email notifications (SMTP) (optional)

Outbound mail is enabled only when **both** `SMTP_USER` and `SMTP_PASSWORD` are set.

For Gmail:

1. Enable 2-Step Verification and generate an **App Password** in your Google account security settings.
2. Set these in `.env`:

```env
SMTP_USER=your_email@umt.edu.pk
SMTP_PASSWORD=your_16_character_app_password
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
EMAIL_FROM=UMT Team Portal <noreply@umt.edu.pk>
PUBLIC_BASE_URL=http://127.0.0.1:8000
```

Admin notice recipients:

- **`ADMIN_EMAILS`**: comma-separated list of inboxes to receive admin notices. For safety, the app only sends to `@umt.edu.pk` recipients; anything else is ignored.

## Google Sheets roster auto-sync (optional)

If configured, the portal can create/update a Google Sheet for each Team Lead showing accepted members.

1. Create a project in Google Cloud Console.
2. Enable **Google Sheets API** and **Google Drive API**.
3. Create a **Service Account** and generate a JSON key.
4. Paste the JSON key contents into `.env` as a single line:

```env
GOOGLE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"..."}
```

Important: keep it as **one line** (no line breaks) in `.env`.

## Production deploy (unified host — DigitalOcean App Platform, Render, VPS, etc.)

Use this when **one public URL** serves both the static portal and the API (same process or reverse proxy in front of uvicorn).

1. **`static/config.js`**: keep `window.API_BASE_URL` as **`""`** (empty string). The UI calls `/api/...` on the same origin as the page.
2. **Server environment** (same variable names as on Render; the app reads these from the process environment):  
   - **`ALLOWED_ORIGINS`**: your **exact** public origin — scheme + host, **no trailing slash**. Example (DigitalOcean App Platform): `https://teamformationportal-ssr3o.ondigitalocean.app`  
   - **`PUBLIC_BASE_URL`**: the same portal URL for email links (typically identical to the origin above, without a path).  
   Add `http://127.0.0.1:8000` to `ALLOWED_ORIGINS` only if you also test locally against production-like settings.

Names like “base URL” or “origin” on a host dashboard must map to **`ALLOWED_ORIGINS`** and **`PUBLIC_BASE_URL`** — arbitrary env keys are not read by this app unless you add code for them.

**After moving from Netlify (or any old static host) to a unified Ocean Gate / App Platform URL:** update **`ALLOWED_ORIGINS`** and **`PUBLIC_BASE_URL`** on the API to that **new** HTTPS origin. Leaving only `https://….netlify.app` breaks CORS (browser sends `Origin: https://your-app-platform-host`) and leaves email links pointing at the wrong site. If some users still use an old Netlify URL, include **both** origins comma-separated; otherwise remove Netlify from env.

Restart/redeploy after changing env vars.

### Optional: split deploy (static site on host A, API on host B)

Example: static files on Netlify (`netlify.toml` publishes `static/`) and FastAPI on Render (`render.yaml`). The browser performs cross-origin requests; configure both sides:

1. **`static/config.js`**: set `window.API_BASE_URL` to the API’s public HTTPS origin (no trailing slash).
2. **`ALLOWED_ORIGINS`** on the API: include **every** frontend origin users use (e.g. `https://your-site.netlify.app`). Must match exactly or login fails in the browser despite healthy API logs.
3. **`PUBLIC_BASE_URL`**: the user-facing portal URL for emails.

Redeploy the static host after editing `config.js`; redeploy/restart the API after env changes.

## Tests

Basic smoke tests live in `tests/`.

```bash
pytest -v
```

These tests require `DATABASE_URL` and `SECRET_KEY` to be set (e.g. via `.env`).
