"""HOSxP login through the read-only SQL account with an MD5 password hash (D-25, §21).

Runs on the made-up schema (``mockhx_schema``) on PostgreSQL, and on MariaDB/MySQL when
PPR_TEST_HOSXP_MYSQL_URL is set (CI). The table/column names exist only in the test
query file, exactly as a hospital's own query file would name its real tables.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from test_hosxp_sql_gateway import _mysql_url

import mockhx_schema
from api_harness import Harness
from ppr.api.app import AppDeps, create_app
from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR
from ppr.integration.hosxp.auth import AuthFailure
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpUnavailableError
from ppr.integration.hosxp.real.query_config import OPERATIONS, Query, load_config
from ppr.integration.hosxp.real.sql_auth import SqlHosxpAuthenticationAdapter, md5_hex
from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway


@pytest.fixture(scope="module")
def query_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    f = tmp_path_factory.mktemp("hxauth") / "hosxp_queries.toml"
    f.write_text(mockhx_schema.QUERIES_TOML, encoding="utf-8")
    return f


@pytest.fixture(params=["postgresql", "mysql"])
def hx_engine(request: pytest.FixtureRequest, db_url: str, empty_db: Engine) -> Iterator[Engine]:
    url = db_url if request.param == "postgresql" else _mysql_url()
    engine = create_engine(url)
    mockhx_schema.create(engine)
    yield engine
    mockhx_schema.drop(engine)
    engine.dispose()


@pytest.fixture
def auth(hx_engine: Engine, query_file: Path) -> Iterator[SqlHosxpAuthenticationAdapter]:
    engine = create_engine(hx_engine.url)
    yield SqlHosxpAuthenticationAdapter(SqlHosxpGateway(engine, load_config(query_file)))
    engine.dispose()


@pytest.mark.spec("D-25", "S-21")
def test_the_right_password_logs_in_even_if_the_stored_hash_is_upper_case(
    auth: SqlHosxpAuthenticationAdapter,
) -> None:
    r = auth.authenticate("mock_ent", "Ent-Pass-1")
    assert r.ok and r.user_source_id == "MOCK-U-1"
    p = auth.get_user_profile("MOCK-U-1")
    assert p is not None
    assert (p.username, p.display_name, p.department_id, p.active) == (
        "mock_ent",
        "Mock ENT user",
        "MOCK-DEP-ENT",
        True,
    )


@pytest.mark.spec("D-25")
def test_thai_passwords_are_hashed_as_utf8(auth: SqlHosxpAuthenticationAdapter) -> None:
    assert auth.authenticate("mock_thai", "รหัสไทย9").ok


@pytest.mark.spec("D-25", "S-21")
@pytest.mark.parametrize(
    ("username", "password", "failure"),
    [
        ("mock_ent", "ent-pass-1", AuthFailure.INVALID_CREDENTIALS),  # case matters
        ("mock_ent", "", AuthFailure.INVALID_CREDENTIALS),
        ("nobody", "Ent-Pass-1", AuthFailure.INVALID_CREDENTIALS),
        ("", "x", AuthFailure.INVALID_CREDENTIALS),
        # the hash itself is not a password (no pass-the-hash)
        ("mock_ent", md5_hex("Ent-Pass-1"), AuthFailure.INVALID_CREDENTIALS),
        ("mock_off", "Off-Pass-2", AuthFailure.ACCOUNT_INACTIVE),
    ],
)
def test_wrong_unknown_or_inactive_logins_are_refused(
    auth: SqlHosxpAuthenticationAdapter, username: str, password: str, failure: AuthFailure
) -> None:
    r = auth.authenticate(username, password)
    assert not r.ok and r.failure is failure


def test_a_query_returning_two_users_is_a_configuration_error(
    auth: SqlHosxpAuthenticationAdapter,
) -> None:
    gw = auth.gateway
    q = dict(gw.config.queries)
    q["get_login_user"] = Query(
        OPERATIONS["get_login_user"],
        "SELECT user_key AS source_id, login_name AS username, pwd_digest AS password_hash "
        "FROM mockhx_user WHERE login_name <> :username",
    )
    gw.config = replace(gw.config, queries=MappingProxyType(q))
    with pytest.raises(HosxpConfigurationError, match="at most one"):
        auth.authenticate("mock_ent", "Ent-Pass-1")


@pytest.mark.spec("S-26")
def test_hosxp_down_at_login_is_unavailable_not_a_wrong_password(query_file: Path) -> None:
    dead = create_engine("postgresql+psycopg://x:y@127.0.0.1:1/none")
    adapter = SqlHosxpAuthenticationAdapter(SqlHosxpGateway(dead, load_config(query_file)))
    with pytest.raises(HosxpUnavailableError):
        adapter.authenticate("mock_ent", "Ent-Pass-1")


@pytest.mark.spec("D-25", "S-21")
def test_login_through_the_api_never_keeps_the_password_or_hash(
    auth: SqlHosxpAuthenticationAdapter, db: Engine
) -> None:
    hx = Harness(db)
    hx.client = TestClient(
        create_app(AppDeps(db, auth, hx.signer, DEFAULT_FISCAL_YEAR, hx.gateway))
    )
    ok = hx.client.post("/api/auth/login", json={"username": "mock_ent", "password": "Ent-Pass-1"})
    assert ok.status_code == 200, ok.text
    me = hx.client.get("/api/me", headers=hx.h(ok.json()["token"])).json()
    assert (me["username"], me["department_source_id"]) == ("mock_ent", "MOCK-DEP-ENT")
    bad = hx.client.post("/api/auth/login", json={"username": "mock_ent", "password": "nope"})
    assert bad.status_code == 401
    secrets = ("Ent-Pass-1", md5_hex("Ent-Pass-1"), md5_hex("Ent-Pass-1").upper())
    with db.connect() as conn:
        dump = " ".join(
            str(r)
            for t in ("app_user", "audit_log")
            for r in conn.execute(text(f"SELECT * FROM {t}"))
        )
    assert not any(s in dump for s in secrets)
