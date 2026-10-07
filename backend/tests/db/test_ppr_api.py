"""Wave 4: PPR draft, allocation, confirm/lock, ledger, numbering, unlock/reconfirm, print.

Spec §11.3, §14, §15, §17, §19, §28, §30, §35, §36, §40.1, §40.3, §40.4, §40.5;
D-03 ... D-06, D-09, D-16 ... D-20.
"""

from __future__ import annotations

import threading
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from api_harness import FY, Harness, Plan, check_pr
from ppr.application import ppr_services as svc
from ppr.application.errors import ConflictError
from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR
from ppr.domain.roles import Role
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.integration.hosxp.status import HosxpPrStatus
from ppr.ports import PprScope

TONER, PAPER, GLOVE = "MOCK-ITEM-TONER", "MOCK-ITEM-PAPER", "MOCK-ITEM-GLOVE"
OFFICE, MED, MAT = "MOCK-CAT-OFFICE", "MOCK-CAT-MED", "MOCK-CAT-MAT"


class World:
    """FY2570 ACTIVE plan for Mock ENT, fund 1:
    OFFICE line 1500 (toner 10 @ 1000, paper 10 @ 500), MED line 300 (gloves 10 @ 300),
    and a fund-2 OFFICE line (for the cross-fund check)."""

    def __init__(self, hx: Harness) -> None:
        self.hx = hx
        self.plan = p = Plan(hx)
        p.sync()
        p.year()
        office = p.budget(OFFICE, "1500").json()["id"]
        med = p.budget(MED, "300").json()["id"]
        other_fund = p.budget(MAT, "100", fund="MOCK-FUND-2").json()["id"]
        assert p.item(office, "1000").status_code == 201
        assert p.item(office, "500", item_id=PAPER).status_code == 201
        assert p.item(med, "300", item_id=GLOVE).status_code == 201
        assert (
            p.item(other_fund, "100", item_id=PAPER, owner_department_id="MOCK-DEP-OR").status_code
            == 201
        )
        assert p.move("APPROVED").status_code == 200
        assert p.move("ACTIVE").status_code == 200
        self.req = hx.h(hx.user("req_ent", Role.REQUESTER, department="MOCK-DEP-ENT"))
        items = hx.client.get(f"/api/plan-years/{FY}/items", headers=p.h).json()
        self.item_ids = {(i["item_id"], i["fund_source_id"]): i["id"] for i in items}

    def pid(self, item: str) -> int:
        return int(self.item_ids[(item, "MOCK-FUND-1")])

    def balance(self, item: str) -> tuple[str, str]:
        b = self.hx.client.get(f"/api/plan-items/{self.pid(item)}/balance", headers=self.plan.h)
        j = b.json()
        return j["remaining_qty"], j["remaining_amount"]

    def draft(self, pr_no: str, *lines: tuple[str, str, str], **kw: Any) -> dict[str, Any]:
        self.hx.put_pr(pr_no, *lines, **kw)
        r = self.hx.create_ppr(pr_no, self.req)
        assert r.status_code == 201, r.text
        return dict(r.json())

    def confirm(self, ppr_id: int, headers: dict[str, str] | None = None) -> Any:
        return self.hx.client.post(f"/api/pprs/{ppr_id}/confirm", headers=headers or self.req)

    def allocate(self, ppr_id: int, allocs: list[tuple[str, str]], ref: str | None = None) -> Any:
        return self.hx.client.put(
            f"/api/pprs/{ppr_id}/allocations",
            json={
                "allocations": [{"budget_category_id": c, "amount": a} for c, a in allocs],
                "adjustment_reference": ref,
            },
            headers=self.req,
        )

    def count(self, table: str) -> int:
        with self.hx.engine.connect() as c:
            return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())


@pytest.fixture
def w(db: Engine) -> World:
    return World(Harness(db))


def _codes(detail: dict[str, Any]) -> set[str]:
    return {d["code"] for d in detail.get("details", [])}


