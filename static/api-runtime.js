/**
 * Split-host support (Netlify frontend + Render backend).
 * Load after config.js. Uses window.API_BASE_URL when set (no trailing slash).
 */
(function () {
  function stripTrailingSlash(s) {
    return String(s || "").replace(/\/+$/, "");
  }

  function getBase() {
    var raw = typeof window.API_BASE_URL === "string" ? window.API_BASE_URL.trim() : "";
    return stripTrailingSlash(raw);
  }

  window.__portalApiBase = getBase;

  window.__portalApiUrl = function (path) {
    var p = path.startsWith("/") ? path : "/" + path;
    var b = getBase();
    return b ? b + p : p;
  };

  window.__portalWsUrl = function (userId, token) {
    var tok = encodeURIComponent(token || "");
    var b = getBase();
    if (b) {
      try {
        var u = new URL(b);
        var wsScheme = u.protocol === "https:" ? "wss" : "ws";
        return wsScheme + "://" + u.host + "/ws/" + userId + "?token=" + tok;
      } catch (e) {
        console.warn("[portal] Invalid API_BASE_URL", e);
      }
    }
    var wsScheme = location.protocol === "https:" ? "wss" : "ws";
    return wsScheme + "://" + location.host + "/ws/" + userId + "?token=" + tok;
  };
})();
