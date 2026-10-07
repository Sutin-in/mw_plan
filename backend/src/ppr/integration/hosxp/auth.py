"""``HosxpAuthenticationAdapter`` port (spec §21).

The real HOSxP authentication mechanism is TBD. This port assumes nothing about
HOSxP APIs, sessions, tokens or password hashing. Implementations must never store
a HOSxP password in plaintext; the application's own session is issued by this
system after a successful ``authenticate`` (Wave 1B+).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ppr.integration.hosxp.contracts import HosxpUserProfile


class AuthFailure(StrEnum):
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"


@dataclass(frozen=True)
class AuthResult:
    user_source_id: str | None
    failure: AuthFailure | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None and self.user_source_id is not None


@runtime_checkable
class HosxpAuthenticationAdapter(Protocol):
    def authenticate(self, username: str, password: str) -> AuthResult:
        """Verify credentials against HOSxP. Raises HosxpUnavailableError if unreachable."""
        ...

    def get_user_profile(self, user_source_id: str) -> HosxpUserProfile | None: ...
