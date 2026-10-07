"""Ports used by the application layer (implemented in ``ppr.infrastructure``).

Application services depend on these Protocols only, so they never import
SQLAlchemy or FastAPI (enforced by import-linter).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

from ppr.domain.alerts import AlertSetting
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.ledger import LedgerBalance, LedgerEntry
from ppr.domain.plan import DemandState, PlanType
from ppr.domain.ppr_state import PprState
from ppr.domain.pr_binding import PrBinding
from ppr.domain.roles import Role
from ppr.integration.hosxp.contracts import BudgetCategory, Department, FundSource, Item


@dataclass(frozen=True)
class Actor:
    """Who performs an audited action: a local user, or the system (user_id None)."""

    user_id: int | None

    @property
    def kind(self) -> str:
        return "SYSTEM" if self.user_id is None else "USER"


SYSTEM_ACTOR = Actor(None)


@dataclass(frozen=True)
class AuditEntry:
    actor: Actor
    action: str
    entity_type: str
    entity_id: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None
    reason: str | None = None


@dataclass(frozen=True)
class AuditRecord:
    id: int
    occurred_at: datetime
    actor_kind: str
    actor_user_id: int | None
    action: str
    entity_type: str
    entity_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    reason: str | None


@dataclass(frozen=True)
class UserRecord:
    id: int
    hosxp_source_id: str
    username: str
    display_name: str | None
    department_source_id: str | None
    active: bool
    roles: frozenset[Role] = field(default_factory=frozenset)


@dataclass(frozen=True)
class PlanYearRecord:
    fiscal_year: int
    state: PlanYearState
    starts_on: date
    ends_on: date


class AuditWriter(Protocol):
    def write(self, entry: AuditEntry) -> None: ...

    def recent(self, limit: int) -> list[AuditRecord]: ...

    def for_entity(self, entity_type: str, entity_id: str) -> list[AuditRecord]:
        """Every audit record of one entity, oldest first (timeline, §33)."""
        ...

    def search(
        self,
        since: datetime | None,
        until: datetime | None,
        action: str | None,
        entity_type: str | None,
        limit: int,
    ) -> list[AuditRecord]:
        """Audit rows in [since, until), optionally one action / entity type, newest first."""
        ...


class UserRepository(Protocol):
    def upsert_from_login(
        self,
        hosxp_source_id: str,
        username: str,
        display_name: str | None,
        department_source_id: str | None,
    ) -> UserRecord: ...

    def get(self, user_id: int) -> UserRecord | None: ...

    def get_by_username(self, username: str) -> UserRecord | None: ...

    def grant_role(self, user_id: int, role: Role, granted_by: int | None) -> bool: ...

    def revoke_role(self, user_id: int, role: Role) -> bool: ...

    def with_role(self, role: Role) -> list[UserRecord]: ...


class PlanYearRepository(Protocol):
    def list(self) -> list[PlanYearRecord]: ...

    def get(self, fiscal_year: int, *, for_update: bool = False) -> PlanYearRecord | None: ...

    def create(self, record: PlanYearRecord, created_by: int | None) -> None: ...

    def set_state(self, fiscal_year: int, state: PlanYearState) -> None: ...


# ------------------------------------------------------------------ Wave 2 records
class MasterKind(StrEnum):
    DEPARTMENT = "DEPARTMENT"
    ITEM = "ITEM"
    FUND_SOURCE = "FUND_SOURCE"
    BUDGET_CATEGORY = "BUDGET_CATEGORY"


MasterRecord = Department | Item | FundSource | BudgetCategory


@dataclass(frozen=True)
class MasterRow:
    kind: MasterKind
    source_id: str
    code: str
    name: str
    active: bool
    unit: str | None = None
    parent_source_id: str | None = None


@dataclass(frozen=True)
class SyncRunRecord:
    id: int
    mode: str
    scope: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    records_checked: int
    records_created: int
    records_updated: int
    error_details: dict[str, Any] | None
    # Wave 6 (PR synchronization)
    requested_by_user_id: int | None = None
    records_changed: int = 0
    records_cancelled: int = 0
    ppr_id: int | None = None
    retry_of_sync_run_id: int | None = None


@dataclass(frozen=True)
class Correction:
    """A data-entry correction made while the plan year is APPROVED (D-15)."""

    reason: str
    source_reference: str


@dataclass(frozen=True)
class BudgetRecord:
    id: int
    fiscal_year: int
    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal


@dataclass(frozen=True)
class DemandInput:
    department_id: str
    demand_qty: Decimal


@dataclass(frozen=True)
class DemandRecord:
    department_id: str
    demand_qty: Decimal
    state: DemandState
    id: int | None = None


@dataclass(frozen=True)
class PlanItemInput:
    plan_budget_id: int
    plan_type: PlanType
    item_id: str | None  # None: a row without a HOSxP item (D-42, D-43)
    owner_department_id: str
    purchasing_department_id: str | None
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    quarters: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None] = (
        None,
        None,
        None,
        None,
    )
    note: str | None = None
    demands: tuple[DemandInput, ...] = ()
    # D-43: the name and unit of a row without a HOSxP item (else the HOSxP item's are used)
    item_name: str | None = None
    unit: str | None = None


@dataclass(frozen=True)
class ItemSnapshot:
    code: str | None  # None: a row without a HOSxP item (D-42, D-43)
    name: str
    unit: str


@dataclass(frozen=True)
class PlanItemRecord:
    id: int
    fiscal_year: int
    plan_budget_id: int
    fund_source_id: str
    budget_category_id: str
    plan_type: PlanType
    item_id: str | None  # None: imported row not yet bound to a HOSxP item (D-42)
    item_code: str | None
    item_name: str
    unit: str
    owner_department_id: str
    purchasing_department_id: str | None
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    quarters: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]
    note: str | None
    demands: tuple[DemandRecord, ...]
    # Wave 12A-1 (D-42): where an imported row came from
    plan_import_id: int | None = None
    source_sheet: str | None = None
    source_row: int | None = None


class MasterRepository(Protocol):
    def upsert(self, kind: MasterKind, records: Sequence[MasterRecord]) -> tuple[int, int]:
        """Insert or update by source id; returns (created, updated)."""
        ...

    def get(self, kind: MasterKind, source_id: str) -> MasterRow | None: ...

    def list(self, kind: MasterKind) -> list[MasterRow]: ...


class SyncRunRepository(Protocol):
    def start(
        self,
        mode: str,
        scope: str,
        requested_by: int | None,
        *,
        ppr_id: int | None = None,
        retry_of: int | None = None,
    ) -> int:
        """Raises UniquenessError when a full PPR synchronization is already RUNNING."""
        ...

    def finish(
        self,
        run_id: int,
        status: str,
        *,
        checked: int = 0,
        created: int = 0,
        updated: int = 0,
        changed: int = 0,
        cancelled: int = 0,
        error: dict[str, Any] | None = None,
    ) -> None: ...

    def recent(self, limit: int) -> list[SyncRunRecord]:
        """Master-data synchronization runs, newest first."""
        ...

    def get(self, run_id: int) -> SyncRunRecord | None: ...

    def recent_ppr_runs(self, limit: int) -> list[SyncRunRecord]:
        """Latest PR-synchronization runs (every scope except MASTERS), newest first."""
        ...

    def fail_stale(self, started_before: datetime) -> int:
        """Mark RUNNING runs started before the given time as FAILED (crashed runs)."""
        ...

    def latest_finished(self, scope: str) -> SyncRunRecord | None:
        """The most recent run of this scope that is no longer RUNNING (Wave 8A, D-33)."""
        ...


# ------------------------------------------------------------------ Wave 12A-1: plan import
@dataclass(frozen=True)
class ImportedPlanItem:
    """One checked row of an imported plan (D-42), written as a Plan Item."""

    plan_budget_id: int
    plan_type: PlanType
    item_id: str | None
    snapshot: ItemSnapshot
    owner_department_id: str
    purchasing_department_id: str | None
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    quarters: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]
    note: str | None
    source_sheet: str
    source_row: int


@dataclass(frozen=True)
class PlanImportRecord:
    id: int
    fiscal_year: int
    file_name: str
    file_sha256: str
    rows_imported: int
    rows_skipped: int
    total_amount: Decimal
    state: str  # IMPORTED | REMOVED
    imported_by_user_id: int
    imported_at: datetime
    removed_by_user_id: int | None = None
    removed_at: datetime | None = None
    removed_reason: str | None = None


class PlanImportRepository(Protocol):
    def create(
        self,
        year_id: int,
        file_name: str,
        file_sha256: str,
        rows_imported: int,
        rows_skipped: int,
        total_amount: Decimal,
        by: int,
    ) -> int: ...

    def get(self, import_id: int) -> PlanImportRecord | None: ...

    def list(self, fiscal_year: int) -> list[PlanImportRecord]: ...

    def live_with_hash(self, fiscal_year: int, file_sha256: str) -> PlanImportRecord | None: ...

    def mark_removed(self, import_id: int, by: int, reason: str) -> bool:
        """Marks a live import REMOVED; False when it was not live any more."""
        ...


class PlanRepository(Protocol):
    def lock_year(self, fiscal_year: int) -> tuple[int, PlanYearState] | None:
        """Row-lock the plan year (serializes edits against activation)."""
        ...

    def year_of_budget(self, budget_id: int) -> int | None: ...

    def year_of_item(self, item_id: int) -> int | None: ...

    def budgets(self, fiscal_year: int) -> list[BudgetRecord]: ...

    def get_budget(self, budget_id: int) -> BudgetRecord | None: ...

    def create_budget(
        self, year_id: int, fund_source_id: str, category_id: str, amount: Decimal, by: int | None
    ) -> int: ...

    def update_budget_amount(self, budget_id: int, amount: Decimal) -> None: ...

    def delete_budget(self, budget_id: int) -> None: ...

    def budget_item_count(self, budget_id: int) -> int: ...

    def items(self, fiscal_year: int, department_id: str | None = None) -> list[PlanItemRecord]:
        """All items of the year; with department_id, only items that department owns
        (not for CENTRAL items, D-38 C-4), purchases, or has demand on."""
        ...

    def get_item(self, item_id: int) -> PlanItemRecord | None: ...

    def create_item(
        self, year_id: int, data: PlanItemInput, snapshot: ItemSnapshot, by: int | None
    ) -> int: ...

    def update_item(self, item_id: int, data: PlanItemInput, snapshot: ItemSnapshot) -> None: ...

    def delete_item(self, item_id: int) -> None: ...

    def create_imported_items(
        self, year_id: int, import_id: int, rows: Sequence[ImportedPlanItem], by: int | None
    ) -> int:
        """Wave 12A-1: insert the rows of one import; returns how many were written."""
        ...

    def delete_imported_items(self, import_id: int) -> int:
        """Wave 12A-1: delete the Plan Items of one import (DRAFT years only)."""
        ...

    def amend_item(self, item_id: int, change: AmendedItem) -> None:
        """Wave 7A: overwrite an item's amendable values (never its demand, quarters, note,
        fund source or plan type); the history lives in ``plan_amendment_change``."""
        ...


# ------------------------------------------------------------------ Wave 7A: Plan Amendment
@dataclass(frozen=True)
class AmendedItem:
    plan_budget_id: int
    item_id: str | None
    snapshot: ItemSnapshot
    owner_department_id: str
    purchasing_department_id: str | None
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal


@dataclass(frozen=True)
class AmendmentChangeRecord:
    target: str  # BUDGET | ITEM
    target_id: int
    change_kind: str  # CREATE | UPDATE
    before: dict[str, Any] | None
    after: dict[str, Any]


@dataclass(frozen=True)
class AmendmentRecord:
    id: int
    fiscal_year: int
    amendment_no: int
    approval_document_no: str
    approval_date: date
    reason: str
    created_by_user_id: int
    created_at: datetime
    changes: tuple[AmendmentChangeRecord, ...]


class AmendmentRepository(Protocol):
    def next_number(self, year_id: int) -> int:
        """1 + the highest amendment number of the plan year (call with the year locked)."""
        ...

    def create(
        self,
        year_id: int,
        amendment_no: int,
        approval_document_no: str,
        approval_date: date,
        reason: str,
        by: int,
    ) -> int: ...

    def add_change(self, amendment_id: int, change: AmendmentChangeRecord) -> None: ...

    def get(self, amendment_id: int) -> AmendmentRecord | None: ...

    def list(self, fiscal_year: int | None = None) -> list[AmendmentRecord]:
        """Newest first, with their changes."""
        ...


# ------------------------------------------------------------------ Wave 7B-2: Assigned Purchase
@dataclass(frozen=True)
class ConfirmedCoverage:
    """A CONFIRMED coverage row with what the demand-state rules need about its PPR."""

    ppr_id: int
    plan_item_id: int
    department_id: str
    qty: Decimal
    ppr_state: PprState
    current_version_verified: bool


class AssignedRepository(Protocol):
    def coverage(self, ppr_id: int, *, confirmed: bool) -> dict[int, dict[str, Decimal]]:
        """plan item -> department -> quantity (WORKING or CONFIRMED rows of one PPR)."""
        ...

    def replace_coverage(
        self, ppr_id: int, coverage: dict[int, dict[str, Decimal]], *, confirmed: bool
    ) -> None: ...

    def confirmed_for_items(self, plan_item_ids: Sequence[int]) -> list[ConfirmedCoverage]:
        """CONFIRMED rows of every PPR on these plan items (any PPR state)."""
        ...

    def set_demand(self, plan_item_id: int, department_id: str, qty: Decimal) -> int:
        """Insert or update a demand row (a new or revived one starts PLANNED, 0 is
        CANCELLED); returns its id."""
        ...

    def set_demand_state(self, demand_id: int, state: DemandState) -> None: ...

    def lock_demands(self, plan_item_ids: Sequence[int]) -> None:
        """SELECT ... FOR UPDATE on the items' demand rows (in id order)."""
        ...


