"""Plan Amendment rules (spec §20; D-11, D-37 7A).

Pure functions over plain values; persistence and HTTP live elsewhere.

An amendment is judged as a whole: the caller passes the plan year's CURRENT budget lines and
Plan Items (with what each item has used) plus the requested changes, and gets back either
errors (nothing may be saved) or the exact before/after of every change and the ledger
entries to post.

Rules (D-37 7A):

* at least one real change (a change equal to the current values is not a change);
* a Plan Item or budget line may be changed at most once per amendment;
* the fund source and the plan type of an existing item never change;
* item data other than numbers (HOSxP item, owner department, purchasing department,
  budget category) may change only while the item is unused (used qty and amount both 0);
* quantity, estimated unit price and planned amount are never negative; quantity and
  amount are never below what is used; a quantity of 0 requires an amount of 0;
* a new item has a quantity above 0; a new ASSIGNED item records each department's demand
  (D-38 A-6);
* demand of an existing ASSIGNED item may rise or fall, or a department be added, but never
  below what PPRs in effect cover for that department; 0 cancels it (A-6);
* a CENTRAL item names its purchasing department, and no DEPARTMENT / CENTRAL items may be
  indistinguishable for PR matching afterwards (D-38: DUPLICATE_MATCH_KEY);
* an ASSIGNED item keeps its purchasing department (unchanged; G-3 is Wave 7B), and a changed
  quantity that no longer equals its (unchanged) demand total is a warning;
* a budget line's approved amount is never negative;
* after the amendment every item sits on a budget line, every budget line is balanced
  (its items sum exactly to its approved amount), and no two DEPARTMENT items share
  (item, owner department, fund source).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from ppr.domain.ledger import LedgerEntry, LedgerEventType
from ppr.domain.plan import (
    BudgetLine,
    PlanItemFacts,
    PlanType,
    demand_claim_issues,
    match_key_issues,
)

ZERO = Decimal(0)


class AmendmentIssueCode(StrEnum):
    EMPTY_AMENDMENT = "EMPTY_AMENDMENT"
    UNKNOWN_PLAN_ITEM = "UNKNOWN_PLAN_ITEM"
    DUPLICATE_CHANGE = "DUPLICATE_CHANGE"
    FUND_SOURCE_CHANGED = "FUND_SOURCE_CHANGED"
    PLAN_TYPE_CHANGED = "PLAN_TYPE_CHANGED"
    DATA_CHANGE_AFTER_USE = "DATA_CHANGE_AFTER_USE"
    NEGATIVE_VALUE = "NEGATIVE_VALUE"
    BELOW_USED_QTY = "BELOW_USED_QTY"
    BELOW_USED_AMOUNT = "BELOW_USED_AMOUNT"
    ZERO_QTY_WITH_AMOUNT = "ZERO_QTY_WITH_AMOUNT"
    NEW_ITEM_ZERO_QTY = "NEW_ITEM_ZERO_QTY"
    ASSIGNED_WITHOUT_DEMAND = "ASSIGNED_WITHOUT_DEMAND"
    DEMAND_NOT_ASSIGNED = "DEMAND_NOT_ASSIGNED"
    BELOW_COVERED_DEMAND = "BELOW_COVERED_DEMAND"
    ASSIGNED_WITHOUT_PURCHASER = "ASSIGNED_WITHOUT_PURCHASER"
    ASSIGNED_PURCHASER_CHANGED = "ASSIGNED_PURCHASER_CHANGED"
    CENTRAL_WITHOUT_PURCHASER = "CENTRAL_WITHOUT_PURCHASER"
    DUPLICATE_MATCH_KEY = "DUPLICATE_MATCH_KEY"
    DUPLICATE_DEMAND_CLAIM = "DUPLICATE_DEMAND_CLAIM"
    NEGATIVE_BUDGET = "NEGATIVE_BUDGET"
    ITEM_WITHOUT_BUDGET = "ITEM_WITHOUT_BUDGET"
    ENVELOPE_MISMATCH = "ENVELOPE_MISMATCH"
    DUPLICATE_DEPARTMENT_ITEM = "DUPLICATE_DEPARTMENT_ITEM"
    NAME_AND_UNIT_REQUIRED = "NAME_AND_UNIT_REQUIRED"  # D-43: a row without a HOSxP item
    ASSIGNED_WITHOUT_ITEM = "ASSIGNED_WITHOUT_ITEM"  # D-38 A-1 needs the HOSxP item
    # warning (never blocks)
    AMOUNT_DIFFERS_FROM_ESTIMATE = "AMOUNT_DIFFERS_FROM_ESTIMATE"
    DEMAND_TOTAL_DIFFERS = "DEMAND_TOTAL_DIFFERS"


C = AmendmentIssueCode


@dataclass(frozen=True)
class AmendmentIssue:
    code: AmendmentIssueCode
    message: str
    subject: str | None = None  # plan item id, new-item ref or "fund/category"


@dataclass(frozen=True)
class ItemValues:
    """Every amendable value of one Plan Item."""

    plan_type: PlanType
    item_id: str | None  # None: a row without a HOSxP item (D-42, D-43)
    owner_department_id: str
    purchasing_department_id: str | None
    fund_source_id: str
    budget_category_id: str
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    # D-43: the row's name and unit (a row with a HOSxP item: the HOSxP item's)
    item_name: str = ""
    unit: str = ""

    @property
    def budget_key(self) -> tuple[str, str]:
        return (self.fund_source_id, self.budget_category_id)


@dataclass(frozen=True)
class CurrentItem:
    """A Plan Item as it is now, with what confirmed PPRs still hold on it."""

    plan_item_id: int
    values: ItemValues
    used_qty: Decimal
    used_amount: Decimal
    demand_total: Decimal | None = None  # ASSIGNED: sum of department demand
    # ASSIGNED: each department's demand and what PPRs in effect cover of it (D-38 A-6)
    demands: tuple[DemandNow, ...] = ()
    # D-43: whether any PPR ever drew on it (even if released since): its name and unit stay
    ever_used: bool = False


@dataclass(frozen=True)
class DemandNow:
    department_id: str
    demand_qty: Decimal
    covered: Decimal


@dataclass(frozen=True)
class DemandChange:
    """The wanted demand of one department on an existing ASSIGNED item (0 cancels it)."""

    plan_item_id: int
    department_id: str
    demand_qty: Decimal


@dataclass(frozen=True)
class DemandDiff:
    target: str  # existing plan item id, or the new item's ref
    department_id: str
    before: Decimal | None  # None: a department added to the item
    after: Decimal


@dataclass(frozen=True)
class ItemChange:
    """The complete wanted values of an existing Plan Item (not a delta)."""

    plan_item_id: int
    after: ItemValues


@dataclass(frozen=True)
class NewItem:
    """A Plan Item added by the amendment. ``ref`` is the caller's key for it."""

    ref: str
    values: ItemValues
    demands: tuple[tuple[str, Decimal], ...] = ()  # ASSIGNED: (department, demand)


