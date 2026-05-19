"""HTTP Basic auth for the admin dashboard.

Single admin user defined in env: ADMIN_USER + ADMIN_PASSWORD_HASH (bcrypt).
If ADMIN_PASSWORD_HASH is unset, the dashboard returns 503 — fail-closed.

Generate the bcrypt hash:
    .venv/Scripts/python.exe -c "import bcrypt; print(bcrypt.hashpw(b'your-password', bcrypt.gensalt()).decode())"

We use the `bcrypt` library directly rather than passlib because passlib has
a known incompatibility with bcrypt 4.x.

`verify_admin_basic` is the underlying check; it accepts a raw `Authorization`
header string and is reused by the voice-playground WebSocket route, which
can't use FastAPI's HTTPBasic dependency (deps don't fire before the WS
upgrade handshake).
"""

from __future__ import annotations

import base64
import binascii
import secrets

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from logging_config import log
from settings import settings


_security = HTTPBasic(realm="Aria Admin")
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Basic realm="Aria Admin"'}


def verify_admin_basic(authorization: str | None) -> str:
    """Validate a raw `Basic <b64>` Authorization header.

    Returns the admin username on success. Raises HTTPException(401) on any
    failure (missing header, malformed scheme, bad base64, wrong creds) and
    HTTPException(503) when the admin password hash is not configured.
    """
    username, password = _parse_basic_header(authorization)
    return _verify_credentials(username, password)


def _verify_credentials(username: str, password: str) -> str:
    """Shared bcrypt verify path used by both the HTTP Basic dep and the WS auth."""
    if not settings.ADMIN_PASSWORD_HASH:
        log.error("admin.no_password_hash_configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin dashboard is not configured. Set ADMIN_PASSWORD_HASH.",
        )

    user_ok = secrets.compare_digest(
        username.encode("utf-8"), settings.ADMIN_USER.encode("utf-8")
    )
    pwd_ok = False
    try:
        # bcrypt has a 72-byte input cap; truncate to match its behavior rather
        # than ValueError on long passwords.
        pwd_bytes = password.encode("utf-8")[:72]
        hash_bytes = settings.ADMIN_PASSWORD_HASH.encode("utf-8")
        pwd_ok = bcrypt.checkpw(pwd_bytes, hash_bytes)
    except Exception:  # noqa: BLE001
        # Malformed hash, etc. — treat as failure but don't leak details.
        log.exception("admin.bcrypt_verify_failed")

    if not (user_ok and pwd_ok):
        # bcrypt's cost factor already absorbs ~100ms per attempt — that's
        # the per-attempt brake; no extra sleep needed here.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return username


def _parse_basic_header(authorization: str | None) -> tuple[str, str]:
    """Parse an `Authorization: Basic <b64(user:pass)>` header. Empty/garbled → 401."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials",
            headers=_UNAUTHORIZED_HEADERS,
        )
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "basic" or not value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unsupported auth scheme",
            headers=_UNAUTHORIZED_HEADERS,
        )
    try:
        raw = base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed credentials",
            headers=_UNAUTHORIZED_HEADERS,
        )
    user, sep, password = raw.partition(":")
    if not sep:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed credentials",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return user, password


def require_admin(creds: HTTPBasicCredentials = Depends(_security)) -> str:
    """FastAPI dep: return the admin username on success, 401/503 otherwise."""
    return _verify_credentials(creds.username, creds.password)
