"""Wave 7B-2 through the API: Assigned Purchase (spec §10.3; D-38 A-1 ... A-6, G-3).

World: FY2570 ACTIVE, fund 1 OFFICE line 4000 -
* Mock ENT's own toner 10 @ 100 (DEPARTMENT);
* an ASSIGNED toner 30 @ 100 bought by Mock Store for Mock ENT (demand 10) and Mock Ward A
  (demand 20).
Server date 2026-10-15. The Head of Procurement is appointed for the year.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from api_harness import FY, Harness, Plan
from ppr.domain.roles import Role
from ppr.integration.hosxp.contracts import HosxpPrStatus

TONER = "MOCK-ITEM-TONER"
OFFICE = "MOCK-CAT-OFFICE"
ENT, WARD, STORE = "MOCK-DEP-ENT", "MOCK-DEP-WARD-A", "MOCK-DEP-STORE"
DOC = {"approval_document_no": "D-1", "approval_date": "2026-10-14", "reason": "r"}


class Assigned:
    def __init__(self, hx: Harness) -> None:
        self.hx = hx
        self.plan = p = Plan(hx)
        p.sync()
        p.year()
        office = p.budget(OFFICE, "4000").json()["id"]
        assert p.item(office, "1000").status_code == 201
        r = p.item(
            office,
            "3000",
            plan_type="ASSIGNED",
            owner_department_id=STORE,
            purchasing_department_id=STORE,
            planned_qty="30",
            estimated_unit_price="100",
            demands=[
                {"department_id": ENT, "demand_qty": "10"},
                {"department_id": WARD, "demand_qty": "20"},
            ],
        )
        assert r.status_code == 201, r.text
        self.item_id = int(r.json()["id"])
        assert p.move("APPROVED").status_code == 200
        r = p.move("ACTIVE")
        assert r.status_code == 200, r.text
        self.store = hx.h(hx.user("req_store", Role.REQUESTER, department=STORE))
        self.ent = hx.h(hx.user("req_ent", Role.REQUESTER, department=ENT))
        admin = hx.h(hx.user("admin", Role.ADMIN))
        self.head = hx.h(hx.user("head", Role.HEAD_OF_PROCUREMENT))
        with hx.engine.connect() as c:
            head_id = c.execute(text("SELECT id FROM app_user WHERE username = 'head'")).scalar()
        r = hx.client.post(
            "/api/appointments",
            json={"user_id": head_id, "fiscal_year": FY, "effective_from": "2026-10-01"},
            headers=admin,
        )
        assert r.status_code == 201, r.text

    # -------------------------------------------------------------- helpers
    def draft(self, pr_no: str, qty: str, dept: str = STORE) -> dict[str, Any]:
        self.hx.put_pr(pr_no, (TONER, qty, "100"), dept=dept)
        r = self.hx.create_ppr(pr_no, self.store if dept == STORE else self.ent)
        assert r.status_code == 201, r.text
        return dict(r.json())

    def confirm(self, ppr_id: int) -> Any:
        return self.hx.client.post(f"/api/pprs/{ppr_id}/confirm", headers=self.store)

    def coverage(self, ppr_id: int) -> dict[str, tuple[str, str]]:
        r = self.hx.client.get(f"/api/pprs/{ppr_id}/coverage", headers=self.store)
        assert r.status_code == 200, r.text
        (item,) = r.json()
        return {x["department_id"]: (x["working"], x["confirmed"]) for x in item["lines"]}

    def put_coverage(self, ppr_id: int, headers: dict[str, str] | None = None, **qty: str) -> Any:
        rows = [
            {"plan_item_id": self.item_id, "department_id": {"ent": ENT, "ward": WARD}[k], "qty": v}
            for k, v in qty.items()
        ]
        return self.hx.client.put(
            f"/api/pprs/{ppr_id}/coverage", json={"coverage": rows}, headers=headers or self.store
        )

    def states(self) -> dict[str, str]:
        r = self.hx.client.get(f"/api/plan-items/{self.item_id}", headers=self.plan.h)
        return {d["department_id"]: d["state"] for d in r.json()["demands"]}

    def verify(self, ppr: dict[str, Any]) -> Any:
        return self.hx.client.post(
            f"/api/pprs/{ppr['id']}/verify", json={"version": ppr["version"]}, headers=self.head
        )

    def cancel(self, pr_no: str, ppr_id: int) -> None:
        gw: Any = self.hx.gateway
        h = gw.data.prs[pr_no].model_copy(
            update={"status": HosxpPrStatus.CANCELLED, "native_status": "MOCK-CANCELLED"}
        )
        gw.replace_pr(h, list(gw.data.pr_items[pr_no]))
        r = self.hx.client.post(f"/api/pprs/{ppr_id}/sync", headers=self.plan.h)
        assert r.status_code == 200, r.text

    def amend(self, *demands: tuple[str, str]) -> Any:
        return self.hx.client.post(
            f"/api/plan-years/{FY}/amendments",
            json={
                **DOC,
                "demands": [
                    {"plan_item_id": self.item_id, "department_id": d, "demand_qty": q}
                    for d, q in demands
                ],
            },
            headers=self.plan.h,
        )

    def audit(self, action: str) -> list[dict[str, Any]]:
        with self.hx.engine.connect() as c:
            rows = c.execute(
                text("SELECT after FROM audit_log WHERE action = :a ORDER BY id"), {"a": action}
            )
            return [dict(r[0]) for r in rows]


@pytest.fixture
def a(db: Engine) -> Assigned:
    return Assigned(Harness(db))


def _codes(r: Any) -> set[str]:
    return {x["code"] for x in r.json()["detail"]["details"]}


# ------------------------------------------------------------------ matching and A-2
@pytest.mark.spec("D-38", "G-3", "S-12")
def test_the_purchasers_pr_draws_on_the_assigned_item_with_a_proposed_coverage(
    a: Assigned,
) -> None:
    d = a.draft("PR-A1", "30")
    assert [i["plan_item_id"] for i in d["items"]] == [a.item_id]
    # Buying exactly what every department needs: the draft proposes each one's need.
    assert a.coverage(d["id"]) == {ENT: ("10.0000", "0"), WARD: ("20.0000", "0")}
    assert a.states() == {ENT: "PLANNED", WARD: "PLANNED"}
    r = a.confirm(d["id"])
    assert r.status_code == 200, r.text
    assert a.states() == {ENT: "INCLUDED_IN_CENTRAL_PURCHASE", WARD: "INCLUDED_IN_CENTRAL_PURCHASE"}
    assert a.coverage(d["id"]) == {ENT: ("10.0000", "10.0000"), WARD: ("20.0000", "20.0000")}
    (v,) = a.hx.client.get(f"/api/pprs/{d['id']}/versions", headers=a.store).json()
    assert v["snapshot"]["coverage"] == [
        {"plan_item_id": a.item_id, "department_id": ENT, "qty": "10.0000"},
        {"plan_item_id": a.item_id, "department_id": WARD, "qty": "20.0000"},
    ]
    changes = a.audit("DEMAND_STATE_CHANGED")
    assert {(c["department_id"], c["state_after"]) for c in changes} == {
        (ENT, "INCLUDED_IN_CENTRAL_PURCHASE"),
        (WARD, "INCLUDED_IN_CENTRAL_PURCHASE"),
    }
    assert changes[0]["cause"] == {"ppr_id": d["id"], "event": "CONFIRM", "version": 1}


@pytest.mark.spec("D-38", "G-3")
def test_coverage_must_add_up_and_never_exceed_the_need(a: Assigned) -> None:
    d = a.draft("PR-A2", "15")
    assert a.coverage(d["id"]) == {ENT: ("0", "0"), WARD: ("0", "0")}  # no proposal
    r = a.confirm(d["id"])
    assert r.status_code == 409 and _codes(r) == {"COVERAGE_REQUIRED"}
    assert a.put_coverage(d["id"], ent="10", ward="6").status_code == 200
    r = a.confirm(d["id"])
    assert r.status_code == 409 and _codes(r) == {"COVERAGE_SUM_MISMATCH"}
    assert a.put_coverage(d["id"], ent="11", ward="4").status_code == 200
    r = a.confirm(d["id"])
    assert r.status_code == 409 and _codes(r) == {"COVERAGE_EXCEEDS_NEED"}
    assert a.put_coverage(d["id"], ent="10", ward="5").status_code == 200
    r = a.confirm(d["id"])
    assert r.status_code == 200, r.text
    assert a.states() == {ENT: "INCLUDED_IN_CENTRAL_PURCHASE", WARD: "PLANNED"}
    # A second purchase: Ward A still needs 15, and that is proposed.
    d2 = a.draft("PR-A3", "15")
    assert a.coverage(d2["id"]) == {ENT: ("0", "0"), WARD: ("15.0000", "0")}
    assert a.confirm(d2["id"]).status_code == 200
    assert a.states() == {ENT: "INCLUDED_IN_CENTRAL_PURCHASE", WARD: "INCLUDED_IN_CENTRAL_PURCHASE"}


@pytest.mark.spec("D-38", "G-3", "D-09")
def test_only_the_purchasing_requester_edits_the_coverage(a: Assigned) -> None:
    d = a.draft("PR-A4", "30")
    assert a.put_coverage(d["id"], headers=a.ent, ent="10", ward="20").status_code == 404
    r = a.put_coverage(d["id"], ent="10", ward="20")
    assert r.status_code == 200
    r = a.hx.client.put(
        f"/api/pprs/{d['id']}/coverage",
        json={
            "coverage": [{"plan_item_id": a.item_id, "department_id": "MOCK-DEP-OR", "qty": "1"}]
        },
        headers=a.store,
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == "COVERAGE_UNKNOWN_DEPARTMENT"
    assert a.confirm(d["id"]).status_code == 200
    r = a.put_coverage(d["id"], ent="10", ward="20")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PPR_LOCKED"


# ------------------------------------------------------------------ A-1
@pytest.mark.spec("D-38", "G-3", "S-12")
def test_a_department_with_open_demand_cannot_buy_the_item_itself(a: Assigned) -> None:
    a.hx.put_pr("PR-E1", (TONER, "1", "100"), dept=ENT)
    r = a.hx.create_ppr("PR-E1", a.ent)
    assert r.status_code == 409
    assert "DEMAND_IN_CENTRAL_PURCHASE" in _codes(r)
    # Still blocked while included in the store's purchase ...
    d = a.draft("PR-A5", "30")
    assert a.confirm(d["id"]).status_code == 200
    r = a.hx.create_ppr("PR-E1", a.ent)
    assert "DEMAND_IN_CENTRAL_PURCHASE" in _codes(r)
    # ... and no longer once it is fulfilled.
    ppr = a.hx.client.get(f"/api/pprs/{d['id']}", headers=a.store).json()
    assert a.verify(ppr).status_code == 200
    r = a.hx.create_ppr("PR-E1", a.ent)
    assert r.status_code == 201, r.text


# ------------------------------------------------------------------ A-3 / A-4 / A-5
@pytest.mark.spec("D-38", "G-3", "D-30")
def test_verification_fulfils_and_cancellation_returns_to_planned(a: Assigned) -> None:
    d = a.draft("PR-A6", "30")
    assert a.confirm(d["id"]).status_code == 200
    ppr = a.hx.client.get(f"/api/pprs/{d['id']}", headers=a.store).json()
    assert a.verify(ppr).status_code == 200
    assert a.states() == {ENT: "FULFILLED", WARD: "FULFILLED"}
    a.cancel("PR-A6", d["id"])
    assert a.states() == {ENT: "PLANNED", WARD: "PLANNED"}
    last = a.audit("DEMAND_STATE_CHANGED")[-1]
    assert (last["state_before"], last["state_after"]) == ("FULFILLED", "PLANNED")
    assert last["cause"]["event"] == "CANCEL_FROM_HOSXP"


@pytest.mark.spec("D-38", "G-3", "S-15.2")
def test_unlocking_keeps_the_state_and_reconfirmation_replaces_the_coverage(
    a: Assigned,
) -> None:
    d = a.draft("PR-A7", "30")
    assert a.confirm(d["id"]).status_code == 200
    ppr = a.hx.client.get(f"/api/pprs/{d['id']}", headers=a.store).json()
    assert a.verify(ppr).status_code == 200
    # The PR changes in HOSxP (now 20): under review the verified version is still in effect.
    a.hx.put_pr("PR-A7", (TONER, "20", "100"), dept=STORE)
    r = a.hx.client.post(f"/api/pprs/{d['id']}/sync", headers=a.plan.h)
    assert r.status_code == 200, r.text
    ppr = a.hx.client.get(f"/api/pprs/{d['id']}", headers=a.store).json()
    assert ppr["state"] == "PR_CHANGED_REVIEW_REQUIRED"
    assert a.states() == {ENT: "FULFILLED", WARD: "FULFILLED"}
    r = a.hx.client.post(f"/api/pprs/{d['id']}/unlock", json={"reason": "fix"}, headers=a.plan.h)
    assert r.status_code == 200, r.text
    assert a.states() == {ENT: "FULFILLED", WARD: "FULFILLED"}  # A-5
    # The coverage form follows the PR as confirmation will read it: 20, for Ward A only.
    (item,) = a.hx.client.get(f"/api/pprs/{d['id']}/coverage", headers=a.store).json()
    assert item["drawn_qty"] == "20.0000"
    assert a.put_coverage(d["id"], ward="20").status_code == 200
    r = a.hx.client.put(
        f"/api/pprs/{d['id']}/allocations",
        json={"allocations": [{"budget_category_id": OFFICE, "amount": "2000"}]},
        headers=a.store,
    )
    assert r.status_code == 200, r.text
    r = a.confirm(d["id"])
    assert r.status_code == 200, r.text
    assert a.coverage(d["id"]) == {ENT: ("0", "0"), WARD: ("20.0000", "20.0000")}
    assert a.states() == {ENT: "PLANNED", WARD: "INCLUDED_IN_CENTRAL_PURCHASE"}
    covered = a.hx.client.get(f"/api/plan-items/{a.item_id}", headers=a.plan.h).json()["demands"]
    assert [(x["department_id"], x["covered"]) for x in covered] == [(ENT, "0"), (WARD, "20.0000")]


# ------------------------------------------------------------------ A-6
@pytest.mark.spec("D-38", "G-3", "D-37")
def test_demand_is_amended_never_below_what_pprs_in_effect_cover(a: Assigned) -> None:
    d = a.draft("PR-A8", "15")
    assert a.put_coverage(d["id"], ent="10", ward="5").status_code == 200
    assert a.confirm(d["id"]).status_code == 200  # not verified: still counts (A-6)
    r = a.amend((ENT, "9"))
    assert r.status_code == 409 and _codes(r) == {"BELOW_COVERED_DEMAND"}
    r = a.amend((WARD, "5"))  # the planned quantity stays 30: a warning only
    assert r.status_code == 201, r.text
    (change,) = [c for c in r.json()["changes"] if c["target"] == "DEMAND"]
    assert (change["before"]["demand_qty"], change["after"]["demand_qty"]) == ("20.0000", "5.0000")
    assert a.states() == {ENT: "INCLUDED_IN_CENTRAL_PURCHASE", WARD: "INCLUDED_IN_CENTRAL_PURCHASE"}
    assert a.amend((ENT, "12")).status_code == 201
    assert a.states()[ENT] == "PLANNED"  # 10 of 12 covered
    a.cancel("PR-A8", d["id"])
    assert a.amend((ENT, "0")).status_code == 201  # nothing in effect covers it any more
    assert a.states() == {ENT: "CANCELLED", WARD: "PLANNED"}
    cause = a.audit("DEMAND_STATE_CHANGED")[-1]["cause"]
    assert set(cause) == {"plan_amendment_id", "amendment_no"}


@pytest.mark.spec("D-38", "G-3", "S-22.1")
def test_a_demand_department_sees_its_own_demand_changes_only(a: Assigned) -> None:
    assert a.amend((WARD, "25")).status_code == 201
    ward = a.hx.h(a.hx.user("req_ward", Role.REQUESTER, department=WARD))
    seen = a.hx.client.get(f"/api/plan-years/{FY}/amendments", headers=ward).json()
    assert [c["target"] for c in seen[0]["changes"]] == ["DEMAND"]
    assert a.hx.client.get(f"/api/plan-years/{FY}/amendments", headers=a.ent).json() == []
    rows = a.hx.client.get("/api/reports/PLAN_AMENDMENT", headers=ward).json()["rows"]
    assert [(x["department"], x["qty_before"], x["qty_after"]) for x in rows] == [
        ("Mock Ward A", "20.0000", "25.0000")
    ]


@pytest.mark.spec("D-38", "G-3")
def test_zero_demand_is_only_ever_the_cancelled_state(a: Assigned) -> None:
    with pytest.raises(IntegrityError), a.hx.engine.begin() as c:
        c.execute(
            text("UPDATE plan_item_demand SET demand_qty = 0 WHERE plan_item_id = :i"),
            {"i": a.item_id},
        )


# ------------------------------------------------------------------ one demand, one purchase line
OR, IT = "MOCK-DEP-OR", "MOCK-DEP-IT"


def _new_assigned(a: Assigned, purchaser: str, dept: str) -> Any:
    return a.hx.client.post(
        f"/api/plan-years/{FY}/amendments",
        json={
            **DOC,
            "budgets": [
                {
                    "fund_source_id": "MOCK-FUND-1",
                    "budget_category_id": OFFICE,
                    "approved_amount": "4100",
                }
            ],
            "new_items": [
                {
                    "ref": "y",
                    "plan_type": "ASSIGNED",
                    "item_id": TONER,
                    "owner_department_id": purchaser,
                    "purchasing_department_id": purchaser,
                    "fund_source_id": "MOCK-FUND-1",
                    "budget_category_id": OFFICE,
                    "planned_qty": "1",
                    "estimated_unit_price": "100",
                    "planned_amount": "100",
                    "demands": [{"department_id": dept, "demand_qty": "1"}],
                }
            ],
        },
        headers=a.plan.h,
    )


@pytest.mark.spec("D-38", "G-3", "D-37")
def test_a_demand_is_never_claimed_by_two_assigned_purchases(a: Assigned) -> None:
    # Ward A's toner need is already bought by Mock Store; a second line (Mock OR) is refused.
    r = _new_assigned(a, OR, WARD)
    assert r.status_code == 409 and "DUPLICATE_DEMAND_CLAIM" in _codes(r)
    assert _new_assigned(a, OR, IT).status_code == 201  # another department's need: fine


@pytest.mark.spec("D-38", "G-3")
def test_a_plan_from_before_the_rule_cannot_cover_one_demand_twice(a: Assigned) -> None:
    # A plan made before the rule may hold Ward A's demand on two ASSIGNED toner items (Mock
    # Store's and Mock OR's). Neither purchase line may then claim it until the plan is amended.
    with a.hx.engine.begin() as c:
        y = c.execute(
            text(
                "INSERT INTO plan_item (plan_year_id, plan_budget_id, plan_type, item_source_id, "
                "item_code_snapshot, item_name_snapshot, unit_snapshot, "
                "owner_department_source_id, purchasing_department_source_id, planned_qty, "
                "estimated_unit_price, planned_amount) "
                "SELECT plan_year_id, plan_budget_id, 'ASSIGNED', item_source_id, "
                "item_code_snapshot, item_name_snapshot, unit_snapshot, :or_, :or_, 5, 0, 0 "
                "FROM plan_item WHERE id = :x RETURNING id"
            ),
            {"or_": OR, "x": a.item_id},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO plan_item_demand (plan_item_id, department_source_id, demand_qty, "
                "state) VALUES (:y, :w, 5, 'PLANNED')"
            ),
            {"y": y, "w": WARD},
        )
    d = a.draft("PR-D1", "30")  # Mock Store buys for ENT 10 and Ward A 20
    r = a.confirm(d["id"])
    assert r.status_code == 409 and "COVERAGE_DEMAND_CLAIMED_ELSEWHERE" in _codes(r)
    assert a.states() == {ENT: "PLANNED", WARD: "PLANNED"}  # nothing was claimed
    assert a.put_coverage(d["id"], ent="10").status_code == 200  # Ward A left out
    r = a.confirm(d["id"])  # 30 bought, 10 covered: still refused (the sum must match)
    assert r.status_code == 409 and _codes(r) == {"COVERAGE_SUM_MISMATCH"}


@pytest.mark.spec("D-38", "G-3", "S-12")
def test_buying_through_ones_own_assigned_item_does_not_bypass_open_demand(a: Assigned) -> None:
    # Ward A buys toner for Mock IT (its own ASSIGNED item) while its own toner need is still
    # open in Mock Store's purchase: A-1 still applies to the ward's PR.
    assert _new_assigned(a, WARD, IT).status_code == 201
    ward = a.hx.h(a.hx.user("req_ward", Role.REQUESTER, department=WARD))
    a.hx.put_pr("PR-W1", (TONER, "1", "100"), dept=WARD)
    r = a.hx.create_ppr("PR-W1", ward)
    assert r.status_code == 409 and "DEMAND_IN_CENTRAL_PURCHASE" in _codes(r)
    # Once the ward's own need is fulfilled, its purchase for Mock IT goes ahead.
    d = a.draft("PR-D2", "30")
    assert a.confirm(d["id"]).status_code == 200
    ppr = a.hx.client.get(f"/api/pprs/{d['id']}", headers=a.store).json()
    assert a.verify(ppr).status_code == 200
    r = a.hx.create_ppr("PR-W1", ward)
    assert r.status_code == 201, r.text


@pytest.mark.spec("AT-40.12.4", "D-38", "G-3", "D-37")
def test_a_fulfilled_demand_is_not_bought_again_without_an_amendment(a: Assigned) -> None:
    d = a.draft("PR-F1", "30")
    assert a.confirm(d["id"]).status_code == 200
    ppr = a.hx.client.get(f"/api/pprs/{d['id']}", headers=a.store).json()
    assert a.verify(ppr).status_code == 200
    assert a.states() == {ENT: "FULFILLED", WARD: "FULFILLED"}
    # The plan has 0 left anyway; give the item room so only the demand rule can refuse.
    r = a.hx.client.post(
        f"/api/plan-years/{FY}/amendments",
        json={
            **DOC,
            "budgets": [
                {
                    "fund_source_id": "MOCK-FUND-1",
                    "budget_category_id": OFFICE,
                    "approved_amount": "4500",
                }
            ],
            "items": [{"plan_item_id": a.item_id, "planned_qty": "35", "planned_amount": "3500"}],
        },
        headers=a.plan.h,
    )
    assert r.status_code == 201, r.text
    d2 = a.draft("PR-F2", "5")
    assert a.put_coverage(d2["id"], ent="5").status_code == 200
    r = a.confirm(d2["id"])
    assert r.status_code == 409 and _codes(r) == {"COVERAGE_EXCEEDS_NEED"}
    # An approved change (more demand) lets it be bought.
    assert a.amend((ENT, "15")).status_code == 201
    assert a.states()[ENT] == "PLANNED"
    assert a.confirm(d2["id"]).status_code == 200
    assert a.states()[ENT] == "INCLUDED_IN_CENTRAL_PURCHASE"