class LedgerRepository(Protocol):
    def append(
        self,
        entry: LedgerEntry,
        actor_user_id: int | None,
        plan_amendment_id: int | None = None,
    ) -> None:
        """Insert one entry; ``plan_amendment_id`` links entries posted by an amendment."""
        ...

    def entries(self, plan_item_id: int) -> tuple[LedgerEntry, ...]: ...

    def plan_items_of_ppr(self, ppr_ref: str) -> list[int]:
        """Every plan item with a ledger entry for this PPR number, in id order."""
        ...

    def balances(self, plan_item_ids: Sequence[int]) -> dict[int, LedgerBalance]:
        """Balances of many plan items in one read (Wave 8A). Items without any ledger
        entry (plan not yet ACTIVE) are absent from the result."""
        ...


# ------------------------------------------------------------------ Wave 4: PPR
class UniquenessError(Exception):
    """A repository write hit a uniqueness rule (e.g. a second working draft for a PR,
    or a PR already bound to a confirmed PPR). Raised instead of driver errors so the
    application layer stays free of SQLAlchemy."""


@dataclass(frozen=True)
class PprItemRecord:
    pr_item_id: str
    item_id: str
    item_name: str | None
    unit: str | None
    qty: Decimal
    unit_price: Decimal
    amount: Decimal
    plan_item_id: int
    # D-43: the chosen plan row as it reads now (read-only; filled when loaded)
    plan_item_name: str | None = None
    plan_item_unit: str | None = None
    plan_item_code: str | None = None


