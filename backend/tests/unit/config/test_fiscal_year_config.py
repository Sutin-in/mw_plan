"""Fiscal-year boundaries come from configuration (D-08)."""

from __future__ import annotations

import pytest

from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR, load_fiscal_year_config


@pytest.mark.spec("D-08")
def test_default_when_nothing_configured() -> None:
    assert load_fiscal_year_config({}) == DEFAULT_FISCAL_YEAR


@pytest.mark.spec("D-08")
def test_override_from_environment() -> None:
    cfg = load_fiscal_year_config(
        {
            "PPR_FY_START_MONTH": "1",
            "PPR_FY_START_DAY": "1",
            "PPR_FY_ERA_OFFSET": "0",
            "PPR_FY_LABEL": "start_year",
        }
    )
    assert (cfg.start_month, cfg.start_day, cfg.era_offset, cfg.label) == (1, 1, 0, "START_YEAR")


def test_partial_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="incomplete"):
        load_fiscal_year_config({"PPR_FY_START_MONTH": "4"})


def test_invalid_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="PPR_FY_LABEL"):
        load_fiscal_year_config(
            {
                "PPR_FY_START_MONTH": "1",
                "PPR_FY_START_DAY": "1",
                "PPR_FY_ERA_OFFSET": "0",
                "PPR_FY_LABEL": "MIDDLE",
            }
        )
