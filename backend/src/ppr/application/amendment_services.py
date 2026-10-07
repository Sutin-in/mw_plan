"""Wave 7A use cases: Plan Amendment (spec §20; D-11, D-37 7A).

* Only a Plan Officer records an amendment, only while the plan year is ACTIVE, and only with
  the number and date of the Director's approval document (the approval stays on paper).
* The whole amendment is judged by ``ppr.domain.amendment.plan_amendment`` against the plan
  year as it is now and what confirmed PPRs still hold on each item; nothing is written unless
  every rule passes.
* The plan year row is locked FOR UPDATE for the whole transaction. PPR confirmation takes the
  same row FOR SHARE, so no confirmation can draw on the plan while it is being amended
  (spec §19); the amended items are locked as well.
* One transaction writes: the amendment header, before/after history per changed budget line,
  Plan Item and department demand of an ASSIGNED item (append-only), the new current values,
  new budget lines and items, the ``PLAN_AMENDMENT`` ledger entries (``PLAN_ACTIVATED`` of
  zero first for a new item) and one ``PLAN_AMENDED`` audit row. Demand states are then
  recomputed (D-38 A-6; a demand of 0 is CANCELLED).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from ppr.application import assigned_services
from ppr.application.errors import ConflictError, InvalidInputError, NotFoundError
from ppr.domain.amendment import (
    AmendmentPlan,
    CurrentItem,
    DemandChange,
    DemandNow,
    ItemChange,
    ItemValues,
    NewItem,
    ledger_entries,
    plan_amendment,
)
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.ledger import PlanItemLedger
from ppr.domain.plan import BudgetLine, PlanType, visible_to_department
from ppr.ports import (
    Actor,
    AmendedItem,
    AmendmentChangeRecord,
    AmendmentRecord,
    AuditEntry,
    BudgetRecord,
    DemandInput,
    ItemSnapshot,
    MasterKind,
    PlanItemInput,
    PlanItemRecord,
    UnitOfWork,
)

ZERO = Decimal(0)
MAX_DOCUMENT_NO = 100
# History is written at the stored precision (D-23), so before and after read alike.
QTY_SCALE = Decimal("0.0001")
MONEY_SCALE = Decimal("0.01")


class _Keep(Enum):
    KEEP = "KEEP"


KEEP = _Keep.KEEP


@dataclass(frozen=True)
class BudgetChangeRequest:
    """The wanted approved amount of an existing budget line, or a new line."""

    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal


@dataclass(frozen=True)
class ItemChangeRequest:
    """Changes to an existing Plan Item; a field left as KEEP keeps its current value."""

    plan_item_id: int
    item_id: str | _Keep = KEEP
    owner_department_id: str | _Keep = KEEP
    purchasing_department_id: str | _Keep | None = KEEP
    budget_category_id: str | _Keep = KEEP
    planned_qty: Decimal | _Keep = KEEP
    estimated_unit_price: Decimal | _Keep = KEEP
    planned_amount: Decimal | _Keep = KEEP
    # D-43: the name and unit of a row without a HOSxP item (only while it is unused)
    item_name: str | _Keep = KEEP
    unit: str | _Keep = KEEP


@dataclass(frozen=True)
class NewItemRequest:
    ref: str
    plan_type: PlanType
    item_id: str | None  # D-43: optional; without one the row carries its own name and unit
    owner_department_id: str
    purchasing_department_id: str | None
    fund_source_id: str
    budget_category_id: str
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    note: str | None = None
    demands: tuple[tuple[str, Decimal], ...] = ()  # ASSIGNED: (department, demand)
    item_name: str | None = None  # D-43: required when there is no HOSxP item
    unit: str | None = None


@dataclass(frozen=True)
class AmendmentRequest:
    approval_document_no: str
    approval_date: date
    reason: str
    budgets: tuple[BudgetChangeRequest, ...] = ()
    items: tuple[ItemChangeRequest, ...] = ()
    new_items: tuple[NewItemRequest, ...] = ()
    # D-38 A-6: department demand of existing ASSIGNED items (0 cancels a demand).
    demands: tuple[DemandChange, ...] = ()
    # Optimistic check (D-37): the number of the latest amendment the form was built on. After
    # ACTIVE the plan changes only through amendments, so a different latest number means the
    # form shows an out-of-date plan and could undo another officer's amendment.
    base_amendment_no: int | None = None


@dataclass(frozen=True)
class AmendmentPreview:
    """The judged amendment plus the usage each changed item holds (for display)."""

    plan: AmendmentPlan
    used: dict[int, tuple[Decimal, Decimal]] = field(default_factory=dict)
    item_labels: dict[int, tuple[str, str, str]] = field(default_factory=dict)


# ------------------------------------------------------------------ helpers
def _j(value: Any) -> Any:
    """JSON-safe copy for history and audit payloads (Decimals as strings)."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _j(v) for k, v in value.items()}
    return value


