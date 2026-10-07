"""Checking a plan imported from Excel (Wave 12A-1, D-42). Pure: no I/O, no framework.

The Excel reader (``ppr.api.plan_import_xlsx``) hands over the two sheets as rows of cell
values keyed by column title; this module turns them into Plan Item values and reports
every problem with its sheet, row and column. Nothing is guessed:

* every code (department, fund source, budget category, HOSxP item) is an exact HOSxP
  ``source_id`` of a synchronized, active record - never looked up by name;
* numbers are taken as written - never rounded; more decimals than stored is an error;
* a missing budget line is an error (D-42: the approved amount comes from the approval
  document, never from the file);
* only ``DEPARTMENT`` and ``CENTRAL`` headings are imported (``ASSIGNED`` needs per-department
  demand, entered on screen);
* a row with quantity and amount both 0 is skipped and listed, not imported.

The import is all or nothing: any error means nothing is imported.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from ppr.domain.plan import PlanType

ZERO = Decimal(0)

# Sheet and column titles of the import template (D-42). The reader matches titles exactly
# (surrounding spaces ignored), so a column may move but not be renamed.
HEADING_SHEET = "หัวข้อ"
ROW_SHEET = "รายการ"


class HCol(StrEnum):
    CODE = "รหัสหัวข้อ"
    NAME = "ชื่อหัวข้อ"
    PLAN_TYPE = "ประเภทแผน"
    OWNER = "รหัสหน่วยงานเจ้าของ"
    PURCHASER = "รหัสหน่วยงานผู้ซื้อ"
    FUND = "รหัสแหล่งเงิน"
    CATEGORY = "รหัสหมวดงบ"
    NOTE = "หมายเหตุ"


class RCol(StrEnum):
    HEADING = "รหัสหัวข้อ"
    NAME = "ชื่อรายการ"
    UNIT = "หน่วยนับ"
    QTY = "จำนวน"
    PRICE = "ราคาต่อหน่วย"
    AMOUNT = "มูลค่า"
    Q1 = "ไตรมาส 1"
    Q2 = "ไตรมาส 2"
    Q3 = "ไตรมาส 3"
    Q4 = "ไตรมาส 4"
    SOURCE_SHEET = "แผ่นต้นทาง"
    NOTE = "หมายเหตุ"


OLD_ITEM_COLUMN = "รหัสสินค้า HOSxP"  # Wave 12A-1 template only; not read (D-43)

REQUIRED_HEADING_COLUMNS = (
    HCol.CODE,
    HCol.PLAN_TYPE,
    HCol.OWNER,
    HCol.PURCHASER,
    HCol.FUND,
    HCol.CATEGORY,
)
REQUIRED_ROW_COLUMNS = (
    RCol.HEADING,
    RCol.NAME,
    RCol.UNIT,
    RCol.QTY,
    RCol.PRICE,
    RCol.AMOUNT,
)

QTY_DECIMALS = 4  # plan_item.planned_qty / q1..q4 NUMERIC(18,4)
MONEY_DECIMALS = 2  # planned_amount / estimated_unit_price NUMERIC(18,2)
MAX_DIGITS = 18
IMPORTABLE_TYPES = (PlanType.DEPARTMENT, PlanType.CENTRAL)


class ImportCode(StrEnum):
    # errors
    NO_ROWS = "NO_ROWS"
    DUPLICATE_HEADING = "DUPLICATE_HEADING"
    UNKNOWN_HEADING = "UNKNOWN_HEADING"
    MISSING_VALUE = "MISSING_VALUE"
    UNKNOWN_PLAN_TYPE = "UNKNOWN_PLAN_TYPE"
    PLAN_TYPE_NOT_IMPORTED = "PLAN_TYPE_NOT_IMPORTED"
    UNKNOWN_CODE = "UNKNOWN_CODE"
    INACTIVE_CODE = "INACTIVE_CODE"
    CENTRAL_WITHOUT_PURCHASER = "CENTRAL_WITHOUT_PURCHASER"
    NO_BUDGET_LINE = "NO_BUDGET_LINE"
    BAD_NUMBER = "BAD_NUMBER"
    NEGATIVE = "NEGATIVE"
    TOO_PRECISE = "TOO_PRECISE"
    ZERO_QTY_WITH_AMOUNT = "ZERO_QTY_WITH_AMOUNT"
    # warnings
    ITEM_COLUMN_IGNORED = "ITEM_COLUMN_IGNORED"
    AMOUNT_DIFFERS_FROM_ESTIMATE = "AMOUNT_DIFFERS_FROM_ESTIMATE"
    HEADING_UNUSED = "HEADING_UNUSED"
    # skipped rows
    ZERO_ROW = "ZERO_ROW"


@dataclass(frozen=True)
class ImportIssue:
    code: ImportCode
    message: str
    sheet: str | None = None
    row: int | None = None
    column: str | None = None


@dataclass(frozen=True)
class SheetRow:
    """One spreadsheet row: its 1-based row number and its cells by column title."""

    row: int
    cells: Mapping[str, object]


@dataclass(frozen=True)
class MasterFacts:
    """What the import may refer to: synchronized HOSxP records by ``source_id``."""

    departments: Mapping[str, bool]  # source_id -> active
    fund_sources: Mapping[str, bool]
    budget_categories: Mapping[str, bool]


@dataclass(frozen=True)
class ImportedRow:
    """A checked row, ready to become a Plan Item."""

    heading: str
    plan_budget_id: int
    plan_type: PlanType
    name: str  # the plan's own name and unit; no HOSxP item (D-43)
    unit: str
    owner_department_id: str
    purchasing_department_id: str | None
    fund_source_id: str
    budget_category_id: str
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    quarters: tuple[Decimal | None, Decimal | None, Decimal | None, Decimal | None]
    note: str | None
    source_sheet: str
    source_row: int


@dataclass(frozen=True)
class BudgetTotal:
    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal
    already_planned: Decimal  # Plan Items of the year before this import
    imported: Decimal
    rows: int

    @property
    def after_import(self) -> Decimal:
        return self.already_planned + self.imported

    @property
    def difference(self) -> Decimal:
        """Planned after the import minus approved; 0 is what approval requires (§9)."""
        return self.after_import - self.approved_amount


@dataclass(frozen=True)
class ImportReport:
    rows: tuple[ImportedRow, ...]
    errors: tuple[ImportIssue, ...] = ()
    warnings: tuple[ImportIssue, ...] = ()
    skipped: tuple[ImportIssue, ...] = ()
    budgets: tuple[BudgetTotal, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def total_amount(self) -> Decimal:
        return sum((r.planned_amount for r in self.rows), ZERO)


@dataclass(frozen=True)
class ExistingItem:
    """A Plan Item already in the year, for the budget totals."""

    fund_source_id: str
    budget_category_id: str
    planned_amount: Decimal


# ------------------------------------------------------------------ cells
def text_of(value: object) -> str:
    """A cell as text: numbers keep their digits (``31230.0`` -> ``31230``), spaces trimmed."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return str(value).strip()


