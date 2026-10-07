"""Small builders for domain test data. All ids are MOCK-prefixed."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.hard_control import Remaining
from ppr.domain.ledger import LedgerEntry, LedgerEventType, PlanItemLedger
from ppr.domain.pr_binding import PrBinding
from ppr.domain.prevalidation import (
    PlanItemCandidate,
    PrevalidationContext,
    PrevalidationResult,
    PrLine,
    PrRequest,
    prevalidate,
)

D = Decimal
DEPT = "MOCK-DEP-ENT"
FUND = "MOCK-FUND-1"
FY = 2570
UNIT = "MOCK-UNIT"
_AUTO = object()


def line(
    pr_item_id: str, item_id: str, qty: str | int, amount: str | int, *, unit: str | None = UNIT
) -> PrLine:
    return PrLine(pr_item_id, item_id, D(qty), D(amount), unit=unit)


def pr(
    *lines: PrLine,
    dept: str | None = DEPT,
    fund: str | None = FUND,
    pr_no: str = "MOCK-PR-1",
    fiscal_year: int | None = FY,
    total: object = _AUTO,
    cancelled: bool = False,
) -> PrRequest:
    """A PR; by default its header total equals the sum of its lines (D-17)."""
    header_total = sum((ln.amount for ln in lines), D(0)) if total is _AUTO else total
    assert header_total is None or isinstance(header_total, Decimal)
    return PrRequest(
        pr_no,
        dept,
        fund,
        tuple(lines),
        fiscal_year=fiscal_year,
        header_total=header_total,
        cancelled_in_hosxp=cancelled,
    )


def plan_item(
    plan_item_id: str,
    item_id: str | None,
    qty: str | int,
    amount: str | int,
    *,
    dept: str = DEPT,
    fund: str = FUND,
    unit: str = UNIT,
) -> PlanItemCandidate:
    return PlanItemCandidate(plan_item_id, item_id, dept, fund, Remaining(D(qty), D(amount)), unit)


def ctx(
    *plan_items: PlanItemCandidate,
    state: PlanYearState | None = PlanYearState.ACTIVE,
    binding: PrBinding = PrBinding.UNBOUND,
    funds: frozenset[str] = frozenset({FUND}),
    current_fiscal_year: int = FY,
    choices: Mapping[str, str] | None = None,
) -> PrevalidationContext:
    return PrevalidationContext(
        state,
        binding,
        funds,
        tuple(plan_items),
        current_fiscal_year=current_fiscal_year,
        choices=dict(choices or {}),
    )


def same_item(pr: PrRequest | None, c: PrevalidationContext) -> PrevalidationContext:
    """D-43 test convenience: when no choice is given, choose for each line the plan row of
    the same HOSxP item if exactly one exists (the system itself never does this)."""
    if c.choices or pr is None:
        return c
    picked: dict[str, str] = {}
    for ln in pr.lines:
        same = [
            p.plan_item_id
            for p in c.plan_items
            if p.item_id == ln.item_id
            and p.department_id == pr.department_id
            and p.fund_source_id == pr.fund_source_id
        ]
        if len(same) == 1:
            picked[ln.pr_item_id] = same[0]
    return replace(c, choices=picked)


def check(pr: PrRequest | None, c: PrevalidationContext, *, pr_no: str) -> PrevalidationResult:
    """``prevalidate`` with the same-item choices of ``same_item``."""
    return prevalidate(pr, same_item(pr, c), pr_no=pr_no)


def approved_ledger(plan_item_id: str, qty: str | int, amount: str | int) -> PlanItemLedger:
    return PlanItemLedger(plan_item_id).append(
        LedgerEntry(
            plan_item_id,
            LedgerEventType.PLAN_ACTIVATED,
            D(qty),
            D(amount),
            idempotency_key=f"activate:{plan_item_id}",
        )
    )


def confirm(
    plan_item_id: str, ppr: str, qty: str | int, amount: str | int, key: str | None = None
) -> LedgerEntry:
    return LedgerEntry(
        plan_item_id,
        LedgerEventType.PPR_CONFIRMED,
        -D(qty),
        -D(amount),
        idempotency_key=key or f"confirm:{ppr}:{plan_item_id}",
        ppr_ref=ppr,
    )
