"""Authentication token management and HTTP middleware for the bridge server."""

from __future__ import annotations

import os
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from bridge.server_state import log_event

# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------

_AUTH_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".voxherd", "auth_token")

# Module-level token — set on import and updated by ensure_auth_token().
_AUTH_TOKEN: str | None = None


def _load_auth_token() -> str | None:
    """Load auth token from env var or file. Returns None if auth is disabled."""
    token = os.environ.get("VOXHERD_AUTH_TOKEN")
    if token:
        return token
    try:
        with open(_AUTH_TOKEN_FILE) as f:
            token = f.read().strip()
        if token:
            return token
    except FileNotFoundError:
        pass
    return None


def persist_auth_token(token: str) -> None:
    """Write token to ~/.voxherd/auth_token with mode 0600."""
    os.makedirs(os.path.dirname(_AUTH_TOKEN_FILE), mode=0o700, exist_ok=True)
    fd = os.open(_AUTH_TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)


def ensure_auth_token() -> str:
    """Ensure an auth token exists. Auto-generates one if missing.

    Returns the active token.
    """
    global _AUTH_TOKEN
    token = _load_auth_token()
    if not token:
        token = secrets.token_hex(32)
        persist_auth_token(token)
    _AUTH_TOKEN = token
    return token


def get_auth_token() -> str | None:
    """Return the current auth token (or None if auth is disabled)."""
    return _AUTH_TOKEN


# Initialize on import
_AUTH_TOKEN = _load_auth_token()


# ---------------------------------------------------------------------------
# Auth checking
# ---------------------------------------------------------------------------


def _check_auth(request: Request) -> bool:
    """Return True if request is authorized. Always True when token is unset."""
    if not _AUTH_TOKEN:
        return True
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return secrets.compare_digest(auth[7:], _AUTH_TOKEN)
    return False


async def auth_middleware(request: Request, call_next):
    """Reject unauthenticated HTTP requests when auth token is configured.

    Also enforces CSRF protection: POST/PUT/PATCH/DELETE requests must include
    an ``X-VoxHerd`` header. Browsers cannot send custom headers in simple
    CORS requests, so this blocks cross-site request forgery from malicious
    websites.
    """
    # WebSocket upgrades are handled in the WebSocket handler, not here
    if request.scope.get("type") == "websocket":
        return await call_next(request)
    # Health check is unauthenticated for monitoring/quick-test
    if request.url.path == "/health":
        return await call_next(request)
    if not _check_auth(request):
        client_host = request.client.host if request.client else "unknown"
        log_event("warning", "bridge", f"Auth rejected: {client_host} {request.method} {request.url.path}")
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})
    # CSRF: require custom header on state-changing methods
    # Accept both X-VoxHerd (current) and X-VoxHeard (legacy) for
    # backwards compatibility with older hooks / macOS app.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not (request.headers.get("x-voxherd") or request.headers.get("x-voxheard")):
            client_host = request.client.host if request.client else "unknown"
            log_event("warning", "bridge", f"CSRF rejected: {client_host} {request.method} {request.url.path} (missing X-VoxHerd header)")
            return JSONResponse(status_code=403, content={"error": "Missing X-VoxHerd header"})
    return await call_next(request)
