"""``SqlHosxpAuthenticationAdapter``: HOSxP login through the read-only SQL account (D-25).

The product owner states that HOSxP stores an MD5 hash of each user's password. This
adapter reads the user's row with the site's ``get_login_user`` query, hashes the typed
password with MD5 and compares the digests in constant time. It knows no HOSxP table or
column names: those are in the site query file, written by IT from the real schema.

Until confirmed on the real system (D-25): the password's UTF-8 bytes are hashed, and
the stored hash is compared as hexadecimal, ignoring letter case and surrounding spaces.

Never stored, logged or returned: the typed password and the stored hash. Nothing is
written to HOSxP (the queries are checked read-only and run in read-only transactions).
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

from ppr.integration.hosxp.auth import AuthFailure, AuthResult
from ppr.integration.hosxp.contracts import HosxpUserProfile
from ppr.integration.hosxp.errors import HosxpConfigurationError
from ppr.integration.hosxp.real.query_config import LoginRow
from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway

_INVALID = AuthResult(None, AuthFailure.INVALID_CREDENTIALS)
_MD5_HEX = re.compile(r"[0-9a-fA-F]{32}")


@dataclass(frozen=True)
class LoginUserCheck:
    """What ``hosxp-check --user`` may show about one HOSxP account (D-41): facts about the
    query mapping only - never the password hash, never a person's name."""

    found: bool
    source_id: str | None = None
    active: bool | None = None
    has_department: bool | None = None
    hash_looks_md5: bool | None = None  # 32 hexadecimal characters (D-25, provisional)
    profile_found: bool | None = None


def md5_hex(password: str) -> str:
    return hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest()


class SqlHosxpAuthenticationAdapter:
    def __init__(self, gateway: SqlHosxpGateway) -> None:
        self.gateway = gateway

    def _one(self, operation: str, params: dict[str, str]) -> dict[str, object] | None:
        rows = self.gateway.rows(operation, params)
        if len(rows) > 1:
            raise HosxpConfigurationError(
                f"{operation} returned {len(rows)} rows; it must return at most one"
            )
        return rows[0] if rows else None

    def authenticate(self, username: str, password: str) -> AuthResult:
        if not username or not password:
            return _INVALID
        row = self._one("get_login_user", {"username": username})
        if row is None:
            # Hash anyway so an unknown user takes about as long as a wrong password.
            hmac.compare_digest(md5_hex(password).encode(), md5_hex(password).encode())
            return _INVALID
        user = self.gateway.build("get_login_user", LoginRow, row)
        stored = user.password_hash.strip().lower()
        if not hmac.compare_digest(md5_hex(password).encode(), stored.encode("utf-8")):
            return _INVALID
        if not user.active:
            return AuthResult(None, AuthFailure.ACCOUNT_INACTIVE)
        return AuthResult(user.source_id)

    def check_user(self, username: str) -> LoginUserCheck:
        """Read-only check of the site's sign-in queries for one account (no password)."""
        row = self._one("get_login_user", {"username": username})
        if row is None:
            return LoginUserCheck(found=False)
        user = self.gateway.build("get_login_user", LoginRow, row)
        return LoginUserCheck(
            found=True,
            source_id=user.source_id,
            active=user.active,
            has_department=bool(user.department_id),
            hash_looks_md5=bool(_MD5_HEX.fullmatch(user.password_hash.strip())),
            profile_found=self.get_user_profile(user.source_id) is not None,
        )

    def get_user_profile(self, user_source_id: str) -> HosxpUserProfile | None:
        row = self._one("get_user_profile", {"source_id": user_source_id})
        if row is None:
            return None
        return self.gateway.build("get_user_profile", HosxpUserProfile, row)
