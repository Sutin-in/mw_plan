"""Migration 0011 (D-42): imported Plan Items may have no HOSxP item.

Existing Plan Items (all with an item) are kept unchanged; a Plan Item without an item is
refused unless it came from an import; downgrading is refused once an import exists, so the
record of where imported rows came from is never dropped.
"""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from test_migration_0003 import _seed_at_0002

from ppr.cli import alembic_config

pytestmark = pytest.mark.spec("AT-40.17.3", "D-42")


def _items(engine: Engine) -> list[tuple[object, ...]]:
    with engine.connect() as c:
        return [tuple(r) for r in c.execute(text("SELECT * FROM plan_item ORDER BY id"))]


def _no_item_row(c: object, year: int, budget: int, import_id: object) -> None:
    c.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO plan_item(plan_year_id, plan_budget_id, plan_type, item_name_snapshot, "
            "unit_snapshot, owner_department_source_id, planned_qty, estimated_unit_price, "
            "planned_amount, plan_import_id, source_sheet, source_row) VALUES (:y, :b, "
            "'DEPARTMENT', 'from Excel', 'box', 'MOCK-hosxp_department', 1, 1, 1, :i, 'รายการ', "
            "CASE WHEN CAST(:i AS BIGINT) IS NULL THEN NULL ELSE 2 END)"
        ),
        {"y": year, "b": budget, "i": import_id},
    )


def test_0011_keeps_items_and_allows_no_item_only_for_imports(
    empty_db: Engine, db_url: str
) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0002")
    _seed_at_0002(empty_db)
    command.upgrade(cfg, "0010")
    before = _items(empty_db)
    command.upgrade(cfg, "0011")
    after = _items(empty_db)
    assert [r[: len(before[0])] for r in after] == before  # every old value as it was
    with empty_db.connect() as c:
        year, budget = c.execute(text("SELECT plan_year_id, plan_budget_id FROM plan_item")).first()
        user = c.execute(
            text(
                "INSERT INTO app_user(hosxp_source_id, username, active) "
                "VALUES ('U1', 'u1', true) RETURNING id"
            )
        ).scalar_one()
        c.commit()
    with pytest.raises(IntegrityError, match="code_or_import"), empty_db.begin() as c:
        _no_item_row(c, year, budget, None)
    with empty_db.begin() as c:
        imp = c.execute(
            text(
                "INSERT INTO plan_import(plan_year_id, file_name, file_sha256, rows_imported, "
                "rows_skipped, total_amount, imported_by_user_id) VALUES (:y, 'f.xlsx', :h, 1, 0, "
                "1, :u) RETURNING id"
            ),
            {"y": year, "h": "a" * 64, "u": user},
        ).scalar_one()
        _no_item_row(c, year, budget, imp)
    with pytest.raises(RuntimeError, match="D-42"):
        command.downgrade(cfg, "0010")
    with empty_db.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0011"
        assert c.execute(text("SELECT count(*) FROM plan_import")).scalar_one() == 1
