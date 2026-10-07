"""PPR header-level budget-category allocation (spec §17, D-04).

Relationship: PPR 1 -> N allocation rows. Rules:

* every row belongs to the same PPR (the caller passes one PPR's rows);
* all categories are under the PR's fund source (cross-fund is rejected);
* a category appears at most once per PPR;
* SUM(amount) == required PPR amount;
* rows do NOT post Plan Ledger usage - this module has no ledger dependency.

The required amount is the sum of the PR item amounts (D-17); the caller supplies it.
Eligible categories (D-19) are supplied as a category -> fund-source lookup: the PR's own
header category, plus the categories of Plan Budget lines of the same fiscal year and
fund source. No HOSxP category hierarchy is inferred. More than one category requires a
non-empty ``adjustment_reference`` (D-19).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from ppr.domain.violations import Violation, ViolationCode

ZERO = Decimal(0)


@dataclass(frozen=True)
class CategoryAllocation:
    budget_category_id: str
    amount: Decimal


def validate_allocations(
    allocations: Sequence[CategoryAllocation],
    *,
    pr_fund_source_id: str,
    category_fund_source: Mapping[str, str],
    required_amount: Decimal,
) -> tuple[Violation, ...]:
    out: list[Violation] = []
    if not allocations:
        return (
            Violation(
                ViolationCode.ALLOCATION_EMPTY,
                "at least one budget-category allocation is required",
            ),
        )

    seen: set[str] = set()
    for a in allocations:
        cid = a.budget_category_id
        if cid in seen:
            out.append(
                Violation(
                    ViolationCode.ALLOCATION_DUPLICATE_CATEGORY,
                    f"category {cid} appears more than once in this PPR",
                    cid,
                )
            )
        seen.add(cid)
        if not a.amount.is_finite() or a.amount <= ZERO:
            out.append(
                Violation(
                    ViolationCode.ALLOCATION_NON_POSITIVE,
                    f"allocation for {cid} must be > 0 (got {a.amount})",
                    cid,
                )
            )
        fund = category_fund_source.get(cid)
        if fund is None:
            out.append(
                Violation(
                    ViolationCode.ALLOCATION_UNKNOWN_CATEGORY,
                    f"category {cid} is not eligible: it is neither the PR's category nor "
                    "a budget line of this fiscal year's plan for the PR fund source",
                    cid,
                )
            )
        elif fund != pr_fund_source_id:
            out.append(
                Violation(
                    ViolationCode.ALLOCATION_CROSS_FUND,
                    f"category {cid} belongs to fund source {fund}, "
                    f"not the PR fund source {pr_fund_source_id}",
                    cid,
                )
            )

    total = sum((a.amount for a in allocations), ZERO)
    if total != required_amount:
        out.append(
            Violation(
                ViolationCode.ALLOCATION_SUM_MISMATCH,
                f"allocations total {total} but the PPR requires {required_amount}",
            )
        )
    return tuple(out)


def is_multi_category(allocations: Sequence[CategoryAllocation]) -> bool:
    """True when more than one category funds the PPR - must be shown on UI/print/audit."""
    return len({a.budget_category_id for a in allocations}) > 1


def adjustment_reference_violation(
    allocations: Sequence[CategoryAllocation], adjustment_reference: str | None
) -> Violation | None:
    """D-19: several categories need proof that HOSxP's budget adjustment was handled."""
    if is_multi_category(allocations) and not (adjustment_reference or "").strip():
        return Violation(
            ViolationCode.ADJUSTMENT_REFERENCE_REQUIRED,
            "more than one budget category is used: enter the reference showing that the "
            "HOSxP budget/category adjustment has been handled",
        )
    return None
