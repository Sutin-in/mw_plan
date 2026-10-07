"""Plan Core rules: envelope per exact category, activation checks, warnings.

Spec §9, §10; D-11, D-12, D-13.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ppr.domain.plan import (
    BudgetLine,
    IssueCode,
    PlanItemFacts,
    PlanType,
    check_activation,
    item_warnings,
)

D = Decimal
F = "MOCK-FUND-1"


def item(
    ref: str,
    amount: int | str,
    *,
    cat: str = "MOCK-CAT-A",
    item_id: str | None = None,
    dept: str = "MOCK-DEP-ENT",
    qty: int | str = 1,
    price: int | str | None = None,
    plan_type: PlanType = PlanType.DEPARTMENT,
    purchaser: str | None = None,
    demands: tuple[int, ...] = (),
    quarters: tuple[int | None, int | None, int | None, int | None] = (None, None, None, None),
) -> PlanItemFacts:
    return PlanItemFacts(
        ref=ref,
        plan_type=plan_type,
        item_id=item_id or f"MOCK-ITEM-{ref}",
        owner_department_id=dept,
        purchasing_department_id=purchaser,
        fund_source_id=F,
        budget_category_id=cat,
        planned_qty=D(qty),
        estimated_unit_price=D(price) if price is not None else D(amount) / D(qty),
        planned_amount=D(amount),
        quarters=tuple(None if q is None else D(q) for q in quarters),  # type: ignore[arg-type]
        demand_qtys=tuple(D(d) for d in demands),
    )


def codes(issues: object) -> set[IssueCode]:
    return {i.code for i in issues}  # type: ignore[attr-defined]


@pytest.mark.spec("S-9.1", "D-11", "D-12")
def test_balanced_envelope_can_activate() -> None:
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(300))], [item("1", 100), item("2", 200)])
    assert r.can_activate
    [line] = r.envelope
    assert (line.approved_amount, line.planned_total, line.difference) == (D(300), D(300), D(0))


@pytest.mark.spec("S-9.1", "D-11")
@pytest.mark.parametrize("planned", [299, 301])
def test_envelope_mismatch_blocks_activation(planned: int) -> None:
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(300))], [item("1", planned)])
    assert not r.can_activate
    assert codes(r.errors) == {IssueCode.ENVELOPE_MISMATCH}


@pytest.mark.spec("D-11")
def test_no_roll_up_from_child_categories() -> None:
    # Parent budget 300 with items only in the child category: the parent is NOT satisfied
    # by the child's items, and the child has no budget line of its own.
    r = check_activation(
        [BudgetLine(F, "MOCK-CAT-PARENT", D(300))], [item("1", 300, cat="MOCK-CAT-CHILD")]
    )
    assert codes(r.errors) == {IssueCode.ENVELOPE_MISMATCH, IssueCode.ITEM_WITHOUT_BUDGET}


def test_each_category_is_checked_independently() -> None:
    r = check_activation(
        [BudgetLine(F, "MOCK-CAT-A", D(100)), BudgetLine(F, "MOCK-CAT-B", D(50))],
        [item("1", 100, cat="MOCK-CAT-A"), item("2", 40, cat="MOCK-CAT-B")],
    )
    assert [line.balanced for line in r.envelope] == [True, False]


def test_empty_plan_cannot_activate() -> None:
    assert codes(check_activation([], []).errors) == {IssueCode.NO_BUDGETS, IssueCode.NO_ITEMS}


@pytest.mark.spec("S-10.1")
def test_same_item_in_different_departments_is_fine() -> None:
    items = [
        item("ent", 50, item_id="MOCK-TONER", dept="MOCK-DEP-ENT"),
        item("or", 30, item_id="MOCK-TONER", dept="MOCK-DEP-OR"),
        item("it", 100, item_id="MOCK-TONER", dept="MOCK-DEP-IT"),
    ]
    assert check_activation([BudgetLine(F, "MOCK-CAT-A", D(180))], items).can_activate


@pytest.mark.spec("D-16")
def test_duplicate_department_item_blocks_activation() -> None:
    items = [item("1", 50, item_id="MOCK-TONER"), item("2", 50, item_id="MOCK-TONER")]
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(100))], items)
    assert codes(r.errors) == {IssueCode.DUPLICATE_DEPARTMENT_ITEM}


@pytest.mark.spec("D-16", "D-04")
def test_same_item_department_and_fund_under_another_category_still_blocks() -> None:
    # Extra funding from another category belongs in PPR category allocation (D-04),
    # never in a second Plan Item for the same item/department/fund.
    items = [
        item("x", 50, item_id="MOCK-TONER", cat="MOCK-CAT-A"),
        item("y", 30, item_id="MOCK-TONER", cat="MOCK-CAT-B"),
    ]
    budgets = [BudgetLine(F, "MOCK-CAT-A", D(50)), BudgetLine(F, "MOCK-CAT-B", D(30))]
    r = check_activation(budgets, items)
    assert codes(r.errors) == {IssueCode.DUPLICATE_DEPARTMENT_ITEM}


@pytest.mark.spec("S-10.3")
def test_assigned_purchase_needs_demand_and_purchaser() -> None:
    bad = item("a", 300, qty=300, plan_type=PlanType.ASSIGNED)
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(300))], [bad])
    assert codes(r.errors) == {
        IssueCode.ASSIGNED_WITHOUT_DEMAND,
        IssueCode.ASSIGNED_WITHOUT_PURCHASER,
    }
    good = item(
        "a",
        300,
        qty=300,
        plan_type=PlanType.ASSIGNED,
        purchaser="MOCK-DEP-STORE",
        demands=(100, 150, 50),
    )
    assert check_activation([BudgetLine(F, "MOCK-CAT-A", D(300))], [good]).can_activate


@pytest.mark.spec("D-13", "S-9.3")
def test_amount_differing_from_qty_times_price_is_only_a_warning() -> None:
    it = item("1", 1000, qty=10, price=90)  # 10 x 90 = 900 != 1000
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(1000))], [it])
    assert r.can_activate
    assert codes(r.warnings) == {IssueCode.AMOUNT_DIFFERS_FROM_ESTIMATE}


@pytest.mark.spec("S-9.4")
def test_quarters_are_monitoring_only() -> None:
    it = item("1", 100, qty=10, price=10, quarters=(1, 1, 1, 1))
    assert codes(item_warnings(it)) == {IssueCode.QUARTERS_DIFFER}
    assert check_activation([BudgetLine(F, "MOCK-CAT-A", D(100))], [it]).can_activate
    ok = item("2", 100, qty=10, price=10, quarters=(3, 3, 2, 2))
    assert item_warnings(ok) == ()


def test_assigned_demand_total_mismatch_is_a_warning() -> None:
    it = item(
        "a",
        300,
        qty=300,
        plan_type=PlanType.ASSIGNED,
        purchaser="MOCK-DEP-STORE",
        demands=(100, 100),
    )
    assert codes(item_warnings(it)) == {IssueCode.DEMAND_TOTAL_DIFFERS}


_amt = st.integers(min_value=0, max_value=10**7)


@pytest.mark.spec("S-9.1", "D-11")
@given(amounts=st.lists(_amt, min_size=1, max_size=15), extra=st.integers(-1000, 1000))
def test_property_envelope_balanced_iff_sum_equals_budget(amounts: list[int], extra: int) -> None:
    items = [item(str(i), a) for i, a in enumerate(amounts)]
    budget = D(sum(amounts) + extra)
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", budget)], items)
    assert r.envelope[0].balanced == (extra == 0)
    assert r.can_activate == (extra == 0)


# ------------------------------------------------------------------ Central Pool (D-38, G-1)
STORE = "MOCK-DEP-STORE"


@pytest.mark.spec("D-38", "G-1")
def test_central_item_needs_its_purchasing_department() -> None:
    items = [item("c", 100, plan_type=PlanType.CENTRAL, dept=STORE)]
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(100))], items)
    assert codes(r.errors) == {IssueCode.CENTRAL_WITHOUT_PURCHASER}


@pytest.mark.spec("D-38", "G-1")
def test_central_item_of_the_store_and_department_items_coexist() -> None:
    items = [
        item("ent", 50, item_id="MOCK-TONER"),
        item(
            "c", 100, item_id="MOCK-TONER", plan_type=PlanType.CENTRAL, dept=STORE, purchaser=STORE
        ),
    ]
    assert check_activation([BudgetLine(F, "MOCK-CAT-A", D(150))], items).can_activate


@pytest.mark.spec("D-38", "G-1", "D-16")
@pytest.mark.parametrize(
    "second",
    [
        # a CENTRAL item bought by ENT next to ENT's own DEPARTMENT item
        {"plan_type": PlanType.CENTRAL, "dept": STORE, "purchaser": "MOCK-DEP-ENT"},
        # two CENTRAL items bought by the same store
        {"plan_type": PlanType.CENTRAL, "dept": "MOCK-DEP-OR", "purchaser": "MOCK-DEP-ENT"},
    ],
)
def test_items_a_pr_line_could_not_tell_apart_block_activation(second: dict[str, object]) -> None:
    first = (
        item("a", 50, item_id="MOCK-TONER")
        if second["dept"] == STORE
        else item(
            "a",
            50,
            item_id="MOCK-TONER",
            plan_type=PlanType.CENTRAL,
            dept=STORE,
            purchaser="MOCK-DEP-ENT",
        )
    )
    items = [first, item("b", 50, item_id="MOCK-TONER", **second)]  # type: ignore[arg-type]
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(100))], items)
    assert IssueCode.DUPLICATE_MATCH_KEY in codes(r.errors)


@pytest.mark.spec("D-38", "G-3", "D-16")
def test_an_assigned_item_collides_with_its_purchasers_own_item() -> None:
    # ENT buys an ASSIGNED toner for others and also plans its own toner: a PR line of ENT
    # could be either, so activation is refused (D-38 C-2 applies to ASSIGNED items too).
    items = [
        item("a", 50, item_id="MOCK-TONER"),
        item(
            "b",
            50,
            item_id="MOCK-TONER",
            plan_type=PlanType.ASSIGNED,
            dept="MOCK-DEP-ENT",
            purchaser="MOCK-DEP-ENT",
            demands=(1,),
        ),
    ]
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(100))], items)
    assert IssueCode.DUPLICATE_MATCH_KEY in codes(r.errors)
    # Bought by the store instead, the two never meet.
    items[1] = item(
        "b",
        50,
        item_id="MOCK-TONER",
        plan_type=PlanType.ASSIGNED,
        dept=STORE,
        purchaser=STORE,
        demands=(1,),
    )
    r = check_activation([BudgetLine(F, "MOCK-CAT-A", D(100))], items)
    assert IssueCode.DUPLICATE_MATCH_KEY not in codes(r.errors)


@pytest.mark.spec("D-38", "G-3")
def test_one_demand_is_claimed_by_one_assigned_purchase_only() -> None:
    from dataclasses import replace

    from ppr.domain.plan import demand_claim_issues

    x = replace(
        item(
            "x",
            50,
            item_id="MOCK-TONER",
            plan_type=PlanType.ASSIGNED,
            dept=STORE,
            purchaser=STORE,
            demands=(1,),
        ),
        demand_departments=("MOCK-DEP-WARD",),
    )
    y = replace(x, ref="y", purchasing_department_id="MOCK-DEP-OR")
    (issue,) = demand_claim_issues([x, y])  # the ward's need, two purchase lines
    assert issue.code is IssueCode.DUPLICATE_DEMAND_CLAIM and issue.subject == "x, y"
    assert IssueCode.DUPLICATE_DEMAND_CLAIM in codes(
        check_activation([BudgetLine(F, "MOCK-CAT-A", D(100))], [x, y]).errors
    )
    assert demand_claim_issues([x, replace(y, demand_departments=("MOCK-DEP-IT",))]) == []
    assert demand_claim_issues([x, replace(y, fund_source_id="MOCK-FUND-2")]) == []
    assert demand_claim_issues([x, replace(y, demand_departments=())]) == []  # cancelled (0)
