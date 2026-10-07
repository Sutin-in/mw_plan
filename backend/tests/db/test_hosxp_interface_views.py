"""The HOSxP interface-view kit (docs/hosxp_interface) works end to end on PostgreSQL.

The view TEMPLATE is filled in with the made-up ``mockhx_*`` tables (as a hospital's IT
fills it with its real HOSxP tables), created, and read through the ready-made view
query file. Every result must equal what the direct mock query file returns, so the
views' column names, types and casts match the contracts exactly. Spec §24, §42, §47;
D-17, D-23, D-25.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text

import mockhx_schema
from ppr.integration.hosxp.real.query_config import OPERATIONS, load_config
from ppr.integration.hosxp.real.sql_gateway import SqlHosxpGateway
from ppr.integration.hosxp.status import HosxpPrStatus

KIT = Path(__file__).resolve().parents[3] / "docs" / "hosxp_interface"
TEMPLATE = KIT / "ppr_interface_views.sql"
VIEW_QUERIES = KIT / "hosxp_queries.views.toml"

# What the hospital's IT would write in place of each <placeholder>, per view, in order.
FILL: dict[str, list[str]] = {
    "pr_header": [
        "p.pr_key",
        "p.pr_number",
        "p.pr_day",
        "p.budget_year",
        "p.dep_code",
        "p.fund_code",
        "p.cat_code",
        "NULL",
        "p.requester",
        "p.total",
        "p.state_code",
        "NULL",
        "mockhx_pr p",
    ],
    "pr_item": [
        "p.pr_number",
        "l.line_key",
        "l.item_code",
        "i.item_abbr",
        "i.item_name",
        "l.qty",
        "i.unit_name",
        "l.price",
        "l.amount",
        "mockhx_pr_line l JOIN mockhx_pr p ON p.pr_key = l.pr_key"
        " LEFT JOIN mockhx_item i ON i.item_code = l.item_code",
    ],
    "department": ["dep_code", "dep_abbr", "dep_name", "is_active = 1", "mockhx_dept"],
    "item": ["item_code", "item_abbr", "item_name", "unit_name", "is_active = 1", "mockhx_item"],
    "fund_source": ["fund_code", "fund_abbr", "fund_name", "is_active = 1", "mockhx_fund"],
    "budget_category": [
        "cat_code",
        "cat_abbr",
        "cat_name",
        "parent_code",
        "is_active = 1",
        "mockhx_cat",
    ],
    "app_user": [
        "user_key",
        "login_name",
        "full_name",
        "dep_code",
        "pwd_digest",
        "is_active = 1",
        "mockhx_user",
    ],
}
_VIEW = re.compile(r"CREATE OR REPLACE VIEW ppr_interface\.(\w+) AS.*?;", re.S)
_HOLE = re.compile(r"<[^<>]+>")


def filled_sql() -> list[str]:
    """The template's statements with every placeholder filled; GRANTs left out (the
    test database has no ``ppr_readonly`` role)."""
    template = TEMPLATE.read_text(encoding="utf-8")
    views = []
    for m in _VIEW.finditer(template):
        name, block = m.group(1), m.group(0)
        values = iter(FILL[name])
        views.append(_HOLE.sub(lambda _, v=values: next(v), block))
        assert next(values, None) is None, f"{name}: unused fill values"
    comments = re.findall(r"^COMMENT ON .*?;$", template, re.M)
    return ["CREATE SCHEMA IF NOT EXISTS ppr_interface", *views, *comments]


@pytest.fixture
def engines(db_url: str, empty_db: Engine) -> Iterator[tuple[Engine, Engine]]:
    setup = create_engine(db_url)
    mockhx_schema.create(setup)
    with setup.begin() as conn:
        for stmt in filled_sql():
            conn.execute(text(stmt))
    reader = create_engine(db_url)  # read-only sessions, as in production
    yield setup, reader
    reader.dispose()
    with setup.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS ppr_interface CASCADE"))
    mockhx_schema.drop(setup)
    setup.dispose()


def test_template_views_cover_every_needed_operation_and_never_fix_a_scale() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")
    assert set(_VIEW.findall(template)) == set(FILL)
    # D-23: a numeric(p, s) cast would round silently; the kit must never do it.
    view_sql = "".join(m.group(0) for m in _VIEW.finditer(template))
    assert "AS numeric)" in view_sql
    assert not re.search(r"numeric\s*\(", view_sql, re.I)
    config = load_config(VIEW_QUERIES)
    assert config.missing_needed() == []
    assert dict(config.status_mapping.native_to_normalized) == {}  # D-30: empty


@pytest.mark.spec("D-17", "D-23", "D-25")
def test_views_return_exactly_what_the_direct_queries_return(
    engines: tuple[Engine, Engine], tmp_path: Path
) -> None:
    _, reader = engines
    direct_file = tmp_path / "direct.toml"
    # The same site queries without a status mapping, to compare like with like.
    direct_file.write_text(
        mockhx_schema.QUERIES_TOML.replace('"A1" = "ACTIVE"\n"X9" = "CANCELLED"\n', ""),
        encoding="utf-8",
    )
    views = SqlHosxpGateway(reader, load_config(VIEW_QUERIES))
    direct = SqlHosxpGateway(reader, load_config(direct_file))

    for pr_no in ("MOCK-SQL-0001", "MOCK-SQL-0002", "MOCK-SQL-0003"):
        h = views.get_pr(pr_no)
        assert h is not None and h == direct.get_pr(pr_no)
        assert h.status is HosxpPrStatus.UNKNOWN  # empty mapping: nothing cancels
        # The direct mock query has no item_code; everything else must be equal.
        assert [i.model_copy(update={"item_code": None}) for i in views.get_pr_items(pr_no)] == [
            *direct.get_pr_items(pr_no)
        ]
    assert views.get_pr("NO-SUCH-PR") is None
    h1 = views.get_pr("MOCK-SQL-0001")
    assert h1 is not None and (h1.fiscal_year, h1.total_amount, h1.native_status) == (
        2570,
        500,
        "A1",
    )
    assert views.get_departments() == direct.get_departments()
    assert views.get_items() == direct.get_items()
    assert views.get_fund_sources() == direct.get_fund_sources()
    assert views.get_budget_categories() == direct.get_budget_categories()
    for op in ("get_login_user", "get_user_profile"):
        param = "username" if op == "get_login_user" else "source_id"
        value = "mock_ent" if op == "get_login_user" else "MOCK-U-1"
        got, want = views.rows(op, {param: value}), direct.rows(op, {param: value})
        # The mock stores 1/0; the view gives a true boolean, as the kit requires.
        assert [{**r, "active": bool(r["active"])} for r in want] == got
    assert set(OPERATIONS) >= {"get_pr", "get_pr_items", "get_login_user"}
