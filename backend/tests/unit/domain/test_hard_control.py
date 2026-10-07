"""Hard control: quantity AND amount (spec §13, §40.2)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ppr.domain.hard_control import Remaining, check_hard_control
from ppr.domain.violations import ViolationCode, codes

D = Decimal
REM = Remaining(D(10), D(30000))


@pytest.mark.spec("AT-40.2.1", "S-13")
def test_both_within_remaining_passes() -> None:
    assert check_hard_control(D(5), D(20000), REM) == ()


@pytest.mark.spec("AT-40.2.2", "S-13")
def test_quantity_exceeded_blocks_even_if_amount_fits() -> None:
    # spec §13 example: remaining 10 / 30,000, request 11 / 20,000
    assert codes(check_hard_control(D(11), D(20000), REM)) == {ViolationCode.QTY_EXCEEDED}


@pytest.mark.spec("AT-40.2.3", "S-13")
def test_amount_exceeded_blocks_even_if_quantity_fits() -> None:
    # spec §13 example: remaining 10 / 30,000, request 5 / 40,000
    assert codes(check_hard_control(D(5), D(40000), REM)) == {ViolationCode.AMOUNT_EXCEEDED}


@pytest.mark.spec("AT-40.2.4")
def test_quantity_exactly_remaining_passes_when_amount_passes() -> None:
    assert check_hard_control(D(10), D(1), REM) == ()
    assert codes(check_hard_control(D(10), D(30001), REM)) == {ViolationCode.AMOUNT_EXCEEDED}


@pytest.mark.spec("AT-40.2.5")
def test_amount_exactly_remaining_passes_when_quantity_passes() -> None:
    assert check_hard_control(D(1), D(30000), REM) == ()
    assert codes(check_hard_control(D(11), D(30000), REM)) == {ViolationCode.QTY_EXCEEDED}


@pytest.mark.spec("AT-40.2.6", "S-9.3")
def test_unit_price_differing_from_estimate_is_not_a_constraint() -> None:
    # Plan estimated 3,000/unit (10 x 3,000). PR buys 4 at 7,000 = 28,000: pricier per unit,
    # but both totals fit -> pass. The unit price is not even an input to the rule.
    assert check_hard_control(D(4), D(28000), REM) == ()


@pytest.mark.spec("AT-40.2.7", "S-13")
def test_quantity_exhausted_while_money_remains_blocks() -> None:
    rem = Remaining(D(0), D(30000))
    assert codes(check_hard_control(D(1), D(100), rem)) == {ViolationCode.QTY_EXHAUSTED}


@pytest.mark.spec("AT-40.2.8", "S-13")
def test_amount_exhausted_while_quantity_remains_blocks() -> None:
    rem = Remaining(D(10), D(0))
    assert codes(check_hard_control(D(1), D(100), rem)) == {ViolationCode.AMOUNT_EXHAUSTED}


@pytest.mark.parametrize("qty", [D(0), D(-1), D("NaN"), D("Infinity")])
def test_invalid_quantity_rejected(qty: Decimal) -> None:
    assert ViolationCode.INVALID_REQUESTED_QTY in codes(check_hard_control(qty, D(1), REM))


def test_negative_amount_rejected() -> None:
    assert ViolationCode.INVALID_REQUESTED_AMOUNT in codes(check_hard_control(D(1), D(-1), REM))


_dec = st.decimals(min_value=0, max_value=10**9, places=2, allow_nan=False, allow_infinity=False)


@pytest.mark.spec("S-13")
@given(req_q=_dec, req_a=_dec, rem_q=_dec, rem_a=_dec)
def test_property_passes_iff_both_limits_hold(
    req_q: Decimal, req_a: Decimal, rem_q: Decimal, rem_a: Decimal
) -> None:
    result = check_hard_control(req_q, req_a, Remaining(rem_q, rem_a))
    if req_q <= 0:
        assert ViolationCode.INVALID_REQUESTED_QTY in codes(result)
        return
    assert (result == ()) == (req_q <= rem_q and req_a <= rem_a)
