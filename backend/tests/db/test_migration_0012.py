"""Migration 0012 (D-43): the requester's plan-row choices; a Plan Item may have no item.

Existing Plan Items are kept unchanged; afterwards a Plan Item entered without a HOSxP item
(and without an import) is accepted; downgrading is refused while such a row exists, so a row
the restored 0011 check would refuse is never left behind.
"""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError
from test_migration_0003 import _seed_at_0002
from test_migration_0011 import _items, _no_item_row

from ppr.cli import alembic_config

pytestmark = pytest.mark.spec("AT-40.18.5", "D-43")


def test_0012_keeps_items_allows_rows_without_an_item_and_downgrades_only_without_them(
    empty_db: Engine, db_url: str
) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0002")
    _seed_at_0002(empty_db)
    command.upgrade(cfg, "0011")
    before = _items(empty_db)
    command.upgrade(cfg, "0012")
    assert _items(empty_db) == before  # every old value as it was
    assert "ppr_line_choice" in inspect(empty_db).get_table_names()
    with empty_db.connect() as c:
        year, budget = c.execute(text("SELECT plan_year_id, plan_budget_id FROM plan_item")).first()
    with empty_db.begin() as c:
        _no_item_row(c, year, budget, None)  # refused by 0011, accepted now
    with empty_db.connect() as c:
        manual = c.execute(
            text("SELECT id FROM plan_item WHERE item_source_id IS NULL")
        ).scalar_one()
    with pytest.raises(RuntimeError, match="D-43"):
        command.downgrade(cfg, "0011")
    with empty_db.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0012"
    with empty_db.begin() as c:
        c.execute(text("DELETE FROM plan_item WHERE id = :i"), {"i": manual})
    command.downgrade(cfg, "0011")
    assert "ppr_line_choice" not in inspect(empty_db).get_table_names()
    with pytest.raises(IntegrityError, match="code_or_import"), empty_db.begin() as c:
        _no_item_row(c, year, budget, None)
    command.upgrade(cfg, "head")