@dataclass(frozen=True)
class PprAllocationRecord:
    budget_category_id: str
    amount: Decimal


@dataclass(frozen=True)
class PprScope:
    """Which PPRs a caller reads (D-41). ``None`` stands for every PPR (organisation-wide
    roles); a user whose only role is REQUESTER reads the PPRs they created."""

    created_by_user_id: int


@dataclass(frozen=True)
class PprRecord:
    id: int
    pr_no: str
    fiscal_year: int
    department_id: str
    fund_source_id: str
    pr_budget_category_id: str | None
    state: PprState
    ppr_number: str | None
    current_version: int
    required_amount: Decimal
    adjustment_reference: str | None
    hosxp_pr_status: str
    hosxp_native_status: str | None
    draft_pr: dict[str, Any]
    created_by_user_id: int
    created_at: datetime
    updated_at: datetime
    discarded_at: datetime | None
    items: tuple[PprItemRecord, ...]
    allocations: tuple[PprAllocationRecord, ...]


@dataclass(frozen=True)
class PprHeaderInput:
    pr_no: str
    fiscal_year: int
    department_id: str
    fund_source_id: str
    pr_budget_category_id: str | None
    required_amount: Decimal
    hosxp_pr_status: str
    hosxp_native_status: str | None


@dataclass(frozen=True)
class PprVersionRecord:
    ppr_id: int
    version: int
    snapshot: dict[str, Any]
    document_code: str
    confirmed_by_user_id: int
    confirmed_at: datetime


