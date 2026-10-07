"""Plan Amendment schema: migration 0008 (D-37 7A, spec §20)."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ppr.cli import alembic_config
from ppr.infrastructure.db.schema import metadata

NEW_TABLES = {"plan_amendment", "plan_amendment_change"}


def _columns(engine, table: str) -> dict[str, tuple[str, bool]]:  # type: ignore[no-untyped-def]
    return {
        c["name"]: (str(c["type"]).upper(), c["nullable"])
        for c in inspect(engine).get_columns(table)
    }


def _constraint(engine, name: str) -> str | None:  # type: ignore[no-untyped-def]
    with engine.connect() as c:
        return c.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": name},
        ).scalar_one_or_none()


def _seed(conn) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    uid = conn.execute(
        text("INSERT INTO app_user (hosxp_source_id, username) VALUES ('u1', 'u1') RETURNING id")
    ).scalar_one()
    yid = conn.execute(
        text(
            "INSERT INTO plan_year (fiscal_year, starts_on, ends_on) "
            "VALUES (2570, '2026-10-01', '2027-09-30') RETURNING id"
        )
    ).scalar_one()
    return uid, yid


def _amend(conn, yid: int, uid: int, no: int = 1, doc: str = "DOC-1", reason: str = "r"):  # type: ignore[no-untyped-def]
    return conn.execute(
        text(
            "INSERT INTO plan_amendment (plan_year_id, amendment_no, approval_document_no, "
            "approval_date, reason, created_by_user_id) "
            "VALUES (:y, :no, :doc, '2026-11-01', :r, :u) RETURNING id"
        ),
        {"y": yid, "no": no, "doc": doc, "r": reason, "u": uid},
    ).scalar_one()


def test_head_includes_0008(db) -> None:  # type: ignore[no-untyped-def]
    with db.connect() as c:
        assert int(c.execute(text("SELECT version_num FROM alembic_version")).scalar_one()) >= 8


def test_models_and_migrations_have_no_drift(db) -> None:  # type: ignore[no-untyped-def]
    with db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []


def test_schema_declares_the_new_tables() -> None:
    from ppr.infrastructure.db import schema_amendment

    assert set(metadata.tables) >= NEW_TABLES
    for name in NEW_TABLES:
        assert getattr(schema_amendment, name).metadata is metadata


def test_plan_amendment_columns(db) -> None:  # type: ignore[no-untyped-def]
    cols = _columns(db, "plan_amendment")
    assert cols["id"] == ("BIGINT", False)
    assert cols["plan_year_id"] == ("BIGINT", False)
    assert cols["amendment_no"] == ("INTEGER", False)
    assert cols["approval_document_no"] == ("TEXT", False)
    assert cols["approval_date"] == ("DATE", False)
    assert cols["reason"] == ("TEXT", False)
    assert cols["created_by_user_id"] == ("BIGINT", False)
    assert cols["created_at"][1] is False and "TIMESTAMP" in cols["created_at"][0]
    assert set(cols) == {
        "id",
        "plan_year_id",
        "amendment_no",
        "approval_document_no",
        "approval_date",
        "reason",
        "created_by_user_id",
        "created_at",
    }


def test_plan_amendment_change_columns(db) -> None:  # type: ignore[no-untyped-def]
    cols = _columns(db, "plan_amendment_change")
    assert cols["plan_amendment_id"] == ("BIGINT", False)
    assert cols["target"] == ("TEXT", False)
    assert cols["target_id"] == ("BIGINT", False)
    assert cols["change_kind"] == ("TEXT", False)
    assert cols["before"] == ("JSONB", True)
    assert cols["after"] == ("JSONB", False)
    assert set(cols) == {
        "id",
        "plan_amendment_id",
        "target",
        "target_id",
        "change_kind",
        "before",
        "after",
    }


def test_ledger_links_amendments(db) -> None:  # type: ignore[no-untyped-def]
    assert _columns(db, "plan_ledger")["plan_amendment_id"] == ("BIGINT", True)
    fks = {
        (fk["referred_table"], tuple(fk["constrained_columns"]))
        for fk in inspect(db).get_foreign_keys("plan_ledger")
    }
    assert ("plan_amendment", ("plan_amendment_id",)) in fks
    d = _constraint(db, "ck_plan_ledger_amendment_has_source")
    assert d and "PLAN_AMENDMENT" in d and "plan_amendment_id IS NOT NULL" in d


def test_plan_item_quantity_may_be_zero(db) -> None:  # type: ignore[no-untyped-def]
    assert _constraint(db, "ck_plan_item_positive_qty") is None
    d = _constraint(db, "ck_plan_item_non_negative_qty")
    assert d and ">= (0)" in d.replace("(0)::numeric", "(0)")


def test_named_constraints_exist(db) -> None:  # type: ignore[no-untyped-def]
    for name in (
        "uq_plan_amendment_year_no",
        "ck_plan_amendment_positive_no",
        "ck_plan_amendment_document_no_present",
        "ck_plan_amendment_reason_present",
        "ck_plan_amendment_change_known_target",
        "ck_plan_amendment_change_known_kind",
        "ck_plan_amendment_change_before_matches_kind",
        "uq_plan_amendment_change_target",
    ):
        assert _constraint(db, name), name


def test_amendment_number_unique_per_year(db) -> None:  # type: ignore[no-untyped-def]
    with db.begin() as c:
        uid, yid = _seed(c)
        _amend(c, yid, uid, 1)
    with pytest.raises(IntegrityError), db.begin() as c:
        _amend(c, yid, uid, 1)


@pytest.mark.parametrize("doc,reason,no", [("   ", "r", 1), ("D", "  ", 1), ("D", "r", 0)])
def test_amendment_needs_document_reason_and_number(db, doc, reason, no) -> None:  # type: ignore[no-untyped-def]
    with db.begin() as c:
        uid, yid = _seed(c)
    with pytest.raises(IntegrityError), db.begin() as c:
        _amend(c, yid, uid, no, doc, reason)


def test_change_rows_and_their_rules(db) -> None:  # type: ignore[no-untyped-def]
    ins = text(
        "INSERT INTO plan_amendment_change (plan_amendment_id, target, target_id, change_kind, "
        "before, after) VALUES (:a, :t, :i, :k, CAST(:b AS jsonb), CAST(:f AS jsonb))"
    )
    with db.begin() as c:
        uid, yid = _seed(c)
        aid = _amend(c, yid, uid)
        c.execute(ins, {"a": aid, "t": "ITEM", "i": 1, "k": "UPDATE", "b": "{}", "f": "{}"})
        c.execute(ins, {"a": aid, "t": "BUDGET", "i": 1, "k": "CREATE", "b": None, "f": "{}"})
    bad = [
        {"a": aid, "t": "OTHER", "i": 2, "k": "UPDATE", "b": "{}", "f": "{}"},
        {"a": aid, "t": "ITEM", "i": 3, "k": "DELETE", "b": "{}", "f": "{}"},
        {"a": aid, "t": "ITEM", "i": 4, "k": "CREATE", "b": "{}", "f": "{}"},
        {"a": aid, "t": "ITEM", "i": 5, "k": "UPDATE", "b": None, "f": "{}"},
        {"a": aid, "t": "ITEM", "i": 1, "k": "UPDATE", "b": "{}", "f": "{}"},  # duplicate
    ]
    for row in bad:
        with pytest.raises(IntegrityError), db.begin() as c:
            c.execute(ins, row)


@pytest.mark.parametrize("table", sorted(NEW_TABLES))
@pytest.mark.spec("AT-40.11.4", "D-37")
def test_history_is_append_only(db, table: str) -> None:  # type: ignore[no-untyped-def]
    with db.begin() as c:
        uid, yid = _seed(c)
        aid = _amend(c, yid, uid)
        c.execute(
            text(
                "INSERT INTO plan_amendment_change (plan_amendment_id, target, target_id, "
                "change_kind, before, after) VALUES (:a, 'ITEM', 1, 'CREATE', NULL, '{}')"
            ),
            {"a": aid},
        )
    for stmt in (
        f"UPDATE {table} SET id = id",
        f"DELETE FROM {table}",
        f"TRUNCATE {table} CASCADE",
    ):
        with pytest.raises(DBAPIError, match="append-only"), db.begin() as c:
            c.execute(text(stmt))


def test_downgrade_to_0007_and_back(empty_db, db_url) -> None:  # type: ignore[no-untyped-def]
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007")
    names = set(inspect(empty_db).get_table_names())
    assert not (NEW_TABLES & names)
    assert "plan_amendment_id" not in _columns(empty_db, "plan_ledger")
    assert _constraint(empty_db, "ck_plan_item_positive_qty")
    command.upgrade(cfg, "head")
    assert set(inspect(empty_db).get_table_names()) >= NEW_TABLES


def test_full_round_trip_to_base(empty_db, db_url) -> None:  # type: ignore[no-untyped-def]
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    assert set(inspect(empty_db).get_table_names()) - {"alembic_version"} == set()
    command.upgrade(cfg, "head")