@dataclass(frozen=True)
class BudgetDiff:
    fund_source_id: str
    budget_category_id: str
    before: Decimal | None  # None: the line is new
    after: Decimal


@dataclass(frozen=True)
class ItemDiff:
    plan_item_id: int
    before: ItemValues
    after: ItemValues

    @property
    def qty_delta(self) -> Decimal:
        return self.after.planned_qty - self.before.planned_qty

    @property
    def amount_delta(self) -> Decimal:
        return self.after.planned_amount - self.before.planned_amount


@dataclass(frozen=True)
class AmendmentPlan:
    errors: tuple[AmendmentIssue, ...]
    warnings: tuple[AmendmentIssue, ...]
    budget_diffs: tuple[BudgetDiff, ...]  # only real changes, in request order
    item_diffs: tuple[ItemDiff, ...]  # only real changes, in request order
    new_items: tuple[NewItem, ...]  # in request order
    demand_diffs: tuple[DemandDiff, ...] = ()  # existing items' demand changes, request order

    @property
    def ok(self) -> bool:
        return not self.errors


def plan_amendment(
    current_budgets: Sequence[BudgetLine],
    current_items: Sequence[CurrentItem],
    budget_changes: Sequence[BudgetLine],
    item_changes: Sequence[ItemChange],
    new_items: Sequence[NewItem],
    demand_changes: Sequence[DemandChange] = (),
) -> AmendmentPlan:
    """Judge an amendment as a whole (see the module docstring for the rules).

    ``budget_changes`` holds the wanted approved amount of existing lines and of new lines
    (a line is new when its (fund source, category) is not in ``current_budgets``).
    A budget change or item change equal to the current values is ignored (not a diff);
    if nothing real remains and there are no new items, the error is EMPTY_AMENDMENT.
    Errors and warnings are all collected (not just the first). When there are errors the
    diffs may still be returned but must not be saved.
    """
    errors: list[AmendmentIssue] = []
    warnings: list[AmendmentIssue] = []

    def err(code: AmendmentIssueCode, message: str, subject: str | None) -> None:
        errors.append(AmendmentIssue(code, message, subject))

    cur_items = {c.plan_item_id: c for c in current_items}
    cur_budgets = {b.key: b.approved_amount for b in current_budgets}

    # ---- budget lines
    budget_diffs: list[BudgetDiff] = []
    seen_lines: set[tuple[str, str]] = set()
    after_budgets = dict(cur_budgets)
    for line in budget_changes:
        subject = _line_subject(line.key)
        if line.key in seen_lines:
            err(C.DUPLICATE_CHANGE, f"budget line {subject} is changed twice", subject)
            continue
        seen_lines.add(line.key)
        if line.approved_amount < ZERO:
            err(C.NEGATIVE_BUDGET, f"budget line {subject} cannot be negative", subject)
        old = cur_budgets.get(line.key)
        if old is not None and old == line.approved_amount:
            continue  # no change
        after_budgets[line.key] = line.approved_amount
        budget_diffs.append(
            BudgetDiff(line.fund_source_id, line.budget_category_id, old, line.approved_amount)
        )

    # ---- existing items
    item_diffs: list[ItemDiff] = []
    seen_items: set[int] = set()
    after_items = {pid: c.values for pid, c in cur_items.items()}
    for change in item_changes:
        subject = str(change.plan_item_id)
        if change.plan_item_id in seen_items:
            err(C.DUPLICATE_CHANGE, f"Plan Item {subject} is changed twice", subject)
            continue
        seen_items.add(change.plan_item_id)
        current = cur_items.get(change.plan_item_id)
        if current is None:
            err(C.UNKNOWN_PLAN_ITEM, f"Plan Item {subject} is not in this plan year", subject)
            continue
        before, after = current.values, change.after
        if after == before:
            continue  # no change
        if after.fund_source_id != before.fund_source_id:
            err(C.FUND_SOURCE_CHANGED, "the fund source of a Plan Item never changes", subject)
        if after.plan_type is not before.plan_type:
            err(C.PLAN_TYPE_CHANGED, "the plan type of a Plan Item never changes", subject)
        used = current.used_qty != ZERO or current.used_amount != ZERO
        if used and _data(after) != _data(before):
            err(
                C.DATA_CHANGE_AFTER_USE,
                "a used Plan Item keeps its item, departments and category; reduce it to what "
                "is used and add a new item instead",
                subject,
            )
        elif (used or current.ever_used) and _label(after) != _label(before):
            err(
                C.DATA_CHANGE_AFTER_USE,
                "a PPR has drawn on this Plan Item: its name and unit stay as the requester "
                "chose it (D-43); add a new item instead",
                subject,
            )
        if (
            before.plan_type is PlanType.ASSIGNED
            and after.purchasing_department_id != before.purchasing_department_id
        ):
            err(
                C.ASSIGNED_PURCHASER_CHANGED,
                "an Assigned Purchase item keeps its purchasing department: the coverage "
                "recorded on its PPRs belongs to that department (D-38, G-3)",
                subject,
            )
        _check_values(after, subject, err, warnings)
        if after.planned_qty < current.used_qty:
            err(
                C.BELOW_USED_QTY,
                f"quantity {after.planned_qty} is below the {current.used_qty} already used",
                subject,
            )
        if after.planned_amount < current.used_amount:
            err(
                C.BELOW_USED_AMOUNT,
                f"amount {after.planned_amount} is below the {current.used_amount} already used",
                subject,
            )
        after_items[change.plan_item_id] = after
        item_diffs.append(ItemDiff(change.plan_item_id, before, after))

    # ---- new items
    news: list[NewItem] = []
    seen_refs: set[str] = set()
    for new in new_items:
        if new.ref in seen_refs:
            err(C.DUPLICATE_CHANGE, f"new item {new.ref} is given twice", new.ref)
            continue
        seen_refs.add(new.ref)
        if new.values.plan_type is PlanType.ASSIGNED:
            depts = [d for d, _ in new.demands]
            if not [q for _, q in new.demands if q > ZERO]:
                err(
                    C.ASSIGNED_WITHOUT_DEMAND,
                    "a new Assigned Purchase item must record each department's demand",
                    new.ref,
                )
            if len(set(depts)) != len(depts):
                err(C.DUPLICATE_CHANGE, "a department is given twice in the demand", new.ref)
            if any(q <= ZERO for _, q in new.demands):
                err(C.NEGATIVE_VALUE, "a new item's demand must be above 0", new.ref)
        elif new.demands:
            err(
                C.DEMAND_NOT_ASSIGNED,
                "only an Assigned Purchase item records department demand",
                new.ref,
            )
        if new.values.planned_qty <= ZERO:
            err(C.NEW_ITEM_ZERO_QTY, "a new Plan Item needs a quantity above 0", new.ref)
        _check_values(new.values, new.ref, err, warnings)
        news.append(new)

    # ---- demand of existing ASSIGNED items (D-38 A-6)
    demand_diffs: list[DemandDiff] = []
    seen_demand: set[tuple[int, str]] = set()
    after_demand: dict[int, dict[str, Decimal]] = {
        pid: {d.department_id: d.demand_qty for d in c.demands} for pid, c in cur_items.items()
    }
    for dc in demand_changes:
        subject = f"{dc.plan_item_id}/{dc.department_id}"
        if (dc.plan_item_id, dc.department_id) in seen_demand:
            err(C.DUPLICATE_CHANGE, f"demand {subject} is changed twice", subject)
            continue
        seen_demand.add((dc.plan_item_id, dc.department_id))
        current = cur_items.get(dc.plan_item_id)
        if current is None:
            err(
                C.UNKNOWN_PLAN_ITEM,
                f"Plan Item {dc.plan_item_id} is not in this plan year",
                subject,
            )
            continue
        if current.values.plan_type is not PlanType.ASSIGNED:
            err(
                C.DEMAND_NOT_ASSIGNED,
                "only an Assigned Purchase item records department demand",
                subject,
            )
            continue
        now = {d.department_id: d for d in current.demands}.get(dc.department_id)
        before_qty = now.demand_qty if now else None
        if dc.demand_qty < ZERO:
            err(C.NEGATIVE_VALUE, "a demand cannot be negative", subject)
            continue
        if before_qty == dc.demand_qty or (before_qty is None and dc.demand_qty == ZERO):
            continue  # no change
        if now is not None and dc.demand_qty < now.covered:
            err(
                C.BELOW_COVERED_DEMAND,
                f"demand {dc.demand_qty} is below the {now.covered} that PPRs in effect already "
                "cover for this department; change or cancel those PPRs first (D-38 A-6)",
                subject,
            )
        after_demand[dc.plan_item_id][dc.department_id] = dc.demand_qty
        demand_diffs.append(
            DemandDiff(str(dc.plan_item_id), dc.department_id, before_qty, dc.demand_qty)
        )
    changed_qty = {d.plan_item_id for d in item_diffs if d.qty_delta != ZERO}
    changed_qty |= {int(d.target) for d in demand_diffs}
    for pid in sorted(changed_qty):
        cur = cur_items.get(pid)
        if cur is None or cur.values.plan_type is not PlanType.ASSIGNED:
            continue
        total = sum(after_demand[pid].values(), ZERO)
        if total != after_items[pid].planned_qty:
            warnings.append(
                AmendmentIssue(
                    C.DEMAND_TOTAL_DIFFERS,
                    f"department demand totals {total}, the planned quantity is "
                    f"{after_items[pid].planned_qty}",
                    str(pid),
                )
            )
    for new in news:
        if new.values.plan_type is PlanType.ASSIGNED and new.demands:
            total = sum((q for _, q in new.demands), ZERO)
            if total != new.values.planned_qty:
                warnings.append(
                    AmendmentIssue(
                        C.DEMAND_TOTAL_DIFFERS,
                        f"department demand totals {total}, the planned quantity is "
                        f"{new.values.planned_qty}",
                        new.ref,
                    )
                )

    if not (budget_diffs or item_diffs or news or demand_diffs or errors):
        err(C.EMPTY_AMENDMENT, "the amendment changes nothing", None)

    # ---- the plan year after the amendment
    everything: list[tuple[str, ItemValues]] = [(str(pid), v) for pid, v in after_items.items()] + [
        (n.ref, n.values) for n in news
    ]
    totals = dict.fromkeys(after_budgets, ZERO)
    for subject, v in everything:
        if v.budget_key not in totals:
            err(
                C.ITEM_WITHOUT_BUDGET,
                f"fund {v.fund_source_id} / category {v.budget_category_id} has no budget line",
                subject,
            )
            continue
        totals[v.budget_key] += v.planned_amount
    for key, approved in after_budgets.items():
        if totals[key] != approved:
            err(
                C.ENVELOPE_MISMATCH,
                f"Plan Items of {_line_subject(key)} total {totals[key]}, approved budget "
                f"{approved} (difference {totals[key] - approved})",
                _line_subject(key),
            )
    dept_keys = Counter(
        (v.item_id, v.owner_department_id, v.fund_source_id)
        for _, v in everything
        if v.plan_type is PlanType.DEPARTMENT and v.item_id is not None
    )
    for (item_id, dept, fund), n in sorted(dept_keys.items()):
        if n > 1:
            err(
                C.DUPLICATE_DEPARTMENT_ITEM,
                f"item {item_id} would appear {n} times as a DEPARTMENT Plan Item of "
                f"department {dept} and fund {fund}; PRs could not be matched unambiguously",
                item_id,
            )
    for issue in match_key_issues(
        PlanItemFacts(
            ref=subject,
            plan_type=v.plan_type,
            item_id=v.item_id,
            owner_department_id=v.owner_department_id,
            purchasing_department_id=v.purchasing_department_id,
            fund_source_id=v.fund_source_id,
            budget_category_id=v.budget_category_id,
            planned_qty=v.planned_qty,
            estimated_unit_price=v.estimated_unit_price,
            planned_amount=v.planned_amount,
        )
        for subject, v in everything
    ):
        err(C.DUPLICATE_MATCH_KEY, issue.message, issue.subject)
    # D-38: one department's demand, one Assigned Purchase line (after this amendment)
    open_demand: dict[str, tuple[str, ...]] = {
        str(pid): tuple(d for d, q in depts.items() if q > ZERO)
        for pid, depts in after_demand.items()
    }
    open_demand.update({n.ref: tuple(d for d, q in n.demands if q > ZERO) for n in news})
    for issue in demand_claim_issues(
        PlanItemFacts(
            ref=subject,
            plan_type=v.plan_type,
            item_id=v.item_id,
            owner_department_id=v.owner_department_id,
            purchasing_department_id=v.purchasing_department_id,
            fund_source_id=v.fund_source_id,
            budget_category_id=v.budget_category_id,
            planned_qty=v.planned_qty,
            estimated_unit_price=v.estimated_unit_price,
            planned_amount=v.planned_amount,
            demand_departments=open_demand.get(subject, ()),
        )
        for subject, v in everything
    ):
        err(C.DUPLICATE_DEMAND_CLAIM, issue.message, issue.subject)
    return AmendmentPlan(
        tuple(errors),
        tuple(warnings),
        tuple(budget_diffs),
        tuple(item_diffs),
        tuple(news),
        tuple(demand_diffs),
    )


