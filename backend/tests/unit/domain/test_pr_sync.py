"""PR synchronization rules: precedence, INVALID_EVIDENCE vs CANCELLED, state matrix
(D-29, D-30, D-31; spec §14.6, §16)."""

from __future__ import annotations

from typing import Any

import pytest

from ppr.domain.ppr_state import PprEvent, PprState
from ppr.domain.pr_sync import (
    REACTIVATED_EXTERNALLY,
    SYNC_STATES,
    Assessment,
    EvidenceClass,
    LiveRead,
    ReadFailure,
    SyncOutcome,
    assess,
    assess_recheck,
    control_issues,
    decide,
    needs_items,
    worth_one_fresh_read,
)

PR = "PR-1"


def header(**over: Any) -> dict[str, Any]:
    h: dict[str, Any] = {
        "pr_id": "ID-PR-1",
        "pr_no": PR,
        "pr_date": "2026-10-15",
        "fiscal_year": 2570,
        "department_id": "D1",
        "requester_id": None,
        "requester_name": None,
        "fund_source_id": "F1",
        "budget_category_id": "C1",
        "total_amount": "150.00",
        "status": "UNKNOWN",
        "native_status": None,
        "last_modified_at": None,
    }
    h.update(over)
    return h


def item(n: int = 1, **over: Any) -> dict[str, Any]:
    it: dict[str, Any] = {
        "pr_item_id": f"PR-1-{n}",
        "item_id": f"I{n}",
        "item_code": None,
        "item_name": f"item {n}",
        "qty": "1",
        "unit": None,
        "unit_price": "100",
        "amount": "100.00",
    }
    it.update(over)
    return it


CONFIRMED_H = header()
CONFIRMED_I = [item(1), item(2, qty="1", unit_price="50", amount="50.00")]


def run(read: LiveRead) -> Assessment:
    return assess(read, bound_pr_no=PR, confirmed_header=CONFIRMED_H, confirmed_items=CONFIRMED_I)


def ok(h: dict[str, Any] | None = None, items: list[dict[str, Any]] | None = None) -> LiveRead:
    return LiveRead(h or header(), tuple(CONFIRMED_I if items is None else items))


# ------------------------------------------------------------------ precedence rungs
@pytest.mark.spec("D-29", "AT-40.6.3")
def test_unavailable_first() -> None:
    a = run(LiveRead(None, failure=ReadFailure.UNAVAILABLE))
    assert a.outcome is SyncOutcome.UNAVAILABLE


@pytest.mark.spec("D-31")
def test_header_query_failure_is_binding_invalid_evidence() -> None:
    a = run(LiveRead(None, failure=ReadFailure.HEADER_QUERY, failure_message="get_pr: 2 rows"))
    assert (a.outcome, a.evidence_class) == (SyncOutcome.INVALID_EVIDENCE, EvidenceClass.BINDING)
    assert [i.code for i in a.issues] == ["HOSXP_QUERY_ERROR"]


@pytest.mark.spec("D-30")
def test_missing_pr_is_not_found_never_cancelled() -> None:
    a = run(LiveRead(None))
    assert a.outcome is SyncOutcome.NOT_FOUND
    d = decide(PprState.CONFIRMED_LOCKED, a)
    assert (d.state_after, d.release) == (PprState.CONFIRMED_LOCKED, False)


@pytest.mark.spec("D-31")
@pytest.mark.parametrize(
    ("over", "code"),
    [({"pr_no": "PR-OTHER"}, "PR_NO_MISMATCH"), ({"pr_id": "ID-OTHER"}, "PR_ID_MISMATCH")],
)
def test_binding_mismatch_is_binding_invalid(over: dict[str, Any], code: str) -> None:
    a = run(ok(header(**over)))
    assert (a.outcome, a.evidence_class) == (SyncOutcome.INVALID_EVIDENCE, EvidenceClass.BINDING)
    assert code in {i.code for i in a.issues}


@pytest.mark.spec("D-31")
@pytest.mark.parametrize(
    "over",
    [
        {"status": "CANCELLED", "native_status": None},  # normalized without a native value
        {"status": "CANCELLED", "native_status": "  "},
        {"status": "WHATEVER", "native_status": "x"},  # not a known normalized status
    ],
)
def test_malformed_status_is_status_invalid(over: dict[str, Any]) -> None:
    a = run(ok(header(**over)))
    assert (a.outcome, a.evidence_class) == (SyncOutcome.INVALID_EVIDENCE, EvidenceClass.STATUS)


@pytest.mark.spec("D-30", "AT-40.8.1")
def test_mapped_cancelled_cancels() -> None:
    a = run(LiveRead(header(status="CANCELLED", native_status="mock-c")))
    assert a.outcome is SyncOutcome.CANCELLED


@pytest.mark.spec("D-31")
def test_items_query_failure_is_binding_invalid() -> None:
    a = run(LiveRead(header(), failure=ReadFailure.ITEMS_QUERY, failure_message="items: bad"))
    assert (a.outcome, a.evidence_class) == (SyncOutcome.INVALID_EVIDENCE, EvidenceClass.BINDING)
    assert a.issues[0].code == "HOSXP_ITEMS_QUERY_ERROR"


