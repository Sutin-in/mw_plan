"""Wave 8A: configurable alert thresholds (D-33).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30

``alert_setting`` holds the inactivity threshold (days) and the low-plan threshold
(percent), seeded with the D-33 defaults. Alerts themselves are derived and never stored.
Codes and ranges are frozen here on purpose (a migration must not change when the code
changes later).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_setting",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_by_user_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "code IN ('PPR_INACTIVE_DAYS', 'PLAN_LOW_PERCENT')",
            name=op.f("ck_alert_setting_known_code"),
        ),
        sa.CheckConstraint(
            "(code <> 'PPR_INACTIVE_DAYS' OR value BETWEEN 1 AND 365) AND "
            "(code <> 'PLAN_LOW_PERCENT' OR value BETWEEN 1 AND 99)",
            name=op.f("ck_alert_setting_value_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_alert_setting_updated_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("code", name=op.f("pk_alert_setting")),
    )
    op.execute(
        "INSERT INTO alert_setting (code, value) VALUES "
        "('PPR_INACTIVE_DAYS', 30), ('PLAN_LOW_PERCENT', 20)"
    )


def downgrade() -> None:
    op.drop_table("alert_setting")
