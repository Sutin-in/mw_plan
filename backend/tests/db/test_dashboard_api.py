"""Wave 8A through the API: dashboards, derived alerts, alert settings, plan balances.

Spec §22.1, §31.2, §31.7 - §31.9, §34, §36; D-32, D-33, D-34.
World (test_ppr_api): FY2570 ACTIVE, Mock ENT fund 1 - toner 10 / 1000, paper 10 / 500,
gloves 10 / 300; Mock OR fund 2 - paper 10 / 100. Server date 2026-10-15.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text
from test_ppr_api import GLOVE, PAPER, TONER, World

from api_harness import FY, Harness
from ppr.domain.roles import Role


class Dash:
    def __init__(self, w: World) -> None:
        self.w = w
        self.hx = w.hx
        self.officer = w.plan.h
        self.admin = self.hx.h(self.hx.user("admin", Role.ADMIN))
        self.exec = self.hx.h(self.hx.user("exec", Role.EXECUTIVE))
        self.or_req = self.hx.h(self.hx.user("req_or", Role.REQUESTER, department="MOCK-DEP-OR"))

    def get(self, path: str, headers: dict[str, str]) -> Any:
        r = self.hx.client.get(path, headers=headers)
        assert r.status_code == 200, r.text
        return r.json()

    def confirmed(self, pr_no: str, *lines: tuple[str, str, str]) -> dict[str, Any]:
        d = self.w.draft(pr_no, *lines)
        r = self.w.confirm(d["id"])
        assert r.status_code == 200, r.text
        return dict(r.json())

    def age(self, ppr_id: int, days: int) -> None:
        with self.hx.engine.begin() as c:
            c.execute(
                text("UPDATE ppr SET updated_at = now() - make_interval(days => :d) WHERE id = :i"),
                {"d": days, "i": ppr_id},
            )

    def alerts(self, headers: dict[str, str]) -> dict[str, Any]:
        return dict(self.get("/api/alerts", headers))

    def types(self, headers: dict[str, str]) -> list[tuple[str, Any]]:
        return [
            (a["type"], a["ppr_number"] or a["item_id"] or a["sync_scope"])
            for a in self.alerts(headers)["alerts"]
        ]

    def put_setting(
        self, code: str, value: int, headers: dict[str, str], reason: Any = "ok"
    ) -> Any:
        return self.hx.client.put(
            f"/api/alert-settings/{code}", json={"value": value, "reason": reason}, headers=headers
        )

    def counts(self) -> tuple[int, int]:
        with self.hx.engine.connect() as c:
            return (
                int(c.execute(text("SELECT count(*) FROM plan_ledger")).scalar_one()),
                int(c.execute(text("SELECT count(*) FROM audit_log")).scalar_one()),
            )


@pytest.fixture
def d(db: Engine) -> Dash:
    return Dash(World(Harness(db)))


# ------------------------------------------------------------------ dashboards (§31, D-32)
@pytest.mark.spec("D-32", "S-31.8", "S-31.9", "S-18.3")
def test_dashboard_totals_follow_the_ledger(d: Dash) -> None:
    d.confirmed("PR-D1", (TONER, "4", "100"), (PAPER, "2", "50"))
    dash = d.get(f"/api/dashboard?fiscal_year={FY}", d.exec)
    assert (dash["fiscal_year"], dash["plan_year_state"], dash["department_scope"]) == (
        FY,
        "ACTIVE",
        None,
    )
    t = dash["totals"]
    assert (t["planned_amount"], t["used_amount"], t["remaining_amount"], t["items"]) == (
        "1900.00",
        "500.00",
        "1400.00",
        4,
    )
    by_dept = {r["department_id"]: r["used_amount"] for r in dash["by_department"]}
    assert by_dept == {"MOCK-DEP-ENT": "500.00", "MOCK-DEP-OR": "0.00"}
    assert dash["ppr_counts"]["CONFIRMED_LOCKED"] == 1
    assert [p["pr_no"] for p in dash["recent"]] == ["PR-D1"]


@pytest.mark.spec("D-32", "S-22.1", "S-31.2")
def test_a_requester_sees_only_their_department(d: Dash) -> None:
    d.confirmed("PR-D2", (TONER, "4", "100"))
    ent = d.get("/api/dashboard", d.w.req)
    assert ent["department_scope"] == "MOCK-DEP-ENT"
    assert ent["totals"]["items"] == 3 and ent["totals"]["planned_amount"] == "1800.00"
    assert ent["ppr_counts"]["CONFIRMED_LOCKED"] == 1
    other = d.get("/api/dashboard", d.or_req)
    assert other["department_scope"] == "MOCK-DEP-OR"
    assert other["totals"]["items"] == 1
    assert other["ppr_counts"]["CONFIRMED_LOCKED"] == 0 and other["recent"] == []
    rows = d.get(f"/api/plan-years/{FY}/balances", d.or_req)
    assert [(r["item_id"], r["owner_department_id"]) for r in rows] == [(PAPER, "MOCK-DEP-OR")]


@pytest.mark.spec("D-32")
def test_dashboards_and_alerts_only_read(d: Dash) -> None:
    d.confirmed("PR-D3", (TONER, "9", "100"))
    before = d.counts()
    for h in (d.officer, d.exec, d.w.req, d.admin):
        d.get("/api/dashboard", h)
        d.get("/api/alerts", h)
        d.get(f"/api/plan-years/{FY}/balances", h)
    assert d.counts() == before  # no ledger posting, no audit rows


def test_balances_of_an_unknown_year_is_404(d: Dash) -> None:
    r = d.hx.client.get("/api/plan-years/2599/balances", headers=d.officer)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "PLAN_YEAR_NOT_FOUND"


@pytest.mark.spec("D-34", "S-18.3")
def test_balances_show_planned_used_and_remaining(d: Dash) -> None:
    d.confirmed("PR-D4", (TONER, "9", "100"))
    rows = {r["item_id"]: r for r in d.get(f"/api/plan-years/{FY}/balances", d.w.req)}
    toner = rows[TONER]
    assert (toner["planned_qty"], toner["used_qty"], toner["remaining_qty"]) == (
        "10.0000",
        "9.0000",
        "1.0000",
    )
    assert (toner["used_amount"], toner["remaining_amount"]) == ("900.00", "100.00")
    assert (toner["alert"], toner["remaining_amount_percent"]) == ("PLAN_LOW", "10.00")
    assert rows[GLOVE]["alert"] is None


# ------------------------------------------------------------------ alerts (§34, D-33)
@pytest.mark.spec("D-33", "S-34")
def test_plan_low_and_exhausted_alerts_appear_and_follow_the_threshold(d: Dash) -> None:
    d.confirmed("PR-L1", (TONER, "9", "100"))  # 10 % left
    d.confirmed("PR-L2", (GLOVE, "10", "30"))  # all used
    got = d.types(d.officer)
    assert ("PLAN_EXHAUSTED", GLOVE) in got and ("PLAN_LOW", TONER) in got
    assert got.index(("PLAN_EXHAUSTED", GLOVE)) < got.index(("PLAN_LOW", TONER))
    assert d.put_setting("PLAN_LOW_PERCENT", 5, d.admin).status_code == 200
    got = d.types(d.officer)
    assert ("PLAN_LOW", TONER) not in got and ("PLAN_EXHAUSTED", GLOVE) in got
    # The OR requester cannot see the ENT plan items.
    assert not [t for t in d.types(d.or_req) if t[0].startswith("PLAN_")]


@pytest.mark.spec("D-33", "S-34", "S-31.7")
def test_inactivity_alert_uses_the_stored_threshold_like_the_queue(d: Dash) -> None:
    ppr = d.confirmed("PR-I1", (TONER, "1", "100"))
    d.age(ppr["id"], 31)
    draft = d.w.draft("PR-I2", (PAPER, "1", "50"))
    d.age(draft["id"], 90)  # drafts never alert
    assert ("PPR_INACTIVE", ppr["ppr_number"]) in d.types(d.officer)
    assert not [t for t in d.types(d.officer) if t[1] is None]
    clerk = d.hx.h(d.hx.user("clerk", Role.PROCUREMENT))
    q = d.get("/api/procurement/queue?bucket=inactive", clerk)
    assert (q["inactive_days"], q["counts"]["inactive"]) == (30, 1)
    assert d.put_setting("PPR_INACTIVE_DAYS", 40, d.admin).status_code == 200
    assert ("PPR_INACTIVE", ppr["ppr_number"]) not in d.types(d.officer)
    q = d.get("/api/procurement/queue?bucket=inactive", clerk)
    assert (q["inactive_days"], q["counts"]["inactive"]) == (40, 0)


@pytest.mark.spec("D-33", "S-34", "S-22.1")
def test_pr_changed_alert_is_scoped_and_disappears_with_its_cause(d: Dash) -> None:
    ppr = d.confirmed("PR-C1", (TONER, "1", "100"))
    with d.hx.engine.begin() as c:
        c.execute(
            text("UPDATE ppr SET state = 'PR_CHANGED_REVIEW_REQUIRED' WHERE id = :i"),
            {"i": ppr["id"]},
        )
    assert ("PR_CHANGED", ppr["ppr_number"]) in d.types(d.w.req)
    assert ("PR_CHANGED", ppr["ppr_number"]) not in d.types(d.or_req)
    assert d.alerts(d.w.req)["counts"]["PR_CHANGED"] == 1
    with d.hx.engine.begin() as c:
        c.execute(text("UPDATE ppr SET state = 'CANCELLED' WHERE id = :i"), {"i": ppr["id"]})
    d.age(ppr["id"], 100)
    assert not [t for t in d.types(d.officer) if t[1] == ppr["ppr_number"]]


@pytest.mark.spec("D-33", "S-34")
def test_sync_failure_alert_goes_to_sync_followers_until_a_later_success(d: Dash) -> None:
    with d.hx.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO sync_run (mode, scope, status, finished_at) "
                "VALUES ('NIGHTLY', 'PPRS', 'PARTIAL', now()), ('NIGHTLY', 'PPRS', 'RUNNING', NULL)"
            )
        )
    assert ("SYNC_FAILURE", "PPRS") in d.types(d.officer)  # RUNNING says nothing yet
    assert ("SYNC_FAILURE", "PPRS") in d.types(d.hx.h(d.hx.user("buyer", Role.PROCUREMENT)))
    assert not [t for t in d.types(d.exec) if t[0] == "SYNC_FAILURE"]
    assert not [t for t in d.types(d.w.req) if t[0] == "SYNC_FAILURE"]
    with d.hx.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO sync_run (mode, scope, status, finished_at) "
                "VALUES ('NIGHTLY', 'PPRS', 'SUCCEEDED', now())"
            )
        )
    assert not [t for t in d.types(d.officer) if t[0] == "SYNC_FAILURE"]


@pytest.mark.spec("D-33", "S-34")
def test_a_closed_plan_year_raises_no_plan_alerts(d: Dash) -> None:
    d.confirmed("PR-X1", (GLOVE, "10", "30"))
    assert ("PLAN_EXHAUSTED", GLOVE) in d.types(d.officer)
    assert d.w.plan.move("CLOSED").status_code == 200
    assert not [t for t in d.types(d.officer) if t[0].startswith("PLAN_")]
    dash = d.get(f"/api/dashboard?fiscal_year={FY}", d.officer)
    assert dash["plan_year_state"] == "CLOSED" and dash["totals"]["exhausted"] == 0


# ------------------------------------------------------------------ settings (D-33, §36)
@pytest.mark.spec("D-33", "S-36", "S-22.8")
def test_only_admin_changes_thresholds_with_a_reason_and_it_is_audited(d: Dash) -> None:
    rows = {s["code"]: s for s in d.get("/api/alert-settings", d.w.req)}
    assert (rows["PPR_INACTIVE_DAYS"]["value"], rows["PLAN_LOW_PERCENT"]["value"]) == (30, 20)
    assert (rows["PLAN_LOW_PERCENT"]["min"], rows["PLAN_LOW_PERCENT"]["max"]) == (1, 99)
    assert d.put_setting("PLAN_LOW_PERCENT", 10, d.officer).status_code == 403
    r = d.put_setting("PLAN_LOW_PERCENT", 10, d.admin, reason="  ")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED"
    r = d.put_setting("PLAN_LOW_PERCENT", 100, d.admin)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "SETTING_OUT_OF_RANGE"
    assert (
        d.hx.client.put(
            "/api/alert-settings/NOPE", json={"value": 1, "reason": "x"}, headers=d.admin
        ).status_code
        == 422
    )
    r = d.put_setting("PLAN_LOW_PERCENT", 10, d.admin, reason="คณะกรรมการกำหนด")
    assert r.status_code == 200 and r.json()["value"] == 10
    audit = d.w.plan.audit("ALERT_SETTING_CHANGED")
    assert [(a[1], a[2]) for a in audit] == [("คณะกรรมการกำหนด", {"value": 10})]


@pytest.mark.spec("D-33")
def test_the_database_refuses_out_of_range_values(d: Dash) -> None:
    with pytest.raises(Exception, match="value_in_range"), d.hx.engine.begin() as c:
        c.execute(text("UPDATE alert_setting SET value = 0 WHERE code = 'PPR_INACTIVE_DAYS'"))


# ------------------------------------------------------------------ review follow-ups
@pytest.mark.spec("D-33", "S-34")
def test_a_failed_master_sync_alerts_until_a_later_success(d: Dash) -> None:
    with d.hx.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO sync_run (mode, scope, status, finished_at) "
                "VALUES ('NIGHTLY', 'MASTERS', 'FAILED', now())"
            )
        )
    assert ("SYNC_FAILURE", "MASTERS") in d.types(d.officer)
    assert d.get("/api/dashboard", d.officer)["alerts"]["counts"]["SYNC_FAILURE"] == 1
    d.w.plan.sync()  # a later successful master sync
    assert not [t for t in d.types(d.officer) if t[0] == "SYNC_FAILURE"]


@pytest.mark.spec("D-33", "S-34")
def test_the_alert_list_is_capped_longest_idle_first_with_exact_counts(
    d: Dash, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ppr.application import dashboard_services

    a = d.confirmed("PR-T1", (TONER, "1", "100"))
    b = d.confirmed("PR-T2", (PAPER, "1", "50"))
    d.age(a["id"], 40)
    d.age(b["id"], 90)
    monkeypatch.setattr(dashboard_services, "ALERT_LIST_LIMIT", 1)
    got = d.alerts(d.officer)
    inactive = [x["ppr_number"] for x in got["alerts"] if x["type"] == "PPR_INACTIVE"]
    assert inactive == [b["ppr_number"]]  # the longest idle is shown
    assert got["counts"]["PPR_INACTIVE"] == 2 and got["truncated"] is True


@pytest.mark.spec("D-32", "S-22.1")
def test_a_requester_sees_items_they_purchase_or_demand(d: Dash) -> None:
    # Plan items of another year where MOCK-DEP-OR is only purchaser / demand department.
    p = d.w.plan
    assert (
        d.hx.client.post("/api/plan-years", json={"fiscal_year": 2571}, headers=p.h).status_code
        == 201
    )
    r = d.hx.client.post(
        "/api/plan-years/2571/budgets",
        json={
            "fund_source_id": "MOCK-FUND-1",
            "budget_category_id": "MOCK-CAT-OFFICE",
            "approved_amount": "200",
        },
        headers=p.h,
    )
    budget = r.json()["id"]
    base = {
        "plan_budget_id": budget,
        "plan_type": "ASSIGNED",
        "owner_department_id": "MOCK-DEP-ENT",
        "planned_qty": "10",
        "estimated_unit_price": "10",
        "planned_amount": "100",
    }
    for body in (
        {
            **base,
            "item_id": TONER,
            "purchasing_department_id": "MOCK-DEP-OR",
            "demands": [{"department_id": "MOCK-DEP-ENT", "demand_qty": "10"}],
        },
        {
            **base,
            "item_id": PAPER,
            "purchasing_department_id": "MOCK-DEP-ENT",
            "demands": [{"department_id": "MOCK-DEP-OR", "demand_qty": "10"}],
        },
        {
            **base,
            "item_id": GLOVE,
            "purchasing_department_id": "MOCK-DEP-ENT",
            "demands": [{"department_id": "MOCK-DEP-ENT", "demand_qty": "10"}],
        },
    ):
        assert (
            d.hx.client.post("/api/plan-years/2571/items", json=body, headers=p.h).status_code
            == 201
        )
    rows = d.get("/api/plan-years/2571/balances", d.or_req)
    assert sorted(r["item_id"] for r in rows) == sorted([TONER, PAPER])
    dash = d.get("/api/dashboard?fiscal_year=2571", d.or_req)
    assert dash["plan_year_state"] == "DRAFT" and dash["totals"]["items"] == 2
    assert dash["totals"]["used_amount"] == "0" and dash["totals"]["low"] == 0


@pytest.mark.spec("D-32", "S-22.1")
def test_a_requester_without_a_department_sees_nothing(d: Dash) -> None:
    d.confirmed("PR-N1", (TONER, "9", "100"))
    lone = d.hx.h(d.hx.user("lone", Role.REQUESTER, department=""))
    dash = d.get("/api/dashboard", lone)
    assert dash["totals"]["items"] == 0 and sum(dash["ppr_counts"].values()) == 0
    assert d.alerts(lone)["alerts"] == []
    assert d.get(f"/api/plan-years/{FY}/balances", lone) == []
