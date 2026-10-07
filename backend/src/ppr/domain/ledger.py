"""Plan Usage Ledger arithmetic (spec §18, §19, §20; D-01).

The ledger is append-only and remaining balances are DERIVED from it - there is no
operation that sets "remaining" directly (spec §18.3). Invariants enforced on every
append:

* one ``PLAN_ACTIVATED`` per plan item, and it must come first;
* idempotency keys are unique (sync can never double-post or double-release);
* per-event sign rules;
* stock issues and returns are NOT ledger events (D-39): HOSxP owns them and they are only
  shown in a report, so ``CENTRAL_STOCK_ISSUE`` / ``CENTRAL_STOCK_RETURN`` are refused here
  and by a database constraint (migration 0010);
* remaining quantity and remaining amount never go negative (oversubscription,
  amendment below used - spec §19, §20);
* a PPR can never be released for more than it consumed.

Nothing in this module reads or posts stock movements.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

ZERO = Decimal(0)


class LedgerEventType(StrEnum):
    PLAN_ACTIVATED = "PLAN_ACTIVATED"
    PPR_CONFIRMED = "PPR_CONFIRMED"
    PPR_RELEASED = "PPR_RELEASED"
    PLAN_AMENDMENT = "PLAN_AMENDMENT"


# D-39: stock issues / returns stay in HOSxP. These names (D-01, superseded) are kept only so
# that an attempt to post one is recognised and refused with a clear reason.
STOCK_MOVEMENT_EVENTS = frozenset({"CENTRAL_STOCK_ISSUE", "CENTRAL_STOCK_RETURN"})


class LedgerRuleError(ValueError):
    """An entry violates a per-event rule."""


class DuplicateLedgerEventError(LedgerRuleError):
    """The idempotency key was already posted."""


class StockMovementNotLedgerError(LedgerRuleError):
    """D-39: a stock issue or return was offered to the Plan Ledger."""


class NegativeRemainingError(LedgerRuleError):
    """The append would make remaining quantity or amount negative."""


_PPR_EVENTS = frozenset({LedgerEventType.PPR_CONFIRMED, LedgerEventType.PPR_RELEASED})


def _check_event(e: LedgerEntry) -> None:
    t = e.event_type
    if str(t) in STOCK_MOVEMENT_EVENTS:
        raise StockMovementNotLedgerError(
            f"{t} cannot be posted: stock issues and returns are HOSxP data shown in reports "
            "only, never Plan Ledger transactions (D-39)"
        )
    if not isinstance(t, LedgerEventType):
        raise LedgerRuleError(f"unknown ledger event {t!r}")


def _check_signs(e: LedgerEntry) -> None:
    q, a, t = e.qty_delta, e.amount_delta, e.event_type
    if not (q.is_finite() and a.is_finite()):
        raise LedgerRuleError("deltas must be finite")
    if t is LedgerEventType.PLAN_ACTIVATED:
        ok = q >= ZERO and a >= ZERO
    elif t is LedgerEventType.PPR_CONFIRMED:
        ok = q < ZERO and a <= ZERO
    elif t is LedgerEventType.PPR_RELEASED:
        ok = q >= ZERO and a >= ZERO and (q > ZERO or a > ZERO)
    elif t is LedgerEventType.PLAN_AMENDMENT:
        ok = q != ZERO or a != ZERO
    else:  # pragma: no cover - exhaustive
        ok = False
    if not ok:
        raise LedgerRuleError(f"{t}: invalid deltas qty={q} amount={a}")
    if t in _PPR_EVENTS and not e.ppr_ref:
        raise LedgerRuleError(f"{t} requires ppr_ref")


@dataclass(frozen=True)
class LedgerEntry:
    plan_item_id: str
    event_type: LedgerEventType
    qty_delta: Decimal
    amount_delta: Decimal
    idempotency_key: str
    ppr_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            raise LedgerRuleError("idempotency_key is required")
        _check_event(self)
        _check_signs(self)


@dataclass(frozen=True)
class LedgerBalance:
    approved_qty: Decimal
    approved_amount: Decimal
    amendment_qty: Decimal
    amendment_amount: Decimal
    ppr_used_qty: Decimal  # positive magnitudes
    ppr_used_amount: Decimal
    released_qty: Decimal
    released_amount: Decimal

    @property
    def remaining_qty(self) -> Decimal:
        """Spec §18.3: no stock term - stock issues and returns never change the plan (D-39)."""
        return self.approved_qty + self.amendment_qty - self.ppr_used_qty + self.released_qty

    @property
    def remaining_amount(self) -> Decimal:
        """Spec §18.3 (D-39: no stock term)."""
        return (
            self.approved_amount
            + self.amendment_amount
            - self.ppr_used_amount
            + self.released_amount
        )


def balance_from_totals(totals: Mapping[LedgerEventType, tuple[Decimal, Decimal]]) -> LedgerBalance:
    """Balance from the summed (qty_delta, amount_delta) of each event type.

    The ONE place that turns ledger deltas into a balance: used for a single item's entries
    and for many items summed by the database (dashboards, Wave 8A).
    """

    def t(event: LedgerEventType) -> tuple[Decimal, Decimal]:
        return totals.get(event, (ZERO, ZERO))

    aq, aa = t(LedgerEventType.PLAN_ACTIVATED)
    mq, ma = t(LedgerEventType.PLAN_AMENDMENT)
    uq, ua = t(LedgerEventType.PPR_CONFIRMED)
    rq, ra = t(LedgerEventType.PPR_RELEASED)
    # ZERO - x (not -x): a missing event gives 0, never "-0".
    return LedgerBalance(aq, aa, mq, ma, ZERO - uq, ZERO - ua, rq, ra)


def _balance(entries: tuple[LedgerEntry, ...]) -> LedgerBalance:
    totals: dict[LedgerEventType, tuple[Decimal, Decimal]] = {}
    for e in entries:
        q, a = totals.get(e.event_type, (ZERO, ZERO))
        totals[e.event_type] = (q + e.qty_delta, a + e.amount_delta)
    return balance_from_totals(totals)


@dataclass(frozen=True)
class PlanItemLedger:
    """Immutable ledger for ONE plan item. ``append`` returns a new ledger."""

    plan_item_id: str
    entries: tuple[LedgerEntry, ...] = field(default=())

    @property
    def balance(self) -> LedgerBalance:
        return _balance(self.entries)

    def outstanding_for_ppr(self, ppr_ref: str) -> tuple[Decimal, Decimal]:
        """(qty, amount) still consumed by ``ppr_ref`` (confirmed minus released)."""
        q = a = ZERO
        for e in self.entries:
            if e.ppr_ref != ppr_ref:
                continue
            if e.event_type in _PPR_EVENTS:
                q -= e.qty_delta
                a -= e.amount_delta
        return q, a

    def append(self, entry: LedgerEntry) -> PlanItemLedger:
        if entry.plan_item_id != self.plan_item_id:
            raise LedgerRuleError("entry belongs to a different plan item")
        if any(e.idempotency_key == entry.idempotency_key for e in self.entries):
            raise DuplicateLedgerEventError(f"duplicate event {entry.idempotency_key!r}")
        has_activation = any(e.event_type is LedgerEventType.PLAN_ACTIVATED for e in self.entries)
        if entry.event_type is LedgerEventType.PLAN_ACTIVATED:
            if has_activation:
                raise LedgerRuleError("plan item already has PLAN_ACTIVATED")
        elif not has_activation:
            raise LedgerRuleError("PLAN_ACTIVATED must be posted first")
        if entry.event_type is LedgerEventType.PPR_RELEASED:
            assert entry.ppr_ref is not None
            oq, oa = self.outstanding_for_ppr(entry.ppr_ref)
            if entry.qty_delta > oq or entry.amount_delta > oa:
                raise LedgerRuleError(
                    f"release exceeds what PPR {entry.ppr_ref} consumed (qty {oq}, amount {oa})"
                )
        new = PlanItemLedger(self.plan_item_id, (*self.entries, entry))
        b = new.balance
        if b.remaining_qty < ZERO or b.remaining_amount < ZERO:
            raise NegativeRemainingError(
                f"{entry.event_type} would make remaining negative "
                f"(qty {b.remaining_qty}, amount {b.remaining_amount})"
            )
        return new

    def release_entry_for(self, ppr_ref: str) -> LedgerEntry | None:
        """Entry that releases whatever ``ppr_ref`` still holds, or None if nothing is held.

        The idempotency key is deterministic, so a repeated cancellation sync can never
        release twice (spec §14.6, §25.5).
        """
        q, a = self.outstanding_for_ppr(ppr_ref)
        if q <= ZERO and a <= ZERO:
            return None
        return LedgerEntry(
            self.plan_item_id,
            LedgerEventType.PPR_RELEASED,
            q,
            a,
            idempotency_key=f"release:{ppr_ref}:{self.plan_item_id}",
            ppr_ref=ppr_ref,
        )