# ------------------------------------------------------------------ Wave 5B
@dataclass(frozen=True)
class AppointmentRecord:
    id: int
    user_id: int
    fiscal_year: int
    effective_from: date
    effective_to: date | None
    status: str  # ACTIVE / REVOKED
    created_by_user_id: int | None
    created_at: datetime
    revoked_at: datetime | None
    revoked_by_user_id: int | None
    revoke_reason: str | None

    def effective_on(self, day: date) -> bool:
        return (
            self.status == "ACTIVE"
            and self.effective_from <= day
            and (self.effective_to is None or day <= self.effective_to)
        )


class AppointmentRepository(Protocol):
    """Head of Procurement appointments (§23, D-27). Never deleted."""

    def lock(self) -> None:
        """Serialize appointment changes (overlap checks see a stable table)."""
        ...

    def list(self, fiscal_year: int | None = None) -> list[AppointmentRecord]: ...

    def get(self, appointment_id: int, *, for_update: bool = False) -> AppointmentRecord | None: ...

    def create(
        self,
        user_id: int,
        fiscal_year: int,
        effective_from: date,
        effective_to: date,
        created_by: int,
    ) -> int:
        """Raises UniquenessError if it would overlap an ACTIVE appointment (D-27)."""
        ...

    def set_effective_to(self, appointment_id: int, effective_to: date) -> None: ...

    def revoke(self, appointment_id: int, revoked_by: int, reason: str) -> None: ...

    def effective_for(
        self, user_id: int, day: date, *, lock: bool = False
    ) -> AppointmentRecord | None:
        """``lock``: FOR SHARE, so a concurrent end/revoke is waited for and re-read."""
        ...

    def last_verification_day(self, appointment_id: int) -> date | None: ...

    def verifications_between(self, start: date, end: date) -> int:
        """Number of verifications dated (verified_on) within [start, end], any appointment."""
        ...


