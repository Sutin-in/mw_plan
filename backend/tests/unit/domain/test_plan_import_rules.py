"""Wave 12A-1 (D-42): cell reading of the plan import - exact, never rounded."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ppr.domain.plan_import import (
    ImportCode,
    MasterFacts,
    NumberError,
    SheetRow,
    check_import,
    number_of,
    text_of,
)


@pytest.mark.spec("D-42", "AT-40.17.2")
@pytest.mark.parametrize(
    ("cell", "expected"),
    [
        (None, None),
        ("", None),
        ("  ", None),
        (12, Decimal(12)),
        (0.1, Decimal("0.1")),
        (106837.5, Decimal("106837.5")),
        ("1,234.50", Decimal("1234.50")),
        (Decimal("2.25"), Decimal("2.25")),
    ],
)
def test_numbers_are_read_exactly(cell: object, expected: Decimal | None) -> None:
    assert number_of(cell) == expected


@pytest.mark.spec("D-42")
@pytest.mark.parametrize("cell", ["สิบ", "1e", True, float("nan"), "inf"])
def test_what_is_not_a_number_is_refused(cell: object) -> None:
    with pytest.raises(NumberError):
        number_of(cell)


@pytest.mark.spec("D-42")
def test_codes_keep_their_digits() -> None:
    assert text_of(31230.0) == "31230"
    assert text_of(45040) == "45040"
    assert text_of(" MOCK-1 ") == "MOCK-1"
    assert text_of(None) == ""


MASTERS = MasterFacts(
    departments={"D": True},
    fund_sources={"F": True},
    budget_categories={"C": True},
)


def _row(n: int, **cells: object) -> SheetRow:
    return SheetRow(n, cells)


@pytest.mark.spec("D-42", "AT-40.17.2")
def test_more_decimals_than_stored_is_an_error_not_a_rounding() -> None:
    head = [
        _row(
            2,
            **{
                "รหัสหัวข้อ": "H",
                "ประเภทแผน": "department",
                "รหัสหน่วยงานเจ้าของ": "D",
                "รหัสแหล่งเงิน": "F",
                "รหัสหมวดงบ": "C",
            },
        )
    ]
    rows = [
        _row(
            2,
            **{
                "รหัสหัวข้อ": "H",
                "ชื่อรายการ": "a",
                "หน่วยนับ": "u",
                "จำนวน": 1.00001,
                "ราคาต่อหน่วย": 1,
                "มูลค่า": 1,
            },
        ),
        _row(
            3,
            **{
                "รหัสหัวข้อ": "H",
                "ชื่อรายการ": "b",
                "หน่วยนับ": "u",
                "จำนวน": 1,
                "ราคาต่อหน่วย": 0.333,
                "มูลค่า": 0.33,
            },
        ),
    ]
    rep = check_import(head, rows, MASTERS, [(1, "F", "C", Decimal(2))])
    assert [(e.code, e.row) for e in rep.errors] == [
        (ImportCode.TOO_PRECISE, 2),
        (ImportCode.TOO_PRECISE, 3),
    ]
    assert rep.rows == ()


@pytest.mark.spec("D-42", "AT-40.17.2")
def test_a_formula_result_reads_as_shown_and_huge_values_are_never_shortened() -> None:
    assert number_of(3 * 1.1) == Decimal("3.3")  # stored as 3.3000000000000003
    assert number_of(0.1 + 0.2) == Decimal("0.3")
    big = number_of(1234567890123.456)  # more than 15 digits: kept, not cut
    assert big is not None and big != Decimal("1234567890123.46")
