"""Pre-PPR validation and the all-or-none rule (spec §11.4, §12, §40.1)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from builders import UNIT, D, check, ctx, line, plan_item, pr
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.pr_binding import PrBinding
from ppr.domain.prevalidation import (
    PrevalidationFailedError,
    PrLine,
    check_lines_originate_from_hosxp,
    prevalidate,
)
from ppr.domain.violations import ViolationCode, codes


@pytest.mark.spec("AT-40.1.7", "S-12")
def test_all_items_in_plan_is_eligible() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 2, 200), line("L2", "MOCK-ITEM-B", 1, 50)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), plan_item("P-B", "MOCK-ITEM-B", 5, 500)),
        pr_no="MOCK-PR-1",
    )
    assert r.eligible
    assert [x.matched_plan_item_id for x in r.lines] == ["P-A", "P-B"]
    r.ensure_eligible()  # does not raise


@pytest.mark.spec("AT-40.1.8", "S-12")
def test_one_item_missing_from_plan_makes_whole_pr_ineligible() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 2, 200), line("L2", "MOCK-ITEM-X", 1, 50, unit="ลิตร")),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert not r.eligible
    # The UI can still explain every line: L1 passed, L2 failed and why.
    assert r.lines[0].passed
    assert codes(r.lines[1].violations) == {ViolationCode.NOT_IN_PLAN}
    with pytest.raises(PrevalidationFailedError):
        r.ensure_eligible()


@pytest.mark.spec("AT-40.1.2")
def test_pr_not_found_is_ineligible() -> None:
    r = check(None, ctx(), pr_no="MOCK-PR-404")
    assert not r.eligible
    assert codes(r.header_violations) == {ViolationCode.PR_NOT_FOUND}


@pytest.mark.spec("AT-40.1.3", "AT-40.1.4", "D-03")
def test_pr_bound_to_confirmed_ppr_is_rejected_even_if_that_ppr_was_cancelled() -> None:
    # BOUND is permanent: it covers both an active and a cancelled confirmed PPR.
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 1)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), binding=PrBinding.BOUND),
        pr_no="MOCK-PR-1",
    )
    assert not r.eligible
    assert codes(r.header_violations) == {ViolationCode.PR_ALREADY_BOUND}


@pytest.mark.spec("AT-40.1.6", "D-05")
def test_second_working_draft_is_rejected() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 1)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), binding=PrBinding.DRAFT_IN_PROGRESS),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.DRAFT_ALREADY_EXISTS}


@pytest.mark.spec("AT-40.1.10", "AT-40.13.2", "S-7")
@pytest.mark.parametrize(
    "state", [PlanYearState.CLOSED, PlanYearState.DRAFT, PlanYearState.APPROVED]
)
def test_non_active_fiscal_year_rejects_new_ppr(state: PlanYearState) -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 1)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), state=state),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.FISCAL_YEAR_NOT_ACTIVE}


@pytest.mark.spec("AT-40.13.1")
def test_active_fiscal_year_allows_new_ppr() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 1)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), state=PlanYearState.ACTIVE),
        pr_no="MOCK-PR-1",
    )
    assert r.eligible


@pytest.mark.spec("D-08", "D-18")
def test_pr_without_budget_year_is_rejected_not_guessed() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 1), fiscal_year=None),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.FISCAL_YEAR_UNKNOWN}


@pytest.mark.spec("D-18", "S-7")
def test_missing_plan_for_the_current_year_is_rejected() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 1)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), state=None),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.PLAN_YEAR_NOT_FOUND}


def test_unknown_fund_source_is_rejected() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 1), fund="MOCK-FUND-X"),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000, fund="MOCK-FUND-X")),
        pr_no="MOCK-PR-1",
    )
    assert ViolationCode.UNKNOWN_FUND_SOURCE in codes(r.header_violations)


def test_pr_with_no_items_is_rejected() -> None:
    r = check(pr(), ctx(), pr_no="MOCK-PR-1")
    assert codes(r.header_violations) == {ViolationCode.PR_HAS_NO_ITEMS}


@pytest.mark.spec("S-10.1")
def test_same_item_in_another_departments_plan_does_not_match() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-TONER", 1, 100), dept="MOCK-DEP-ENT"),
        ctx(plan_item("P-OR", "MOCK-ITEM-TONER", 10, 1000, dept="MOCK-DEP-OR")),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.lines[0].violations) == {ViolationCode.NOT_IN_PLAN}


def test_plan_item_under_a_different_fund_source_does_not_match() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100)),
        ctx(
            plan_item("P-A", "MOCK-ITEM-A", 10, 1000, fund="MOCK-FUND-2"),
            funds=frozenset({"MOCK-FUND-1", "MOCK-FUND-2"}),
        ),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.lines[0].violations) == {ViolationCode.NOT_IN_PLAN}


@pytest.mark.spec("D-43", "AT-40.18.1")
def test_two_rows_of_the_same_item_are_never_chosen_automatically() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100)),
        ctx(plan_item("P-1", "MOCK-ITEM-A", 10, 1000), plan_item("P-2", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.lines[0].violations) == {ViolationCode.PLAN_ROW_NOT_CHOSEN}
    assert r.lines[0].offered == ("P-1", "P-2")


@pytest.mark.spec("S-13")
def test_split_lines_on_same_plan_item_are_checked_in_aggregate() -> None:
    # Each line alone fits (6 <= 10) but together (12) they exceed the plan item.
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 6, 100), line("L2", "MOCK-ITEM-A", 6, 100)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert not r.eligible
    assert all(ViolationCode.QTY_EXCEEDED in codes(x.violations) for x in r.lines)


@pytest.mark.spec("S-13")
def test_line_over_remaining_amount_makes_pr_ineligible() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100), line("L2", "MOCK-ITEM-B", 1, 999)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), plan_item("P-B", "MOCK-ITEM-B", 10, 500)),
        pr_no="MOCK-PR-1",
    )
    assert not r.eligible
    assert codes(r.lines[1].violations) == {ViolationCode.AMOUNT_EXCEEDED}


def test_duplicate_pr_item_id_is_rejected() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 10), line("L1", "MOCK-ITEM-A", 1, 10)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert ViolationCode.DUPLICATE_PR_ITEM in codes(r.lines[1].violations)


def test_header_failures_still_report_every_line_for_the_ui() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 10), line("L2", "MOCK-ITEM-Z", 1, 10, unit="ลิตร")),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), binding=PrBinding.BOUND),
        pr_no="MOCK-PR-1",
    )
    assert len(r.lines) == 2
    assert r.lines_checked
    assert codes(r.all_violations()) == {
        ViolationCode.PR_ALREADY_BOUND,
        ViolationCode.NOT_IN_PLAN,
    }


@pytest.mark.spec("D-18", "AT-40.1.10")
def test_unusable_plan_year_lists_lines_without_judging_them() -> None:
    # A CLOSED/expired plan cannot be used, so lines are listed but not matched against it.
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 10), line("L2", "MOCK-ITEM-Z", 1, 10, unit="ลิตร")),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), state=PlanYearState.CLOSED),
        pr_no="MOCK-PR-1",
    )
    assert len(r.lines) == 2
    assert not r.lines_checked and not r.eligible
    assert codes(r.all_violations()) == {ViolationCode.FISCAL_YEAR_NOT_ACTIVE}


@pytest.mark.spec("AT-40.1.9", "S-11.4")
def test_manually_added_pr_item_is_rejected() -> None:
    vs = check_lines_originate_from_hosxp(["L1", "L2", "MANUAL-1"], ["L1", "L2"])
    assert codes(vs) == {ViolationCode.MANUAL_PR_ITEM}
    assert vs[0].subject == "MANUAL-1"
    assert check_lines_originate_from_hosxp(["L1"], ["L1", "L2"]) == ()


# ------------------------------------------------------------------ D-17 required amount
@pytest.mark.spec("D-17", "S-17")
def test_required_amount_is_the_sum_of_the_pr_lines() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 2, "200.50"), line("L2", "MOCK-ITEM-B", 1, "49.50")),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), plan_item("P-B", "MOCK-ITEM-B", 5, 500)),
        pr_no="MOCK-PR-1",
    )
    assert r.eligible
    assert r.required_amount == Decimal("250.00")


@pytest.mark.spec("D-17")
@pytest.mark.parametrize("total", ["250.01", "249.99", "0"])
def test_header_total_that_differs_from_the_lines_blocks_the_ppr(total: str) -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 2, 200), line("L2", "MOCK-ITEM-B", 1, 50), total=D(total)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), plan_item("P-B", "MOCK-ITEM-B", 5, 500)),
        pr_no="MOCK-PR-1",
    )
    assert not r.eligible
    assert codes(r.header_violations) == {ViolationCode.PR_TOTAL_MISMATCH}
    assert all(line.passed for line in r.lines)  # the lines themselves are fine


@pytest.mark.spec("D-17")
def test_missing_header_total_fails_closed() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100), total=None),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.PR_TOTAL_UNAVAILABLE}


# ------------------------------------------------------------------ D-18 fiscal year
@pytest.mark.spec("D-18", "AT-40.13.2")
def test_pr_from_a_past_budget_year_is_rejected_because_that_plan_expired() -> None:
    # The real example: budget year 2568 on a PR requested in fiscal year 2569.
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100), fiscal_year=2568),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), current_fiscal_year=2569),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.PLAN_YEAR_EXPIRED}
    assert not r.eligible
    assert r.lines_checked is False  # not judged against an expired plan
    assert all(ln.matched_plan_item_id is None and not ln.violations for ln in r.lines)


@pytest.mark.spec("D-18")
def test_pr_for_a_future_budget_year_is_rejected() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100), fiscal_year=2571),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000), current_fiscal_year=2570),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.FISCAL_YEAR_NOT_CURRENT}


# ------------------------------------------------------------------ PR header integrity
@pytest.mark.spec("S-12")
def test_pr_without_department_or_fund_source_is_rejected() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100), dept=None, fund=None),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {
        ViolationCode.PR_MISSING_DEPARTMENT,
        ViolationCode.UNKNOWN_FUND_SOURCE,
    }
    assert codes(r.lines[0].violations) == {ViolationCode.NOT_IN_PLAN}


def test_pr_cancelled_in_hosxp_is_rejected() -> None:
    r = check(
        pr(line("L1", "MOCK-ITEM-A", 1, 100), cancelled=True),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.header_violations) == {ViolationCode.PR_CANCELLED_IN_HOSXP}


@pytest.mark.spec("D-23")
@pytest.mark.parametrize(
    ("qty", "amount", "price", "what"),
    [
        ("1.00005", "100", "100", "quantity"),
        ("1", "100.005", "100", "amount"),
        ("1", "100", "100.000000001", "unit price"),
    ],
)
def test_values_beyond_supported_precision_are_refused_not_rounded(
    qty: str, amount: str, price: str, what: str
) -> None:
    r = check(
        pr(PrLine("L1", "MOCK-ITEM-A", D(qty), D(amount), D(price))),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    [v] = [v for v in r.lines[0].violations if v.code is ViolationCode.PR_VALUE_PRECISION]
    assert v.subject == "L1" and "PR item L1" in v.message and what in v.message
    assert not r.eligible


@pytest.mark.spec("D-23")
def test_trailing_zero_decimals_and_eight_decimal_prices_are_fine() -> None:
    r = check(
        pr(PrLine("L1", "MOCK-ITEM-A", D("2.00000000"), D("100.00000000"), D("50.12345678"), UNIT)),
        ctx(plan_item("P-A", "MOCK-ITEM-A", 10, 1000)),
        pr_no="MOCK-PR-1",
    )
    assert r.eligible


# ---------------------------------------------------------------- D-43 (Wave 12A-2)
@pytest.mark.spec("D-43", "AT-40.18.1")
def test_rows_are_offered_by_department_fund_and_unit_never_by_item() -> None:
    rows = (
        plan_item("P-BOX", "MOCK-ITEM-X", 10, 1000, unit="กล่อง"),
        plan_item("P-NOCODE", None, 10, 1000, unit="กล่อง"),
        plan_item("P-PIECE", "MOCK-ITEM-A", 10, 1000, unit="ชิ้น"),
        plan_item("P-OR", None, 10, 1000, unit="กล่อง", dept="MOCK-DEP-OR"),
    )
    r = prevalidate(
        pr(line("L1", "MOCK-ITEM-A", 1, 100, unit=" กล่อง ")),
        ctx(*rows),
        pr_no="MOCK-PR-1",
    )
    assert r.lines[0].offered == ("P-BOX", "P-NOCODE")
    assert codes(r.lines[0].violations) == {ViolationCode.PLAN_ROW_NOT_CHOSEN}
    assert not r.eligible


@pytest.mark.spec("D-43", "AT-40.18.1")
def test_a_chosen_row_passes_and_carries_the_hard_control() -> None:
    rows = (plan_item("P-NOCODE", None, 10, 1000),)
    ok = prevalidate(
        pr(line("L1", "MOCK-ITEM-A", 2, 200)),
        ctx(*rows, choices={"L1": "P-NOCODE"}),
        pr_no="MOCK-PR-1",
    )
    assert ok.eligible and ok.lines[0].matched_plan_item_id == "P-NOCODE"
    over = prevalidate(
        pr(line("L1", "MOCK-ITEM-A", 11, 200)),
        ctx(*rows, choices={"L1": "P-NOCODE"}),
        pr_no="MOCK-PR-1",
    )
    assert codes(over.lines[0].violations) == {ViolationCode.QTY_EXCEEDED}


@pytest.mark.spec("D-43", "AT-40.18.1")
@pytest.mark.parametrize("chosen", ["P-OTHER-UNIT", "P-OTHER-DEPT", "P-UNKNOWN"])
def test_a_row_outside_those_offered_is_refused(chosen: str) -> None:
    rows = (
        plan_item("P-OK", None, 10, 1000),
        plan_item("P-OTHER-UNIT", None, 10, 1000, unit="ลิตร"),
        plan_item("P-OTHER-DEPT", None, 10, 1000, dept="MOCK-DEP-OR"),
    )
    r = prevalidate(
        pr(line("L1", "MOCK-ITEM-A", 1, 100)),
        ctx(*rows, choices={"L1": chosen}),
        pr_no="MOCK-PR-1",
    )
    assert codes(r.lines[0].violations) == {ViolationCode.PLAN_ROW_NOT_ALLOWED}
    assert r.lines[0].matched_plan_item_id is None


@pytest.mark.spec("D-43", "AT-40.18.1")
def test_a_line_whose_unit_is_unknown_is_offered_nothing() -> None:
    r = prevalidate(
        pr(line("L1", "MOCK-ITEM-A", 1, 100, unit=None)),
        ctx(plan_item("P-OK", "MOCK-ITEM-A", 10, 1000), choices={"L1": "P-OK"}),
        pr_no="MOCK-PR-1",
    )
    # nothing offered, and the choice given is not kept (never usable blind)
    assert codes(r.lines[0].violations) == {
        ViolationCode.NOT_IN_PLAN,
        ViolationCode.PLAN_ROW_NOT_ALLOWED,
    }
    assert r.lines[0].offered == ()


@pytest.mark.spec("D-43", "AT-40.18.2", "S-13")
def test_lines_of_different_items_choosing_one_row_are_checked_in_aggregate() -> None:
    r = prevalidate(
        pr(line("L1", "MOCK-ITEM-A", 6, 600), line("L2", "MOCK-ITEM-B", 6, 600)),
        ctx(
            plan_item("P-ROW", None, 10, 1000),
            choices={"L1": "P-ROW", "L2": "P-ROW"},
        ),
        pr_no="MOCK-PR-1",
    )
    assert not r.eligible
    assert all(ViolationCode.QTY_EXCEEDED in codes(x.violations) for x in r.lines)