@dataclass(frozen=True)
class VerificationRecord:
    ppr_id: int
    version: int
    appointment_id: int
    verified_by_user_id: int
    verified_at: datetime


@dataclass(frozen=True)
class PprSearch:
    """Search filters (§32). Text filters match case-insensitively on part of the value."""

    pr_no: str | None = None
    ppr_number: str | None = None
    requester: str | None = None  # PR requester name or the preparing user's name
    department_id: str | None = None
    item: str | None = None  # HOSxP item id or name
    budget_category_id: str | None = None
    fund_source_id: str | None = None
    fiscal_year: int | None = None
    states: tuple[PprState, ...] = ()
    created_from: datetime | None = None  # inclusive, server-local start of day
    created_to: datetime | None = None  # exclusive, server-local start of the next day
    inactive_before: datetime | None = None  # last change strictly before this time
    limit: int = 100
    oldest_first: bool = False  # Wave 8A: alert lists show the longest-idle cases first
    # Wave 8B (D-35): first confirmation (version 1) date range; confirmed PPRs only.
    confirmed_from: datetime | None = None  # inclusive
    confirmed_to: datetime | None = None  # exclusive
    confirmed_only: bool = False


class PprRepository(Protocol):
    def binding_state(self, pr_no: str) -> PrBinding: ...

    def create_draft(
        self, header: PprHeaderInput, draft_pr: dict[str, Any], created_by: int
    ) -> int: ...

    def get(self, ppr_id: int, *, for_update: bool = False) -> PprRecord | None: ...

    def search(self, scope: PprScope | None, limit: int) -> list[PprRecord]: ...

    def update_header(self, ppr_id: int, header: PprHeaderInput) -> None: ...

    def replace_items(self, ppr_id: int, items: Sequence[PprItemRecord]) -> None: ...

    def choices(self, ppr_id: int) -> dict[str, int]:
        """D-43: the plan row chosen for each PR line (pr_item_id -> plan_item_id)."""
        ...

    def chosen_labels(self, ppr_id: int) -> dict[str, tuple[int, str, str]]:
        """D-43: pr_item_id -> (plan_item_id, the row's name, unit) as they were chosen."""
        ...

    def set_choices(
        self, ppr_id: int, choices: Mapping[str, tuple[int, str, str]], by: int
    ) -> None:
        """D-43: replace every choice of the PPR: pr_item_id -> (plan_item_id, the row's name
        and unit as the requester chose it)."""
        ...

    def replace_allocations(
        self,
        ppr_id: int,
        allocations: Sequence[PprAllocationRecord],
        adjustment_reference: str | None,
    ) -> None: ...

    def discard(self, ppr_id: int) -> None: ...

    def set_state(self, ppr_id: int, state: PprState) -> None: ...

    def set_hosxp_status(self, ppr_id: int, status: str, native_status: str | None) -> None:
        """Latest observed HOSxP status (display only, D-02); not a business change."""
        ...

    def mark_confirmed(self, ppr_id: int, ppr_number: str, version: int) -> None: ...

    def next_sequence(self, fiscal_year: int) -> int:
        """Allocate the next PPR sequence for a fiscal year (unique; gaps allowed, D-06)."""
        ...

    def bind(self, pr_no: str, ppr_id: int) -> None:
        """Create the lifetime PR binding (D-03). Raises if the PR is already bound."""
        ...

    def add_version(
        self,
        ppr_id: int,
        version: int,
        snapshot: dict[str, Any],
        document_code: str,
        confirmed_by: int,
    ) -> None: ...

    def versions(self, ppr_id: int) -> list[PprVersionRecord]: ...

    def find(self, search: PprSearch, scope: PprScope | None) -> list[PprRecord]:
        """Search visible PPRs (discarded drafts never; scope as for reading one PPR)."""
        ...

    def count(self, search: PprSearch, scope: PprScope | None) -> int: ...

    def count_by_state(self, fiscal_year: int, scope: PprScope | None) -> dict[str, int]:
        """Visible PPRs of one fiscal year per state (Wave 8A dashboards)."""
        ...

    def first_confirmations(self, ppr_ids: Sequence[int]) -> dict[int, datetime]:
        """When version 1 of each PPR was confirmed (Wave 8B register)."""
        ...

    def last_verifications(self, ppr_ids: Sequence[int]) -> dict[int, datetime]:
        """The latest Procurement verification of each PPR, if any."""
        ...

    def add_verification(
        self, ppr_id: int, version: int, appointment_id: int, verified_by: int, verified_on: date
    ) -> None:
        """Raises UniquenessError if this version is already verified."""
        ...

    def verifications(self, ppr_id: int) -> list[VerificationRecord]: ...

    def lock_plan_year_shared(self, fiscal_year: int) -> None:
        """SELECT ... FOR SHARE on the plan-year row: no state transition (e.g. closing
        the year) can commit while a confirmation is in progress."""
        ...

    def lock_plan_items(self, plan_item_ids: Sequence[int]) -> None:
        """SELECT ... FOR UPDATE on the Plan Item rows (in id order), serializing concurrent
        confirmations that draw on the same Plan Items (spec §19)."""
        ...


