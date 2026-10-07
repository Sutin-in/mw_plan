"""Reading an imported plan workbook and writing its template (Wave 12A-1, D-42).

Reading is defensive - the file comes from a user:

* at most ``MAX_FILE_BYTES``; it must be an Excel 2007+ workbook (a ZIP holding
  ``xl/workbook.xml``) whose parts unpack to at most ``MAX_UNPACKED_BYTES`` (no zip bomb);
* openpyxl reads it read-only with cached values (formulas are never evaluated); the
  standard-library XML parser refuses entity expansion attacks (expat >= 2.4.1);
* only the two sheets of the template are read, by column title, at most ``MAX_ROWS`` rows.

The template lists the synchronized active HOSxP departments, fund sources and budget
categories (``source_id`` + name) and the plan year's budget lines, so codes are copied,
never typed from memory.
"""

from __future__ import annotations

import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

from ppr.domain.plan_import import (
    HEADING_SHEET,
    REQUIRED_HEADING_COLUMNS,
    REQUIRED_ROW_COLUMNS,
    ROW_SHEET,
    HCol,
    RCol,
    SheetRow,
)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_UNPACKED_BYTES = 60 * 1024 * 1024
MAX_ROWS = 20_000  # rows with a value
MAX_SCANNED_ROWS = 200_000  # rows read at all (formatting can reach far down a sheet)
MAX_COLUMNS = 60


class ImportFileError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkbookRows:
    headings: tuple[SheetRow, ...]
    rows: tuple[SheetRow, ...]


