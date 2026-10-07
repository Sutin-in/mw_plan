"""In-app alert rules (spec §34; D-33) and the shared ledger balance formula (§18.3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from builders import approved_ledger, confirm
from ppr.domain.alerts import (
    SETTING_DEFAULTS,
    AlertSetting,
    AlertType,
    in_range,
    is_inactive,
    is_pr_changed,
    plan_level,
    sync_failed,
)
from ppr.domain.ledger import (
    LedgerBalance,
    LedgerEntry,
    LedgerEventType,
    PlanItemLedger,
    balance_from_totals,
)
from ppr.domain.ppr_state import PprState

D = Decimal
P = "MOCK-PLAN-1"
NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def used(qty: str | int, amount: str | int, planned: tuple[int, int] = (10, 1000)) -> LedgerBalance:
    led = approved_ledger(P, *planned)
    if D(qty) or D(amount):
        led = led.append(confirm(P, "PPR-A", qty, amount))
    return led.balance


@pytest.mark.spec("D-33", "S-34")
def test_defaults_are_thirty_days_and_twenty_percent() -> None:
    assert SETTING_DEFAULTS == {
        AlertSetting.PPR_INACTIVE_DAYS: 30,
        AlertSetting.PLAN_LOW_PERCENT: 20,
    }
    assert in_range(AlertSetting.PPR_INACTIVE_DAYS, 1) and in_range(
        AlertSetting.PPR_INACTIVE_DAYS, 365
    )
    assert not in_range(AlertSetting.PPR_INACTIVE_DAYS, 0)
    assert not in_range(AlertSetting.PLAN_LOW_PERCENT, 100)


@pytest.mark.spec("D-33", "S-34")
def test_plan_level_low_exhausted_and_quiet() -> None:
    assert plan_level(used(0, 0), 20).alert is None
    assert plan_level(used(7, 700), 20).alert is None  # 30 % left
    assert plan_level(used(8, 800), 20).alert is AlertType.PLAN_LOW  # exactly 20 % left
    assert plan_level(used(9, 950), 20).alert is AlertType.PLAN_LOW
    assert plan_level(used(10, 1000), 20).alert is AlertType.PLAN_EXHAUSTED


@pytest.mark.spec("D-33")
def test_quantity_and_amount_are_judged_separately() -> None:
    # Quantity used up while money remains (price below estimate) is exhausted.
    lvl = plan_level(used(10, 600), 20)
    assert lvl.alert is AlertType.PLAN_EXHAUSTED
    assert lvl.remaining_qty_percent == 0 and lvl.remaining_amount_percent == 40
    # Money low while quantity remains is low.
    assert plan_level(used(2, 900), 20).alert is AlertType.PLAN_LOW


@pytest.mark.spec("D-33")
def test_threshold_change_moves_the_line() -> None:
    b = used(9, 900)  # 10 % left
    assert plan_level(b, 20).alert is AlertType.PLAN_LOW
    assert plan_level(b, 5).alert is None


@pytest.mark.spec("D-33", "S-20")
def test_amendments_are_part_of_the_plan_base() -> None:
    led = approved_ledger(P, 10, 1000).append(confirm(P, "PPR-A", 9, 900))
    led = led.append(
        LedgerEntry(P, LedgerEventType.PLAN_AMENDMENT, D(10), D(1000), idempotency_key="am-1")
    )
    # 11 of 20 left: not low any more.
    assert plan_level(led.balance, 20).alert is None


def test_nothing_planned_raises_no_alert() -> None:
    empty = PlanItemLedger(P).balance
    assert plan_level(empty, 20).alert is None


@pytest.mark.spec("D-33", "S-34")
def test_inactivity_counts_open_confirmed_states_only() -> None:
    old = NOW - timedelta(days=31)
    for st_ in (
        PprState.CONFIRMED_LOCKED,
        PprState.PROCUREMENT_VERIFIED,
        PprState.PR_CHANGED_REVIEW_REQUIRED,
        PprState.UNLOCKED_FOR_REVISION,
    ):
        assert is_inactive(st_, old, NOW, 30)
    # Drafts hold no plan usage; cancelled/closed cases never remain as stale alerts.
    for st_ in (PprState.DRAFT, PprState.CANCELLED, PprState.CLOSED):
        assert not is_inactive(st_, old, NOW, 30)
    exactly = NOW - timedelta(days=30)
    assert not is_inactive(PprState.CONFIRMED_LOCKED, exactly, NOW, 30)  # strictly older
    assert is_pr_changed(PprState.PR_CHANGED_REVIEW_REQUIRED)
    assert not is_pr_changed(PprState.CONFIRMED_LOCKED)


@pytest.mark.spec("D-33")
def test_sync_failure_uses_the_latest_finished_run() -> None:
    assert sync_failed("FAILED", masters=False)
    assert sync_failed("PARTIAL", masters=False)
    assert not sync_failed("SUCCEEDED", masters=False)
    assert not sync_failed(None, masters=False)
    assert sync_failed("FAILED", masters=True)
    assert not sync_failed("SUCCEEDED", masters=True)


# ------------------------------------------------------------------ one balance formula
money = st.integers(min_value=0, max_value=10_000)


@given(q=st.integers(min_value=1, max_value=50), a=money, uq=st.integers(0, 50), ua=money)
def test_summed_totals_give_the_same_balance_as_the_entries(
    q: int, a: int, uq: int, ua: int
) -> None:
    uq, ua = min(uq, q), min(ua, a)
    led = approved_ledger(P, q, a)
    if uq:
        led = led.append(confirm(P, "PPR-A", uq, ua))
        led = led.append(confirm(P, "PPR-B", q - uq, 0)) if q > uq else led
        release = led.release_entry_for("PPR-A")
        assert release is not None
        led = led.append(release)
    totals: dict[LedgerEventType, tuple[Decimal, Decimal]] = {}
    for e in led.entries:
        tq, ta = totals.get(e.event_type, (D(0), D(0)))
        totals[e.event_type] = (tq + e.qty_delta, ta + e.amount_delta)
    assert balance_from_totals(totals) == led.balance


def test_missing_events_are_zero_not_negative_zero() -> None:
    b = balance_from_totals({})
    assert str(b.ppr_used_qty) == "0" and str(b.released_qty) == "0"
