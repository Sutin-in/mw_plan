"""Wave 12A-2: the requester chooses the plan row of every PR line (D-43).

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-07

D-43 (product owner, 2026-10-07): nothing is matched automatically; for every PR line the
requester chooses the Plan Item it uses, and the plan holds no HOSxP item codes. This
revision:

* adds ``ppr_line_choice`` - the chosen Plan Item per PPR and PR line, with the row's name
  and unit as chosen (who, when);
* drops ``ck_plan_item_code_or_import``: any Plan Item may have no HOSxP item (its own name
  and unit); ``ck_plan_item_code_snapshot_with_code`` stays.

No row is changed or deleted. Migrations 0001-0011 are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ppr_line_choice",
        sa.Column("ppr_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_item_id", sa.Text(), nullable=False),
        sa.Column("plan_item_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_item_name", sa.Text(), nullable=False),
        sa.Column("plan_item_unit", sa.Text(), nullable=False),
        sa.Column("chosen_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "chosen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["ppr_id"], ["ppr.id"], name=op.f("fk_ppr_line_choice_ppr_id_ppr"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_item_id"],
            ["plan_item.id"],
            name=op.f("fk_ppr_line_choice_plan_item_id_plan_item"),
        ),
        sa.ForeignKeyConstraint(
            ["chosen_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_ppr_line_choice_chosen_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("ppr_id", "pr_item_id", name="pk_ppr_line_choice"),
    )
    op.drop_constraint(op.f("ck_plan_item_code_or_import"), "plan_item", type_="check")


def downgrade() -> None:
    choices = op.get_bind().execute(sa.text("SELECT count(*) FROM ppr_line_choice")).scalar_one()
    if choices:
        raise RuntimeError(
            f"ppr_line_choice holds {choices} plan-row choice(s) of PPRs (D-43); downgrading "
            "would drop who chose which row. Nothing was changed."
        )
    found = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM plan_item WHERE item_source_id IS NULL AND plan_import_id IS NULL"
        )
    ).scalar_one()
    if found:
        raise RuntimeError(
            f"plan_item holds {found} row(s) entered without a HOSxP item (D-43); 0011 does not "
            "allow them. Nothing was changed."
        )
    op.create_check_constraint(
        op.f("ck_plan_item_code_or_import"),
        "plan_item",
        "item_source_id IS NOT NULL OR plan_import_id IS NOT NULL",
    )
    op.drop_table("ppr_line_choice")
