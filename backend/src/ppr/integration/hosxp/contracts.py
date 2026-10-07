"""Normalized HOSxP domain contracts (spec §24, §27).

These are the ONLY HOSxP shapes core code may depend on. Field names are this
system's normalized names from the spec - they are NOT claimed to be HOSxP field,
table or API names. Fields the spec marks "if available" are optional, because
their existence in HOSxP is unverified (spec §47). ``fiscal_year`` on the PR header
stays optional: a PR without it is rejected for a PPR, never guessed (D-08, D-18).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from ppr.integration.hosxp.status import HosxpPrStatus


class _Contract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- masters (§24.1)
class Department(_Contract):
    source_id: str = Field(min_length=1)
    code: str
    name: str
    active: bool


class Item(_Contract):
    source_id: str = Field(min_length=1)
    code: str
    name: str
    unit: str
    active: bool


class FundSource(_Contract):
    source_id: str = Field(min_length=1)
    code: str
    name: str
    active: bool


class BudgetCategory(_Contract):
    """Hierarchical via ``parent_source_id``; no fixed depth (spec §8.1)."""

    source_id: str = Field(min_length=1)
    code: str
    name: str
    parent_source_id: str | None = None
    active: bool


# ---------------------------------------------------------------- PR (§24.2, §24.3)
class PrItem(_Contract):
    pr_item_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    item_code: str | None = None
    item_name: str | None = None
    qty: Decimal
    unit: str | None = None
    unit_price: Decimal
    amount: Decimal


class PrHeader(_Contract):
    pr_id: str = Field(min_length=1)
    pr_no: str = Field(min_length=1)
    pr_date: date | None = None
    # The PR budget year ("ปีงบประมาณ"), visible on the real HOSxP PR screen; its data
    # source is verified with the real adapter (Wave 9). Required for a PPR (D-08, D-18).
    fiscal_year: int | None = None
    department_id: str | None = None
    requester_id: str | None = None
    requester_name: str | None = None
    fund_source_id: str | None = None
    budget_category_id: str | None = None
    total_amount: Decimal | None = None
    status: HosxpPrStatus = HosxpPrStatus.UNKNOWN  # normalized; native value below
    native_status: str | None = None  # raw value as received, for audit/diagnosis only
    last_modified_at: datetime | None = None


# ---------------------------------------------------------------- budget (§24.4)
class BudgetSnapshot(_Contract):
    """HOSxP budget - distinct from Plan Item remaining (spec §24.4)."""

    fund_source_id: str
    budget_category_id: str
    approved_budget: Decimal | None = None
    used_or_reserved: Decimal | None = None
    remaining: Decimal | None = None
    as_of: datetime


# ---------------------------------------------------------------- stock (§27, D-39)
class StockMovement(_Contract):
    """One issue or return of the central store, as the site's verified query returns it.

    Read for the *Central Plan Usage* report only (D-39): never stored, never posted to the
    Plan Ledger. ``native_kind`` is HOSxP's own movement kind, kept as received; it becomes
    an issue or a return only through the mapping IT writes in the site query file.
    """

    movement_id: str = Field(min_length=1)
    movement_date: date
    department_id: str = Field(min_length=1)  # the receiving / returning department
    item_id: str = Field(min_length=1)
    qty: Decimal = Field(gt=0)
    native_kind: str = Field(min_length=1)
    last_modified_at: datetime | None = None


class StockMovementKind(StrEnum):
    ISSUE = "ISSUE"  # from the central store to a department
    RETURN = "RETURN"  # from a department back to the central store


@dataclass(frozen=True)
class StockVerification:
    """Who verified the site's stock-movement query against HOSxP, when, and where it is
    recorded (D-39). Written by IT in the query file; nothing is shown without it."""

    verified_by: str
    verified_on: date
    reference: str


@dataclass(frozen=True)
class StockMovementSource:
    """What a gateway can say about its stock-movement query, without calling HOSxP.

    ``problem`` is None only when the query is configured, verified and has a kind mapping;
    otherwise it says (in Thai, for the report) why there is no data.
    """

    problem: str | None = "ยังไม่ได้ตั้งค่า query ยอดเบิก/คืนจาก HOSxP"
    verification: StockVerification | None = None
    kind_map: Mapping[str, StockMovementKind] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def usable(self) -> bool:
        return self.problem is None


# ---------------------------------------------------------------- identity (§21)
class HosxpUserProfile(_Contract):
    """Minimal normalized identity. Real attributes are TBD (spec §47)."""

    source_id: str = Field(min_length=1)
    username: str
    display_name: str | None = None
    department_id: str | None = None
    active: bool = True
