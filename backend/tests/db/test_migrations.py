"""Migrations: round-trip and zero drift between models and the migrated schema."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect, text

from ppr.cli import alembic_config
from ppr.domain.roles import Role
from ppr.infrastructure.db.schema import metadata

APP_TABLES = {t.name for t in metadata.sorted_tables}


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


@pytest.mark.spec("S-29", "S-38.3")
def test_upgrade_downgrade_upgrade_round_trip(empty_db: Engine, db_url: str) -> None:
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    assert _tables(empty_db) == APP_TABLES
    command.downgrade(cfg, "base")
    assert _tables(empty_db) == set()
    with empty_db.connect() as conn:
        fn = conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname = 'ppr_reject_audit_mutation'")
        ).scalar_one()
    assert fn == 0  # downgrade removes the audit guard too
    command.upgrade(cfg, "head")
    assert _tables(empty_db) == APP_TABLES


def test_models_and_migrations_have_no_drift(db: Engine) -> None:
    with db.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []


@pytest.mark.spec("S-22")
def test_roles_are_seeded_exactly(db: Engine) -> None:
    with db.connect() as conn:
        codes = set(conn.execute(text("SELECT code FROM role")).scalars())
    assert codes == {r.value for r in Role}


def test_phase1_scope_tables_only(db: Engine) -> None:
    # No PlanFin/GL/PO/payment tables slipped into the schema (spec §3).
    names = " ".join(_tables(db)).lower()
    for forbidden in ("planfin", "general_ledger", "purchase_order", "payment"):
        assert forbidden not in names
