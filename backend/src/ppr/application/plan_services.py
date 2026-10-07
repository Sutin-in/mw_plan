"""Wave 2 use cases: master sync, plan budgets, Plan Items, validation, activation, balances.

Rules enforced here (spec §9, §10, §18, D-11 ... D-14):
* Budgets and Plan Items change freely while the plan year is DRAFT; while APPROVED only
  as data-entry corrections with a reason and a source-document reference (D-15); never
  after ACTIVE (Plan Amendment, Wave 7). The plan year row is locked for the duration of
  each change, so edits cannot race activation.
* Plan Items reference existing, active HOSxP masters by source id (D-14, §8).
* Activation (APPROVED -> ACTIVE) runs ``check_activation`` and, only if it passes,
  posts one PLAN_ACTIVATED ledger entry per Plan Item in the same transaction (D-12).
* Every change is audited in the same transaction (§36).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ppr.application.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.ledger import LedgerBalance, LedgerEntry, LedgerEventType, PlanItemLedger
from ppr.domain.plan import (
    ActivationReport,
    BudgetLine,
    Issue,
    PlanItemFacts,
    PlanType,
    check_activation,
    item_warnings,
)
from ppr.integration.hosxp.errors import HosxpUnavailableError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.ports import (
    Actor,
    AuditEntry,
    BudgetRecord,
    Correction,
    ItemSnapshot,
    MasterKind,
    MasterRecord,
    PlanItemInput,
    PlanItemRecord,
    SyncRunRecord,
    UnitOfWork,
    UnitOfWorkFactory,
)


def _j(value: Any) -> Any:
    """JSON-safe copy for audit payloads (Decimals as strings)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _j(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_j(v) for v in value]
    return value


def _item_audit(rec: PlanItemRecord) -> dict[str, Any]:
    payload: dict[str, Any] = _j(
        {
            "plan_budget_id": rec.plan_budget_id,
            "plan_type": rec.plan_type.value,
            "item_id": rec.item_id,
            "owner_department_id": rec.owner_department_id,
            "purchasing_department_id": rec.purchasing_department_id,
            "planned_qty": rec.planned_qty,
            "estimated_unit_price": rec.estimated_unit_price,
            "planned_amount": rec.planned_amount,
            "quarters": list(rec.quarters),
            "demands": [[d.department_id, d.demand_qty] for d in rec.demands],
        }
    )
    return payload


# ------------------------------------------------------------------ master sync (D-14)
@dataclass(frozen=True)
class MasterSyncResult:
    run: SyncRunRecord
    per_kind: dict[str, tuple[int, int]]


def sync_masters(
    uow_factory: UnitOfWorkFactory, gateway: HosxpGateway, actor: Actor
) -> MasterSyncResult:
    """Manual master sync (spec §25.2, §25.4). The run is logged even if it fails."""
    if actor.user_id is None:
        raise InvalidInputError("REQUESTER_REQUIRED", "a manual sync must name who requested it")
    with uow_factory() as uow:
        run_id = uow.sync_runs.start("MANUAL", "MASTERS", actor.user_id)
        uow.audit.write(AuditEntry(actor, "MANUAL_SYNC_STARTED", "sync_run", str(run_id)))

    try:
        fetched: dict[MasterKind, list[MasterRecord]] = {
            MasterKind.FUND_SOURCE: list(gateway.get_fund_sources()),
            MasterKind.BUDGET_CATEGORY: list(gateway.get_budget_categories()),
            MasterKind.DEPARTMENT: list(gateway.get_departments()),
            MasterKind.ITEM: list(gateway.get_items()),
        }
    except HosxpUnavailableError as exc:
        with uow_factory() as uow:
            uow.sync_runs.finish(run_id, "FAILED", error={"reason": "HOSXP_UNAVAILABLE"})
        raise UpstreamUnavailableError("HOSXP_UNAVAILABLE", "HOSxP is unavailable") from exc

    # Wave 11B: HOSxP must name each record once; say which, instead of a bare failure.
    for kind, records in fetched.items():
        counts = Counter(r.source_id for r in records)
        if repeated := sorted(i for i, n in counts.items() if n > 1):
            with uow_factory() as uow:
                uow.sync_runs.finish(
                    run_id,
                    "FAILED",
                    error={
                        "reason": "DUPLICATE_SOURCE_ID",
                        "kind": kind.value,
                        "count": len(repeated),
                        "examples": repeated[:5],
                    },
                )
            raise ConflictError(
                "DUPLICATE_SOURCE_ID",
                f"HOSxP returned {len(repeated)} {kind.value} id(s) more than once "
                f"(e.g. {', '.join(repeated[:5])}): fix the site query",
            )

    per_kind: dict[str, tuple[int, int]] = {}
    try:
        with uow_factory() as uow:
            for kind, records in fetched.items():
                per_kind[kind.value] = uow.masters.upsert(kind, records)
            checked = sum(len(r) for r in fetched.values())
            created = sum(c for c, _ in per_kind.values())
            updated = sum(u for _, u in per_kind.values())
            uow.sync_runs.finish(
                run_id, "SUCCEEDED", checked=checked, created=created, updated=updated
            )
    except Exception as exc:
        with uow_factory() as uow:
            uow.sync_runs.finish(run_id, "FAILED", error={"reason": type(exc).__name__})
        raise

    with uow_factory() as uow:
        run = uow.sync_runs.get(run_id)
    assert run is not None
    return MasterSyncResult(run, per_kind)


