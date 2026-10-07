"""Wave 7B-1 through the API: Central Pool (spec §10.2, §12, §22.1, §37; D-16, D-38, G-1).

World: FY2570 ACTIVE, fund 1 OFFICE line 2000 - Mock ENT toner 10 / 1000 and paper 10 / 500
(DEPARTMENT), and a CENTRAL toner 5 / 500 bought by Mock Store (its owner field says Mock ENT,
to show that only the purchaser sees and draws on it). Server date 2026-10-15.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text

from api_harness import FY, Harness, Plan
from ppr.domain.roles import Role

TONER, PAPER = "MOCK-ITEM-TONER", "MOCK-ITEM-PAPER"
OFFICE = "MOCK-CAT-OFFICE"
ENT, STORE = "MOCK-DEP-ENT", "MOCK-DEP-STORE"


class Central:
    def __init__(self, hx: Harness) -> None:
        self.hx = hx
        self.plan = p = Plan(hx)
        p.sync()
        p.year()
        office = p.budget(OFFICE, "2000").json()["id"]
        assert p.item(office, "1000").status_code == 201
        assert p.item(office, "500", item_id=PAPER).status_code == 201
        r = p.item(
            office,
            "500",
            plan_type="CENTRAL",
            owner_department_id=ENT,
            purchasing_department_id=STORE,
            planned_qty="5",
        )
        assert r.status_code == 201, r.text
        self.central_id = int(r.json()["id"])
        assert p.move("APPROVED").status_code == 200
        r = p.move("ACTIVE")
        assert r.status_code == 200, r.text
        self.store = hx.h(hx.user("req_store", Role.REQUESTER, department=STORE))
        self.ent = hx.h(hx.user("req_ent", Role.REQUESTER, department=ENT))

    def draft(self, pr_no: str, dept: str, headers: dict[str, str], *lines: Any) -> Any:
        self.hx.put_pr(pr_no, *lines, dept=dept)
        return self.hx.create_ppr(pr_no, headers)

    def ledger(self, plan_item_id: int) -> list[tuple[str, str, str | None]]:
        with self.hx.engine.connect() as c:
            rows = c.execute(
                text(
                    "SELECT event_type, amount_delta, ppr_ref FROM plan_ledger "
                    "WHERE plan_item_id = :i ORDER BY id"
                ),
                {"i": plan_item_id},
            )
            return [(r[0], str(r[1]), r[2]) for r in rows]


@pytest.fixture
def c(db: Engine) -> Central:
    return Central(Harness(db))


@pytest.mark.spec("D-38", "G-1", "S-12")
def test_a_store_pr_draws_on_the_central_item(c: Central) -> None:
    r = c.draft("PR-C1", STORE, c.store, (TONER, "3", "100"))
    assert r.status_code == 201, r.text
    d = r.json()
    assert [i["plan_item_id"] for i in d["items"]] == [c.central_id]
    ok = c.hx.client.post(f"/api/pprs/{d['id']}/confirm", headers=c.store)
    assert ok.status_code == 200, ok.text
    assert c.ledger(c.central_id)[-1][:2] == ("PPR_CONFIRMED", "-300.00")
    bal = c.hx.client.get(f"/api/plan-items/{c.central_id}/balance", headers=c.plan.h).json()
    assert (bal["remaining_qty"], bal["remaining_amount"]) == ("2.0000", "200.00")


@pytest.mark.spec("D-38", "G-1", "D-16")
def test_a_department_pr_never_draws_on_the_central_item(c: Central) -> None:
    r = c.draft("PR-C2", ENT, c.ent, (TONER, "1", "100"))
    assert r.status_code == 201, r.text
    assert r.json()["items"][0]["plan_item_id"] != c.central_id  # ENT's own toner item


@pytest.mark.spec("D-38", "G-1")
def test_the_central_item_holds_its_own_hard_control(c: Central) -> None:
    r = c.draft("PR-C3", STORE, c.store, (TONER, "6", "100"))  # 6 > 5 planned
    assert r.status_code == 409
    codes = {x["code"] for x in r.json()["detail"]["details"]}
    assert "QTY_EXCEEDED" in codes


@pytest.mark.spec("D-38", "G-1", "S-22.1")
def test_only_the_purchasing_department_sees_the_central_item(c: Central) -> None:
    def ids(h: dict[str, str]) -> set[int]:
        items = c.hx.client.get(f"/api/plan-years/{FY}/items", headers=h).json()
        return {int(i["id"]) for i in items}

    assert c.central_id in ids(c.store)
    assert c.central_id not in ids(c.ent)  # the owner field alone gives no view
    assert c.central_id in ids(c.plan.h)  # organisation-wide


@pytest.mark.spec("D-38", "G-1", "S-37")
def test_central_plan_usage_report(c: Central) -> None:
    r = c.draft("PR-C4", STORE, c.store, (TONER, "2", "100"))
    assert (
        c.hx.client.post(f"/api/pprs/{r.json()['id']}/confirm", headers=c.store).status_code == 200
    )

    def rows(h: dict[str, str]) -> list[dict[str, Any]]:
        got = c.hx.client.get("/api/reports/CENTRAL_PLAN_USAGE", headers=h)
        assert got.status_code == 200, got.text
        return list(got.json()["rows"])

    # Toner is also on ENT's DEPARTMENT plan: the central line keeps its plan figures and the
    # item's HOSxP issues go on an item row, never onto the central line (D-39).
    row, total = rows(c.plan.h)
    assert (row["purchasing_department"], row["purchased_qty"], row["remaining_amount"]) == (
        "Mock Store",
        "2.0000",
        "300.00",
    )
    assert row.get("issued_qty") is None
    assert total["issued_qty"] == "ยังไม่มีข้อมูล"  # until IT verifies a query (D-39)
    assert total["not_issued_qty"] == "ยังไม่มีข้อมูล"
    assert [x["item_name"] for x in rows(c.store)] == ["Mock toner", "Mock toner"]
    assert rows(c.ent) == []
    xlsx = c.hx.client.get("/api/reports/CENTRAL_PLAN_USAGE/xlsx", headers=c.plan.h)
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"


@pytest.mark.spec("D-38", "G-1", "S-12")
def test_a_line_matching_a_department_and_a_central_item_fails_closed(c: Central) -> None:
    # A plan from before D-38 could hold both; simulate one: a CENTRAL toner bought by ENT
    # next to ENT's own DEPARTMENT toner (activation and amendment now refuse this).
    with c.hx.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plan_item (plan_year_id, plan_budget_id, plan_type, item_source_id, "
                "item_code_snapshot, item_name_snapshot, unit_snapshot, "
                "owner_department_source_id, purchasing_department_source_id, planned_qty, "
                "estimated_unit_price, planned_amount) "
                "SELECT plan_year_id, plan_budget_id, 'CENTRAL', item_source_id, "
                "item_code_snapshot, item_name_snapshot, unit_snapshot, :store, :ent, 1, 0, 0 "
                "FROM plan_item WHERE id = :cid"
            ),
            {"store": STORE, "ent": ENT, "cid": c.central_id},
        )
    r = c.draft("PR-C5", ENT, c.ent, (TONER, "1", "100"))
    assert r.status_code == 409
    codes = {x["code"] for x in r.json()["detail"]["details"]}
    # D-43: both rows are offered and neither is chosen automatically (fails closed)
    assert "PLAN_ROW_NOT_CHOSEN" in codes


@pytest.mark.spec("D-38", "G-1", "D-37")
def test_amendments_keep_central_items_bought_and_unambiguous(c: Central) -> None:
    body = {"approval_document_no": "D", "approval_date": "2026-10-14", "reason": "r"}
    base = f"/api/plan-years/{FY}/amendments"
    r = c.hx.client.post(
        base,
        json={**body, "items": [{"plan_item_id": c.central_id, "purchasing_department_id": ""}]},
        headers=c.plan.h,
    )
    assert r.status_code == 409
    assert "CENTRAL_WITHOUT_PURCHASER" in {x["code"] for x in r.json()["detail"]["details"]}
    r = c.hx.client.post(
        base,
        json={**body, "items": [{"plan_item_id": c.central_id, "purchasing_department_id": ENT}]},
        headers=c.plan.h,
    )
    assert r.status_code == 409
    assert "DUPLICATE_MATCH_KEY" in {x["code"] for x in r.json()["detail"]["details"]}


@pytest.mark.spec("D-38", "G-1", "S-22.1")
def test_the_owner_field_gives_no_view_anywhere(c: Central) -> None:
    for path in (f"/api/plan-items/{c.central_id}", f"/api/plan-items/{c.central_id}/balance"):
        assert c.hx.client.get(path, headers=c.ent).status_code == 404, path
        assert c.hx.client.get(path, headers=c.store).status_code == 200, path
    # amendment history: a change to the Central Pool item is the store's, not ENT's
    body = {"approval_document_no": "D", "approval_date": "2026-10-14", "reason": "r"}
    r = c.hx.client.post(
        f"/api/plan-years/{FY}/amendments",
        json={**body, "items": [{"plan_item_id": c.central_id, "estimated_unit_price": "90"}]},
        headers=c.plan.h,
    )
    assert r.status_code == 201, r.text
    seen = c.hx.client.get(f"/api/plan-years/{FY}/amendments", headers=c.ent).json()
    assert seen == []
    assert len(c.hx.client.get(f"/api/plan-years/{FY}/amendments", headers=c.store).json()) == 1
    ent_rows = c.hx.client.get("/api/reports/PLAN_AMENDMENT", headers=c.ent).json()["rows"]
    assert ent_rows == []
    store_rows = c.hx.client.get("/api/reports/PLAN_AMENDMENT", headers=c.store).json()["rows"]
    assert [x["department"] for x in store_rows] == ["Mock Store"]


@pytest.mark.spec("D-38", "G-1", "D-32")
def test_central_items_count_under_the_purchasing_department(c: Central) -> None:
    dash = c.hx.client.get("/api/dashboard", headers=c.plan.h).json()
    by_dept = {x["department_id"]: x["planned_amount"] for x in dash["by_department"]}
    assert by_dept == {ENT: "1500.00", STORE: "500.00"}
    rows = c.hx.client.get(
        "/api/reports/PLAN_REMAINING", params={"department_id": STORE}, headers=c.store
    ).json()["rows"]
    assert [(x["department"], x["plan_type"]) for x in rows] == [("Mock Store", "แผนกลาง")]
    util = c.hx.client.get("/api/reports/DEPARTMENT_UTILIZATION", headers=c.plan.h).json()["rows"]
    assert {x["department"]: x["planned_amount"] for x in util} == {
        "Mock ENT": "1500.00",
        "Mock Store": "500.00",
    }
