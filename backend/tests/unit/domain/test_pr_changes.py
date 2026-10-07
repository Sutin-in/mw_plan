"""PR change classifier shared by draft confirmation (D-21) and sync (D-10): §16, §40.7."""

from __future__ import annotations

from typing import Any

import pytest

from ppr.domain.pr_changes import critical_only, diff_pr, identity_changes

HEADER: dict[str, Any] = {
    "pr_no": "PR-1",
    "pr_date": "2026-10-15",
    "fiscal_year": 2570,
    "department_id": "ENT",
    "fund_source_id": "F1",
    "budget_category_id": "OFFICE",
    "requester_name": "A",
    "total_amount": "500.00",
    "status": "ACTIVE",
    "native_status": "A1",
}
LINES: list[dict[str, Any]] = [
    {
        "pr_item_id": "L1",
        "item_id": "TONER",
        "item_name": "Toner",
        "unit": "box",
        "qty": "4",
        "unit_price": "100",
        "amount": "400.00",
    },
    {
        "pr_item_id": "L2",
        "item_id": "PAPER",
        "item_name": "Paper",
        "unit": "ream",
        "qty": "2",
        "unit_price": "50",
        "amount": "100.00",
    },
]


def _diff(header: dict[str, Any] | None = None, lines: list[dict[str, Any]] | None = None) -> Any:
    return diff_pr(
        HEADER, LINES, {**HEADER, **(header or {})}, lines if lines is not None else LINES
    )


def _line(n: int, **over: Any) -> list[dict[str, Any]]:
    out = [dict(x) for x in LINES]
    out[n].update(over)
    return out


def test_identical_prs_have_no_changes() -> None:
    assert _diff() == ()


def test_numbers_are_compared_by_value_not_text() -> None:
    assert _diff({"total_amount": "500"}, _line(0, qty="4.0000", unit_price="100.00000000")) == ()


@pytest.mark.spec("D-10", "D-21", "S-16", "AT-40.7.4", "AT-40.7.5", "AT-40.7.6")
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fiscal_year", 2569),
        ("department_id", "OR"),
        ("fund_source_id", "F2"),
        ("budget_category_id", "MED"),
        ("total_amount", "499.00"),
    ],
)
def test_critical_header_fields(field: str, value: Any) -> None:
    [c] = _diff({field: value})
    assert (c.field, c.critical) == (field, True)


@pytest.mark.spec("D-21", "S-16")
@pytest.mark.parametrize(
    ("field", "value"),
    [("pr_date", "2026-10-16"), ("requester_name", "B"), ("native_status", "A2")],
)
def test_metadata_header_fields_are_not_critical(field: str, value: Any) -> None:
    [c] = _diff({field: value})
    assert c.critical is False


@pytest.mark.spec("D-10", "D-21", "AT-40.7.6")
@pytest.mark.parametrize(
    ("before", "after", "critical"),
    [("ACTIVE", "APPROVED", False), ("ACTIVE", "CANCELLED", True), ("CANCELLED", "ACTIVE", True)],
)
def test_status_is_critical_only_where_it_affects_validity(
    before: str, after: str, critical: bool
) -> None:
    [c] = diff_pr({**HEADER, "status": before}, LINES, {**HEADER, "status": after}, LINES)
    assert c.critical is critical


@pytest.mark.spec("D-10", "D-21", "AT-40.7.1", "AT-40.7.2", "AT-40.7.3")
@pytest.mark.parametrize(
    ("field", "value"), [("item_id", "GLOVE"), ("qty", "5"), ("amount", "401.00")]
)
def test_critical_line_fields(field: str, value: str) -> None:
    assert critical_only(_diff(lines=_line(0, **{field: value})))


@pytest.mark.spec("D-10", "D-21")
def test_unit_price_is_critical_only_with_a_quantity_or_amount_change() -> None:
    [alone] = _diff(lines=_line(0, unit_price="99.99"))
    assert (alone.field, alone.critical) == ("unit_price", False)
    both = _diff(lines=_line(0, unit_price="99", amount="396.00"))
    assert {(c.field, c.critical) for c in both} == {("unit_price", True), ("amount", True)}


@pytest.mark.spec("D-21")
@pytest.mark.parametrize(("field", "value"), [("item_name", "Toner XL"), ("unit", "pack")])
def test_line_labels_are_not_critical(field: str, value: str) -> None:
    [c] = _diff(lines=_line(1, **{field: value}))
    assert (c.subject, c.critical) == ("L2", False)


@pytest.mark.spec("D-10", "D-21", "AT-40.7.3")
def test_lines_added_or_removed_are_critical() -> None:
    added = [*LINES, {**LINES[0], "pr_item_id": "L3"}]
    assert [(c.subject, c.after, c.critical) for c in _diff(lines=added)] == [("L3", "added", True)]
    assert [(c.subject, c.after) for c in _diff(lines=LINES[:1])] == [("L2", "removed")]


def test_line_order_does_not_matter() -> None:
    assert _diff(lines=list(reversed(LINES))) == ()


@pytest.mark.spec("D-22")
def test_identity_changes_are_fiscal_year_department_and_fund_only() -> None:
    changes = _diff({"department_id": "OR", "budget_category_id": "MED", "fiscal_year": 2571})
    assert sorted(c.field for c in identity_changes(changes)) == ["department_id", "fiscal_year"]
