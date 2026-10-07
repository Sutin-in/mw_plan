"""``MockHosxpGateway``: in-memory implementation of the ``HosxpGateway`` port."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from ppr.integration.hosxp.contracts import (
    BudgetCategory,
    BudgetSnapshot,
    Department,
    FundSource,
    Item,
    PrHeader,
    PrItem,
    StockMovement,
    StockMovementSource,
)
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpUnavailableError


@dataclass
class MockHosxpData:
    departments: list[Department] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    fund_sources: list[FundSource] = field(default_factory=list)
    budget_categories: list[BudgetCategory] = field(default_factory=list)
    prs: dict[str, PrHeader] = field(default_factory=dict)  # by pr_no
    pr_items: dict[str, list[PrItem]] = field(default_factory=dict)  # by pr_no
    budget_snapshots: list[BudgetSnapshot] = field(default_factory=list)
    # D-39: MOCK stock movements, shown only when ``stock_source`` is usable (a test or the
    # demonstration marks it so; by default there is no verified query, as on a new site).
    stock_movements: list[StockMovement] = field(default_factory=list)
    stock_source: StockMovementSource = field(default_factory=StockMovementSource)


class MockHosxpGateway:
    """Deterministic, scriptable HOSxP stand-in.

    * ``available = False`` simulates downtime: every call raises HosxpUnavailableError.
    * ``calls`` records method names, so tests can assert live retrieval happened.
    * ``replace_pr`` simulates a PR being changed/cancelled in HOSxP (for sync tests).
    """

    def __init__(self, data: MockHosxpData | None = None) -> None:
        self.data = data or MockHosxpData()
        self.available = True
        self.calls: list[str] = []

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if not self.available:
            raise HosxpUnavailableError("mock HOSxP is unavailable")

    # -- scripting helpers (not part of the port) ---------------------------------
    def replace_pr(self, header: PrHeader, items: Sequence[PrItem]) -> None:
        self.data.prs[header.pr_no] = header
        self.data.pr_items[header.pr_no] = list(items)

    # -- HosxpGateway -----------------------------------------------------------
    def get_pr(self, pr_no: str) -> PrHeader | None:
        self._enter("get_pr")
        return self.data.prs.get(pr_no)

    def get_pr_items(self, pr_no: str) -> Sequence[PrItem]:
        self._enter("get_pr_items")
        return tuple(self.data.pr_items.get(pr_no, ()))

    def get_departments(self) -> Sequence[Department]:
        self._enter("get_departments")
        return tuple(self.data.departments)

    def get_items(self) -> Sequence[Item]:
        self._enter("get_items")
        return tuple(self.data.items)

    def get_fund_sources(self) -> Sequence[FundSource]:
        self._enter("get_fund_sources")
        return tuple(self.data.fund_sources)

    def get_budget_categories(self) -> Sequence[BudgetCategory]:
        self._enter("get_budget_categories")
        return tuple(self.data.budget_categories)

    def get_budget_snapshot(
        self, fund_source_id: str, budget_category_id: str
    ) -> BudgetSnapshot | None:
        self._enter("get_budget_snapshot")
        for s in self.data.budget_snapshots:
            if s.fund_source_id == fund_source_id and s.budget_category_id == budget_category_id:
                return s
        return None

    def stock_movement_source(self) -> StockMovementSource:
        return self.data.stock_source

    def get_stock_movements(self, date_from: date, date_to: date) -> Sequence[StockMovement]:
        self._enter("get_stock_movements")
        source = self.data.stock_source
        if not source.usable:
            raise HosxpConfigurationError(f"stock movements: {source.problem}")
        return tuple(
            m for m in self.data.stock_movements if date_from <= m.movement_date <= date_to
        )
