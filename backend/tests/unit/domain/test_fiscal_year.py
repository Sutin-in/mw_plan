"""Fiscal-year rules and PPR numbering (spec §7, D-06, D-08)."""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ppr.domain.fiscal_year import (
    FiscalYearConfig,
    PlanYearState,
    can_transition_plan_year,
    fiscal_year_bounds,
    fiscal_year_label,
    new_ppr_allowed,
)
from ppr.domain.ppr_number import (
    FiscalYearSequencer,
    format_ppr_number,
    parse_ppr_number,
)

THAI = FiscalYearConfig(start_month=10, start_day=1, era_offset=543, label="END_YEAR")
CALENDAR_CE = FiscalYearConfig(start_month=1, start_day=1, era_offset=0, label="END_YEAR")
APRIL_START = FiscalYearConfig(start_month=4, start_day=1, era_offset=0, label="START_YEAR")


@pytest.mark.spec("D-08")
@pytest.mark.parametrize(
    ("cfg", "day", "label"),
    [
        (THAI, date(2026, 9, 30), 2569),
        (THAI, date(2026, 10, 1), 2570),
        (THAI, date(2027, 9, 30), 2570),
        (CALENDAR_CE, date(2026, 12, 31), 2026),
        (CALENDAR_CE, date(2027, 1, 1), 2027),
        (APRIL_START, date(2027, 3, 31), 2026),
        (APRIL_START, date(2027, 4, 1), 2027),
    ],
)
def test_fiscal_year_label_follows_configuration(
    cfg: FiscalYearConfig, day: date, label: int
) -> None:
    assert fiscal_year_label(day, cfg) == label


@pytest.mark.spec("D-08")
@pytest.mark.parametrize("cfg", [THAI, CALENDAR_CE, APRIL_START])
@given(day=st.dates(min_value=date(2000, 1, 1), max_value=date(2090, 12, 31)))
def test_property_bounds_contain_day_and_are_contiguous(cfg: FiscalYearConfig, day: date) -> None:
    label = fiscal_year_label(day, cfg)
    first, last = fiscal_year_bounds(label, cfg)
    assert first <= day <= last
    assert fiscal_year_label(first, cfg) == label == fiscal_year_label(last, cfg)
    next_first, _ = fiscal_year_bounds(label + 1, cfg)
    assert (next_first - last).days == 1


@pytest.mark.parametrize(("month", "day"), [(2, 29), (13, 1), (4, 31), (0, 1)])
def test_invalid_fiscal_year_start_rejected(month: int, day: int) -> None:
    with pytest.raises(ValueError):
        FiscalYearConfig(start_month=month, start_day=day, era_offset=0, label="END_YEAR")


@pytest.mark.spec("S-7")
def test_plan_year_lifecycle_is_forward_only() -> None:
    s_ = PlanYearState
    assert can_transition_plan_year(s_.DRAFT, s_.APPROVED)
    assert can_transition_plan_year(s_.APPROVED, s_.ACTIVE)
    assert can_transition_plan_year(s_.ACTIVE, s_.CLOSED)
    assert not can_transition_plan_year(s_.CLOSED, s_.ACTIVE)  # no reopening
    assert not can_transition_plan_year(s_.DRAFT, s_.ACTIVE)  # must be approved first
    assert [s for s in s_ if new_ppr_allowed(s)] == [s_.ACTIVE]


@pytest.mark.spec("S-7", "D-06")
def test_ppr_number_format_matches_spec_example() -> None:
    assert format_ppr_number(2570, 1) == "PPR-2570-000001"
    assert parse_ppr_number("PPR-2570-000001") == (2570, 1)


@pytest.mark.parametrize(
    "bad", ["PPR-2570-1", "PPR-257-000001", "ppr-2570-000001", "PPR-2570-000000", "X"]
)
def test_malformed_ppr_numbers_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_ppr_number(bad)


@pytest.mark.parametrize(("fy", "seq"), [(999, 1), (2570, 0), (2570, 1_000_000)])
def test_out_of_range_numbers_rejected(fy: int, seq: int) -> None:
    with pytest.raises(ValueError):
        format_ppr_number(fy, seq)


@pytest.mark.spec("AT-40.13.4", "D-06")
def test_sequence_is_unique_and_resets_each_fiscal_year() -> None:
    seq = FiscalYearSequencer()
    a = [seq.next_number(2570) for _ in range(3)]
    assert a == ["PPR-2570-000001", "PPR-2570-000002", "PPR-2570-000003"]
    assert seq.next_number(2571) == "PPR-2571-000001"  # new year resets
    assert seq.next_number(2570) == "PPR-2570-000004"  # old year continues independently
    assert len(set(a)) == len(a)
