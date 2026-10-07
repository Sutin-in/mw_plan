# ruff: noqa: E501 - HTML template text
"""Printed PPR (spec §35, D-20): an A4 HTML page with print CSS.

The page is built ONLY from an append-only confirmation snapshot (``ppr_version``), so a
printed PPR always shows exactly what was confirmed. If a newer version exists, or the
PPR is no longer in a confirmed state, the page says so prominently, so an old print
cannot silently keep an outdated meaning (§35). Browser "Print / Save as PDF" is enough
for Phase 1.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

from ppr.domain.ppr_state import PprState

_STATE_TH = {
    PprState.CONFIRMED_LOCKED: "ยืนยันและล็อกแล้ว",
    PprState.PROCUREMENT_VERIFIED: "พัสดุตรวจสอบและยืนยันแล้ว",
    PprState.PR_CHANGED_REVIEW_REQUIRED: "PR มีการเปลี่ยนแปลง รอตรวจทาน",
    PprState.UNLOCKED_FOR_REVISION: "ปลดล็อกเพื่อแก้ไข",
    PprState.CANCELLED: "ยกเลิก",
    PprState.CLOSED: "ปิดแล้ว",
    PprState.DRAFT: "ร่าง",
}


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _money(v: Any) -> str:
    try:
        return f"{Decimal(str(v)):,.2f}"
    except (InvalidOperation, ValueError):
        return _e(v)


def _qty(v: Any) -> str:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return _e(v)
    text = f"{d:,.4f}".rstrip("0").rstrip(".")
    return text or "0"


CSS = """
@page { size: A4; margin: 14mm 12mm; }
* { box-sizing: border-box; }
body { font-family: "TH Sarabun New", "Sarabun", "Leelawadee UI", "Tahoma", sans-serif;
       font-size: 14px; color: #000; margin: 0; }
.sheet { max-width: 186mm; margin: 0 auto; }
h1 { font-size: 20px; text-align: center; margin: 0 0 2px; }
.sub { text-align: center; margin: 0 0 10px; }
.meta { display: flex; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.meta div { border: 1px solid #000; padding: 4px 8px; }
table { width: 100%; border-collapse: collapse; margin: 6px 0 10px; }
th, td { border: 1px solid #000; padding: 3px 5px; vertical-align: top; }
th { background: #eee; font-weight: bold; }
td.n { text-align: right; white-space: nowrap; }
.info td:first-child { width: 28%; font-weight: bold; background: #f7f7f7; }
.warn { border: 2px solid #b00; color: #b00; padding: 6px 8px; margin: 6px 0;
        font-weight: bold; text-align: center; }
.note { border: 1px dashed #000; padding: 5px 8px; margin: 6px 0; }
.sign { display: flex; justify-content: space-between; gap: 10px; margin-top: 26px; }
.sign div { flex: 1; text-align: center; }
.line { border-bottom: 1px dotted #000; height: 30px; margin: 0 10px 4px; }
.foot { margin-top: 14px; font-size: 12px; border-top: 1px solid #000; padding-top: 4px; }
@media print { .noprint { display: none; } th { -webkit-print-color-adjust: exact;
       print-color-adjust: exact; } }
"""


def render_ppr(
    snapshot: dict[str, Any],
    document_code: str,
    *,
    current_state: PprState,
    current_version: int,
) -> str:
    s = snapshot
    pr = s.get("pr", {})
    names = s.get("names", {})
    version = int(s["version"])
    warnings: list[str] = []
    if version != current_version:
        warnings.append(
            f"ฉบับนี้คือเวอร์ชัน {version} ซึ่งไม่ใช่ฉบับปัจจุบัน (ฉบับปัจจุบันคือเวอร์ชัน {current_version}) ห้ามใช้อ้างอิง"
        )
    if current_state not in (PprState.CONFIRMED_LOCKED, PprState.PROCUREMENT_VERIFIED):
        warnings.append(f"สถานะปัจจุบันของ PPR: {_STATE_TH.get(current_state, current_state)}")

    item_rows = "".join(
        f"<tr><td class='n'>{n}</td>"
        f"<td>{_e(i.get('item_name') or i.get('item_id'))}"
        f"<br><small>รหัส {_e(i.get('item_id'))}</small></td>"
        f"<td class='n'>{_qty(i['qty'])}</td><td>{_e(i.get('unit'))}</td>"
        f"<td class='n'>{_money(i['unit_price'])}</td><td class='n'>{_money(i['amount'])}</td>"
        f"<td>{_e(i.get('plan_item_name'))}"
        + (f" ({_e(i['plan_item_unit'])})" if i.get("plan_item_unit") else "")
        + f"<br><small>แผน: {_qty(i.get('planned_qty'))} / "
        f"{_money(i.get('planned_amount'))}</small></td>"
        f"<td class='n'>{_qty(i.get('remaining_qty_after'))}</td>"
        f"<td class='n'>{_money(i.get('remaining_amount_after'))}</td></tr>"
        for n, i in enumerate(s.get("items", []), 1)
    )
    cat_names = names.get("categories", {})
    alloc_rows = "".join(
        f"<tr><td>{_e(cat_names.get(a['budget_category_id']) or a['budget_category_id'])}"
        f" <small>({_e(a['budget_category_id'])})</small></td>"
        f"<td class='n'>{_money(a['amount'])}</td></tr>"
        for a in s.get("allocations", [])
    )
    multi = ""
    if s.get("multi_category"):
        multi = (
            "<div class='note'><b>ใช้เงินจากมากกว่า 1 หมวดงบ</b> - อ้างอิงการปรับงบใน HOSxP: "
            f"{_e(s.get('adjustment_reference'))}</div>"
        )
    warn_html = "".join(f"<div class='warn'>{_e(w)}</div>" for w in warnings)

    return f"""<!doctype html>
<html lang="th"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(s["ppr_number"])} v{version}</title><style>{CSS}</style></head>
<body><div class="sheet">
<p class="noprint"><button onclick="window.print()">พิมพ์ / บันทึกเป็น PDF</button></p>
<h1>คำขอใช้แผนประกอบ PR (PPR)</h1>
<p class="sub">เลขที่ <b>{_e(s["ppr_number"])}</b> &nbsp; เวอร์ชัน <b>{version}</b></p>
{warn_html}
<div class="meta"><div>รหัสอ้างอิงเอกสาร: <b>{_e(document_code)}</b></div>
<div>ยืนยันเมื่อ: {_e(s.get("confirmed_at", "")[:19].replace("T", " "))} UTC</div></div>
<table class="info">
<tr><td>เลขที่ใบขอซื้อ (PR)</td><td>{_e(pr.get("pr_no"))}
  &nbsp; วันที่ {_e(pr.get("pr_date"))}</td></tr>
<tr><td>ปีงบประมาณ</td><td>{_e(s.get("fiscal_year"))}</td></tr>
<tr><td>หน่วยงานที่ขอ</td><td>{_e(names.get("department") or pr.get("department_id"))}</td></tr>
<tr><td>ผู้ขอ</td><td>{_e(pr.get("requester_name"))}</td></tr>
<tr><td>ประเภทงบ (แหล่งเงิน)</td>
  <td>{_e(names.get("fund_source") or pr.get("fund_source_id"))}</td></tr>
<tr><td>ยอดที่ขอใช้แผน</td><td><b>{_money(s.get("required_amount"))}</b> บาท
  (รวมมูลค่ารายการใน PR)</td></tr>
<tr><td>ผลการตรวจสอบกับแผน</td><td>ผ่านทุกรายการ (ตรวจจำนวนและจำนวนเงินกับยอดคงเหลือของแผน)</td></tr>
<tr><td>ยืนยันโดย</td><td>{_e(names.get("confirmed_by"))}</td></tr>
</table>
<b>รายการใน PR และรายการแผนที่ใช้</b>
<table><thead><tr><th>#</th><th>รายการ</th><th>จำนวน</th><th>หน่วย</th><th>ราคา/หน่วย</th>
<th>มูลค่า</th><th>รายการแผน</th><th>คงเหลือ (จำนวน)</th><th>คงเหลือ (บาท)</th></tr></thead>
<tbody>{item_rows}</tbody></table>
<b>การใช้เงินตามหมวดงบ</b>
<table><thead><tr><th>หมวดงบ (ชื่องบ)</th><th>จำนวนเงิน (บาท)</th></tr></thead>
<tbody>{alloc_rows}</tbody></table>
{multi}
<div class="sign">
<div><div class="line"></div>ผู้ขอใช้แผน<br>(............................................)</div>
<div><div class="line"></div>หัวหน้างาน / หัวหน้ากลุ่มงาน<br>(............................................)</div>
<div><div class="line"></div>หัวหน้าเจ้าหน้าที่พัสดุ<br>(............................................)</div>
</div>
<div class="foot">"คงเหลือ" คือยอดคงเหลือของรายการแผนหลังหักคำขอนี้ ณ เวลายืนยัน
เอกสารนี้สร้างจากข้อมูลที่ยืนยันแล้ว (เวอร์ชัน {version}) ตรวจสอบความถูกต้องได้ด้วยรหัสอ้างอิงเอกสาร</div>
</div></body></html>
"""
