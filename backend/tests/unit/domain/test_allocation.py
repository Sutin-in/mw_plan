"""Header-level category allocation, PPR 1 -> N rows (spec §17, §40.3, D-04)."""

from __future__ import annotations

import ast
import inspect
from decimal import Decimal

import pytest

import ppr.domain.allocation as allocation_module
from ppr.domain.allocation import CategoryAllocation, is_multi_category, validate_allocations
from ppr.domain.violations import ViolationCode, codes

D = Decimal
FUND = "MOCK-FUND-BAMRUNG"
CATS = {"MOCK-CAT-A": FUND, "MOCK-CAT-B": FUND, "MOCK-CAT-OTHER": "MOCK-FUND-OTHER"}


def check(*rows: tuple[str, int], required: int = 100000) -> set[ViolationCode]:
    allocs = [CategoryAllocation(c, D(a)) for c, a in rows]
    return codes(
        validate_allocations(
            allocs, pr_fund_source_id=FUND, category_fund_source=CATS, required_amount=D(required)
        )
    )


@pytest.mark.spec("AT-40.3.1")
def test_single_category_same_fund_passes() -> None:
    assert check(("MOCK-CAT-A", 100000)) == set()


@pytest.mark.spec("AT-40.3.2", "D-04", "S-17")
def test_spec_example_70k_plus_30k_under_same_fund_passes() -> None:
    assert check(("MOCK-CAT-A", 70000), ("MOCK-CAT-B", 30000)) == set()


@pytest.mark.spec("AT-40.3.3", "S-17")
def test_cross_fund_category_rejected() -> None:
    assert check(("MOCK-CAT-A", 70000), ("MOCK-CAT-OTHER", 30000)) == {
        ViolationCode.ALLOCATION_CROSS_FUND
    }


@pytest.mark.spec("AT-40.3.4", "S-17")
@pytest.mark.parametrize("second", [29999, 30001])
def test_sum_not_equal_required_rejected(second: int) -> None:
    assert check(("MOCK-CAT-A", 70000), ("MOCK-CAT-B", second)) == {
        ViolationCode.ALLOCATION_SUM_MISMATCH
    }


@pytest.mark.spec("AT-40.3.7", "D-04")
def test_same_category_twice_rejected_even_if_sum_matches() -> None:
    assert check(("MOCK-CAT-A", 50000), ("MOCK-CAT-A", 50000)) == {
        ViolationCode.ALLOCATION_DUPLICATE_CATEGORY
    }


@pytest.mark.spec("AT-40.3.5")
def test_multi_category_flag_for_display_and_audit() -> None:
    one = [CategoryAllocation("MOCK-CAT-A", D(1))]
    two = [CategoryAllocation("MOCK-CAT-A", D(1)), CategoryAllocation("MOCK-CAT-B", D(1))]
    assert not is_multi_category(one)
    assert is_multi_category(two)


@pytest.mark.spec("AT-40.3.6", "AT-40.3.8", "D-04")
def test_allocation_is_header_level_and_independent_of_the_ledger() -> None:
    fields = {f for f in CategoryAllocation.__dataclass_fields__}
    assert fields == {"budget_category_id", "amount"}  # no PR item, no plan item
    tree = ast.parse(inspect.getsource(allocation_module))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any("ledger" in m for m in imported)  # cannot post Plan Ledger usage


def test_empty_unknown_and_non_positive_rejected() -> None:
    assert check() == {ViolationCode.ALLOCATION_EMPTY}
    assert ViolationCode.ALLOCATION_UNKNOWN_CATEGORY in check(("MOCK-CAT-Z", 100000))
    assert ViolationCode.ALLOCATION_NON_POSITIVE in check(("MOCK-CAT-A", 100000), ("MOCK-CAT-B", 0))