def _values(rec: PlanItemRecord) -> ItemValues:
    return ItemValues(
        plan_type=rec.plan_type,
        item_id=rec.item_id,
        owner_department_id=rec.owner_department_id,
        purchasing_department_id=rec.purchasing_department_id,
        fund_source_id=rec.fund_source_id,
        budget_category_id=rec.budget_category_id,
        planned_qty=rec.planned_qty,
        estimated_unit_price=rec.estimated_unit_price,
        planned_amount=rec.planned_amount,
        item_name=rec.item_name,
        unit=rec.unit,
    )


def _pick[T](value: T | _Keep, current: T) -> T:
    return current if isinstance(value, _Keep) else value


def _after(rec: PlanItemRecord, ch: ItemChangeRequest) -> ItemValues:
    return ItemValues(
        plan_type=rec.plan_type,
        item_id=_pick(ch.item_id, rec.item_id),
        owner_department_id=_pick(ch.owner_department_id, rec.owner_department_id),
        purchasing_department_id=_pick(ch.purchasing_department_id, rec.purchasing_department_id),
        fund_source_id=rec.fund_source_id,
        budget_category_id=_pick(ch.budget_category_id, rec.budget_category_id),
        planned_qty=_pick(ch.planned_qty, rec.planned_qty),
        estimated_unit_price=_pick(ch.estimated_unit_price, rec.estimated_unit_price),
        planned_amount=_pick(ch.planned_amount, rec.planned_amount),
        item_name=rec.item_name if isinstance(ch.item_name, _Keep) else ch.item_name.strip(),
        unit=rec.unit if isinstance(ch.unit, _Keep) else ch.unit.strip(),
    )


def _values_payload(v: ItemValues, snap: ItemSnapshot) -> dict[str, Any]:
    payload: dict[str, Any] = _j(
        {
            "item_code": snap.code,
            "item_name": snap.name,
            "unit": snap.unit,
            "plan_type": v.plan_type.value,
            "item_id": v.item_id,
            "owner_department_id": v.owner_department_id,
            "purchasing_department_id": v.purchasing_department_id,
            "fund_source_id": v.fund_source_id,
            "budget_category_id": v.budget_category_id,
            "planned_qty": v.planned_qty.quantize(QTY_SCALE),
            "estimated_unit_price": v.estimated_unit_price.quantize(MONEY_SCALE),
            "planned_amount": v.planned_amount.quantize(MONEY_SCALE),
        }
    )
    return payload


def _budget_payload(fund: str, category: str, amount: Decimal) -> dict[str, Any]:
    return {
        "fund_source_id": fund,
        "budget_category_id": category,
        "approved_amount": str(amount.quantize(MONEY_SCALE)),
    }


def _require_master(uow: UnitOfWork, kind: MasterKind, source_id: str, label: str) -> None:
    row = uow.masters.get(kind, source_id)
    if row is None:
        raise InvalidInputError(
            "UNKNOWN_MASTER", f"{label} {source_id!r} is not a synchronized HOSxP record"
        )
    if not row.active:
        raise InvalidInputError("INACTIVE_MASTER", f"{label} {source_id!r} is inactive in HOSxP")


def _check_header(req: AmendmentRequest, today: date) -> None:
    doc = req.approval_document_no.strip()
    if not doc:
        raise InvalidInputError(
            "APPROVAL_DOCUMENT_REQUIRED", "the approval document number is required (D-37)"
        )
    if len(doc) > MAX_DOCUMENT_NO:
        raise InvalidInputError(
            "APPROVAL_DOCUMENT_TOO_LONG",
            f"the approval document number is longer than {MAX_DOCUMENT_NO} characters",
        )
    if req.approval_date > today:
        raise InvalidInputError(
            "APPROVAL_DATE_IN_FUTURE", "the approval date cannot be later than today"
        )
    if not req.reason.strip():
        raise InvalidInputError("REASON_REQUIRED", "the reason for the amendment is required")


