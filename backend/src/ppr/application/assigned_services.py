"""Wave 7B-2 use cases: Assigned Purchase coverage and demand states (D-38 A-1 ... A-6, G-3).

* The purchasing department's PPR states how much it buys for each demand department
  (``WORKING`` coverage, edited while the PPR is a draft or unlocked); confirmation checks it
  (``check_coverage``) and makes it the PPR's ``CONFIRMED`` coverage.
* Demand states are derived (``ppr.domain.assigned.demand_state``) and refreshed after every
  event that can change them: confirmation / reconfirmation, verification, a state change or
  release by PR synchronization, and a demand amendment. Each change is audited
  (``DEMAND_STATE_CHANGED``) with the event that caused it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ppr.application.errors import ConflictError, InvalidInputError
from ppr.domain.assigned import (
    CoverageRow,
    DemandFacts,
    blocked_keys,
    check_coverage,
    counts_as_verified,
    covered,
    default_coverage,
    demand_state,
)
from ppr.domain.plan import DemandState, PlanType
from ppr.domain.ppr_state import PprState
from ppr.integration.hosxp.contracts import PrHeader
from ppr.ports import Actor, AuditEntry, PlanItemRecord, PprRecord, UnitOfWork

ZERO = Decimal(0)
QTY_SCALE = Decimal("0.0001")
EDITABLE = (PprState.DRAFT, PprState.UNLOCKED_FOR_REVISION)


def _rows(uow: UnitOfWork, plan_item_ids: Iterable[int]) -> dict[int, list[CoverageRow]]:
    out: dict[int, list[CoverageRow]] = {}
    for c in uow.assigned.confirmed_for_items(list(plan_item_ids)):
        out.setdefault(c.plan_item_id, []).append(
            CoverageRow(
                c.ppr_id,
                c.department_id,
                c.qty,
                c.ppr_state,
                counts_as_verified(c.ppr_state, c.current_version_verified),
            )
        )
    return out


def covered_elsewhere(
    uow: UnitOfWork, plan_item_ids: Iterable[int], except_ppr: int | None
) -> dict[tuple[int, str], Decimal]:
    """What PPRs in effect (other than ``except_ppr``) cover, per (plan item, department)."""
    out: dict[tuple[int, str], Decimal] = {}
    for pid, rows in _rows(uow, plan_item_ids).items():
        for dept in {r.department_id for r in rows}:
            out[(pid, dept)] = covered(rows, dept, except_ppr=except_ppr)
    return out


def _demands(item: PlanItemRecord) -> list[DemandFacts]:
    return [DemandFacts(d.department_id, d.demand_qty) for d in item.demands]


# ------------------------------------------------------------------ demand states
def refresh_demands(
    uow: UnitOfWork,
    plan_item_ids: Iterable[int],
    actor: Actor,
    cause: Mapping[str, Any],
    prior: Mapping[int, DemandState] | None = None,
) -> list[dict[str, Any]]:
    """Recompute the demand states of these ASSIGNED items; write and audit every change.

    ``prior`` (demand id -> state) gives the states before the caller's own writes in this
    transaction (a demand amendment stores a consistent interim state with the quantity), so
    the audit records the change from what the state really was."""
    items = [uow.plans.get_item(i) for i in sorted(set(plan_item_ids))]
    assigned = [r for r in items if r is not None and r.plan_type is PlanType.ASSIGNED]
    if not assigned:
        return []
    uow.assigned.lock_demands([r.id for r in assigned])
    rows = _rows(uow, [r.id for r in assigned])
    changes: list[dict[str, Any]] = []
    for item in assigned:
        fresh = uow.plans.get_item(item.id)
        assert fresh is not None
        for d in fresh.demands:
            if d.id is None:  # pragma: no cover - repositories always read the id
                continue
            want = demand_state(d.demand_qty, rows.get(item.id, []), d.department_id)
            if want is not d.state:
                uow.assigned.set_demand_state(d.id, want)
            was = (prior or {}).get(d.id, d.state)
            if want is was:
                continue
            change = {
                "plan_item_id": item.id,
                "department_id": d.department_id,
                "demand_qty": str(d.demand_qty),
                "state_before": was.value,
                "state_after": want.value,
            }
            changes.append(change)
            uow.audit.write(
                AuditEntry(
                    actor,
                    "DEMAND_STATE_CHANGED",
                    "plan_item_demand",
                    str(d.id),
                    before={"state": was.value},
                    after={**change, "cause": dict(cause)},
                )
            )
    return changes


def assigned_items_of(uow: UnitOfWork, plan_item_ids: Iterable[int]) -> list[int]:
    out = []
    for pid in sorted(set(plan_item_ids)):
        rec = uow.plans.get_item(pid)
        if rec is not None and rec.plan_type is PlanType.ASSIGNED:
            out.append(pid)
    return out


def assigned_items_for_pr(uow: UnitOfWork, fiscal_year: int, header: PrHeader) -> list[int]:
    """ASSIGNED items of the year the PR's department sees (buys or has demand on) with an
    item and the fund source of the PR: the ones whose demand rows A-1 and A-2 read."""
    if header.department_id is None:
        return []
    return [
        r.id
        for r in uow.plans.items(fiscal_year, header.department_id)
        if r.plan_type is PlanType.ASSIGNED and r.fund_source_id == header.fund_source_id
    ]


# ------------------------------------------------------------------ coverage of one PPR
@dataclass(frozen=True)
class DemandLine:
    department_id: str
    demand_qty: Decimal
    covered_by_others: Decimal
    state: str
    working: Decimal  # this PPR's working coverage for the department
    confirmed: Decimal  # this PPR's confirmed coverage for the department


@dataclass(frozen=True)
class ItemCoverage:
    plan_item_id: int
    item_code: str
    item_name: str
    unit: str
    drawn_qty: Decimal  # what the PPR's lines buy on this item
    lines: tuple[DemandLine, ...]


def _drawn(rec: PprRecord, uow: UnitOfWork) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    for line in rec.items:
        item = uow.plans.get_item(line.plan_item_id)
        if item is not None and item.plan_type is PlanType.ASSIGNED:
            out[item.id] = out.get(item.id, ZERO) + line.qty
    return out


def coverage_view(
    uow: UnitOfWork, rec: PprRecord, drawn_now: Mapping[int, Decimal] | None = None
) -> list[ItemCoverage]:
    """``drawn_now``: for an editable PPR, what its PR buys now (the lines confirmation will
    check); otherwise the PPR's own lines."""
    drawn = dict(drawn_now) if drawn_now is not None else _drawn(rec, uow)
    if not drawn:
        return []
    working = uow.assigned.coverage(rec.id, confirmed=False)
    confirmed = uow.assigned.coverage(rec.id, confirmed=True)
    others = covered_elsewhere(uow, drawn, rec.id)
    out = []
    for pid, qty in sorted(drawn.items()):
        item = uow.plans.get_item(pid)
        assert item is not None
        out.append(
            ItemCoverage(
                pid,
                item.item_code or "",  # ASSIGNED items always carry a HOSxP item (D-42)
                item.item_name,
                item.unit,
                qty.quantize(QTY_SCALE),  # as stored (D-23), live or not
                tuple(
                    DemandLine(
                        d.department_id,
                        d.demand_qty,
                        others.get((pid, d.department_id), ZERO),
                        d.state.value,
                        working.get(pid, {}).get(d.department_id, ZERO),
                        confirmed.get(pid, {}).get(d.department_id, ZERO),
                    )
                    for d in item.demands
                ),
            )
        )
    return out


