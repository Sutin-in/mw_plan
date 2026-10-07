"""Validation outcome vocabulary shared by domain rules.

Rules return ``Violation`` values instead of raising, so the UI can explain every
failed item (spec §12 "The UI must explain which Item failed and why").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ViolationCode(StrEnum):
    # PR / header (spec §12)
    PR_NOT_FOUND = "PR_NOT_FOUND"
    PR_HAS_NO_ITEMS = "PR_HAS_NO_ITEMS"
    FISCAL_YEAR_UNKNOWN = "FISCAL_YEAR_UNKNOWN"  # the PR carries no budget year
    FISCAL_YEAR_NOT_ACTIVE = "FISCAL_YEAR_NOT_ACTIVE"
    PLAN_YEAR_NOT_FOUND = "PLAN_YEAR_NOT_FOUND"  # no plan exists for the current year
    PLAN_YEAR_EXPIRED = "PLAN_YEAR_EXPIRED"  # D-18: PR budget year < current fiscal year
    FISCAL_YEAR_NOT_CURRENT = "FISCAL_YEAR_NOT_CURRENT"  # D-18: PR budget year in the future
    PR_MISSING_DEPARTMENT = "PR_MISSING_DEPARTMENT"
    PR_CANCELLED_IN_HOSXP = "PR_CANCELLED_IN_HOSXP"  # assumption A-13
    PR_TOTAL_UNAVAILABLE = "PR_TOTAL_UNAVAILABLE"  # D-17: header total missing
    PR_TOTAL_MISMATCH = "PR_TOTAL_MISMATCH"  # D-17: header total != sum of lines
    PR_VALUE_PRECISION = "PR_VALUE_PRECISION"  # qty > 4 or amount > 2 decimals
    PR_ALREADY_BOUND = "PR_ALREADY_BOUND"  # D-03
    DRAFT_ALREADY_EXISTS = "DRAFT_ALREADY_EXISTS"  # D-05
    INVALID_BINDING_TRANSITION = "INVALID_BINDING_TRANSITION"
    UNKNOWN_FUND_SOURCE = "UNKNOWN_FUND_SOURCE"
    DUPLICATE_PR_ITEM = "DUPLICATE_PR_ITEM"
    MANUAL_PR_ITEM = "MANUAL_PR_ITEM"  # spec §11.4
    # Item -> plan matching
    NOT_IN_PLAN = "NOT_IN_PLAN"
    AMBIGUOUS_PLAN_MATCH = "AMBIGUOUS_PLAN_MATCH"  # D-16, superseded by D-43 (not raised)
    PLAN_ROW_NOT_CHOSEN = "PLAN_ROW_NOT_CHOSEN"  # D-43: the requester has not chosen a row
    PLAN_ROW_NOT_ALLOWED = "PLAN_ROW_NOT_ALLOWED"  # D-43: the chosen row is not offered
    # D-38 A-1: the department has open demand on an Assigned Purchase item
    DEMAND_IN_CENTRAL_PURCHASE = "DEMAND_IN_CENTRAL_PURCHASE"
    # Hard control (spec §13)
    INVALID_REQUESTED_QTY = "INVALID_REQUESTED_QTY"
    INVALID_REQUESTED_AMOUNT = "INVALID_REQUESTED_AMOUNT"
    QTY_EXCEEDED = "QTY_EXCEEDED"
    AMOUNT_EXCEEDED = "AMOUNT_EXCEEDED"
    QTY_EXHAUSTED = "QTY_EXHAUSTED"
    AMOUNT_EXHAUSTED = "AMOUNT_EXHAUSTED"
    # Category allocation (spec §17, D-04)
    ALLOCATION_EMPTY = "ALLOCATION_EMPTY"
    ALLOCATION_DUPLICATE_CATEGORY = "ALLOCATION_DUPLICATE_CATEGORY"
    ALLOCATION_NON_POSITIVE = "ALLOCATION_NON_POSITIVE"
    ALLOCATION_UNKNOWN_CATEGORY = "ALLOCATION_UNKNOWN_CATEGORY"
    ALLOCATION_CROSS_FUND = "ALLOCATION_CROSS_FUND"
    ALLOCATION_SUM_MISMATCH = "ALLOCATION_SUM_MISMATCH"
    ADJUSTMENT_REFERENCE_REQUIRED = "ADJUSTMENT_REFERENCE_REQUIRED"  # D-19


@dataclass(frozen=True)
class Violation:
    code: ViolationCode
    message: str
    subject: str | None = None  # e.g. PR item id or category id


def codes(violations: tuple[Violation, ...] | list[Violation]) -> set[ViolationCode]:
    return {v.code for v in violations}
