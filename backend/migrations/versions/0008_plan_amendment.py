"""Wave 7A: Plan Amendment history (D-37 7A, spec §20).

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARDED = ("plan_amendment", "plan_amendment_change")


def upgrade() -> None:
    op.create_table(
        "plan_amendment",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_year_id", sa.BigInteger(), nullable=False),
        sa.Column("amendment_no", sa.Integer(), nullable=False),
        sa.Column("approval_document_no", sa.Text(), nullable=False),
        sa.Column("approval_date", sa.Date(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("amendment_no > 0", name=op.f("ck_plan_amendment_positive_no")),
        sa.CheckConstraint(
            "length(trim(approval_document_no)) > 0",
            name=op.f("ck_plan_amendment_document_no_present"),
        ),
        sa.CheckConstraint(
            "length(trim(reason)) > 0", name=op.f("ck_plan_amendment_reason_present")
        ),
        sa.ForeignKeyConstraint(
            ["plan_year_id"],
            ["plan_year.id"],
            name=op.f("fk_plan_amendment_plan_year_id_plan_year"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_plan_amendment_created_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_amendment")),
        sa.UniqueConstraint("plan_year_id", "amendment_no", name="uq_plan_amendment_year_no"),
    )
    op.create_table(
        "plan_amendment_change",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_amendment_id", sa.BigInteger(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=False),
        sa.Column("change_kind", sa.Text(), nullable=False),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "target IN ('BUDGET', 'ITEM')", name=op.f("ck_plan_amendment_change_known_target")
        ),
        sa.CheckConstraint(
            "change_kind IN ('CREATE', 'UPDATE')", name=op.f("ck_plan_amendment_change_known_kind")
        ),
        sa.CheckConstraint(
            "(change_kind = 'CREATE') = (before IS NULL)",
            name=op.f("ck_plan_amendment_change_before_matches_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_amendment_id"],
            ["plan_amendment.id"],
            name=op.f("fk_plan_amendment_change_plan_amendment_id_plan_amendment"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_amendment_change")),
        sa.UniqueConstraint(
            "plan_amendment_id", "target", "target_id", name="uq_plan_amendment_change_target"
        ),
    )
    for table in GUARDED:
        op.execute(
            f"CREATE TRIGGER {table}_no_update_delete BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION ppr_reject_evidence_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION ppr_reject_evidence_mutation()"
        )
    op.add_column("plan_ledger", sa.Column("plan_amendment_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_plan_ledger_plan_amendment_id_plan_amendment"),
        "plan_ledger",
        "plan_amendment",
        ["plan_amendment_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_plan_ledger_amendment_has_source"),
        "plan_ledger",
        "event_type <> 'PLAN_AMENDMENT' OR plan_amendment_id IS NOT NULL",
    )
    op.drop_constraint(op.f("ck_plan_item_positive_qty"), "plan_item", type_="check")
    op.create_check_constraint(
        op.f("ck_plan_item_non_negative_qty"), "plan_item", "planned_qty >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_plan_item_non_negative_qty"), "plan_item", type_="check")
    op.create_check_constraint(op.f("ck_plan_item_positive_qty"), "plan_item", "planned_qty > 0")
    op.drop_constraint(op.f("ck_plan_ledger_amendment_has_source"), "plan_ledger", type_="check")
    op.drop_constraint(
        op.f("fk_plan_ledger_plan_amendment_id_plan_amendment"), "plan_ledger", type_="foreignkey"
    )
    op.drop_column("plan_ledger", "plan_amendment_id")
    for table in GUARDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_delete ON {table}")
    op.drop_table("plan_amendment_change")
    op.drop_table("plan_amendment")
