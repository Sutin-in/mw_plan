"""Wave 7B-2: Assigned Purchase coverage and demand amendments (D-38, G-3).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-01

* ``ppr_coverage``: per PPR and ASSIGNED Plan Item, the quantity bought for each demand
  department (WORKING rows while editable, CONFIRMED rows from the last confirmation).
* ``plan_item_demand.demand_qty`` may be 0, which is exactly the CANCELLED state (A-6).
* ``plan_amendment_change`` may record DEMAND rows (A-6).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ppr_coverage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ppr_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_item_id", sa.BigInteger(), nullable=False),
        sa.Column("department_source_id", sa.Text(), nullable=False),
        sa.Column("qty", sa.Numeric(18, 4), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.CheckConstraint("qty > 0", name=op.f("ck_ppr_coverage_positive_qty")),
        sa.ForeignKeyConstraint(
            ["ppr_id"], ["ppr.id"], name=op.f("fk_ppr_coverage_ppr_id_ppr"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["plan_item_id"], ["plan_item.id"], name=op.f("fk_ppr_coverage_plan_item_id_plan_item")
        ),
        sa.ForeignKeyConstraint(
            ["department_source_id"],
            ["hosxp_department.source_id"],
            name=op.f("fk_ppr_coverage_department_source_id_hosxp_department"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ppr_coverage")),
        sa.UniqueConstraint(
            "ppr_id",
            "plan_item_id",
            "department_source_id",
            "confirmed",
            name="uq_ppr_coverage_row",
        ),
    )
    op.create_index("ix_ppr_coverage_item", "ppr_coverage", ["plan_item_id"])

    op.drop_constraint(op.f("ck_plan_item_demand_positive_qty"), "plan_item_demand", type_="check")
    op.create_check_constraint(
        op.f("ck_plan_item_demand_non_negative_qty"), "plan_item_demand", "demand_qty >= 0"
    )
    op.create_check_constraint(
        op.f("ck_plan_item_demand_cancelled_iff_zero"),
        "plan_item_demand",
        "(demand_qty = 0) = (state = 'CANCELLED')",
    )

    op.drop_constraint(
        op.f("ck_plan_amendment_change_known_target"), "plan_amendment_change", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_plan_amendment_change_known_target"),
        "plan_amendment_change",
        "target IN ('BUDGET', 'ITEM', 'DEMAND')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_plan_amendment_change_known_target"), "plan_amendment_change", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_plan_amendment_change_known_target"),
        "plan_amendment_change",
        "target IN ('BUDGET', 'ITEM')",
    )
    op.drop_constraint(
        op.f("ck_plan_item_demand_cancelled_iff_zero"), "plan_item_demand", type_="check"
    )
    op.drop_constraint(
        op.f("ck_plan_item_demand_non_negative_qty"), "plan_item_demand", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_plan_item_demand_positive_qty"), "plan_item_demand", "demand_qty > 0"
    )
    op.drop_index("ix_ppr_coverage_item", table_name="ppr_coverage")
    op.drop_table("ppr_coverage")
