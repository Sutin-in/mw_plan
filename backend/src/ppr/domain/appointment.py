"""Head of Procurement appointment rules (spec §23, D-27, D-28). Pure: no I/O.

* D-27: an appointment's effective dates lie inside its fiscal year; an appointment
  without an end date ends on the last day of that fiscal year; ACTIVE appointments never
  overlap (checked against the existing ones here, and again by the database).
* D-28: verification authority is judged on the server date of verification - the
  appointment must be ACTIVE, effective on that date, and belong to that date's fiscal year.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from ppr.domain.fiscal_year import FiscalYearConfig, fiscal_year_bounds, fiscal_year_label


class AppointmentRuleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Period:
    """An ACTIVE appointment's inclusive effective range."""

    appointment_id: int
    effective_from: date
    effective_to: date | None

    def overlaps(self, start: date, end: date) -> bool:
        return self.effective_from <= end and (
            self.effective_to is None or start <= self.effective_to
        )


def resolve_range(
    fiscal_year: int, effective_from: date, effective_to: date | None, cfg: FiscalYearConfig
) -> tuple[date, date]:
    """D-27: both dates inside the fiscal year; no end date -> the fiscal year's last day."""
    start, end = fiscal_year_bounds(fiscal_year, cfg)
    to = effective_to if effective_to is not None else end
    if not (start <= effective_from <= end):
        raise AppointmentRuleError(
            "APPOINTMENT_OUTSIDE_FISCAL_YEAR",
            f"effective_from must be within fiscal year {fiscal_year} ({start} .. {end})",
        )
    if not (start <= to <= end):
        raise AppointmentRuleError(
            "APPOINTMENT_OUTSIDE_FISCAL_YEAR",
            f"effective_to must be within fiscal year {fiscal_year} ({start} .. {end})",
        )
    if to < effective_from:
        raise AppointmentRuleError(
            "APPOINTMENT_INVALID_RANGE", "effective_to must not be before effective_from"
        )
    return effective_from, to


def check_no_overlap(start: date, end: date, active: Iterable[Period]) -> None:
    """D-27: at most one ACTIVE appointment is effective on any date."""
    for p in active:
        if p.overlaps(start, end):
            raise AppointmentRuleError(
                "APPOINTMENT_OVERLAP",
                f"the dates overlap active appointment {p.appointment_id} "
                f"({p.effective_from} .. {p.effective_to or 'open'}); end it first",
            )


def check_new_end(
    current: Period, new_end: date, today: date, last_verification: date | None
) -> None:
    """Ending early (D-27): only shorten; never before its start, yesterday, or its last use."""
    if current.effective_to is not None and new_end >= current.effective_to:
        raise AppointmentRuleError(
            "APPOINTMENT_END_NOT_EARLIER", "the new end date must be earlier than the current one"
        )
    floor = max(
        current.effective_from,
        date.fromordinal(today.toordinal() - 1),
        last_verification or current.effective_from,
    )
    if new_end < floor:
        raise AppointmentRuleError(
            "APPOINTMENT_END_TOO_EARLY",
            f"the end date cannot be before {floor} (start, yesterday, or its last verification)",
        )


def authorizes(
    status: str,
    fiscal_year: int,
    effective_from: date,
    effective_to: date | None,
    day: date,
    cfg: FiscalYearConfig,
) -> bool:
    """D-28: ACTIVE, effective on ``day``, and of ``day``'s fiscal year."""
    return (
        status == "ACTIVE"
        and effective_from <= day
        and (effective_to is None or day <= effective_to)
        and fiscal_year == fiscal_year_label(day, cfg)
    )
