"""Wave 11A (D-41): sign-in reads a separate HOSxP login server.

The made-up HOSxP (``mockhx_*``) is created twice in the test database: the stock server in
the ``public`` schema, and a login server in its own schema ``mockhx_login`` holding only a
user table, reached through a second URL whose search path is that schema. A user that
exists only on the stock server must therefore be refused, and one that exists only on the
login server signs in - which proves which connection each query uses.
"""

from __future__ import annotations

import argparse
import hashlib
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url

import mockhx_schema
from ppr import cli
from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR
from ppr.config.settings import Settings
from ppr.ops_hosxp_verify import run as hosxp_verify

LOGIN_SCHEMA = "mockhx_login"
SPLIT_PASSWORD = "Split-Pass-1"
SPLIT_DIGEST = hashlib.md5(SPLIT_PASSWORD.encode(), usedforsecurity=False).hexdigest().upper()


def _login_url(db_url: str, *, port: int | None = None) -> str:
    url = make_url(db_url).update_query_dict({"options": f"-csearch_path={LOGIN_SCHEMA}"})
    if port is not None:
        url = url.set(port=port)
    return url.render_as_string(hide_password=False)


@pytest.fixture
def servers(db: Engine, db_url: str, tmp_path: Path) -> Iterator[dict[str, Any]]:
    mockhx_schema.create(db)  # the stock server (public schema), with its own user table
    with db.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {LOGIN_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {LOGIN_SCHEMA}"))
        conn.execute(
            text(
                f"CREATE TABLE {LOGIN_SCHEMA}.mockhx_user (user_key VARCHAR(30) PRIMARY KEY,"
                " login_name VARCHAR(50), full_name VARCHAR(100), dep_code VARCHAR(30),"
                " pwd_digest VARCHAR(64), is_active INT)"
            )
        )
        conn.execute(
            text(
                f"INSERT INTO {LOGIN_SCHEMA}.mockhx_user VALUES "
                "('MOCK-U-9', 'split_user', 'Mock split user', NULL, :d, 1)"
            ),
            {"d": SPLIT_DIGEST},
        )
    queries = tmp_path / "hosxp_queries.toml"
    queries.write_text(mockhx_schema.QUERIES_TOML, encoding="utf-8")
    settings = Settings(
        database_url=db_url,
        test_database_url=None,
        session_secret="s" * 40,
        session_ttl_minutes=60,
        hosxp_mode="sql",
        hosxp_database_url=db_url,
        hosxp_auth_database_url=_login_url(db_url),
        hosxp_queries_file=queries,
    )
    yield {"settings": settings, "db_url": db_url}
    with db.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {LOGIN_SCHEMA} CASCADE"))
    mockhx_schema.drop(db)


def _with(settings: Settings, **over: Any) -> Settings:
    return Settings(**{**settings.__dict__, **over})


def _login(client: TestClient, user: str, password: str) -> Any:
    return client.post("/api/auth/login", json={"username": user, "password": password})


@pytest.mark.spec("D-41", "D-40", "D-25", "S-21", "AT-40.15.1")
def test_sign_in_reads_only_the_login_server(servers: dict[str, Any]) -> None:
    from ppr.server import build

    s = servers["settings"]
    built = build(s, demo=False, env={})
    try:
        assert any("separate login server" in n for n in built.notes)
        client = TestClient(built.app)
        ok = _login(client, "split_user", SPLIT_PASSWORD)
        assert ok.status_code == 200, ok.text
        me = client.get("/api/me", headers={"Authorization": f"Bearer {ok.json()['token']}"})
        assert me.status_code == 200 and me.json()["department_source_id"] is None
        # mock_ent exists only on the stock server: the login server does not know it.
        assert _login(client, "mock_ent", "Ent-Pass-1").status_code == 401
        assert _login(client, "split_user", "wrong").status_code == 401
    finally:
        built.engine.dispose()
    # Without the second URL, sign-in reads the one HOSxP database, as before (Wave 9).
    single = build(_with(s, hosxp_auth_database_url=None), demo=False, env={})
    try:
        client = TestClient(single.app)
        assert _login(client, "mock_ent", "Ent-Pass-1").status_code == 200
        assert _login(client, "split_user", SPLIT_PASSWORD).status_code == 401
    finally:
        single.engine.dispose()
    # The login server down: 503, never another way in (D-40).
    down = build(
        _with(s, hosxp_auth_database_url=_login_url(servers["db_url"], port=1)),
        demo=False,
        env={},
    )
    try:
        client = TestClient(down.app)
        for user, pw in (("split_user", SPLIT_PASSWORD), ("mock_ent", "Ent-Pass-1")):
            r = _login(client, user, pw)
            assert r.status_code == 503 and r.json()["detail"]["code"] == "HOSXP_UNAVAILABLE"
    finally:
        down.engine.dispose()


def _check(settings: Settings, user: str | None = None, pr: str | None = None) -> int:
    args = argparse.Namespace(cmd="hosxp-check", pr=pr, user=user)
    return cli._hosxp_command(args, settings)


@pytest.mark.spec("D-41", "D-25", "AT-40.15.5")
def test_hosxp_check_tests_the_login_server_without_showing_secrets(
    servers: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    s = servers["settings"]
    assert _check(s, user="split_user") == 0
    out = capsys.readouterr().out
    assert "HOSxP login database (D-41): postgresql" in out
    assert "login database: connected OK" in out
    assert "user split_user: found (source_id MOCK-U-9); active: True; department: none" in out
    assert "password hash: 32 hex characters (MD5 form)" in out
    assert "get_user_profile: found" in out
    # never the hash, the password, a person's name or the account's password
    password = make_url(servers["db_url"]).password
    for secret in (SPLIT_DIGEST, SPLIT_DIGEST.lower(), SPLIT_PASSWORD, "Mock split user"):
        assert secret not in out
    assert password and password not in out
    # The PR and master queries read the stock server (the login server has no PR tables).
    assert _check(s, user="split_user", pr="MOCK-SQL-0001") == 0
    out = capsys.readouterr().out
    assert '"pr_no": "MOCK-SQL-0001"' in out and "get_items: " in out and "row(s) OK" in out
    assert _check(s, user="mock_ent") == 1  # only on the stock server
    assert "user mock_ent: not found by get_login_user" in capsys.readouterr().out
    gone = _with(s, hosxp_auth_database_url=_login_url(servers["db_url"], port=1))
    assert _check(gone) == 1
    assert "login database: FAILED - cannot connect" in capsys.readouterr().out


@pytest.mark.spec("D-41", "D-40", "AT-40.15.5")
def test_hosxp_verify_checks_the_login_account_too(
    servers: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = servers["settings"]
    oct31 = date(2026, 10, 31)
    hosxp_verify(s, DEFAULT_FISCAL_YEAR, [], None, oct31)
    out = capsys.readouterr().out
    # the test account can write: both accounts are reported, each on its own line
    assert "[account] the HOSxP account can do more than read" in out
    assert "[login account] the HOSxP account can do more than read" in out
    assert "[login account] HOSxP login database postgresql" in out
    monkeypatch.setattr("ppr.ops_hosxp_verify.account_write_rights", lambda engine: [])
    hosxp_verify(s, DEFAULT_FISCAL_YEAR, [], None, oct31)
    out = capsys.readouterr().out
    assert "PASS  [login account] no right beyond reading" in out
    gone = _with(s, hosxp_auth_database_url=_login_url(servers["db_url"], port=1))
    monkeypatch.undo()
    assert hosxp_verify(gone, DEFAULT_FISCAL_YEAR, [], None, oct31) == 1
    assert "FAIL  [login account] cannot reach HOSxP" in capsys.readouterr().out
