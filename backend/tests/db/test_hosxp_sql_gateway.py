"""The real (SQL) HOSxP adapter against a made-up schema, on PostgreSQL and MariaDB/MySQL.

Spec §24, §24.5, §25.1, §26, §42, §47; D-02, D-08, D-17, D-18.
MySQL/MariaDB runs when PPR_TEST_HOSXP_MYSQL_URL is set (CI provides MariaDB); otherwise
those cases are skipped with a visible reason. PostgreSQL always runs.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest
from sqlalchemy import Engine, create_engine, text

import mockhx_schema
from api_harness import Harness, Plan, check_pr
from ppr.config.settings import database_name, load_environment
from ppr.integration.hosxp.contracts import Department, PrHeader, PrItem
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpUnavailableError
from ppr.integration.hosxp.real.query_config import OPERATIONS, Query, QueryConfig, load_config
from ppr.integration.hosxp.real.schema_discovery import discover, to_markdown
from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway
from ppr.integration.hosxp.status import HosxpPrStatus

D = Decimal


@pytest.fixture(scope="module")
def query_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    f = tmp_path_factory.mktemp("hx") / "hosxp_queries.toml"
    f.write_text(mockhx_schema.QUERIES_TOML, encoding="utf-8")
    return f


def _mysql_url() -> str:
    url = load_environment().get("PPR_TEST_HOSXP_MYSQL_URL")
    if not url:
        pytest.skip("PPR_TEST_HOSXP_MYSQL_URL not set: MariaDB/MySQL adapter tests skipped")
    if not database_name(url).endswith("_test"):
        pytest.fail("PPR_TEST_HOSXP_MYSQL_URL must name a *_test database")
    return url


@pytest.fixture(params=["postgresql", "mysql"])
def hx_engine(request: pytest.FixtureRequest, db_url: str, empty_db: Engine) -> Iterator[Engine]:
    url = db_url if request.param == "postgresql" else _mysql_url()
    engine = create_engine(url)
    mockhx_schema.create(engine)
    yield engine
    mockhx_schema.drop(engine)
    engine.dispose()


@pytest.fixture
def gw(hx_engine: Engine, query_file: Path) -> Iterator[SqlHosxpGateway]:
    # The adapter gets its own engine, as in production: its sessions become read-only.
    engine = create_engine(hx_engine.url)
    yield SqlHosxpGateway(engine, load_config(query_file))
    engine.dispose()


# ------------------------------------------------------------------ contract mapping
@pytest.mark.spec("S-24", "S-24.5", "S-42", "D-02")
def test_pr_header_and_items_map_onto_the_contracts(gw: SqlHosxpGateway) -> None:
    h = gw.get_pr("MOCK-SQL-0001")
    assert h == PrHeader(
        pr_id="1",
        pr_no="MOCK-SQL-0001",
        pr_date=date(2026, 10, 15),
        fiscal_year=2570,
        department_id="MOCK-DEP-ENT",
        fund_source_id="MOCK-FUND-1",
        budget_category_id="MOCK-CAT-OFFICE",
        requester_name="Mock requester",
        total_amount=D("500.00"),
        status=HosxpPrStatus.ACTIVE,
        native_status="A1",
    )
    items = gw.get_pr_items("MOCK-SQL-0001")
    assert [(i.pr_item_id, i.item_id, i.qty, i.unit_price, i.amount) for i in items] == [
        ("11", "MOCK-ITEM-TONER", D("4"), D("100.00"), D("400.00")),
        ("12", "MOCK-ITEM-PAPER", D("2"), D("50.00"), D("100.00")),
    ]
    assert isinstance(items[0], PrItem) and items[0].unit == "box"


@pytest.mark.spec("S-24.5", "D-02")
def test_native_status_is_normalized_through_the_site_mapping(gw: SqlHosxpGateway) -> None:
    h = gw.get_pr("MOCK-SQL-0003")
    assert h is not None
    assert (h.status, h.native_status) == (HosxpPrStatus.CANCELLED, "X9")


def test_unknown_pr_is_none(gw: SqlHosxpGateway) -> None:
    assert gw.get_pr("MOCK-SQL-9999") is None
    assert list(gw.get_pr_items("MOCK-SQL-9999")) == []


@pytest.mark.spec("S-8", "S-24.1")
def test_masters_map_onto_the_contracts(gw: SqlHosxpGateway) -> None:
    deps = {d.source_id: d for d in gw.get_departments()}
    assert deps["MOCK-DEP-OLD"] == Department(
        source_id="MOCK-DEP-OLD", code="OLD", name="Mock closed", active=False
    )
    assert {i.source_id: i.unit for i in gw.get_items()}["MOCK-ITEM-PAPER"] == "ream"
    cats = {c.source_id: c.parent_source_id for c in gw.get_budget_categories()}
    assert cats == {"MOCK-CAT-MAT": None, "MOCK-CAT-OFFICE": "MOCK-CAT-MAT"}
    assert [f.source_id for f in gw.get_fund_sources()] == ["MOCK-FUND-1"]


# ------------------------------------------------------------------ safety
def _config_with(base: QueryConfig, op: str, sql: str) -> QueryConfig:
    q = dict(base.queries)
    q[op] = Query(OPERATIONS[op], sql)  # bypasses the load-time text check on purpose
    return QueryConfig(MappingProxyType(q), base.status_mapping)


@pytest.mark.spec("S-39")
def test_database_itself_refuses_writes_inside_the_read_only_transaction(
    gw: SqlHosxpGateway, hx_engine: Engine
) -> None:
    # A query that slipped past the text check still cannot change data: the transaction
    # is READ ONLY at the database level.
    with hx_engine.begin() as conn:
        conn.execute(text("CREATE TABLE mockhx_counter (n INT)"))
    try:
        if hx_engine.dialect.name == "postgresql":
            # A statement that WOULD insert a row if the transaction were not read-only.
            writer = (
                "WITH w AS (INSERT INTO mockhx_counter VALUES (1) RETURNING n) "
                "SELECT 'x' AS source_id, 'x' AS code, 'x' AS name, true AS active FROM w"
            )
            gw.config = _config_with(gw.config, "get_departments", writer)
            with pytest.raises(HosxpConfigurationError):
                gw.get_departments()
        else:
            # MySQL has no writing SELECT; prove the session the adapter uses is read-only.
            gw.get_departments()
            # The adapter's own (now read-only) session refuses a direct write.
            with gw.engine.connect() as c, pytest.raises(Exception, match=r"READ ONLY|1792"):
                c.exec_driver_sql("INSERT INTO mockhx_counter VALUES (1)")
        with hx_engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM mockhx_counter")).scalar_one() == 0
    finally:
        with hx_engine.begin() as conn:
            conn.execute(text("DROP TABLE mockhx_counter"))


def test_wrong_columns_and_broken_queries_fail_closed(gw: SqlHosxpGateway) -> None:
    base = gw.config
    gw.config = _config_with(
        base, "get_fund_sources", "SELECT fund_code AS source_id FROM mockhx_fund"
    )
    with pytest.raises(HosxpConfigurationError, match="missing column"):
        gw.get_fund_sources()
    gw.config = _config_with(
        base,
        "get_fund_sources",
        "SELECT fund_code AS source_id, fund_abbr AS code, fund_name AS name, "
        "is_active AS active, fund_name AS patient_name FROM mockhx_fund",
    )
    with pytest.raises(HosxpConfigurationError, match="unexpected column"):
        gw.get_fund_sources()
    gw.config = _config_with(base, "get_fund_sources", "SELECT * FROM mockhx_no_such_table")
    with pytest.raises(HosxpConfigurationError):
        gw.get_fund_sources()
    # A configuration error is a kind of "unavailable": callers refuse either way.
    assert issubclass(HosxpConfigurationError, HosxpUnavailableError)


def test_one_pr_number_must_yield_one_header(gw: SqlHosxpGateway) -> None:
    gw.config = _config_with(
        gw.config,
        "get_pr",
        "SELECT CONCAT('', pr_key) AS pr_id, pr_number AS pr_no FROM mockhx_pr "
        "WHERE pr_number = :pr_no OR pr_key > 0",
    )
    with pytest.raises(HosxpConfigurationError, match="must return one"):
        gw.get_pr("MOCK-SQL-0001")


@pytest.mark.spec("S-26", "AT-40.6.3")
def test_unreachable_database_is_unavailable_not_a_config_error(
    hx_engine: Engine, query_file: Path
) -> None:
    url = hx_engine.url.set(port=1)  # nothing listens there
    dead = SqlHosxpGateway(create_engine(url), load_config(query_file))
    with pytest.raises(HosxpUnavailableError) as exc:
        dead.get_pr("MOCK-SQL-0001")
    assert not isinstance(exc.value, HosxpConfigurationError)


# ------------------------------------------------------------------ schema discovery
@pytest.mark.spec("S-47")
def test_schema_discovery_lists_names_only_never_row_data(hx_engine: Engine) -> None:
    tables = discover(hx_engine, ["mockhx_pr"])
    names = {t.name for t in tables}
    assert names == {"mockhx_pr", "mockhx_pr_line"}
    pr = next(t for t in tables if t.name == "mockhx_pr")
    assert "budget_year" in {c.name for c in pr.columns}
    report = to_markdown(tables, title="test")
    assert "Mock requester" not in report and "MOCK-SQL-0001" not in report


# ------------------------------------------------------------------ end to end
@pytest.mark.spec("S-12", "S-25.1", "D-16", "D-17", "D-18", "S-42")
def test_the_application_works_end_to_end_on_the_sql_adapter(
    db: Engine, db_url: str, query_file: Path
) -> None:
    engine = create_engine(db_url)
    mockhx_schema.create(engine)
    try:
        hx = Harness(db, gateway=SqlHosxpGateway(engine, load_config(query_file)))
        p = Plan(hx)
        p.sync()  # masters come from the SQL adapter
        p.year()
        b = p.budget("MOCK-CAT-OFFICE", "1500").json()["id"]
        assert p.item(b, "1000").status_code == 201
        assert p.item(b, "500", item_id="MOCK-ITEM-PAPER").status_code == 201
        assert p.move("APPROVED").status_code == 200
        assert p.move("ACTIVE").status_code == 200

        def check(pr_no: str) -> dict[str, object]:
            r = check_pr(hx.client, pr_no, p.h)
            assert r.status_code == 200, r.text
            return dict(r.json())

        ok = check("MOCK-SQL-0001")
        assert ok["eligible"] is True and ok["required_amount"] == "500.00"
        bad_total = check("MOCK-SQL-0002")
        assert [v["code"] for v in bad_total["header_violations"]] == ["PR_TOTAL_MISMATCH"]  # type: ignore[attr-defined,index]
        cancelled = check("MOCK-SQL-0003")
        assert [v["code"] for v in cancelled["header_violations"]] == ["PR_CANCELLED_IN_HOSXP"]  # type: ignore[attr-defined,index]
    finally:
        mockhx_schema.drop(engine)
        engine.dispose()


# ------------------------------------------------------------------ CLI tools for IT
@pytest.mark.spec("S-47")
def test_cli_schema_and_check_commands(
    hx_engine: Engine,
    query_file: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ppr.cli import main

    monkeypatch.setenv(
        "PPR_HOSXP_DATABASE_URL", hx_engine.url.render_as_string(hide_password=False)
    )
    monkeypatch.setenv("PPR_HOSXP_QUERIES", str(query_file))
    out = tmp_path / "schema.md"
    assert main(["hosxp-schema", "--match", "mockhx", "--out", str(out)]) == 0
    report = out.read_text(encoding="utf-8")
    assert "## mockhx_pr (table)" in report and "Mock requester" not in report

    assert main(["hosxp-check", "--pr", "MOCK-SQL-0001"]) == 0
    printed = capsys.readouterr().out
    assert "missing queries: none" in printed and "RESULT: OK" in printed
    assert "stock movements (optional): verified by MOCK IT" in printed  # D-39
    assert '"sum_of_item_amounts": "500.00"' in printed
    assert "***" in printed  # the password is never printed


# ------------------------------------------------------------------ stock movements (D-39)
@pytest.mark.spec("AT-40.12.1", "AT-40.12.7", "D-39", "S-27")
def test_stock_movements_through_the_verified_site_query(gw: SqlHosxpGateway) -> None:
    from ppr.application.stock_services import StockTotals, read_stock

    src = gw.stock_movement_source()
    assert src.usable and src.verification is not None
    moves = gw.get_stock_movements(date(2026, 10, 1), date(2027, 9, 30))
    assert [m.movement_id for m in moves] == ["1", "2", "3", "4"]  # 2025-09-30 is outside
    assert {m.native_kind for m in moves} == {"OUT", "BACK", "ADJ"}  # as HOSxP has them
    got = read_stock(gw, date(2026, 10, 1), date(2027, 9, 30))
    assert got.available and got.returns_known and got.unmapped == 1  # ADJ is not mapped
    assert dict(got.totals) == {
        ("MOCK-ITEM-TONER", "MOCK-DEP-ENT"): StockTotals(D(3), D(1)),
        ("MOCK-ITEM-TONER", "MOCK-DEP-OR"): StockTotals(D(2), D(0)),
    }
    assert got.totals[("MOCK-ITEM-TONER", "MOCK-DEP-ENT")].net_used == D(2)
    # ENT also has an unmapped ADJ movement: its figures and the item's are unknown, not 2.
    assert got.departments("MOCK-ITEM-TONER") == {
        "MOCK-DEP-ENT": None,
        "MOCK-DEP-OR": StockTotals(D(2), D(0)),
    }
    assert got.item_total("MOCK-ITEM-TONER") is None
    narrow = read_stock(gw, date(2026, 10, 16), date(2026, 10, 16))
    assert dict(narrow.totals) == {("MOCK-ITEM-TONER", "MOCK-DEP-OR"): StockTotals(D(2), D(0))}


@pytest.mark.spec("AT-40.12.5", "D-39")
def test_an_unverified_stock_query_is_never_run(hx_engine: Engine, tmp_path: Path) -> None:
    from ppr.application.stock_services import read_stock

    body = mockhx_schema.QUERIES_TOML.replace('verified_by = "MOCK IT"\n', "")
    f = tmp_path / "q.toml"
    f.write_text(body, encoding="utf-8")
    engine = create_engine(hx_engine.url)
    try:
        gw = SqlHosxpGateway(engine, load_config(f))
        assert gw.get_departments()  # the rest of the adapter works
        with pytest.raises(HosxpConfigurationError, match="ยังไม่ได้รับการยืนยัน"):
            gw.get_stock_movements(date(2026, 10, 1), date(2026, 10, 31))
        got = read_stock(gw, date(2026, 10, 1), date(2026, 10, 31))
        assert not got.available and "ยังไม่ได้รับการยืนยัน" in (got.reason or "")
    finally:
        engine.dispose()