def _active_year(uow: UnitOfWork, fiscal_year: int) -> int:
    locked = uow.plans.lock_year(fiscal_year)
    if locked is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
    year_id, state = locked
    if state is not PlanYearState.ACTIVE:
        raise ConflictError(
            "PLAN_YEAR_NOT_ACTIVE",
            f"plan year {fiscal_year} is {state}: Plan Amendment applies to an ACTIVE plan "
            "year only (D-37)",
        )
    return year_id


@dataclass(frozen=True)
class _Judged:
    year_id: int
    budgets: list[BudgetRecord]
    items: dict[int, PlanItemRecord]
    preview: AmendmentPreview


def _judge(uow: UnitOfWork, fiscal_year: int, req: AmendmentRequest, today: date) -> _Judged:
    _check_header(req, today)
    year_id = _active_year(uow, fiscal_year)
    latest = uow.amendments.next_number(year_id) - 1
    if req.base_amendment_no is not None and req.base_amendment_no != latest:
        raise ConflictError(
            "PLAN_CHANGED_SINCE_FORM",
            f"the plan was amended (latest amendment {latest}) after this form was opened "
            f"(amendment {req.base_amendment_no}); open the form again",
        )
    budgets = uow.plans.budgets(fiscal_year)
    items = {r.id: r for r in uow.plans.items(fiscal_year)}
    wanted = {c.plan_item_id for c in req.items} | {d.plan_item_id for d in req.demands}
    changed_ids = sorted(wanted & set(items))
    uow.pprs.lock_plan_items(changed_ids)
    # D-38 A-6: demand states and coverage are read under lock (after the items, the order
    # every other writer uses), so the floor and the audited states are the current ones.
    uow.assigned.lock_demands([r.id for r in items.values() if r.plan_type is PlanType.ASSIGNED])
    items = {r.id: r for r in uow.plans.items(fiscal_year)}
    balances = uow.ledger.balances(list(items))
    assigned = [r.id for r in items.values() if r.plan_type is PlanType.ASSIGNED]
    covered = assigned_services.covered_elsewhere(uow, assigned, None)
    used: dict[int, tuple[Decimal, Decimal]] = {}
    current: list[CurrentItem] = []
    for rec in items.values():
        b = balances.get(rec.id)
        uq = b.ppr_used_qty - b.released_qty if b else ZERO
        ua = b.ppr_used_amount - b.released_amount if b else ZERO
        used[rec.id] = (uq, ua)
        demand = (
            sum((d.demand_qty for d in rec.demands), ZERO)
            if rec.plan_type is PlanType.ASSIGNED
            else None
        )
        current.append(
            CurrentItem(
                rec.id,
                _values(rec),
                uq,
                ua,
                demand,
                tuple(
                    DemandNow(
                        d.department_id, d.demand_qty, covered.get((rec.id, d.department_id), ZERO)
                    )
                    for d in rec.demands
                ),
                ever_used=b is not None and (b.ppr_used_qty != ZERO or b.ppr_used_amount != ZERO),
            )
        )

    # Masters: every value that is new or changes must be a synchronized, active record.
    known_lines = {(b.fund_source_id, b.budget_category_id) for b in budgets}
    for bc in req.budgets:
        if (bc.fund_source_id, bc.budget_category_id) not in known_lines:
            _require_master(uow, MasterKind.FUND_SOURCE, bc.fund_source_id, "fund source")
            _require_master(
                uow, MasterKind.BUDGET_CATEGORY, bc.budget_category_id, "budget category"
            )
    changes: list[ItemChange] = []
    for ch in req.items:
        found = items.get(ch.plan_item_id)
        if found is None:
            changes.append(ItemChange(ch.plan_item_id, _placeholder()))
            continue
        rec = found
        after = _after(rec, ch)
        if rec.item_id is not None and not (
            isinstance(ch.item_name, _Keep) and isinstance(ch.unit, _Keep)
        ):
            raise InvalidInputError(
                "NAME_FROM_HOSXP",
                f"plan item {rec.id} carries a HOSxP item: its name and unit are the item's",
            )
        if after.item_id != rec.item_id:
            if rec.item_id is None or after.item_id is None:
                # D-43: a row keeps having, or not having, a HOSxP item; add a new row instead
                raise InvalidInputError(
                    "UNBOUND_ROW",
                    f"plan item {rec.id}: a row without a HOSxP item cannot be given one by "
                    "amendment, nor the reverse (D-43); add a new row instead",
                )
            _require_master(uow, MasterKind.ITEM, after.item_id, "item")
            master = uow.masters.get(MasterKind.ITEM, after.item_id)
            assert master is not None
            after = replace(after, item_name=master.name, unit=master.unit or "")
        if after.owner_department_id != rec.owner_department_id:
            _require_master(
                uow, MasterKind.DEPARTMENT, after.owner_department_id, "owner department"
            )
        if (
            after.purchasing_department_id
            and after.purchasing_department_id != rec.purchasing_department_id
        ):
            _require_master(
                uow,
                MasterKind.DEPARTMENT,
                after.purchasing_department_id,
                "purchasing department",
            )
        changes.append(ItemChange(rec.id, after))
    for dc in req.demands:
        found = items.get(dc.plan_item_id)
        if found is not None and dc.department_id not in {d.department_id for d in found.demands}:
            _require_master(uow, MasterKind.DEPARTMENT, dc.department_id, "demand department")
    news: list[NewItem] = []
    for n in req.new_items:
        name, unit = (n.item_name or "").strip(), (n.unit or "").strip()
        if n.item_id is not None:
            _require_master(uow, MasterKind.ITEM, n.item_id, "item")
            master = uow.masters.get(MasterKind.ITEM, n.item_id)
            assert master is not None
            name, unit = master.name, master.unit or ""
        _require_master(uow, MasterKind.DEPARTMENT, n.owner_department_id, "owner department")
        if n.purchasing_department_id:
            _require_master(
                uow, MasterKind.DEPARTMENT, n.purchasing_department_id, "purchasing department"
            )
        for dept, _ in n.demands:
            _require_master(uow, MasterKind.DEPARTMENT, dept, "demand department")
        news.append(
            NewItem(
                n.ref,
                ItemValues(
                    plan_type=n.plan_type,
                    item_id=n.item_id,
                    owner_department_id=n.owner_department_id,
                    purchasing_department_id=n.purchasing_department_id,
                    fund_source_id=n.fund_source_id,
                    budget_category_id=n.budget_category_id,
                    planned_qty=n.planned_qty,
                    estimated_unit_price=n.estimated_unit_price,
                    planned_amount=n.planned_amount,
                    item_name=name,
                    unit=unit,
                ),
                n.demands,
            )
        )

    plan = plan_amendment(
        [BudgetLine(b.fund_source_id, b.budget_category_id, b.approved_amount) for b in budgets],
        current,
        [
            BudgetLine(bc.fund_source_id, bc.budget_category_id, bc.approved_amount)
            for bc in req.budgets
        ],
        changes,
        news,
        req.demands,
    )
    labels = {r.id: (r.item_code or "", r.item_name, r.unit) for r in items.values()}
    return _Judged(year_id, budgets, items, AmendmentPreview(plan, used, labels))