# ------------------------------------------------------------------ draft (D-05, D-19)
@pytest.mark.spec("S-14.2", "D-05", "D-19", "AT-40.4.1", "AT-40.3.6")
def test_draft_is_unnumbered_and_gets_the_default_allocation(w: World) -> None:
    ledger_before = w.count("plan_ledger")
    d = w.draft("PR-1", (TONER, "4", "100"), (PAPER, "2", "50"))
    assert (d["state"], d["ppr_number"], d["version"]) == ("DRAFT", None, 0)
    assert d["required_amount"] == "500.00"
    assert d["allocations"] == [{"budget_category_id": OFFICE, "amount": "500.00"}]
    assert [(i["item_id"], i["plan_item_id"]) for i in d["items"]] == [
        (TONER, w.pid(TONER)),
        (PAPER, w.pid(PAPER)),
    ]
    assert w.count("plan_ledger") == ledger_before  # a draft holds no plan usage
    assert w.count("ppr_binding") == 0  # and does not bind the PR (D-05)
    assert ("PPR_DRAFT_CREATED", "ppr", str(d["id"])) in w.hx.audit_actions()


@pytest.mark.spec("AT-40.1.8", "S-12", "S-14.1")
def test_ineligible_pr_creates_no_ppr_record(w: World) -> None:
    w.hx.put_pr("PR-X", (TONER, "1", "100"), ("MOCK-ITEM-OLD", "1", "10"))
    r = w.hx.create_ppr("PR-X", w.req)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PR_NOT_ELIGIBLE"
    assert "NOT_IN_PLAN" in _codes(r.json()["detail"])
    assert w.count("ppr") == 0


@pytest.mark.spec("AT-40.1.5", "AT-40.1.6", "D-05")
def test_one_working_draft_per_pr_and_discard_frees_it(w: World) -> None:
    d = w.draft("PR-D", (TONER, "1", "100"))
    again = w.hx.create_ppr("PR-D", w.req)
    assert again.status_code == 409
    assert "DRAFT_ALREADY_EXISTS" in _codes(again.json()["detail"])
    assert w.hx.client.post(f"/api/pprs/{d['id']}/discard", headers=w.req).status_code == 204
    assert ("PPR_DRAFT_DISCARDED", "ppr", str(d["id"])) in w.hx.audit_actions()
    new = w.hx.create_ppr("PR-D", w.req)
    assert new.status_code == 201
    assert w.hx.client.get(f"/api/pprs/{d['id']}", headers=w.req).status_code == 404


# ------------------------------------------------------------------ confirm (§14.3)
@pytest.mark.spec("S-14.3", "D-06", "D-03", "AT-40.4.2", "AT-40.5.1", "AT-40.5.4", "S-18.3", "S-36")
def test_confirm_numbers_locks_binds_posts_and_snapshots(w: World) -> None:
    d = w.draft("PR-1", (TONER, "4", "100"), (PAPER, "2", "50"))
    r = w.confirm(d["id"])
    assert r.status_code == 200, r.text
    c = r.json()
    assert (c["state"], c["ppr_number"], c["version"]) == (
        "CONFIRMED_LOCKED",
        f"PPR-{FY}-000001",
        1,
    )
    assert w.balance(TONER) == ("6.0000", "600.00")
    assert w.balance(PAPER) == ("8.0000", "400.00")
    with w.hx.engine.connect() as conn:
        assert conn.execute(text("SELECT pr_no FROM ppr_binding")).scalar_one() == "PR-1"
    [v] = w.hx.client.get(f"/api/pprs/{d['id']}/versions", headers=w.req).json()
    snap = v["snapshot"]
    assert (snap["ppr_number"], snap["version"], snap["validation"]) == (
        c["ppr_number"],
        1,
        "PASSED",
    )
    assert snap["pr"]["total_amount"] == "500.00"
    toner = snap["items"][0]
    assert (toner["remaining_qty_before"], toner["remaining_qty_after"]) == ("10.0000", "6.0000")
    assert len(v["document_code"]) == 19
    assert ("PPR_CONFIRMED", "ppr", str(d["id"])) in w.hx.audit_actions()
    # numbering continues per fiscal year
    d2 = w.draft("PR-2", (PAPER, "1", "50"))
    assert w.confirm(d2["id"]).json()["ppr_number"] == f"PPR-{FY}-000002"


@pytest.mark.spec("AT-40.1.3", "AT-40.1.4", "D-03", "S-11.3")
def test_a_confirmed_pr_can_never_get_another_ppr(w: World) -> None:
    d = w.draft("PR-1", (TONER, "1", "100"))
    assert w.confirm(d["id"]).status_code == 200
    r = w.hx.create_ppr("PR-1", w.req)
    assert r.status_code == 409
    assert "PR_ALREADY_BOUND" in _codes(r.json()["detail"])


