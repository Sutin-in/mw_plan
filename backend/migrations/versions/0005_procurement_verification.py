"""Wave 5B: Head of Procurement appointment rules and verification evidence; search indexes.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29

* ``role_appointment`` (exists since 0001, unused until now) gains revocation evidence and
  a database-level rule that ACTIVE appointments never overlap (D-27; GiST exclusion on
  the inclusive date range, no extension needed).
* ``ppr_verification``: append-only evidence of each verification (D-28, §14.4), guarded
  by the ``ppr_reject_evidence_mutation`` trigger function from 0004.
* Indexes for PPR search (§32).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARDED = ("ppr_verification",)


def upgrade() -> None:
    op.add_column(
        "role_appointment", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "role_appointment", sa.Column("revoked_by_user_id", sa.BigInteger(), nullable=True)
    )
    op.add_column("role_appointment", sa.Column("revoke_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        op.f("fk_role_appointment_revoked_by_user_id_app_user"),
        "role_appointment",
        "app_user",
        ["revoked_by_user_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_role_appointment_revoked_iff_timestamped"),
        "role_appointment",
        "(status = 'REVOKED') = (revoked_at IS NOT NULL)",
    )
    op.execute(
        "ALTER TABLE role_appointment ADD CONSTRAINT no_overlapping_active "
        "EXCLUDE USING gist (daterange(effective_from, effective_to, '[]') WITH &&) "
        "WHERE (status = 'ACTIVE')"
    )

    op.create_table(
        "ppr_verification",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ppr_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.BigInteger(), nullable=False),
        sa.Column("verified_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("verified_on", sa.Date(), nullable=False),
        sa.CheckConstraint("version >= 1", name=op.f("ck_ppr_verification_positive_version")),
        sa.ForeignKeyConstraint(
            ["appointment_id"],
            ["role_appointment.id"],
            name=op.f("fk_ppr_verification_appointment_id_role_appointment"),
        ),
        sa.ForeignKeyConstraint(
            ["ppr_id"], ["ppr.id"], name=op.f("fk_ppr_verification_ppr_id_ppr")
        ),
        sa.ForeignKeyConstraint(
            ["verified_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_ppr_verification_verified_by_user_id_app_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ppr_verification")),
        sa.UniqueConstraint("ppr_id", "version", name="uq_ppr_verification_version"),
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

    op.create_index("ix_ppr_fiscal_year_state", "ppr", ["fiscal_year", "state"])
    op.create_index("ix_ppr_pr_no", "ppr", ["pr_no"])
    op.create_index("ix_ppr_item_item", "ppr_item", ["item_source_id"])


def downgrade() -> None:
    op.drop_index("ix_ppr_item_item", table_name="ppr_item")
    op.drop_index("ix_ppr_pr_no", table_name="ppr")
    op.drop_index("ix_ppr_fiscal_year_state", table_name="ppr")
    for table in GUARDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_delete ON {table}")
    op.drop_table("ppr_verification")
    op.execute("ALTER TABLE role_appointment DROP CONSTRAINT IF EXISTS no_overlapping_active")
    op.drop_constraint(
        op.f("ck_role_appointment_revoked_iff_timestamped"), "role_appointment", type_="check"
    )
    op.drop_constraint(
        op.f("fk_role_appointment_revoked_by_user_id_app_user"),
        "role_appointment",
        type_="foreignkey",
    )
    op.drop_column("role_appointment", "revoke_reason")
    op.drop_column("role_appointment", "revoked_by_user_id")
    op.drop_column("role_appointment", "revoked_at")