@pytest.mark.spec("D-31", "D-17", "D-23")
@pytest.mark.parametrize(
    ("h_over", "items", "codes"),
    [
        ({"total_amount": "151.00"}, None, {"PR_TOTAL_MISMATCH"}),
        ({"total_amount": None}, None, {"PR_TOTAL_UNAVAILABLE"}),
        ({}, [], {"PR_HAS_NO_ITEMS"}),
        ({"total_amount": "200.00"}, [item(1), item(1)], {"DUPLICATE_PR_ITEM"}),
        (
            {"total_amount": "150.001"},
            [item(1), item(2, qty="1", unit_price="50", amount="50.001")],
            {"PR_VALUE_PRECISION"},
        ),
    ],
)
def test_control_evidence_failures(
    h_over: dict[str, Any], items: list[dict[str, Any]] | None, codes: set[str]
) -> None:
    a = run(ok(header(**h_over), items))
    assert (a.outcome, a.evidence_class) == (SyncOutcome.INVALID_EVIDENCE, EvidenceClass.CONTROL)
    assert codes <= {i.code for i in a.issues}


def test_control_issue_names_field_and_line() -> None:
    issues = control_issues(
        header(total_amount="150.001"), [item(1), item(2, amount="50.001", unit_price="50")]
    )
    precision = [i for i in issues if i.code == "PR_VALUE_PRECISION"]
    assert [(i.field, i.line) for i in precision] == [("amount", "PR-1-2")]


# ------------------------------------------------------------------ explicit precedence pairs
@pytest.mark.spec("D-31", "D-30")
def test_mapped_cancelled_wins_over_invalid_control_evidence() -> None:
    # Bound PR, authoritative CANCELLED, malformed items/total: cancellation proceeds.
    read = LiveRead(
        header(status="CANCELLED", native_status="mock-c", total_amount="999.00"),
        (item(1), item(1)),
    )
    a = run(read)
    assert a.outcome is SyncOutcome.CANCELLED
    d = decide(PprState.PROCUREMENT_VERIFIED, a)
    assert (d.state_after, d.event, d.release) == (
        PprState.CANCELLED,
        PprEvent.CANCEL_FROM_HOSXP,
        True,
    )


@pytest.mark.spec("D-31", "D-30")
@pytest.mark.parametrize("over", [{"pr_id": "ID-OTHER"}, {"pr_no": "PR-OTHER"}])
def test_unreliable_binding_never_cancels(over: dict[str, Any]) -> None:
    a = run(LiveRead(header(status="CANCELLED", native_status="mock-c", **over)))
    assert (a.outcome, a.evidence_class) == (SyncOutcome.INVALID_EVIDENCE, EvidenceClass.BINDING)
    for state in SYNC_STATES:
        d = decide(state, a)
        assert (d.state_after, d.release) == (state, False)


@pytest.mark.spec("D-31", "D-30")
def test_unreadable_status_never_cancels() -> None:
    a = run(LiveRead(header(status="CANCELLED", native_status=None)))
    assert a.evidence_class is EvidenceClass.STATUS
    for state in SYNC_STATES:
        assert decide(state, a) == decide(state, Assessment(SyncOutcome.UNCHANGED))


@pytest.mark.spec("D-30", "D-31")
def test_unknown_status_with_invalid_totals_is_control_invalid_not_cancel() -> None:
    a = run(ok(header(total_amount="1.00")))  # status UNKNOWN (empty mapping)
    assert (a.outcome, a.evidence_class) == (SyncOutcome.INVALID_EVIDENCE, EvidenceClass.CONTROL)


@pytest.mark.spec("D-30")
def test_unknown_status_is_not_active_and_changes_nothing_by_itself() -> None:
    a = run(ok())
    assert a.outcome is SyncOutcome.UNCHANGED
    # UNKNOWN -> ACTIVE (a mapping added later) is a non-critical status change only.
    b = run(ok(header(status="ACTIVE", native_status="mock-a")))
    assert b.outcome is SyncOutcome.NON_CRITICAL_CHANGE
    assert decide(PprState.CONFIRMED_LOCKED, b).state_after is PprState.CONFIRMED_LOCKED


# ------------------------------------------------------------------ classification
@pytest.mark.spec("D-22", "D-29")
def test_identity_change_ranks_above_critical() -> None:
    a = run(ok(header(department_id="D2")))
    assert a.outcome is SyncOutcome.IDENTITY_CHANGE


@pytest.mark.spec("D-29", "AT-40.7.1", "AT-40.7.2", "AT-40.7.3")
@pytest.mark.parametrize(
    "items",
    [
        [item(1, qty="2", amount="200.00"), CONFIRMED_I[1]],  # qty + amount
        [item(1, item_id="I9"), CONFIRMED_I[1]],  # item
    ],
)
def test_critical_line_changes(items: list[dict[str, Any]]) -> None:
    total = sum(float(i["amount"]) for i in items)
    a = run(ok(header(total_amount=f"{total:.2f}"), items))
    assert a.outcome is SyncOutcome.CRITICAL_CHANGE


