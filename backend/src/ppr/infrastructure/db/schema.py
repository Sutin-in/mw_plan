"""Physical schema - Wave 1B persistence foundation (spec §29, ADR-004).

Tables in this wave:
* HOSxP master mirrors (§8, §29.1) keyed by the HOSxP source identifier.
* ``plan_year`` (§7, D-08).
* Identity/authorization: ``app_user``, ``role``, ``user_role`` (§22, §29.6).
* ``role_appointment`` for the annual Head of Procurement (§23, §29.5).
* ``audit_log`` - append-only, enforced by a database trigger (§36).
* ``sync_run`` / ``sync_change`` (§25.4, §29.7).
* Wave 2 (Plan Core): ``plan_budget``, ``plan_item``, ``plan_item_demand`` and the
  append-only ``plan_ledger`` (§9, §10, §18, §29.2, §29.4).
* Wave 6 (PR sync): append-only ``ppr_sync_observation``; ``sync_run`` scopes for PPRs
  (D-29 ... D-31).
* Wave 8A: ``alert_setting`` - the configurable alert thresholds (D-33).
* Wave 4 (PPR): ``ppr``, ``ppr_item``, ``ppr_category_allocation``, append-only
  ``ppr_version`` snapshots, the lifetime ``ppr_binding`` and ``ppr_sequence`` (§14,
  §15, §17, §29.3, §30; D-03 ... D-06, D-19).

PPR tables arrive in their own waves. Column names are this system's
names; they do not describe HOSxP tables or fields.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint

from ppr.domain.alerts import SETTING_RANGES
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.ppr_state import PprState
from ppr.domain.pr_sync import EvidenceClass, SyncOutcome
from ppr.domain.roles import Role

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=NAMING)


def _in(column: str, values: list[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


ROLE_CODES = [r.value for r in Role]
PLAN_YEAR_STATES = [s.value for s in PlanYearState]
SYNC_MODES = ["MANUAL", "NIGHTLY"]
SYNC_STATUSES = ["RUNNING", "SUCCEEDED", "PARTIAL", "FAILED"]
ACTOR_KINDS = ["USER", "SYSTEM"]
APPOINTMENT_STATUSES = ["ACTIVE", "REVOKED"]


# ------------------------------------------------------------------ HOSxP mirrors
def _mirror(name: str, *extra: Column[Any]) -> Table:
    return Table(
        name,
        metadata,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("source_id", Text, nullable=False, unique=True),
        Column("code", Text, nullable=False),
        Column("name", Text, nullable=False),
        *extra,
        Column("active", Boolean, nullable=False),
        Column("synced_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


hosxp_department = _mirror("hosxp_department")
hosxp_item = _mirror("hosxp_item", Column("unit", Text, nullable=False))
hosxp_fund_source = _mirror("hosxp_fund_source")
hosxp_budget_category = _mirror(
    "hosxp_budget_category",
    # Parent by HOSxP source id; no FK so any sync order works and depth is unbounded (§8.1).
    Column("parent_source_id", Text, nullable=True),
)
Index("ix_hosxp_budget_category_parent", hosxp_budget_category.c.parent_source_id)

# ------------------------------------------------------------------ identity / RBAC
app_user = Table(
    "app_user",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("hosxp_source_id", Text, nullable=False, unique=True),
    Column("username", Text, nullable=False),
    Column("display_name", Text, nullable=True),
    Column("department_source_id", Text, nullable=True),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_login_at", DateTime(timezone=True), nullable=True),
)

role = Table(
    "role",
    metadata,
    Column("code", Text, primary_key=True),
    Column("description", Text, nullable=False),
    CheckConstraint(_in("code", ROLE_CODES), name="known_code"),
)

user_role = Table(
    "user_role",
    metadata,
    Column("user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("role_code", Text, ForeignKey("role.code"), nullable=False),
    Column("granted_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("granted_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("user_id", "role_code"),
)

role_appointment = Table(
    "role_appointment",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("role_code", Text, ForeignKey("role.code"), nullable=False),
    Column("fiscal_year", Integer, nullable=False),
    Column("effective_from", Date, nullable=False),
    Column("effective_to", Date, nullable=True),
    Column("status", Text, nullable=False, server_default=text("'ACTIVE'")),
    Column("created_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("role_code = 'HEAD_OF_PROCUREMENT'", name="appointable_role"),
    CheckConstraint(_in("status", APPOINTMENT_STATUSES), name="known_status"),
    CheckConstraint(
        "effective_to IS NULL OR effective_to >= effective_from", name="effective_range"
    ),
    # Wave 5B (migration 0005): revocation evidence; D-27 no overlap of ACTIVE appointments.
    Column("revoked_at", DateTime(timezone=True), nullable=True),
    Column("revoked_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("revoke_reason", Text, nullable=True),
    CheckConstraint(
        "(status = 'REVOKED') = (revoked_at IS NOT NULL)", name="revoked_iff_timestamped"
    ),
    ExcludeConstraint(
        (text("daterange(effective_from, effective_to, '[]')"), "&&"),
        where=text("status = 'ACTIVE'"),
        using="gist",
        name="no_overlapping_active",
    ),
)

# ------------------------------------------------------------------ plan year
plan_year = Table(
    "plan_year",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("fiscal_year", Integer, nullable=False, unique=True),
    Column("state", Text, nullable=False, server_default=text("'DRAFT'")),
    Column("starts_on", Date, nullable=False),
    Column("ends_on", Date, nullable=False),
    Column("created_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(_in("state", PLAN_YEAR_STATES), name="known_state"),
    CheckConstraint("fiscal_year BETWEEN 1000 AND 9999", name="four_digit_year"),
    CheckConstraint("ends_on > starts_on", name="range"),
)

# ------------------------------------------------------------------ audit (append-only)
audit_log = Table(
    "audit_log",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("occurred_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("actor_kind", Text, nullable=False),
    Column("actor_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("action", Text, nullable=False),
    Column("entity_type", Text, nullable=False),
    Column("entity_id", Text, nullable=False),
    Column("before", JSONB, nullable=True),
    Column("after", JSONB, nullable=True),
    Column("reason", Text, nullable=True),
    CheckConstraint(_in("actor_kind", ACTOR_KINDS), name="known_actor_kind"),
    CheckConstraint(
        "(actor_kind = 'USER' AND actor_user_id IS NOT NULL) OR "
        "(actor_kind = 'SYSTEM' AND actor_user_id IS NULL)",
        name="actor_consistent",
    ),
)
Index("ix_audit_log_entity", audit_log.c.entity_type, audit_log.c.entity_id)
Index("ix_audit_log_occurred_at", audit_log.c.occurred_at)

# ------------------------------------------------------------------ sync log (§25.4)
sync_run = Table(
    "sync_run",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("mode", Text, nullable=False),
    Column("requested_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("started_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("scope", Text, nullable=False),
    Column("records_checked", Integer, nullable=False, server_default=text("0")),
    Column("records_created", Integer, nullable=False, server_default=text("0")),
    Column("records_updated", Integer, nullable=False, server_default=text("0")),
    Column("records_changed", Integer, nullable=False, server_default=text("0")),
    Column("records_cancelled", Integer, nullable=False, server_default=text("0")),
    Column("status", Text, nullable=False, server_default=text("'RUNNING'")),
    Column("error_details", JSONB, nullable=True),
    CheckConstraint(_in("mode", SYNC_MODES), name="known_mode"),
    CheckConstraint(_in("status", SYNC_STATUSES), name="known_status"),
    CheckConstraint(
        "mode <> 'MANUAL' OR requested_by_user_id IS NOT NULL", name="manual_has_requester"
    ),
    CheckConstraint(
        "records_checked >= 0 AND records_created >= 0 AND records_updated >= 0 "
        "AND records_changed >= 0 AND records_cancelled >= 0",
        name="non_negative_counts",
    ),
    CheckConstraint("finished_at IS NULL OR finished_at >= started_at", name="finish_after_start"),
    # Wave 6 (0006): one-PPR runs name the PPR; a retry names the run it retries.
    Column("ppr_id", BigInteger, ForeignKey("ppr.id"), nullable=True),
    Column("retry_of_sync_run_id", BigInteger, ForeignKey("sync_run.id"), nullable=True),
    CheckConstraint(
        "(scope IN ('PPR', 'PPR_RECHECK')) = (ppr_id IS NOT NULL)", name="ppr_iff_single"
    ),
    CheckConstraint(
        "(scope = 'PPR_RETRY') = (retry_of_sync_run_id IS NOT NULL)", name="retry_iff_retry"
    ),
)
# At most one full PPR synchronization runs at a time (a second one is refused).
Index(
    "uq_sync_run_one_running_pprs",
    sync_run.c.scope,
    unique=True,
    postgresql_where=text("status = 'RUNNING' AND scope = 'PPRS'"),
)

sync_change = Table(
    "sync_change",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("sync_run_id", BigInteger, ForeignKey("sync_run.id"), nullable=False),
    Column("entity_type", Text, nullable=False),
    Column("source_id", Text, nullable=False),
    Column("change_kind", Text, nullable=False),
    Column("is_critical", Boolean, nullable=False, server_default=text("false")),
    Column("details", JSONB, nullable=True),
    Column("detected_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("sync_run_id", "entity_type", "source_id", "change_kind"),
)
Index("ix_sync_change_run", sync_change.c.sync_run_id)


# ================================================================== Wave 2: Plan Core
PLAN_TYPES = ["DEPARTMENT", "CENTRAL", "ASSIGNED"]
DEMAND_STATES = ["PLANNED", "INCLUDED_IN_CENTRAL_PURCHASE", "FULFILLED", "CANCELLED"]
LEDGER_EVENTS = [
    "PLAN_ACTIVATED",
    "PPR_CONFIRMED",
    "PPR_RELEASED",
    "PLAN_AMENDMENT",
]
# D-39: stock issues / returns are HOSxP data shown in reports, never Plan Ledger events.
STOCK_MOVEMENT_EVENTS = ["CENTRAL_STOCK_ISSUE", "CENTRAL_STOCK_RETURN"]
MONEY = Numeric(18, 2)
QTY = Numeric(18, 4)

plan_budget = Table(
    "plan_budget",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("plan_year_id", BigInteger, ForeignKey("plan_year.id"), nullable=False),
    Column(
        "fund_source_source_id",
        Text,
        ForeignKey("hosxp_fund_source.source_id"),
        nullable=False,
    ),
    Column(
        "budget_category_source_id",
        Text,
        ForeignKey("hosxp_budget_category.source_id"),
        nullable=False,
    ),
    Column("approved_amount", MONEY, nullable=False),
    Column("created_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint(
        "plan_year_id",
        "fund_source_source_id",
        "budget_category_source_id",
        name="uq_plan_budget_line",
    ),
    UniqueConstraint("id", "plan_year_id", name="uq_plan_budget_id_year"),
    CheckConstraint("approved_amount >= 0", name="non_negative_amount"),
)

# Wave 12A-1 (D-42): a plan imported from Excel. Removing an import (DRAFT years only) deletes
# its Plan Items and marks the record REMOVED; the record and the audit trail stay.
PLAN_IMPORT_STATES = ["IMPORTED", "REMOVED"]

plan_import = Table(
    "plan_import",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("plan_year_id", BigInteger, ForeignKey("plan_year.id"), nullable=False),
    Column("file_name", Text, nullable=False),
    Column("file_sha256", Text, nullable=False),
    Column("rows_imported", Integer, nullable=False),
    Column("rows_skipped", Integer, nullable=False),
    Column("total_amount", MONEY, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'IMPORTED'")),
    Column("imported_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("imported_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("removed_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("removed_at", DateTime(timezone=True), nullable=True),
    Column("removed_reason", Text, nullable=True),
    CheckConstraint(_in("state", PLAN_IMPORT_STATES), name="known_state"),
    CheckConstraint("file_sha256 ~ '^[0-9a-f]{64}$'", name="sha256_form"),
    CheckConstraint("rows_imported > 0 AND rows_skipped >= 0", name="row_counts"),
    CheckConstraint("total_amount >= 0", name="non_negative_amount"),
    CheckConstraint(
        "(state = 'REMOVED') = (removed_at IS NOT NULL AND removed_by_user_id IS NOT NULL "
        "AND length(trim(coalesce(removed_reason, ''))) > 0)",
        name="removed_has_details",
    ),
    Index(
        "uq_plan_import_live_file",
        "plan_year_id",
        "file_sha256",
        unique=True,
        postgresql_where=text("state = 'IMPORTED'"),
    ),
)

plan_item = Table(
    "plan_item",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("plan_year_id", BigInteger, nullable=False),
    Column("plan_budget_id", BigInteger, nullable=False),
    Column("plan_type", Text, nullable=False),
    # D-42 / D-43: a Plan Item may have no HOSxP item (its own name and unit).
    Column("item_source_id", Text, ForeignKey("hosxp_item.source_id"), nullable=True),
    Column("item_code_snapshot", Text, nullable=True),
    Column("item_name_snapshot", Text, nullable=False),
    Column("unit_snapshot", Text, nullable=False),
    Column(
        "owner_department_source_id",
        Text,
        ForeignKey("hosxp_department.source_id"),
        nullable=False,
    ),
    Column(
        "purchasing_department_source_id",
        Text,
        ForeignKey("hosxp_department.source_id"),
        nullable=True,
    ),
    Column("planned_qty", QTY, nullable=False),
    Column("estimated_unit_price", MONEY, nullable=False),
    Column("planned_amount", MONEY, nullable=False),
    Column("q1", QTY, nullable=True),
    Column("q2", QTY, nullable=True),
    Column("q3", QTY, nullable=True),
    Column("q4", QTY, nullable=True),
    Column("note", Text, nullable=True),
    Column("created_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("plan_import_id", BigInteger, ForeignKey("plan_import.id"), nullable=True),
    Column("source_sheet", Text, nullable=True),
    Column("source_row", Integer, nullable=True),
    CheckConstraint(
        "(item_source_id IS NULL) = (item_code_snapshot IS NULL)", name="code_snapshot_with_code"
    ),
    CheckConstraint(
        "(plan_import_id IS NULL) = (source_row IS NULL)", name="source_row_with_import"
    ),
    # A Plan Item's budget line must belong to the same plan year.
    ForeignKeyConstraint(
        ["plan_budget_id", "plan_year_id"],
        ["plan_budget.id", "plan_budget.plan_year_id"],
        name="fk_plan_item_budget_same_year",
    ),
    CheckConstraint(_in("plan_type", PLAN_TYPES), name="known_plan_type"),
    CheckConstraint("planned_qty >= 0", name="non_negative_qty"),  # D-37: 0 closes an item
    CheckConstraint("estimated_unit_price >= 0", name="non_negative_price"),
    CheckConstraint("planned_amount >= 0", name="non_negative_amount"),
    CheckConstraint(
        "coalesce(q1, 0) >= 0 AND coalesce(q2, 0) >= 0 AND coalesce(q3, 0) >= 0 "
        "AND coalesce(q4, 0) >= 0",
        name="non_negative_quarters",
    ),
)
Index("ix_plan_item_year", plan_item.c.plan_year_id)
Index("ix_plan_item_owner", plan_item.c.owner_department_source_id)

plan_item_demand = Table(
    "plan_item_demand",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "plan_item_id",
        BigInteger,
        ForeignKey("plan_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "department_source_id",
        Text,
        ForeignKey("hosxp_department.source_id"),
        nullable=False,
    ),
    Column("demand_qty", QTY, nullable=False),
    Column("state", Text, nullable=False, server_default=text("'PLANNED'")),
    UniqueConstraint("plan_item_id", "department_source_id", name="uq_plan_item_demand_dept"),
    # Wave 7B-2 (migration 0009, D-38 A-6): an amendment may reduce a demand to 0, which is
    # exactly the CANCELLED state.
    CheckConstraint("demand_qty >= 0", name="non_negative_qty"),
    CheckConstraint("(demand_qty = 0) = (state = 'CANCELLED')", name="cancelled_iff_zero"),
    CheckConstraint(_in("state", DEMAND_STATES), name="known_state"),
)

# Append-only Plan Usage Ledger (spec §18). Sign rules mirror ppr.domain.ledger (D-01).
plan_ledger = Table(
    "plan_ledger",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("plan_item_id", BigInteger, ForeignKey("plan_item.id"), nullable=False),
    Column("event_type", Text, nullable=False),
    Column("qty_delta", QTY, nullable=False),
    Column("amount_delta", MONEY, nullable=False),
    Column("idempotency_key", Text, nullable=False, unique=True),
    Column("ppr_ref", Text, nullable=True),
    Column("actor_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("plan_amendment_id", BigInteger, ForeignKey("plan_amendment.id"), nullable=True),
    CheckConstraint(_in("event_type", LEDGER_EVENTS), name="known_event"),
    CheckConstraint(
        "event_type <> 'PLAN_AMENDMENT' OR plan_amendment_id IS NOT NULL",
        name="amendment_has_source",
    ),
    CheckConstraint(
        "(event_type <> 'PLAN_ACTIVATED' OR (qty_delta >= 0 AND amount_delta >= 0)) AND "
        "(event_type <> 'PPR_CONFIRMED' OR (qty_delta < 0 AND amount_delta <= 0)) AND "
        "(event_type <> 'PPR_RELEASED' OR (qty_delta >= 0 AND amount_delta >= 0 "
        "AND (qty_delta > 0 OR amount_delta > 0))) AND "
        "(event_type <> 'PLAN_AMENDMENT' OR (qty_delta <> 0 OR amount_delta <> 0))",
        name="event_signs",
    ),
    CheckConstraint(
        "event_type NOT IN ('CENTRAL_STOCK_ISSUE', 'CENTRAL_STOCK_RETURN')",
        name="no_stock_movement",
    ),
    CheckConstraint(
        "event_type NOT IN ('PPR_CONFIRMED', 'PPR_RELEASED') OR ppr_ref IS NOT NULL",
        name="ppr_ref_required",
    ),
)
Index("ix_plan_ledger_item", plan_ledger.c.plan_item_id)
# One PLAN_ACTIVATED per plan item.
Index(
    "uq_plan_ledger_one_activation",
    plan_ledger.c.plan_item_id,
    unique=True,
    postgresql_where=text("event_type = 'PLAN_ACTIVATED'"),
)


# ================================================================== Wave 4: PPR
PPR_STATES = [st.value for st in PprState]

# One row per PPR (a draft, or a confirmed PPR and its later states). The PR itself stays
# in HOSxP; ``hosxp_pr_status`` is the separate, synchronized external status (D-02).
ppr = Table(
    "ppr",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("pr_no", Text, nullable=False),
    Column("fiscal_year", Integer, nullable=False),
    Column("department_source_id", Text, nullable=False),
    Column("fund_source_source_id", Text, nullable=False),
    Column("pr_budget_category_source_id", Text, nullable=True),
    Column("state", Text, nullable=False, server_default=text("'DRAFT'")),
    Column("ppr_number", Text, nullable=True, unique=True),
    Column("current_version", Integer, nullable=False, server_default=text("0")),
    Column("required_amount", MONEY, nullable=False),
    Column("adjustment_reference", Text, nullable=True),
    Column("hosxp_pr_status", Text, nullable=False),
    Column("hosxp_native_status", Text, nullable=True),
    # The PR exactly as retrieved when the draft was prepared (D-21 comparison basis).
    Column("draft_pr", JSONB, nullable=False),
    Column("created_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("discarded_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(_in("state", PPR_STATES), name="known_state"),
    # D-05: a draft is unnumbered; everything past DRAFT carries its number (D-06).
    CheckConstraint("(state = 'DRAFT') = (ppr_number IS NULL)", name="number_iff_confirmed"),
    CheckConstraint("discarded_at IS NULL OR state = 'DRAFT'", name="only_drafts_discarded"),
    CheckConstraint("current_version >= 0", name="version_non_negative"),
    CheckConstraint("required_amount >= 0", name="non_negative_amount"),
)
# D-05: at most one working (not discarded) draft per PR number.
Index(
    "uq_ppr_one_working_draft",
    ppr.c.pr_no,
    unique=True,
    postgresql_where=text("state = 'DRAFT' AND discarded_at IS NULL"),
)
Index("ix_ppr_department", ppr.c.department_source_id)

# D-03: a PR number is bound to at most ONE confirmed PPR, for life. Unconditional
# uniqueness (primary key), never removed - also not when the PPR is cancelled.
ppr_binding = Table(
    "ppr_binding",
    metadata,
    Column("pr_no", Text, primary_key=True),
    Column("ppr_id", BigInteger, ForeignKey("ppr.id"), nullable=False, unique=True),
    Column("bound_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

# Current PR lines of the PPR (from HOSxP only, §11.4) and their matched Plan Items.
ppr_item = Table(
    "ppr_item",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ppr_id", BigInteger, ForeignKey("ppr.id", ondelete="CASCADE"), nullable=False),
    Column("pr_item_id", Text, nullable=False),
    Column("item_source_id", Text, nullable=False),
    Column("item_name", Text, nullable=True),
    Column("unit", Text, nullable=True),
    Column("qty", QTY, nullable=False),
    # Full HOSxP precision (the PR screen shows 8 decimals); not used by the ledger.
    Column("unit_price", Numeric(20, 8), nullable=False),
    Column("amount", MONEY, nullable=False),
    Column("plan_item_id", BigInteger, ForeignKey("plan_item.id"), nullable=False),
    UniqueConstraint("ppr_id", "pr_item_id", name="uq_ppr_item_line"),
    CheckConstraint("qty > 0", name="positive_qty"),
    CheckConstraint("amount >= 0", name="non_negative_amount"),
)

# Wave 12A-2 (D-43): the plan row the requester chose for each PR line of a PPR. Kept apart
# from ppr_item (which is replaced at every confirmation) so a choice survives until the
# requester changes it; one row per PPR and PR line.
ppr_line_choice = Table(
    "ppr_line_choice",
    metadata,
    Column("ppr_id", BigInteger, ForeignKey("ppr.id", ondelete="CASCADE"), nullable=False),
    Column("pr_item_id", Text, nullable=False),
    Column("plan_item_id", BigInteger, ForeignKey("plan_item.id"), nullable=False),
    # the row's name and unit when it was chosen: a row renamed since is chosen again (D-43)
    Column("plan_item_name", Text, nullable=False),
    Column("plan_item_unit", Text, nullable=False),
    Column("chosen_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("chosen_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("ppr_id", "pr_item_id", name="pk_ppr_line_choice"),
)
Index("ix_ppr_item_ppr", ppr_item.c.ppr_id)

# D-04 / D-19: header-level budget-category allocation (never posts to the ledger).
ppr_category_allocation = Table(
    "ppr_category_allocation",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ppr_id", BigInteger, ForeignKey("ppr.id", ondelete="CASCADE"), nullable=False),
    Column("budget_category_source_id", Text, nullable=False),
    Column("amount", MONEY, nullable=False),
    UniqueConstraint("ppr_id", "budget_category_source_id", name="uq_ppr_allocation_category"),
    CheckConstraint("amount > 0", name="positive_amount"),
)

# Append-only evidence of every confirmation (§15.3, §35): what was confirmed, by whom,
# against which PR data, with which remaining balances. A trigger forbids UPDATE/DELETE.
ppr_version = Table(
    "ppr_version",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ppr_id", BigInteger, ForeignKey("ppr.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("snapshot", JSONB, nullable=False),
    Column("document_code", Text, nullable=False, unique=True),
    Column("confirmed_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("confirmed_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    UniqueConstraint("ppr_id", "version", name="uq_ppr_version_number"),
    CheckConstraint("version >= 1", name="positive_version"),
)

# Wave 5B, D-28: append-only evidence of each Head of Procurement verification: which
# version, by whom, under which appointment. One verification per PPR version.
ppr_verification = Table(
    "ppr_verification",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("ppr_id", BigInteger, ForeignKey("ppr.id"), nullable=False),
    Column("version", Integer, nullable=False),
    Column("appointment_id", BigInteger, ForeignKey("role_appointment.id"), nullable=False),
    Column("verified_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=False),
    Column("verified_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # The server date the authority was judged on (D-28), independent of DB time zone.
    Column("verified_on", Date, nullable=False),
    UniqueConstraint("ppr_id", "version", name="uq_ppr_verification_version"),
    CheckConstraint("version >= 1", name="positive_version"),
)

# Wave 5B search (§32).
Index("ix_ppr_fiscal_year_state", ppr.c.fiscal_year, ppr.c.state)
Index("ix_ppr_pr_no", ppr.c.pr_no)
Index("ix_ppr_item_item", ppr_item.c.item_source_id)

# D-06: per-fiscal-year sequence (unique, gaps allowed).
ppr_sequence = Table(
    "ppr_sequence",
    metadata,
    Column("fiscal_year", Integer, primary_key=True),
    Column("last_value", Integer, nullable=False),
    CheckConstraint("last_value BETWEEN 0 AND 999999", name="range"),
)


# ================================================================== Wave 6: PR sync
SYNC_OUTCOMES = [o.value for o in SyncOutcome]
EVIDENCE_CLASSES = [c.value for c in EvidenceClass]

# D-29 ... D-31: one append-only row per attempt to observe one PPR's bound PR.
ppr_sync_observation = Table(
    "ppr_sync_observation",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("sync_run_id", BigInteger, ForeignKey("sync_run.id"), nullable=False),
    Column("ppr_id", BigInteger, ForeignKey("ppr.id"), nullable=False),
    Column("pr_no", Text, nullable=False),
    # The confirmed version whose PR snapshot the observation was compared with.
    Column("compared_version", Integer, nullable=True),
    # When HOSxP answered (NULL when nothing was observed, e.g. unavailable).
    Column("observed_at", DateTime(timezone=True), nullable=True),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("outcome", Text, nullable=False),
    Column("evidence_class", Text, nullable=True),
    Column("hosxp_status", Text, nullable=True),
    Column("native_status", Text, nullable=True),
    Column("changes", JSONB, nullable=True),
    Column("evidence_issues", JSONB, nullable=True),
    # Canonical PR snapshot form only (application.pr_snapshot); never raw rows.
    Column("live_pr", JSONB, nullable=True),
    # Technical evidence, e.g. a discarded inconsistent first read (D-29).
    Column("details", JSONB, nullable=True),
    Column("state_before", Text, nullable=False),
    Column("state_after", Text, nullable=False),
    Column("released", Boolean, nullable=False, server_default=text("false")),
    Column("anomaly_code", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_message", Text, nullable=True),
    CheckConstraint(_in("outcome", SYNC_OUTCOMES), name="known_outcome"),
    CheckConstraint(
        "evidence_class IS NULL OR " + _in("evidence_class", EVIDENCE_CLASSES),
        name="known_evidence_class",
    ),
    CheckConstraint(
        "(outcome = 'INVALID_EVIDENCE') = (evidence_class IS NOT NULL)",
        name="class_iff_invalid",
    ),
    CheckConstraint(_in("state_before", PPR_STATES), name="known_state_before"),
    CheckConstraint(_in("state_after", PPR_STATES), name="known_state_after"),
    CheckConstraint(
        "NOT released OR (outcome = 'CANCELLED' AND state_after = 'CANCELLED')",
        name="release_only_on_cancel",
    ),
    CheckConstraint("state_after <> 'CLOSED'", name="never_closes"),
)
Index("ix_ppr_sync_observation_ppr", ppr_sync_observation.c.ppr_id, ppr_sync_observation.c.id)
Index("ix_ppr_sync_observation_run", ppr_sync_observation.c.sync_run_id)
# D-30: a PPR's plan usage is released by synchronization at most once, ever.
Index(
    "uq_ppr_sync_observation_released",
    ppr_sync_observation.c.ppr_id,
    unique=True,
    postgresql_where=text("released"),
)
# Governance list (NOT_FOUND, invalid evidence, anomalies, errors).
Index(
    "ix_ppr_sync_observation_governance",
    ppr_sync_observation.c.recorded_at,
    postgresql_where=text(
        "outcome IN ('NOT_FOUND', 'INVALID_EVIDENCE', 'ANOMALY', 'ERROR') "
        "OR anomaly_code IS NOT NULL"
    ),
)


# ---------------------------------------------------------------------- Wave 8A (D-33)
ALERT_SETTING_CODES = [s.value for s in SETTING_RANGES]
alert_setting = Table(
    "alert_setting",
    metadata,
    Column("code", Text, primary_key=True),
    Column("value", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_by_user_id", BigInteger, ForeignKey("app_user.id"), nullable=True),
    CheckConstraint(_in("code", ALERT_SETTING_CODES), name="known_code"),
    CheckConstraint(
        " AND ".join(
            f"(code <> '{s.value}' OR value BETWEEN {lo} AND {hi})"
            for s, (lo, hi) in SETTING_RANGES.items()
        ),
        name="value_in_range",
    ),
)


# Wave 7A (migration 0008, D-37): the Plan Amendment history tables live in their own module
# and register themselves on ``metadata``; so do the Wave 7B-2 coverage tables (0009).
# Imported last, once ``metadata`` exists.
from ppr.infrastructure.db import schema_amendment, schema_assigned  # noqa: E402, F401