# ------------------------------------------------------------------ helpers
@dataclass(frozen=True)
class EditContext:
    year_id: int
    correction: Correction | None  # set when editing an APPROVED plan (D-15)

    def action(self, base: str) -> str:
        """PLAN_ITEM_CREATED -> PLAN_ITEM_CORRECTED when this is a correction."""
        if self.correction is None:
            return base
        return base.rsplit("_", 1)[0] + "_CORRECTED"

    def annotate(self, payload: dict[str, Any] | None, op: str) -> dict[str, Any] | None:
        if self.correction is None:
            return payload
        out = dict(payload or {})
        out["correction"] = {"operation": op, "source_reference": self.correction.source_reference}
        return out

    @property
    def reason(self) -> str | None:
        return self.correction.reason if self.correction else None


def _editable_year(uow: UnitOfWork, fiscal_year: int, correction: Correction | None) -> EditContext:
    locked = uow.plans.lock_year(fiscal_year)
    if locked is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
    year_id, state = locked
    if state is PlanYearState.DRAFT:
        return EditContext(year_id, None)
    if state is PlanYearState.APPROVED:
        if (
            correction is None
            or not correction.reason.strip()
            or not correction.source_reference.strip()
        ):
            raise ConflictError(
                "CORRECTION_DETAILS_REQUIRED",
                f"plan year {fiscal_year} is APPROVED: only data-entry corrections that match "
                "the approved source document are allowed, with a reason and a source "
                "reference (D-15). Changes to approved business values need Plan Amendment.",
            )
        return EditContext(year_id, correction)
    raise ConflictError(
        "PLAN_YEAR_LOCKED",
        f"plan year {fiscal_year} is {state}; plan data changes only through Plan Amendment",
    )


def _require_master(uow: UnitOfWork, kind: MasterKind, source_id: str, label: str) -> None:
    row = uow.masters.get(kind, source_id)
    if row is None:
        raise InvalidInputError(
            "UNKNOWN_MASTER", f"{label} {source_id!r} is not a synchronized HOSxP record"
        )
    if not row.active:
        raise InvalidInputError("INACTIVE_MASTER", f"{label} {source_id!r} is inactive in HOSxP")


# ------------------------------------------------------------------ budgets
def _budget_payload(b: BudgetRecord) -> dict[str, Any]:
    payload: dict[str, Any] = _j(
        {
            "fiscal_year": b.fiscal_year,
            "fund_source_id": b.fund_source_id,
            "budget_category_id": b.budget_category_id,
            "approved_amount": b.approved_amount,
        }
    )
    return payload


