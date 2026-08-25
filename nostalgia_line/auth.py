"""Optional access control.

Nostalgia Line holds a Plex token and a TMDB key, writes files, and reaches out
to whatever URL it is pointed at. On a trusted LAN that is fine and a password
would only be friction, so authentication is **off by default** - turning it on
silently would lock people out of their own tool on upgrade.

When a password is set, every ``/api`` call needs it. The check is a constant-time
comparison against a salted hash; the password itself is never stored.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

COOKIE = "nostalgia_session"
SESSION_TTL = 30 * 24 * 3600  # a month; this is a homelab tool, not a bank

# Paths that must stay reachable without a session, or you could never log in
# and a container healthcheck could never pass.
OPEN_PATHS = frozenset({"/api/auth/status", "/api/auth/login", "/api/status"})


def hash_password(password: str, salt: str | None = None) -> str:
    """Salted PBKDF2. Stored in config; the password itself never is."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return f"pbkdf2_sha256${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt, _ = stored.split("$", 2)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


class Sessions:
    """In-memory sessions. A restart signs everyone out, which is acceptable for
    a tool that is usually a single tab on one machine."""

    def __init__(self, ttl: int = SESSION_TTL):
        self.ttl = ttl
        self._tokens: dict[str, float] = {}

    def issue(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expires = self._tokens.get(token)
        if expires is None:
            return False
        if expires < time.time():
            self._tokens.pop(token, None)
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token:
            self._tokens.pop(token, None)

    def clear(self) -> None:
        self._tokens.clear()


def password_from_env() -> str:
    """A password may also be supplied by the environment, for compose files."""
    return (os.getenv("NOSTALGIA_PASSWORD") or "").strip()
