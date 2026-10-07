"""Stock issues and returns of the central store, for the *Central Plan Usage* report (D-39).

HOSxP is the source of truth for stock movements. This module only READS them, live, each
time a report is built, through the site's verified query, and totals them per HOSxP item
and department. It writes nothing: no repository, no Plan Ledger, no audit, no cache - so
running it any number of times changes no plan or PPR data, and there is no stock ledger here.

Fail closed: without a verified query, when HOSxP cannot be reached, when the query fails
or when what it returns breaks the contract (a movement twice, a date outside the asked
range), the result is "not available" with a reason, and the report shows "ยังไม่มีข้อมูล".
Unknown is never coerced to zero: a movement of a kind the site has not mapped could be an
issue or a return, so the figures of that HOSxP item and department become unknown (not
"counted without it"). Nothing is guessed: no kind, no fund source, no split.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from ppr.integration.hosxp.contracts import StockMovementKind, StockVerification
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpError
from ppr.integration.hosxp.gateway import HosxpGateway

ZERO = Decimal(0)

NO_DATA = "ยังไม่มีข้อมูล"
UNREACHABLE = "เชื่อมต่อ HOSxP ไม่ได้ขณะสร้างรายงาน"
QUERY_FAILED = "query ยอดเบิก/คืนจาก HOSxP ทำงานไม่สำเร็จ"
BAD_DATA = "ข้อมูลยอดเบิก/คืนจาก HOSxP ไม่ตรงตามที่กำหนด"


@dataclass(frozen=True)
class StockTotals:
    """Issued and returned quantity of one HOSxP item for one department (or all)."""

    issued: Decimal = ZERO
    returned: Decimal = ZERO

    @property
    def net_used(self) -> Decimal:
        """D-39: used = issued to the department - returned by it."""
        return self.issued - self.returned

    def __add__(self, other: StockTotals) -> StockTotals:
        return StockTotals(self.issued + other.issued, self.returned + other.returned)


@dataclass(frozen=True)
class StockRead:
    available: bool
    reason: str | None = None  # why not available (Thai)
    date_from: date | None = None
    date_to: date | None = None
    verification: StockVerification | None = None
    returns_known: bool = False  # the site mapped a RETURN kind
    unmapped: int = 0  # movements of a kind the site has not mapped
    # (HOSxP item id, department id) -> totals of mapped movements
    totals: Mapping[tuple[str, str], StockTotals] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # (HOSxP item id, department id) with at least one unmapped movement: their figures are
    # unknown, never the mapped part alone
    unknown: frozenset[tuple[str, str]] = frozenset()

    def departments(self, item_id: str) -> dict[str, StockTotals | None]:
        """Department -> totals of one HOSxP item (None = unknown)."""
        out: dict[str, StockTotals | None] = {
            d: t for (i, d), t in self.totals.items() if i == item_id
        }
        out.update({d: None for (i, d) in self.unknown if i == item_id})
        return out

    def item_total(self, item_id: str) -> StockTotals | None:
        """All departments together; None if any part is unknown. No movement at all in a
        verified, complete read is a known zero."""
        total = StockTotals()
        for t in self.departments(item_id).values():
            if t is None:
                return None
            total = total + t
        return total


def not_available(reason: str) -> StockRead:
    return StockRead(available=False, reason=reason)


def read_stock(gateway: HosxpGateway, date_from: date, date_to: date) -> StockRead:
    """Totals of the central store's issues and returns dated ``date_from`` .. ``date_to``."""
    source = gateway.stock_movement_source()
    if not source.usable:
        return not_available(source.problem or NO_DATA)
    returns_known = StockMovementKind.RETURN in source.kind_map.values()
    if date_to < date_from:  # the fiscal year has not started: nothing can have moved yet
        return StockRead(True, None, date_from, date_to, source.verification, returns_known, 0)
    try:
        movements = gateway.get_stock_movements(date_from, date_to)
    except HosxpConfigurationError:
        return not_available(QUERY_FAILED)
    except HosxpError:
        return not_available(UNREACHABLE)

    seen: set[str] = set()
    totals: dict[tuple[str, str], StockTotals] = {}
    unmapped = 0
    unknown: set[tuple[str, str]] = set()
    for m in movements:
        if m.movement_id in seen:
            return not_available(BAD_DATA + " (เลขที่รายการซ้ำ)")
        seen.add(m.movement_id)
        if not date_from <= m.movement_date <= date_to:
            return not_available(BAD_DATA + " (มีรายการนอกช่วงวันที่)")
        kind = source.kind_map.get(m.native_kind)
        if kind is None:
            unmapped += 1
            unknown.add((m.item_id, m.department_id))
            continue
        add = (
            StockTotals(issued=m.qty)
            if kind is StockMovementKind.ISSUE
            else StockTotals(returned=m.qty)
        )
        key = (m.item_id, m.department_id)
        totals[key] = totals.get(key, StockTotals()) + add
    return StockRead(
        True,
        None,
        date_from,
        date_to,
        source.verification,
        returns_known,
        unmapped,
        MappingProxyType(totals),
        frozenset(unknown),
    )
