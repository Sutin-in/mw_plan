"""Application session tokens (owned by this system, not by HOSxP).

After ``HosxpAuthenticationAdapter.authenticate`` succeeds, the application issues its
own short-lived, HMAC-SHA256-signed token. The token carries only the local user id
and expiry - never a password or HOSxP credential. Roles are NOT in the token: they
are read from the database on every request, so a revoked role takes effect at once.

Format: ``base64url(json payload) + "." + base64url(hmac)``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

TOKEN_VERSION = 1


class InvalidTokenError(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class SessionClaims:
    user_id: int
    expires_at: datetime


class SessionSigner:
    def __init__(self, secret: str, ttl: timedelta) -> None:
        if len(secret) < 32:
            raise ValueError("session secret must be at least 32 characters")
        self._key = secret.encode("utf-8")
        self._ttl = ttl

    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(self._key, payload, hashlib.sha256).digest())

    def issue(self, user_id: int, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        exp = int((now + self._ttl).timestamp())
        payload = json.dumps(
            {"v": TOKEN_VERSION, "uid": user_id, "exp": exp}, separators=(",", ":")
        ).encode()
        return f"{_b64(payload)}.{self._sign(payload)}"

    def verify(self, token: str, now: datetime | None = None) -> SessionClaims:
        now = now or datetime.now(UTC)
        try:
            body, sig = token.split(".", 1)
            payload = _unb64(body)
        except (ValueError, TypeError) as exc:
            raise InvalidTokenError("malformed token") from exc
        if not hmac.compare_digest(sig, self._sign(payload)):
            raise InvalidTokenError("bad signature")
        try:
            data = json.loads(payload)
            uid, exp, ver = int(data["uid"]), int(data["exp"]), int(data["v"])
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidTokenError("malformed payload") from exc
        if ver != TOKEN_VERSION:
            raise InvalidTokenError("unsupported token version")
        expires_at = datetime.fromtimestamp(exp, UTC)
        if now >= expires_at:
            raise InvalidTokenError("token expired")
        return SessionClaims(uid, expires_at)