def propose_default(uow: UnitOfWork, rec: PprRecord, drawn: Mapping[int, Decimal]) -> None:
    """A new draft gets a proposal when the purchase meets every department's need (A-2)."""
    if not drawn:
        return
    others = covered_elsewhere(uow, drawn, rec.id)
    proposal: dict[int, dict[str, Decimal]] = {}
    for pid, qty in drawn.items():
        item = uow.plans.get_item(pid)
        assert item is not None
        mine = {dept: q for (p, dept), q in others.items() if p == pid}
        if d := default_coverage(qty, _demands(item), mine):
            proposal[pid] = d
    if proposal:
        uow.assigned.replace_coverage(rec.id, proposal, confirmed=False)


def set_coverage(
    uow: UnitOfWork,
    actor: Actor,
    rec: PprRecord,
    coverage: Mapping[int, Mapping[str, Decimal]],
    drawn_now: Mapping[int, Decimal] | None = None,
) -> None:
    """The purchasing department edits the working coverage of its draft or unlocked PPR
    (the caller has checked it is a requester of that department, D-09). Shape is checked
    now; the totals are enforced at confirmation, like allocations."""
    if rec.state not in EDITABLE:
        raise ConflictError("PPR_LOCKED", f"PPR is {rec.state.value}; it cannot be edited")
    drawn = dict(drawn_now) if drawn_now is not None else _drawn(rec, uow)
    clean: dict[int, dict[str, Decimal]] = {}
    for pid, depts in coverage.items():
        if pid not in drawn:
            raise InvalidInputError(
                "COVERAGE_NOT_IN_PPR", f"Plan Item {pid} is not drawn on by this PPR"
            )
        item = uow.plans.get_item(pid)
        assert item is not None
        known = {d.department_id for d in item.demands if d.demand_qty > ZERO}
        for dept, qty in depts.items():
            if dept not in known:
                raise InvalidInputError(
                    "COVERAGE_UNKNOWN_DEPARTMENT",
                    f"department {dept} has no open demand on Plan Item {pid}",
                )
            if qty < ZERO:
                raise InvalidInputError("COVERAGE_NON_POSITIVE", "quantities cannot be negative")
            if qty > ZERO:
                clean.setdefault(pid, {})[dept] = qty
    before = uow.assigned.coverage(rec.id, confirmed=False)
    uow.assigned.replace_coverage(rec.id, clean, confirmed=False)
    uow.audit.write(
        AuditEntry(
            actor,
            "PPR_COVERAGE_UPDATED",
            "ppr",
            str(rec.id),
            before={"coverage": _json(before)},
            after={"coverage": _json(clean)},
        )
    )


