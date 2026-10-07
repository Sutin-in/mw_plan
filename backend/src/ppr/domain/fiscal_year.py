"""Fiscal-year rules (spec §7, D-08).

Boundaries and labeling are *configuration* (``FiscalYearConfig``); nothing here
hard-codes a start date or calendar era. Whether HOSxP exposes a PR fiscal-year
field is unverified (D-08), so callers resolve a PR's fiscal year explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Literal


class PlanYearState(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


_PLAN_YEAR_TRANSITIONS: dict[PlanYearState, frozenset[PlanYearState]] = {
    PlanYearState.DRAFT: frozenset({PlanYearState.APPROVED}),
    PlanYearState.APPROVED: frozenset({PlanYearState.ACTIVE}),
    PlanYearState.ACTIVE: frozenset({PlanYearState.CLOSED}),
    PlanYearState.CLOSED: frozenset(),
}


def can_transition_plan_year(current: PlanYearState, target: PlanYearState) -> bool:
    return target in _PLAN_YEAR_TRANSITIONS[current]


def new_ppr_allowed(state: PlanYearState) -> bool:
    """New PPRs may only be created against an ACTIVE fiscal year (spec §7)."""
    return state is PlanYearState.ACTIVE


LabelRule = Literal["END_YEAR", "START_YEAR"]


@dataclass(frozen=True)
class FiscalYearConfig:
    """Fiscal-year boundary configuration.

    start_month/start_day: first day of every fiscal year.
    era_offset: added to the Gregorian year to form the label (0 for CE).
    label: whether the fiscal year is named after the calendar year it starts or ends in.
    """

    start_month: int
    start_day: int
    era_offset: int
    label: LabelRule

    def __post_init__(self) -> None:
        # Validate against a non-leap year so a Feb-29 start is rejected.
        try:
            date(2001, self.start_month, self.start_day)
        except ValueError as exc:
            raise ValueError(
                f"invalid fiscal-year start {self.start_month}-{self.start_day}"
            ) from exc
        if self.era_offset < 0:
            raise ValueError("era_offset must be >= 0")
        if self.label not in ("END_YEAR", "START_YEAR"):
            raise ValueError(f"invalid label rule {self.label!r}")

    @property
    def starts_jan_1(self) -> bool:
        return (self.start_month, self.start_day) == (1, 1)


def fiscal_year_label(day: date, cfg: FiscalYearConfig) -> int:
    """Return the fiscal-year label that contains ``day``."""
    start_this_year = date(day.year, cfg.start_month, cfg.start_day)
    start_year = day.year if day >= start_this_year else day.year - 1
    end_year = start_year if cfg.starts_jan_1 else start_year + 1
    base = end_year if cfg.label == "END_YEAR" else start_year
    return base + cfg.era_offset


def fiscal_year_bounds(label: int, cfg: FiscalYearConfig) -> tuple[date, date]:
    """Return (first_day, last_day) inclusive for a fiscal-year label."""
    base = label - cfg.era_offset
    start_year = base if cfg.label == "START_YEAR" or cfg.starts_jan_1 else base - 1
    first = date(start_year, cfg.start_month, cfg.start_day)
    last = date(start_year + 1, cfg.start_month, cfg.start_day) - timedelta(days=1)
    return first, last
