"""Plan Usage Ledger arithmetic (spec §18-§20, §40.5, §40.8, §40.11, §40.12; D-39)."""

from __future__ import annotations

import contextlib
import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from builders import approved_ledger, confirm
from ppr.domain.ledger import (
    DuplicateLedgerEventError,
    LedgerEntry,
    LedgerEventType,
    LedgerRuleError,
    NegativeRemainingError,
    PlanItemLedger,
    StockMovementNotLedgerError,
)

D = Decimal
P = "MOCK-PLAN-1"


def amend(q: str | int, a: str | int, key: str) -> LedgerEntry:
    return LedgerEntry(P, LedgerEventType.PLAN_AMENDMENT, D(q), D(a), idempotency_key=key)


@pytest.mark.spec("AT-40.5.1", "S-18.3")
def test_confirm_reduces_remaining() -> None:
    led = approved_ledger(P, 10, 30000).append(confirm(P, "PPR-A", 4, 12000))
    assert (led.balance.remaining_qty, led.balance.remaining_amount) == (D(6), D(18000))
    assert (led.balance.ppr_used_qty, led.balance.ppr_used_amount) == (D(4), D(12000))


@pytest.mark.spec("AT-40.5.2", "AT-40.8.2", "S-14.6")
def test_cancel_releases_exactly_what_the_ppr_consumed() -> None:
    led = approved_ledger(P, 10, 30000).append(confirm(P, "PPR-A", 4, 12000))
    rel = led.release_entry_for("PPR-A")
    assert rel is not None
    assert (rel.qty_delta, rel.amount_delta) == (D(4), D(12000))
    led = led.append(rel)
    assert (led.balance.remaining_qty, led.balance.remaining_amount) == (D(10), D(30000))


@pytest.mark.spec("AT-40.8.3", "S-25.5", "S-38.2")
def test_repeated_cancellation_never_releases_twice() -> None:
    led = approved_ledger(P, 10, 30000).append(confirm(P, "PPR-A", 4, 12000))
    rel = led.release_entry_for("PPR-A")
    assert rel is not None
    led = led.append(rel)
    assert led.release_entry_for("PPR-A") is None  # nothing left to release
    with pytest.raises(DuplicateLedgerEventError):
        led.append(rel)  # replaying the same sync event is rejected
    over = LedgerEntry(P, LedgerEventType.PPR_RELEASED, D(1), D(0), "manual-x", ppr_ref="PPR-A")
    with pytest.raises(LedgerRuleError, match="exceeds"):
        led.append(over)


@pytest.mark.spec("AT-40.5.3", "S-18.3")
def test_remaining_cannot_be_overwritten_directly() -> None:
    led = approved_ledger(P, 10, 30000)
    bal = led.balance
    with pytest.raises(dataclasses.FrozenInstanceError):
        bal.remaining_qty = D(999)  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        led.entries = ()  # type: ignore[misc]
    assert not any("SET" in t.value or "OVERWRITE" in t.value for t in LedgerEventType)


@pytest.mark.spec("S-19", "S-30")
def test_oversubscription_is_rejected() -> None:
    led = approved_ledger(P, 10, 30000).append(confirm(P, "PPR-A", 10, 10000))
    with pytest.raises(NegativeRemainingError):
        led.append(confirm(P, "PPR-B", 1, 1))


@pytest.mark.spec("AT-40.11.1", "S-20")
def test_amendment_increase_updates_remaining() -> None:
    led = approved_ledger(P, 10, 30000).append(amend(5, 15000, "amd-1"))
    assert (led.balance.remaining_qty, led.balance.remaining_amount) == (D(15), D(45000))


@pytest.mark.spec("AT-40.11.2", "S-20")
def test_amendment_cannot_reduce_quantity_below_used() -> None:
    led = approved_ledger(P, 10, 30000).append(confirm(P, "PPR-A", 8, 1000))
    with pytest.raises(NegativeRemainingError):
        led.append(amend(-3, 0, "amd-1"))  # used 8 of 10; reducing by 3 -> 7 < 8
    assert led.append(amend(-2, 0, "amd-2")).balance.remaining_qty == D(0)


@pytest.mark.spec("AT-40.11.3", "S-20")
def test_amendment_cannot_reduce_amount_below_used() -> None:
    led = approved_ledger(P, 10, 30000).append(confirm(P, "PPR-A", 1, 25000))
    with pytest.raises(NegativeRemainingError):
        led.append(amend(0, -5001, "amd-1"))


