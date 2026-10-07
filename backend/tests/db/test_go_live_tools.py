"""Wave 10B-2 (D-40): go-live tools and production safeguards.

* ``hosxp-verify``: read-only checks after IT connects HOSxP - PASS / FAIL / OPEN, OPEN never
  PASS; run against the made-up ``mockhx_*`` HOSxP on PostgreSQL and MariaDB;
* ``first-start --check``: read-only readiness of a production server;
* MOCK/DEMO data in production: WARN at start-up, in ops-status and on every page, never a
  failure, and never posted to the Plan Ledger;
* production login goes through HOSxP only: no fallback to demonstration users.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from test_hosxp_sql_gateway import _mysql_url

import mockhx_schema
from api_harness import FY, Harness, Plan
from ppr.application.errors import ConflictError
from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR
from ppr.config.settings import Settings, SettingsError
from ppr.domain.ledger import LedgerEntry, LedgerEventType
from ppr.infrastructure.db.mockdata import mock_data_summary, production_engine
from ppr.infrastructure.db.plan_repositories import SqlLedgerRepository
from ppr.ops_first_start import run as first_start
from ppr.ops_hosxp_verify import run as hosxp_verify
from ppr.ops_report import CheckReport
from ppr.ops_status import run as ops_status

OCT31 = date(2026, 10, 31)  # inside fiscal year 2570


# ------------------------------------------------------------------ helpers
def _queries(tmp_path: Path, body: str = mockhx_schema.QUERIES_TOML) -> Path:
    f = tmp_path / "hosxp_queries.toml"
    f.write_text(body, encoding="utf-8")
    return f


def _settings(db_url: str, queries: Path, **over: Any) -> Settings:
    base = Settings(
        database_url=db_url,
        test_database_url=None,
        session_secret="s" * 40,
        session_ttl_minutes=60,
        hosxp_mode="sql",
        hosxp_database_url=db_url,
        hosxp_queries_file=queries,
    )
    return Settings(**{**base.__dict__, **over})


@pytest.fixture(params=["postgresql", "mysql"])
def hx_url(request: pytest.FixtureRequest, db_url: str, empty_db: Engine) -> Iterator[str]:
    url = db_url if request.param == "postgresql" else _mysql_url()
    engine = create_engine(url)
    mockhx_schema.create(engine)
    yield url
    mockhx_schema.drop(engine)
    engine.dispose()


def _lines(out: str) -> list[str]:
    return [
        line
        for line in out.splitlines()
        if line[:4] in ("PASS", "FAIL", "WARN", "OPEN", "TODO", "INFO")
    ]


def _has(out: str, state: str, *words: str) -> bool:
    return any(line.startswith(state) and all(w in line for w in words) for line in _lines(out))


# ------------------------------------------------------------------ hosxp-verify
@pytest.mark.spec("AT-40.14.1", "D-40", "S-24", "S-47")
def test_hosxp_verify_reads_the_site_queries_and_reports_open_items_as_open(
    hx_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_file = tmp_path / "verify.md"
    s = _settings(hx_url, _queries(tmp_path))
    code = hosxp_verify(s, DEFAULT_FISCAL_YEAR, ["MOCK-SQL-0001", "MOCK-SQL-0002"], out_file, OCT31)
    out = capsys.readouterr().out
    # the test account can write: a real FAIL, so the command exits 1
    assert _has(out, "FAIL", "[account]", "more than read") and code == 1
    assert _has(out, "PASS", "[query file]", "missing queries: none")
    for kind in ("departments", "items", "fund sources", "budget categories"):
        assert _has(out, "PASS", "[masters]", kind)
    assert _has(out, "PASS", "[PR MOCK-SQL-0001]", "fiscal year 2570")
    assert _has(out, "PASS", "[PR MOCK-SQL-0001]", "department_id matches")
    assert _has(out, "PASS", "[PR MOCK-SQL-0001]", "every item_id matches")
    assert _has(out, "INFO", "[PR MOCK-SQL-0001]", "'A1' -> ACTIVE")
    assert _has(out, "INFO", "[PR MOCK-SQL-0002]", "differs", "D-17")
    # a mapping in the file is not proof: still OPEN, with what is configured
    assert _has(out, "OPEN", "[PR cancellation (D-30)]", "'X9'", "evidence pending")
    assert _has(out, "OPEN", "[G-4]") and _has(out, "OPEN", "[login (D-25)]")
    assert _has(out, "INFO", "[stock (D-39)]", "recorded on 2026-10-01", "MOCK-REF-SQL")
    assert "MOCK IT" not in out  # who verified is a person: not printed
    assert _has(out, "OPEN", "[stock (D-39)]", "unmapped kind")  # the ADJ movement
    assert _has(out, "PASS", "[stock (D-39)]", "ids match the masters")
    # no names of people, no secret
    assert "Mock requester" not in out
    password = make_url(hx_url).password
    assert password and password not in out
    report = out_file.read_text(encoding="utf-8")
    assert "## OPEN items (not PASS)" in report and "[G-4]" in report
    assert "Mock requester" not in report and password not in report and "MOCK IT" not in report
    assert "[PR cancellation (D-30)]" in report.split("## OPEN items (not PASS)")[1]


@pytest.mark.spec("AT-40.14.1", "D-40")
def test_hosxp_verify_never_summarises_open_items_as_pass(
    hx_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # As with a correctly read-only HOSxP account (the test account itself can write).
    monkeypatch.setattr("ppr.ops_hosxp_verify.account_write_rights", lambda engine: [])
    code = hosxp_verify(
        _settings(hx_url, _queries(tmp_path)), DEFAULT_FISCAL_YEAR, ["MOCK-SQL-0001"], None, OCT31
    )
    out = capsys.readouterr().out
    assert code == 0  # no FAIL
    assert _has(out, "PASS", "[account]", "no right beyond reading")
    summary = out.strip().splitlines()[-1]
    assert summary.startswith("hosxp-verify: NO FAIL") and "OPEN" in summary
    assert "(not PASS)" in summary and not summary.startswith("hosxp-verify: PASS")


@pytest.mark.spec("D-40", "D-30", "D-39")
def test_hosxp_verify_without_mappings_reports_them_open(
    db_url: str, empty_db: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mockhx_schema.create(empty_db)
    body = mockhx_schema.QUERIES_TOML
    body = body[body.index("[queries.get_pr]") :]  # no [status_map]
    body = body[: body.index("[stock_movement]")] + body[body.index("[queries.get_user_profile]") :]
    hosxp_verify(
        _settings(db_url, _queries(tmp_path, body)),
        DEFAULT_FISCAL_YEAR,
        ["MOCK-SQL-0001"],
        None,
        OCT31,
    )
    out = capsys.readouterr().out
    assert _has(out, "OPEN", "[PR MOCK-SQL-0001]", "'A1' is not mapped")
    assert _has(out, "OPEN", "[PR cancellation (D-30)]", "no native status maps to CANCELLED")
    assert _has(out, "OPEN", "[stock (D-39)]", "ยังไม่ได้ตั้งค่า")
    assert not _has(out, "PASS", "[stock (D-39)]")


@pytest.mark.spec("D-40", "S-24")
def test_hosxp_verify_fails_ids_outside_the_masters_and_an_unreachable_hosxp(
    db_url: str, empty_db: Engine, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mockhx_schema.create(empty_db)
    body = mockhx_schema.QUERIES_TOML.replace(
        "p.dep_code AS department_id", "CONCAT('X-', p.dep_code) AS department_id"
    )
    code = hosxp_verify(
        _settings(db_url, _queries(tmp_path, body)),
        DEFAULT_FISCAL_YEAR,
        ["MOCK-SQL-0001"],
        None,
        OCT31,
    )
    out = capsys.readouterr().out
    assert code == 1 and _has(out, "FAIL", "[PR MOCK-SQL-0001]", "department_id", "code space")
    gone = make_url(db_url).set(port=1).render_as_string(hide_password=False)
    code = hosxp_verify(
        _settings(db_url, _queries(tmp_path), hosxp_database_url=gone),
        DEFAULT_FISCAL_YEAR,
        [],
        None,
        OCT31,
    )
    out = capsys.readouterr().out
    assert code == 1 and _has(out, "FAIL", "[account]", "cannot reach HOSxP")
    assert _has(out, "FAIL", "[masters]")


def test_check_report_states() -> None:
    r = CheckReport("x")
    r.add("PASS", "a", "ok")
    assert r.summary() == "x: PASS"
    r.add("OPEN", "a", "evidence missing")
    assert r.summary() == "x: NO FAIL - 1 OPEN (not PASS)"
    r.add("WARN", "a", "mock")
    assert r.summary() == "x: NO FAIL - 1 WARN, 1 OPEN (not PASS)"
    r.add("FAIL", "a", "bad")
    assert r.summary() == "x: FAIL" and r.failed
    with pytest.raises(AssertionError):
        r.add("OK", "a", "unknown state")


# ------------------------------------------------------------------ first-start --check
GOOD_UI_ENV = {
    "PPR_UI_SECURE_COOKIES": "1",
    "PPR_UI_TRUST_PROXY": "loopback",
    "PPR_UI_HOST": "127.0.0.1",
    "PPR_API_URL": "http://127.0.0.1:8000",
    "PPR_BACKUP_DIR": "D:\\ppr\\backups",
}


def _row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as c:
        tables = [
            r[0]
            for r in c.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() ORDER BY 1"
                )
            )
        ]
        return {t: c.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}


@pytest.mark.spec("AT-40.14.2", "D-40", "S-39")
def test_first_start_check_reports_without_changing_anything(
    db: Engine, db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    before = _row_counts(db)
    out_file = tmp_path / "first.md"
    s = _settings(
        db_url,
        _queries(tmp_path),
        profile="production",
        migration_database_url=db_url,
    )
    code = first_start(s, GOOD_UI_ENV, out_file)
    out = capsys.readouterr().out
    assert code == 1  # the test database is a *_test one, owned by the test account
    assert _has(out, "PASS", "[settings]", "PPR_PROFILE=production")
    assert _has(out, "FAIL", "[settings]", "development / test")
    assert _has(out, "PASS", "[settings]", "user interface behind HTTPS")
    assert _has(out, "PASS", "[database]", "(latest)")
    assert _has(out, "FAIL", "[database]", "least-privilege")
    assert _has(out, "PASS", "[MOCK/DEMO (D-40)]", "no MOCK/DEMO")
    assert _has(out, "TODO", "[first start]", "grant-role")
    assert _has(out, "TODO", "[first start]", "masters")
    assert _has(out, "OPEN", "[G-4]")
    assert _has(out, "OPEN", "[PR cancellation (D-30)]", "evidence pending")
    assert _has(out, "INFO", "[stock (D-39)]")
    assert "OPEN items (not PASS)" in out_file.read_text(encoding="utf-8")
    assert _row_counts(db) == before  # read only
    # development settings and an unprotected interface fail
    code = first_start(_settings(db_url, _queries(tmp_path)), {}, None)
    out = capsys.readouterr().out
    assert code == 1 and _has(out, "FAIL", "[settings]", "PPR_PROFILE is 'development'")
    assert _has(out, "FAIL", "[settings]", "PPR_UI_SECURE_COOKIES=1")
    assert _has(out, "TODO", "[settings]", "PPR_BACKUP_DIR")


# ------------------------------------------------------------------ MOCK/DEMO in production
def _mock_plan(hx: Harness) -> tuple[Plan, int]:
    p = Plan(hx)
    p.sync()
    p.year()
    budget = p.budget("MOCK-CAT-OFFICE", "1000").json()["id"]
    r = p.item(budget, "1000")
    assert r.status_code == 201, r.text
    assert p.move("APPROVED").status_code == 200
    return p, int(r.json()["id"])


@pytest.mark.spec("AT-40.14.3", "D-40", "S-18")
def test_mock_data_never_reaches_the_plan_ledger_in_production(db: Engine) -> None:
    hx = Harness(db)
    p, item = _mock_plan(hx)
    entry = LedgerEntry(str(item), LedgerEventType.PLAN_ACTIVATED, Decimal(10), Decimal(1000), "k")
    with pytest.raises(ConflictError) as err, production_engine(db).begin() as conn:
        SqlLedgerRepository(conn).append(entry, None)
    assert err.value.code == "MOCK_DATA_IN_PRODUCTION"
    # Through the API of a production server: activation is refused, nothing is posted.
    prod = TestClient(_app(production_engine(db), hx, production=True))
    r = prod.post(f"/api/plan-years/{FY}/transition", json={"target": "ACTIVE"}, headers=p.h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "MOCK_DATA_IN_PRODUCTION"
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM plan_ledger")).scalar_one() == 0
        assert c.execute(text("SELECT state FROM plan_year")).scalar_one() == "APPROVED"
    # A development server is unchanged (the demonstration works as before).
    assert p.move("ACTIVE").status_code == 200


def _app(engine: Engine, hx: Harness, *, production: bool) -> Any:
    from ppr.api.app import AppDeps, create_app

    return create_app(
        AppDeps(
            engine,
            hx.adapter,
            hx.signer,
            DEFAULT_FISCAL_YEAR,
            hx.gateway,
            today=lambda: hx.today,
            production=production,
        )
    )


@pytest.mark.spec("AT-40.14.3", "D-40", "S-39")
def test_mock_data_in_production_is_a_warning_everywhere_never_a_failure(
    db: Engine, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    hx = Harness(db)
    _mock_plan(hx)
    with db.connect() as c:
        found = mock_data_summary(c)
    assert found.found and found.mock_masters > 0 and found.mock_plan_items == 1
    # every page: /api/info carries the Thai warning in production only
    info = TestClient(_app(db, hx, production=True)).get("/api/info").json()
    assert "MOCK/DEMO" in info["mock_data_warning"]
    assert (
        TestClient(_app(db, hx, production=False)).get("/api/info").json()["mock_data_warning"]
        is None
    )
    # ops-status: WARN in production, INFO in development; never the cause of a FAIL
    with db.begin() as c:
        c.execute(
            text(
                "INSERT INTO sync_run (mode, scope, status, started_at, finished_at) "
                "VALUES ('NIGHTLY', 'PPRS', 'SUCCEEDED', now(), now())"
            )
        )
    dev = Settings(
        database_url=db_url, test_database_url=None, session_secret=None, session_ttl_minutes=60
    )
    assert ops_status(dev, 26) == 0
    out = capsys.readouterr().out
    assert _has(out, "INFO", "MOCK/DEMO data") and "ops-status: PASS" in out
    prod = Settings(**{**dev.__dict__, "profile": "production"})
    ops_status(prod, 26)
    out = capsys.readouterr().out
    assert _has(out, "WARN", "MOCK/DEMO data", "never posted to the Plan Ledger")
    fails = [line for line in _lines(out) if line.startswith("FAIL")]
    assert fails and all("least-privilege" in f for f in fails)  # the test account, not MOCK


# ------------------------------------------------------------------ production start-up / login
@pytest.fixture
def prod_server(
    db: Engine, db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Any]:
    """``server.build`` in production on the test database, with the made-up HOSxP: the two
    rules the test database cannot meet (its name, its owner account) are lifted."""
    monkeypatch.setattr("ppr.server.database_name", lambda url: "ppr")
    monkeypatch.setattr("ppr.server.runtime_account_issues", lambda conn: [])
    mockhx_schema.create(db)

    def make(**over: Any) -> Any:
        from ppr.server import build

        s = _settings(db_url, _queries(tmp_path), profile="production", **over)
        return build(s, demo=False, env={})

    yield make
    mockhx_schema.drop(db)


@pytest.mark.spec("AT-40.14.4", "D-40", "D-25", "S-21")
def test_production_signs_in_through_hosxp_only(prod_server: Any, db: Engine) -> None:
    built = prod_server()
    client = TestClient(built.app)
    ok = client.post("/api/auth/login", json={"username": "mock_ent", "password": "Ent-Pass-1"})
    assert ok.status_code == 200  # a HOSxP (made-up) account, checked against HOSxP
    for user in ("demo_req_ent", "demo_admin"):  # demonstration users never sign in
        r = client.post("/api/auth/login", json={"username": user, "password": "demo1234"})
        assert r.status_code == 401
    built.engine.dispose()
    # HOSxP unreachable: refused with 503, never another way in
    gone = make_url(db.url).set(port=1).render_as_string(hide_password=False)
    down = TestClient(prod_server(hosxp_database_url=gone).app)
    for user, pw in (("mock_ent", "Ent-Pass-1"), ("demo_req_ent", "demo1234")):
        r = down.post("/api/auth/login", json={"username": user, "password": pw})
        assert r.status_code == 503 and r.json()["detail"]["code"] == "HOSXP_UNAVAILABLE"


@pytest.mark.spec("AT-40.14.4", "D-40", "S-39")
def test_production_refuses_demo_and_mock_hosxp(
    prod_server: Any, db_url: str, tmp_path: Path
) -> None:
    from ppr.server import build

    s = _settings(db_url, _queries(tmp_path), profile="production")
    with pytest.raises(SettingsError, match="demo mode is not allowed"):
        build(s, demo=True, env={})
    with pytest.raises(SettingsError, match="mock HOSxP has no users"):
        build(Settings(**{**s.__dict__, "hosxp_mode": "mock"}), demo=False, env={})


@pytest.mark.spec("AT-40.14.3", "D-40")
def test_production_start_up_warns_about_mock_data(prod_server: Any, db: Engine) -> None:
    clean = prod_server()
    assert not any("MOCK/DEMO" in n for n in clean.notes)
    clean.engine.dispose()
    hx = Harness(db)
    _mock_plan(hx)
    built = prod_server()
    warning = [n for n in built.notes if n.startswith("WARNING")]
    assert warning and "MOCK/DEMO" in warning[0] and "Plan Ledger" in warning[0]
    assert "MOCK/DEMO" in TestClient(built.app).get("/api/info").json()["mock_data_warning"]
    built.engine.dispose()


# ------------------------------------------------------------------ the hand-over documents
DOCS = Path(__file__).resolve().parents[3] / "docs"


@pytest.mark.spec("D-40", "AT-40.14.1")
def test_go_live_checklist_keeps_open_items_apart_and_never_as_pass() -> None:
    text_ = (DOCS / "GO_LIVE_CHECKLIST.md").read_text(encoding="utf-8")
    section_c = text_[text_.index("## C.") : text_.index("## D.")]
    for item in ("G-4", "D-30", "D-39"):
        row = next(line for line in section_c.splitlines() if item in line and line.startswith("|"))
        assert "**OPEN**" in row, item
    assert "PASS" not in section_c.replace("ไม่ใช่ PASS", "").replace("ห้ามทำเครื่องหมายว่า PASS", "")
    section_d = text_[text_.index("## D.") :]
    assert "ไม่ใช่ PASS" in section_d and "เจ้าของระบบ" in section_d
    # every item of section A asks for evidence
    section_a = text_[text_.index("## A.") : text_.index("## B.")]
    rows = [r for r in section_a.splitlines() if r.startswith("| A")]
    assert len(rows) >= 10 and all(r.count("|") >= 6 for r in rows)


@pytest.mark.spec("D-40")
def test_hand_over_documents_name_only_commands_that_exist() -> None:
    import re

    from ppr.cli import main

    for doc in ("IT_HANDOFF.md", "GO_LIVE_CHECKLIST.md"):
        named = set(re.findall(r"ppr\.cli ([a-z][a-z-]+)", (DOCS / doc).read_text("utf-8")))
        assert named, doc
        for cmd in named:
            with pytest.raises(SystemExit) as ex:
                main([cmd, "--help"])
            assert ex.value.code == 0, (doc, cmd)


# ------------------------------------------------------------------ the HOSxP account check
@pytest.mark.spec("AT-40.14.1", "D-40")
@pytest.mark.parametrize(
    ("grants", "bad"),
    [
        (["GRANT USAGE ON *.* TO `r`@`%`", "GRANT SELECT ON `hosxp`.* TO `r`@`%`"], []),
        (["GRANT SELECT, SHOW VIEW ON `hosxp`.* TO `r`@`h`", "SET DEFAULT ROLE NONE FOR r"], []),
        (["GRANT SELECT (`a`, `b`), UPDATE (`b`) ON `db`.`t` TO `r`@`%`"], ["UPDATE"]),
        (
            ["GRANT CREATE USER, DELETE HISTORY, CREATE TEMPORARY TABLES ON *.* TO `r`@`%`"],
            ["CREATE TEMPORARY TABLES", "CREATE USER", "DELETE HISTORY"],
        ),
        (["GRANT SELECT ON `db`.* TO `r`@`%` WITH GRANT OPTION"], ["GRANT OPTION"]),
        (["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost`"], ["ALL PRIVILEGES"]),
        (["GRANT PROXY ON ''@'%' TO 'r'@'%'"], ["PROXY"]),
        (["GRANT BINLOG_ADMIN ON *.* TO `r`@`%`"], ["BINLOG_ADMIN"]),  # MySQL 8 dynamic
        (["GRANT `writer`@`%` TO `r`@`%`"], ["membership of a role (check that role's rights)"]),
    ],
)
def test_mysql_grants_are_read_only_only_when_every_right_is_a_read_right(
    grants: list[str], bad: list[str]
) -> None:
    from ppr.ops_hosxp_verify import mysql_grant_problems

    assert mysql_grant_problems(grants) == bad


@pytest.mark.spec("AT-40.14.1", "D-40", "D-41")
def test_postgresql_account_check_on_real_roles(db: Engine, db_url: str) -> None:
    """A real read-only role passes; write rights through a view, a column, a sequence or
    the database fail. Needs an account that may create roles (CI); skipped otherwise."""
    from ppr.ops_hosxp_verify import account_write_rights

    role, pw = "ppr_verify_ro", "Verify-Ro-1"
    with db.connect() as c:
        if not c.execute(
            text("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user")
        ).scalar_one():
            pytest.skip("the test database account may not create roles")
    admin = db.execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"DROP ROLE IF EXISTS {role}"))
        c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}'"))
        c.execute(text("CREATE TABLE ro_t (a int, b int)"))
        c.execute(text("CREATE VIEW ro_v AS SELECT a, b FROM ro_t"))
        c.execute(text("CREATE SEQUENCE ro_s"))
        c.execute(text(f"GRANT CONNECT ON DATABASE {make_url(db_url).database} TO {role}"))
        c.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        c.execute(text(f"GRANT SELECT ON ro_t, ro_v TO {role}"))
    ro = create_engine(make_url(db_url).set(username=role, password=pw))
    try:
        assert account_write_rights(ro) == []
        for grant, words in (
            (f"GRANT INSERT ON ro_v TO {role}", "table(s) / view(s)"),
            (f"GRANT UPDATE (b) ON ro_t TO {role}", "column(s)"),
            (f"GRANT USAGE ON SEQUENCE ro_s TO {role}", "sequence(s)"),
        ):
            with admin.connect() as c:
                c.execute(text(grant))
            assert any(words in f for f in account_write_rights(ro)), grant
        with admin.connect() as c:
            c.execute(text(f"REVOKE ALL ON ro_t, ro_v FROM {role}"))
            c.execute(text(f"REVOKE ALL ON SEQUENCE ro_s FROM {role}"))
            c.execute(text(f"GRANT SELECT ON ro_t, ro_v TO {role}"))
        assert account_write_rights(ro) == []
        # D-41: role attributes are not inherited but a member may SET ROLE to them.
        with admin.connect() as c:
            c.execute(text("DROP ROLE IF EXISTS ppr_verify_db"))
            c.execute(text("CREATE ROLE ppr_verify_db NOLOGIN CREATEDB"))
            c.execute(text(f"GRANT ppr_verify_db TO {role}"))
        assert "CREATEDB" in account_write_rights(ro)
        with admin.connect() as c:
            c.execute(text("DROP ROLE ppr_verify_db"))
        # D-41: a role it belongs to without inheriting it can be taken with SET ROLE.
        with admin.connect() as c:
            c.execute(text("DROP ROLE IF EXISTS ppr_verify_w"))
            c.execute(text("CREATE ROLE ppr_verify_w NOLOGIN"))
            c.execute(text("GRANT INSERT ON ro_t TO ppr_verify_w"))
            c.execute(text(f"ALTER ROLE {role} NOINHERIT"))
            c.execute(text(f"GRANT ppr_verify_w TO {role}"))
        assert account_write_rights(ro) == ["membership of 1 role(s) it can SET ROLE to"]
    finally:
        ro.dispose()
        with admin.connect() as c:
            for extra in ("ppr_verify_w", "ppr_verify_db"):
                if c.execute(
                    text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": extra}
                ).first():
                    c.execute(text(f"DROP OWNED BY {extra}"))
                    c.execute(text(f"DROP ROLE {extra}"))
            c.execute(text("DROP VIEW IF EXISTS ro_v"))
            c.execute(text("DROP TABLE IF EXISTS ro_t"))
            c.execute(text("DROP SEQUENCE IF EXISTS ro_s"))
            c.execute(text(f"DROP OWNED BY {role}"))
            c.execute(text(f"DROP ROLE IF EXISTS {role}"))


# ------------------------------------------------------------------ production engines
@pytest.mark.spec("AT-40.14.3", "D-40")
def test_every_production_writer_uses_the_guarded_engine(
    prod_server: Any, db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ppr import cli
    from ppr.infrastructure.db.mockdata import is_production

    built = prod_server()
    with built.engine.connect() as c:
        assert is_production(c)  # the API server
    built.engine.dispose()
    seen: list[bool] = []

    def fake_nightly(engine: Engine, gateway: Any) -> int:
        with engine.connect() as c:
            seen.append(is_production(c))
        return 0

    monkeypatch.setattr(cli, "run_nightly", fake_nightly)
    monkeypatch.setattr("ppr.server.require_current_schema", lambda engine: None)
    s = _settings(db_url, _queries(tmp_path), profile="production")
    assert cli._sync_prs(s) == 0 and seen == [True]  # the nightly PR synchronization
    seen.clear()
    assert cli._sync_prs(Settings(**{**s.__dict__, "profile": "development"})) == 0
    assert seen == [False]