class NumberError(ValueError):
    pass


# A number typed as text: digits, an optional point, thousands commas only in groups of
# three ("1,234.5"). Anything else ("1,5", Thai digits, "1_000", "1e3") is refused rather
# than read in a way the user may not have meant.
_TEXT_NUMBER = re.compile(r"-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")
EXCEL_DIGITS = 15  # Excel keeps and shows numbers to 15 significant digits
LARGE = Decimal(10) ** 10


def number_of(value: object) -> Decimal | None:
    """A cell as a decimal, or None when empty.

    A number cell is a binary float; it is read as Excel itself holds and shows it - to 15
    significant digits - so a formula result such as 3 x 1.1 (stored as 3.3000000000000003)
    reads 3.3, the value on the sheet. Nothing else is rounded: digits beyond what the plan
    stores are refused by the caller (``TOO_PRECISE``), never cut."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (bool, date, datetime)):
        raise NumberError("not a number")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise NumberError("not a number")
        shown = Decimal(f"{value:.{EXCEL_DIGITS}g}").normalize() + 0
        exact = Decimal(repr(value))
        if shown != exact and abs(exact) >= LARGE:
            # Above 1e10 the 15 digits would cut real digits (only a formula or another tool
            # can store more): take the float's own digits; the caller then refuses them as
            # too precise rather than using a shortened value.
            return exact
        return shown
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise NumberError("not a number")
        return value
    raw = str(value).strip()
    if not _TEXT_NUMBER.fullmatch(raw):
        raise NumberError("not a number")
    return Decimal(raw.replace(",", ""))


def _decimals(d: Decimal) -> int:
    exp = d.normalize().as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def _fits(d: Decimal, decimals: int) -> bool:
    """At most ``decimals`` decimals and at most MAX_DIGITS - decimals digits before the
    point (NUMERIC(18, decimals)); counted with ``adjusted()`` so 1E+20 counts 21 digits."""
    whole_digits = max(d.adjusted() + 1, 1) if d != 0 else 1
    return _decimals(d) <= decimals and whole_digits <= MAX_DIGITS - decimals


# ------------------------------------------------------------------ the check
@dataclass
class _Issues:
    errors: list[ImportIssue] = field(default_factory=list)
    warnings: list[ImportIssue] = field(default_factory=list)
    skipped: list[ImportIssue] = field(default_factory=list)


@dataclass(frozen=True)
class _Heading:
    code: str
    row: int
    plan_type: PlanType
    owner: str
    purchaser: str | None
    fund: str
    category: str


def _code_issue(
    out: _Issues,
    known: Mapping[str, bool],
    code: str,
    label: str,
    sheet: str,
    row: int,
    column: str,
) -> bool:
    """Appends an error and returns False when ``code`` is not an active HOSxP record."""
    if code not in known:
        out.errors.append(
            ImportIssue(
                ImportCode.UNKNOWN_CODE,
                f"{label} '{code}' ไม่มีในข้อมูลหลักจาก HOSxP (ซิงค์ข้อมูลหลักก่อน หรือแก้รหัส)",
                sheet,
                row,
                column,
            )
        )
        return False
    if not known[code]:
        out.errors.append(
            ImportIssue(
                ImportCode.INACTIVE_CODE,
                f"{label} '{code}' ปิดใช้งานแล้วใน HOSxP",
                sheet,
                row,
                column,
            )
        )
        return False
    return True


def _headings(
    rows: Sequence[SheetRow],
    masters: MasterFacts,
    budgets: Mapping[tuple[str, str], int],
    out: _Issues,
) -> dict[str, _Heading]:
    sheet = HEADING_SHEET
    found: dict[str, _Heading] = {}
    seen = Counter(text_of(r.cells.get(HCol.CODE)) for r in rows)
    for r in rows:
        code = text_of(r.cells.get(HCol.CODE))
        if not code:
            if any(text_of(v) for v in r.cells.values()):
                out.errors.append(
                    ImportIssue(ImportCode.MISSING_VALUE, "ไม่มีรหัสหัวข้อ", sheet, r.row, HCol.CODE)
                )
            continue
        if seen[code] > 1:
            out.errors.append(
                ImportIssue(
                    ImportCode.DUPLICATE_HEADING,
                    f"รหัสหัวข้อ '{code}' ซ้ำ {seen[code]} แถว",
                    sheet,
                    r.row,
                    HCol.CODE,
                )
            )
            continue
        ok = True
        values = {c: text_of(r.cells.get(c)) for c in HCol}
        for col in (HCol.PLAN_TYPE, HCol.OWNER, HCol.FUND, HCol.CATEGORY):
            if not values[col]:
                out.errors.append(
                    ImportIssue(ImportCode.MISSING_VALUE, f"ต้องกรอก {col}", sheet, r.row, col)
                )
                ok = False
        raw_type = values[HCol.PLAN_TYPE].upper()
        plan_type: PlanType | None = None
        if raw_type:
            try:
                plan_type = PlanType(raw_type)
            except ValueError:
                out.errors.append(
                    ImportIssue(
                        ImportCode.UNKNOWN_PLAN_TYPE,
                        f"ประเภทแผน '{values[HCol.PLAN_TYPE]}' ต้องเป็น DEPARTMENT หรือ CENTRAL",
                        sheet,
                        r.row,
                        HCol.PLAN_TYPE,
                    )
                )
                ok = False
            else:
                if plan_type not in IMPORTABLE_TYPES:
                    out.errors.append(
                        ImportIssue(
                            ImportCode.PLAN_TYPE_NOT_IMPORTED,
                            "แผนแบบ ASSIGNED ต้องระบุความต้องการรายหน่วยงาน: กรอกทางหน้าจอ "
                            "ไม่นำเข้าจาก Excel (D-42)",
                            sheet,
                            r.row,
                            HCol.PLAN_TYPE,
                        )
                    )
                    ok = False
        purchaser = values[HCol.PURCHASER] or None
        if values[HCol.OWNER] and not _code_issue(
            out, masters.departments, values[HCol.OWNER], "หน่วยงาน", sheet, r.row, HCol.OWNER
        ):
            ok = False
        if purchaser and not _code_issue(
            out, masters.departments, purchaser, "หน่วยงาน", sheet, r.row, HCol.PURCHASER
        ):
            ok = False
        if plan_type is PlanType.CENTRAL and not purchaser:
            out.errors.append(
                ImportIssue(
                    ImportCode.CENTRAL_WITHOUT_PURCHASER,
                    "แผนแบบ CENTRAL ต้องระบุหน่วยงานผู้ซื้อ (D-38)",
                    sheet,
                    r.row,
                    HCol.PURCHASER,
                )
            )
            ok = False
        fund, category = values[HCol.FUND], values[HCol.CATEGORY]
        fund_ok = bool(fund) and _code_issue(
            out, masters.fund_sources, fund, "แหล่งเงิน", sheet, r.row, HCol.FUND
        )
        cat_ok = bool(category) and _code_issue(
            out, masters.budget_categories, category, "หมวดงบ", sheet, r.row, HCol.CATEGORY
        )
        if fund_ok and cat_ok and (fund, category) not in budgets:
            out.errors.append(
                ImportIssue(
                    ImportCode.NO_BUDGET_LINE,
                    f"ยังไม่มีวงเงินของแหล่งเงิน {fund} หมวดงบ {category} ในแผนปีนี้: กรอกวงเงิน"
                    "ที่อนุมัติจากเอกสารก่อน แล้วจึงนำเข้า (D-42)",
                    sheet,
                    r.row,
                    HCol.CATEGORY,
                )
            )
            ok = False
        if ok and fund_ok and cat_ok and plan_type is not None:
            found[code] = _Heading(
                code, r.row, plan_type, values[HCol.OWNER], purchaser, fund, category
            )
    return found


def _number(
    r: SheetRow, col: str, decimals: int, out: _Issues, *, required: bool
) -> tuple[bool, Decimal | None]:
    sheet = ROW_SHEET
    try:
        value = number_of(r.cells.get(col))
    except NumberError:
        out.errors.append(
            ImportIssue(
                ImportCode.BAD_NUMBER,
                f"{col} '{text_of(r.cells.get(col))}' ไม่ใช่ตัวเลข",
                sheet,
                r.row,
                col,
            )
        )
        return False, None
    if value is None:
        if required:
            out.errors.append(
                ImportIssue(ImportCode.MISSING_VALUE, f"ต้องกรอก {col}", sheet, r.row, col)
            )
            return False, None
        return True, None
    if value < ZERO:
        out.errors.append(
            ImportIssue(ImportCode.NEGATIVE, f"{col} ติดลบไม่ได้ ({value})", sheet, r.row, col)
        )
        return False, None
    if not _fits(value, decimals):
        out.errors.append(
            ImportIssue(
                ImportCode.TOO_PRECISE,
                f"{col} {value} มีทศนิยมเกิน {decimals} ตำแหน่ง หรือยาวเกิน: ระบบไม่ปัดเศษให้",
                sheet,
                r.row,
                col,
            )
        )
        return False, None
    return True, value


def check_import(
    heading_rows: Sequence[SheetRow],
    item_rows: Sequence[SheetRow],
    masters: MasterFacts,
    budgets: Sequence[tuple[int, str, str, Decimal]],  # (id, fund, category, approved)
    existing: Iterable[ExistingItem] = (),
) -> ImportReport:
    out = _Issues()
    budget_ids = {(f, c): bid for bid, f, c, _ in budgets}
    headings = _headings(heading_rows, masters, budget_ids, out)
    heading_codes = {text_of(r.cells.get(HCol.CODE)) for r in heading_rows}
    existing = list(existing)

    accepted: list[ImportedRow] = []
    used_headings: set[str] = set()
    sheet = ROW_SHEET
    for r in item_rows:
        if not any(text_of(v) for v in r.cells.values()):
            continue  # an empty row
        ok = True
        heading = text_of(r.cells.get(RCol.HEADING))
        name = text_of(r.cells.get(RCol.NAME))
        unit = text_of(r.cells.get(RCol.UNIT))
        for col, v in ((RCol.HEADING, heading), (RCol.NAME, name), (RCol.UNIT, unit)):
            if not v:
                out.errors.append(
                    ImportIssue(ImportCode.MISSING_VALUE, f"ต้องกรอก {col}", sheet, r.row, col)
                )
                ok = False
        h = headings.get(heading)
        if heading:
            used_headings.add(heading)
            if heading not in heading_codes:
                out.errors.append(
                    ImportIssue(
                        ImportCode.UNKNOWN_HEADING,
                        f"รหัสหัวข้อ '{heading}' ไม่มีในแผ่น {HEADING_SHEET}",
                        sheet,
                        r.row,
                        RCol.HEADING,
                    )
                )
                ok = False
            elif h is None:
                ok = False  # the heading itself has an error, reported on its own row
        q_ok, qty = _number(r, RCol.QTY, QTY_DECIMALS, out, required=True)
        p_ok, price = _number(r, RCol.PRICE, MONEY_DECIMALS, out, required=True)
        a_ok, amount = _number(r, RCol.AMOUNT, MONEY_DECIMALS, out, required=True)
        quarters: list[Decimal | None] = []
        for col in (RCol.Q1, RCol.Q2, RCol.Q3, RCol.Q4):
            qq_ok, qv = _number(r, col, QTY_DECIMALS, out, required=False)
            ok = ok and qq_ok
            quarters.append(qv)
        ok = ok and q_ok and p_ok and a_ok
        if qty is not None and amount is not None and qty == ZERO:
            if amount == ZERO:
                out.skipped.append(
                    ImportIssue(
                        ImportCode.ZERO_ROW,
                        f"ข้าม: จำนวนและมูลค่าเป็น 0 ({name or '-'})",
                        sheet,
                        r.row,
                    )
                )
                continue
            out.errors.append(
                ImportIssue(
                    ImportCode.ZERO_QTY_WITH_AMOUNT,
                    f"จำนวนเป็น 0 แต่มีมูลค่า {amount}",
                    sheet,
                    r.row,
                    RCol.QTY,
                )
            )
            ok = False
        if not ok or h is None or qty is None or price is None or amount is None:
            continue
        if qty * price != amount:
            out.warnings.append(
                ImportIssue(
                    ImportCode.AMOUNT_DIFFERS_FROM_ESTIMATE,
                    f"มูลค่า {amount} ไม่เท่ากับ จำนวน x ราคา = {qty * price} "
                    "(ยอมรับได้ ราคาเป็นราคาประมาณ)",
                    sheet,
                    r.row,
                    RCol.AMOUNT,
                )
            )
        note = text_of(r.cells.get(RCol.NOTE)) or None
        source_sheet = text_of(r.cells.get(RCol.SOURCE_SHEET)) or ROW_SHEET
        accepted.append(
            ImportedRow(
                heading=h.code,
                plan_budget_id=budget_ids[(h.fund, h.category)],
                plan_type=h.plan_type,
                name=name,
                unit=unit,
                owner_department_id=h.owner,
                purchasing_department_id=h.purchaser,
                fund_source_id=h.fund,
                budget_category_id=h.category,
                planned_qty=qty,
                estimated_unit_price=price,
                planned_amount=amount,
                quarters=(quarters[0], quarters[1], quarters[2], quarters[3]),
                note=note,
                source_sheet=source_sheet,
                source_row=r.row,
            )
        )

    # D-43: the plan holds no HOSxP item codes; a column left from an older template is
    # not read (the requester chooses the plan row on every PPR).
    if any(text_of(r.cells.get(OLD_ITEM_COLUMN)) for r in item_rows):
        out.warnings.append(
            ImportIssue(
                ImportCode.ITEM_COLUMN_IGNORED,
                f"คอลัมน์ '{OLD_ITEM_COLUMN}' ไม่ใช้แล้ว: ไม่นำเข้ารหัสสินค้า "
                "ผู้ขอเลือกรายการแผนเองทุกครั้งที่ทำ PPR (D-43)",
                ROW_SHEET,
                None,
                OLD_ITEM_COLUMN,
            )
        )

    for code, h in sorted(headings.items()):
        if code not in used_headings:
            out.warnings.append(
                ImportIssue(
                    ImportCode.HEADING_UNUSED,
                    f"หัวข้อ '{code}' ไม่มีรายการใดใช้",
                    HEADING_SHEET,
                    h.row,
                    HCol.CODE,
                )
            )
    if not accepted and not out.errors:
        out.errors.append(ImportIssue(ImportCode.NO_ROWS, "ไม่มีรายการให้นำเข้า", ROW_SHEET))

    totals: list[BudgetTotal] = []
    for _bid, fund, category, approved in budgets:
        mine = [a for a in accepted if (a.fund_source_id, a.budget_category_id) == (fund, category)]
        before = sum(
            (
                e.planned_amount
                for e in existing
                if (e.fund_source_id, e.budget_category_id) == (fund, category)
            ),
            ZERO,
        )
        if mine or before:
            totals.append(
                BudgetTotal(
                    fund,
                    category,
                    approved,
                    before,
                    sum((a.planned_amount for a in mine), ZERO),
                    len(mine),
                )
            )
    return ImportReport(
        tuple(accepted) if not out.errors else (),
        tuple(out.errors),
        tuple(out.warnings),
        tuple(out.skipped),
        tuple(totals),
    )
