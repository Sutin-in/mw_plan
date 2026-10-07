"""``HosxpGateway`` port (spec §42).

Method names are this system's conceptual interface. They do NOT describe real
HOSxP endpoints (spec §42: "Do not infer real endpoint names from this interface").
Implementations: ``mock.MockHosxpGateway`` now; ``real`` only after verified evidence.

Every method raises ``HosxpUnavailableError`` when HOSxP cannot be reached.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Protocol, runtime_checkable

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


@runtime_checkable
class HosxpGateway(Protocol):
    def get_pr(self, pr_no: str) -> PrHeader | None:
        """Live PR header, or None if the PR does not exist."""
        ...

    def get_pr_items(self, pr_no: str) -> Sequence[PrItem]:
        """ALL items of the PR (spec §12)."""
        ...

    def get_departments(self) -> Sequence[Department]: ...

    def get_items(self) -> Sequence[Item]: ...

    def get_fund_sources(self) -> Sequence[FundSource]: ...

    def get_budget_categories(self) -> Sequence[BudgetCategory]: ...

    def get_budget_snapshot(
        self, fund_source_id: str, budget_category_id: str
    ) -> BudgetSnapshot | None: ...

    # D-39: stock movements are read for the Central Plan Usage report only.
    def stock_movement_source(self) -> StockMovementSource:
        """Is a verified stock-movement query configured? (no HOSxP call)"""
        ...

    def get_stock_movements(self, date_from: date, date_to: date) -> Sequence[StockMovement]:
        """Issues and returns dated ``date_from`` .. ``date_to`` (inclusive). Raises
        ``HosxpConfigurationError`` when the source is not usable."""
        ...
