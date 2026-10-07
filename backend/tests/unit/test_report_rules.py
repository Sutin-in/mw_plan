"""Report helpers (D-35): hospital-time day boundaries and report states."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ppr.application import report_services as rs
from ppr.domain.ppr_state import PprState


@pytest.mark.spec("D-35")
def test_days_start_at_midnight_hospital_time_whatever_the_server_zone() -> None:
    start = rs._day_start(date(2026, 10, 1))
    assert start.utcoffset() == timedelta(hours=7)
    # 06:00 in Bangkok on 1 October is 23:00 UTC on 30 September - still 1 October.
    confirmed = datetime(2026, 10, 1, 6, 0, tzinfo=rs.HOSPITAL_TZ)
    assert confirmed.astimezone(UTC).date() == date(2026, 9, 30)
    assert start <= confirmed < rs._day_start(date(2026, 10, 2))


@pytest.mark.spec("D-35")
def test_report_states_follow_the_decision() -> None:
    assert rs.states_of("PENDING_PPR") == (
        PprState.DRAFT,
        PprState.CONFIRMED_LOCKED,
        PprState.UNLOCKED_FOR_REVISION,
        PprState.PR_CHANGED_REVIEW_REQUIRED,
    )
    assert PprState.DRAFT not in rs.states_of("PPR_REGISTER")
    assert rs.states_of("CANCELLED_PPR") == (PprState.CANCELLED,)
    assert rs.states_of("PLAN_SUMMARY") == ()
    assert len(rs.REPORTS) == 11  # the nine of D-35 + Plan Amendment (D-37) + Central (D-38)
    assert rs.states_of("PLAN_AMENDMENT") == ()
