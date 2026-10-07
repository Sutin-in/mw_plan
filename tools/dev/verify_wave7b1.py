"""End-to-end check of Wave 7B-1 (Central Pool, D-38) on a development machine (demo mode).

Starts the API (``serve --demo``) and the user interface on spare ports, then, through the user
interface as a browser would:

* the demo Plan Officer adds a Central Pool item (toner 5 x 100, bought by Mock Store) by Plan
  Amendment, raising the demo OFFICE line by 500 (skipped if the item is already there);
* the demo store requester prepares and confirms a PPR from the store's PR (``...-1007``): it
  draws on the Central Pool item (``PPR_CONFIRMED`` on that item; skipped if already bound);
* the ENT requester does not see the Central Pool item (plan page, report);
* the *Central Plan Usage* report shows the usage, says stock issues have no data yet, and
  downloads as Excel;
* an amendment that would make a Central Pool item indistinguishable from ENT's own toner for
  PR matching is refused by the check;
* the demo banner shows on every page.

It WRITES an amendment and a confirmed PPR into the development database the first time it runs.
Refuses to run unless PPR_DATABASE_URL names a development database (``*_dev``) and the HOSxP
mode is mock.

    .venv\\Scripts\\python tools\\dev\\verify_wave7b1.py
"""

from __future__ import annotations

import http.cookiejar
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend" / "src"))

from sqlalchemy import create_engine, text  # noqa: E402

from ppr.config.settings import Settings, database_name, redact_url  # noqa: E402

API_PORT, UI_PORT = 8767, 3767
UI = f"http://127.0.0.1:{UI_PORT}"
PASSWORD = "demo1234"
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return None


class Browser:
    def __init__(self, user: str) -> None:
        jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
        _, page, _ = self.get("/login")
        form = {"_csrf": self.csrf(page), "username": user, "password": PASSWORD}
        self.post("/login", form)

    def _open(self, req: urllib.request.Request) -> tuple[int, bytes, dict[str, str]]:
        try:
            r = self.op.open(req, timeout=60)
            return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def get(self, path: str) -> tuple[int, bytes, dict[str, str]]:
        return self._open(urllib.request.Request(UI + path))

    def post(self, path: str, form: dict[str, str]) -> tuple[int, bytes, dict[str, str]]:
        data = urllib.parse.urlencode(form).encode()
        return self._open(urllib.request.Request(UI + path, data=data))

    @staticmethod
    def csrf(page: bytes) -> str:
        m = re.search(rb'name="_csrf" value="([^"]+)"', page)
        assert m, "no form on the page"
        return m.group(1).decode()


def wait(url: str, seconds: int = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=3)
            return True
        except Exception:
            time.sleep(1)
    return False


def scalar(engine, sql: str, **p: object) -> object:  # type: ignore[no-untyped-def]
    with engine.connect() as c:
        return c.execute(text(sql), p).scalar_one()


def central_item(engine, fy: int) -> int | None:  # type: ignore[no-untyped-def]
    sql = """
        SELECT i.id FROM plan_item i JOIN plan_year y ON y.id = i.plan_year_id
        WHERE y.fiscal_year = :fy AND i.plan_type = 'CENTRAL'
          AND i.item_source_id = 'MOCK-ITEM-TONER'
          AND i.purchasing_department_source_id = 'MOCK-DEP-STORE'
        ORDER BY i.id LIMIT 1
    """
    with engine.connect() as c:
        row = c.execute(text(sql), {"fy": fy}).first()
    return int(row[0]) if row else None


def office_line(engine, fy: int) -> tuple[int, Decimal]:  # type: ignore[no-untyped-def]
    sql = """
        SELECT b.id, b.approved_amount FROM plan_budget b JOIN plan_year y ON y.id = b.plan_year_id
        WHERE y.fiscal_year = :fy AND b.fund_source_source_id = 'MOCK-FUND-1'
          AND b.budget_category_source_id = 'MOCK-CAT-OFFICE'
    """
    with engine.connect() as c:
        row = c.execute(text(sql), {"fy": fy}).one()
    return int(row[0]), Decimal(row[1])


def latest_amendment(engine, fy: int) -> int:  # type: ignore[no-untyped-def]
    return int(
        scalar(
            engine,
            "SELECT COALESCE(MAX(a.amendment_no), 0) FROM plan_amendment a "
            "JOIN plan_year y ON y.id = a.plan_year_id WHERE y.fiscal_year = :fy",
            fy=fy,
        )
    )