# ------------------------------------------------------------------ Wave 6: PR sync
@dataclass(frozen=True)
class SyncTarget:
    """What a synchronization selects before reading HOSxP (no lock held)."""

    ppr_id: int
    pr_no: str
    state: PprState
    version: int


@dataclass(frozen=True)
class ObservationInput:
    sync_run_id: int
    ppr_id: int
    pr_no: str
    outcome: str
    state_before: PprState
    state_after: PprState
    compared_version: int | None = None
    observed_at: datetime | None = None
    evidence_class: str | None = None
    hosxp_status: str | None = None
    native_status: str | None = None
    changes: list[dict[str, Any]] | None = None
    evidence_issues: list[dict[str, Any]] | None = None
    live_pr: dict[str, Any] | None = None
    details: dict[str, Any] | None = None
    released: bool = False
    anomaly_code: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ObservationRecord:
    id: int
    sync_run_id: int
    ppr_id: int
    pr_no: str
    compared_version: int | None
    observed_at: datetime | None
    recorded_at: datetime
    outcome: str
    evidence_class: str | None
    hosxp_status: str | None
    native_status: str | None
    changes: list[dict[str, Any]] | None
    evidence_issues: list[dict[str, Any]] | None
    live_pr: dict[str, Any] | None
    details: dict[str, Any] | None
    state_before: str
    state_after: str
    released: bool
    anomaly_code: str | None
    error_code: str | None
    error_message: str | None
    ppr_number: str | None = None  # joined for lists
    department_id: str | None = None  # joined for scope checks