@pytest.mark.spec("AT-40.4.3", "S-15.1")
def test_locked_ppr_cannot_be_edited_or_discarded(w: World) -> None:
    d = w.draft("PR-1", (TONER, "1", "100"))
    assert w.confirm(d["id"]).status_code == 200
    assert w.allocate(d["id"], [(OFFICE, "100")]).status_code == 409
    assert w.hx.client.post(f"/api/pprs/{d['id']}/discard", headers=w.req).status_code == 409


# ------------------------------------------------------------------ allocation (D-04, D-19)
@pytest.mark.spec(
    "D-04",
    "D-19",
    "AT-40.3.1",
    "AT-40.3.2",
    "AT-40.3.3",
    "AT-40.3.4",
    "AT-40.3.5",
    "AT-40.3.7",
    "AT-40.3.8",
)
def test_allocation_rules(w: World) -> None:
    d = w.draft("PR-A", (TONER, "5", "100"))  # required 500
    pid = d["id"]
    eligible = w.hx.client.get(f"/api/pprs/{pid}/eligible-categories", headers=w.req).json()
    assert eligible == [MED, OFFICE]  # same FY + fund 1 only; fund-2 MAT is not offered
    # not eligible / cross-fund / duplicate -> rejected when saved
    assert w.allocate(pid, [(MAT, "500")]).status_code == 422
    dup = w.allocate(pid, [(OFFICE, "250"), (OFFICE, "250")])
    assert dup.status_code == 422
    assert "ALLOCATION_DUPLICATE_CATEGORY" in _codes(dup.json()["detail"])
    # unbalanced total may be saved but blocks confirmation
    assert w.allocate(pid, [(OFFICE, "300"), (MED, "100")], "memo 12/2570").status_code == 200
    r = w.confirm(pid)
    assert r.status_code == 409 and "ALLOCATION_SUM_MISMATCH" in _codes(r.json()["detail"])
    # two categories without a reference -> blocked (D-19)
    assert w.allocate(pid, [(OFFICE, "300"), (MED, "200")]).status_code == 200
    r = w.confirm(pid)
    assert r.status_code == 409
    assert _codes(r.json()["detail"]) == {"ADJUSTMENT_REFERENCE_REQUIRED"}
    # with the reference -> confirmed; the ledger reflects items only (D-04)
    assert w.allocate(pid, [(OFFICE, "300"), (MED, "200")], "  memo 12/2570 ").status_code == 200
    c = w.confirm(pid)
    assert c.status_code == 200, c.text
    assert c.json()["multi_category"] is True
    assert c.json()["adjustment_reference"] == "memo 12/2570"
    assert w.balance(TONER) == ("5.0000", "500.00")
    assert w.balance(GLOVE) == ("10.0000", "300.00")  # MED allocation posts nothing
    html = w.hx.client.get(f"/api/pprs/{pid}/print", headers=w.req).text
    assert "มากกว่า 1 หมวดงบ" in html and "memo 12/2570" in html


# ------------------------------------------------------------------ live re-check at confirm
@pytest.mark.spec("D-18", "S-14.3")
def test_confirm_uses_the_server_date_of_confirmation(w: World) -> None:
    w.hx.today = date(2027, 9, 30)  # last day of FY2570
    d = w.draft("PR-EDGE", (TONER, "1", "100"))
    w.hx.today = date(2027, 10, 1)  # confirmed on the first day of FY2571
    r = w.confirm(d["id"])
    assert r.status_code == 409
    assert "PLAN_YEAR_EXPIRED" in _codes(r.json()["detail"])
    assert w.hx.client.get(f"/api/pprs/{d['id']}", headers=w.req).json()["state"] == "DRAFT"
    assert w.count("ppr_binding") == 0 and w.count("ppr_version") == 0


