"""Hard plan control: quantity AND amount (spec §9.3, §13).

A request passes only if ``requested_qty <= remaining_qty`` AND
``requested_amount <= remaining_amount``. The estimated unit price is never a
constraint, and Q1-Q4 values are monitoring only (spec §9.4) - neither is an input.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ppr.domain.violations import Violation, ViolationCode

ZERO = Decimal(0)


@dataclass(frozen=True)
class Remaining:
    qty: Decimal
    amount: Decimal


def check_hard_control(
    requested_qty: Decimal,
    requested_amount: Decimal,
    remaining: Remaining,
    *,
    subject: str | None = None,
) -> tuple[Violation, ...]:
    out: list[Violation] = []

    if not requested_qty.is_finite() or requested_qty <= ZERO:
        out.append(
            Violation(
                ViolationCode.INVALID_REQUESTED_QTY,
                f"requested quantity must be > 0 (got {requested_qty})",
                subject,
            )
        )
    if not requested_amount.is_finite() or requested_amount < ZERO:
        out.append(
            Violation(
                ViolationCode.INVALID_REQUESTED_AMOUNT,
                f"requested amount must be >= 0 (got {requested_amount})",
                subject,
            )
        )
    if out:
        return tuple(out)

    if remaining.qty <= ZERO:
        out.append(
            Violation(
                ViolationCode.QTY_EXHAUSTED,
                f"plan quantity exhausted (remaining {remaining.qty})",
                subject,
            )
        )
    elif requested_qty > remaining.qty:
        out.append(
            Violation(
                ViolationCode.QTY_EXCEEDED,
                f"requested quantity {requested_qty} exceeds remaining {remaining.qty}",
                subject,
            )
        )

    if remaining.amount <= ZERO and requested_amount > ZERO:
        out.append(
            Violation(
                ViolationCode.AMOUNT_EXHAUSTED,
                f"plan amount exhausted (remaining {remaining.amount})",
                subject,
            )
        )
    elif requested_amount > remaining.amount:
        out.append(
            Violation(
                ViolationCode.AMOUNT_EXCEEDED,
                f"requested amount {requested_amount} exceeds remaining {remaining.amount}",
                subject,
            )
        )
    return tuple(out)