class SyncObservationRepository(Protocol):
    """Append-only PR observations (D-29)."""

    def add(self, obs: ObservationInput) -> int:
        """Raises UniquenessError on a second released observation for a PPR (D-30)."""
        ...

    def for_ppr(self, ppr_id: int, limit: int = 200) -> list[ObservationRecord]:
        """Newest first."""
        ...

    def for_run(self, run_id: int, scope: PprScope | None) -> list[ObservationRecord]: ...

    def governance(self, outcomes: Sequence[str], limit: int) -> list[ObservationRecord]:
        """The latest observation of each PPR when it has one of the outcomes or an
        anomaly code, newest first."""
        ...

    def released_for(self, ppr_id: int) -> bool: ...

    def sync_targets(self, states: Sequence[PprState]) -> list[SyncTarget]:
        """Confirmed PPRs in the given states, oldest first (plain read, no lock)."""
        ...

    def retry_targets(
        self, run_id: int, outcomes: Sequence[str], invalid_classes: Sequence[str]
    ) -> list[int]:
        """PPR ids of the run whose observation had one of the outcomes, or was invalid
        evidence of one of the classes."""
        ...

    def latest_after(
        self, run_id: int, ppr_ids: Sequence[int], scopes: Sequence[str]
    ) -> dict[int, tuple[str, str | None]]:
        """The latest (outcome, evidence class) of each PPR observed by a FINISHED run of
        one of the scopes that started after ``run_id`` (D-36)."""
        ...


# ------------------------------------------------------------------ Wave 8A: alert settings
@dataclass(frozen=True)
class AlertSettingRecord:
    code: AlertSetting
    value: int
    updated_at: datetime | None
    updated_by_user_id: int | None


class AlertSettingRepository(Protocol):
    def all(self) -> dict[AlertSetting, AlertSettingRecord]:
        """Every stored setting (a missing row falls back to the D-33 default)."""
        ...

    def set(self, code: AlertSetting, value: int, by: int) -> None: ...


class UnitOfWork(Protocol):
    """One database transaction. Everything written through it commits or rolls back together."""

    @property
    def audit(self) -> AuditWriter: ...

    @property
    def users(self) -> UserRepository: ...

    @property
    def plan_years(self) -> PlanYearRepository: ...

    @property
    def masters(self) -> MasterRepository: ...

    @property
    def sync_runs(self) -> SyncRunRepository: ...

    @property
    def plans(self) -> PlanRepository: ...

    @property
    def ledger(self) -> LedgerRepository: ...

    @property
    def pprs(self) -> PprRepository: ...

    @property
    def appointments(self) -> AppointmentRepository: ...

    @property
    def sync_observations(self) -> SyncObservationRepository: ...

    @property
    def alert_settings(self) -> AlertSettingRepository: ...

    @property
    def amendments(self) -> AmendmentRepository: ...

    @property
    def assigned(self) -> AssignedRepository: ...

    @property
    def plan_imports(self) -> PlanImportRepository: ...


UnitOfWorkFactory = Callable[[], AbstractContextManager[UnitOfWork]]
