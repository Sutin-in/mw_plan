"""Wave 7A: append-only Plan Amendment history tables (migration 0008, D-37 7A, spec §20).

Registered on the shared ``metadata`` of ``ppr.infrastructure.db.schema``.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from ppr.infrastructure.db.schema import metadata

plan_amendment = Table(
    "plan_amendment",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("plan_year_id", BigInteger, ForeignKey("plan_year.id"), nullable=False),
    Column("amendment_no", Integer, nullable=False),
    Column("approval_document_no", Text, nullable=False),
    Column("approval_date", Date, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("plan_year_id", "amendment_no", name="uq_plan_amendment_year_no"),
    CheckConstraint("amendment_no > 0", name="positive_no"),
    CheckConstraint("length(trim(approval_document_no)) > 0", name="document_no_present"),
    CheckConstraint("length(trim(reason)) > 0", name="reason_present"),
)

plan_amendment_change = Table(
    "plan_amendment_change",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("plan_amendment_id", BigInteger, ForeignKey("plan_amendment.id"), nullable=False),
    Column("target", Text, nullable=False),
    Column("target_id", BigInteger, nullable=False),
    Column("change_kind", Text, nullable=False),
    Column("before", JSONB, nullable=True),
    Column("after", JSONB, nullable=False),
    UniqueConstraint(
        "plan_amendment_id", "target", "target_id", name="uq_plan_amendment_change_target"
    ),
    # DEMAND: Assigned Purchase demand rows (Wave 7B-2, migration 0009, D-38 A-6)
    CheckConstraint("target IN ('BUDGET', 'ITEM', 'DEMAND')", name="known_target"),
    CheckConstraint("change_kind IN ('CREATE', 'UPDATE')", name="known_kind"),
    CheckConstraint("(change_kind = 'CREATE') = (before IS NULL)", name="before_matches_kind"),
)
