"""Wave 8B: reports of §37 (D-35). One row builder feeds the screen and the Excel file.

Read only: reports never change plan or PPR data. Only *Central Plan Usage* reads HOSxP - the
central store's issues and returns, live, through a verified query, for display (D-39,
``stock_services``). Scopes are those of
reading the underlying records (§22.1): PPR reports use ``Caller.ppr_scope`` (D-41), plan
reports the Plan Item scope, and the audit report needs ``AUDIT_READ`` (checked by the caller
of ``run`` through ``can_audit``). Exporting a file is audited by ``record_export``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

from ppr.application import amendment_services as am
from ppr.application.dashboard_services import ItemStatus, item_statuses
from ppr.application.errors import ForbiddenError, InvalidInputError, NotFoundError
from ppr.application.ppr_services import Caller
from ppr.application.stock_services import (
    NO_DATA,
    StockRead,
    StockTotals,
    not_available,
    read_stock,
)
from ppr.domain.alerts import AlertSetting
from ppr.domain.plan import PlanType, responsible_department
from ppr.domain.ppr_state import PprState
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.ports import (
    Actor,
    AuditEntry,
    MasterKind,
    PlanItemRecord,
    PprRecord,
    PprSearch,
    UnitOfWork,
)

MAX_ROWS = 5000
# Report dates and times are hospital time (Thailand, UTC+7, no daylight saving), the same
# zone the screens use - never the server's own zone (a server may run on UTC).
HOSPITAL_TZ = timezone(timedelta(hours=7), "Asia/Bangkok")
EARLIEST, LATEST = date(2000, 1, 1), date(2200, 12, 31)
ZERO = Decimal(0)
HUNDRED = Decimal(100)


class ColType(StrEnum):
    TEXT = "TEXT"
    INT = "INT"
    MONEY = "MONEY"
    QTY = "QTY"
    PERCENT = "PERCENT"
    DATE = "DATE"
    DATETIME = "DATETIME"


@dataclass(frozen=True)
class Col:
    key: str
    label: str
    type: ColType = ColType.TEXT


@dataclass(frozen=True)
class Filters:
    fiscal_year: int | None = None
    department_id: str | None = None
    fund_source_id: str | None = None
    budget_category_id: str | None = None
    item: str | None = None
    state: PprState | None = None
    date_from: date | None = None  # inclusive
    date_to: date | None = None  # inclusive
    action: str | None = None  # audit report
    entity_type: str | None = None  # audit report


@dataclass(frozen=True)
class ReportDef:
    code: str
    title: str
    kind: str  # "plan" | "amendment" | "ppr" | "audit"
    filters: tuple[str, ...]
    columns: tuple[Col, ...]


@dataclass(frozen=True)
class Report:
    definition: ReportDef
    rows: list[dict[str, Any]]
    truncated: bool
    filters: list[tuple[str, str]]  # (label, value) as applied, for the screen and the file
    generated_at: datetime
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ definitions (D-35)
_PLAN_F = ("fiscal_year", "department_id", "fund_source_id", "budget_category_id", "item")
_PPR_F = (
    "fiscal_year",
    "department_id",
    "fund_source_id",
    "budget_category_id",
    "item",
    "date_from",
    "date_to",
)
_MONEY_COLS = (
    Col("items", "จำนวนรายการแผน", ColType.INT),
    Col("planned_amount", "วงเงินตามแผน (บาท)", ColType.MONEY),
    Col("used_amount", "ขอใช้แล้ว (บาท)", ColType.MONEY),
    Col("remaining_amount", "คงเหลือ (บาท)", ColType.MONEY),
    Col("used_percent", "ใช้ไป (%)", ColType.PERCENT),
    Col("low", "ใกล้หมด (รายการ)", ColType.INT),
    Col("exhausted", "หมดแล้ว (รายการ)", ColType.INT),
)
_PPR_HEAD = (
    Col("ppr_number", "เลขที่ PPR"),
    Col("pr_no", "เลข PR"),
    Col("fiscal_year", "ปีงบ", ColType.INT),
    Col("department", "หน่วยงาน"),
)

REPORTS: dict[str, ReportDef] = {
    d.code: d
    for d in (
        ReportDef(
            "PLAN_SUMMARY",
            "สรุปแผนตามวงเงิน (แหล่งเงิน / หมวดงบ)",
            "plan",
            ("fiscal_year", "fund_source_id", "budget_category_id"),
            (
                Col("fund_source", "แหล่งเงิน"),
                Col("budget_category", "หมวดงบ"),
                Col("approved_budget", "วงเงินอนุมัติ (บาท)", ColType.MONEY),
                *_MONEY_COLS,
            ),
        ),
        ReportDef(
            "PLAN_REMAINING",
            "แผนคงเหลือรายรายการ",
            "plan",
            _PLAN_F,
            (
                Col("item_code", "รหัสสินค้า"),
                Col("item_name", "รายการ"),
                Col("unit", "หน่วย"),
                Col("plan_type", "ประเภทแผน"),
                Col("department", "หน่วยงาน (เจ้าของแผน / คลังผู้ซื้อของแผนกลาง)"),
                Col("fund_source", "แหล่งเงิน"),
                Col("budget_category", "หมวดงบ"),
                Col("planned_qty", "จำนวนตามแผน", ColType.QTY),
                Col("used_qty", "ใช้แล้ว (จำนวน)", ColType.QTY),
                Col("remaining_qty", "คงเหลือ (จำนวน)", ColType.QTY),
                Col("planned_amount", "วงเงินตามแผน (บาท)", ColType.MONEY),
                Col("used_amount", "ขอใช้แล้ว (บาท)", ColType.MONEY),
                Col("remaining_amount", "คงเหลือ (บาท)", ColType.MONEY),
                Col("remaining_percent", "คงเหลือ (%)", ColType.PERCENT),
                Col("alert", "แจ้งเตือน"),
            ),
        ),
        ReportDef(
            "DEPARTMENT_UTILIZATION",
            "การใช้แผนตามหน่วยงาน",
            "plan",
            ("fiscal_year", "department_id", "fund_source_id", "budget_category_id"),
            (Col("department", "หน่วยงาน"), *_MONEY_COLS),
        ),
        ReportDef(
            "CATEGORY_UTILIZATION",
            "การใช้แผนตามหมวดงบ",
            "plan",
            ("fiscal_year", "department_id", "fund_source_id", "budget_category_id"),
            (Col("budget_category", "หมวดงบ"), *_MONEY_COLS),
        ),
        ReportDef(
            "PPR_REGISTER",
            "ทะเบียน PPR",
            "ppr",
            (*_PPR_F, "state"),
            (
                *_PPR_HEAD,
                Col("fund_source", "แหล่งเงิน"),
                Col("allocations", "หมวดงบ (ยอด)"),
                Col("required_amount", "ยอดที่ขอใช้ (บาท)", ColType.MONEY),
                Col("state", "สถานะ"),
                Col("version", "ฉบับที่", ColType.INT),
                Col("first_confirmed_at", "ยืนยันครั้งแรก", ColType.DATETIME),
                Col("verified_at", "พัสดุตรวจสอบล่าสุด", ColType.DATETIME),
                Col("requester", "ผู้จัดทำ"),
                Col("updated_at", "เปลี่ยนแปลงล่าสุด", ColType.DATETIME),
            ),
        ),
        ReportDef(
            "PENDING_PPR",
            "PPR ค้างดำเนินการ (ยังไม่ผ่านพัสดุตรวจ)",
            "ppr",
            (*_PPR_F, "state"),
            (
                *_PPR_HEAD,
                Col("state", "สถานะ"),
                Col("required_amount", "ยอดที่ขอใช้ (บาท)", ColType.MONEY),
                Col("created_at", "สร้างเมื่อ", ColType.DATETIME),
                Col("updated_at", "เปลี่ยนแปลงล่าสุด", ColType.DATETIME),
                Col("days_inactive", "ไม่เคลื่อนไหว (วัน)", ColType.INT),
            ),
        ),
        ReportDef(
            "CANCELLED_PPR",
            "PPR ที่ยกเลิก",
            "ppr",
            _PPR_F,
            (
                *_PPR_HEAD,
                Col("required_amount", "ยอดที่เคยขอใช้ (บาท)", ColType.MONEY),
                Col("first_confirmed_at", "ยืนยันครั้งแรก", ColType.DATETIME),
                Col("updated_at", "เปลี่ยนแปลงล่าสุด", ColType.DATETIME),
                Col("hosxp_status", "สถานะ PR ใน HOSxP"),
            ),
        ),
        ReportDef(
            "PR_CHANGED",
            "PR เปลี่ยน ต้องทบทวน",
            "ppr",
            _PPR_F,
            (
                *_PPR_HEAD,
                Col("required_amount", "ยอดที่ขอใช้ (บาท)", ColType.MONEY),
                Col("version", "ฉบับที่", ColType.INT),
                Col("updated_at", "เปลี่ยนแปลงล่าสุด", ColType.DATETIME),
                Col("days_inactive", "ไม่เคลื่อนไหว (วัน)", ColType.INT),
            ),
        ),
        ReportDef(
            "CENTRAL_PLAN_USAGE",
            "การใช้แผนกลาง",
            "plan",
            _PLAN_F,
            (
                Col("row_kind", "ระดับ"),
                Col("item_code", "รหัสสินค้า"),
                Col("item_name", "รายการ"),
                Col("unit", "หน่วย"),
                Col("purchasing_department", "หน่วยงานผู้ซื้อ (คลัง)"),
                Col("fund_source", "แหล่งเงิน"),
                Col("budget_category", "หมวดงบ"),
                Col("department", "หน่วยงานที่เบิก/คืน"),
                Col("planned_qty", "จำนวนตามแผน", ColType.QTY),
                Col("purchased_qty", "ซื้อแล้ว (PPR, จำนวน)", ColType.QTY),
                Col("remaining_qty", "แผนคงเหลือ ยังไม่ซื้อ (จำนวน)", ColType.QTY),
                Col("planned_amount", "วงเงินตามแผน (บาท)", ColType.MONEY),
                Col("purchased_amount", "ซื้อแล้ว (PPR, บาท)", ColType.MONEY),
                Col("remaining_amount", "แผนคงเหลือ (บาท)", ColType.MONEY),
                Col("issued_qty", "เบิกจากคลัง (HOSxP)", ColType.QTY),
                Col("returned_qty", "รับคืนเข้าคลัง (HOSxP)", ColType.QTY),
                Col("net_used_qty", "ใช้ไปสุทธิ (เบิก - คืน)", ColType.QTY),
                Col("not_issued_qty", "ซื้อแล้วยังไม่เบิก", ColType.QTY),
                Col("note", "หมายเหตุ"),
            ),
        ),
        ReportDef(
            "PLAN_AMENDMENT",
            "การปรับแผน",
            "amendment",
            (*_PLAN_F, "date_from", "date_to"),
            (
                Col("amendment_no", "ครั้งที่", ColType.INT),
                Col("approval_document_no", "เลขที่หนังสืออนุมัติ"),
                Col("approval_date", "วันที่อนุมัติ", ColType.DATE),
                Col("recorded_at", "บันทึกเมื่อ", ColType.DATETIME),
                Col("recorded_by", "ผู้บันทึก"),
                Col("change", "การเปลี่ยนแปลง"),
                Col("item_code", "รหัสสินค้า"),
                Col("item_name", "รายการ"),
                Col("department", "หน่วยงานเจ้าของแผน"),
                Col("fund_source", "แหล่งเงิน"),
                Col("budget_category", "หมวดงบ"),
                Col("qty_before", "จำนวนเดิม", ColType.QTY),
                Col("qty_after", "จำนวนใหม่", ColType.QTY),
                Col("amount_before", "วงเงินเดิม (บาท)", ColType.MONEY),
                Col("amount_after", "วงเงินใหม่ (บาท)", ColType.MONEY),
                Col("other_changes", "ข้อมูลอื่นที่เปลี่ยน"),
                Col("reason", "เหตุผล"),
            ),
        ),
        ReportDef(
            "AUDIT",
            "ประวัติการตรวจสอบ (Audit)",
            "audit",
            ("date_from", "date_to", "action", "entity_type"),
            (
                Col("occurred_at", "เวลา", ColType.DATETIME),
                Col("actor", "ผู้กระทำ"),
                Col("action", "การกระทำ"),
                Col("entity", "สิ่งที่ถูกกระทำ"),
                Col("reason", "เหตุผล"),
                Col("before", "ก่อน"),
                Col("after", "หลัง"),
            ),
        ),
    )
}

PENDING_STATES = (
    PprState.DRAFT,
    PprState.CONFIRMED_LOCKED,
    PprState.UNLOCKED_FOR_REVISION,
    PprState.PR_CHANGED_REVIEW_REQUIRED,
)
_FIXED_STATES: dict[str, tuple[PprState, ...]] = {
    "PENDING_PPR": PENDING_STATES,
    "CANCELLED_PPR": (PprState.CANCELLED,),
    "PR_CHANGED": (PprState.PR_CHANGED_REVIEW_REQUIRED,),
}
# Longest-waiting cases first where waiting is the point; newest first otherwise.
_OLDEST_FIRST = {"PENDING_PPR", "PR_CHANGED"}


def states_of(code: str) -> tuple[PprState, ...]:
    """The PPR states a report may be narrowed to (empty = not a PPR report)."""
    d = definition(code)
    if d.kind != "ppr":
        return ()
    if code in _FIXED_STATES:
        return _FIXED_STATES[code]
    return tuple(st for st in PprState if st is not PprState.DRAFT)  # register: confirmed


STATE_TEXT = {
    PprState.DRAFT: "ฉบับร่าง",
    PprState.CONFIRMED_LOCKED: "ยืนยันแล้ว (ล็อก)",
    PprState.PROCUREMENT_VERIFIED: "พัสดุตรวจสอบแล้ว",
    PprState.PR_CHANGED_REVIEW_REQUIRED: "PR มีการเปลี่ยนแปลง ต้องทบทวน",
    PprState.UNLOCKED_FOR_REVISION: "ปลดล็อกเพื่อแก้ไข",
    PprState.CANCELLED: "ยกเลิก",
    PprState.CLOSED: "ปิดแล้ว",
}
PLAN_TYPE_TEXT = {"DEPARTMENT": "แผนหน่วยงาน", "CENTRAL": "แผนกลาง", "ASSIGNED": "แผนซื้อแทน"}
ALERT_TEXT = {"PLAN_LOW": "ใกล้หมด", "PLAN_EXHAUSTED": "หมดแล้ว"}
FILTER_LABELS = {
    "fiscal_year": "ปีงบประมาณ",
    "department_id": "หน่วยงาน",
    "fund_source_id": "แหล่งเงิน",
    "budget_category_id": "หมวดงบ",
    "item": "รายการ",
    "state": "สถานะ",
    "date_from": "ตั้งแต่วันที่",
    "date_to": "ถึงวันที่",
    "action": "การกระทำ",
    "entity_type": "ประเภทข้อมูล",
}


# ------------------------------------------------------------------ helpers
class _Names:
    """Master names by source id (cached per report)."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow
        self._cache: dict[tuple[MasterKind, str], str] = {}

    def __call__(self, kind: MasterKind, source_id: str | None) -> str:
        if not source_id:
            return ""
        key = (kind, source_id)
        if key not in self._cache:
            row = self._uow.masters.get(kind, source_id)
            self._cache[key] = row.name if row else source_id
        return self._cache[key]


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=HOSPITAL_TZ)


