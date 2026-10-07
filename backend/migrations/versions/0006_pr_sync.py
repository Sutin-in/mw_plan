"""Wave 6: PR synchronization evidence (D-29 ... D-31).

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-29

* ``ppr_sync_observation``: one append-only row per attempt to observe a PPR's bound PR
  in HOSxP (guarded by ``ppr_reject_evidence_mutation`` from 0004). A partial unique
  index makes a second plan-usage release by synchronization impossible (D-30).
* ``sync_run``: the PPR of a one-PPR run, the run a retry repeats, and at
  most one RUNNING full PPR synchronization.

The values in the CHECK constraints are frozen here on purpose (a migration must not
change when the code changes later).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GUARDED = ("ppr_sync_observation",)
OUTCOMES = (
    "'UNCHANGED', 'NON_CRITICAL_CHANGE', 'CRITICAL_CHANGE', 'IDENTITY_CHANGE', "
    "'INVALID_EVIDENCE', 'CANCELLED', 'NOT_FOUND', 'UNAVAILABLE', 'SKIPPED_STATE_MOVED', "
    "'ERROR', 'ANOMALY', 'NO_AUTHORITATIVE_STATUS'"
)
CLASSES = "'BINDING', 'STATUS', 'CONTROL'"
STATES = (
    "'DRAFT', 'CONFIRMED_LOCKED', 'PROCUREMENT_VERIFIED', 'PR_CHANGED_REVIEW_REQUIRED', "
    "'UNLOCKED_FOR_REVISION', 'CANCELLED', 'CLOSED'"
)


def upgrade() -> None:
    # ------------------------------------------------------------------ sync_run
    op.add_column("sync_run", sa.Column("ppr_id", sa.BigInteger(), nullable=True))
    op.add_column("sync_run", sa.Column("retry_of_sync_run_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(op.f("fk_sync_run_ppr_id_ppr"), "sync_run", "ppr", ["ppr_id"], ["id"])
    op.create_foreign_key(
        op.f("fk_sync_run_retry_of_sync_run_id_sync_run"),
        "sync_run",
        "sync_run",
        ["retry_of_sync_run_id"],
        ["id"],
    )
    op.create_check_constraint(
        op.f("ck_sync_run_ppr_iff_single"),
        "sync_run",
        "(scope IN ('PPR', 'PPR_RECHECK')) = (ppr_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_sync_run_retry_iff_retry"),
        "sync_run",
        "(scope = 'PPR_RETRY') = (retry_of_sync_run_id IS NOT NULL)",
    )
    op.create_index(
        "uq_sync_run_one_running_pprs",
        "sync_run",
        ["scope"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING' AND scope = 'PPRS'"),
    )

    # ------------------------------------------------------------------ observation
    op.create_table(
        "ppr_sync_observation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sync_run_id", sa.BigInteger(), nullable=False),
        sa.Column("ppr_id", sa.BigInteger(), nullable=False),
        sa.Column("pr_no", sa.Text(), nullable=False),
        sa.Column("compared_version", sa.Integer(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("evidence_class", sa.Text(), nullable=True),
        sa.Column("hosxp_status", sa.Text(), nullable=True),
        sa.Column("native_status", sa.Text(), nullable=True),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_issues", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("live_pr", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("state_before", sa.Text(), nullable=False),
        sa.Column("state_after", sa.Text(), nullable=False),
        sa.Column("released", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("anomaly_code", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"outcome IN ({OUTCOMES})", name=op.f("ck_ppr_sync_observation_known_outcome")
        ),
        sa.CheckConstraint(
            f"evidence_class IS NULL OR evidence_class IN ({CLASSES})",
            name=op.f("ck_ppr_sync_observation_known_evidence_class"),
        ),
        sa.CheckConstraint(
            "(outcome = 'INVALID_EVIDENCE') = (evidence_class IS NOT NULL)",
            name=op.f("ck_ppr_sync_observation_class_iff_invalid"),
        ),
        sa.CheckConstraint(
            f"state_before IN ({STATES})", name=op.f("ck_ppr_sync_observation_known_state_before")
        ),
        sa.CheckConstraint(
            f"state_after IN ({STATES})", name=op.f("ck_ppr_sync_observation_known_state_after")
        ),
        sa.CheckConstraint(
            "NOT released OR (outcome = 'CANCELLED' AND state_after = 'CANCELLED')",
            name=op.f("ck_ppr_sync_observation_release_only_on_cancel"),
        ),
        sa.CheckConstraint(
            "state_after <> 'CLOSED'", name=op.f("ck_ppr_sync_observation_never_closes")
        ),
        sa.ForeignKeyConstraint(
            ["ppr_id"], ["ppr.id"], name=op.f("fk_ppr_sync_observation_ppr_id_ppr")
        ),
        sa.ForeignKeyConstraint(
            ["sync_run_id"],
            ["sync_run.id"],
            name=op.f("fk_ppr_sync_observation_sync_run_id_sync_run"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ppr_sync_observation")),
    )
    op.create_index(
        "ix_ppr_sync_observation_ppr", "ppr_sync_observation", ["ppr_id", "id"], unique=False
    )
    op.create_index(
        "ix_ppr_sync_observation_run", "ppr_sync_observation", ["sync_run_id"], unique=False
    )
    op.create_index(
        "uq_ppr_sync_observation_released",
        "ppr_sync_observation",
        ["ppr_id"],
        unique=True,
        postgresql_where=sa.text("released"),
    )
    op.create_index(
        "ix_ppr_sync_observation_governance",
        "ppr_sync_observation",
        ["recorded_at"],
        unique=False,
        postgresql_where=sa.text(
            "outcome IN ('NOT_FOUND', 'INVALID_EVIDENCE', 'ANOMALY', 'ERROR') "
            "OR anomaly_code IS NOT NULL"
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


def downgrade() -> None:
    for table in GUARDED:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_update_delete ON {table}")
    op.drop_index("ix_ppr_sync_observation_governance", table_name="ppr_sync_observation")
    op.drop_index("uq_ppr_sync_observation_released", table_name="ppr_sync_observation")
    op.drop_index("ix_ppr_sync_observation_run", table_name="ppr_sync_observation")
    op.drop_index("ix_ppr_sync_observation_ppr", table_name="ppr_sync_observation")
    op.drop_table("ppr_sync_observation")
    op.drop_index("uq_sync_run_one_running_pprs", table_name="sync_run")
    op.drop_constraint(op.f("ck_sync_run_retry_iff_retry"), "sync_run", type_="check")
    op.drop_constraint(op.f("ck_sync_run_ppr_iff_single"), "sync_run", type_="check")
    op.drop_constraint(
        op.f("fk_sync_run_retry_of_sync_run_id_sync_run"), "sync_run", type_="foreignkey"
    )
    op.drop_constraint(op.f("fk_sync_run_ppr_id_ppr"), "sync_run", type_="foreignkey")
    op.drop_column("sync_run", "retry_of_sync_run_id")
    op.drop_column("sync_run", "ppr_id")
