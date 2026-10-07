"""Wave 7A through the API: Plan Amendment (spec §20, §19, §22.1, §36; D-11, D-37).

World (test_ppr_api): FY2570 ACTIVE, Mock ENT fund 1 - OFFICE line 1500 (toner 10 / 1000,
paper 10 / 500), MED line 300 (gloves 10 / 300); Mock OR fund 2 - MAT line 100 (paper
10 / 100). Server date 2026-10-15.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text
from test_ppr_api import GLOVE, MED, OFFICE, PAPER, TONER, World

from api_harness import FY, Harness, Plan
from ppr.domain.roles import Role

DOC = {"approval_document_no": "สส 0032.1/123", "approval_date": "2026-10-14", "reason": "r"}


class Am:
    def __init__(self, w: World) -> None:
        self.w = w
        self.hx = w.hx
        self.officer = w.plan.h

    def body(self, **parts: Any) -> dict[str, Any]:
        return {**DOC, **parts}

    def post(self, headers: dict[str, str] | None = None, **parts: Any) -> Any:
        return self.hx.client.post(
            f"/api/plan-years/{FY}/amendments",
            json=self.body(**parts),
            headers=headers or self.officer,
        )

    def preview(self, **parts: Any) -> Any:
        return self.hx.client.post(
            f"/api/plan-years/{FY}/amendments/preview",
            json=self.body(**parts),
            headers=self.officer,
        )

    def item(self, pid: int) -> dict[str, Any]:
        items = self.hx.client.get(f"/api/plan-years/{FY}/items", headers=self.officer).json()
        return dict(next(i for i in items if i["id"] == pid))

    def balance(self, pid: int) -> dict[str, Any]:
        r = self.hx.client.get(f"/api/plan-items/{pid}/balance", headers=self.officer)
        assert r.status_code == 200, r.text
        return dict(r.json())

    def rows(self, sql: str, **p: Any) -> list[tuple[Any, ...]]:
        with self.hx.engine.connect() as c:
            return [tuple(r) for r in c.execute(text(sql), p)]

    def confirmed(self, pr_no: str, *lines: tuple[str, str, str]) -> None:
        d = self.w.draft(pr_no, *lines)
        r = self.w.confirm(d["id"])
        assert r.status_code == 200, r.text


@pytest.fixture
def a(db: Engine) -> Am:
    return Am(World(Harness(db)))


def _codes(r: Any) -> set[str]:
    return {d["code"] for d in r.json()["detail"].get("details", [])}


# ------------------------------------------------------------------ recording
@pytest.mark.spec("AT-40.11.1", "AT-40.11.4", "S-20", "S-18.2", "D-37")
def test_transfer_between_items_posts_ledger_history_and_audit(a: Am) -> None:
    toner, paper = a.w.pid(TONER), a.w.pid(PAPER)
    r = a.post(
        items=[
            {"plan_item_id": toner, "planned_qty": "12", "planned_amount": "1200"},
            {"plan_item_id": paper, "planned_qty": "6", "planned_amount": "300"},
        ]
    )
    assert r.status_code == 201, r.text
    rec = r.json()
    assert (rec["amendment_no"], rec["approval_document_no"]) == (1, DOC["approval_document_no"])
    assert [(c["target"], c["target_id"], c["change_kind"]) for c in rec["changes"]] == [
        ("ITEM", toner, "UPDATE"),
        ("ITEM", paper, "UPDATE"),
    ]
    first = rec["changes"][0]
    assert (first["before"]["planned_qty"], first["after"]["planned_qty"]) == ("10.0000", "12.0000")
    # current values, ledger and balance follow
    assert (a.item(toner)["planned_qty"], a.item(toner)["planned_amount"]) == ("12.0000", "1200.00")
    b = a.balance(toner)
    assert (b["amendment_qty"], b["amendment_amount"], b["remaining_qty"]) == (
        "2.0000",
        "200.00",
        "12.0000",
    )
    ledger = a.rows(
        "SELECT plan_item_id, qty_delta, amount_delta, plan_amendment_id, idempotency_key "
        "FROM plan_ledger WHERE event_type = 'PLAN_AMENDMENT' ORDER BY id"
    )
    assert [(x[0], str(x[1]), str(x[2]), x[3]) for x in ledger] == [
        (toner, "2.0000", "200.00", rec["id"]),
        (paper, "-4.0000", "-200.00", rec["id"]),
    ]
    (action, reason, after), *_ = a.w.plan.audit("PLAN_AMENDED")
    assert (action, reason, after["amendment_no"], len(after["changes"])) == (
        "PLAN_AMENDED",
        "r",
        1,
        2,
    )
    # a second amendment is numbered 2
    again = a.post(items=[{"plan_item_id": paper, "estimated_unit_price": "40"}])
    assert again.status_code == 201 and again.json()["amendment_no"] == 2


@pytest.mark.spec("AT-40.11.1", "D-37", "D-11")
def test_increase_with_a_new_item_and_a_new_budget_line(a: Am) -> None:
    r = a.post(
        budgets=[
            {"fund_source_id": "MOCK-FUND-1", "budget_category_id": MED, "approved_amount": "450"},
            {
                "fund_source_id": "MOCK-FUND-2",
                "budget_category_id": OFFICE,
                "approved_amount": "80",
            },
        ],
        items=[{"plan_item_id": a.w.pid(GLOVE), "planned_qty": "15", "planned_amount": "450"}],
        new_items=[
            {
                "ref": "n1",
                "plan_type": "DEPARTMENT",
                "item_id": TONER,
                "owner_department_id": "MOCK-DEP-OR",
                "fund_source_id": "MOCK-FUND-2",
                "budget_category_id": OFFICE,
                "planned_qty": "1",
                "estimated_unit_price": "80",
                "planned_amount": "80",
            }
        ],
    )
    assert r.status_code == 201, r.text
    kinds = [(c["target"], c["change_kind"]) for c in r.json()["changes"]]
    assert kinds == [
        ("BUDGET", "UPDATE"),
        ("BUDGET", "CREATE"),
        ("ITEM", "UPDATE"),
        ("ITEM", "CREATE"),
    ]
    new_id = r.json()["changes"][3]["target_id"]
    b = a.balance(new_id)
    assert (b["approved_qty"], b["amendment_qty"], b["remaining_amount"]) == (
        "0.0000",
        "1.0000",
        "80.00",
    )
    events = a.rows(
        "SELECT event_type FROM plan_ledger WHERE plan_item_id = :i ORDER BY id", i=new_id
    )
    assert [e[0] for e in events] == ["PLAN_ACTIVATED", "PLAN_AMENDMENT"]
    budgets = a.hx.client.get(f"/api/plan-years/{FY}/budgets", headers=a.officer).json()
    assert {
        (x["fund_source_id"], x["budget_category_id"], x["approved_amount"]) for x in budgets
    } >= {
        ("MOCK-FUND-1", MED, "450.00"),
        ("MOCK-FUND-2", OFFICE, "80.00"),
    }


@pytest.mark.spec("D-37")
def test_an_unused_item_closed_to_zero_takes_no_new_ppr(a: Am) -> None:
    paper, toner = a.w.pid(PAPER), a.w.pid(TONER)
    r = a.post(
        items=[
            {"plan_item_id": paper, "planned_qty": "0", "planned_amount": "0"},
            {"plan_item_id": toner, "planned_qty": "15", "planned_amount": "1500"},
        ]
    )
    assert r.status_code == 201, r.text
    assert a.balance(paper)["remaining_qty"] == "0.0000"
    a.hx.put_pr("PR-Z", (PAPER, "1", "10"))
    r = a.hx.create_ppr("PR-Z", a.w.req)
    assert r.status_code == 409 and "QTY_EXHAUSTED" in _codes(r)  # nothing left on it


@pytest.mark.spec("D-37")
def test_an_unused_item_may_change_its_data(a: Am) -> None:
    paper = a.w.pid(PAPER)
    r = a.post(items=[{"plan_item_id": paper, "owner_department_id": "MOCK-DEP-IT"}])
    assert r.status_code == 201, r.text
    assert a.item(paper)["owner_department_id"] == "MOCK-DEP-IT"
    assert a.rows("SELECT count(*) FROM plan_ledger WHERE event_type = 'PLAN_AMENDMENT'") == [(0,)]


# ------------------------------------------------------------------ refusals
@pytest.mark.spec("AT-40.11.2", "AT-40.11.3", "S-20", "D-37")
def test_reducing_below_used_is_refused_and_nothing_is_written(a: Am) -> None:
    a.confirmed("PR-U", (TONER, "9", "100"))  # toner: 9 of 10 used, 900 of 1000
    toner, paper = a.w.pid(TONER), a.w.pid(PAPER)
    before = a.rows("SELECT count(*) FROM plan_ledger")
    r = a.post(
        items=[
            {"plan_item_id": toner, "planned_qty": "8", "planned_amount": "800"},
            {"plan_item_id": paper, "planned_qty": "12", "planned_amount": "700"},
        ]
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "AMENDMENT_NOT_VALID"
    assert {"BELOW_USED_QTY", "BELOW_USED_AMOUNT"} <= _codes(r)
    assert a.rows("SELECT count(*) FROM plan_ledger") == before
    assert a.rows("SELECT count(*) FROM plan_amendment") == [(0,)]
    assert a.item(toner)["planned_qty"] == "10.0000"


@pytest.mark.spec("D-37")
def test_a_used_item_keeps_its_data(a: Am) -> None:
    a.confirmed("PR-U2", (TONER, "1", "100"))
    r = a.post(items=[{"plan_item_id": a.w.pid(TONER), "owner_department_id": "MOCK-DEP-IT"}])
    assert r.status_code == 409 and "DATA_CHANGE_AFTER_USE" in _codes(r)


@pytest.mark.spec("D-11", "D-37")
def test_unbalanced_amendment_is_refused(a: Am) -> None:
    r = a.post(items=[{"plan_item_id": a.w.pid(TONER), "planned_amount": "1100"}])
    assert r.status_code == 409 and _codes(r) == {"ENVELOPE_MISMATCH"}


@pytest.mark.spec("D-37", "D-38", "G-3")
def test_new_assigned_items_record_their_demand(a: Am) -> None:
    item: dict[str, object] = {
        "ref": "x",
        "plan_type": "ASSIGNED",
        "item_id": TONER,
        "owner_department_id": "MOCK-DEP-STORE",
        "purchasing_department_id": "MOCK-DEP-STORE",
        "fund_source_id": "MOCK-FUND-1",
        "budget_category_id": MED,
        "planned_qty": "1",
        "estimated_unit_price": "100",
        "planned_amount": "100",
    }
    budgets = [
        {"fund_source_id": "MOCK-FUND-1", "budget_category_id": MED, "approved_amount": "400"}
    ]
    r = a.post(budgets=budgets, new_items=[item])
    assert r.status_code == 409 and "ASSIGNED_WITHOUT_DEMAND" in _codes(r)  # D-38 A-6
    item["demands"] = [{"department_id": "MOCK-DEP-ENT", "demand_qty": "1"}]
    r = a.post(budgets=budgets, new_items=[item])
    assert r.status_code == 201, r.text
    demand = [c for c in r.json()["changes"] if c["target"] == "DEMAND"]
    assert [
        (c["change_kind"], c["after"]["department_id"], c["after"]["demand_qty"]) for c in demand
    ] == [("CREATE", "MOCK-DEP-ENT", "1.0000")]


@pytest.mark.parametrize(
    "change,status,code",
    [
        ({"approval_date": "2026-10-16"}, 422, "APPROVAL_DATE_IN_FUTURE"),
        ({"approval_document_no": "   "}, 422, "APPROVAL_DOCUMENT_REQUIRED"),
        ({"reason": "  "}, 422, "REASON_REQUIRED"),
    ],
)
@pytest.mark.spec("D-37")
def test_the_approval_document_and_reason_are_required(
    a: Am, change: dict[str, str], status: int, code: str
) -> None:
    body = {
        **DOC,
        **change,
        "items": [{"plan_item_id": a.w.pid(PAPER), "estimated_unit_price": "40"}],
    }
    r = a.hx.client.post(f"/api/plan-years/{FY}/amendments", json=body, headers=a.officer)
    assert r.status_code == status and r.json()["detail"]["code"] == code


def test_unknown_or_inactive_masters_are_refused(a: Am) -> None:
    r = a.post(items=[{"plan_item_id": a.w.pid(PAPER), "item_id": "MOCK-ITEM-OLD"}])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "INACTIVE_MASTER"
    r = a.post(items=[{"plan_item_id": a.w.pid(PAPER), "owner_department_id": "NOPE"}])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "UNKNOWN_MASTER"


@pytest.mark.spec("D-37")
def test_only_an_active_year_is_amended(db: Engine) -> None:
    hx = Harness(db)
    p = Plan(hx)
    p.sync()
    p.year()  # DRAFT
    r = hx.client.post(f"/api/plan-years/{FY}/amendments", json={**DOC}, headers=p.h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PLAN_YEAR_NOT_ACTIVE"


@pytest.mark.spec("AT-40.11.5", "D-37", "D-15")
def test_an_active_plan_has_no_direct_edit_only_amendment(a: Am) -> None:
    toner = a.w.pid(TONER)
    item = a.item(toner)
    body = {k: item[k] for k in ("plan_budget_id", "plan_type", "item_id", "owner_department_id")}
    body.update(planned_qty="10", estimated_unit_price="100", planned_amount="1")
    r = a.hx.client.put(f"/api/plan-items/{toner}", json=body, headers=a.officer)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PLAN_YEAR_LOCKED"


@pytest.mark.spec("S-22", "D-37")
def test_only_a_plan_officer_amends(a: Am) -> None:
    body = {"items": [{"plan_item_id": a.w.pid(PAPER), "estimated_unit_price": "40"}]}
    for role in (Role.REQUESTER, Role.ADMIN, Role.CFO, Role.EXECUTIVE, Role.FINANCE):
        h = a.hx.h(a.hx.user(f"u_{role.value}", role, department="MOCK-DEP-ENT"))
        assert a.post(h, **body).status_code == 403
        r = a.hx.client.post(
            f"/api/plan-years/{FY}/amendments/preview", json=a.body(**body), headers=h
        )
        assert r.status_code == 403


def test_preview_judges_and_writes_nothing(a: Am) -> None:
    r = a.preview(items=[{"plan_item_id": a.w.pid(TONER), "planned_amount": "1100"}])
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] is False and [e["code"] for e in j["errors"]] == ["ENVELOPE_MISMATCH"]
    assert j["items"][0]["amount_delta"] == "100.00" and j["items"][0]["item_name"] == "Mock toner"
    assert a.rows("SELECT count(*) FROM plan_amendment") == [(0,)]
    ok = a.preview(items=[{"plan_item_id": a.w.pid(PAPER), "estimated_unit_price": "40"}]).json()
    assert ok["ok"] is True and ok["warnings"][0]["code"] == "AMOUNT_DIFFERS_FROM_ESTIMATE"


# ------------------------------------------------------------------ reading (§22.1)
@pytest.mark.spec("S-22.1", "D-37")
def test_history_is_scoped_like_plan_items(a: Am) -> None:
    ent_paper, or_paper = a.w.pid(PAPER), int(a.w.item_ids[(PAPER, "MOCK-FUND-2")])
    assert (
        a.post(items=[{"plan_item_id": ent_paper, "estimated_unit_price": "40"}]).status_code == 201
    )
    assert (
        a.post(items=[{"plan_item_id": or_paper, "estimated_unit_price": "9"}]).status_code == 201
    )
    all_ = a.hx.client.get(f"/api/plan-years/{FY}/amendments", headers=a.officer).json()
    assert [x["amendment_no"] for x in all_] == [2, 1]  # newest first
    or_req = a.hx.h(a.hx.user("req_or", Role.REQUESTER, department="MOCK-DEP-OR"))
    mine = a.hx.client.get(f"/api/plan-years/{FY}/amendments", headers=or_req).json()
    assert [(x["amendment_no"], [c["target_id"] for c in x["changes"]]) for x in mine] == [
        (2, [or_paper])
    ]
    first_id = all_[1]["id"]
    assert a.hx.client.get(f"/api/plan-amendments/{first_id}", headers=or_req).status_code == 404
    assert a.hx.client.get(f"/api/plan-amendments/{first_id}", headers=a.officer).status_code == 200


# ------------------------------------------------------------------ report and export (§37)
def _report(a: Am, code: str, headers: dict[str, str], **q: Any) -> list[dict[str, Any]]:
    r = a.hx.client.get(f"/api/reports/{code}", params=q, headers=headers)
    assert r.status_code == 200, r.text
    return list(r.json()["rows"])


@pytest.mark.spec("AT-40.11.6", "S-37", "D-37")
def test_amendment_report_and_plan_exports_show_the_amended_plan(a: Am) -> None:
    toner, paper = a.w.pid(TONER), a.w.pid(PAPER)
    assert (
        a.post(
            items=[
                {"plan_item_id": toner, "planned_qty": "12", "planned_amount": "1200"},
                {
                    "plan_item_id": paper,
                    "planned_qty": "6",
                    "planned_amount": "300",
                    "owner_department_id": "MOCK-DEP-IT",
                },
            ]
        ).status_code
        == 201
    )
    rows = _report(a, "PLAN_AMENDMENT", a.officer)
    assert [(x["amendment_no"], x["change"], x["item_name"]) for x in rows] == [
        (1, "แก้ไขรายการแผน", "Mock toner"),
        (1, "แก้ไขรายการแผน", "Mock paper"),
    ]
    assert (rows[0]["qty_before"], rows[0]["qty_after"], rows[0]["amount_after"]) == (
        "10.0000",
        "12.0000",
        "1200.00",
    )
    assert rows[1]["other_changes"] == "หน่วยงานเจ้าของแผน: Mock ENT → Mock IT"
    assert rows[0]["approval_document_no"] == DOC["approval_document_no"]
    # the plan reports (and so their Excel files) show the amended plan, not the original
    remaining = {x["item_name"]: x for x in _report(a, "PLAN_REMAINING", a.officer)}
    assert remaining["Mock toner"]["planned_qty"] == "12.0000"
    xlsx = a.hx.client.get("/api/reports/PLAN_AMENDMENT/xlsx", headers=a.officer)
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
    # a requester sees only their own department's item changes
    it_req = a.hx.h(a.hx.user("req_it", Role.REQUESTER, department="MOCK-DEP-IT"))
    assert [x["item_name"] for x in _report(a, "PLAN_AMENDMENT", it_req)] == ["Mock paper"]
    assert _report(a, "PLAN_AMENDMENT", a.officer, date_from="2026-10-15") == []


@pytest.mark.spec("D-37")
def test_a_form_built_on_an_older_plan_is_refused(a: Am) -> None:
    paper = a.w.pid(PAPER)
    first = a.post(
        base_amendment_no=0, items=[{"plan_item_id": paper, "estimated_unit_price": "40"}]
    )
    assert first.status_code == 201, first.text
    stale = a.post(
        base_amendment_no=0, items=[{"plan_item_id": paper, "estimated_unit_price": "45"}]
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "PLAN_CHANGED_SINCE_FORM"
    fresh = a.post(
        base_amendment_no=1, items=[{"plan_item_id": paper, "estimated_unit_price": "45"}]
    )
    assert fresh.status_code == 201


@pytest.mark.spec("D-37")
def test_history_keeps_the_item_code_and_name_and_blank_purchaser_is_none(a: Am) -> None:
    r = a.post(
        items=[{"plan_item_id": a.w.pid(PAPER), "purchasing_department_id": ""}],
        budgets=[
            {"fund_source_id": "MOCK-FUND-1", "budget_category_id": MED, "approved_amount": "400"}
        ],
        new_items=[
            {
                "ref": "n",
                "plan_type": "DEPARTMENT",
                "item_id": TONER,
                "owner_department_id": "MOCK-DEP-OR",
                "purchasing_department_id": "",
                "fund_source_id": "MOCK-FUND-1",
                "budget_category_id": MED,
                "planned_qty": "1",
                "estimated_unit_price": "100",
                "planned_amount": "100",
            }
        ],
    )
    assert r.status_code == 201, r.text
    created = r.json()["changes"][-1]["after"]
    assert (created["item_code"], created["item_name"], created["purchasing_department_id"]) == (
        "I1",
        "Mock toner",
        None,
    )
