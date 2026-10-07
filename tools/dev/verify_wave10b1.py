"""End-to-end check of Wave 10B-1 (D-39: stock issues / returns are HOSxP data for the report
only) on a development machine (demo mode).

* migrates the development database to 0010 and checks that the Plan Ledger now refuses
  ``CENTRAL_STOCK_ISSUE`` / ``CENTRAL_STOCK_RETURN`` itself (an insert that bypasses the
  application is refused by ``ck_plan_ledger_no_stock_movement``; tried inside a transaction
  that is always rolled back);
* starts the API (``serve --demo``, whose MOCK HOSxP holds made-up toner issues and returns
  and a stand-in "verified" query) and the user interface on spare ports, then, as a browser:
  - the Plan Officer and the store requester see the Central Pool toner with the issued,
    returned and net-used figures and one row per receiving department, with the
    verification note; the report downloads as Excel;
  - the ENT requester sees only Mock ENT's own consumption row, no plan figures, no other
    department, no purchaser; the OR requester only Mock OR's;
* every plan and PPR table is identical before and after (reports read, nothing is written).

Needs the Central Pool toner of Wave 7B-1 in the active plan year (``verify_wave7b1.py``).
WRITES nothing but the migration and one REPORT_EXPORTED audit row. Refuses to run unless
PPR_DATABASE_URL names a development database (``*_dev``) and the HOSxP mode is mock.

    .venv\\Scripts\\python tools\\dev\\verify_wave10b1.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from verify_wave7b1 import (  # noqa: E402
    API_PORT,
    UI,
    UI_PORT,
    Browser,
    central_item,
    check,
    results,
    scalar,
    wait,
)

from ppr.config.settings import Settings, database_name, redact_url  # noqa: E402

PLAN_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() "
    "AND (table_name LIKE 'plan%' OR table_name LIKE 'ppr%') "
    "AND table_name <> 'ppr_sync_observation' ORDER BY table_name"
)


def plan_data(engine) -> dict[str, list[tuple[object, ...]]]:  # type: ignore[no-untyped-def]
    with engine.connect() as c:
        tables = [r[0] for r in c.execute(text(PLAN_TABLES_SQL))]
        return {
            t: [tuple(r) for r in c.execute(text(f"SELECT * FROM {t} ORDER BY 1"))] for t in tables
        }


def body(page: bytes) -> bytes:
    """The report table only (the page header names the signed-in user's department)."""
    return page.split(b"<tbody>")[-1].split(b"</tbody>")[0] if b"<tbody>" in page else b""


def main() -> int:
    settings = Settings.load()
    url = settings.require_database_url()
    if not database_name(url).lower().endswith("_dev") or settings.hosxp_mode != "mock":
        print(
            f"REFUSED: needs a *_dev database and PPR_HOSXP_MODE=mock "
            f"(got {redact_url(url)}, mode {settings.hosxp_mode})"
        )
        return 2
    py = sys.executable
    src = str(REPO / "backend" / "src")
    pypath = os.pathsep.join(x for x in (src, os.environ.get("PYTHONPATH", "")) if x)
    base_env = {**os.environ, "PYTHONPATH": pypath}
    subprocess.run([py, "-m", "ppr.cli", "migrate"], cwd=REPO / "backend", env=base_env, check=True)
    engine = create_engine(url)
    head = scalar(engine, "SELECT version_num FROM alembic_version")
    check(head == "0010", f"database schema at the latest migration ({head})")
    named = scalar(
        engine,
        "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_plan_ledger_no_stock_movement'",
    )
    check(named == 1, "the Plan Ledger has the constraint ck_plan_ledger_no_stock_movement")
    stock_rows = scalar(
        engine,
        "SELECT count(*) FROM plan_ledger WHERE event_type IN "
        "('CENTRAL_STOCK_ISSUE', 'CENTRAL_STOCK_RETURN')",
    )
    check(stock_rows == 0, "no stock issue / return in the Plan Ledger")

    fy = int(
        scalar(
            engine,
            "SELECT fiscal_year FROM plan_year WHERE state = 'ACTIVE' "
            "ORDER BY fiscal_year DESC LIMIT 1",
        )
    )
    item = central_item(engine, fy)
    check(item is not None, f"the Central Pool toner of Wave 7B-1 exists (plan item {item})")
    for event, qty in (("CENTRAL_STOCK_ISSUE", -1), ("CENTRAL_STOCK_RETURN", 1)):
        refused = False
        with engine.connect() as c:
            tx = c.begin()
            try:
                c.execute(
                    text(
                        "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, "
                        "amount_delta, idempotency_key) VALUES (:i, :e, :q, 0, :k)"
                    ),
                    {"i": item, "e": event, "q": qty, "k": f"verify-10b1-{event}"},
                )
            except IntegrityError as exc:
                refused = "no_stock_movement" in str(exc) or "known_event" in str(exc)
            finally:
                tx.rollback()  # never keep anything, refused or not
        check(refused, f"the database refuses {event} (D-01 shape, amount 0)")

    before = plan_data(engine)
    env = {**base_env, "PPR_API_URL": f"http://127.0.0.1:{API_PORT}", "PPR_UI_PORT": str(UI_PORT)}
    api = subprocess.Popen(
        [py, "-m", "ppr.cli", "serve", "--demo", "--port", str(API_PORT)],
        cwd=REPO / "backend",
        env=env,
    )
    ui = subprocess.Popen(["node", str(REPO / "frontend" / "src" / "server.js")], env=env)
    try:
        if not (wait(f"http://127.0.0.1:{API_PORT}/api/health") and wait(UI + "/login")):
            check(False, "servers started")
            return 1
        path = f"/reports/CENTRAL_PLAN_USAGE?fiscal_year={fy}&go=1"
        for user in ("demo_plan", "demo_req_store"):
            st, page, _ = Browser(user).get(path)
            rows = body(page)
            check(
                st == 200
                and b"demo-banner" in page
                and "เบิกจากคลัง (HOSxP)".encode() in page
                and "ใช้ไปสุทธิ".encode() in page,
                f"{user}: report has the issued / returned / net-used columns",
            )
            check(
                b"Mock toner" in rows
                and "หน่วยงานเบิก/คืน".encode() in rows
                and b"Mock ENT" in rows
                and b"Mock OR" in rows,
                f"{user}: the toner with one row per receiving department (Mock ENT, Mock OR)",
            )
            check(
                "MOCK (ข้อมูลจำลอง)".encode() in page and "ไม่ลดหรือเพิ่มแผน".encode() in page,
                f"{user}: the note names the verification and says the plan is not changed",
            )
            check(
                b"Mock gloves" not in rows,
                f"{user}: glove movements (an ASSIGNED item, not Central Pool) are not shown",
            )
        st, page, _ = Browser("demo_req_ent").get(path)
        rows = body(page)
        check(
            st == 200 and b"Mock toner" in rows and b"Mock ENT" in rows,
            "ENT requester: sees its own toner consumption",
        )
        check(
            b"Mock OR" not in rows and b"Mock Store" not in rows and b"Mock fund" not in rows,
            "ENT requester: no other department, no purchaser, no fund source",
        )
        st, page, _ = Browser("demo_req_or").get(path)
        rows = body(page)
        check(
            st == 200 and b"Mock OR" in rows and b"Mock ENT" not in rows,
            "OR requester: only its own consumption",
        )
        st, data, _ = Browser("demo_plan").get(f"/reports/CENTRAL_PLAN_USAGE/xlsx?fiscal_year={fy}")
        check(st == 200 and data[:2] == b"PK", "the report downloads as Excel")
    finally:
        for p in (ui, api):
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
    after = plan_data(engine)
    changed = sorted(t for t in before if before[t] != after.get(t))
    check(not changed, f"every plan and PPR table is unchanged ({changed or 'none changed'})")
    engine.dispose()

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
