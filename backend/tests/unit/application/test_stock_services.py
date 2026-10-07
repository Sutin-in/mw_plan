"""Reading stock movements for the report (D-39): fail closed, never guess, never write."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from ppr.application.stock_services import StockTotals, read_stock
from ppr.integration.hosxp.contracts import (
    StockMovement,
    StockMovementKind,
    StockMovementSource,
    StockVerification,
)
from ppr.integration.hosxp.mock import MockHosxpData, MockHosxpGateway

D = Decimal
SRC = Path(__file__).resolve().parents[3] / "src" / "ppr"
SOURCE = StockMovementSource(
    problem=None,
    verification=StockVerification("MOCK IT", date(2026, 10, 1), "MOCK-REF"),
    kind_map=MappingProxyType(
        {"MOCK-ISSUE": StockMovementKind.ISSUE, "MOCK-RETURN": StockMovementKind.RETURN}
    ),
)


def m(i: str, d: date, kind: str = "MOCK-ISSUE", qty: str = "1") -> StockMovement:
    return StockMovement(
        movement_id=i,
        movement_date=d,
        department_id="MOCK-DEP-ENT",
        item_id="MOCK-ITEM-A",
        qty=D(qty),
        native_kind=kind,
    )


class RawGateway(MockHosxpGateway):
    """Returns whatever it holds, as a wrong site query would (no date filter)."""

    def get_stock_movements(self, date_from: date, date_to: date) -> Sequence[StockMovement]:
        self._enter("get_stock_movements")
        return tuple(self.data.stock_movements)


def gw(*moves: StockMovement, raw: bool = False) -> MockHosxpGateway:
    data = MockHosxpData(stock_movements=list(moves), stock_source=SOURCE)
    return RawGateway(data) if raw else MockHosxpGateway(data)


OCT1, OCT31 = date(2026, 10, 1), date(2026, 10, 31)


@pytest.mark.spec("AT-40.12.1", "D-39")
def test_totals_per_item_and_department() -> None:
    got = read_stock(
        gw(m("1", OCT1, qty="5"), m("2", OCT1, "MOCK-RETURN", "2"), m("3", OCT31, qty="1")),
        OCT1,
        OCT31,
    )
    t = got.totals[("MOCK-ITEM-A", "MOCK-DEP-ENT")]
    assert (t, t.net_used) == (StockTotals(D(6), D(2)), D(4))
    assert (
        got.available
        and got.unmapped == 0
        and got.departments("MOCK-ITEM-A") == {"MOCK-DEP-ENT": t}
    )


@pytest.mark.spec("AT-40.12.5", "D-39")
@pytest.mark.parametrize(
    "moves",
    [
        (m("1", OCT1), m("1", OCT1)),  # the same movement twice would be counted twice
        (m("1", date(2026, 9, 30)),),  # outside the asked range: the query is wrong
    ],
)
def test_data_that_breaks_the_contract_is_not_shown(moves: tuple[StockMovement, ...]) -> None:
    got = read_stock(gw(*moves, raw=True), OCT1, OCT31)
    assert not got.available and "ไม่ตรงตามที่กำหนด" in (got.reason or "")
    assert dict(got.totals) == {}


@pytest.mark.spec("D-39")
def test_unmapped_kinds_make_figures_unknown_not_zero() -> None:
    got = read_stock(gw(m("1", OCT1), m("2", OCT1, "MOCK-TRANSFER", "7")), OCT1, OCT31)
    assert got.unmapped == 1
    # Unknown is never coerced to zero: the department's and the item's figures are unknown.
    assert got.departments("MOCK-ITEM-A") == {"MOCK-DEP-ENT": None}
    assert got.item_total("MOCK-ITEM-A") is None
    assert got.item_total("MOCK-ITEM-OTHER") == StockTotals()  # no movement: a known zero
    assert got.totals[("MOCK-ITEM-A", "MOCK-DEP-ENT")] == StockTotals(D(1), D(0))


@pytest.mark.spec("D-39")
def test_before_the_year_starts_nothing_is_read() -> None:
    g = gw(m("1", OCT1))
    got = read_stock(g, OCT31, OCT1)
    assert got.available and dict(got.totals) == {} and "get_stock_movements" not in g.calls


@pytest.mark.spec("AT-40.12.2", "AT-40.12.7", "D-39")
def test_stock_reading_has_no_path_to_the_ledger_or_to_storage() -> None:
    """Static check of the source: stock movements are read in exactly one application module,
    which imports nothing that writes (no ledger, no repository, no unit of work), and only
    the report and the read-only ``hosxp-verify`` check use it."""
    tree = ast.parse((SRC / "application" / "stock_services.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not {i for i in imported if "ledger" in i or "infrastructure" in i or i == "ppr.ports"}
    users = sorted(
        p.relative_to(SRC).as_posix()  # same text on Windows and Linux
        for p in SRC.rglob("*.py")
        if "get_stock_movements" in p.read_text(encoding="utf-8")
        or "read_stock" in p.read_text(encoding="utf-8")
    )
    assert users == [
        "application/report_services.py",
        "application/stock_services.py",
        "integration/hosxp/gateway.py",
        "integration/hosxp/mock/gateway.py",
        "integration/hosxp/real/query_config.py",
        "integration/hosxp/real/sql_gateway.py",
        "ops_hosxp_verify.py",  # Wave 10B-2: read-only check for IT, prints counts only
    ]
