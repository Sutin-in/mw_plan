"""Wave 7B-2: Assigned Purchase coverage (migration 0009, D-38 A-2 ... A-5).

``ppr_coverage`` says, for a PPR drawing on an ASSIGNED Plan Item, how much it buys for each
demand department. ``WORKING`` rows are edited while the PPR is a draft or unlocked;
confirmation (and reconfirmation) replaces the PPR's ``CONFIRMED`` rows with them. Demand
states are derived from the CONFIRMED rows of PPRs still in effect. Each confirmed version's
coverage is also kept in its append-only version snapshot.

Registered on the shared ``metadata`` of ``ppr.infrastructure.db.schema``.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Table,
    Text,
    UniqueConstraint,
)

from ppr.infrastructure.db.schema import QTY, metadata

ppr_coverage = Table(
    "ppr_coverage",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ppr_id", BigInteger, ForeignKey("ppr.id", ondelete="CASCADE"), nullable=False),
    Column("plan_item_id", BigInteger, ForeignKey("plan_item.id"), nullable=False),
    Column(
        "department_source_id",
        Text,
        ForeignKey("hosxp_department.source_id"),
        nullable=False,
    ),
    Column("qty", QTY, nullable=False),
    Column("confirmed", Boolean, nullable=False),
    UniqueConstraint(
        "ppr_id", "plan_item_id", "department_source_id", "confirmed", name="uq_ppr_coverage_row"
    ),
    CheckConstraint("qty > 0", name="positive_qty"),
)
Index("ix_ppr_coverage_item", ppr_coverage.c.plan_item_id)
