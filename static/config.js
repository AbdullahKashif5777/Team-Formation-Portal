/**
 * Frontend ↔ backend base URL for API/WebSocket calls.
 *
 * Leave empty ("") when the portal HTML and FastAPI are served from the **same** HTTPS origin
 * (recommended): local uvicorn, Ocean Gate, or any host where `/api` and `/` share one hostname.
 * Then the browser uses same-origin `/api/...` and `/ws/...`.
 *
 * Set to a full HTTPS origin **only** when the static site is on a different host than the API
 * (split deploy). No trailing slash. Example: https://your-api.onrender.com
 */
window.API_BASE_URL = "https://umt-team-portal-dc72.onrender.com";