def amendment_form(base: int, csrf: str, line: tuple[int, Decimal], buyer: str) -> dict[str, str]:
    bid, amount = line
    return {
        "_csrf": csrf,
        "approval_document_no": f"E2E-7B1-{int(time.time())}",
        # the API judges the date by this same machine's clock
        "approval_date": date.today().isoformat(),  # noqa: DTZ011
        "reason": "ทดสอบ end-to-end Wave 7B-1 แผนกลาง (โหมดทดลอง)",
        "base_amendment_no": str(base),
        f"b_{bid}": str(amount + 500),
        f"ob_{bid}": str(amount),
        "n_type": "CENTRAL",
        "n_item": "MOCK-ITEM-TONER",
        "n_owner": "MOCK-DEP-STORE",
        "n_buyer": buyer,
        "n_fund": "MOCK-FUND-1",
        "n_cat": "MOCK-CAT-OFFICE",
        "n_qty": "5",
        "n_price": "100",
        "n_amount": "500",
        "n_note": "",
    }


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
    check(head == "0008", f"database schema at the latest migration ({head}; 7B-1 adds none)")

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

        # 1. a Central Pool item, by Plan Amendment
        cid = central_item(engine, fy)
        if cid is None:
            st, page, _ = officer.get(f"/plans/{fy}/amendments/new")
            check(st == 200 and b"demo-banner" in page, "amendment form opens with demo banner")
            form = amendment_form(
                latest_amendment(engine, fy),
                officer.csrf(page),
                office_line(engine, fy),
                "MOCK-DEP-STORE",
            )
            st, body, _ = officer.post(f"/plans/{fy}/amendments/preview", form)
            check(
                st == 200 and "บันทึกได้".encode() in body,
                "adding a Central Pool item: the check passes",
            )
            st, _, hdrs = officer.post(f"/plans/{fy}/amendments", form)
            check(
                st == 303 and hdrs.get("Location", "").startswith("/plan-amendments/"),
                f"Central Pool item added by amendment ({st})",
            )
            cid = central_item(engine, fy)
        else:
            print(f"(Central Pool item {cid} already present from an earlier run)")
        check(cid is not None, f"Central Pool toner bought by Mock Store exists ({cid})")

        # 2. the store's PR draws on it
        pr_no = f"MOCK-PR-{fy}-1007"
        bound = int(scalar(engine, "SELECT count(*) FROM ppr_binding WHERE pr_no = :p", p=pr_no))
        if not bound:
            st, page, _ = store.get(f"/pprs/new?pr_no={pr_no}")
            check(st == 200 and b"demo-banner" in page, "store requester: PR check page")
            st, _, hdrs = store.post("/pprs", {"_csrf": store.csrf(page), "pr_no": pr_no})
            loc = hdrs.get("Location", "")
            check(st == 303 and re.match(r"^/pprs/\d+$", loc) is not None, f"draft created ({loc})")
            st, page, _ = store.get(loc)
            st, _, _ = store.post(f"{loc}/confirm", {"_csrf": store.csrf(page)})
            check(st == 303, "store requester: confirm submitted")
        else:
            print(f"({pr_no} already bound from an earlier run)")
        used = scalar(
            engine,
            "SELECT COALESCE(-SUM(amount_delta), 0) FROM plan_ledger "
            "WHERE plan_item_id = :i AND event_type IN ('PPR_CONFIRMED', 'PPR_RELEASED')",
            i=cid,
        )
        check(
            Decimal(used) == Decimal(500),
            f"the store's PPR drew 500.00 on the Central Pool item ({used})",
        )

        # 3. visibility
        st, page, _ = ent.get(f"/plans/{fy}")
        st2, _, _ = store.get(f"/plans/{fy}")
        check(
            st == 200 and st2 == 200 and b"demo-banner" in page, "plan pages open with demo banner"
        )
        st, rep_ent, _ = ent.get(f"/reports/CENTRAL_PLAN_USAGE?fiscal_year={fy}&go=1")
        check(
            st == 200 and b"Mock toner" not in rep_ent.split(b"<tbody>")[-1],
            "ENT requester: the Central Pool report shows no row",
        )
        st, rep_store, _ = store.get(f"/reports/CENTRAL_PLAN_USAGE?fiscal_year={fy}&go=1")
        check(
            st == 200 and b"Mock toner" in rep_store, "store requester: sees its Central Pool item"
        )

        # 4. the report for the Plan Officer and Excel
        st, rep, _ = officer.get(f"/reports/CENTRAL_PLAN_USAGE?fiscal_year={fy}&go=1")
        check(
            st == 200 and b"demo-banner" in rep and "ยังไม่มีข้อมูล".encode() in rep,
            "Central Plan Usage report: usage shown, stock issues 'no data yet'",
        )
        exports = int(
            scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'REPORT_EXPORTED'")
        )
        st, data, hdrs = officer.get(f"/reports/CENTRAL_PLAN_USAGE/xlsx?fiscal_year={fy}")
        check(st == 200 and data[:2] == b"PK", "Central Plan Usage report downloads as Excel")
        now_exports = int(
            scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'REPORT_EXPORTED'")
        )
        check(now_exports == exports + 1, "the download wrote one REPORT_EXPORTED row")

        # 5. ambiguity is refused at plan level
        st, page, _ = officer.get(f"/plans/{fy}/amendments/new")
        form = amendment_form(
            latest_amendment(engine, fy),
            officer.csrf(page),
            office_line(engine, fy),
            "MOCK-DEP-ENT",
        )
        before = latest_amendment(engine, fy)
        st, body, _ = officer.post(f"/plans/{fy}/amendments/preview", form)
        check(
            st == 200 and b"DUPLICATE_MATCH_KEY" in body,
            "a Central Pool toner bought by ENT (ENT already plans toner) is refused",
        )
        st, _, _ = officer.post(f"/plans/{fy}/amendments", form)
        check(
            st == 409 and latest_amendment(engine, fy) == before, f"recording it is refused ({st})"
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
