"""Head of Procurement appointment rules (spec §23, D-27, D-28) - pure domain."""

from __future__ import annotations

from datetime import date

import pytest

from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR as CFG
from ppr.domain.appointment import (
    AppointmentRuleError,
    Period,
    authorizes,
    check_new_end,
    check_no_overlap,
    resolve_range,
)

FY = 2570  # 2026-10-01 .. 2027-09-30


@pytest.mark.spec("D-27", "S-23")
def test_open_ended_appointment_ends_at_the_fiscal_year_end() -> None:
    assert resolve_range(FY, date(2026, 10, 1), None, CFG) == (
        date(2026, 10, 1),
        date(2027, 9, 30),
    )


@pytest.mark.spec("D-27")
@pytest.mark.parametrize(
    ("start", "end", "code"),
    [
        (date(2026, 9, 30), None, "APPOINTMENT_OUTSIDE_FISCAL_YEAR"),
        (date(2026, 10, 1), date(2027, 10, 1), "APPOINTMENT_OUTSIDE_FISCAL_YEAR"),
        (date(2027, 1, 10), date(2027, 1, 9), "APPOINTMENT_INVALID_RANGE"),
    ],
)
def test_dates_must_lie_inside_the_fiscal_year(start: date, end: date | None, code: str) -> None:
    with pytest.raises(AppointmentRuleError) as e:
        resolve_range(FY, start, end, CFG)
    assert e.value.code == code


@pytest.mark.spec("D-27", "AT-40.9.4")
def test_active_appointments_never_overlap_but_may_follow_each_other() -> None:
    first = Period(1, date(2026, 10, 1), date(2027, 3, 31))
    check_no_overlap(date(2027, 4, 1), date(2027, 9, 30), [first])  # the next day: fine
    with pytest.raises(AppointmentRuleError) as e:
        check_no_overlap(date(2027, 3, 31), date(2027, 9, 30), [first])
    assert e.value.code == "APPOINTMENT_OVERLAP"


@pytest.mark.spec("D-27")
def test_ending_early_only_shortens_and_never_rewrites_used_history() -> None:
    p = Period(1, date(2026, 10, 1), date(2027, 9, 30))
    today = date(2027, 3, 15)
    check_new_end(p, date(2027, 3, 14), today, None)  # yesterday: fine (handover today)
    for new_end, last, code in [
        (date(2027, 9, 30), None, "APPOINTMENT_END_NOT_EARLIER"),
        (date(2027, 3, 13), None, "APPOINTMENT_END_TOO_EARLY"),  # before yesterday
        (date(2027, 3, 14), date(2027, 3, 15), "APPOINTMENT_END_TOO_EARLY"),  # used today
    ]:
        with pytest.raises(AppointmentRuleError) as e:
            check_new_end(p, new_end, today, last)
        assert e.value.code == code


@pytest.mark.spec("D-28", "AT-40.9.1", "AT-40.9.3", "AT-40.9.4")
@pytest.mark.parametrize(
    ("status", "fy", "start", "end", "day", "ok"),
    [
        ("ACTIVE", 2570, date(2026, 10, 1), date(2027, 9, 30), date(2026, 10, 15), True),
        ("REVOKED", 2570, date(2026, 10, 1), date(2027, 9, 30), date(2026, 10, 15), False),
        # previous year's head cannot verify in the new fiscal year
        ("ACTIVE", 2569, date(2025, 10, 1), date(2026, 9, 30), date(2026, 10, 1), False),
        # mid-year change: before the start, after the end
        ("ACTIVE", 2570, date(2027, 4, 1), date(2027, 9, 30), date(2027, 3, 31), False),
        ("ACTIVE", 2570, date(2026, 10, 1), date(2027, 3, 31), date(2027, 4, 1), False),
        # an appointment of another fiscal year is never used, even if the dates matched
        ("ACTIVE", 2571, date(2026, 10, 1), date(2027, 9, 30), date(2026, 10, 15), False),
    ],
)
def test_authority_is_judged_on_the_verification_date(
    status: str, fy: int, start: date, end: date, day: date, ok: bool
) -> None:
    assert authorizes(status, fy, start, end, day, CFG) is ok
