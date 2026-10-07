"""Wave 3: live PR retrieval + Pre-PPR validation, end to end (API -> service -> PostgreSQL).

Spec §11, §12, §13, §22.1, §25.1, §26, §40.1, §40.2, §40.6, §40.13; D-16, D-17, D-18.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text

from api_harness import Harness, Plan, check_pr
from ppr.domain.roles import Role
from ppr.infrastructure.db.schema import metadata
from ppr.integration.hosxp.status import HosxpPrStatus

TONER, PAPER, GLOVE = "MOCK-ITEM-TONER", "MOCK-ITEM-PAPER", "MOCK-ITEM-GLOVE"


@pytest.fixture
def hx(db: Engine) -> Harness:
    return Harness(db)


def _plan(hx: Harness, *, activate: bool = True) -> Plan:
    """FY2570 plan for ENT / fund 1: toner 10 @ 1000.00, paper 10 @ 500.00."""
    p = Plan(hx)
    p.sync()
    p.year()
    b = p.budget("MOCK-CAT-OFFICE", "1500").json()["id"]
    assert p.item(b, "1000").status_code == 201
    assert p.item(b, "500", item_id=PAPER).status_code == 201
    assert p.move("APPROVED").status_code == 200
    if activate:
        assert p.move("ACTIVE").status_code == 200
    return p


@pytest.fixture
def plan(hx: Harness) -> Plan:
    return _plan(hx)


def _requester(hx: Harness, dept: str = "MOCK-DEP-ENT") -> dict[str, str]:
    return hx.h(hx.user(f"req-{dept.lower()}", Role.REQUESTER, department=dept))


def _check(hx: Harness, pr_no: str, headers: dict[str, str]) -> Any:
    return check_pr(hx.client, pr_no, headers)


def _row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            t.name: conn.execute(text(f"SELECT count(*) FROM {t.name}")).scalar_one()
            for t in metadata.sorted_tables
        }


def _codes(items: list[dict[str, Any]]) -> set[str]:
    return {v["code"] for v in items}


# ------------------------------------------------------------------ eligible / all-or-none
@pytest.mark.spec("AT-40.1.1", "AT-40.1.7", "S-12", "S-25.1", "D-16", "D-17")
def test_eligible_pr_is_retrieved_live_and_nothing_is_written(hx: Harness, plan: Plan) -> None:
    h = _requester(hx)
    hx.put_pr("PR-OK", (TONER, "4", "100"), (PAPER, "2", "50"))
    before = _row_counts(hx.engine)
    calls = len(hx.gateway.calls)

    r = _check(hx, "PR-OK", h)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["found"] and body["eligible"] and body["lines_checked"]
    assert body["required_amount"] == "500.00"
    assert body["header"]["header_total"] == "500.00"
    assert body["header"]["budget_year"] == 2570 and body["current_fiscal_year"] == 2570
    toner = body["lines"][0]
    assert (toner["item_id"], toner["qty"], toner["amount"]) == (TONER, "4", "400.00")
    assert toner["matched_plan_item_id"] is not None and toner["passed"]
    assert (toner["plan_remaining_qty"], toner["plan_remaining_amount"]) == (
        "10.0000",
        "1000.00",
    )
    # Retrieved live (header and ALL items), and pre-validation wrote nothing at all.
    # (check_pr reads the PR twice: once to pick the rows, once with the choices)
    assert hx.gateway.calls[calls:] == ["get_pr", "get_pr_items"] * 2
    assert _row_counts(hx.engine) == before


@pytest.mark.spec("AT-40.1.8", "S-12", "D-16")
def test_one_item_outside_the_plan_makes_the_whole_pr_ineligible(hx: Harness, plan: Plan) -> None:
    h = _requester(hx)
    hx.put_pr("PR-GLOVE", (TONER, "1", "100"), (GLOVE, "1", "10"))
    before = _row_counts(hx.engine)
    body = _check(hx, "PR-GLOVE", h).json()
    assert body["eligible"] is False
    ok, glove = body["lines"]
    assert ok["passed"] and not ok["violations"]
    assert _codes(glove["violations"]) == {"NOT_IN_PLAN"} and not glove["passed"]
    assert _row_counts(hx.engine) == before  # no PPR record, no partial PPR


# ------------------------------------------------------------------ hard control (§13)
@pytest.mark.spec("AT-40.2.1", "AT-40.2.2", "AT-40.2.3", "AT-40.2.4", "AT-40.2.5", "S-13")
@pytest.mark.parametrize(
    ("qty", "price", "expected"),
    [
        ("10", "100", set()),  # exactly the remaining qty and amount -> pass
        ("11", "50", {"QTY_EXCEEDED"}),  # qty exceeds, amount within
        ("5", "250", {"AMOUNT_EXCEEDED"}),  # amount exceeds, qty within
        ("11", "100", {"QTY_EXCEEDED", "AMOUNT_EXCEEDED"}),
    ],
)
def test_quantity_and_amount_are_both_hard_limits(
    hx: Harness, plan: Plan, qty: str, price: str, expected: set[str]
) -> None:
    hx.put_pr("PR-HC", (TONER, qty, price))
    body = _check(hx, "PR-HC", _requester(hx)).json()
    assert _codes(body["lines"][0]["violations"]) == expected
    assert body["eligible"] is (not expected)


@pytest.mark.spec("S-13")
def test_split_lines_on_one_plan_item_are_checked_together(hx: Harness, plan: Plan) -> None:
    hx.put_pr("PR-SPLIT", (TONER, "6", "10"), (TONER, "6", "10"))  # 12 > 10 in total
    body = _check(hx, "PR-SPLIT", _requester(hx)).json()
    assert not body["eligible"]
    assert all("QTY_EXCEEDED" in _codes(ln["violations"]) for ln in body["lines"])


# ------------------------------------------------------------------ D-17
@pytest.mark.spec("D-17")
def test_header_total_that_differs_from_the_items_blocks_the_pr(hx: Harness, plan: Plan) -> None:
    hx.put_pr("PR-TOTAL", (TONER, "4", "100"), total=Decimal("400.01"))
    body = _check(hx, "PR-TOTAL", _requester(hx)).json()
    assert body["eligible"] is False
    assert _codes(body["header_violations"]) == {"PR_TOTAL_MISMATCH"}
    assert body["required_amount"] == "400.00"  # never the header figure


# ------------------------------------------------------------------ D-18 / fiscal year
@pytest.mark.spec("D-18")
def test_pr_with_a_past_budget_year_is_rejected_as_expired(hx: Harness, plan: Plan) -> None:
    hx.put_pr("PR-OLD", (TONER, "1", "100"), budget_year=2569)
    body = _check(hx, "PR-OLD", _requester(hx)).json()
    assert _codes(body["header_violations"]) == {"PLAN_YEAR_EXPIRED"}
    assert body["lines_checked"] is False and body["eligible"] is False


@pytest.mark.spec("D-18")
def test_plan_expires_at_the_end_of_its_fiscal_year_even_if_still_active(
    hx: Harness, plan: Plan
) -> None:
    h = _requester(hx)
    hx.put_pr("PR-EDGE", (TONER, "1", "100"))  # budget year 2570
    hx.today = date(2027, 9, 30)  # last day of FY2570
    assert _check(hx, "PR-EDGE", h).json()["eligible"] is True
    hx.today = date(2027, 10, 1)  # first day of FY2571: the 2570 plan has expired
    body = _check(hx, "PR-EDGE", h).json()
    assert body["current_fiscal_year"] == 2571
    assert _codes(body["header_violations"]) == {"PLAN_YEAR_EXPIRED"}


@pytest.mark.spec("AT-40.1.10", "AT-40.13.1", "AT-40.13.2", "S-7")
def test_only_an_active_plan_year_accepts_prs(hx: Harness) -> None:
    p = _plan(hx, activate=False)  # APPROVED, not yet ACTIVE
    h = _requester(hx)
    hx.put_pr("PR-Y", (TONER, "1", "100"))
    assert _codes(_check(hx, "PR-Y", h).json()["header_violations"]) == {"FISCAL_YEAR_NOT_ACTIVE"}
    assert p.move("ACTIVE").status_code == 200
    assert _check(hx, "PR-Y", h).json()["eligible"] is True
    assert p.move("CLOSED").status_code == 200
    assert _codes(_check(hx, "PR-Y", h).json()["header_violations"]) == {"FISCAL_YEAR_NOT_ACTIVE"}


@pytest.mark.spec("D-18")
def test_no_plan_for_the_current_year_is_reported(hx: Harness) -> None:
    Plan(hx).sync()
    hx.put_pr("PR-NOPLAN", (TONER, "1", "100"))
    body = _check(hx, "PR-NOPLAN", _requester(hx)).json()
    assert _codes(body["header_violations"]) == {"PLAN_YEAR_NOT_FOUND"}


# ------------------------------------------------------------------ PR header rules
@pytest.mark.spec("AT-40.1.2")
def test_unknown_pr_is_reported_not_found(hx: Harness, plan: Plan) -> None:
    r = _check(hx, "PR-NOPE", _requester(hx))
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is False and body["eligible"] is False
    assert _codes(body["header_violations"]) == {"PR_NOT_FOUND"}


@pytest.mark.spec("S-11.1", "S-12")
def test_pr_with_an_unknown_fund_source_is_rejected(hx: Harness, plan: Plan) -> None:
    hx.put_pr("PR-FUND", (TONER, "1", "100"), fund="MOCK-FUND-X")
    body = _check(hx, "PR-FUND", _requester(hx)).json()
    assert "UNKNOWN_FUND_SOURCE" in _codes(body["header_violations"])
    assert _codes(body["lines"][0]["violations"]) == {"NOT_IN_PLAN"}


def test_pr_cancelled_in_hosxp_is_rejected(hx: Harness, plan: Plan) -> None:
    hx.put_pr("PR-CXL", (TONER, "1", "100"), status=HosxpPrStatus.CANCELLED)
    body = _check(hx, "PR-CXL", _requester(hx)).json()
    assert _codes(body["header_violations"]) == {"PR_CANCELLED_IN_HOSXP"}


# ------------------------------------------------------------------ HOSxP downtime (§26)
@pytest.mark.spec("AT-40.6.3", "S-26", "S-25.1")
def test_hosxp_down_refuses_instead_of_using_stale_data(hx: Harness, plan: Plan) -> None:
    h = _requester(hx)
    hx.put_pr("PR-DOWN", (TONER, "1", "100"))
    assert _check(hx, "PR-DOWN", h).json()["eligible"] is True
    before = _row_counts(hx.engine)
    hx.gateway.available = False
    r = _check(hx, "PR-DOWN", h)
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "HOSXP_UNAVAILABLE"
    assert _row_counts(hx.engine) == before
    # Existing functions stay available while HOSxP is down (§26).
    assert hx.client.get("/api/plan-years/2570/items", headers=h).status_code == 200


# ------------------------------------------------------------------ any department's PR (D-41)
@pytest.mark.spec("S-22.1", "S-22.2", "S-22.9", "D-41", "AT-40.15.2")
def test_a_requester_validates_a_pr_of_any_department(hx: Harness, plan: Plan) -> None:
    hx.put_pr("PR-ENT", (TONER, "1", "100"))
    other = _check(hx, "PR-ENT", _requester(hx, "MOCK-DEP-OR"))
    assert other.status_code == 200 and other.json()["eligible"] is True
    assert _check(hx, "PR-ENT", _requester(hx)).status_code == 200
    assert _check(hx, "PR-ENT", plan.h).status_code == 200  # Plan Officer: all departments