def _placeholder() -> ItemValues:
    """Values for a change naming an item outside the plan year (the domain reports it)."""
    return ItemValues(PlanType.DEPARTMENT, "", "", None, "", "", ZERO, ZERO, ZERO)


def preview_amendment(
    uow: UnitOfWork, fiscal_year: int, req: AmendmentRequest, today: date
) -> AmendmentPreview:
    """Judge the amendment without writing anything (the caller rolls back)."""
    return _judge(uow, fiscal_year, req, today).preview


def _refuse(plan: AmendmentPlan) -> ConflictError:
    return ConflictError(
        "AMENDMENT_NOT_VALID",
        "the amendment cannot be recorded: " + "; ".join(e.message for e in plan.errors),
        details=[
            {"code": e.code.value, "message": e.message, "subject": e.subject} for e in plan.errors
        ],
    )


def amend_plan(
    uow: UnitOfWork, actor: Actor, fiscal_year: int, req: AmendmentRequest, today: date
) -> AmendmentRecord:
    if actor.user_id is None:
        raise InvalidInputError("REQUESTER_REQUIRED", "an amendment must name who recorded it")
    judged = _judge(uow, fiscal_year, req, today)
    plan = judged.preview.plan
    if not plan.ok:
        raise _refuse(plan)

    number = uow.amendments.next_number(judged.year_id)
    amendment_id = uow.amendments.create(
        judged.year_id,
        number,
        req.approval_document_no.strip(),
        req.approval_date,
        req.reason.strip(),
        actor.user_id,
    )

    # Budget lines (existing ones change in place; new lines are created).
    budget_ids = {(b.fund_source_id, b.budget_category_id): b.id for b in judged.budgets}
    for d in plan.budget_diffs:
        key = (d.fund_source_id, d.budget_category_id)
        if d.before is None:
            bid = uow.plans.create_budget(
                judged.year_id, d.fund_source_id, d.budget_category_id, d.after, actor.user_id
            )
            budget_ids[key] = bid
            before = None
        else:
            bid = budget_ids[key]
            uow.plans.update_budget_amount(bid, d.after)
            before = _budget_payload(d.fund_source_id, d.budget_category_id, d.before)
        uow.amendments.add_change(
            amendment_id,
            AmendmentChangeRecord(
                "BUDGET",
                bid,
                "CREATE" if before is None else "UPDATE",
                before,
                _budget_payload(d.fund_source_id, d.budget_category_id, d.after),
            ),
        )

    # Existing Plan Items.
    for diff in plan.item_diffs:
        a = diff.after
        old = judged.items[diff.plan_item_id]
        snap = _snapshot(uow, a, old)
        uow.plans.amend_item(
            diff.plan_item_id,
            AmendedItem(
                plan_budget_id=budget_ids[a.budget_key],
                item_id=a.item_id,
                snapshot=snap,
                owner_department_id=a.owner_department_id,
                purchasing_department_id=a.purchasing_department_id,
                planned_qty=a.planned_qty,
                estimated_unit_price=a.estimated_unit_price,
                planned_amount=a.planned_amount,
            ),
        )
        uow.amendments.add_change(
            amendment_id,
            AmendmentChangeRecord(
                "ITEM",
                diff.plan_item_id,
                "UPDATE",
                _values_payload(diff.before, ItemSnapshot(old.item_code, old.item_name, old.unit)),
                _values_payload(a, snap),
            ),
        )

    # New Plan Items.
    notes = {n.ref: n.note for n in req.new_items}
    new_ids: dict[str, int] = {}
    for new in plan.new_items:
        v = new.values
        new_snap = _snapshot(uow, v, None)
        item_id = uow.plans.create_item(
            judged.year_id,
            PlanItemInput(
                plan_budget_id=budget_ids[v.budget_key],
                plan_type=v.plan_type,
                item_id=v.item_id,
                owner_department_id=v.owner_department_id,
                purchasing_department_id=v.purchasing_department_id,
                planned_qty=v.planned_qty,
                estimated_unit_price=v.estimated_unit_price,
                planned_amount=v.planned_amount,
                note=notes.get(new.ref),
                demands=tuple(DemandInput(d, q) for d, q in new.demands),
            ),
            new_snap,
            actor.user_id,
        )
        new_ids[new.ref] = item_id
        uow.amendments.add_change(
            amendment_id,
            AmendmentChangeRecord("ITEM", item_id, "CREATE", None, _values_payload(v, new_snap)),
        )
        for dept, qty in new.demands:
            _demand_change(uow, amendment_id, item_id, dept, None, qty, created=True)

    # Department demand of existing ASSIGNED items (D-38 A-6).
    for dd in plan.demand_diffs:
        pid = int(dd.target)
        _demand_change(uow, amendment_id, pid, dd.department_id, dd.before, dd.after)

    # Ledger (checked against each item's ledger by the domain before it is written).
    for entry in ledger_entries(amendment_id, plan, new_ids):
        pid = int(entry.plan_item_id)
        PlanItemLedger(entry.plan_item_id, uow.ledger.entries(pid)).append(entry)
        uow.ledger.append(entry, actor.user_id, plan_amendment_id=amendment_id)

    record = uow.amendments.get(amendment_id)
    assert record is not None
    assigned_services.refresh_demands(
        uow,
        {int(d.target) for d in plan.demand_diffs} | set(new_ids.values()),
        actor,
        {"plan_amendment_id": amendment_id, "amendment_no": number},
        prior={d.id: d.state for r in judged.items.values() for d in r.demands if d.id is not None},
    )
    uow.audit.write(
        AuditEntry(
            actor,
            "PLAN_AMENDED",
            "plan_amendment",
            str(amendment_id),
            after={
                "fiscal_year": fiscal_year,
                "amendment_no": number,
                "approval_document_no": record.approval_document_no,
                "approval_date": record.approval_date.isoformat(),
                "changes": [
                    {
                        "target": c.target,
                        "target_id": c.target_id,
                        "change_kind": c.change_kind,
                        "before": c.before,
                        "after": c.after,
                    }
                    for c in record.changes
                ],
                "warnings": [
                    {"code": w.code.value, "message": w.message, "subject": w.subject}
                    for w in plan.warnings
                ],
            },
            reason=record.reason,
        )
    )
    return record


