"""Mock HOSxP authentication. Stores only salted PBKDF2 hashes - never plaintext."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from ppr.integration.hosxp.auth import AuthFailure, AuthResult
from ppr.integration.hosxp.contracts import HosxpUserProfile
from ppr.integration.hosxp.errors import HosxpUnavailableError

_ITERATIONS = 200_000


@dataclass(frozen=True)
class _Credential:
    salt: bytes
    digest: bytes


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)


class MockHosxpAuthenticationAdapter:
    def __init__(self) -> None:
        self.available = True
        self._creds: dict[str, _Credential] = {}
        self._profiles: dict[str, HosxpUserProfile] = {}
        self._by_username: dict[str, str] = {}

    def add_user(self, profile: HosxpUserProfile, password: str) -> None:
        salt = secrets.token_bytes(16)
        self._creds[profile.source_id] = _Credential(salt, _hash(password, salt))
        self._profiles[profile.source_id] = profile
        self._by_username[profile.username] = profile.source_id

    def stored_secrets(self) -> list[bytes]:
        """For tests: everything persisted about passwords (must not contain plaintext)."""
        return [c.salt + c.digest for c in self._creds.values()]

    def authenticate(self, username: str, password: str) -> AuthResult:
        if not self.available:
            raise HosxpUnavailableError("mock HOSxP authentication is unavailable")
        sid = self._by_username.get(username)
        cred = self._creds.get(sid) if sid else None
        if (
            sid is None
            or cred is None
            or not hmac.compare_digest(cred.digest, _hash(password, cred.salt))
        ):
            return AuthResult(None, AuthFailure.INVALID_CREDENTIALS)
        if not self._profiles[sid].active:
            return AuthResult(None, AuthFailure.ACCOUNT_INACTIVE)
        return AuthResult(sid)

    def get_user_profile(self, user_source_id: str) -> HosxpUserProfile | None:
        if not self.available:
            raise HosxpUnavailableError("mock HOSxP authentication is unavailable")
        return self._profiles.get(user_source_id)