def _line_subject(key: tuple[str, str]) -> str:
    return f"{key[0]}/{key[1]}"


def _data(v: ItemValues) -> tuple[str | None, str, str | None, str]:
    """The non-numeric values that may change only while an item is unused."""
    return (v.item_id, v.owner_department_id, v.purchasing_department_id, v.budget_category_id)


def _label(v: ItemValues) -> tuple[str, str]:
    """D-43: the name and unit a requester chooses a row by."""
    return (v.item_name.strip(), v.unit.strip())


def _check_values(
    v: ItemValues,
    subject: str,
    err: Callable[[AmendmentIssueCode, str, str | None], None],
    warnings: list[AmendmentIssue],
) -> None:
    if v.item_id is None and (not v.item_name.strip() or not v.unit.strip()):
        err(
            C.NAME_AND_UNIT_REQUIRED,
            "a Plan Item without a HOSxP item needs its own name and unit (D-43)",
            subject,
        )
    if v.item_id is None and v.plan_type is PlanType.ASSIGNED:
        err(
            C.ASSIGNED_WITHOUT_ITEM,
            "an Assigned Purchase item needs a HOSxP item: one department's demand is "
            "recognized on the PR by its item (D-38 A-1)",
            subject,
        )
    if v.planned_qty < ZERO or v.estimated_unit_price < ZERO or v.planned_amount < ZERO:
        err(C.NEGATIVE_VALUE, "quantity, estimated price and amount cannot be negative", subject)
    if v.planned_qty == ZERO and v.planned_amount != ZERO:
        err(C.ZERO_QTY_WITH_AMOUNT, "a quantity of 0 needs an amount of 0", subject)
    if v.plan_type is PlanType.CENTRAL and not v.purchasing_department_id:
        err(
            C.CENTRAL_WITHOUT_PURCHASER,
            "a Central Pool item must name its purchasing department (D-38)",
            subject,
        )
    if v.plan_type is PlanType.ASSIGNED and not v.purchasing_department_id:
        err(
            C.ASSIGNED_WITHOUT_PURCHASER,
            "an Assigned Purchase item must name the purchasing department",
            subject,
        )
    estimate = v.planned_qty * v.estimated_unit_price
    if estimate != v.planned_amount:
        warnings.append(
            AmendmentIssue(
                C.AMOUNT_DIFFERS_FROM_ESTIMATE,
                f"planned amount {v.planned_amount} differs from quantity x estimated price "
                f"{estimate} (allowed; price is an estimate)",
                subject,
            )
        )