@pytest.mark.spec("AT-40.12.2", "D-39", "S-18.2")
@pytest.mark.parametrize("event", ["CENTRAL_STOCK_ISSUE", "CENTRAL_STOCK_RETURN"])
@pytest.mark.parametrize(
    ("qty", "amount"),
    [(-100, 0), (100, 0), (-1, -1), (1, 1), (0, 0)],  # D-01's old "quantity only" shapes too
)
def test_stock_issue_and_return_can_never_be_ledger_entries(
    event: str, qty: int, amount: int
) -> None:
    """D-39: whatever the deltas, a stock movement is refused before it reaches a ledger."""
    with pytest.raises(StockMovementNotLedgerError, match="D-39"):
        LedgerEntry(P, event, D(qty), D(amount), "k")  # type: ignore[arg-type]
    assert event not in {e.value for e in LedgerEventType}  # no enum member to post with
    with pytest.raises(ValueError):
        LedgerEventType(event)


@pytest.mark.spec("AT-40.12.2", "D-39", "S-18.3")
def test_balance_has_no_stock_term() -> None:
    """Remaining is approved + amendments - PPR usage + releases; nothing else (D-39)."""
    led = approved_ledger(P, 1000, 50000).append(confirm(P, "PPR-A", 100, 5000))
    b = led.balance
    assert (b.remaining_qty, b.remaining_amount) == (D(900), D(45000))
    assert not any("stock" in f.name for f in dataclasses.fields(b))


def test_unknown_event_type_is_refused() -> None:
    with pytest.raises(LedgerRuleError, match="unknown"):
        LedgerEntry(P, "SOMETHING_ELSE", D(1), D(1), "k")  # type: ignore[arg-type]


def test_plan_activated_must_come_first_and_only_once() -> None:
    with pytest.raises(LedgerRuleError, match="first"):
        PlanItemLedger(P).append(confirm(P, "PPR-A", 1, 1))
    led = approved_ledger(P, 1, 1)
    with pytest.raises(LedgerRuleError, match="already"):
        led.append(LedgerEntry(P, LedgerEventType.PLAN_ACTIVATED, D(1), D(1), "activate-2"))


def test_entry_for_other_plan_item_rejected() -> None:
    with pytest.raises(LedgerRuleError, match="different plan item"):
        approved_ledger(P, 1, 1).append(confirm("MOCK-PLAN-2", "PPR-A", 1, 1))


def test_ppr_events_require_ppr_ref_and_keys_are_required() -> None:
    with pytest.raises(LedgerRuleError, match="ppr_ref"):
        LedgerEntry(P, LedgerEventType.PPR_CONFIRMED, D(-1), D(-1), "k")
    with pytest.raises(LedgerRuleError, match="idempotency_key"):
        LedgerEntry(P, LedgerEventType.PLAN_AMENDMENT, D(1), D(1), "")


def test_ledger_is_append_only_value() -> None:
    led = approved_ledger(P, 10, 100)
    led2 = led.append(confirm(P, "PPR-A", 1, 10))
    assert len(led.entries) == 1 and len(led2.entries) == 2  # original unchanged


# ------------------------------------------------------------------ properties
_q = st.integers(min_value=1, max_value=50)
_a = st.integers(min_value=0, max_value=5000)


@pytest.mark.spec("AT-40.5.4", "S-18.3", "S-19")
@given(
    approved=st.tuples(st.integers(0, 200), st.integers(0, 20000)),
    ops=st.lists(
        st.one_of(
            st.tuples(st.just("confirm"), _q, _a),
            st.tuples(st.just("cancel"), st.integers(0, 30), st.just(0)),
            st.tuples(st.just("amend"), st.integers(-50, 50), st.integers(-5000, 5000)),
        ),
        max_size=40,
    ),
)
def test_property_remaining_matches_formula_and_never_negative(
    approved: tuple[int, int], ops: list[tuple[str, int, int]]
) -> None:
    led = approved_ledger(P, *approved)
    for i, (op, x, y) in enumerate(ops):
        entry: LedgerEntry | None
        if op == "confirm":
            entry = confirm(P, f"PPR-{i}", x, y)
        elif op == "cancel":
            entry = led.release_entry_for(f"PPR-{x}")
        else:
            entry = amend(x, y, f"amd-{i}") if (x or y) else None
        if entry is None:
            continue
        with contextlib.suppress(NegativeRemainingError):  # rejected appends change nothing
            led = led.append(entry)
        b = led.balance
        # Remaining is always the sum of all deltas, never negative.
        assert b.remaining_qty == sum((e.qty_delta for e in led.entries), D(0))
        assert b.remaining_amount == sum((e.amount_delta for e in led.entries), D(0))
        assert b.remaining_qty >= 0 and b.remaining_amount >= 0
        # And equals the spec §18.3 formula (no stock term, D-39).
        assert b.remaining_amount == (
            b.approved_amount + b.amendment_amount - b.ppr_used_amount + b.released_amount
        )
