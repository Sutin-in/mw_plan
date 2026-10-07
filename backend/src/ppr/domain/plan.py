"""Plan Core rules (spec §9, §10; D-11, D-12, D-13).

Pure functions over plain values; persistence and HTTP live elsewhere.

* Budget envelope (§9.1, D-11): for every approved budget line (fund source, budget
  category) the Plan Items attached to THAT EXACT category must sum to the line's
  approved amount. No roll-up from child categories.
* Activation checks (D-12) also require every Plan Item to sit on a budget line, and no
  two DEPARTMENT Plan Items to be indistinguishable for PR matching (assumption A-9,
  consistent with the fail-closed matcher A-1).
* Central Pool (D-38, G-1): a CENTRAL item names its purchasing department, and a PR line
  matches it when the PR comes from that department. So a DEPARTMENT item (by owner) and a
  CENTRAL item (by purchaser) with the same item, department and fund source - or two such
  CENTRAL items - would be indistinguishable too (DUPLICATE_MATCH_KEY).
* Planned amount vs quantity x estimated price (D-13): a mismatch is a WARNING only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal(0)


class PlanType(StrEnum):
    DEPARTMENT = "DEPARTMENT"
    CENTRAL = "CENTRAL"
    ASSIGNED = "ASSIGNED"


class DemandState(StrEnum):
    PLANNED = "PLANNED"
    INCLUDED_IN_CENTRAL_PURCHASE = "INCLUDED_IN_CENTRAL_PURCHASE"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"


class IssueCode(StrEnum):
    NO_BUDGETS = "NO_BUDGETS"
    NO_ITEMS = "NO_ITEMS"
    ENVELOPE_MISMATCH = "ENVELOPE_MISMATCH"
    ITEM_WITHOUT_BUDGET = "ITEM_WITHOUT_BUDGET"
    DUPLICATE_DEPARTMENT_ITEM = "DUPLICATE_DEPARTMENT_ITEM"
    ASSIGNED_WITHOUT_DEMAND = "ASSIGNED_WITHOUT_DEMAND"
    ASSIGNED_WITHOUT_PURCHASER = "ASSIGNED_WITHOUT_PURCHASER"
    CENTRAL_WITHOUT_PURCHASER = "CENTRAL_WITHOUT_PURCHASER"
    DUPLICATE_MATCH_KEY = "DUPLICATE_MATCH_KEY"
    DUPLICATE_DEMAND_CLAIM = "DUPLICATE_DEMAND_CLAIM"
    # warnings
    AMOUNT_DIFFERS_FROM_ESTIMATE = "AMOUNT_DIFFERS_FROM_ESTIMATE"
    DEMAND_TOTAL_DIFFERS = "DEMAND_TOTAL_DIFFERS"
    QUARTERS_DIFFER = "QUARTERS_DIFFER"


@dataclass(frozen=True)
class Issue:
    code: IssueCode
    message: str
    subject: str | None = None


@dataclass(frozen=True)
class BudgetLine:
    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal

    @property
    def key(self) -> tuple[str, str]:
        return (self.fund_source_id, self.budget_category_id)


@dataclass(frozen=True)
class PlanItemFacts:
    ref: str  # stable id for messages
    plan_type: PlanType
    item_id: str | None  # None: an imported row not yet bound; matches no PR line (D-42)
    owner_department_id: str
    purchasing_department_id: str | None
    fund_source_id: str
    budget_category_id: str
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    quarters: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None] = (
        None,
        None,
        None,
        None,
    )
    demand_qtys: tuple[Decimal, ...] = ()
    # ASSIGNED: departments with demand above 0 (D-38: one demand, one purchase line)
    demand_departments: tuple[str, ...] = ()

    @property
    def budget_key(self) -> tuple[str, str]:
        return (self.fund_source_id, self.budget_category_id)


@dataclass(frozen=True)
class EnvelopeLine:
    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal
    planned_total: Decimal

    @property
    def difference(self) -> Decimal:
        return self.planned_total - self.approved_amount

    @property
    def balanced(self) -> bool:
        return self.difference == ZERO


@dataclass(frozen=True)
class ActivationReport:
    envelope: tuple[EnvelopeLine, ...]
    errors: tuple[Issue, ...] = field(default=())
    warnings: tuple[Issue, ...] = field(default=())

    @property
    def can_activate(self) -> bool:
        return not self.errors


def item_warnings(item: PlanItemFacts) -> tuple[Issue, ...]:
    """Non-blocking observations about one Plan Item (D-13, §9.4)."""
    out: list[Issue] = []
    estimate = item.planned_qty * item.estimated_unit_price
    if estimate != item.planned_amount:
        out.append(
            Issue(
                IssueCode.AMOUNT_DIFFERS_FROM_ESTIMATE,
                f"planned amount {item.planned_amount} differs from quantity x estimated "
                f"price {estimate} (allowed; price is an estimate)",
                item.ref,
            )
        )
    qs = [q for q in item.quarters if q is not None]
    if qs and len(qs) == 4 and sum(qs, ZERO) not in (item.planned_qty, item.planned_amount):
        out.append(
            Issue(
                IssueCode.QUARTERS_DIFFER,
                "Q1-Q4 do not add up to the planned quantity or amount "
                "(monitoring only, never blocks)",
                item.ref,
            )
        )
    if item.plan_type is PlanType.ASSIGNED and item.demand_qtys:
        total = sum(item.demand_qtys, ZERO)
        if total != item.planned_qty:
            out.append(
                Issue(
                    IssueCode.DEMAND_TOTAL_DIFFERS,
                    f"department demand totals {total}, planned quantity is {item.planned_qty}",
                    item.ref,
                )
            )
    return tuple(out)


def check_activation(
    budgets: Sequence[BudgetLine], items: Iterable[PlanItemFacts]
) -> ActivationReport:
    items = list(items)
    errors: list[Issue] = []
    warnings: list[Issue] = []

    if not budgets:
        errors.append(Issue(IssueCode.NO_BUDGETS, "the plan year has no approved budget lines"))
    if not items:
        errors.append(Issue(IssueCode.NO_ITEMS, "the plan year has no Plan Items"))

    totals: dict[tuple[str, str], Decimal] = {b.key: ZERO for b in budgets}
    for it in items:
        if it.budget_key not in totals:
            errors.append(
                Issue(
                    IssueCode.ITEM_WITHOUT_BUDGET,
                    f"Plan Item uses fund {it.fund_source_id} / category "
                    f"{it.budget_category_id}, which has no budget line",
                    it.ref,
                )
            )
            continue
        totals[it.budget_key] += it.planned_amount

    envelope = tuple(
        EnvelopeLine(b.fund_source_id, b.budget_category_id, b.approved_amount, totals[b.key])
        for b in budgets
    )
    for line in envelope:
        if not line.balanced:
            errors.append(
                Issue(
                    IssueCode.ENVELOPE_MISMATCH,
                    f"category {line.budget_category_id} (fund {line.fund_source_id}): "
                    f"Plan Items total {line.planned_total}, approved budget "
                    f"{line.approved_amount} (difference {line.difference})",
                    line.budget_category_id,
                )
            )

    errors.extend(match_key_issues(items))
    errors.extend(demand_claim_issues(items))
    dept_keys = Counter(
        (it.item_id, it.owner_department_id, it.fund_source_id)
        for it in items
        if it.plan_type is PlanType.DEPARTMENT and it.item_id is not None
    )
    for (item_id, dept, fund), n in sorted(dept_keys.items()):
        if n > 1:
            errors.append(
                Issue(
                    IssueCode.DUPLICATE_DEPARTMENT_ITEM,
                    f"item {item_id} appears {n} times as a DEPARTMENT Plan Item for department "
                    f"{dept} and fund {fund}; PRs could not be matched unambiguously",
                    item_id,
                )
            )

    for it in items:
        if it.plan_type is PlanType.CENTRAL and not it.purchasing_department_id:
            errors.append(
                Issue(
                    IssueCode.CENTRAL_WITHOUT_PURCHASER,
                    "a Central Pool item must name its purchasing department (D-38)",
                    it.ref,
                )
            )
        if it.plan_type is PlanType.ASSIGNED:
            if not it.demand_qtys:
                errors.append(
                    Issue(
                        IssueCode.ASSIGNED_WITHOUT_DEMAND,
                        "an Assigned Purchase item must record per-department demand (§10.3)",
                        it.ref,
                    )
                )
            if not it.purchasing_department_id:
                errors.append(
                    Issue(
                        IssueCode.ASSIGNED_WITHOUT_PURCHASER,
                        "an Assigned Purchase item must name the purchasing department (§10.3)",
                        it.ref,
                    )
                )
        warnings.extend(item_warnings(it))

    return ActivationReport(envelope, tuple(errors), tuple(warnings))


def match_department(plan_type: PlanType, owner: str, purchaser: str | None) -> str | None:
    """The department whose PRs a Plan Item answers (D-16, D-38): the owner of a DEPARTMENT
    item, the purchasing department of a CENTRAL or ASSIGNED item."""
    if plan_type is PlanType.DEPARTMENT:
        return owner
    return purchaser


def responsible_department(plan_type: PlanType, owner: str, purchaser: str | None) -> str:
    """The department a Plan Item is reported and seen under (D-38 C-4): the purchasing
    department of a CENTRAL item (its owner field gives no view), otherwise the owner."""
    if plan_type is PlanType.CENTRAL and purchaser:
        return purchaser
    return owner


def visible_to_department(
    department_id: str,
    plan_type: PlanType,
    owner: str,
    purchaser: str | None,
    demand_departments: Iterable[str] = (),
) -> bool:
    """§22.1 / D-38 C-4: a department sees the items it owns (not CENTRAL ones), purchases,
    or has demand on. Must agree with ``PlanRepository.items(fy, department)``."""
    return (
        (plan_type is not PlanType.CENTRAL and owner == department_id)
        or purchaser == department_id
        or department_id in demand_departments
    )


def match_key_issues(items: Iterable[PlanItemFacts]) -> list[Issue]:
    """D-38 C-2: items of different kinds (DEPARTMENT, CENTRAL, ASSIGNED) or two pooled items
    (CENTRAL / ASSIGNED) that a PR line could not tell apart. Two DEPARTMENT items alone are
    DUPLICATE_DEPARTMENT_ITEM."""
    keys: dict[tuple[str, str, str], list[PlanItemFacts]] = {}
    for it in items:
        if it.item_id is None:  # D-42: an unbound row answers no PR line (yet)
            continue
        dept = match_department(it.plan_type, it.owner_department_id, it.purchasing_department_id)
        if dept:
            keys.setdefault((it.item_id, dept, it.fund_source_id), []).append(it)
    out: list[Issue] = []
    for (item_id, dept, fund), group in sorted(keys.items()):
        types = [it.plan_type for it in group]
        if len(types) > 1 and any(t is not PlanType.DEPARTMENT for t in types):
            out.append(
                Issue(
                    IssueCode.DUPLICATE_MATCH_KEY,
                    f"item {item_id} of fund {fund} is planned {len(types)} times for PRs of "
                    f"department {dept} (department and/or Central Pool items); a PR line could "
                    "not be matched unambiguously (D-38)",
                    ", ".join(it.ref for it in group),  # the colliding Plan Items
                )
            )
    return out


def demand_claim_issues(items: Iterable[PlanItemFacts]) -> list[Issue]:
    """D-38 (product owner, 2026-10-01): a department's need for one HOSxP item and fund source
    is claimed by at most one Assigned Purchase at a time - open demand of the same department
    on two ``ASSIGNED`` items with the same item and fund (two purchase lines that could both
    buy it) is refused. A demand of 0 (cancelled) claims nothing."""
    keys: dict[tuple[str, str, str], list[str]] = {}
    for it in items:
        if it.plan_type is PlanType.ASSIGNED and it.item_id is not None:
            for dept in sorted(set(it.demand_departments)):
                keys.setdefault((it.item_id, it.fund_source_id, dept), []).append(it.ref)
    return [
        Issue(
            IssueCode.DUPLICATE_DEMAND_CLAIM,
            f"department {dept} has demand for item {item_id} of fund {fund} on {len(refs)} "
            "Assigned Purchase items; one demand may be bought by one purchase line only (D-38)",
            ", ".join(refs),
        )
        for (item_id, fund, dept), refs in sorted(keys.items())
        if len(refs) > 1
    ]