@pytest.mark.spec("D-29", "AT-40.7.4", "AT-40.7.5", "AT-40.7.6")
@pytest.mark.parametrize("over", [{"fund_source_id": "F2"}, {"budget_category_id": "C2"}])
def test_critical_header_changes(over: dict[str, Any]) -> None:
    a = run(ok(header(**over)))
    assert a.outcome in (SyncOutcome.CRITICAL_CHANGE, SyncOutcome.IDENTITY_CHANGE)


@pytest.mark.spec("D-29", "S-16")
def test_non_critical_change_is_not_a_false_positive() -> None:
    a = run(
        ok(header(requester_name="someone else"), [item(1, item_name="renamed"), CONFIRMED_I[1]])
    )
    assert a.outcome is SyncOutcome.NON_CRITICAL_CHANGE
    for state in SYNC_STATES:
        assert decide(state, a).state_after is state


# ------------------------------------------------------------------ state matrix
REVIEW = PprState.PR_CHANGED_REVIEW_REQUIRED


@pytest.mark.spec("D-29", "D-31")
@pytest.mark.parametrize(
    ("assessment", "expected"),
    [
        (
            Assessment(SyncOutcome.CRITICAL_CHANGE),
            {"CONFIRMED_LOCKED": REVIEW, "PROCUREMENT_VERIFIED": REVIEW},
        ),
        (
            Assessment(SyncOutcome.IDENTITY_CHANGE),
            {"CONFIRMED_LOCKED": REVIEW, "PROCUREMENT_VERIFIED": REVIEW},
        ),
        (
            Assessment(SyncOutcome.INVALID_EVIDENCE, EvidenceClass.CONTROL),
            {"CONFIRMED_LOCKED": REVIEW, "PROCUREMENT_VERIFIED": REVIEW},
        ),
        (Assessment(SyncOutcome.INVALID_EVIDENCE, EvidenceClass.BINDING), {}),
        (Assessment(SyncOutcome.INVALID_EVIDENCE, EvidenceClass.STATUS), {}),
        (Assessment(SyncOutcome.NOT_FOUND), {}),
        (Assessment(SyncOutcome.UNAVAILABLE), {}),
        (Assessment(SyncOutcome.NON_CRITICAL_CHANGE), {}),
        (Assessment(SyncOutcome.UNCHANGED), {}),
    ],
)
def test_state_matrix(assessment: Assessment, expected: dict[str, PprState]) -> None:
    for state in SYNC_STATES:
        d = decide(state, assessment)
        assert d.state_after is expected.get(state.value, state), (state, assessment.outcome)
        assert d.release is False  # nothing but cancellation releases


@pytest.mark.spec("D-30", "D-07", "G-4")
def test_cancellation_from_every_open_state_and_never_closed() -> None:
    for state in SYNC_STATES:
        d = decide(state, Assessment(SyncOutcome.CANCELLED))
        assert (d.state_after, d.release) == (PprState.CANCELLED, True)
    for state in (PprState.DRAFT, PprState.CANCELLED, PprState.CLOSED):
        d = decide(state, Assessment(SyncOutcome.CANCELLED))
        assert (d.state_after, d.release) == (state, False)


# ------------------------------------------------------------------ fresh read + items
@pytest.mark.spec("D-29")
def test_one_fresh_read_only_for_explainable_inconsistency() -> None:
    mismatch = run(ok(header(total_amount="1.00"))).issues
    precision = run(
        ok(
            header(total_amount="150.001"),
            [item(1), item(2, unit_price="50", amount="50.001")],
        )
    ).issues
    assert worth_one_fresh_read(mismatch)
    assert not worth_one_fresh_read([i for i in precision if i.code == "PR_VALUE_PRECISION"])


def test_items_needed_only_for_bound_readable_non_cancelled_pr() -> None:
    assert needs_items(LiveRead(header()), PR, "ID-PR-1")
    assert not needs_items(LiveRead(header(status="CANCELLED", native_status="c")), PR, "ID-PR-1")
    assert not needs_items(LiveRead(header(pr_id="X")), PR, "ID-PR-1")
    assert not needs_items(LiveRead(None), PR, "ID-PR-1")


# ------------------------------------------------------------------ governance re-check
@pytest.mark.spec("D-30")
@pytest.mark.parametrize(
    ("h", "outcome"),
    [
        (header(status="CANCELLED", native_status="c"), SyncOutcome.CANCELLED),
        (header(status="ACTIVE", native_status="a"), SyncOutcome.ANOMALY),
        (header(), SyncOutcome.NO_AUTHORITATIVE_STATUS),  # UNKNOWN is never an anomaly
        (None, SyncOutcome.NOT_FOUND),
    ],
)
def test_recheck_outcomes(h: dict[str, Any] | None, outcome: SyncOutcome) -> None:
    a = assess_recheck(LiveRead(h), bound_pr_no=PR, confirmed_header=CONFIRMED_H)
    assert a.outcome is outcome
    assert (a.anomaly_code == REACTIVATED_EXTERNALLY) is (outcome is SyncOutcome.ANOMALY)
    assert decide(PprState.CANCELLED, a).state_after is PprState.CANCELLED
