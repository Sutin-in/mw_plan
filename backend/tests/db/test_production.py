"""Wave 10A: production configuration (spec §38, §39; ADR-004 hardening).

The full role split (schema owner + least-privilege runtime account) is exercised end to end by
tools/dev/drill_production.py, which needs a PostgreSQL superuser to create the roles; these
tests cover the rules themselves on the test database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

from ppr.config.settings import Settings, SettingsError
from ppr.infrastructure.db.privileges import (
    append_only_tables,
    grant_statements,
    runtime_account_issues,
)
from ppr.ops_status import run as ops_status
from ppr.server import build, is_loopback

EVIDENCE = {
    "audit_log",
    "plan_amendment",
    "plan_amendment_change",
    "plan_ledger",
    "ppr_binding",
    "ppr_sync_observation",
    "ppr_verification",
    "ppr_version",
}


def _settings(**over: Any) -> Settings:
    base = Settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/ppr",
        test_database_url=None,
        session_secret="s" * 40,
        session_ttl_minutes=60,
    )
    return Settings(**{**base.__dict__, **over})


@pytest.mark.spec("S-39", "S-38")
def test_profile_and_role_settings_are_checked() -> None:
    env = {"PPR_PROFILE": "Production", "PPR_RUNTIME_ROLE": "ppr_runtime"}
    s = Settings.from_mapping(env)
    assert s.production and s.runtime_role == "ppr_runtime"
    with pytest.raises(SettingsError, match="PPR_PROFILE"):
        Settings.from_mapping({"PPR_PROFILE": "staging"})
    with pytest.raises(SettingsError, match="PPR_RUNTIME_ROLE"):
        Settings.from_mapping({"PPR_RUNTIME_ROLE": 'x"; DROP TABLE y; --'})
    # production never migrates with the runtime account
    with pytest.raises(SettingsError, match="PPR_MIGRATION_DATABASE_URL"):
        _settings(profile="production").migration_url()
    owner = "postgresql+psycopg://owner:p@localhost:5432/ppr"
    assert _settings(profile="production", migration_database_url=owner).migration_url() == owner
    assert _settings().migration_url() == _settings().database_url  # development: one account


@pytest.mark.spec("S-39")
@pytest.mark.parametrize(
    ("over", "demo_flag", "message"),
    [
        ({"profile": "production"}, True, "demo mode is not allowed"),
        (
            {"profile": "production", "database_url": "postgresql+psycopg://u:p@h/ppr_dev"},
            False,
            "development or test",
        ),
    ],
)
def test_production_refuses_demo_and_development_databases(
    over: dict[str, Any], demo_flag: bool, message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        build(_settings(**over), demo=demo_flag, env={})


@pytest.mark.spec("S-39")
def test_the_api_listens_on_this_machine_only_in_production() -> None:
    assert all(is_loopback(h) for h in ("127.0.0.1", "localhost", "::1", "127.0.0.2"))
    assert not any(is_loopback(h) for h in ("0.0.0.0", "10.0.0.5", "ppr.hospital.local"))


@pytest.mark.spec("S-39", "S-36")
def test_runtime_rights_keep_evidence_tables_append_only(db: Engine) -> None:
    with db.connect() as conn:
        found = set(append_only_tables(conn))
    assert found == EVIDENCE
    stmts = grant_statements(
        "ppr_runtime", sorted(found | {"ppr", "alembic_version"}), sorted(found)
    )
    by_table = {s.split(" ON ")[1].split(" TO ")[0]: s for s in stmts if s.startswith("GRANT ")}
    for t in EVIDENCE:
        assert by_table[f'public."{t}"'].startswith("GRANT SELECT, INSERT ON")
    assert by_table['public."ppr"'].startswith("GRANT SELECT, INSERT, UPDATE, DELETE ON")
    assert by_table['public."alembic_version"'].startswith("GRANT SELECT ON")
    assert stmts[0].startswith("REVOKE ALL ON ALL TABLES")


@pytest.mark.spec("S-39")
def test_the_schema_owner_is_not_accepted_as_the_runtime_account(db: Engine) -> None:
    # The test database's account owns the schema: exactly what production refuses.
    with db.connect() as conn:
        issues = runtime_account_issues(conn)
    assert any("owns" in i for i in issues)


@pytest.mark.spec("S-25.3", "S-38")
def test_ops_status_needs_a_recent_successful_nightly_run(
    db: Engine, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _settings(database_url=db_url)
    assert ops_status(s, 26) == 1
    assert "no successful run yet" in capsys.readouterr().out
    now = datetime.now(UTC)
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO sync_run (mode, scope, status, started_at, finished_at) "
                "VALUES ('NIGHTLY', 'PPRS', 'SUCCEEDED', :t, :t)"
            ),
            {"t": now - timedelta(hours=2)},
        )
    assert ops_status(s, 26, now=now) == 0
    out = capsys.readouterr().out
    assert "PASS  nightly PR synchronization" in out and "ops-status: PASS" in out
    assert ops_status(s, 1, now=now) == 1  # older than the limit
    out += capsys.readouterr().out
    secret = make_url(db_url).password
    assert not secret or secret not in out  # no password printed
