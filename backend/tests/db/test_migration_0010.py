"""Migration 0010 (D-39): the Plan Ledger refuses stock issues and returns.

Built on the 0003 test's seed (a database with real ledger rows): upgrading keeps every row
and balance, downgrading restores the old constraints, and a database that somehow holds a
stock entry is never silently changed - the upgrade stops.
"""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from test_migration_0003 import _seed_at_0002

from ppr.cli import alembic_config

pytestmark = pytest.mark.spec("AT-40.12.2", "D-39")


def _rows(engine: Engine) -> list[tuple[object, ...]]:
    with engine.connect() as c:
        return [
            tuple(r)
            for r in c.execute(
                text(
                    "SELECT id, plan_item_id, event_type, qty_delta, amount_delta, "
                    "idempotency_key FROM plan_ledger ORDER BY id"
                )
            )
        ]


def _rules(engine: Engine) -> dict[str, str]:
    with engine.connect() as c:
        return {
            r[0]: r[1]
            for r in c.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'plan_ledger'::regclass AND contype = 'c'"
                )
            )
        }


def _insert_stock(engine: Engine, item: int, key: str) -> None:
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, amount_delta, "
                "idempotency_key) VALUES (:i, 'CENTRAL_STOCK_ISSUE', -1, 0, :k)"
            ),
            {"i": item, "k": key},
        )


def test_upgrade_to_0010_keeps_rows_and_refuses_stock(empty_db: Engine, db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0002")
    i1, _ = _seed_at_0002(empty_db)
    command.upgrade(cfg, "0009")
    before = _rows(empty_db)

    command.upgrade(cfg, "0010")
    assert _rows(empty_db) == before
    assert _rules(empty_db)["ck_plan_ledger_no_stock_movement"].count("CENTRAL_STOCK") == 2
    assert "CENTRAL_STOCK" not in _rules(empty_db)["ck_plan_ledger_known_event"]
    assert "CENTRAL_STOCK" not in _rules(empty_db)["ck_plan_ledger_event_signs"]
    with pytest.raises(IntegrityError, match=r"no_stock_movement|known_event"):
        _insert_stock(empty_db, i1, "refused-after-0010")

    command.downgrade(cfg, "0009")
    assert _rows(empty_db) == before
    assert "ck_plan_ledger_no_stock_movement" not in _rules(empty_db)
    _insert_stock(empty_db, i1, "old-rule-again")  # downgrade restores the 0009 constraints


def test_upgrade_stops_when_a_stock_entry_exists(empty_db: Engine, db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0002")
    i1, _ = _seed_at_0002(empty_db)
    command.upgrade(cfg, "0009")
    _insert_stock(empty_db, i1, "found-by-0010")
    before = _rows(empty_db)
    with pytest.raises(RuntimeError, match="D-39 forbids them"):
        command.upgrade(cfg, "0010")
    assert _rows(empty_db) == before  # nothing deleted or changed
    with empty_db.connect() as c:
        assert c.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0009"