def _demand_payload(plan_item_id: int, department_id: str, qty: Decimal) -> dict[str, Any]:
    return {
        "plan_item_id": plan_item_id,
        "department_id": department_id,
        "demand_qty": str(qty.quantize(QTY_SCALE)),
    }


def _demand_change(
    uow: UnitOfWork,
    amendment_id: int,
    plan_item_id: int,
    department_id: str,
    before: Decimal | None,
    after: Decimal,
    *,
    created: bool = False,
) -> None:
    """Write one department's demand and its history (target DEMAND, id of the demand row)."""
    if created:
        rec = uow.plans.get_item(plan_item_id)
        assert rec is not None  # created in this transaction
        demand_id = next(d.id for d in rec.demands if d.department_id == department_id)
    else:
        demand_id = uow.assigned.set_demand(plan_item_id, department_id, after)
    assert demand_id is not None
    uow.amendments.add_change(
        amendment_id,
        AmendmentChangeRecord(
            "DEMAND",
            demand_id,
            "CREATE" if before is None else "UPDATE",
            None if before is None else _demand_payload(plan_item_id, department_id, before),
            _demand_payload(plan_item_id, department_id, after),
        ),
    )


def _snapshot(uow: UnitOfWork, v: ItemValues, rec: PlanItemRecord | None) -> ItemSnapshot:
    """The item's code/name/unit: kept as recorded when nothing of it changes; a row without
    a HOSxP item carries its own name and unit (D-43); a HOSxP item gives its own."""
    if v.item_id is None:
        return ItemSnapshot(None, v.item_name, v.unit)
    if rec is not None and rec.item_id == v.item_id:
        return ItemSnapshot(rec.item_code, rec.item_name, rec.unit)
    row = uow.masters.get(MasterKind.ITEM, v.item_id)
    assert row is not None  # checked by _require_master
    return ItemSnapshot(row.code, row.name, row.unit or "")


