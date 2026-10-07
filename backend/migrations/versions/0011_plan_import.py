"""Wave 12A-1: plan imported from Excel; imported rows may have no HOSxP item yet (D-42).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-07

D-42 (product owner, 2026-10-07): the hospital's plan is kept in Excel without HOSxP item
codes. A Plan Officer imports it into a DRAFT plan year; a requester binds a PR item to a
plan row later (Wave 12A-2). This revision:

* adds ``plan_import`` - one row per imported file (name, SHA-256, counts, total, who/when;
  removal while DRAFT marks it REMOVED with who/when/why, the row stays);
* lets ``plan_item.item_source_id`` / ``item_code_snapshot`` be empty **only** for a row that
  came from an import (``ck_plan_item_code_or_import``), and keeps the import, sheet and row
  each imported Plan Item came from.

Existing rows all have an item, so every new check holds for them. No row is changed or
deleted. Migrations 0001-0010 are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_import",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_year_id", sa.BigInteger(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_sha256", sa.Text(), nullable=False),
        sa.Column("rows_imported", sa.Integer(), nullable=False),
        sa.Column("rows_skipped", sa.Integer(), nullable=False),
        sa.Column("total_amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'IMPORTED'"), nullable=False),
        sa.Column("imported_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("removed_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "state IN ('IMPORTED', 'REMOVED')", name=op.f("ck_plan_import_known_state")
        ),
        sa.CheckConstraint(
            "file_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_plan_import_sha256_form")
        ),
        sa.CheckConstraint(
            "rows_imported > 0 AND rows_skipped >= 0", name=op.f("ck_plan_import_row_counts")
        ),
        sa.CheckConstraint(
            "total_amount >= 0", name=op.f("ck_plan_import_non_negative_amount")
        ),
        sa.CheckConstraint(
            "(state = 'REMOVED') = (removed_at IS NOT NULL AND removed_by_user_id IS NOT NULL "
            "AND length(trim(coalesce(removed_reason, ''))) > 0)",
            name=op.f("ck_plan_import_removed_has_details"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_year_id"], ["plan_year.id"], name=op.f("fk_plan_import_plan_year_id_plan_year")
        ),
        sa.ForeignKeyConstraint(
            ["imported_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_plan_import_imported_by_user_id_app_user"),
        ),
        sa.ForeignKeyConstraint(
            ["removed_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_plan_import_removed_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_import")),
    )
    op.create_index(
        "uq_plan_import_live_file",
        "plan_import",
        ["plan_year_id", "file_sha256"],
        unique=True,
        postgresql_where=sa.text("state = 'IMPORTED'"),
    )
    op.add_column("plan_item", sa.Column("plan_import_id", sa.BigInteger(), nullable=True))
    op.add_column("plan_item", sa.Column("source_sheet", sa.Text(), nullable=True))
    op.add_column("plan_item", sa.Column("source_row", sa.Integer(), nullable=True))
    op.create_foreign_key(
        op.f("fk_plan_item_plan_import_id_plan_import"),
        "plan_item",
        "plan_import",
        ["plan_import_id"],
        ["id"],
    )
    op.alter_column("plan_item", "item_source_id", existing_type=sa.Text(), nullable=True)
    op.alter_column("plan_item", "item_code_snapshot", existing_type=sa.Text(), nullable=True)
    op.create_check_constraint(
        op.f("ck_plan_item_code_or_import"),
        "plan_item",
        "item_source_id IS NOT NULL OR plan_import_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_plan_item_code_snapshot_with_code"),
        "plan_item",
        "(item_source_id IS NULL) = (item_code_snapshot IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_plan_item_source_row_with_import"),
        "plan_item",
        "(plan_import_id IS NULL) = (source_row IS NULL)",
    )


def downgrade() -> None:
    found = op.get_bind().execute(sa.text("SELECT count(*) FROM plan_import")).scalar_one()
    if found:
        raise RuntimeError(
            f"plan_import holds {found} import record(s); downgrading would lose them and where "
            "imported Plan Items came from (D-42). Nothing was changed."
        )
    op.drop_constraint(op.f("ck_plan_item_source_row_with_import"), "plan_item", type_="check")
    op.drop_constraint(op.f("ck_plan_item_code_snapshot_with_code"), "plan_item", type_="check")
    op.drop_constraint(op.f("ck_plan_item_code_or_import"), "plan_item", type_="check")
    op.alter_column("plan_item", "item_code_snapshot", existing_type=sa.Text(), nullable=False)
    op.alter_column("plan_item", "item_source_id", existing_type=sa.Text(), nullable=False)
    op.drop_constraint(
        op.f("fk_plan_item_plan_import_id_plan_import"), "plan_item", type_="foreignkey"
    )
    op.drop_column("plan_item", "source_row")
    op.drop_column("plan_item", "source_sheet")
    op.drop_column("plan_item", "plan_import_id")
    op.drop_index("uq_plan_import_live_file", table_name="plan_import")
    op.drop_table("plan_import")