def _percent(part: Decimal, whole: Decimal) -> Decimal | None:
    if whole <= ZERO:
        return None
    return (part * HUNDRED / whole).quantize(Decimal("0.01"))


def _applied(d: ReportDef, f: Filters, names: _Names, scope: str | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    values: dict[str, Any] = {
        "fiscal_year": f.fiscal_year,
        "department_id": names(MasterKind.DEPARTMENT, f.department_id) if f.department_id else None,
        "fund_source_id": names(MasterKind.FUND_SOURCE, f.fund_source_id)
        if f.fund_source_id
        else None,
        "budget_category_id": names(MasterKind.BUDGET_CATEGORY, f.budget_category_id)
        if f.budget_category_id
        else None,
        "item": f.item,
        "state": STATE_TEXT.get(f.state) if f.state else None,
        "date_from": f.date_from.isoformat() if f.date_from else None,
        "date_to": f.date_to.isoformat() if f.date_to else None,
        "action": f.action,
        "entity_type": f.entity_type,
    }
    for key in d.filters:
        if values.get(key) not in (None, ""):
            out.append((FILTER_LABELS[key], str(values[key])))
    if scope == "" and d.kind != "audit":  # D-41: an account without a department
        out.append(("ขอบเขต", "บัญชีนี้ไม่มีหน่วยงาน จึงไม่แสดงข้อมูลแผน"))
    elif scope is not None and d.kind != "audit":
        out.append(("ขอบเขต", "เฉพาะหน่วยงาน " + (names(MasterKind.DEPARTMENT, scope) or "-")))
    return out


def _check(d: ReportDef, f: Filters) -> None:
    if d.kind in ("plan", "amendment") and f.fiscal_year is None:
        raise InvalidInputError("FISCAL_YEAR_REQUIRED", "a plan report needs a fiscal year")
    for d_ in (f.date_from, f.date_to):
        if d_ is not None and not EARLIEST <= d_ <= LATEST:
            raise InvalidInputError("INVALID_DATE_RANGE", "dates must be between 2000 and 2200")
    if f.date_from and f.date_to and f.date_to < f.date_from:
        raise InvalidInputError("INVALID_DATE_RANGE", "the end date is before the start date")
    if f.state is not None and f.state not in states_of(d.code):
        raise InvalidInputError(
            "STATE_NOT_IN_REPORT", f"{f.state.value} is not a state of this report"
        )


# ------------------------------------------------------------------ plan reports
def _dept_of(r: PlanItemRecord) -> str:
    """The department a Plan Item is reported under (D-38 C-4: purchaser of a CENTRAL item)."""
    return responsible_department(r.plan_type, r.owner_department_id, r.purchasing_department_id)


def _plan_statuses(uow: UnitOfWork, f: Filters, plan_scope: str | None) -> list[ItemStatus]:
    assert f.fiscal_year is not None
    if uow.plan_years.get(f.fiscal_year) is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {f.fiscal_year} not found")
    low = uow.alert_settings.all()[AlertSetting.PLAN_LOW_PERCENT].value
    out = []
    text = (f.item or "").strip().lower()
    for s in item_statuses(uow, f.fiscal_year, plan_scope, low):
        r = s.item
        if f.department_id and _dept_of(r) != f.department_id:
            continue
        if f.fund_source_id and r.fund_source_id != f.fund_source_id:
            continue
        if f.budget_category_id and r.budget_category_id != f.budget_category_id:
            continue
        if text and text not in f"{r.item_id or ''} {r.item_code or ''} {r.item_name}".lower():
            continue
        out.append(s)
    return out


def _group(
    statuses: Iterable[ItemStatus], key: Callable[[ItemStatus], Any]
) -> dict[Any, dict[str, Any]]:
    groups: dict[Any, dict[str, Any]] = {}
    for s in statuses:
        g = groups.setdefault(
            key(s),
            {
                "items": 0,
                "planned_amount": ZERO,
                "used_amount": ZERO,
                "remaining_amount": ZERO,
                "low": 0,
                "exhausted": 0,
            },
        )
        g["items"] += 1
        g["planned_amount"] += s.planned_amount
        g["used_amount"] += s.used_amount
        g["remaining_amount"] += s.remaining_amount
        g["low"] += s.level.alert is not None and s.level.alert.value == "PLAN_LOW"
        g["exhausted"] += s.level.alert is not None and s.level.alert.value == "PLAN_EXHAUSTED"
    for g in groups.values():
        g["used_percent"] = _percent(g["used_amount"], g["planned_amount"])
    return groups


# ------------------------------------------------------------------ Central Plan Usage (D-39)
NO_FUND_SPLIT = "ไม่สามารถแบ่งยอดตามแหล่งเงินได้จากข้อมูล HOSxP"
ROW_PLAN = "รายการแผน"
NOT_BOUND = "ไม่มีรหัสสินค้า HOSxP: ไม่มีข้อมูลเบิกจ่าย (D-43)"
ROW_ITEM_TOTAL = "รวมสินค้า (ทุกรายการแผน)"
ROW_DEPARTMENT = "หน่วยงานเบิก/คืน"
OVER_ISSUED = "เบิกมากกว่าที่ซื้อในปีงบนี้ (เช่น ของคงคลังเดิม)"
OTHER_SOURCES = (
    "ยอดเบิกของสินค้านี้ใน HOSxP อาจมาจากการซื้อตามแผนประเภทอื่นของปีนี้ จึงผูกกับแผนกลางไม่ได้"
    ' และคำนวณ "ซื้อแล้วยังไม่เบิก" ไม่ได้'
)
OTHER_PURCHASER = (
    'ยอดตามแผน/ซื้อแล้วรวมเฉพาะแผนที่แสดงในรายงานนี้ มีหน่วยงานผู้ซื้ออื่นของสินค้านี้ จึงคำนวณ "ซื้อแล้วยังไม่เบิก" ไม่ได้'
)


def stock_period(uow: UnitOfWork, fiscal_year: int, today: date) -> tuple[date, date]:
    """The fiscal year so far (hospital dates)."""
    year = uow.plan_years.get(fiscal_year)
    if year is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
    return year.starts_on, min(year.ends_on, today)


def _stock_cells(
    t: StockTotals | None, stock: StockRead, purchased: Decimal | None
) -> dict[str, Any]:
    """Issued / returned / net used (and, for item rows, bought but not issued).
    ``t`` None = unknown (an unmapped movement): every cell says so, none becomes 0."""
    if not stock.available or t is None:
        cells: dict[str, Any] = {k: NO_DATA for k in ("issued_qty", "returned_qty")}
        cells["net_used_qty"] = NO_DATA
        if purchased is not None:
            cells["not_issued_qty"] = NO_DATA
        return cells
    cells = {"issued_qty": t.issued}
    if stock.returns_known:
        cells["returned_qty"] = t.returned
        cells["net_used_qty"] = t.net_used
        if purchased is not None:
            cells["not_issued_qty"] = purchased - t.net_used
    else:  # IT has not said how HOSxP records a return: no net figure is guessed
        cells.update(returned_qty=NO_DATA, net_used_qty=NO_DATA)
        if purchased is not None:
            cells["not_issued_qty"] = NO_DATA
    return cells


def _stock_notes(stock: StockRead, *, whole: bool = True) -> list[str]:
    """``whole`` = the caller sees the whole report (organisation-wide or a purchaser);
    a department that sees only its own rows is not told hospital-wide counts."""
    if not stock.available:
        return [f"ยอดเบิก/คืนจากคลัง: {NO_DATA} ({stock.reason})"]
    v = stock.verification
    out = [
        "ยอดเบิก/คืนจากคลังอ่านจาก HOSxP ขณะสร้างรายงาน"
        f" ช่วงวันที่ {stock.date_from} ถึง {stock.date_to}"
        + (f" (query ยืนยันโดย {v.verified_by} เมื่อ {v.verified_on} อ้างอิง {v.reference})" if v else "")
        + " ใช้แสดงผลเท่านั้น ไม่ลดหรือเพิ่มแผนและไม่บันทึกในระบบนี้"
    ]
    if not stock.returns_known:
        out.append(f'ยังไม่ได้กำหนดประเภทการรับคืนของ HOSxP: ยอดรับคืนและยอดใช้ไปสุทธิแสดง "{NO_DATA}"')
    if stock.unmapped:
        count = f" {stock.unmapped:,} รายการ" if whole else "บางรายการ"
        out.append(
            f"ข้อมูลไม่ครบ: มีรายการเคลื่อนไหว{count}ที่ยังไม่ได้กำหนดประเภท (เบิก/คืน)"
            f' ยอดของสินค้าและหน่วยงานที่เกี่ยวข้องจึงแสดง "{NO_DATA}" (ไม่นับเป็นศูนย์)'
        )
    return out


def _sum(statuses: list[ItemStatus], attr: str) -> Decimal:
    return sum((getattr(s, attr) for s in statuses), ZERO)


def _central_rows(
    uow: UnitOfWork, f: Filters, plan_scope: str | None, names: _Names, stock: StockRead
) -> list[dict[str, Any]]:
    """CENTRAL items, per HOSxP item (D-38 C-1, D-39).

    * Items the caller may read as Plan Items (purchasing department, organisation-wide
      roles - C-4): plan rows (usage = the central store's PPRs), the item's stock totals and
      one row per receiving department. When the HOSxP item is in more than one CENTRAL Plan
      Item, the stock totals are shown once for the item, marked ``NO_FUND_SPLIT``: HOSxP
      movements carry no fund source, so nothing is divided.
    * Any other department: only its own issues / returns / net use of central items it
      received - no plan figure, fund source or purchaser, never another department.
    The department filter is the purchasing department.
    """
    assert f.fiscal_year is not None
    statuses = _plan_statuses(
        uow,
        Filters(fiscal_year=f.fiscal_year, item=f.item),
        plan_scope,
    )
    # Every Plan Item of the year, of any type (internal only: decides whether an item's
    # stock figures can be tied to its central plan lines; nothing of it is shown beyond the
    # caller's scope). HOSxP issues of an item that is also bought under another plan type
    # cannot be attributed to the Central Plan - they are never allocated by inference.
    every_plan: dict[str, list[PlanItemRecord]] = {}
    for r in uow.plans.items(f.fiscal_year):
        if r.item_id is not None:  # D-42: an unbound imported row has no HOSxP stock to tie
            every_plan.setdefault(r.item_id, []).append(r)
    every_central = {
        i: c
        for i, rs in every_plan.items()
        if (c := [r for r in rs if r.plan_type is PlanType.CENTRAL])
    }

    groups: dict[str, list[ItemStatus]] = {}
    for s in statuses:
        if s.item.plan_type is PlanType.CENTRAL and (
            not f.department_id or s.item.purchasing_department_id == f.department_id
        ):
            key = s.item.item_id if s.item.item_id is not None else f"#{s.item.id}"
            groups.setdefault(key, []).append(s)

    def passes(s: ItemStatus) -> bool:
        return (not f.fund_source_id or s.item.fund_source_id == f.fund_source_id) and (
            not f.budget_category_id or s.item.budget_category_id == f.budget_category_id
        )

    def plan_cells(group: list[ItemStatus]) -> dict[str, Any]:
        return {
            "planned_qty": _sum(group, "planned_qty"),
            "purchased_qty": _sum(group, "used_qty"),
            "remaining_qty": _sum(group, "remaining_qty"),
            "planned_amount": _sum(group, "planned_amount"),
            "purchased_amount": _sum(group, "used_amount"),
            "remaining_amount": _sum(group, "remaining_amount"),
        }

    def head(r: PlanItemRecord) -> dict[str, Any]:
        return {"item_code": r.item_code, "item_name": r.item_name, "unit": r.unit}

    def department_rows(r: PlanItemRecord, only: str | None) -> list[dict[str, Any]]:
        per = stock.departments(r.item_id) if stock.available and r.item_id else {}
        rows = []
        for dept, t in sorted(per.items(), key=lambda kv: names(MasterKind.DEPARTMENT, kv[0])):
            if only is not None and dept != only:
                continue
            rows.append(
                {
                    "row_kind": ROW_DEPARTMENT,
                    **head(r),
                    "department": names(MasterKind.DEPARTMENT, dept),
                    **_stock_cells(t, stock, None),
                }
            )
        return rows

    out: list[dict[str, Any]] = []
    order = sorted(
        groups.values(),
        key=lambda g: (
            g[0].item.purchasing_department_id or "",
            g[0].item.item_code or "",
            g[0].item.id,
        ),
    )
    for group in order:
        shown = sorted((s for s in group if passes(s)), key=lambda s: s.item.id)
        if not shown:
            continue
        first = shown[0].item
        if first.item_id is None:
            # D-42: not yet bound to a HOSxP item - no issue figures exist for it; they are
            # shown as unknown, never as 0.
            for s in shown:
                out.append(
                    {
                        "row_kind": ROW_PLAN,
                        **head(s.item),
                        "purchasing_department": names(
                            MasterKind.DEPARTMENT, s.item.purchasing_department_id
                        ),
                        "fund_source": names(MasterKind.FUND_SOURCE, s.item.fund_source_id),
                        "budget_category": names(
                            MasterKind.BUDGET_CATEGORY, s.item.budget_category_id
                        ),
                        **plan_cells([s]),
                        **_stock_cells(None, stock, s.used_qty),
                        "note": NOT_BOUND,
                    }
                )
            continue
        siblings = every_central.get(first.item_id, [])
        other_types = len(every_plan.get(first.item_id, [])) > len(siblings)
        notes: list[str] = []
        if len(siblings) > 1:
            split = NO_FUND_SPLIT
            if len({x.purchasing_department_id for x in siblings}) > 1:
                split += " และตามหน่วยงานผู้ซื้อ"
            notes.append(split)
        # The item's issues sit on its one plan line only when that line is the item's only
        # plan line of the year, of any type.
        single = len(siblings) == 1 and not other_types
        # Bought (these lines) can be set against issued (the whole item) only when every
        # plan line of the item is a CENTRAL line in the caller's view.
        if other_types:
            notes.append(OTHER_SOURCES)
        elif {s.item.id for s in group} != {x.id for x in siblings}:
            notes.append(OTHER_PURCHASER)
        comparable = not other_types and {s.item.id for s in group} == {x.id for x in siblings}
        total = stock.item_total(first.item_id) if stock.available else None
        for s in shown:
            row: dict[str, Any] = {
                "row_kind": ROW_PLAN,
                **head(s.item),
                "purchasing_department": names(
                    MasterKind.DEPARTMENT, s.item.purchasing_department_id
                ),
                "fund_source": names(MasterKind.FUND_SOURCE, s.item.fund_source_id),
                "budget_category": names(MasterKind.BUDGET_CATEGORY, s.item.budget_category_id),
                **plan_cells([s]),
                "note": "",
            }
            if single:
                row.update(_stock_cells(total, stock, s.used_qty))
            out.append(row)
        if not single:
            purchased = _sum(group, "used_qty")
            out.append(
                {
                    "row_kind": ROW_ITEM_TOTAL,
                    **head(first),
                    "purchasing_department": ", ".join(
                        sorted(
                            {
                                names(MasterKind.DEPARTMENT, s.item.purchasing_department_id)
                                for s in group
                            }
                        )
                    ),
                    **plan_cells(group),
                    **_stock_cells(total, stock, purchased),
                    "note": "; ".join(notes),
                }
            )
            if not comparable:
                out[-1]["not_issued_qty"] = NO_DATA
        carrier = out[-1]  # the row with the item's stock totals (plan row or item total)
        nq = carrier.get("not_issued_qty")
        if isinstance(nq, Decimal) and nq < ZERO:
            carrier["note"] = "; ".join(x for x in (carrier["note"], OVER_ISSUED) if x)
        out.extend(department_rows(first, None))

    # Other departments: their own consumption only (D-39 visibility).
    own = plan_scope
    if (
        own
        and stock.available
        and not f.fund_source_id
        and not f.budget_category_id
        and (not f.department_id or f.department_id == own)
    ):
        text = (f.item or "").strip().lower()
        for item_id, records in sorted(
            every_central.items(), key=lambda kv: (kv[1][0].item_code or "", kv[0])
        ):
            if item_id in groups:  # already shown in full to its purchaser
                continue
            r = records[0]
            if text and text not in f"{r.item_id or ''} {r.item_code or ''} {r.item_name}".lower():
                continue
            out.extend(department_rows(r, own))
    return out


def _plan_rows(
    uow: UnitOfWork, d: ReportDef, f: Filters, plan_scope: str | None, names: _Names
) -> list[dict[str, Any]]:
    statuses = _plan_statuses(uow, f, plan_scope)
    if d.code == "PLAN_REMAINING":
        return [
            {
                "item_code": s.item.item_code,
                "item_name": s.item.item_name,
                "unit": s.item.unit,
                "plan_type": PLAN_TYPE_TEXT.get(s.item.plan_type.value, s.item.plan_type.value),
                "department": names(MasterKind.DEPARTMENT, _dept_of(s.item)),
                "fund_source": names(MasterKind.FUND_SOURCE, s.item.fund_source_id),
                "budget_category": names(MasterKind.BUDGET_CATEGORY, s.item.budget_category_id),
                "planned_qty": s.planned_qty,
                "used_qty": s.used_qty,
                "remaining_qty": s.remaining_qty,
                "planned_amount": s.planned_amount,
                "used_amount": s.used_amount,
                "remaining_amount": s.remaining_amount,
                "remaining_percent": _percent(s.remaining_amount, s.planned_amount),
                "alert": ALERT_TEXT.get(s.level.alert.value, "") if s.level.alert else "",
            }
            for s in sorted(
                statuses,
                key=lambda s: (_dept_of(s.item), s.item.item_code or "", s.item.id),
            )
        ]
    if d.code == "DEPARTMENT_UTILIZATION":
        groups = _group(statuses, lambda s: _dept_of(s.item))
        return [
            {"department": names(MasterKind.DEPARTMENT, k), **g} for k, g in sorted(groups.items())
        ]
    if d.code == "CATEGORY_UTILIZATION":
        groups = _group(statuses, lambda s: s.item.budget_category_id)
        return [
            {"budget_category": names(MasterKind.BUDGET_CATEGORY, k), **g}
            for k, g in sorted(groups.items())
        ]
    # PLAN_SUMMARY: every budget line, with or without visible items.
    assert f.fiscal_year is not None
    groups = _group(statuses, lambda s: (s.item.fund_source_id, s.item.budget_category_id))
    rows = []
    for b in uow.plans.budgets(f.fiscal_year):
        key = (b.fund_source_id, b.budget_category_id)
        if f.fund_source_id and b.fund_source_id != f.fund_source_id:
            continue
        if f.budget_category_id and b.budget_category_id != f.budget_category_id:
            continue
        g = groups.pop(key, None)
        if g is None and plan_scope is not None:
            continue  # a department user sees only lines their items use
        rows.append(
            {
                "fund_source": names(MasterKind.FUND_SOURCE, b.fund_source_id),
                "budget_category": names(MasterKind.BUDGET_CATEGORY, b.budget_category_id),
                # The approved envelope is organisation-wide: shown to organisation-wide roles.
                "approved_budget": b.approved_amount if plan_scope is None else None,
                **(g or _empty_group()),
            }
        )
    return rows


def _empty_group() -> dict[str, Any]:
    return {
        "items": 0,
        "planned_amount": ZERO,
        "used_amount": ZERO,
        "remaining_amount": ZERO,
        "used_percent": None,
        "low": 0,
        "exhausted": 0,
    }


# ------------------------------------------------------------------ PPR reports
def _ppr_rows(
    uow: UnitOfWork,
    d: ReportDef,
    f: Filters,
    caller: Caller,
    names: _Names,
    now: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    states: tuple[PprState, ...] = _FIXED_STATES.get(d.code, ())
    if f.state is not None:
        states = (f.state,)
    search = PprSearch(
        department_id=f.department_id,
        fund_source_id=f.fund_source_id,
        budget_category_id=f.budget_category_id,
        item=f.item,
        fiscal_year=f.fiscal_year,
        states=states,
        confirmed_from=_day_start(f.date_from) if f.date_from else None,
        confirmed_to=_day_start(f.date_to + timedelta(days=1)) if f.date_to else None,
        confirmed_only=d.code == "PPR_REGISTER",
        limit=MAX_ROWS,
        oldest_first=d.code in _OLDEST_FIRST,
    )
    records = uow.pprs.find(search, caller.ppr_scope)
    total = uow.pprs.count(search, caller.ppr_scope)
    ids = [r.id for r in records]
    first = uow.pprs.first_confirmations(ids)
    verified = uow.pprs.last_verifications(ids) if d.code == "PPR_REGISTER" else {}
    users: dict[int, str] = {}

    def user(uid: int) -> str:
        if uid not in users:
            u = uow.users.get(uid)
            users[uid] = (u.display_name or u.username) if u else str(uid)
        return users[uid]

    def row(p: PprRecord) -> dict[str, Any]:
        return {
            "ppr_number": p.ppr_number or "(ร่าง)",
            "pr_no": p.pr_no,
            "fiscal_year": p.fiscal_year,
            "department": names(MasterKind.DEPARTMENT, p.department_id),
            "fund_source": names(MasterKind.FUND_SOURCE, p.fund_source_id),
            "allocations": ", ".join(
                f"{names(MasterKind.BUDGET_CATEGORY, a.budget_category_id)} {a.amount}"
                for a in p.allocations
            ),
            "required_amount": p.required_amount,
            "state": STATE_TEXT.get(p.state, p.state.value),
            "version": p.current_version or None,
            "first_confirmed_at": first.get(p.id),
            "verified_at": verified.get(p.id),
            "requester": user(p.created_by_user_id),
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "days_inactive": max(0, (now - p.updated_at).days),
            "hosxp_status": p.hosxp_native_status or p.hosxp_pr_status,
        }

    return [row(p) for p in records], total > len(records)


# ------------------------------------------------------------------ audit report
def _audit_rows(uow: UnitOfWork, f: Filters) -> tuple[list[dict[str, Any]], bool]:
    rows = uow.audit.search(
        _day_start(f.date_from) if f.date_from else None,
        _day_start(f.date_to + timedelta(days=1)) if f.date_to else None,
        (f.action or "").strip() or None,
        (f.entity_type or "").strip() or None,
        MAX_ROWS + 1,
    )
    users: dict[int, str] = {}

    def actor(uid: int | None) -> str:
        if uid is None:
            return "ระบบ"
        if uid not in users:
            u = uow.users.get(uid)
            users[uid] = (u.display_name or u.username) if u else str(uid)
        return users[uid]

    def compact(v: dict[str, Any] | None) -> str:
        if not v:
            return ""
        return "; ".join(f"{k}={v[k]}" for k in sorted(v))[:2000]

    out = [
        {
            "occurred_at": r.occurred_at,
            "actor": actor(r.actor_user_id),
            "action": r.action,
            "entity": f"{r.entity_type} {r.entity_id}",
            "reason": r.reason or "",
            "before": compact(r.before),
            "after": compact(r.after),
        }
        for r in rows[:MAX_ROWS]
    ]
    return out, len(rows) > MAX_ROWS


# ------------------------------------------------------------------ amendment report (D-37)
CHANGE_TEXT = {
    ("ITEM", "UPDATE"): "แก้ไขรายการแผน",
    ("ITEM", "CREATE"): "เพิ่มรายการแผนใหม่",
    ("BUDGET", "UPDATE"): "แก้วงเงินหมวดงบ",
    ("BUDGET", "CREATE"): "เพิ่มหมวดงบใหม่",
    ("DEMAND", "UPDATE"): "แก้ความต้องการของหน่วยงาน",
    ("DEMAND", "CREATE"): "เพิ่มความต้องการของหน่วยงาน",
}
_DATA_TEXT = {
    "item_id": ("รายการ", MasterKind.ITEM),
    "owner_department_id": ("หน่วยงานเจ้าของแผน", MasterKind.DEPARTMENT),
    "purchasing_department_id": ("หน่วยงานผู้ซื้อ", MasterKind.DEPARTMENT),
    "budget_category_id": ("หมวดงบ", MasterKind.BUDGET_CATEGORY),
}


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _amendment_rows(
    uow: UnitOfWork, f: Filters, plan_scope: str | None, names: _Names
) -> list[dict[str, Any]]:
    """One row per change of every amendment of the year (newest amendment first).

    Scope and filters follow the plan reports: a requester sees only item changes of their own
    department (owner or purchaser, before or after); the department filter keeps item changes
    of that department; fund, category and item filters match the before or the after values.
    """
    assert f.fiscal_year is not None
    records = am.scoped(am.list_amendments(uow, f.fiscal_year), plan_scope)
    text = (f.item or "").strip().lower()
    users: dict[int, str] = {}
    out: list[dict[str, Any]] = []
    for rec in records:
        if f.date_from and rec.approval_date < f.date_from:
            continue
        if f.date_to and rec.approval_date > f.date_to:
            continue
        if rec.created_by_user_id not in users:
            u = uow.users.get(rec.created_by_user_id)
            users[rec.created_by_user_id] = (
                (u.display_name or u.username) if u else str(rec.created_by_user_id)
            )
        for c in rec.changes:
            sides = [x for x in (c.before, c.after) if x]
            item = (
                uow.plans.get_item(int(c.after["plan_item_id"])) if c.target == "DEMAND" else None
            )
            if item is not None:  # a demand row: fund and category are the item's
                extra = {
                    "fund_source_id": item.fund_source_id,
                    "budget_category_id": item.budget_category_id,
                }
                sides = [{**x, **extra} for x in sides]
            if f.department_id and not (
                (c.target == "ITEM" and am.touches_department(c, f.department_id))
                or (c.target == "DEMAND" and c.after.get("department_id") == f.department_id)
            ):
                continue
            if f.fund_source_id and sides[-1].get("fund_source_id") != f.fund_source_id:
                continue
            if f.budget_category_id and not any(
                x.get("budget_category_id") == f.budget_category_id for x in sides
            ):
                continue
            after, before = c.after, c.before or {}
            row: dict[str, Any] = {
                "amendment_no": rec.amendment_no,
                "approval_document_no": rec.approval_document_no,
                "approval_date": rec.approval_date,
                "recorded_at": rec.created_at,
                "recorded_by": users[rec.created_by_user_id],
                "change": CHANGE_TEXT.get((c.target, c.change_kind), c.change_kind),
                "item_code": "",
                "item_name": "",
                "department": "",
                "fund_source": names(MasterKind.FUND_SOURCE, sides[-1].get("fund_source_id")),
                "budget_category": names(
                    MasterKind.BUDGET_CATEGORY, sides[-1].get("budget_category_id")
                ),
                "qty_before": None,
                "qty_after": None,
                "amount_before": None,
                "amount_after": None,
                "other_changes": "",
                "reason": rec.reason,
            }
            if c.target == "ITEM":
                # The history keeps the item's code and name as they were; older records
                # without them fall back to the current HOSxP record.
                item_id = after.get("item_id")  # None: an imported row not yet bound (D-42)
                master = uow.masters.get(MasterKind.ITEM, str(item_id)) if item_id else None
                row["item_code"] = after.get("item_code") or (
                    master.code if master else (str(item_id) if item_id else "")
                )
                row["item_name"] = after.get("item_name") or (master.name if master else "")
                row["department"] = names(
                    MasterKind.DEPARTMENT,
                    responsible_department(
                        PlanType(after.get("plan_type", PlanType.DEPARTMENT.value)),
                        str(after.get("owner_department_id") or ""),
                        after.get("purchasing_department_id"),
                    ),
                )
                row["qty_before"] = _dec(before.get("planned_qty"))
                row["qty_after"] = _dec(after.get("planned_qty"))
                row["amount_before"] = _dec(before.get("planned_amount"))
                row["amount_after"] = _dec(after.get("planned_amount"))
                row["other_changes"] = "; ".join(
                    f"{label}: {names(kind, before.get(key)) or '-'} → "
                    f"{names(kind, after.get(key)) or '-'}"
                    for key, (label, kind) in _DATA_TEXT.items()
                    if c.before is not None and before.get(key) != after.get(key)
                )
                if before.get("estimated_unit_price") not in (
                    None,
                    after.get("estimated_unit_price"),
                ):
                    price = (
                        f"ราคาประมาณ: {before['estimated_unit_price']} → "
                        f"{after['estimated_unit_price']}"
                    )
                    row["other_changes"] = "; ".join(x for x in (row["other_changes"], price) if x)
                if text and text not in (
                    f"{after.get('item_id')} {before.get('item_id', '')} "
                    f"{row['item_code']} {row['item_name']}".lower()
                ):
                    continue
            elif c.target == "DEMAND":
                # D-38 A-6: one department's demand on an ASSIGNED item (quantities only).
                row["item_code"] = item.item_code if item else ""
                row["item_name"] = item.item_name if item else ""
                row["department"] = names(MasterKind.DEPARTMENT, after.get("department_id"))
                row["qty_before"] = _dec(before.get("demand_qty"))
                row["qty_after"] = _dec(after.get("demand_qty"))
                if text and text not in (
                    f"{item.item_id if item else ''} {row['item_code']} {row['item_name']}".lower()
                ):
                    continue
            else:
                if text:
                    continue
                row["amount_before"] = _dec(before.get("approved_amount"))
                row["amount_after"] = _dec(after.get("approved_amount"))
            out.append(row)
    return out


# ------------------------------------------------------------------ entry points
def definition(code: str) -> ReportDef:
    d = REPORTS.get(code)
    if d is None:
        raise NotFoundError("REPORT_NOT_FOUND", f"unknown report {code!r}")
    return d


def run(
    uow: UnitOfWork,
    code: str,
    caller: Caller,
    plan_scope: str | None,
    can_audit: bool,
    f: Filters,
    now: datetime,
    gateway: HosxpGateway | None = None,
    today: date | None = None,
) -> Report:
    """``gateway`` is used by *Central Plan Usage* only, to read stock movements (D-39) up to
    ``today`` (the server's hospital date; default: from ``now``); without a gateway, that
    report shows "ยังไม่มีข้อมูล" for them."""
    d = definition(code)
    if d.kind == "audit" and not can_audit:
        raise ForbiddenError("FORBIDDEN", "the audit report requires AUDIT_READ")
    # Only the filters a report lists apply (and are shown on it): no hidden narrowing.
    f = Filters(**{k: v if k in d.filters else None for k, v in f.__dict__.items()})
    _check(d, f)
    names = _Names(uow)
    notes: list[str] = []
    if d.code == "CENTRAL_PLAN_USAGE":
        assert f.fiscal_year is not None
        day = today if today is not None else now.astimezone(HOSPITAL_TZ).date()
        period = stock_period(uow, f.fiscal_year, day)
        stock = read_stock(gateway, *period) if gateway is not None else not_available(NO_DATA)
        rows = _central_rows(uow, f, plan_scope, names, stock)
        whole = plan_scope is None or any(r["row_kind"] != ROW_DEPARTMENT for r in rows)
        notes.extend(_stock_notes(stock, whole=whole))
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
    elif d.kind == "plan":
        rows = _plan_rows(uow, d, f, plan_scope, names)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
    elif d.kind == "amendment":
        rows = _amendment_rows(uow, f, plan_scope, names)
        truncated = len(rows) > MAX_ROWS
        rows = rows[:MAX_ROWS]
    elif d.kind == "ppr":
        rows, truncated = _ppr_rows(uow, d, f, caller, names, now)
        if d.code == "PENDING_PPR" and (f.date_from or f.date_to):
            notes.append("ใช้ช่วงวันที่ยืนยันครั้งแรก ฉบับร่างจึงไม่อยู่ในผล")
    else:
        rows, truncated = _audit_rows(uow, f)
    if truncated:
        notes.append(f"แสดง {MAX_ROWS:,} แถวแรก ใช้ตัวกรองเพื่อให้ได้ครบ")
    applied = _applied(d, f, names, None if d.kind == "ppr" else plan_scope)
    if d.kind == "ppr" and caller.ppr_scope is not None:
        applied.append(("ขอบเขต", "เฉพาะ PPR ที่คุณสร้าง"))
    return Report(d, rows, truncated, applied, now, notes)


def record_export(uow: UnitOfWork, actor: Actor, report: Report) -> None:
    """A downloaded file leaves the application: who took which report with which filters."""
    uow.audit.write(
        AuditEntry(
            actor,
            "REPORT_EXPORTED",
            "report",
            report.definition.code,
            after={
                "filters": [[k, v] for k, v in report.filters],
                "rows": len(report.rows),
                "truncated": report.truncated,
            },
        )
    )
