"""End-to-end check of Wave 7A (Plan Amendment) on a development machine (demo mode, mock HOSxP).

Starts the API (``serve --demo``) and the user interface on spare ports, then, through the user
interface as a browser would, as the demo Plan Officer:

* opens the plan year, the amendment form and the history (demo banner on each);
* checks an amendment that does not balance: the check says it cannot be recorded, and recording
  it anyway is refused with nothing written;
* moves 100 baht between two Plan Items of the same budget line: checks it, records it, and then
  verifies the history (before/after), the ledger (two ``PLAN_AMENDMENT`` entries linked to the
  amendment), the new current values, the balanced budget line and the ``PLAN_AMENDED`` audit row;
* checks the requester gets no amendment form and a refusal from the API, with nothing written;
* opens the *Plan Amendment* report and downloads it as Excel (one ``REPORT_EXPORTED`` row);
* checks the history cannot be edited (database trigger) and the schema is at 0008.

It WRITES one real amendment into the development database every time it runs (history is
append-only by design). Refuses to run unless PPR_DATABASE_URL names a development database
(``*_dev``) and the HOSxP mode is mock.

    .venv\\Scripts\\python tools\\dev\\verify_wave7a.py
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
from sqlalchemy.exc import DBAPIError  # noqa: E402

from ppr.config.settings import Settings, database_name, redact_url  # noqa: E402

API_PORT, UI_PORT = 8766, 3766
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


def counts(engine) -> tuple[int, int, int]:  # type: ignore[no-untyped-def]
    return (
        int(scalar(engine, "SELECT count(*) FROM plan_amendment")),
        int(scalar(engine, "SELECT count(*) FROM plan_ledger WHERE event_type = 'PLAN_AMENDMENT'")),
        int(scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'PLAN_AMENDED'")),
    )


def office_items(engine, fy: int) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    """Plan Items of the demo OFFICE line (fund 1), with what confirmed PPRs still hold."""
    sql = """
        SELECT i.id, i.planned_qty, i.estimated_unit_price, i.planned_amount, i.plan_budget_id,
               COALESCE(SUM(CASE WHEN l.event_type IN ('PPR_CONFIRMED', 'PPR_RELEASED')
                                 THEN -l.amount_delta END), 0) AS used_amount
        FROM plan_item i
        JOIN plan_year y ON y.id = i.plan_year_id
        JOIN plan_budget b ON b.id = i.plan_budget_id
        LEFT JOIN plan_ledger l ON l.plan_item_id = i.id
        WHERE y.fiscal_year = :fy AND b.fund_source_source_id = 'MOCK-FUND-1'
          AND b.budget_category_source_id = 'MOCK-CAT-OFFICE' AND i.plan_type = 'DEPARTMENT'
        GROUP BY i.id ORDER BY i.id
    """
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(text(sql), {"fy": fy})]


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
    check(head == "0008", f"database schema at the latest migration ({head})")

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
        requester = Browser("demo_req_ent")

        st, page, _ = officer.get(f"/plans/{fy}")
        check(
            st == 200 and f"/plans/{fy}/amendments/new".encode() in page and b"demo-banner" in page,
            f"plan year {fy}: amendment link and demo banner",
        )
        st, form_page, _ = officer.get(f"/plans/{fy}/amendments/new")
        check(
            st == 200
            and b"demo-banner" in form_page
            and b'name="approval_document_no"' in form_page,
            "amendment form opens with demo banner",
        )
        csrf = officer.csrf(form_page)

        items = office_items(engine, fy)
        # Move 100 baht from the item with the most unused amount to another item of the line.
        items.sort(
            key=lambda r: Decimal(r["planned_amount"]) - Decimal(r["used_amount"]), reverse=True
        )  # type: ignore[arg-type]
        giver, taker = items[0], items[1]
        g_new = Decimal(giver["planned_amount"]) - 100  # type: ignore[arg-type]
        t_new = Decimal(taker["planned_amount"]) + 100  # type: ignore[arg-type]
        header = {
            "_csrf": csrf,
            "approval_document_no": f"E2E-7A-{int(time.time())}",
            # the API judges the date by this same machine's clock
            "approval_date": date.today().isoformat(),  # noqa: DTZ011
            "reason": "ทดสอบ end-to-end Wave 7A (โหมดทดลอง)",
        }

        def fields(g_amount: Decimal, t_amount: Decimal) -> dict[str, str]:
            out = dict(header)
            for row, amount in ((giver, g_amount), (taker, t_amount)):
                out[f"q_{row['id']}"] = str(row["planned_qty"])
                out[f"p_{row['id']}"] = str(row["estimated_unit_price"])
                out[f"a_{row['id']}"] = str(amount)
            return out

        # 1. an unbalanced amendment: checked, refused, nothing written
        before = counts(engine)
        bad = fields(Decimal(giver["planned_amount"]), t_new)  # type: ignore[arg-type]
        st, body, _ = officer.post(f"/plans/{fy}/amendments/preview", bad)
        check(
            st == 200 and "ยังบันทึกไม่ได้".encode() in body and b"ENVELOPE_MISMATCH" in body,
            "unbalanced amendment: the check says it cannot be recorded",
        )
        st, body, _ = officer.post(f"/plans/{fy}/amendments", bad)
        check(
            st == 409 and counts(engine) == before,
            f"recording it anyway is refused ({st}), nothing written",
        )

        # 2. the balanced transfer
        good = fields(g_new, t_new)
        st, body, _ = officer.post(f"/plans/{fy}/amendments/preview", good)
        check(
            st == 200 and "บันทึกได้".encode() in body,
            "balanced transfer: the check says it can be recorded",
        )
        check(counts(engine) == before, "checking writes nothing")
        st, _, headers = officer.post(f"/plans/{fy}/amendments", good)
        location = headers.get("Location", "")
        check(
            st == 303 and location.startswith("/plan-amendments/"), f"recorded ({st} -> {location})"
        )
        after = counts(engine)
        check(
            after == (before[0] + 1, before[1] + 2, before[2] + 1),
            "one amendment, two PLAN_AMENDMENT ledger entries, one PLAN_AMENDED audit row "
            f"({before} -> {after})",
        )
        aid = int(location.rsplit("/", 1)[-1]) if location else 0
        linked = int(
            scalar(engine, "SELECT count(*) FROM plan_ledger WHERE plan_amendment_id = :a", a=aid)
        )
        check(linked == 2, f"the ledger entries are linked to amendment {aid}")
        now = {r["id"]: Decimal(r["planned_amount"]) for r in office_items(engine, fy)}  # type: ignore[arg-type]
        check(
            now[giver["id"]] == g_new and now[taker["id"]] == t_new,
            f"current values changed ({giver['id']}: {g_new}, {taker['id']}: {t_new})",
        )
        line = scalar(
            engine,
            "SELECT approved_amount FROM plan_budget WHERE id = :b",
            b=giver["plan_budget_id"],
        )
        total = scalar(
            engine,
            "SELECT sum(planned_amount) FROM plan_item WHERE plan_budget_id = :b",
            b=giver["plan_budget_id"],
        )
        check(line == total, f"the budget line still balances ({line} = {total})")
        doc = header["approval_document_no"].encode()
        st, detail, _ = officer.get(location)
        check(
            st == 200 and doc in detail and b"demo-banner" in detail,
            "amendment page shows the approval document",
        )
        st, hist, _ = officer.get(f"/plans/{fy}/amendments")
        check(st == 200 and doc in hist and b"demo-banner" in hist, "history lists the amendment")

        # 3. the requester
        st, page, _ = requester.get(f"/plans/{fy}")
        check(st == 200 and b"/amendments/new" not in page, "requester: no amendment button")
        st, rform, _ = requester.get(f"/plans/{fy}/amendments/new")
        rgood = {**good, "_csrf": requester.csrf(rform)}
        mark = counts(engine)
        st, _, _ = requester.post(f"/plans/{fy}/amendments", rgood)
        check(
            st == 403 and counts(engine) == mark,
            f"requester: recording refused by the API ({st}), nothing written",
        )

        # 4. report and Excel
        exports = int(
            scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'REPORT_EXPORTED'")
        )
        st, rep, _ = officer.get(f"/reports/PLAN_AMENDMENT?fiscal_year={fy}&go=1")
        check(
            st == 200 and doc in rep and b"demo-banner" in rep,
            "Plan Amendment report shows the amendment",
        )
        st, data, hdrs = officer.get(f"/reports/PLAN_AMENDMENT/xlsx?fiscal_year={fy}")
        check(
            st == 200 and data[:2] == b"PK" and "spreadsheetml" in hdrs.get("Content-Type", ""),
            "Plan Amendment report downloads as Excel",
        )
        now_exports = int(
            scalar(engine, "SELECT count(*) FROM audit_log WHERE action = 'REPORT_EXPORTED'")
        )
        check(now_exports == exports + 1, "the download wrote one REPORT_EXPORTED row")

        # 5. history cannot be edited
        try:
            with engine.begin() as c:
                c.execute(text("UPDATE plan_amendment SET reason = 'x' WHERE id = :a"), {"a": aid})
            check(False, "history cannot be edited")
        except DBAPIError as exc:
            check("append-only" in str(exc), "history cannot be edited (database trigger)")
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
