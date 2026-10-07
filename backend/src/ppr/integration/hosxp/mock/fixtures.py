"""Sample mock HOSxP master data for tests and local development.

All identifiers are ``MOCK-*``; nothing here describes real HOSxP codes.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ppr.integration.hosxp.contracts import (
    BudgetCategory,
    Department,
    FundSource,
    Item,
    PrHeader,
    PrItem,
)
from ppr.integration.hosxp.mock.gateway import MockHosxpData
from ppr.integration.hosxp.status import HosxpPrStatus


def _pr(
    pr_no: str,
    budget_year: int,
    lines: list[tuple[str, str, str]],
    *,
    total: str | None = None,
) -> tuple[PrHeader, list[PrItem]]:
    items = [
        PrItem(
            pr_item_id=f"{pr_no}-{n}",
            item_id=item,
            qty=Decimal(q),
            unit_price=Decimal(p),
            amount=Decimal(q) * Decimal(p),
        )
        for n, (item, q, p) in enumerate(lines, 1)
    ]
    header = PrHeader(
        pr_id=f"MOCK-ID-{pr_no}",
        pr_no=pr_no,
        pr_date=date(2026, 10, 15),
        fiscal_year=budget_year,
        department_id="MOCK-DEP-ENT",
        fund_source_id="MOCK-FUND-1",
        budget_category_id="MOCK-CAT-OFFICE",
        total_amount=Decimal(total) if total is not None else sum(i.amount for i in items),
        status=HosxpPrStatus.ACTIVE,
        # A mock native value (not a HOSxP code): a normalized status always comes from
        # a native value, as with the real adapter's mapping.
        native_status="MOCK-ACTIVE",
    )
    return header, items


def sample_prs() -> list[tuple[PrHeader, list[PrItem]]]:
    """Sample PRs for local trials of Pre-PPR validation (FY2570, Mock ENT, fund 1).

    * MOCK-PR-0001 - ordinary PR (header total = sum of items)
    * MOCK-PR-0002 - header total differs from the sum of items (D-17 -> blocked)
    * MOCK-PR-0003 - budget year 2569, i.e. an expired plan year (D-18 -> blocked)
    """
    return [
        _pr(
            "MOCK-PR-0001",
            2570,
            [("MOCK-ITEM-TONER", "2", "100.00"), ("MOCK-ITEM-PAPER", "5", "50.00")],
        ),
        _pr("MOCK-PR-0002", 2570, [("MOCK-ITEM-TONER", "1", "100.00")], total="107.00"),
        _pr("MOCK-PR-0003", 2569, [("MOCK-ITEM-TONER", "1", "100.00")]),
    ]


def sample_data() -> MockHosxpData:
    prs = sample_prs()
    return MockHosxpData(
        prs={h.pr_no: h for h, _ in prs},
        pr_items={h.pr_no: items for h, items in prs},
        fund_sources=[
            FundSource(source_id="MOCK-FUND-1", code="F1", name="Mock fund 1", active=True),
            FundSource(source_id="MOCK-FUND-2", code="F2", name="Mock fund 2", active=True),
        ],
        budget_categories=[
            BudgetCategory(source_id="MOCK-CAT-OPEX", code="C0", name="Operating", active=True),
            BudgetCategory(
                source_id="MOCK-CAT-MAT",
                code="C1",
                name="Materials",
                parent_source_id="MOCK-CAT-OPEX",
                active=True,
            ),
            BudgetCategory(
                source_id="MOCK-CAT-OFFICE",
                code="C2",
                name="Office supplies",
                parent_source_id="MOCK-CAT-MAT",
                active=True,
            ),
            BudgetCategory(
                source_id="MOCK-CAT-MED",
                code="C3",
                name="Medical supplies",
                parent_source_id="MOCK-CAT-MAT",
                active=True,
            ),
        ],
        departments=[
            Department(source_id="MOCK-DEP-ENT", code="ENT", name="Mock ENT", active=True),
            Department(source_id="MOCK-DEP-OR", code="OR", name="Mock OR", active=True),
            Department(source_id="MOCK-DEP-IT", code="IT", name="Mock IT", active=True),
            Department(source_id="MOCK-DEP-WARD-A", code="WA", name="Mock Ward A", active=True),
            Department(source_id="MOCK-DEP-WARD-B", code="WB", name="Mock Ward B", active=True),
            Department(source_id="MOCK-DEP-STORE", code="ST", name="Mock Store", active=True),
            Department(source_id="MOCK-DEP-OLD", code="OLD", name="Mock closed", active=False),
        ],
        items=[
            Item(
                source_id="MOCK-ITEM-TONER", code="I1", name="Mock toner", unit="box", active=True
            ),
            Item(
                source_id="MOCK-ITEM-PAPER", code="I2", name="Mock paper", unit="ream", active=True
            ),
            Item(
                source_id="MOCK-ITEM-GLOVE", code="I3", name="Mock gloves", unit="pack", active=True
            ),
            Item(
                source_id="MOCK-ITEM-OLD", code="I9", name="Mock retired", unit="each", active=False
            ),
        ],
    )