# ------------------------------------------------------------------ reading
def list_amendments(uow: UnitOfWork, fiscal_year: int) -> list[AmendmentRecord]:
    if uow.plan_years.get(fiscal_year) is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
    return uow.amendments.list(fiscal_year)


def get_amendment(uow: UnitOfWork, amendment_id: int) -> AmendmentRecord:
    rec = uow.amendments.get(amendment_id)
    if rec is None:
        raise NotFoundError("AMENDMENT_NOT_FOUND", f"amendment {amendment_id} not found")
    return rec


def touches_department(change: AmendmentChangeRecord, department_id: str) -> bool:
    """An item change concerns a department that could see the item before or after it
    (§22.1, D-38 C-4: a CENTRAL item is seen by its purchasing department, not its owner)."""
    for side in (change.before, change.after):
        if side and visible_to_department(
            department_id,
            PlanType(side.get("plan_type", PlanType.DEPARTMENT.value)),
            str(side.get("owner_department_id") or ""),
            side.get("purchasing_department_id"),
        ):
            return True
    return False


def _demand_of(change: AmendmentChangeRecord, department_id: str) -> bool:
    return any(
        side and side.get("department_id") == department_id
        for side in (change.before, change.after)
    )


def scoped(
    records: Sequence[AmendmentRecord], department_scope: str | None
) -> list[AmendmentRecord]:
    """§22.1: a requester-only user sees the item changes of their own department (owner or
    purchaser, before or after), changes to their own demand on an ASSIGNED item (D-38) and
    no budget-line changes; amendments with nothing left to
    show are hidden. ``None`` = organisation-wide."""
    if department_scope is None:
        return list(records)
    out: list[AmendmentRecord] = []
    for r in records:
        visible = tuple(
            c
            for c in r.changes
            if (c.target == "ITEM" and touches_department(c, department_scope))
            or (c.target == "DEMAND" and _demand_of(c, department_scope))
        )
        if visible:
            out.append(
                AmendmentRecord(
                    r.id,
                    r.fiscal_year,
                    r.amendment_no,
                    r.approval_document_no,
                    r.approval_date,
                    r.reason,
                    r.created_by_user_id,
                    r.created_at,
                    visible,
                )
            )
    return out
