"""End-to-end check of Wave 7B-2 (Assigned Purchase, D-38 A-1 ... A-6) on a development machine
(demo mode).

Starts the API (``serve --demo``) and the user interface on spare ports, then, through the user
interface as a browser would:

* the demo Plan Officer adds an Assigned Purchase item (gloves 30 x 30, bought by Mock Store for
  Mock ENT 20 and Mock OR 10) by Plan Amendment, raising the demo MED line by 900 (skipped if the
  item is already there); the plan page shows each department's demand;
* while ENT's demand is open, ENT's own glove PR (``...-1002``) is refused (A-1);
* the demo store requester prepares a PPR from the store's glove PR (``...-1008``): the draft
  proposes 20 for ENT and 10 for OR (A-2); confirming makes both demands "included";
* the appointed demo head verifies it: both demands become "fulfilled" (A-3);
* an amendment lowering ENT's demand below what the PPR covers is refused (A-6);
* every demand state change is audited, and the amendment report lists the demand changes;
* the demo banner shows on every page.

It WRITES an amendment, a confirmed and verified PPR into the development database the first
time it runs. Refuses to run unless PPR_DATABASE_URL names a development database (``*_dev``)
and the HOSxP mode is mock.

    .venv\\Scripts\\python tools\\dev\\verify_wave7b2.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, text  # noqa: E402
from verify_wave7b1 import (  # noqa: E402
    API_PORT,
    UI,
    UI_PORT,
    Browser,
    check,
    latest_amendment,
    results,
    scalar,
    wait,
)

from ppr.config.settings import Settings, database_name, redact_url  # noqa: E402

GLOVE, STORE, ENT, OR = "MOCK-ITEM-GLOVE", "MOCK-DEP-STORE", "MOCK-DEP-ENT", "MOCK-DEP-OR"
BLOCKED = "หน่วยงานนี้มีความต้องการรายการนี้อยู่ในแผนซื้อแทน"


def assigned_item(engine, fy: int) -> int | None:  # type: ignore[no-untyped-def]
    sql = """
        SELECT i.id FROM plan_item i JOIN plan_year y ON y.id = i.plan_year_id
        WHERE y.fiscal_year = :fy AND i.plan_type = 'ASSIGNED'
          AND i.item_source_id = :g AND i.purchasing_department_source_id = :s
        ORDER BY i.id LIMIT 1
    """
    with engine.connect() as c:
        row = c.execute(text(sql), {"fy": fy, "g": GLOVE, "s": STORE}).first()
    return int(row[0]) if row else None


def med_line(engine, fy: int) -> tuple[int, Decimal]:  # type: ignore[no-untyped-def]
    sql = """
        SELECT b.id, b.approved_amount FROM plan_budget b JOIN plan_year y ON y.id = b.plan_year_id
        WHERE y.fiscal_year = :fy AND b.fund_source_source_id = 'MOCK-FUND-1'
          AND b.budget_category_source_id = 'MOCK-CAT-MED'
    """
    with engine.connect() as c:
        row = c.execute(text(sql), {"fy": fy}).one()
    return int(row[0]), Decimal(row[1])


def states(engine, item: int) -> dict[str, str]:  # type: ignore[no-untyped-def]
    with engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT department_source_id, state FROM plan_item_demand WHERE plan_item_id = :i"
            ),
            {"i": item},
        )
        return {r[0]: r[1] for r in rows}


def header(csrf: str, base: int, tag: str) -> dict[str, str]:
    return {
        "_csrf": csrf,
        "approval_document_no": f"E2E-7B2-{tag}-{int(time.time())}",
        # the API judges the date by this same machine's clock
        "approval_date": date.today().isoformat(),  # noqa: DTZ011
        "reason": "ทดสอบ end-to-end Wave 7B-2 แผนซื้อแทน (โหมดทดลอง)",
        "base_amendment_no": str(base),
    }


def add_item_form(csrf: str, base: int, line: tuple[int, Decimal]) -> list[tuple[str, str]]:
    bid, amount = line
    fields = {
        **header(csrf, base, "ADD"),
        f"b_{bid}": str(amount + 900),
        f"ob_{bid}": str(amount),
        "n_type": "ASSIGNED",
        "n_item": GLOVE,
        "n_owner": STORE,
        "n_buyer": STORE,
        "n_fund": "MOCK-FUND-1",
        "n_cat": "MOCK-CAT-MED",
        "n_qty": "30",
        "n_price": "30",
        "n_amount": "900",
        "n_note": "",
    }
    return [*fields.items(), ("n_dd_0", ENT), ("n_dq_0", "20"), ("n_dd_0", OR), ("n_dq_0", "10")]


def post_pairs(b: Browser, path: str, pairs: list[tuple[str, str]]):  # type: ignore[no-untyped-def]
    """A form with repeated fields (demand rows), as a browser sends it."""
    data = urllib.parse.urlencode(pairs).encode()
    return b._open(urllib.request.Request(UI + path, data=data))


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
    # The API and the migration run from THIS checkout, whatever copy is installed.
    src = str(REPO / "backend" / "src")
    pypath = os.pathsep.join(x for x in (src, os.environ.get("PYTHONPATH", "")) if x)
    base_env = {**os.environ, "PYTHONPATH": pypath}
    subprocess.run([py, "-m", "ppr.cli", "migrate"], cwd=REPO / "backend", env=base_env, check=True)
    engine = create_engine(url)
    head = scalar(engine, "SELECT version_num FROM alembic_version")
    check(head == "0009", f"database schema at the latest migration ({head})")

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
        fy = int(
            scalar(
                engine,
                "SELECT fiscal_year FROM plan_year WHERE state = 'ACTIVE' "
                "ORDER BY fiscal_year DESC LIMIT 1",
            )
        )
        officer = Browser("demo_plan")
        store = Browser("demo_req_store")
        ent = Browser("demo_req_ent")
        head_user = Browser("demo_head")

        # 1. an Assigned Purchase item with department demand, by Plan Amendment
        item = assigned_item(engine, fy)
        if item is None:
            st, page, _ = officer.get(f"/plans/{fy}/amendments/new")
            check(st == 200 and b"demo-banner" in page, "amendment form opens with demo banner")
            form = add_item_form(
                officer.csrf(page), latest_amendment(engine, fy), med_line(engine, fy)
            )
            st, body, _ = post_pairs(officer, f"/plans/{fy}/amendments/preview", form)
            check(
                st == 200 and "บันทึกได้".encode() in body,
                "adding an Assigned Purchase item with demand: the check passes",
            )
            st, _, hdrs = post_pairs(officer, f"/plans/{fy}/amendments", form)
            check(
                st == 303 and hdrs.get("Location", "").startswith("/plan-amendments/"),
                f"Assigned Purchase item added by amendment ({st})",
            )
            item = assigned_item(engine, fy)
        else:
            print(f"(Assigned Purchase item {item} already present from an earlier run)")
        check(item is not None, f"Assigned Purchase gloves bought by Mock Store exist ({item})")
        assert item is not None
        st, page, _ = officer.get(f"/plans/{fy}")
        check(
            st == 200 and "ความต้องการของหน่วยงานในแผนซื้อแทน".encode() in page,
            "plan page lists each department's demand",
        )

        # 2. A-1: ENT may not buy the gloves itself while its demand is open
        pr_ent = f"MOCK-PR-{fy}-1002"
        ent_bound = int(
            scalar(engine, "SELECT count(*) FROM ppr_binding WHERE pr_no = :p", p=pr_ent)
        )
        if ent_bound:
            print(f"({pr_ent} is already bound to a PPR: A-1 is shown by the API tests only)")
        elif states(engine, item).get(ENT) in ("PLANNED", "INCLUDED_IN_CENTRAL_PURCHASE"):
            st, page, _ = ent.get(f"/pprs/new?pr_no={pr_ent}")
            check(
                st == 200 and BLOCKED.encode() in page,
                "ENT's own glove PR is refused while its demand is open (A-1)",
            )
        else:
            print(f"(ENT's demand is {states(engine, item).get(ENT)}: A-1 no longer applies)")

        # 3. A-2: the store's PPR, with the proposed coverage, then confirmed
        pr_no = f"MOCK-PR-{fy}-1008"
        bound = int(scalar(engine, "SELECT count(*) FROM ppr_binding WHERE pr_no = :p", p=pr_no))
        if not bound:
            st, page, _ = store.get(f"/pprs/new?pr_no={pr_no}")
            check(st == 200 and b"demo-banner" in page, "store requester: PR check page")
            st, _, hdrs = store.post("/pprs", {"_csrf": store.csrf(page), "pr_no": pr_no})
            loc = hdrs.get("Location", "")
            check(st == 303 and re.match(r"^/pprs/\d+$", loc) is not None, f"draft created ({loc})")
            st, page, _ = store.get(loc)
            check(
                st == 200
                and "ซื้อแทนหน่วยงาน".encode() in page
                and b'name="cov_qty" inputmode="decimal" class="num" value="20.0000"' in page,
                "the draft proposes 20 for ENT and 10 for OR",
            )
            st, _, _ = store.post(f"{loc}/confirm", {"_csrf": store.csrf(page)})
            check(st == 303, "store requester: confirm submitted")
        else:
            print(f"({pr_no} already bound from an earlier run)")
        ppr_id = int(scalar(engine, "SELECT ppr_id FROM ppr_binding WHERE pr_no = :p", p=pr_no))
        covered = scalar(
            engine,
            "SELECT COALESCE(SUM(qty), 0) FROM ppr_coverage WHERE ppr_id = :i AND confirmed",
            i=ppr_id,
        )
        check(Decimal(covered) == 30, f"the confirmed PPR covers 30 gloves ({covered})")

        # 4. A-3: verification fulfils both demands
        st_now = scalar(engine, "SELECT state FROM ppr WHERE id = :i", i=ppr_id)
        if st_now == "CONFIRMED_LOCKED":
            check(
                set(states(engine, item).values()) == {"INCLUDED_IN_CENTRAL_PURCHASE"},
                "after confirmation both demands are 'included'",
            )
            st, page, _ = head_user.get(f"/pprs/{ppr_id}")
            version = re.search(rb'name="version" value="(\d+)"', page)
            check(st == 200 and version is not None, "the head sees the verify button")
            if version:
                st, _, _ = head_user.post(
                    f"/pprs/{ppr_id}/verify",
                    {"_csrf": head_user.csrf(page), "version": version.group(1).decode()},
                )
                check(st == 303, "verification submitted")
        check(
            states(engine, item) == {ENT: "FULFILLED", OR: "FULFILLED"},
            f"after verification both demands are 'fulfilled' ({states(engine, item)})",
        )

        # 5. A-6: never below what PPRs in effect cover
        st, page, _ = officer.get(f"/plans/{fy}/amendments/new")
        dd = re.findall(rf'name="dd_{item}" value="([^"]+)"'.encode(), page)
        check(st == 200 and len(dd) == 2, "amendment form lists the item's demand rows")
        before = latest_amendment(engine, fy)
        pairs = [*header(officer.csrf(page), before, "LOW").items()]
        for d in dd:
            dept = d.decode()
            pairs += [
                (f"dd_{item}", dept),
                (f"dq_{item}", "19" if dept == ENT else "10"),
                (f"odq_{item}", "20" if dept == ENT else "10"),
            ]
        st, body, _ = post_pairs(officer, f"/plans/{fy}/amendments/preview", pairs)
        check(
            st == 200 and b"BELOW_COVERED_DEMAND" in body,
            "lowering ENT's demand below what the PPR covers is refused by the check",
        )
        st, _, _ = post_pairs(officer, f"/plans/{fy}/amendments", pairs)
        check(
            st == 409 and latest_amendment(engine, fy) == before, f"recording it is refused ({st})"
        )

        # 6. evidence
        audited = int(
            scalar(
                engine,
                "SELECT count(*) FROM audit_log WHERE action = 'DEMAND_STATE_CHANGED' "
                "AND entity_id IN (SELECT id::text FROM plan_item_demand WHERE plan_item_id = :i)",
                i=item,
            )
        )
        check(audited >= 4, f"every demand state change is audited ({audited} rows)")
        st, rep, _ = officer.get(f"/reports/PLAN_AMENDMENT?fiscal_year={fy}&go=1")
        check(
            st == 200 and b"demo-banner" in rep and "เพิ่มความต้องการของหน่วยงาน".encode() in rep,
            "the amendment report lists the demand changes",
        )
    finally:
        for p in (ui, api):
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        engine.dispose()

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
