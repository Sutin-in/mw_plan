"""Runtime settings from environment variables and an optional, uncommitted ``.env`` file.

Secrets (database passwords, session secret) live only in the environment or in
``.env`` at the repository root, which is git-ignored. Nothing here logs a secret;
use ``redact_url`` when a URL must be shown.

Variables:
    PPR_DATABASE_URL        development database (SQLAlchemy URL, postgresql+psycopg://...)
    PPR_TEST_DATABASE_URL   test database; its name MUST end with ``_test``
    PPR_SESSION_SECRET      HMAC key for application session tokens (>= 32 chars)
    PPR_SESSION_TTL_MINUTES session lifetime in minutes (default 480)
    PPR_HOSXP_MODE          ``mock`` (default) or ``sql`` (real HOSxP via read-only SQL)
    PPR_HOSXP_DATABASE_URL  HOSxP database, READ-ONLY account (mysql+pymysql://... or
                            postgresql+psycopg://...); required when mode is ``sql``
    PPR_HOSXP_AUTH_DATABASE_URL  optional: HOSxP login server, READ-ONLY account, used only by
                            the sign-in queries (D-41); unset = PPR_HOSXP_DATABASE_URL
    PPR_HOSXP_QUERIES       site query file (default: hosxp_queries.toml at the repo root)
    PPR_PROFILE             ``development`` (default) or ``production`` (Wave 10A): production
                            refuses demo mode, a runtime database account with more than its
                            runtime rights, and an API listening beyond this machine
    PPR_MIGRATION_DATABASE_URL  the schema OWNER account, used only by ``ppr.cli migrate``
                            (default: PPR_DATABASE_URL, as in development)
    PPR_RUNTIME_ROLE        database role of the runtime account; ``migrate`` grants it
                            exactly the runtime rights (append-only tables: SELECT, INSERT)
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
MIN_SECRET_LENGTH = 32
DEFAULT_SESSION_TTL_MINUTES = 480
DEFAULT_HOSXP_QUERIES = REPO_ROOT / "hosxp_queries.toml"
HOSXP_MODES = ("mock", "sql")
PROFILES = ("development", "production")
_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class SettingsError(ValueError):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments and blank lines ignored; optional quotes)."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def load_environment(env_file: Path | None = DEFAULT_ENV_FILE) -> dict[str, str]:
    """``.env`` values overlaid by the real environment (the environment wins)."""
    merged = parse_env_file(env_file) if env_file else {}
    merged.update({k: v for k, v in os.environ.items() if k.startswith("PPR_")})
    return merged


def redact_url(url: str) -> str:
    """Hide the password of a database URL, whatever characters it holds (Wave 11B).

    Not parsed as a URL: a password written with a raw ``#``, ``?``, ``/`` or ``@`` breaks
    URL parsing (``#`` starts a fragment), and the password then leaked. Everything from
    the first ``:`` after ``scheme://`` up to the last ``@`` is hidden, and a ``password=``
    query parameter; when in doubt more is hidden, never less.
    """
    url = _QUERY_PASSWORD.sub(r"\1***", url)
    start = url.find("://")
    if start < 0:
        return url
    rest = url[start + 3 :]
    at = rest.rfind("@")
    if at < 0:
        return url
    colon = rest.find(":", 0, at)
    if colon < 0:
        return url
    return url[: start + 3] + rest[:colon] + ":***" + rest[at:]


_QUERY_PASSWORD = re.compile(r"(?i)([?&]password=)[^&]*")


def database_name(url: str) -> str:
    """The database a URL names, read as SQLAlchemy reads it (Wave 11B: URL parsing gave a
    wrong name - or part of the password - when the password holds ``/``, ``#`` or ``?``)."""
    try:
        return make_url(url).database or ""
    except ArgumentError:
        return ""


def require_test_database(url: str) -> str:
    """Refuse to run destructive test setup against anything but a ``*_test`` database."""
    name = database_name(url)
    if not name.endswith("_test"):
        raise SettingsError(
            f"refusing to use database {name!r} for tests: the test database name must end "
            "with '_test' (tests drop and recreate the schema)"
        )
    return url


@dataclass(frozen=True)
class Settings:
    database_url: str | None
    test_database_url: str | None
    session_secret: str | None
    session_ttl_minutes: int
    hosxp_mode: str = "mock"
    hosxp_database_url: str | None = None
    hosxp_auth_database_url: str | None = None  # D-41: sign-in server, None = the same
    hosxp_queries_file: Path = DEFAULT_HOSXP_QUERIES
    profile: str = "development"
    migration_database_url: str | None = None
    runtime_role: str | None = None

    @property
    def production(self) -> bool:
        return self.profile == "production"

    @staticmethod
    def from_mapping(env: Mapping[str, str]) -> Settings:
        ttl_raw = env.get("PPR_SESSION_TTL_MINUTES", str(DEFAULT_SESSION_TTL_MINUTES))
        try:
            ttl = int(ttl_raw)
        except ValueError as exc:
            raise SettingsError("PPR_SESSION_TTL_MINUTES must be an integer") from exc
        if ttl <= 0:
            raise SettingsError("PPR_SESSION_TTL_MINUTES must be > 0")
        mode = (env.get("PPR_HOSXP_MODE") or "mock").strip().lower()
        if mode not in HOSXP_MODES:
            raise SettingsError(f"PPR_HOSXP_MODE must be one of {HOSXP_MODES}, got {mode!r}")
        queries = env.get("PPR_HOSXP_QUERIES")
        profile = (env.get("PPR_PROFILE") or "development").strip().lower()
        if profile not in PROFILES:
            raise SettingsError(f"PPR_PROFILE must be one of {PROFILES}, got {profile!r}")
        runtime_role = (env.get("PPR_RUNTIME_ROLE") or "").strip() or None
        if runtime_role is not None and not _ROLE_NAME.match(runtime_role):
            raise SettingsError("PPR_RUNTIME_ROLE must be a plain lower-case PostgreSQL role name")
        return Settings(
            database_url=env.get("PPR_DATABASE_URL") or None,
            test_database_url=env.get("PPR_TEST_DATABASE_URL") or None,
            session_secret=env.get("PPR_SESSION_SECRET") or None,
            session_ttl_minutes=ttl,
            hosxp_mode=mode,
            hosxp_database_url=env.get("PPR_HOSXP_DATABASE_URL") or None,
            hosxp_auth_database_url=(env.get("PPR_HOSXP_AUTH_DATABASE_URL") or "").strip() or None,
            hosxp_queries_file=Path(queries) if queries else DEFAULT_HOSXP_QUERIES,
            profile=profile,
            migration_database_url=env.get("PPR_MIGRATION_DATABASE_URL") or None,
            runtime_role=runtime_role,
        )

    @staticmethod
    def load(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
        return Settings.from_mapping(load_environment(env_file))

    def require_session_secret(self) -> str:
        if not self.session_secret or len(self.session_secret) < MIN_SECRET_LENGTH:
            raise SettingsError(
                f"PPR_SESSION_SECRET must be set and at least {MIN_SECRET_LENGTH} characters"
            )
        return self.session_secret

    def require_hosxp_database_url(self) -> str:
        if not self.hosxp_database_url:
            raise SettingsError(
                "PPR_HOSXP_DATABASE_URL is not set (a READ-ONLY HOSxP database account)"
            )
        return self.hosxp_database_url

    def migration_url(self) -> str:
        """The schema owner's account (``migrate`` only); the runtime one in development."""
        if self.migration_database_url:
            return self.migration_database_url
        if self.production:
            raise SettingsError(
                "production: set PPR_MIGRATION_DATABASE_URL (the schema owner account); the "
                "runtime account PPR_DATABASE_URL must not own or change the schema"
            )
        return self.require_database_url()

    def require_database_url(self) -> str:
        if not self.database_url:
            raise SettingsError("PPR_DATABASE_URL is not set (see .env.example)")
        return self.database_url