def create_budget(
    uow: UnitOfWork,
    actor: Actor,
    fiscal_year: int,
    fund_source_id: str,
    category_id: str,
    amount: Decimal,
    correction: Correction | None = None,
) -> BudgetRecord:
    ctx = _editable_year(uow, fiscal_year, correction)
    _require_master(uow, MasterKind.FUND_SOURCE, fund_source_id, "fund source")
    _require_master(uow, MasterKind.BUDGET_CATEGORY, category_id, "budget category")
    if any(
        b.fund_source_id == fund_source_id and b.budget_category_id == category_id
        for b in uow.plans.budgets(fiscal_year)
    ):
        raise ConflictError("BUDGET_EXISTS", "this fund source / category already has a budget")
    budget_id = uow.plans.create_budget(
        ctx.year_id, fund_source_id, category_id, amount, actor.user_id
    )
    rec = uow.plans.get_budget(budget_id)
    assert rec is not None
    uow.audit.write(
        AuditEntry(
            actor,
            ctx.action("PLAN_BUDGET_CREATED"),
            "plan_budget",
            str(budget_id),
            after=ctx.annotate(_budget_payload(rec), "create"),
            reason=ctx.reason,
        )
    )
    return rec


def _budget_for_change(
    uow: UnitOfWork, budget_id: int, correction: Correction | None
) -> tuple[BudgetRecord, EditContext]:
    fy = uow.plans.year_of_budget(budget_id)
    if fy is None:
        raise NotFoundError("BUDGET_NOT_FOUND", f"budget {budget_id} not found")
    ctx = _editable_year(uow, fy, correction)
    rec = uow.plans.get_budget(budget_id)
    assert rec is not None
    return rec, ctx


def update_budget(
    uow: UnitOfWork,
    actor: Actor,
    budget_id: int,
    amount: Decimal,
    correction: Correction | None = None,
) -> BudgetRecord:
    before, ctx = _budget_for_change(uow, budget_id, correction)
    uow.plans.update_budget_amount(budget_id, amount)
    after = uow.plans.get_budget(budget_id)
    assert after is not None
    uow.audit.write(
        AuditEntry(
            actor,
            ctx.action("PLAN_BUDGET_UPDATED"),
            "plan_budget",
            str(budget_id),
            before=_budget_payload(before),
            after=ctx.annotate(_budget_payload(after), "update"),
            reason=ctx.reason,
        )
    )
    return after


def delete_budget(
    uow: UnitOfWork, actor: Actor, budget_id: int, correction: Correction | None = None
) -> None:
    before, ctx = _budget_for_change(uow, budget_id, correction)
    if uow.plans.budget_item_count(budget_id):
        raise ConflictError("BUDGET_IN_USE", "remove or move the Plan Items on this budget first")
    uow.plans.delete_budget(budget_id)
    uow.audit.write(
        AuditEntry(
            actor,
            ctx.action("PLAN_BUDGET_DELETED"),
            "plan_budget",
            str(budget_id),
            before=_budget_payload(before),
            after=ctx.annotate(None, "delete"),
            reason=ctx.reason,
        )
    )


# ------------------------------------------------------------------ plan items
def _used(uow: UnitOfWork, plan_item_id: int) -> bool:
    """Whether any PPR has drawn on the Plan Item (D-43: its name and unit are then fixed)."""
    return any(e.ppr_ref is not None for e in uow.ledger.entries(plan_item_id))


