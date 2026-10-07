"""In-app alert rules (spec §34; D-33).

Pure functions. Alerts are DERIVED from the current data every time they are shown: there is
no alert record and no acknowledgement, so an alert disappears as soon as its cause is gone
(a cancelled or closed PPR, a plan year that is no longer ACTIVE, a later successful sync).
Alerts never block anything; the hard controls stay where they are (§34).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from ppr.domain.ledger import LedgerBalance
from ppr.domain.ppr_state import PprState

ZERO = Decimal(0)
HUNDRED = Decimal(100)


class AlertType(StrEnum):
    PPR_INACTIVE = "PPR_INACTIVE"
    PR_CHANGED = "PR_CHANGED"
    PLAN_LOW = "PLAN_LOW"
    PLAN_EXHAUSTED = "PLAN_EXHAUSTED"
    SYNC_FAILURE = "SYNC_FAILURE"


class AlertSetting(StrEnum):
    PPR_INACTIVE_DAYS = "PPR_INACTIVE_DAYS"
    PLAN_LOW_PERCENT = "PLAN_LOW_PERCENT"


# Defaults and allowed ranges (D-33). The database enforces the same ranges.
SETTING_DEFAULTS: dict[AlertSetting, int] = {
    AlertSetting.PPR_INACTIVE_DAYS: 30,
    AlertSetting.PLAN_LOW_PERCENT: 20,
}
SETTING_RANGES: dict[AlertSetting, tuple[int, int]] = {
    AlertSetting.PPR_INACTIVE_DAYS: (1, 365),
    AlertSetting.PLAN_LOW_PERCENT: (1, 99),
}

# Open confirmed-or-later PPRs: the same set as the procurement queue's "inactive" bucket.
# Drafts hold no plan usage and never alert; CANCELLED / CLOSED are finished cases (§34).
INACTIVITY_STATES: frozenset[PprState] = frozenset(
    {
        PprState.CONFIRMED_LOCKED,
        PprState.PROCUREMENT_VERIFIED,
        PprState.PR_CHANGED_REVIEW_REQUIRED,
        PprState.UNLOCKED_FOR_REVISION,
    }
)


def in_range(setting: AlertSetting, value: int) -> bool:
    low, high = SETTING_RANGES[setting]
    return low <= value <= high


def inactive_cutoff(now: datetime, days: int) -> datetime:
    """A PPR whose last change is strictly before this time is inactive."""
    return now - timedelta(days=days)


def is_inactive(state: PprState, last_change: datetime, now: datetime, days: int) -> bool:
    return state in INACTIVITY_STATES and last_change < inactive_cutoff(now, days)


def is_pr_changed(state: PprState) -> bool:
    return state is PprState.PR_CHANGED_REVIEW_REQUIRED


@dataclass(frozen=True)
class PlanLevel:
    """How far one Plan Item's plan is used up. ``None`` = no alert."""

    alert: AlertType | None
    remaining_qty_percent: Decimal | None  # of the current plan (approved + amendments)
    remaining_amount_percent: Decimal | None


def _percent(remaining: Decimal, base: Decimal) -> Decimal | None:
    if base <= ZERO:
        return None
    return remaining * HUNDRED / base


def plan_level(balance: LedgerBalance, low_percent: int) -> PlanLevel:
    """Low / exhausted plan (§34, D-33).

    Quantity and amount are judged separately. The base is the current plan: approved plus
    amendments. Zero remaining on either is ``PLAN_EXHAUSTED``; otherwise remaining at or
    below ``low_percent`` % on either is ``PLAN_LOW``. An item without a plan base (nothing
    approved) raises no alert.
    """
    qty_base = balance.approved_qty + balance.amendment_qty
    amount_base = balance.approved_amount + balance.amendment_amount
    q_pct = _percent(balance.remaining_qty, qty_base)
    a_pct = _percent(balance.remaining_amount, amount_base)
    if q_pct is None and a_pct is None:
        return PlanLevel(None, None, None)
    exhausted = (q_pct is not None and balance.remaining_qty <= ZERO) or (
        a_pct is not None and balance.remaining_amount <= ZERO
    )
    if exhausted:
        return PlanLevel(AlertType.PLAN_EXHAUSTED, q_pct, a_pct)
    limit = Decimal(low_percent)
    low = (q_pct is not None and q_pct <= limit) or (a_pct is not None and a_pct <= limit)
    return PlanLevel(AlertType.PLAN_LOW if low else None, q_pct, a_pct)


# Terminal statuses of synchronization runs (a RUNNING run says nothing yet).
FAILED_PPR_SYNC = frozenset({"FAILED", "PARTIAL"})
FAILED_MASTER_SYNC = frozenset({"FAILED"})


def sync_failed(latest_finished_status: str | None, *, masters: bool) -> bool:
    """D-33: the latest FINISHED run of that kind failed (PR sync: FAILED or PARTIAL)."""
    if latest_finished_status is None:
        return False
    return latest_finished_status in (FAILED_MASTER_SYNC if masters else FAILED_PPR_SYNC)
