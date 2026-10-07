"""Wave 12A-2 (D-43): the requester chooses the plan row of every PR line on every PPR.

Spec §40.18; D-43 (supersedes the D-16 automatic match and the D-42 binding).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Engine, text

from api_harness import FY, Harness, Plan
from ppr.domain.roles import Role

TONER = "MOCK-ITEM-TONER"  # counted in "box" in the mock HOSxP
OFFICE = "MOCK-CAT-OFFICE"


class World:
    """FY2570 ACTIVE plan for Mock ENT, fund 1, OFFICE 1800: a toner row carrying the HOSxP
    item (10 box @ 100), a row without one counted in boxes (10 @ 50) and one in reams."""

    def __init__(self, hx: Harness) -> None:
        self.hx = hx
        self.plan = p = Plan(hx)
        p.sync()
        p.year()
        office = p.budget(OFFICE, "1800").json()["id"]
        self.toner = p.item(office, "1000").json()["id"]
        r = p.item(office, "500", item_id=None, item_name="หมึกพิมพ์ทั่วไป", unit="box")
        assert r.status_code == 201, r.text
        self.generic = r.json()["id"]
        r = p.item(office, "300", item_id=None, item_name="กระดาษ", unit="รีม")
        assert r.status_code == 201, r.text
        self.paper = r.json()["id"]
        assert p.move("APPROVED").status_code == 200
        assert p.move("ACTIVE").status_code == 200
        self.req = hx.h(hx.user("req_ent", Role.REQUESTER))

    def pr(self, pr_no: str, qty: str = "2", **kw: Any) -> None:
        self.hx.put_pr(pr_no, (TONER, qty, "100"), **kw)

    def create(self, pr_no: str, choice: int | None, headers: dict[str, str] | None = None) -> Any:
        choices = [] if choice is None else [{"pr_item_id": f"{pr_no}-1", "plan_item_id": choice}]
        return self.hx.client.post(
            "/api/pprs", json={"pr_no": pr_no, "choices": choices}, headers=headers or self.req
        )

    def put(
        self, ppr_id: int, choices: dict[str, int], headers: dict[str, str] | None = None
    ) -> Any:
        return self.hx.client.put(
            f"/api/pprs/{ppr_id}/choices",
            json={"choices": [{"pr_item_id": k, "plan_item_id": v} for k, v in choices.items()]},
            headers=headers or self.req,
        )

    def confirm(self, ppr_id: int) -> Any:
        return self.hx.client.post(f"/api/pprs/{ppr_id}/confirm", headers=self.req)

    def balance(self, plan_item_id: int) -> tuple[str, str]:
        j = self.hx.client.get(f"/api/plan-items/{plan_item_id}/balance", headers=self.plan.h)
        return j.json()["remaining_qty"], j.json()["remaining_amount"]


@pytest.fixture
def w(db: Engine) -> World:
    return World(Harness(db))


def _codes(r: Any) -> set[str]:
    return {d["code"] for d in r.json()["detail"].get("details", [])}


@pytest.mark.spec("D-43", "AT-40.18.1", "AT-40.18.2", "AT-40.18.4")
def test_a_draft_needs_a_chosen_row_for_every_line_and_usage_goes_to_it(w: World) -> None:
    w.pr("PR-1")
    check = w.hx.client.get("/api/prs/PR-1/prevalidation", headers=w.req).json()
    [line] = check["lines"]
    # offered: the department's rows of the fund counted in boxes - never by the item
    assert line["offered"] == [w.toner, w.generic]
    assert {o["id"] for o in check["options"]} == {w.toner, w.generic}
    assert [v["code"] for v in line["violations"]] == ["PLAN_ROW_NOT_CHOSEN"]
    assert not check["eligible"] and line["matched_plan_item_id"] is None

    none = w.create("PR-1", None)
    assert none.status_code == 409 and _codes(none) == {"PLAN_ROW_NOT_CHOSEN"}
    reams = w.create("PR-1", w.paper)
    assert reams.status_code == 409 and _codes(reams) == {"PLAN_ROW_NOT_ALLOWED"}
    d = w.create("PR-1", w.generic)
    assert d.status_code == 201, d.text
    assert [i["plan_item_id"] for i in d.json()["items"]] == [w.generic]

    c = w.confirm(d.json()["id"])
    assert c.status_code == 200, c.text
    assert w.balance(w.generic) == ("8.0000", "300.00")  # 10 @ 50 less 2 boxes for 200
    assert w.balance(w.toner) == ("10.0000", "1000.00")  # the same item's row is untouched
    page = w.hx.client.get(f"/api/pprs/{d.json()['id']}/print", headers=w.req)
    assert page.status_code == 200 and "หมึกพิมพ์ทั่วไป" in page.text
    [v] = w.hx.client.get(f"/api/pprs/{d.json()['id']}/versions", headers=w.req).json()
    assert v["snapshot"]["items"][0]["plan_item_name"] == "หมึกพิมพ์ทั่วไป"

    # nothing is remembered for the next PR of the same item
    w.pr("PR-2")
    again = w.hx.client.get("/api/prs/PR-2/prevalidation", headers=w.req).json()
    assert [v["code"] for v in again["lines"][0]["violations"]] == ["PLAN_ROW_NOT_CHOSEN"]


@pytest.mark.spec("D-43", "AT-40.18.3")
def test_choices_change_only_by_the_creator_while_editable_and_are_audited(w: World) -> None:
    w.pr("PR-3")
    d = w.create("PR-3", w.toner).json()
    pid = d["id"]
    got = w.hx.client.get(f"/api/pprs/{pid}/choices", headers=w.req)
    assert got.status_code == 200, got.text
    assert got.json()["stored"] == [{"pr_item_id": "PR-3-1", "plan_item_id": w.toner}]
    assert got.json()["check"]["eligible"]
    trial = w.hx.client.get(
        f"/api/pprs/{pid}/choices", params={"choice": f"PR-3-1:{w.paper}"}, headers=w.req
    ).json()
    assert [v["code"] for v in trial["check"]["lines"][0]["violations"]] == ["PLAN_ROW_NOT_ALLOWED"]
    bad = w.hx.client.get(f"/api/pprs/{pid}/choices", params={"choice": "x"}, headers=w.req)
    assert bad.status_code == 400 and bad.json()["detail"]["code"] == "BAD_CHOICE"

    other = w.hx.h(w.hx.user("req_other", Role.REQUESTER))
    assert w.put(pid, {"PR-3-1": w.generic}, other).status_code == 404  # D-41: not theirs
    assert w.put(pid, {"PR-3-1": w.generic}, w.plan.h).status_code == 403
    r = w.put(pid, {"PR-3-1": w.paper})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PLAN_ROW_NOT_ALLOWED"
    r = w.put(pid, {"NO-SUCH-LINE": w.generic})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "CHOICE_NOT_A_PR_LINE"
    twice = w.hx.client.put(
        f"/api/pprs/{pid}/choices",
        json={
            "choices": [
                {"pr_item_id": "PR-3-1", "plan_item_id": w.toner},
                {"pr_item_id": "PR-3-1", "plan_item_id": w.generic},
            ]
        },
        headers=w.req,
    )
    assert twice.status_code == 422 and twice.json()["detail"]["code"] == "DUPLICATE_CHOICE"

    ok = w.put(pid, {"PR-3-1": w.generic})
    assert ok.status_code == 200, ok.text
    assert [i["plan_item_id"] for i in ok.json()["items"]] == [w.generic]  # a draft follows
    assert w.confirm(pid).status_code == 200
    assert w.balance(w.generic) == ("8.0000", "300.00")
    locked = w.put(pid, {"PR-3-1": w.toner})
    assert locked.status_code == 409 and locked.json()["detail"]["code"] == "PPR_LOCKED"

    # a wrong choice: Plan Officer unlocks, the requester chooses again and reconfirms
    u = w.hx.client.post(f"/api/pprs/{pid}/unlock", json={"reason": "ผิดแถว"}, headers=w.plan.h)
    assert u.status_code == 200, u.text
    moved = w.put(pid, {"PR-3-1": w.toner})
    assert moved.status_code == 200, moved.text
    # an unlocked PPR keeps its confirmed lines (and usage) until it is reconfirmed
    assert [i["plan_item_id"] for i in moved.json()["items"]] == [w.generic]
    assert w.balance(w.generic) == ("8.0000", "300.00")
    re = w.confirm(pid)
    assert re.status_code == 200, re.text
    assert [i["plan_item_id"] for i in re.json()["items"]] == [w.toner]
    assert w.balance(w.generic) == ("10.0000", "500.00")  # released in full
    assert w.balance(w.toner) == ("8.0000", "800.00")

    with w.hx.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT before, after FROM audit_log WHERE action = 'PPR_CHOICES_UPDATED' "
                "ORDER BY id"
            )
        ).all()
    assert [(b["choices"], a["choices"]) for b, a in rows] == [
        ({"PR-3-1": w.toner}, {"PR-3-1": w.generic}),
        ({"PR-3-1": w.generic}, {"PR-3-1": w.toner}),
    ]


@pytest.mark.spec("D-43", "D-21", "AT-40.18.1")
def test_a_unit_changed_in_hosxp_after_the_draft_fails_closed(w: World) -> None:
    w.pr("PR-4")
    d = w.create("PR-4", w.generic).json()
    h = w.hx.gateway.data.prs["PR-4"]
    [item] = w.hx.gateway.data.pr_items["PR-4"]
    w.hx.gateway.replace_pr(h, [item.model_copy(update={"unit": "รีม"})])
    r = w.confirm(d["id"])
    assert r.status_code == 409 and _codes(r) == {"PLAN_ROW_NOT_ALLOWED"}
    assert w.balance(w.generic) == ("10.0000", "500.00")


@pytest.mark.spec("D-43", "AT-40.18.5")
def test_rows_without_an_item_are_added_and_renamed_by_amendment_only_while_unused(
    w: World,
) -> None:
    w.pr("PR-5")
    d = w.create("PR-5", w.generic).json()
    assert w.confirm(d["id"]).status_code == 200

    def amend(**body: Any) -> Any:
        return w.hx.client.post(
            f"/api/plan-years/{FY}/amendments",
            json={
                "approval_document_no": "สส 9/2570",
                "approval_date": "2026-10-15",
                "reason": "D-43",
                **body,
            },
            headers=w.plan.h,
        )

    used = amend(items=[{"plan_item_id": w.generic, "item_name": "หมึกสี"}])
    assert used.status_code == 409 and _codes(used) == {"DATA_CHANGE_AFTER_USE"}
    coded = amend(items=[{"plan_item_id": w.toner, "unit": "ชิ้น"}])
    assert coded.status_code == 422 and coded.json()["detail"]["code"] == "NAME_FROM_HOSXP"
    nameless = amend(
        budgets=[
            {
                "fund_source_id": "MOCK-FUND-1",
                "budget_category_id": OFFICE,
                "approved_amount": "1900",
            }
        ],
        new_items=[_new("N1", item_name=" ")],
    )
    assert nameless.status_code == 409 and _codes(nameless) == {"NAME_AND_UNIT_REQUIRED"}
    ok = amend(
        budgets=[
            {
                "fund_source_id": "MOCK-FUND-1",
                "budget_category_id": OFFICE,
                "approved_amount": "1900",
            }
        ],
        items=[{"plan_item_id": w.paper, "item_name": "กระดาษ A4", "unit": "รีม"}],
        new_items=[_new("N1", item_name="ซองเอกสาร", unit="ซอง")],
    )
    assert ok.status_code == 201, ok.text
    items = {
        i["id"]: i for i in w.hx.client.get(f"/api/plan-years/{FY}/items", headers=w.plan.h).json()
    }
    assert (items[w.paper]["item_name"], items[w.paper]["unit"]) == ("กระดาษ A4", "รีม")
    [new] = [i for i in items.values() if i["item_name"] == "ซองเอกสาร"]
    assert (new["item_id"], new["unit"]) == (None, "ซอง")


def _new(ref: str, **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ref": ref,
        "plan_type": "DEPARTMENT",
        "owner_department_id": "MOCK-DEP-ENT",
        "fund_source_id": "MOCK-FUND-1",
        "budget_category_id": OFFICE,
        "planned_qty": "10",
        "estimated_unit_price": "10",
        "planned_amount": "100",
    }
    body.update(over)
    return body


@pytest.mark.spec("D-43", "AT-40.18.1", "AT-40.18.3")
def test_a_line_offered_nothing_never_keeps_a_choice(w: World) -> None:
    w.hx.put_pr("PR-6", (TONER, "1", "100"), ("MOCK-ITEM-PAPER", "1", "30"))  # box; "ream"
    check = w.hx.client.get(
        "/api/prs/PR-6/prevalidation", params=[("choice", f"PR-6-2:{w.paper}")], headers=w.req
    ).json()
    assert check["lines"][1]["offered"] == []  # the plan counts paper in "รีม", HOSxP in "ream"
    assert {v["code"] for v in check["lines"][1]["violations"]} == {
        "NOT_IN_PLAN",
        "PLAN_ROW_NOT_ALLOWED",
    }
    w.pr("PR-6b")
    d = w.create("PR-6b", w.toner).json()
    # the PR gains a paper line in HOSxP: a choice for it is refused, never stored
    h = w.hx.gateway.data.prs["PR-6b"]
    [toner] = w.hx.gateway.data.pr_items["PR-6b"]
    paper = toner.model_copy(
        update={"pr_item_id": "PR-6b-2", "item_id": "MOCK-ITEM-PAPER", "unit": None}
    )
    w.hx.gateway.replace_pr(h, [toner, paper])
    r = w.put(d["id"], {"PR-6b-2": w.paper})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PLAN_ROW_NOT_ALLOWED"
    big = w.put(d["id"], {"PR-6b-1": 2**63})
    assert big.status_code == 422  # outside the id range: refused by the API, never a 500
    stored = w.hx.client.get(f"/api/pprs/{d['id']}/choices", headers=w.req).json()["stored"]
    assert stored == [{"pr_item_id": "PR-6b-1", "plan_item_id": w.toner}]


@pytest.mark.spec("D-43", "AT-40.18.3", "AT-40.18.5")
def test_a_row_renamed_after_it_was_chosen_is_chosen_again(w: World) -> None:
    w.hx.put_pr("PR-7", (TONER, "1", "100"), (TONER, "1", "100"))
    r = w.hx.client.post(
        "/api/pprs",
        json={
            "pr_no": "PR-7",
            "choices": [
                {"pr_item_id": "PR-7-1", "plan_item_id": w.generic},
                {"pr_item_id": "PR-7-2", "plan_item_id": w.toner},
            ],
        },
        headers=w.req,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    # naming one line changes that line only
    assert w.put(pid, {"PR-7-2": w.generic}).status_code == 200
    stored = w.hx.client.get(f"/api/pprs/{pid}/choices", headers=w.req).json()["stored"]
    assert stored == [
        {"pr_item_id": "PR-7-1", "plan_item_id": w.generic},
        {"pr_item_id": "PR-7-2", "plan_item_id": w.generic},
    ]
    # the Plan Officer renames the row while only a draft has chosen it (no usage yet)
    am = w.hx.client.post(
        f"/api/plan-years/{FY}/amendments",
        json={
            "approval_document_no": "สส 10/2570",
            "approval_date": "2026-10-15",
            "reason": "rename",
            "items": [{"plan_item_id": w.generic, "item_name": "หมึกพิมพ์สี"}],
        },
        headers=w.plan.h,
    )
    assert am.status_code == 201, am.text
    c = w.confirm(pid)
    assert c.status_code == 409 and c.json()["detail"]["code"] == "PLAN_ROW_CHANGED"
    assert {d["subject"] for d in c.json()["detail"]["details"]} == {"PR-7-1", "PR-7-2"}
    # choosing one line again leaves the other still to be chosen again
    assert w.put(pid, {"PR-7-1": w.generic}).status_code == 200
    assert w.confirm(pid).json()["detail"]["code"] == "PLAN_ROW_CHANGED"
    assert w.put(pid, {"PR-7-1": w.generic, "PR-7-2": w.generic}).status_code == 200
    ok = w.confirm(pid)
    assert ok.status_code == 200, ok.text
    assert w.balance(w.generic) == ("8.0000", "300.00")

    # once a PPR has drawn on it, the name stays - even after the usage is released
    u = w.hx.client.post(f"/api/pprs/{pid}/unlock", json={"reason": "x"}, headers=w.plan.h)
    assert u.status_code == 200
    assert w.put(pid, {"PR-7-1": w.toner, "PR-7-2": w.toner}).status_code == 200
    assert w.confirm(pid).status_code == 200
    assert w.balance(w.generic) == ("10.0000", "500.00")
    again = w.hx.client.post(
        f"/api/plan-years/{FY}/amendments",
        json={
            "approval_document_no": "สส 11/2570",
            "approval_date": "2026-10-15",
            "reason": "rename again",
            "items": [{"plan_item_id": w.generic, "item_name": "หมึกพิมพ์"}],
        },
        headers=w.plan.h,
    )
    assert again.status_code == 409 and _codes(again) == {"DATA_CHANGE_AFTER_USE"}
