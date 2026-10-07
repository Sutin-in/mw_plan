"""Fiscal-year configuration loading (D-08: boundaries are configuration).

Keys (environment or any string mapping):
    PPR_FY_START_MONTH, PPR_FY_START_DAY, PPR_FY_ERA_OFFSET, PPR_FY_LABEL

All four must be provided together; if none are provided, the documented
deployment default is used. The default is a deployment setting, not a domain
rule, and can be overridden without code changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ppr.domain.fiscal_year import FiscalYearConfig, LabelRule

# Deployment default: Thai government fiscal year (1 October - 30 September),
# labeled by the Buddhist-Era year in which it ends (e.g. FY2570 = 2026-10-01..2027-09-30).
DEFAULT_FISCAL_YEAR = FiscalYearConfig(
    start_month=10, start_day=1, era_offset=543, label="END_YEAR"
)

_KEYS = ("PPR_FY_START_MONTH", "PPR_FY_START_DAY", "PPR_FY_ERA_OFFSET", "PPR_FY_LABEL")


def load_fiscal_year_config(env: Mapping[str, str]) -> FiscalYearConfig:
    present = [k for k in _KEYS if k in env]
    if not present:
        return DEFAULT_FISCAL_YEAR
    missing = [k for k in _KEYS if k not in env]
    if missing:
        raise ValueError(f"incomplete fiscal-year configuration; missing {missing}")
    label = env["PPR_FY_LABEL"].strip().upper()
    if label not in ("END_YEAR", "START_YEAR"):
        raise ValueError(f"PPR_FY_LABEL must be END_YEAR or START_YEAR, got {label!r}")
    return FiscalYearConfig(
        start_month=int(env["PPR_FY_START_MONTH"]),
        start_day=int(env["PPR_FY_START_DAY"]),
        era_offset=int(env["PPR_FY_ERA_OFFSET"]),
        label=cast(LabelRule, label),
    )