def _validate_item_input(
    uow: UnitOfWork,
    fiscal_year: int,
    data: PlanItemInput,
    current: PlanItemRecord | None = None,
) -> ItemSnapshot:
    budget = uow.plans.get_budget(data.plan_budget_id)
    if budget is None or budget.fiscal_year != fiscal_year:
        raise InvalidInputError(
            "BUDGET_NOT_IN_YEAR", "the budget line does not belong to this plan year"
        )
    if data.item_id is None:
        # D-43: a row may have no HOSxP item; it then carries its own name and unit. A row
        # with one never loses it here, and an Assigned Purchase item needs one (D-38 A-1).
        if current is not None and current.item_id is not None:
            raise InvalidInputError(
                "ITEM_REQUIRED", "this Plan Item carries a HOSxP item; it cannot be removed"
            )
        if data.plan_type is PlanType.ASSIGNED:
            raise InvalidInputError(
                "ITEM_REQUIRED",
                "an Assigned Purchase item needs a HOSxP item: one department's demand is "
                "recognized on the PR by its item (D-38 A-1)",
            )
    else:
        _require_master(uow, MasterKind.ITEM, data.item_id, "item")
    _require_master(uow, MasterKind.DEPARTMENT, data.owner_department_id, "owner department")
    if data.purchasing_department_id:
        _require_master(
            uow, MasterKind.DEPARTMENT, data.purchasing_department_id, "purchasing department"
        )
    if data.demands and data.plan_type is not PlanType.ASSIGNED:
        raise InvalidInputError(
            "DEMAND_NOT_ALLOWED", "per-department demand applies to Assigned Purchase items only"
        )
    seen: set[str] = set()
    for d in data.demands:
        if d.department_id in seen:
            raise InvalidInputError(
                "DUPLICATE_DEMAND", f"department {d.department_id} listed twice in demand"
            )
        seen.add(d.department_id)
        _require_master(uow, MasterKind.DEPARTMENT, d.department_id, "demand department")
    if data.item_id is None:
        name = (
            data.item_name if data.item_name is not None else (current.item_name if current else "")
        )
        unit = data.unit if data.unit is not None else (current.unit if current else "")
        name, unit = name.strip(), unit.strip()
        if not name or not unit:
            raise InvalidInputError(
                "NAME_AND_UNIT_REQUIRED",
                "a Plan Item without a HOSxP item needs its own name and unit (D-43)",
            )
        if (
            current is not None
            and (name, unit) != (current.item_name.strip(), current.unit.strip())
            and _used(uow, current.id)
        ):
            raise ConflictError(
                "NAME_CHANGE_AFTER_USE",
                "a requester chose this row by its name and unit: they change only while no "
                "PPR has used it (D-43)",
            )
        return ItemSnapshot(None, name, unit)
    item = uow.masters.get(MasterKind.ITEM, data.item_id)
    assert item is not None
    # Giving a row without a HOSxP item one: the row's unit is the plan's unit, and only an
    # item counted in that unit may stand for it (D-42).
    if (
        current is not None
        and current.item_id is None
        and (item.unit or "").strip() != current.unit.strip()
    ):
        raise InvalidInputError(
            "UNIT_MISMATCH",
            f"HOSxP item {data.item_id} is counted in {item.unit or '-'!r}, the plan row in "
            f"{current.unit!r}: convert the row to the HOSxP unit first (D-42)",
        )
    return ItemSnapshot(item.code, item.name, item.unit or "")


def create_plan_item(
    uow: UnitOfWork,
    actor: Actor,
    fiscal_year: int,
    data: PlanItemInput,
    correction: Correction | None = None,
) -> PlanItemRecord:
    ctx = _editable_year(uow, fiscal_year, correction)
    snapshot = _validate_item_input(uow, fiscal_year, data)
    item_id = uow.plans.create_item(ctx.year_id, data, snapshot, actor.user_id)
    rec = uow.plans.get_item(item_id)
    assert rec is not None
    uow.audit.write(
        AuditEntry(
            actor,
            ctx.action("PLAN_ITEM_CREATED"),
            "plan_item",
            str(item_id),
            after=ctx.annotate(_item_audit(rec), "create"),
            reason=ctx.reason,
        )
    )
    return rec


def _item_for_change(
    uow: UnitOfWork, item_id: int, correction: Correction | None
) -> tuple[int, PlanItemRecord, EditContext]:
    fy = uow.plans.year_of_item(item_id)
    if fy is None:
        raise NotFoundError("PLAN_ITEM_NOT_FOUND", f"plan item {item_id} not found")
    ctx = _editable_year(uow, fy, correction)
    before = uow.plans.get_item(item_id)
    assert before is not None
    return fy, before, ctx


def update_plan_item(
    uow: UnitOfWork,
    actor: Actor,
    item_id: int,
    data: PlanItemInput,
    correction: Correction | None = None,
) -> PlanItemRecord:
    fy, before, ctx = _item_for_change(uow, item_id, correction)
    snapshot = _validate_item_input(uow, fy, data, before)
    uow.plans.update_item(item_id, data, snapshot)
    after = uow.plans.get_item(item_id)
    assert after is not None
    uow.audit.write(
        AuditEntry(
            actor,
            ctx.action("PLAN_ITEM_UPDATED"),
            "plan_item",
            str(item_id),
            before=_item_audit(before),
            after=ctx.annotate(_item_audit(after), "update"),
            reason=ctx.reason,
        )
    )
    return after


