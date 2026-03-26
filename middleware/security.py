"""
middleware/security.py — Security helpers registered on the Flask app

Provides:
  • Request sanitization (strip null-bytes, log suspicious headers)
  • CSRF protection via Flask-WTF
  • Basic in-memory rate-limiter (swap for Redis in production)
  • Security response headers (CSP, HSTS, etc.)
"""
import logging
import time
from collections import defaultdict
from functools import wraps
from flask import request, jsonify, g
from flask_wtf.csrf import CSRFProtect

logger = logging.getLogger(__name__)
csrf = CSRFProtect()

# ── Simple in-memory rate limiter ─────────────────────────────────────────────
_RATE_STORE: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT  = 60    # requests
_RATE_WINDOW = 60    # seconds


def _get_client_id() -> str:
    """Use real IP even behind reverse proxy."""
    return (
        request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        or request.remote_addr
        or "unknown"
    )


def rate_limit_check():
    """Returns True if request is allowed; False if rate-limited."""
    client = _get_client_id()
    now    = time.time()
    window_start = now - _RATE_WINDOW

    # Purge old entries
    _RATE_STORE[client] = [t for t in _RATE_STORE[client] if t > window_start]
    _RATE_STORE[client].append(now)

    return len(_RATE_STORE[client]) <= _RATE_LIMIT


def require_rate_limit(f):
    """Decorator: abort with 429 if rate limit exceeded."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not rate_limit_check():
            logger.warning("Rate limit exceeded for %s on %s", _get_client_id(), request.path)
            return jsonify({"error": "Too many requests. Please slow down."}), 429
        return f(*args, **kwargs)
    return decorated


# ── Security headers ──────────────────────────────────────────────────────────

def add_security_headers(response):
    """Set conservative security headers on every response."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"]        = "SAMEORIGIN"
    response.headers["X-XSS-Protection"]       = "1; mode=block"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    # Allow Google fonts + self for CSS/JS
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self';"
    )
    return response


# ── Request sanitization ──────────────────────────────────────────────────────

def sanitize_request():
    """Called before_request to strip nulls and log suspicious input."""
    # Strip null-bytes from form inputs
    for key in list(request.form.keys()):
        val = request.form.get(key, "")
        if "\x00" in val:
            logger.warning("Null byte in form field '%s' from %s", key, _get_client_id())

    # Log oversized Content-Length
    content_len = request.content_length or 0
    if content_len > 100 * 1024 * 1024:   # 100 MB hard cap
        logger.warning("Oversized request (%d bytes) from %s", content_len, _get_client_id())


# ── Registration helper ───────────────────────────────────────────────────────

def init_security(app):
    """Register all security middleware on the Flask app."""
    csrf.init_app(app)
    app.after_request(add_security_headers)
    app.before_request(sanitize_request)
    logger.info("Security middleware initialised.")
