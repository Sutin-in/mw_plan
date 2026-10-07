"""Migration 0003: PLAN_APPROVED -> PLAN_ACTIVATED on a database that already holds data.

Builds a database at revision 0002 with real PLAN_APPROVED rows (written with plain SQL,
exactly as the Wave 2 code wrote them), upgrades to 0003 and checks that the data,
the append-only guarantees, the one-activation-per-item rule, idempotency and the
derived balances all survive. Downgrade is exercised the same way.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ppr.cli import alembic_config
from ppr.domain.ledger import PlanItemLedger
from ppr.infrastructure.db.plan_repositories import SqlLedgerRepository

D = Decimal


def _seed_at_0002(engine: Engine) -> tuple[int, int]:
    """Two activated Plan Items; item 1 also has a confirmed and a released PPR."""
    with engine.begin() as c:
        for table, extra_cols, extra_vals in (
            ("hosxp_fund_source", "", ""),
            ("hosxp_budget_category", "", ""),
            ("hosxp_department", "", ""),
            ("hosxp_item", ", unit", ", 'box'"),
        ):
            c.execute(
                text(
                    f"INSERT INTO {table}(source_id, code, name, active{extra_cols}) "
                    f"VALUES ('MOCK-{table}', 'C', 'Mock', true{extra_vals})"
                )
            )
        year = c.execute(
            text(
                "INSERT INTO plan_year(fiscal_year, state, starts_on, ends_on) "
                "VALUES (2570, 'ACTIVE', '2026-10-01', '2027-09-30') RETURNING id"
            )
        ).scalar_one()
        budget = c.execute(
            text(
                "INSERT INTO plan_budget(plan_year_id, fund_source_source_id, "
                "budget_category_source_id, approved_amount) VALUES "
                "(:y, 'MOCK-hosxp_fund_source', 'MOCK-hosxp_budget_category', 1500) "
                "RETURNING id"
            ),
            {"y": year},
        ).scalar_one()
        ids = []
        for qty, amount in (("10", "1000.00"), ("5", "500.00")):
            ids.append(
                c.execute(
                    text(
                        "INSERT INTO plan_item(plan_year_id, plan_budget_id, plan_type, "
                        "item_source_id, item_code_snapshot, item_name_snapshot, "
                        "unit_snapshot, owner_department_source_id, planned_qty, "
                        "estimated_unit_price, planned_amount) VALUES (:y, :b, 'DEPARTMENT', "
                        "'MOCK-hosxp_item', 'C', 'Mock', 'box', 'MOCK-hosxp_department', "
                        ":q, 100, :a) RETURNING id"
                    ),
                    {"y": year, "b": budget, "q": qty, "a": amount},
                ).scalar_one()
            )
        i1, i2 = ids
        rows = [
            (i1, "PLAN_APPROVED", "10", "1000.00", f"approve:{i1}", None),
            (i2, "PLAN_APPROVED", "5", "500.00", f"approve:{i2}", None),
            (i1, "PPR_CONFIRMED", "-4", "-400.00", "confirm:PPR-1", "PPR-1"),
            (i1, "PPR_RELEASED", "1", "100.00", "release:PPR-1", "PPR-1"),
        ]
        for item, event, q, a, key, ppr in rows:
            c.execute(
                text(
                    "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, "
                    "amount_delta, idempotency_key, ppr_ref) VALUES (:i, :e, :q, :a, :k, :p)"
                ),
                {"i": item, "e": event, "q": q, "a": a, "k": key, "p": ppr},
            )
        # An audit row, so the row-level audit guard has something to protect.
        c.execute(
            text(
                "INSERT INTO audit_log(actor_kind, action, entity_type, entity_id) "
                "VALUES ('SYSTEM', 'PLAN_ACTIVATED', 'plan_year', '2570')"
            )
        )
    return i1, i2


def _ledger(c: Connection) -> list[tuple[int, int, str, str, str]]:
    rows = c.execute(
        text(
            "SELECT id, plan_item_id, event_type, idempotency_key, "
            "qty_delta::text || '/' || amount_delta::text FROM plan_ledger ORDER BY id"
        )
    ).all()
    return [tuple(r) for r in rows]  # type: ignore[misc]


def _trigger_states(c: Connection) -> dict[str, str]:
    rows = c.execute(
        text(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid IN ('plan_ledger'::regclass, 'audit_log'::regclass) "
            "AND NOT tgisinternal"
        )
    ).all()
    return {r[0]: r[1] for r in rows}


def _assert_append_only(engine: Engine) -> None:
    for sql in (
        "UPDATE plan_ledger SET qty_delta = qty_delta",
        "DELETE FROM plan_ledger",
        "TRUNCATE plan_ledger",
        "UPDATE audit_log SET action = action",
        "DELETE FROM audit_log",
        "TRUNCATE audit_log",
    ):
        with pytest.raises(DBAPIError), engine.begin() as c:
            c.execute(text(sql))


@pytest.mark.spec("S-18", "S-29", "D-12", "AT-40.5.4")
def test_upgrade_0002_to_0003_relabels_existing_rows_and_keeps_guarantees(
    empty_db: Engine, db_url: str
) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0002")
    i1, i2 = _seed_at_0002(empty_db)
    with empty_db.connect() as c:
        before = _ledger(c)
        triggers_before = _trigger_states(c)

    command.upgrade(cfg, "0003")

    with empty_db.connect() as c:
        after = _ledger(c)
        # Same rows, same ids, same deltas; only the activation label and key changed.
        assert [(r[0], r[1], r[4]) for r in after] == [(r[0], r[1], r[4]) for r in before]
        assert [(r[2], r[3]) for r in after] == [
            ("PLAN_ACTIVATED", f"activate:{i1}"),
            ("PLAN_ACTIVATED", f"activate:{i2}"),
            ("PPR_CONFIRMED", "confirm:PPR-1"),
            ("PPR_RELEASED", "release:PPR-1"),
        ]
        assert (
            c.execute(
                text("SELECT count(*) FROM plan_ledger WHERE event_type = 'PLAN_APPROVED'")
            ).scalar_one()
            == 0
        )
        # Trigger definitions are untouched and enabled again ('O' = enabled).
        assert _trigger_states(c) == triggers_before
        assert set(triggers_before.values()) == {"O"}
        # Balances derived through the domain are exactly what the rows encode.
        repo = SqlLedgerRepository(c)
        b1 = PlanItemLedger(str(i1), repo.entries(i1)).balance
        b2 = PlanItemLedger(str(i2), repo.entries(i2)).balance
    assert (b1.approved_qty, b1.approved_amount) == (D("10"), D("1000.00"))
    assert (b1.remaining_qty, b1.remaining_amount) == (D("7"), D("700.00"))
    assert (b2.remaining_qty, b2.remaining_amount) == (D("5"), D("500.00"))

    _assert_append_only(empty_db)

    # One activation per Plan Item is still enforced (renamed partial unique index).
    with pytest.raises(IntegrityError), empty_db.begin() as c:
        c.execute(
            text(
                "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, amount_delta, "
                "idempotency_key) VALUES (:i, 'PLAN_ACTIVATED', 1, 1, 'fresh-key')"
            ),
            {"i": i1},
        )
    # Idempotency keys are still unique.
    with pytest.raises(IntegrityError), empty_db.begin() as c:
        c.execute(
            text(
                "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, amount_delta, "
                "idempotency_key, ppr_ref) VALUES (:i, 'PPR_CONFIRMED', -1, -1, "
                "'confirm:PPR-1', 'PPR-2')"
            ),
            {"i": i2},
        )
    # The old label is no longer a known event, and sign rules still apply to the new one.
    for event, q, a in (("PLAN_APPROVED", 1, 1), ("PLAN_ACTIVATED", -1, 1)):
        with pytest.raises(IntegrityError), empty_db.begin() as c:
            c.execute(
                text(
                    "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, "
                    "amount_delta, idempotency_key) VALUES (:i, :e, :q, :a, 'x-key')"
                ),
                {"i": i2, "e": event, "q": q, "a": a},
            )


@pytest.mark.spec("S-29", "S-38.3")
def test_downgrade_0003_to_0002_restores_plan_approved(empty_db: Engine, db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0002")
    i1, i2 = _seed_at_0002(empty_db)
    with empty_db.connect() as c:
        original = _ledger(c)
    command.upgrade(cfg, "0003")
    command.downgrade(cfg, "0002")
    with empty_db.connect() as c:
        assert _ledger(c) == original
        assert set(_trigger_states(c).values()) == {"O"}
        indexes = set(
            c.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'plan_ledger'")
            ).scalars()
        )
    assert "uq_plan_ledger_one_approval" in indexes
    assert "uq_plan_ledger_one_activation" not in indexes
    _assert_append_only(empty_db)
    with pytest.raises(IntegrityError), empty_db.begin() as c:
        c.execute(
            text(
                "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, amount_delta, "
                "idempotency_key) VALUES (:i, 'PLAN_APPROVED', 1, 1, 'fresh-key')"
            ),
            {"i": i2},
        )
    # And forward again.
    command.upgrade(cfg, "head")
    with empty_db.connect() as c:
        assert (
            c.execute(
                text("SELECT count(*) FROM plan_ledger WHERE event_type = 'PLAN_ACTIVATED'")
            ).scalar_one()
            == 2
        )
    assert i1 != i2


@pytest.mark.spec("S-29")
def test_fresh_database_walks_every_revision_in_order(empty_db: Engine, db_url: str) -> None:
    cfg = alembic_config(db_url)
    head = ScriptDirectory.from_config(cfg).get_current_head()
    for rev in ("0001", "0002", "0003", "head"):
        command.upgrade(cfg, rev)
        with empty_db.connect() as c:
            current = c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert current == (head if rev == "head" else rev)