def _check_zip(data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError("FILE_TOO_LARGE", f"ไฟล์ใหญ่เกิน {MAX_FILE_BYTES // (1024 * 1024)} MB")
    try:
        with zipfile.ZipFile(BytesIO(data)) as z:
            infos = z.infolist()
            names = {i.filename for i in infos}
    except zipfile.BadZipFile:
        raise ImportFileError("NOT_XLSX", "ไม่ใช่ไฟล์ Excel (.xlsx)") from None
    if "xl/workbook.xml" not in names:
        raise ImportFileError("NOT_XLSX", "ไม่ใช่ไฟล์ Excel (.xlsx)")
    if len(infos) > 5000 or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
        raise ImportFileError("FILE_TOO_LARGE", "ไฟล์คลายออกแล้วใหญ่เกินไป")


def _sheet_rows(ws: Any, sheet: str, required: Sequence[str]) -> tuple[SheetRow, ...]:
    it = ws.iter_rows(values_only=True, max_col=MAX_COLUMNS)
    try:
        header = next(it)
    except StopIteration:
        raise ImportFileError("MISSING_COLUMNS", f"แผ่น '{sheet}' ว่าง") from None
    titles = [str(v).strip() if v is not None else "" for v in header]
    missing = [c for c in required if c not in titles]
    if missing:
        raise ImportFileError("MISSING_COLUMNS", f"แผ่น '{sheet}' ไม่มีคอลัมน์: {', '.join(missing)}")
    dup = sorted({t for t in titles if t and titles.count(t) > 1})
    if dup:
        raise ImportFileError("DUPLICATE_COLUMNS", f"แผ่น '{sheet}' มีคอลัมน์ซ้ำ: {', '.join(dup)}")
    out: list[SheetRow] = []
    for n, values in enumerate(it, start=2):
        if n > MAX_SCANNED_ROWS:
            raise ImportFileError(
                "TOO_MANY_ROWS", f"แผ่น '{sheet}' ยาวเกิน {MAX_SCANNED_ROWS} แถว (ลบแถวว่างท้ายแผ่น)"
            )
        cells = {t: v for t, v in zip(titles, values, strict=False) if t}
        if any(v is not None and str(v).strip() for v in cells.values()):
            out.append(SheetRow(n, cells))
            if len(out) > MAX_ROWS:
                raise ImportFileError("TOO_MANY_ROWS", f"แผ่น '{sheet}' มีข้อมูลเกิน {MAX_ROWS} แถว")
    return tuple(out)


def read_workbook(data: bytes) -> WorkbookRows:
    _check_zip(data)
    try:
        wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for a damaged file
        raise ImportFileError("NOT_XLSX", f"อ่านไฟล์ Excel ไม่ได้ ({type(exc).__name__})") from None
    try:
        for sheet in (HEADING_SHEET, ROW_SHEET):
            if sheet not in wb.sheetnames:
                raise ImportFileError("MISSING_SHEET", f"ไม่มีแผ่นชื่อ '{sheet}' (ใช้แม่แบบนำเข้า)")
        return WorkbookRows(
            _sheet_rows(wb[HEADING_SHEET], HEADING_SHEET, REQUIRED_HEADING_COLUMNS),
            _sheet_rows(wb[ROW_SHEET], ROW_SHEET, REQUIRED_ROW_COLUMNS),
        )
    finally:
        wb.close()


# ------------------------------------------------------------------ template
@dataclass(frozen=True)
class TemplateData:
    fiscal_year: int
    departments: Sequence[tuple[str, str]]  # (source_id, name), active only
    fund_sources: Sequence[tuple[str, str]]
    budget_categories: Sequence[tuple[str, str]]
    budgets: Sequence[tuple[str, str, str, str, Decimal]]  # fund id/name, category id/name, amount


_BOLD = Font(bold=True)
_FILL = PatternFill("solid", fgColor="DDEBF7")


def _text(v: str) -> str:
    """A name or code as text. openpyxl writes a string starting with "=" as a formula;
    such a value is written as an inline string instead (shown and copied unchanged)."""
    return str(ILLEGAL_CHARACTERS_RE.sub("", v))


def _table(
    ws: Any, titles: Sequence[str], rows: Sequence[Sequence[Any]], widths: Sequence[int]
) -> None:
    ws.append(list(titles))
    for c in range(1, len(titles) + 1):
        ws.cell(row=1, column=c).font = _BOLD
        ws.cell(row=1, column=c).fill = _FILL
    for r in rows:
        ws.append([_text(v) if isinstance(v, str) else v for v in r])
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"  # a value from HOSxP is text, never a formula
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"


def build_template(t: TemplateData) -> bytes:
    wb = Workbook()
    guide = wb.active
    guide.title = "คำอธิบาย"
    lines = [
        f"แม่แบบนำเข้าแผนปีงบประมาณ {t.fiscal_year} (D-42, D-43)",
        "",
        f"1. แผ่น '{HEADING_SHEET}': หนึ่งแถวต่อหนึ่งหัวข้อ (หน่วยงาน x แหล่งเงิน x หมวดงบ x ประเภทแผน)",
        "   - ประเภทแผน: DEPARTMENT (หน่วยงานซื้อเอง) หรือ CENTRAL (ซื้อรวม ต้องมีหน่วยงานผู้ซื้อ)",
        "   - ASSIGNED ไม่นำเข้าจาก Excel: กรอกทางหน้าจอ",
        "   - รหัสทุกช่องคัดลอกจากแผ่นรายชื่อ HOSxP ในไฟล์นี้ (ระบบไม่เดารหัสจากชื่อ)",
        f"2. แผ่น '{ROW_SHEET}': หนึ่งแถวต่อหนึ่งรายการแผน อ้างหัวข้อด้วย '{RCol.HEADING}'",
        "   - จำนวน ทศนิยมไม่เกิน 4 ตำแหน่ง; ราคาต่อหน่วยและมูลค่า ไม่เกิน 2 ตำแหน่ง (ระบบไม่ปัดเศษ)",
        "   - ไตรมาส 1-4 ใส่หรือไม่ใส่ก็ได้ (เพื่อติดตามผลเท่านั้น)",
        "   - จำนวนและมูลค่าเป็น 0 ทั้งคู่: ระบบข้ามแถวนั้นและแจ้งในรายงาน",
        "   - ไม่ใส่รหัสสินค้า HOSxP: ผู้ขอเลือกรายการแผนเองทุกครั้งที่ทำ PPR (D-43)",
        "     จึงต้องใช้ชื่อรายการที่ผู้ขออ่านแล้วเข้าใจ และหน่วยนับเดียวกับที่หน่วยงานเปิด PR ใน HOSxP",
        "3. วงเงิน (แหล่งเงิน x หมวดงบ) ต้องกรอกในระบบก่อนนำเข้า ตามเอกสารอนุมัติ:",
        "   ระบบไม่สร้างวงเงินจากไฟล์ (ดูแผ่น 'วงเงินในระบบ')",
        "4. นำเข้าได้เมื่อแผนอยู่สถานะร่างเท่านั้น และทั้งไฟล์หรือไม่มีเลย: ผิดแถวเดียวไม่นำเข้าเลย",
        "5. ห้ามเปลี่ยนชื่อแผ่นและชื่อคอลัมน์ (ย้ายลำดับคอลัมน์ได้)",
    ]
    for line in lines:
        guide.append([line])
    guide["A1"].font = Font(bold=True, size=14)
    guide.column_dimensions["A"].width = 110

    hs = wb.create_sheet(HEADING_SHEET)
    _table(hs, [c.value for c in HCol], [], [12, 40, 14, 20, 20, 16, 16, 30])
    dv = DataValidation(type="list", formula1='"DEPARTMENT,CENTRAL"', allow_blank=True)
    hs.add_data_validation(dv)
    dv.add("C2:C2000")

    rs = wb.create_sheet(ROW_SHEET)
    _table(rs, [c.value for c in RCol], [], [12, 50, 10, 12, 14, 16, 12, 12, 12, 12, 20, 30])

    _table(wb.create_sheet("หน่วยงาน HOSxP"), ["รหัส", "ชื่อ"], t.departments, [16, 60])
    _table(wb.create_sheet("แหล่งเงิน HOSxP"), ["รหัส", "ชื่อ"], t.fund_sources, [16, 60])
    _table(wb.create_sheet("หมวดงบ HOSxP"), ["รหัส", "ชื่อ"], t.budget_categories, [16, 60])
    _table(
        wb.create_sheet("วงเงินในระบบ"),
        ["รหัสแหล่งเงิน", "แหล่งเงิน", "รหัสหมวดงบ", "หมวดงบ", "วงเงินอนุมัติ (บาท)"],
        [(f, fn, c, cn, float(a)) for f, fn, c, cn, a in t.budgets],
        [16, 30, 16, 40, 20],
    )
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