def _json(c: Mapping[int, Mapping[str, Decimal]]) -> dict[str, dict[str, str]]:
    return {str(p): {d: str(q) for d, q in v.items()} for p, v in c.items()}


def _claimed_elsewhere(
    uow: UnitOfWork, fiscal_year: int, drawn: Iterable[int]
) -> frozenset[tuple[int, str]]:
    """(drawn ASSIGNED item, department) where the department also has open demand on another
    ASSIGNED item of the year with the same HOSxP item and fund source."""
    year = [r for r in uow.plans.items(fiscal_year) if r.plan_type is PlanType.ASSIGNED]
    out: set[tuple[int, str]] = set()
    for pid in drawn:
        me = next((r for r in year if r.id == pid), None)
        if me is None:
            continue
        for other in year:
            if other.id == pid or (other.item_id, other.fund_source_id) != (
                me.item_id,
                me.fund_source_id,
            ):
                continue
            out |= {(pid, d.department_id) for d in other.demands if d.demand_qty > ZERO}
    return frozenset(out)


def check_for_confirmation(
    uow: UnitOfWork, ppr_id: int, drawn: Mapping[int, Decimal], fiscal_year: int
) -> dict[int, dict[str, Decimal]]:
    """A-2 at (re)confirmation: the working coverage must fit the lines being confirmed."""
    working = uow.assigned.coverage(ppr_id, confirmed=False)
    if not drawn and not working:
        return {}
    uow.assigned.lock_demands(list(drawn))
    demands = {}
    for pid in drawn:
        item = uow.plans.get_item(pid)
        assert item is not None
        demands[pid] = _demands(item)
    issues = check_coverage(
        drawn,
        working,
        demands,
        covered_elsewhere(uow, drawn, ppr_id),
        _claimed_elsewhere(uow, fiscal_year, drawn),
    )
    if issues:
        raise ConflictError(
            "COVERAGE_INVALID",
            "; ".join(i.message for i in issues),
            details=[
                {"code": i.code.value, "message": i.message, "subject": i.subject} for i in issues
            ],
        )
    return working


def confirm_coverage(
    uow: UnitOfWork, ppr_id: int, working: Mapping[int, Mapping[str, Decimal]]
) -> None:
    uow.assigned.replace_coverage(ppr_id, {p: dict(v) for p, v in working.items()}, confirmed=True)


def start_revision(uow: UnitOfWork, ppr_id: int) -> None:
    """Unlocking for revision starts the working coverage from the confirmed one (A-5)."""
    uow.assigned.replace_coverage(
        ppr_id, uow.assigned.coverage(ppr_id, confirmed=True), confirmed=False
    )


def coverage_json(c: Mapping[int, Mapping[str, Decimal]]) -> list[dict[str, Any]]:
    """For the confirmed version's snapshot (evidence)."""
    return [
        {"plan_item_id": pid, "department_id": dept, "qty": str(qty)}
        for pid, depts in sorted(c.items())
        for dept, qty in sorted(depts.items())
    ]


def blocked_for(items: Sequence[PlanItemRecord], department_id: str) -> frozenset[tuple[str, str]]:
    """A-1 input for prevalidation: the (item, fund) keys the department may not buy now."""
    return blocked_keys(
        department_id,
        (
            (
                r.item_id,
                r.fund_source_id,
                r.purchasing_department_id or "",
                d.department_id,
                d.state,
            )
            for r in items
            if r.plan_type is PlanType.ASSIGNED and r.item_id is not None
            for d in r.demands
        ),
    )
