"""Session tokens, settings loading and the role->permission policy (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ppr.auth.session import InvalidTokenError, SessionSigner
from ppr.config.settings import (
    Settings,
    SettingsError,
    database_name,
    parse_env_file,
    redact_url,
    require_test_database,
)
from ppr.domain.permissions import ROLE_PERMISSIONS, Permission, is_allowed
from ppr.domain.roles import READ_ONLY_ROLES, Role

SECRET = "s" * 40
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)


def signer() -> SessionSigner:
    return SessionSigner(SECRET, timedelta(minutes=30))


# ------------------------------------------------------------------ sessions
def test_token_round_trip_carries_only_user_id_and_expiry() -> None:
    token = signer().issue(42, now=NOW)
    claims = signer().verify(token, now=NOW + timedelta(minutes=29))
    assert claims.user_id == 42
    assert claims.expires_at == NOW + timedelta(minutes=30)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),  # tampered signature
        lambda t: "e30" + t[t.index(".") :],  # payload swapped ({} base64)
        lambda t: t.replace(".", ""),  # no separator
        lambda t: "",
    ],
)
def test_tampered_tokens_are_rejected(mutate) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidTokenError):
        signer().verify(mutate(signer().issue(1, now=NOW)), now=NOW)


def test_expired_token_is_rejected() -> None:
    token = signer().issue(1, now=NOW)
    with pytest.raises(InvalidTokenError, match="expired"):
        signer().verify(token, now=NOW + timedelta(minutes=30))


def test_token_from_other_secret_is_rejected() -> None:
    other = SessionSigner("o" * 40, timedelta(minutes=30)).issue(1, now=NOW)
    with pytest.raises(InvalidTokenError, match="signature"):
        signer().verify(other, now=NOW)


def test_short_secret_is_refused() -> None:
    with pytest.raises(ValueError):
        SessionSigner("short", timedelta(minutes=1))


# ------------------------------------------------------------------ settings
def test_env_file_parsing_and_environment_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / ".env"
    f.write_text(
        "# comment\n\nPPR_DATABASE_URL='postgresql+psycopg://u:p@h/ppr_dev'\n"
        'PPR_SESSION_SECRET="abc"\nPPR_SESSION_TTL_MINUTES=15\n',
        encoding="utf-8",
    )
    assert parse_env_file(f)["PPR_SESSION_SECRET"] == "abc"
    # Isolate from the real environment (CI sets PPR_* variables; the environment wins).
    for name in ("PPR_DATABASE_URL", "PPR_TEST_DATABASE_URL", "PPR_SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PPR_SESSION_TTL_MINUTES", "60")
    s = Settings.load(f)
    assert s.database_url == "postgresql+psycopg://u:p@h/ppr_dev"
    assert s.session_ttl_minutes == 60  # real environment wins over .env
    with pytest.raises(SettingsError):
        s.require_session_secret()  # "abc" is too short


def test_missing_env_file_is_fine(tmp_path: Path) -> None:
    assert parse_env_file(tmp_path / "nope.env") == {}


@pytest.mark.parametrize("ttl", ["0", "-5", "ten"])
def test_invalid_ttl_rejected(ttl: str) -> None:
    with pytest.raises(SettingsError):
        Settings.from_mapping({"PPR_SESSION_TTL_MINUTES": ttl})


def test_passwords_are_redacted() -> None:
    url = "postgresql+psycopg://ppr_app:SuperSecret@localhost:5432/ppr_dev"
    assert "SuperSecret" not in redact_url(url)
    assert redact_url("postgresql://localhost/db") == "postgresql://localhost/db"


@pytest.mark.spec("AT-40.16.2", "S-36")
@pytest.mark.parametrize(
    "password", ["Pa#ss1", "a@b", "x/y", "q?w=1", "p:q", "#start", "end@", "ไทย#1"]
)
def test_a_password_is_hidden_whatever_it_holds(password: str) -> None:
    # Wave 11B: a raw '#' starts a URL fragment, and the old parser printed the password.
    for url in (
        f"postgresql+psycopg://reader:{password}@192.0.2.97:5432/hosxp",
        f"mysql+pymysql://u:{password}@h/db?charset=utf8mb4",
    ):
        shown = redact_url(url)
        assert password not in shown, shown
        assert ":***@" in shown
        assert shown.split("@")[-1] in url  # host and database still shown
    assert redact_url("postgresql://only_user@h:5432/db") == "postgresql://only_user@h:5432/db"
    assert redact_url("not a url") == "not a url"
    assert "s3cret" not in redact_url("postgresql://h/db?sslmode=require&password=s3cret")


@pytest.mark.spec("AT-40.16.2")
@pytest.mark.parametrize("password", ["Pa#ss1", "a@b", "x/y", "q?w=1", "p:q", "ไทย#1"])
def test_the_database_name_is_read_whatever_the_password_holds(password: str) -> None:
    # Wave 11B: URL parsing returned '' or part of the password as the database name.
    url = f"postgresql+psycopg://reader:{password}@192.0.2.97:5432/ppr_dev"
    assert database_name(url) == "ppr_dev"
    with pytest.raises(SettingsError, match="_test"):
        require_test_database(url)


@pytest.mark.parametrize("name", ["ppr_dev", "hospital", "ppr_test_backup", "postgres"])
def test_tests_refuse_non_test_databases(name: str) -> None:
    with pytest.raises(SettingsError, match="_test"):
        require_test_database(f"postgresql+psycopg://u:p@h/{name}")
    assert require_test_database("postgresql+psycopg://u:p@h/ppr_test")


# ------------------------------------------------------------------ permission policy
@pytest.mark.spec("S-22")
def test_every_permission_has_a_policy() -> None:
    assert set(ROLE_PERMISSIONS) == set(Permission)


@pytest.mark.spec("AT-40.10.4", "S-22.5", "S-22.6", "S-22.7")
def test_read_only_roles_hold_only_read_permissions() -> None:
    for perm, roles in ROLE_PERMISSIONS.items():
        if not perm.value.endswith("_READ"):
            assert not (roles & READ_ONLY_ROLES), perm


@pytest.mark.spec("AT-40.10.5", "S-22.8", "S-39")
def test_admin_has_no_plan_management_permission() -> None:
    assert not is_allowed([Role.ADMIN], Permission.PLAN_YEAR_MANAGE)
    assert is_allowed([Role.ADMIN], Permission.USER_ROLE_MANAGE)


def test_no_roles_means_no_permissions() -> None:
    assert not any(is_allowed([], p) for p in Permission)