def delete_plan_item(
    uow: UnitOfWork, actor: Actor, item_id: int, correction: Correction | None = None
) -> None:
    _, before, ctx = _item_for_change(uow, item_id, correction)
    uow.plans.delete_item(item_id)
    uow.audit.write(
        AuditEntry(
            actor,
            ctx.action("PLAN_ITEM_DELETED"),
            "plan_item",
            str(item_id),
            before=_item_audit(before),
            after=ctx.annotate(None, "delete"),
            reason=ctx.reason,
        )
    )


def warnings_for(rec: PlanItemRecord) -> tuple[Issue, ...]:
    return item_warnings(_facts(rec))


# ------------------------------------------------------------------ validation / activation
def _facts(rec: PlanItemRecord) -> PlanItemFacts:
    return PlanItemFacts(
        ref=str(rec.id),
        plan_type=rec.plan_type,
        item_id=rec.item_id,
        owner_department_id=rec.owner_department_id,
        purchasing_department_id=rec.purchasing_department_id,
        fund_source_id=rec.fund_source_id,
        budget_category_id=rec.budget_category_id,
        planned_qty=rec.planned_qty,
        estimated_unit_price=rec.estimated_unit_price,
        planned_amount=rec.planned_amount,
        quarters=rec.quarters,
        demand_qtys=tuple(d.demand_qty for d in rec.demands),
        demand_departments=tuple(d.department_id for d in rec.demands if d.demand_qty > 0),
    )


def validate_plan(uow: UnitOfWork, fiscal_year: int) -> ActivationReport:
    if uow.plan_years.get(fiscal_year) is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
    budgets = [
        BudgetLine(b.fund_source_id, b.budget_category_id, b.approved_amount)
        for b in uow.plans.budgets(fiscal_year)
    ]
    return check_activation(budgets, [_facts(r) for r in uow.plans.items(fiscal_year)])


def activate_plan(uow: UnitOfWork, actor: Actor, fiscal_year: int) -> ActivationReport:
    """Validate the whole plan and post PLAN_ACTIVATED for every item (D-12).

    Called inside the APPROVED -> ACTIVE transition's transaction, after the plan year row
    has been locked; raises ConflictError (nothing written) if the plan is not valid.
    """
    report = validate_plan(uow, fiscal_year)
    if not report.can_activate:
        raise ConflictError(
            "PLAN_NOT_VALID",
            "the plan cannot be activated: " + "; ".join(e.message for e in report.errors),
            details=[
                {"code": e.code.value, "message": e.message, "subject": e.subject}
                for e in report.errors
            ],
        )
    items = uow.plans.items(fiscal_year)
    for rec in items:
        entry = LedgerEntry(
            str(rec.id),
            LedgerEventType.PLAN_ACTIVATED,
            rec.planned_qty,
            rec.planned_amount,
            idempotency_key=f"activate:{rec.id}",
        )
        PlanItemLedger(str(rec.id), uow.ledger.entries(rec.id)).append(entry)  # domain rules
        uow.ledger.append(entry, actor.user_id)
    uow.audit.write(
        AuditEntry(
            actor,
            "PLAN_ACTIVATED",
            "plan_year",
            str(fiscal_year),
            after=_j(
                {
                    "plan_items": len(items),
                    "envelope": [
                        {
                            "fund_source_id": e.fund_source_id,
                            "budget_category_id": e.budget_category_id,
                            "approved_amount": e.approved_amount,
                        }
                        for e in report.envelope
                    ],
                }
            ),
        )
    )
    return report


def item_balance(uow: UnitOfWork, item_id: int) -> LedgerBalance:
    if uow.plans.get_item(item_id) is None:
        raise NotFoundError("PLAN_ITEM_NOT_FOUND", f"plan item {item_id} not found")
    return PlanItemLedger(str(item_id), uow.ledger.entries(item_id)).balance