def ledger_entries(
    amendment_id: int, plan: AmendmentPlan, new_item_ids: Mapping[str, int]
) -> tuple[LedgerEntry, ...]:
    """The ledger entries an accepted amendment posts, in this order:

    1. for each item diff with a non-zero qty or amount delta: one ``PLAN_AMENDMENT``
       (qty_delta, amount_delta), key ``amendment:{amendment_id}:item:{plan_item_id}``;
       item diffs that change only data post nothing;
    2. for each new item (``new_item_ids[ref]`` is its saved id): ``PLAN_ACTIVATED`` of
       (0, 0), key ``amendment:{amendment_id}:activate:{id}``, then ``PLAN_AMENDMENT`` of
       (planned_qty, planned_amount), key ``amendment:{amendment_id}:item:{id}``.

    ``plan_item_id`` of each entry is the item id as a string (as in ``ppr.domain.ledger``).
    Raises ``ValueError`` if the plan has errors or a new item's id is missing.
    """
    if not plan.ok:
        raise ValueError("the amendment has errors")
    out: list[LedgerEntry] = []
    for d in plan.item_diffs:
        if d.qty_delta != ZERO or d.amount_delta != ZERO:
            out.append(
                LedgerEntry(
                    str(d.plan_item_id),
                    LedgerEventType.PLAN_AMENDMENT,
                    d.qty_delta,
                    d.amount_delta,
                    f"amendment:{amendment_id}:item:{d.plan_item_id}",
                )
            )
    for n in plan.new_items:
        if n.ref not in new_item_ids:
            raise ValueError(f"no id for new item {n.ref}")
        pid = new_item_ids[n.ref]
        out.append(
            LedgerEntry(
                str(pid),
                LedgerEventType.PLAN_ACTIVATED,
                ZERO,
                ZERO,
                f"amendment:{amendment_id}:activate:{pid}",
            )
        )
        out.append(
            LedgerEntry(
                str(pid),
                LedgerEventType.PLAN_AMENDMENT,
                n.values.planned_qty,
                n.values.planned_amount,
                f"amendment:{amendment_id}:item:{pid}",
            )
        )
    return tuple(out)


__all__ = [
    "AmendmentIssue",
    "AmendmentIssueCode",
    "AmendmentPlan",
    "BudgetDiff",
    "CurrentItem",
    "DemandChange",
    "DemandDiff",
    "DemandNow",
    "ItemChange",
    "ItemDiff",
    "ItemValues",
    "LedgerEntry",
    "LedgerEventType",
    "NewItem",
    "ledger_entries",
    "plan_amendment",
]
