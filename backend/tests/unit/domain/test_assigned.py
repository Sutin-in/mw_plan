"""Assigned Purchase rules (D-38 A-1 ... A-5, G-3)."""

from __future__ import annotations

from decimal import Decimal as D  # noqa: N817

import pytest

from ppr.domain.assigned import (
    CoverageCode,
    CoverageRow,
    DemandFacts,
    blocked_keys,
    check_coverage,
    counts_as_verified,
    covered,
    default_coverage,
    demand_state,
)
from ppr.domain.plan import DemandState
from ppr.domain.ppr_state import PprState as S


def row(
    ppr: int, dept: str, qty: int, state: S = S.CONFIRMED_LOCKED, verified: bool = False
) -> CoverageRow:
    return CoverageRow(ppr, dept, D(qty), state, verified)


@pytest.mark.spec("D-38", "G-3")
def test_demand_state_follows_coverage_of_pprs_in_effect() -> None:
    assert demand_state(D(10), [], "A") is DemandState.PLANNED
    assert demand_state(D(10), [row(1, "A", 6)], "A") is DemandState.PLANNED  # partly covered
    assert demand_state(D(10), [row(1, "A", 6), row(2, "A", 4)], "A") is (
        DemandState.INCLUDED_IN_CENTRAL_PURCHASE
    )
    done = [
        row(1, "A", 6, S.PROCUREMENT_VERIFIED, True),
        row(2, "A", 4, S.PROCUREMENT_VERIFIED, True),
    ]
    assert demand_state(D(10), done, "A") is DemandState.FULFILLED
    # A-4: a covering PPR cancelled -> back to PLANNED, even from FULFILLED
    cancelled = [done[0], row(2, "A", 4, S.CANCELLED, True)]
    assert demand_state(D(10), cancelled, "A") is DemandState.PLANNED
    # another department's coverage does not count
    assert demand_state(D(10), [row(1, "B", 10)], "A") is DemandState.PLANNED
    assert demand_state(D(0), done, "A") is DemandState.CANCELLED  # A-6


@pytest.mark.spec("D-38", "G-3")
def test_unlocked_ppr_keeps_its_coverage_and_its_verification() -> None:
    # A-5: while unlocked, the last confirmed coverage still counts, verified if that version was
    assert counts_as_verified(S.UNLOCKED_FOR_REVISION, True)
    assert not counts_as_verified(S.UNLOCKED_FOR_REVISION, False)
    assert counts_as_verified(S.PROCUREMENT_VERIFIED, False)
    assert counts_as_verified(S.PR_CHANGED_REVIEW_REQUIRED, True)  # the verified version
    assert not counts_as_verified(S.PR_CHANGED_REVIEW_REQUIRED, False)
    assert not counts_as_verified(S.CONFIRMED_LOCKED, False)  # reconfirmed: a new version
    assert not counts_as_verified(S.CANCELLED, True)
    unlocked = [row(1, "A", 10, S.UNLOCKED_FOR_REVISION, True)]
    assert demand_state(D(10), unlocked, "A") is DemandState.FULFILLED
    assert covered(unlocked, "A") == D(10) and covered(unlocked, "A", except_ppr=1) == 0


DEMANDS = {7: [DemandFacts("A", D(10)), DemandFacts("B", D(20)), DemandFacts("C", D(30))]}


@pytest.mark.spec("D-38", "G-3")
def test_coverage_must_add_up_and_never_exceed_need() -> None:
    ok = check_coverage({7: D(60)}, {7: {"A": D(10), "B": D(20), "C": D(30)}}, DEMANDS, {})
    assert ok == []
    partial = check_coverage({7: D(40)}, {7: {"A": D(10), "C": D(30)}}, DEMANDS, {})
    assert partial == []  # 6B: the purchaser chooses whom a partial purchase is for

    def codes(*args):  # type: ignore[no-untyped-def]
        return {i.code for i in check_coverage(*args)}

    assert codes({7: D(40)}, {}, DEMANDS, {}) == {CoverageCode.COVERAGE_REQUIRED}
    assert codes({7: D(40)}, {7: {"A": D(10)}}, DEMANDS, {}) == {CoverageCode.COVERAGE_SUM_MISMATCH}
    assert CoverageCode.COVERAGE_EXCEEDS_NEED in codes({7: D(11)}, {7: {"A": D(11)}}, DEMANDS, {})
    # A already covered 4 by another PPR: needs 6
    assert CoverageCode.COVERAGE_EXCEEDS_NEED in codes(
        {7: D(7)}, {7: {"A": D(7)}}, DEMANDS, {(7, "A"): D(4)}
    )
    assert CoverageCode.COVERAGE_UNKNOWN_DEPARTMENT in codes(
        {7: D(1)}, {7: {"Z": D(1)}}, DEMANDS, {}
    )
    assert CoverageCode.COVERAGE_NON_POSITIVE in codes({7: D(0)}, {7: {"A": D(0)}}, DEMANDS, {})
    assert CoverageCode.COVERAGE_NOT_IN_PPR in codes({}, {7: {"A": D(1)}}, DEMANDS, {})


@pytest.mark.spec("D-38", "G-3")
def test_default_coverage_only_when_the_purchase_meets_every_need() -> None:
    assert default_coverage(D(60), DEMANDS[7], {}) == {"A": D(10), "B": D(20), "C": D(30)}
    assert default_coverage(D(50), DEMANDS[7], {"A": D(10)}) == {"B": D(20), "C": D(30)}
    assert default_coverage(D(40), DEMANDS[7], {}) == {}


@pytest.mark.spec("D-38", "G-3")
def test_open_demand_blocks_the_department_but_not_the_purchaser() -> None:
    rows = [
        ("I1", "F1", "STORE", "A", DemandState.PLANNED),
        ("I1", "F1", "STORE", "B", DemandState.FULFILLED),
        ("I2", "F1", "STORE", "A", DemandState.CANCELLED),
        ("I3", "F1", "STORE", "STORE", DemandState.PLANNED),
        ("I4", "F1", "STORE", "A", DemandState.INCLUDED_IN_CENTRAL_PURCHASE),
    ]
    assert blocked_keys("A", rows) == {("I1", "F1"), ("I4", "F1")}
    assert blocked_keys("B", rows) == frozenset()
    assert blocked_keys("STORE", rows) == frozenset()


@pytest.mark.spec("D-38", "G-3")
def test_coverage_of_a_demand_claimed_by_another_assigned_purchase_is_refused() -> None:
    issues = check_coverage(
        {7: D(10)},
        {7: {"A": D(10)}},
        DEMANDS,
        {},
        frozenset({(7, "A")}),
    )
    assert [i.code for i in issues] == [CoverageCode.COVERAGE_DEMAND_CLAIMED_ELSEWHERE]
