"""Plan Amendment domain rules (D-37 7A, spec §20)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D  # noqa: N817

import pytest

from ppr.domain.amendment import (
    AmendmentIssueCode as C,
)
from ppr.domain.amendment import (
    CurrentItem,
    ItemChange,
    ItemValues,
    NewItem,
    ledger_entries,
    plan_amendment,
)
from ppr.domain.ledger import LedgerEntry, LedgerEventType, PlanItemLedger
from ppr.domain.plan import BudgetLine, PlanType


def vals(**kw: object) -> ItemValues:
    base: dict[str, object] = dict(
        plan_type=PlanType.DEPARTMENT,
        item_id="I1",
        owner_department_id="ENT",
        purchasing_department_id=None,
        fund_source_id="F1",
        budget_category_id="C1",
        planned_qty=D("10"),
        estimated_unit_price=D("100"),
        planned_amount=D("1000"),
    )
    base.update(kw)
    return ItemValues(**base)  # type: ignore[arg-type]


def item(pid: int, used_qty: str = "0", used_amount: str = "0", **kw: object) -> CurrentItem:
    return CurrentItem(pid, vals(**kw), D(used_qty), D(used_amount))


def line(fund: str, cat: str, amount: str) -> BudgetLine:
    return BudgetLine(fund, cat, D(amount))


# Plan year used by most tests: fund F1 has categories C1 (1000 + 500) and C2 (2000).
BUDGETS = [line("F1", "C1", "1500"), line("F1", "C2", "2000")]
ITEMS = [
    item(1, used_qty="4", used_amount="400"),
    item(2, item_id="I2", planned_qty=D("5"), planned_amount=D("500")),
    item(3, item_id="I3", budget_category_id="C2", planned_qty=D("20"), planned_amount=D("2000")),
]


def codes(plan) -> set[C]:  # type: ignore[no-untyped-def]
    return {e.code for e in plan.errors}


def change(pid: int, **kw: object) -> ItemChange:
    cur = {c.plan_item_id: c for c in ITEMS}[pid]
    return ItemChange(pid, replace(cur.values, **kw))  # type: ignore[arg-type]


# ------------------------------------------------------------------ accepted amendments
@pytest.mark.spec("D-37", "S-20")
def test_transfer_between_items_of_same_category_is_accepted() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [],
        [
            change(1, planned_qty=D("8"), planned_amount=D("800")),
            change(2, planned_qty=D("7"), planned_amount=D("700")),
        ],
        [],
    )
    assert p.ok, p.errors
    assert [d.plan_item_id for d in p.item_diffs] == [1, 2]
    assert p.item_diffs[0].qty_delta == D("-2") and p.item_diffs[0].amount_delta == D("-200")
    assert p.item_diffs[1].qty_delta == D("2") and p.item_diffs[1].amount_delta == D("200")
    assert p.item_diffs[0].before == ITEMS[0].values
    assert p.budget_diffs == () and p.new_items == ()


def test_transfer_between_categories_of_same_fund_changes_both_lines() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C1", "1300"), line("F1", "C2", "2200")],
        [
            change(2, planned_qty=D("3"), planned_amount=D("300")),
            change(3, planned_qty=D("22"), planned_amount=D("2200")),
        ],
        [],
    )
    assert p.ok, p.errors
    assert [(b.budget_category_id, b.before, b.after) for b in p.budget_diffs] == [
        ("C1", D("1500"), D("1300")),
        ("C2", D("2000"), D("2200")),
    ]


@pytest.mark.spec("AT-40.11.1", "AT-40.11.4", "D-37")
def test_increase_goes_to_a_new_item_in_the_same_amendment() -> None:
    new = NewItem("n1", vals(item_id="I9", planned_qty=D("2"), planned_amount=D("250")))
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1750")], [], [new])
    assert p.ok, p.errors
    assert p.new_items == (new,)
    assert p.budget_diffs[0].before == D("1500") and p.budget_diffs[0].after == D("1750")


@pytest.mark.spec("D-37", "D-11")
def test_new_budget_line_with_its_items() -> None:
    new = NewItem(
        "n1",
        vals(
            item_id="I9",
            fund_source_id="F2",
            budget_category_id="C9",
            planned_qty=D("1"),
            planned_amount=D("90"),
            estimated_unit_price=D("90"),
        ),
    )
    p = plan_amendment(BUDGETS, ITEMS, [line("F2", "C9", "90")], [], [new])
    assert p.ok, p.errors
    assert p.budget_diffs[0].before is None and p.budget_diffs[0].after == D("90")


def test_new_central_item_is_allowed() -> None:
    new = NewItem(
        "c",
        vals(
            plan_type=PlanType.CENTRAL,
            item_id="I8",
            purchasing_department_id="STORE",
            planned_qty=D("1"),
            planned_amount=D("100"),
        ),
    )
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [new])
    assert p.ok, p.errors


@pytest.mark.spec("D-37")
def test_unused_item_may_be_reduced_to_zero() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C1", "1000")],
        [change(2, planned_qty=D("0"), planned_amount=D("0"))],
        [],
    )
    assert p.ok, p.errors


def test_used_item_may_be_reduced_exactly_to_used() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C1", "900")],
        [change(1, planned_qty=D("4"), planned_amount=D("400"))],
        [],
    )
    assert p.ok, p.errors


def test_unused_item_may_change_its_data() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [],
        [change(2, item_id="I7", owner_department_id="OR", purchasing_department_id="STORE")],
        [],
    )
    assert p.ok, p.errors
    assert p.item_diffs[0].qty_delta == 0 and p.item_diffs[0].amount_delta == 0


def test_unused_item_may_move_to_another_category_of_the_same_fund() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C1", "1000"), line("F1", "C2", "2500")],
        [change(2, budget_category_id="C2")],
        [],
    )
    assert p.ok, p.errors


def test_price_only_change_is_a_diff_without_ledger_delta() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(2, estimated_unit_price=D("90"))], [])
    assert p.ok, p.errors
    assert len(p.item_diffs) == 1
    warn = {w.code for w in p.warnings}
    assert C.AMOUNT_DIFFERS_FROM_ESTIMATE in warn  # 5 x 90 != 500


def test_assigned_item_numbers_may_be_amended() -> None:
    items = [
        *ITEMS,
        item(
            4,
            plan_type=PlanType.ASSIGNED,
            item_id="I4",
            purchasing_department_id="STORE",
            budget_category_id="C3",
            planned_qty=D("3"),
            planned_amount=D("30"),
        ),
    ]
    budgets = [*BUDGETS, line("F1", "C3", "30")]
    cur = items[-1].values
    p = plan_amendment(
        budgets,
        items,
        [line("F1", "C3", "40")],
        [ItemChange(4, replace(cur, planned_qty=D("4"), planned_amount=D("40")))],
        [],
    )
    assert p.ok, p.errors


# ------------------------------------------------------------------ refused amendments
def test_empty_amendment_is_refused() -> None:
    assert codes(plan_amendment(BUDGETS, ITEMS, [], [], [])) == {C.EMPTY_AMENDMENT}


def test_no_op_changes_are_ignored_and_refused_as_empty() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1500")], [change(2)], [])
    assert codes(p) == {C.EMPTY_AMENDMENT}
    assert p.item_diffs == () and p.budget_diffs == ()


def test_unknown_item() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [ItemChange(99, vals())], [])
    assert C.UNKNOWN_PLAN_ITEM in codes(p)


def test_same_item_twice() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [],
        [change(2, planned_qty=D("6")), change(2, planned_qty=D("7"))],
        [],
    )
    assert C.DUPLICATE_CHANGE in codes(p)


def test_same_budget_line_twice() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600"), line("F1", "C1", "1700")], [], [])
    assert C.DUPLICATE_CHANGE in codes(p)


def test_same_new_ref_twice() -> None:
    n = NewItem("n", vals(item_id="I9", planned_qty=D("1"), planned_amount=D("50")))
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [n, n])
    assert C.DUPLICATE_CHANGE in codes(p)


@pytest.mark.spec("D-37")
def test_fund_source_never_changes() -> None:
    p = plan_amendment(
        [*BUDGETS, line("F2", "C1", "0")],
        ITEMS,
        [line("F1", "C1", "1000"), line("F2", "C1", "500")],
        [change(2, fund_source_id="F2")],
        [],
    )
    assert C.FUND_SOURCE_CHANGED in codes(p)


def test_plan_type_never_changes() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [],
        [change(2, plan_type=PlanType.CENTRAL, purchasing_department_id="STORE")],
        [],
    )
    assert C.PLAN_TYPE_CHANGED in codes(p)


@pytest.mark.parametrize(
    "kw",
    [
        {"item_id": "I7"},
        {"owner_department_id": "OR"},
        {"purchasing_department_id": "STORE"},
    ],
)
@pytest.mark.spec("D-37")
def test_used_item_data_cannot_change(kw: dict[str, str]) -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(1, **kw)], [])
    assert C.DATA_CHANGE_AFTER_USE in codes(p)


def test_used_item_cannot_move_category() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C1", "500"), line("F1", "C2", "3000")],
        [change(1, budget_category_id="C2")],
        [],
    )
    assert C.DATA_CHANGE_AFTER_USE in codes(p)


def test_item_with_only_used_amount_counts_as_used() -> None:
    items = [item(1, used_qty="0", used_amount="1"), *ITEMS[1:]]
    p = plan_amendment(BUDGETS, items, [], [change(1, item_id="I7")], [])
    assert C.DATA_CHANGE_AFTER_USE in codes(p)


@pytest.mark.spec("AT-40.11.2", "S-20", "D-37")
def test_below_used_qty() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(1, planned_qty=D("3"))], [])
    assert C.BELOW_USED_QTY in codes(p)


@pytest.mark.spec("AT-40.11.3", "S-20", "D-37")
def test_below_used_amount() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C1", "899")],
        [change(1, planned_amount=D("399"))],
        [],
    )
    assert C.BELOW_USED_AMOUNT in codes(p)


@pytest.mark.parametrize(
    "kw",
    [
        {"planned_qty": D("-1")},
        {"estimated_unit_price": D("-1")},
        {"planned_amount": D("-1")},
    ],
)
def test_negative_values(kw: dict[str, D]) -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(2, **kw)], [])
    assert C.NEGATIVE_VALUE in codes(p)


def test_zero_qty_needs_zero_amount() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(2, planned_qty=D("0"))], [])
    assert C.ZERO_QTY_WITH_AMOUNT in codes(p)


def test_new_item_needs_positive_qty() -> None:
    n = NewItem("n", vals(item_id="I9", planned_qty=D("0"), planned_amount=D("0")))
    p = plan_amendment(BUDGETS, ITEMS, [], [], [n])
    assert C.NEW_ITEM_ZERO_QTY in codes(p)


@pytest.mark.spec("D-38", "G-3")
def test_new_assigned_item_needs_its_demand() -> None:
    v = vals(
        plan_type=PlanType.ASSIGNED,
        item_id="I9",
        purchasing_department_id="STORE",
        planned_qty=D(3),
        planned_amount=D(100),
    )
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [NewItem("n", v)])
    assert C.ASSIGNED_WITHOUT_DEMAND in codes(p)
    ok = NewItem("n", v, (("ENT", D(1)), ("OR", D(2))))
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [ok])
    assert p.ok, p.errors
    odd = NewItem("n", v, (("ENT", D(1)),))
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [odd])
    assert p.ok and C.DEMAND_TOTAL_DIFFERS in {w.code for w in p.warnings}


def test_assigned_item_keeps_its_purchaser() -> None:
    items = [
        *ITEMS,
        item(
            4,
            plan_type=PlanType.ASSIGNED,
            item_id="I4",
            purchasing_department_id="STORE",
            budget_category_id="C3",
            planned_qty=D("3"),
            planned_amount=D("30"),
        ),
    ]
    budgets = [*BUDGETS, line("F1", "C3", "30")]
    p = plan_amendment(
        budgets,
        items,
        [],
        [ItemChange(4, replace(items[-1].values, purchasing_department_id=None))],
        [],
    )
    assert C.ASSIGNED_WITHOUT_PURCHASER in codes(p)


def test_negative_budget() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "-1")], [], [])
    assert C.NEGATIVE_BUDGET in codes(p)


def test_item_without_budget_line() -> None:
    n = NewItem(
        "n", vals(item_id="I9", budget_category_id="C9", planned_qty=D("1"), planned_amount=D("10"))
    )
    p = plan_amendment(BUDGETS, ITEMS, [], [], [n])
    assert C.ITEM_WITHOUT_BUDGET in codes(p)


@pytest.mark.spec("D-11", "D-37")
def test_unbalanced_line_after_item_increase() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(2, planned_amount=D("600"))], [])
    errs = [e for e in p.errors if e.code is C.ENVELOPE_MISMATCH]
    assert errs and errs[0].subject == "F1/C1"


def test_unbalanced_line_after_budget_change_only() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C2", "2100")], [], [])
    assert C.ENVELOPE_MISMATCH in codes(p)


def test_unbalanced_new_line() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [line("F2", "C9", "90")], [], [])
    assert C.ENVELOPE_MISMATCH in codes(p)


def test_duplicate_department_item_after_amendment() -> None:
    n = NewItem("n", vals(item_id="I2", planned_qty=D("1"), planned_amount=D("100")))
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [n])
    assert C.DUPLICATE_DEPARTMENT_ITEM in codes(p)


def test_duplicate_created_by_changing_item_id() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(2, item_id="I1")], [])
    assert C.DUPLICATE_DEPARTMENT_ITEM in codes(p)


def test_all_errors_are_collected() -> None:
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C2", "-5")],
        [change(1, planned_qty=D("1")), change(2, planned_qty=D("-1"))],
        [],
    )
    assert {C.BELOW_USED_QTY, C.NEGATIVE_VALUE, C.NEGATIVE_BUDGET} <= codes(p)


def test_error_subjects_name_the_item() -> None:
    p = plan_amendment(BUDGETS, ITEMS, [], [change(1, planned_qty=D("1"))], [])
    e = next(e for e in p.errors if e.code is C.BELOW_USED_QTY)
    assert e.subject == "1" and e.message


def test_inputs_are_not_mutated_and_result_is_immutable() -> None:
    budgets, items = list(BUDGETS), list(ITEMS)
    p = plan_amendment(budgets, items, [], [change(2, planned_qty=D("6"))], [])
    assert budgets == BUDGETS and items == ITEMS
    assert isinstance(p.errors, tuple) and isinstance(p.item_diffs, tuple)


# ------------------------------------------------------------------ ledger entries
def _accepted():  # type: ignore[no-untyped-def]
    new = NewItem(
        "n1",
        vals(
            item_id="I9", planned_qty=D("2"), planned_amount=D("250"), estimated_unit_price=D("125")
        ),
    )
    p = plan_amendment(
        BUDGETS,
        ITEMS,
        [line("F1", "C1", "1650")],
        [
            change(1, planned_qty=D("8"), planned_amount=D("800")),  # -2 / -200
            change(2, owner_department_id="OR", planned_qty=D("6"), planned_amount=D("600")),
            change(3, estimated_unit_price=D("99")),  # data only, no entry
        ],
        [new],
    )
    assert p.ok, p.errors
    return p


@pytest.mark.spec("AT-40.11.1", "S-18.2", "D-37")
def test_ledger_entries_order_keys_and_deltas() -> None:
    es = ledger_entries(7, _accepted(), {"n1": 50})
    assert [
        (e.plan_item_id, e.event_type, e.qty_delta, e.amount_delta, e.idempotency_key) for e in es
    ] == [
        ("1", LedgerEventType.PLAN_AMENDMENT, D("-2"), D("-200"), "amendment:7:item:1"),
        ("2", LedgerEventType.PLAN_AMENDMENT, D("1"), D("100"), "amendment:7:item:2"),
        ("50", LedgerEventType.PLAN_ACTIVATED, D("0"), D("0"), "amendment:7:activate:50"),
        ("50", LedgerEventType.PLAN_AMENDMENT, D("2"), D("250"), "amendment:7:item:50"),
    ]
    assert all(isinstance(e, LedgerEntry) and e.ppr_ref is None for e in es)


def test_ledger_entries_apply_to_real_ledgers() -> None:
    es = ledger_entries(7, _accepted(), {"n1": 50})
    new_ledger = PlanItemLedger("50")
    for e in es:
        if e.plan_item_id == "50":
            new_ledger = new_ledger.append(e)
    b = new_ledger.balance
    assert (b.approved_qty, b.amendment_qty, b.remaining_qty) == (D(0), D(2), D(2))
    assert b.remaining_amount == D("250")


def test_ledger_entries_refuse_a_plan_with_errors() -> None:
    bad = plan_amendment(BUDGETS, ITEMS, [], [], [])
    with pytest.raises(ValueError):
        ledger_entries(1, bad, {})


def test_ledger_entries_need_every_new_item_id() -> None:
    with pytest.raises(ValueError):
        ledger_entries(7, _accepted(), {})


@pytest.mark.spec("D-37", "G-3")
def test_assigned_item_keeps_the_same_purchaser_and_warns_on_demand() -> None:
    items = [
        *ITEMS,
        CurrentItem(
            4,
            vals(
                plan_type=PlanType.ASSIGNED,
                item_id="I4",
                purchasing_department_id="STORE",
                budget_category_id="C3",
                planned_qty=D(3),
                planned_amount=D(30),
                estimated_unit_price=D(10),
            ),
            D(0),
            D(0),
            demand_total=D(3),
        ),
    ]
    budgets = [*BUDGETS, line("F1", "C3", "30")]
    cur = items[-1].values
    moved = plan_amendment(
        budgets, items, [], [ItemChange(4, replace(cur, purchasing_department_id="OR"))], []
    )
    assert C.ASSIGNED_PURCHASER_CHANGED in codes(moved)
    more = plan_amendment(
        budgets,
        items,
        [line("F1", "C3", "40")],
        [ItemChange(4, replace(cur, planned_qty=D(4), planned_amount=D(40)))],
        [],
    )
    assert more.ok and C.DEMAND_TOTAL_DIFFERS in {w.code for w in more.warnings}


@pytest.mark.spec("D-38", "G-1")
def test_new_central_item_needs_a_purchaser_and_a_distinct_match_key() -> None:
    no_buyer = NewItem(
        "c",
        vals(plan_type=PlanType.CENTRAL, item_id="I8", planned_qty=D(1), planned_amount=D(100)),
    )
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [no_buyer])
    assert C.CENTRAL_WITHOUT_PURCHASER in codes(p)
    # ENT already plans I1 (fund F1) as a DEPARTMENT item: a CENTRAL I1 bought by ENT clashes
    clash = NewItem(
        "c",
        vals(
            plan_type=PlanType.CENTRAL,
            item_id="I1",
            owner_department_id="STORE",
            purchasing_department_id="ENT",
            planned_qty=D(1),
            planned_amount=D(100),
        ),
    )
    p = plan_amendment(BUDGETS, ITEMS, [line("F1", "C1", "1600")], [], [clash])
    assert C.DUPLICATE_MATCH_KEY in codes(p)


def _assigned_world():  # type: ignore[no-untyped-def]
    from ppr.domain.amendment import DemandNow

    cur = CurrentItem(
        4,
        vals(
            plan_type=PlanType.ASSIGNED,
            item_id="I4",
            purchasing_department_id="STORE",
            budget_category_id="C3",
            planned_qty=D(60),
            planned_amount=D(600),
            estimated_unit_price=D(10),
        ),
        D(30),
        D(300),
        demand_total=D(60),
        demands=(
            DemandNow("A", D(10), D(10)),
            DemandNow("B", D(20), D(0)),
            DemandNow("C", D(30), D(20)),
        ),
    )
    return [*BUDGETS, line("F1", "C3", "600")], [*ITEMS, cur]


@pytest.mark.spec("D-38", "G-3")
def test_demand_amendment_rules() -> None:
    from ppr.domain.amendment import DemandChange

    budgets, items = _assigned_world()
    # B (nothing covered) cancelled, D added: total stays 60 -> no warning
    p = plan_amendment(
        budgets, items, [], [], [], [DemandChange(4, "B", D(0)), DemandChange(4, "D", D(20))]
    )
    assert p.ok, p.errors
    assert [(d.department_id, d.before, d.after) for d in p.demand_diffs] == [
        ("B", D(20), D(0)),
        ("D", None, D(20)),
    ]
    assert not p.warnings
    # C has 20 covered by PPRs in effect: 19 is refused, 20 allowed
    p = plan_amendment(budgets, items, [], [], [], [DemandChange(4, "C", D(19))])
    assert C.BELOW_COVERED_DEMAND in codes(p)
    p = plan_amendment(budgets, items, [], [], [], [DemandChange(4, "C", D(20))])
    assert p.ok and C.DEMAND_TOTAL_DIFFERS in {w.code for w in p.warnings}
    # demand only on ASSIGNED items; no duplicates; no negatives
    p = plan_amendment(budgets, items, [], [], [], [DemandChange(1, "A", D(1))])
    assert C.DEMAND_NOT_ASSIGNED in codes(p)
    p = plan_amendment(
        budgets, items, [], [], [], [DemandChange(4, "A", D(11)), DemandChange(4, "A", D(12))]
    )
    assert C.DUPLICATE_CHANGE in codes(p)
    p = plan_amendment(budgets, items, [], [], [], [DemandChange(4, "A", D(-1))])
    assert C.NEGATIVE_VALUE in codes(p)
    # unchanged demand is not a change
    p = plan_amendment(budgets, items, [], [], [], [DemandChange(4, "A", D(10))])
    assert codes(p) == {C.EMPTY_AMENDMENT}
