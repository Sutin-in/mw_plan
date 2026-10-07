"""Assigned Purchase rules (spec §10.3; D-38 A-1 ... A-6, G-3).

One pooled ASSIGNED Plan Item records each department's demand. The purchasing department's
PPR draws on the item and states how much it buys for each demand department (coverage).
Demand states are DERIVED, never set by hand:

* ``CANCELLED`` - the demand was amended to 0 (A-6);
* ``PLANNED`` - PPRs still in effect cover less than the demand (A-2), also after a covering
  PPR was cancelled (A-4);
* ``INCLUDED_IN_CENTRAL_PURCHASE`` - fully covered, not every covering PPR verified;
* ``FULFILLED`` - fully covered and every covering PPR verified by Procurement (A-3).

A PPR "in effect" is a confirmed one that is not cancelled. While it is under PR-change review
or unlocked for revision it keeps its last confirmed coverage, and counts as verified if that
version was (A-5).

Pure functions over plain values; persistence and HTTP live elsewhere.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ppr.domain.plan import DemandState
from ppr.domain.ppr_state import PprState

ZERO = Decimal(0)

# PPR states whose confirmed coverage counts (A-2, A-4, A-5). CANCELLED never; a DRAFT has
# never been confirmed.
EFFECTIVE_STATES = frozenset(
    {
        PprState.CONFIRMED_LOCKED,
        PprState.PROCUREMENT_VERIFIED,
        PprState.PR_CHANGED_REVIEW_REQUIRED,
        PprState.UNLOCKED_FOR_REVISION,
        PprState.CLOSED,
    }
)
# Demand states that block the department from buying the item itself (A-1).
BLOCKING_STATES = frozenset({DemandState.PLANNED, DemandState.INCLUDED_IN_CENTRAL_PURCHASE})


def counts_as_verified(state: PprState, current_version_verified: bool) -> bool:
    """A-3 / A-5: the version of a PPR in effect was verified by Procurement. It stays so while
    the PPR is under PR-change review, unlocked for revision or closed (the verified version
    is still the one in effect); a reconfirmation makes a new, unverified version."""
    return state in EFFECTIVE_STATES and (
        state is PprState.PROCUREMENT_VERIFIED or current_version_verified
    )


@dataclass(frozen=True)
class CoverageRow:
    """Confirmed coverage of one PPR for one demand department of one ASSIGNED item."""

    ppr_id: int
    department_id: str
    qty: Decimal
    ppr_state: PprState
    verified: bool  # counts_as_verified(...) for that PPR

    @property
    def effective(self) -> bool:
        return self.ppr_state in EFFECTIVE_STATES


def covered(
    rows: Iterable[CoverageRow], department_id: str, *, except_ppr: int | None = None
) -> Decimal:
    """What PPRs in effect cover for one department (optionally leaving one PPR out)."""
    return sum(
        (
            r.qty
            for r in rows
            if r.effective and r.department_id == department_id and r.ppr_id != except_ppr
        ),
        ZERO,
    )


def demand_state(
    demand_qty: Decimal, rows: Iterable[CoverageRow], department_id: str
) -> DemandState:
    """The state a demand should be in, from the coverage of PPRs in effect."""
    if demand_qty == ZERO:
        return DemandState.CANCELLED
    mine = [r for r in rows if r.effective and r.department_id == department_id]
    if sum((r.qty for r in mine), ZERO) < demand_qty:
        return DemandState.PLANNED
    if all(r.verified for r in mine):
        return DemandState.FULFILLED
    return DemandState.INCLUDED_IN_CENTRAL_PURCHASE


@dataclass(frozen=True)
class DemandFacts:
    department_id: str
    demand_qty: Decimal


class CoverageCode(StrEnum):
    COVERAGE_REQUIRED = "COVERAGE_REQUIRED"
    COVERAGE_SUM_MISMATCH = "COVERAGE_SUM_MISMATCH"
    COVERAGE_UNKNOWN_DEPARTMENT = "COVERAGE_UNKNOWN_DEPARTMENT"
    COVERAGE_NON_POSITIVE = "COVERAGE_NON_POSITIVE"
    COVERAGE_EXCEEDS_NEED = "COVERAGE_EXCEEDS_NEED"
    COVERAGE_NOT_IN_PPR = "COVERAGE_NOT_IN_PPR"
    COVERAGE_DEMAND_CLAIMED_ELSEWHERE = "COVERAGE_DEMAND_CLAIMED_ELSEWHERE"


@dataclass(frozen=True)
class CoverageIssue:
    code: CoverageCode
    message: str
    subject: str  # "plan_item_id" or "plan_item_id/department"


def check_coverage(
    drawn: Mapping[int, Decimal],
    coverage: Mapping[int, Mapping[str, Decimal]],
    demands: Mapping[int, Sequence[DemandFacts]],
    covered_elsewhere: Mapping[tuple[int, str], Decimal],
    claimed_elsewhere: frozenset[tuple[int, str]] = frozenset(),
) -> list[CoverageIssue]:
    """A-2: for each ASSIGNED item a PPR draws on (``drawn``: plan item -> quantity), the
    quantities bought per demand department add up exactly to that quantity, are positive,
    name a department with open demand, and never exceed what that department still needs
    (its demand minus what other PPRs in effect cover). Coverage of an item the PPR does
    not draw on is refused. A department whose need for the same HOSxP item and fund is also
    open demand on another Assigned Purchase item (``claimed_elsewhere``: plan item,
    department) is refused too: one demand is bought by one purchase line only (plans made
    before that rule; activation and amendment refuse such plans now)."""
    out: list[CoverageIssue] = []
    for pid in sorted(set(coverage) - set(drawn)):
        out.append(
            CoverageIssue(
                CoverageCode.COVERAGE_NOT_IN_PPR,
                f"Plan Item {pid} is not drawn on by this PPR",
                str(pid),
            )
        )
    for pid, qty in sorted(drawn.items()):
        given = coverage.get(pid, {})
        if not given:
            out.append(
                CoverageIssue(
                    CoverageCode.COVERAGE_REQUIRED,
                    f"state how much of the {qty} bought on Plan Item {pid} is for each "
                    "department (D-38 A-2)",
                    str(pid),
                )
            )
            continue
        open_demand = {
            d.department_id: d.demand_qty for d in demands.get(pid, ()) if d.demand_qty > ZERO
        }
        for dept, q in sorted(given.items()):
            subject = f"{pid}/{dept}"
            if dept not in open_demand:
                out.append(
                    CoverageIssue(
                        CoverageCode.COVERAGE_UNKNOWN_DEPARTMENT,
                        f"department {dept} has no open demand on Plan Item {pid}",
                        subject,
                    )
                )
                continue
            if (pid, dept) in claimed_elsewhere:
                out.append(
                    CoverageIssue(
                        CoverageCode.COVERAGE_DEMAND_CLAIMED_ELSEWHERE,
                        f"department {dept} also has open demand for this item on another "
                        "Assigned Purchase item; one demand is bought by one purchase line "
                        "only - amend the plan first (D-38)",
                        subject,
                    )
                )
                continue
            if q <= ZERO:
                out.append(
                    CoverageIssue(
                        CoverageCode.COVERAGE_NON_POSITIVE,
                        f"the quantity for department {dept} must be above 0",
                        subject,
                    )
                )
                continue
            need = open_demand[dept] - covered_elsewhere.get((pid, dept), ZERO)
            if q > need:
                out.append(
                    CoverageIssue(
                        CoverageCode.COVERAGE_EXCEEDS_NEED,
                        f"department {dept} still needs {max(need, ZERO)} on Plan Item {pid}, "
                        f"not {q}",
                        subject,
                    )
                )
        total = sum(given.values(), ZERO)
        if total != qty:
            out.append(
                CoverageIssue(
                    CoverageCode.COVERAGE_SUM_MISMATCH,
                    f"the departments add up to {total}, the PPR buys {qty} on Plan Item {pid}",
                    str(pid),
                )
            )
    return out


def default_coverage(
    qty: Decimal,
    demands: Sequence[DemandFacts],
    covered_elsewhere: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    """A proposal for a new draft: when the quantity bought equals exactly what all
    departments still need, each department gets its need; otherwise nothing is proposed and
    the purchaser decides (A-2)."""
    needs = {
        d.department_id: d.demand_qty - covered_elsewhere.get(d.department_id, ZERO)
        for d in demands
        if d.demand_qty > ZERO
    }
    needs = {k: v for k, v in needs.items() if v > ZERO}
    if needs and sum(needs.values(), ZERO) == qty:
        return needs
    return {}


def blocked_keys(
    department_id: str,
    assigned: Iterable[tuple[str, str, str, str, DemandState]],
) -> frozenset[tuple[str, str]]:
    """A-1: (HOSxP item, fund source) pairs the department may not buy itself, from
    (item, fund source, purchasing department, demand department, demand state) of the
    ASSIGNED items' demand rows. The purchaser's own demand never blocks its own purchase
    (that PPR draws on the ASSIGNED item)."""
    return frozenset(
        (item_id, fund)
        for item_id, fund, purchaser, dept, state in assigned
        if dept == department_id and dept != purchaser and state in BLOCKING_STATES
    )
