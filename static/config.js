/**
 * Frontend ↔ backend URL when frontend is NOT served by FastAPI (e.g. Netlify + Render).
 * Set to your Render web service origin, no trailing slash:
 *   window.API_BASE_URL = 'https://your-service.onrender.com';
 * Leave empty when using one server (local uvicorn): same-origin /api and /ws work.
 */
window.API_BASE_URL = "https://team-formation-portal.onrender.com";