@pytest.mark.spec("S-14.3", "S-16")
def test_pr_changed_since_the_draft_cannot_be_confirmed(w: World) -> None:
    d = w.draft("PR-C", (TONER, "4", "100"))
    w.hx.put_pr("PR-C", (TONER, "5", "100"))
    r = w.confirm(d["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PR_CHANGED_SINCE_DRAFT"


@pytest.mark.spec("S-26", "AT-40.6.3")
def test_hosxp_down_at_confirmation_writes_nothing(w: World) -> None:
    d = w.draft("PR-1", (TONER, "1", "100"))
    before = (w.count("plan_ledger"), w.count("ppr_version"), w.count("audit_log"))
    w.hx.gateway.available = False
    r = w.confirm(d["id"])
    assert r.status_code == 503
    assert (w.count("plan_ledger"), w.count("ppr_version"), w.count("audit_log")) == before


@pytest.mark.spec("D-21", "D-10")
def test_pr_cancelled_in_hosxp_before_confirmation_is_refused(w: World) -> None:
    d = w.draft("PR-1", (TONER, "1", "100"))
    w.hx.put_pr("PR-1", (TONER, "1", "100"), status=HosxpPrStatus.CANCELLED)
    r = w.confirm(d["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PR_CHANGED_SINCE_DRAFT"
    [c] = r.json()["detail"]["details"]
    assert (c["field"], c["after"], c["critical"]) == ("status", "CANCELLED", True)


# ------------------------------------------------------------------ who may act (D-09, D-41, §22)
@pytest.mark.spec("D-09", "D-41", "S-22.1", "S-22.9", "AT-40.15.3", "AT-40.15.4")
def test_only_the_requester_who_created_a_ppr_confirms_it(w: World) -> None:
    d = w.draft("PR-1", (TONER, "1", "100"))
    officer = w.confirm(d["id"], w.plan.h)
    assert officer.status_code == 403  # Plan Officer never confirms (D-09)
    # Another requester - even of the same department - neither sees nor confirms it (D-41).
    same = w.hx.h(w.hx.user("req_ent2", Role.REQUESTER, department="MOCK-DEP-ENT"))
    assert w.confirm(d["id"], same).status_code == 404
    assert w.hx.client.get(f"/api/pprs/{d['id']}", headers=same).status_code == 404
    both = w.hx.h(w.hx.user("req_or2", Role.REQUESTER, Role.PLAN_OFFICER, department="MOCK-DEP-OR"))
    r = w.confirm(d["id"], both)  # sees it (Plan Officer) but did not create it
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_THE_CREATOR"
    for path, body in (
        (f"/api/pprs/{d['id']}/allocations", {"allocations": []}),
        (f"/api/pprs/{d['id']}/coverage", {"coverage": []}),
    ):
        r = w.hx.client.put(path, json=body, headers=both)
        assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_THE_CREATOR", path
    finance = w.hx.h(w.hx.user("fin", Role.FINANCE, department="MOCK-DEP-IT"))
    assert w.hx.client.get(f"/api/pprs/{d['id']}", headers=finance).status_code == 200
    assert w.hx.client.get("/api/pprs", headers=same).json() == []
    assert [p["id"] for p in w.hx.client.get("/api/pprs", headers=w.req).json()] == [d["id"]]
    assert w.confirm(d["id"]).status_code == 200  # the creator does
    # After an unlock, reconfirming is the creator's too.
    r = w.hx.client.post(f"/api/pprs/{d['id']}/unlock", json={"reason": "x"}, headers=w.plan.h)
    assert r.status_code == 200
    r = w.confirm(d["id"], both)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "NOT_THE_CREATOR"
    assert w.confirm(d["id"]).status_code == 200


@pytest.mark.spec("D-41", "D-05", "S-36", "AT-40.15.3")
def test_an_abandoned_draft_is_discarded_by_planning_with_a_reason(w: World) -> None:
    d = w.draft("PR-GONE", (TONER, "1", "100"))
    other = w.hx.h(w.hx.user("req_or", Role.REQUESTER, department="MOCK-DEP-OR"))
    w.hx.put_pr("PR-GONE", (TONER, "1", "100"))
    blocked = w.hx.create_ppr("PR-GONE", other)
    assert blocked.status_code == 409  # the draft holds the PR (D-05)
    path = f"/api/pprs/{d['id']}/discard"
    assert w.hx.client.post(path, headers=other).status_code == 404  # not theirs, not visible
    finance = w.hx.h(w.hx.user("fin", Role.FINANCE, department="MOCK-DEP-IT"))
    r = w.hx.client.post(path, json={"reason": "x"}, headers=finance)
    assert r.status_code == 403  # neither the creator nor Planning / Admin
    r = w.hx.client.post(path, headers=w.plan.h)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "REASON_REQUIRED"
    r = w.hx.client.post(path, json={"reason": "requester left the hospital"}, headers=w.plan.h)
    assert r.status_code == 204, r.text
    with w.hx.engine.connect() as c:
        row = c.execute(
            text(
                "SELECT reason, after FROM audit_log WHERE action = 'PPR_DRAFT_DISCARDED' "
                "AND entity_id = :i"
            ),
            {"i": str(d["id"])},
        ).one()
    assert row.reason == "requester left the hospital" and row.after["by_governance"]
    again = w.hx.create_ppr("PR-GONE", other)
    assert again.status_code == 201, again.text  # the PR is free again
    # Only a draft: a confirmed PPR is never discarded.
    assert (
        w.hx.client.post(f"/api/pprs/{again.json()['id']}/confirm", headers=other).status_code
        == 200
    )
    r = w.hx.client.post(
        f"/api/pprs/{again.json()['id']}/discard", json={"reason": "x"}, headers=w.plan.h
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_A_DRAFT"
    # A requester who is also a Plan Officer discards their OWN draft as its creator.
    both = w.hx.h(w.hx.user("req_po", Role.REQUESTER, Role.PLAN_OFFICER, department="MOCK-DEP-OR"))
    w.hx.put_pr("PR-OWN", (TONER, "1", "100"))
    own = w.hx.create_ppr("PR-OWN", both)
    assert own.status_code == 201, own.text
    gone = w.hx.client.post(f"/api/pprs/{own.json()['id']}/discard", headers=both)
    assert gone.status_code == 204  # no reason needed: it is theirs


@pytest.mark.spec("D-41", "S-22.1", "AT-40.15.2")
def test_a_requester_without_a_department_makes_a_ppr_for_any_departments_pr(w: World) -> None:
    nodept = w.hx.h(w.hx.user("req_none", Role.REQUESTER, department=None))
    w.hx.put_pr("PR-ND", (TONER, "2", "100"))  # Mock ENT's PR
    check = check_pr(w.hx.client, "PR-ND", nodept)
    assert check.status_code == 200 and check.json()["eligible"] is True
    r = w.hx.create_ppr("PR-ND", nodept)
    assert r.status_code == 201, r.text
    ppr = r.json()
    assert ppr["department_id"] == "MOCK-DEP-ENT"  # the PR's department, not the user's
    c = w.hx.client.post(f"/api/pprs/{ppr['id']}/confirm", headers=nodept)
    assert c.status_code == 200, c.text
    assert w.balance(TONER) == ("8.0000", "800.00")  # drawn on Mock ENT's plan
    # The creator sees their PPR; plan data needs a department (none here).
    assert [p["id"] for p in w.hx.client.get("/api/pprs", headers=nodept).json()] == [ppr["id"]]
    assert w.hx.client.get(f"/api/plan-years/{FY}/items", headers=nodept).json() == []
    # The Mock ENT requester did not create it: not visible to them.
    assert w.hx.client.get(f"/api/pprs/{ppr['id']}", headers=w.req).status_code == 404


# ------------------------------------------------------------------ unlock / reconfirm (§15)
@pytest.mark.spec(
    "S-15.2", "S-15.3", "AT-40.4.4", "AT-40.4.5", "AT-40.4.6", "AT-40.4.7", "D-09", "S-35"
)
def test_unlock_then_reconfirm_creates_a_new_version(w: World) -> None:
    d = w.draft("PR-U", (TONER, "4", "100"), (PAPER, "2", "50"))
    pid = d["id"]
    assert w.confirm(pid).status_code == 200
    proc = w.hx.h(w.hx.user("proc", Role.PROCUREMENT, department="MOCK-DEP-ST"))
    assert (
        w.hx.client.post(f"/api/pprs/{pid}/unlock", json={"reason": "x"}, headers=proc).status_code
        == 403
    )
    no_reason = w.hx.client.post(f"/api/pprs/{pid}/unlock", json={}, headers=w.plan.h)
    assert no_reason.status_code == 422
    u = w.hx.client.post(
        f"/api/pprs/{pid}/unlock", json={"reason": "PR quantity corrected"}, headers=w.plan.h
    )
    assert u.status_code == 200 and u.json()["state"] == "UNLOCKED_FOR_REVISION"
    assert w.balance(TONER) == ("6.0000", "600.00")  # usage stays held while unlocked
    # The PR is revised in HOSxP; the requester updates the allocation and reconfirms.
    w.hx.put_pr("PR-U", (TONER, "3", "100"), (PAPER, "2", "50"))
    r = w.confirm(pid)
    assert r.status_code == 409 and "ALLOCATION_SUM_MISMATCH" in _codes(r.json()["detail"])
    assert w.allocate(pid, [(OFFICE, "400")]).status_code == 200
    r = w.confirm(pid)
    assert r.status_code == 200, r.text
    assert (r.json()["version"], r.json()["ppr_number"]) == (2, f"PPR-{FY}-000001")
    assert w.balance(TONER) == ("7.0000", "700.00")  # 4 released, 3 used
    assert w.balance(PAPER) == ("8.0000", "400.00")
    vs = w.hx.client.get(f"/api/pprs/{pid}/versions", headers=w.req).json()
    assert [v["version"] for v in vs] == [1, 2]  # old version preserved
    assert vs[0]["snapshot"]["items"][0]["qty"] == "4"
    old = w.hx.client.get(f"/api/pprs/{pid}/print?version=1", headers=w.req).text
    assert "ไม่ใช่ฉบับปัจจุบัน" in old
    actions = [a for a, _, e in w.hx.audit_actions() if e == str(pid)]
    assert actions[-4:] == [
        "PPR_UNLOCKED",
        "PPR_ALLOCATION_UPDATED",
        "PPR_RECONFIRMED",
        "PPR_PRINT_VIEWED",  # Wave 5B: printing is timeline evidence (§33)
    ]


# ------------------------------------------------------------------ concurrency (§19)
@pytest.mark.spec("S-19", "AT-40.5.5", "S-30")
def test_concurrent_confirmations_cannot_oversubscribe(w: World) -> None:
    a = w.draft("PR-RACE-A", (TONER, "10", "100"))
    b = w.draft("PR-RACE-B", (TONER, "10", "100"))
    with unit_of_work(w.hx.engine) as uow:
        user = uow.users.get_by_username("req_ent")
    assert user is not None
    caller = svc.Caller(user.id, frozenset(user.roles), PprScope(user.id))
    barrier = threading.Barrier(2)
    outcomes: dict[int, str] = {}

    class SlowGateway:
        """Both confirmations fetch the PR at the same moment, then race for the locks."""

        def get_pr(self, pr_no: str) -> Any:
            h = w.hx.gateway.get_pr(pr_no)
            barrier.wait(timeout=10)
            return h

        def get_pr_items(self, pr_no: str) -> Any:
            return w.hx.gateway.get_pr_items(pr_no)

    def run(ppr_id: int) -> None:
        try:
            with unit_of_work(w.hx.engine) as uow:
                svc.confirm(uow, SlowGateway(), DEFAULT_FISCAL_YEAR, w.hx.today, caller, ppr_id)  # type: ignore[arg-type]
            outcomes[ppr_id] = "OK"
        except ConflictError as exc:
            outcomes[ppr_id] = exc.code

    threads = [threading.Thread(target=run, args=(i,)) for i in (a["id"], b["id"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sorted(outcomes.values()) == ["OK", "PR_NOT_ELIGIBLE"]
    assert w.balance(TONER) == ("0.0000", "0.00")  # never negative


# ------------------------------------------------------------------ evidence is append-only
@pytest.mark.spec("S-15.3", "D-03", "S-36")
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE ppr_version SET document_code = 'X'",
        "DELETE FROM ppr_version",
        "DELETE FROM ppr_binding",
        "UPDATE ppr_binding SET pr_no = 'OTHER'",
        "TRUNCATE ppr_binding CASCADE",
    ],
)
def test_versions_and_bindings_cannot_be_changed(w: World, sql: str) -> None:
    d = w.draft("PR-1", (TONER, "1", "100"))
    assert w.confirm(d["id"]).status_code == 200
    with pytest.raises(DBAPIError), w.hx.engine.begin() as conn:
        conn.execute(text(sql))


# ------------------------------------------------------------------ print (D-20, §35)
@pytest.mark.spec("D-20", "S-35")
def test_printed_ppr_shows_the_confirmed_evidence(w: World) -> None:
    d = w.draft("PR-P", (TONER, "4", "100"))
    assert w.hx.client.get(f"/api/pprs/{d['id']}/print", headers=w.req).status_code == 409
    c = w.confirm(d["id"]).json()
    r = w.hx.client.get(f"/api/pprs/{d['id']}/print", headers=w.req)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    code = w.hx.client.get(f"/api/pprs/{d['id']}/versions", headers=w.req).json()[0][
        "document_code"
    ]
    for expected in (
        c["ppr_number"],
        "เวอร์ชัน <b>1</b>",
        "PR-P",
        str(FY),
        "Mock ENT",
        "Mock fund 1",
        "Office supplies",
        "400.00",
        "6",  # remaining qty after
        code,
        "ผู้ขอใช้แผน",
        "หัวหน้าเจ้าหน้าที่พัสดุ",
        "@page { size: A4",
    ):
        assert expected in html, expected
    assert "ไม่ใช่ฉบับปัจจุบัน" not in html
    assert Decimal(c["required_amount"]) == Decimal("400.00")


# ------------------------------------------------------------------ review regressions
@pytest.mark.spec("D-09", "D-04", "D-19", "S-14.3")
@pytest.mark.parametrize(
    "change",
    [
        {"dept": "MOCK-DEP-OR"},  # another department's plan
        {"fund": "MOCK-FUND-2"},  # another fund source
        {"category": MED},  # the PR's own budget category
        {"price": "90"},  # unit price shown in the draft
    ],
)
def test_any_pr_change_after_the_draft_blocks_confirmation(
    w: World, change: dict[str, str]
) -> None:
    d = w.draft("PR-H", (PAPER, "1", "100"))
    price = change.get("price", "100")
    w.hx.put_pr(
        "PR-H",
        (PAPER, "1", price),
        dept=change.get("dept", "MOCK-DEP-ENT"),
        fund=change.get("fund", "MOCK-FUND-1"),
    )
    if "category" in change:
        h = w.hx.gateway.data.prs["PR-H"]
        w.hx.gateway.data.prs["PR-H"] = h.model_copy(
            update={"budget_category_id": change["category"]}
        )
    before = (w.count("plan_ledger"), w.count("ppr_binding"))
    r = w.confirm(d["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PR_CHANGED_SINCE_DRAFT"
    assert (w.count("plan_ledger"), w.count("ppr_binding")) == before


def test_same_lines_in_a_different_order_still_confirm(w: World) -> None:
    d = w.draft("PR-O", (TONER, "1", "100"), (PAPER, "1", "50"))
    h = w.hx.gateway.data.prs["PR-O"]
    items = list(reversed(w.hx.gateway.data.pr_items["PR-O"]))
    w.hx.gateway.replace_pr(h, items)
    assert w.confirm(d["id"]).status_code == 200


def _unlock(w: World, ppr_id: int) -> None:
    r = w.hx.client.post(f"/api/pprs/{ppr_id}/unlock", json={"reason": "revise"}, headers=w.plan.h)
    assert r.status_code == 200, r.text


@pytest.mark.spec("D-22", "D-09", "S-15.3")
@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"dept": "MOCK-DEP-OR"}, "department_id"),
        ({"fund": "MOCK-FUND-2"}, "fund_source_id"),
        ({"budget_year": 2571}, "fiscal_year"),
    ],
)
def test_reconfirm_refuses_identity_changes_and_asks_for_governance_review(
    w: World, change: dict[str, Any], field: str
) -> None:
    d = w.draft("PR-M", (PAPER, "1", "50"))
    assert w.confirm(d["id"]).status_code == 200
    _unlock(w, d["id"])
    w.hx.put_pr("PR-M", (PAPER, "1", "50"), **change)
    before = (w.count("plan_ledger"), w.count("ppr_version"), w.count("ppr"))
    r = w.confirm(d["id"])
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "PR_IDENTITY_CHANGED" and "governance review" in detail["message"]
    assert [c["field"] for c in detail["details"]] == [field]
    # nothing cancelled, replaced or re-posted; the PPR waits for governance review
    assert (w.count("plan_ledger"), w.count("ppr_version"), w.count("ppr")) == before
    assert w.hx.client.get(f"/api/pprs/{d['id']}", headers=w.req).json()["state"] == (
        "UNLOCKED_FOR_REVISION"
    )
    assert w.balance(PAPER) == ("9.0000", "450.00")  # original usage still held


@pytest.mark.spec("D-21", "S-16")
def test_non_critical_changes_since_the_draft_are_recorded_not_blocking(w: World) -> None:
    d = w.draft("PR-N", (TONER, "4", "100"))
    h = w.hx.gateway.data.prs["PR-N"]
    [item] = w.hx.gateway.data.pr_items["PR-N"]
    w.hx.gateway.replace_pr(
        h.model_copy(
            update={
                "requester_name": "Mock requester (renamed)",
                "pr_date": date(2026, 10, 16),
                "status": HosxpPrStatus.APPROVED,
            }
        ),
        [
            item.model_copy(
                update={
                    "item_name": "Mock toner (HOSxP label)",
                    "unit_price": Decimal("100.00000001"),  # qty and amount unchanged
                }
            )
        ],
    )
    r = w.confirm(d["id"])
    assert r.status_code == 200, r.text
    [v] = w.hx.client.get(f"/api/pprs/{d['id']}/versions", headers=w.req).json()
    snap = v["snapshot"]
    changed = {(c["field"], c["critical"]) for c in snap["pr_changes"]}
    assert changed == {
        ("requester_name", False),
        ("pr_date", False),
        ("status", False),
        ("item_name", False),
        ("unit_price", False),
    }
    # the confirmation carries the LATEST live values, never the stale draft
    assert snap["pr"]["requester_name"] == "Mock requester (renamed)"
    assert snap["items"][0]["item_name"] == "Mock toner (HOSxP label)"
    assert r.json()["items"][0]["unit_price"] == "100.00000001"
    assert r.json()["hosxp_pr_status"] == "APPROVED"
    with w.hx.engine.connect() as conn:
        after = conn.execute(
            text("SELECT after FROM audit_log WHERE action = 'PPR_CONFIRMED'")
        ).scalar_one()
    assert len(after["pr_changes"]) == 5


@pytest.mark.spec("D-22", "D-19", "S-15.3")
def test_reconfirm_accepts_a_changed_budget_category_after_full_revalidation(w: World) -> None:
    d = w.draft("PR-K", (TONER, "2", "100"))
    assert w.confirm(d["id"]).status_code == 200
    _unlock(w, d["id"])
    h = w.hx.gateway.data.prs["PR-K"]
    w.hx.gateway.data.prs["PR-K"] = h.model_copy(update={"budget_category_id": MED})
    assert w.allocate(d["id"], [(MED, "200")]).status_code == 200
    r = w.confirm(d["id"])
    assert r.status_code == 200, r.text
    assert (r.json()["version"], r.json()["pr_budget_category_id"]) == (2, MED)
    v2 = w.hx.client.get(f"/api/pprs/{d['id']}/versions", headers=w.req).json()[-1]
    assert {(c["field"], c["critical"]) for c in v2["snapshot"]["pr_changes"]} == {
        ("budget_category_id", True)
    }


@pytest.mark.spec("D-23")
def test_unit_price_beyond_eight_decimals_is_refused_not_rounded(w: World) -> None:
    w.hx.put_pr("PR-P9", (TONER, "1", "100.000000001"), total=Decimal("100.00"))
    items = w.hx.gateway.data.pr_items["PR-P9"]
    w.hx.gateway.data.pr_items["PR-P9"] = [
        i.model_copy(update={"amount": Decimal("100.00")}) for i in items
    ]
    r = w.hx.create_ppr("PR-P9", w.req)
    assert r.status_code == 409
    [v] = [x for x in r.json()["detail"]["details"] if x["code"] == "PR_VALUE_PRECISION"]
    assert v["subject"] == "PR-P9-1" and "unit price" in v["message"]
    assert w.count("ppr") == 0


@pytest.mark.spec("D-23")
def test_unit_price_with_many_decimals_can_be_confirmed(w: World) -> None:
    # HOSxP shows prices with up to 8 decimals; they are stored at full precision.
    d = w.draft("PR-DEC", (TONER, "3", "33.335"))
    r = w.confirm(d["id"])
    assert r.status_code == 200, r.text
    assert r.json()["items"][0]["unit_price"] == "33.33500000"
