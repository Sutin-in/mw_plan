"""Wave 2 Plan Core end-to-end: master sync, budgets, Plan Items, validation, activation,
ledger posting, corrections while APPROVED, and department-scoped reads.

Spec §8, §9, §10, §18, §22.1, §25.2, §25.4, §36; D-11 ... D-15.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from api_harness import FY, Harness, Plan
from ppr.domain.roles import Role
from ppr.integration.hosxp.contracts import Item


@pytest.fixture
def hx(db: Engine) -> Harness:
    return Harness(db)


@pytest.fixture
def plan(hx: Harness) -> Plan:
    p = Plan(hx)
    p.sync()
    p.year()
    return p


# ------------------------------------------------------------------ master sync (D-14)
@pytest.mark.spec("D-14", "S-8", "S-25.2", "S-25.4", "AT-40.6.2")
def test_manual_master_sync_is_logged_and_idempotent(hx: Harness) -> None:
    p = Plan(hx)
    first = p.sync()
    assert first["status"] == "SUCCEEDED"
    assert first["records_created"] == first["records_checked"] > 0
    again = p.sync()  # repeated sync creates nothing new and changes nothing
    assert (again["records_created"], again["records_updated"]) == (0, 0)

    hx.gateway.data.items[0] = Item(
        source_id="MOCK-ITEM-TONER",
        code="I1",
        name="Mock toner (renamed)",
        unit="box",
        active=True,
    )
    changed = p.sync()
    assert (changed["records_created"], changed["records_updated"]) == (0, 1)

    with hx.engine.connect() as conn:
        runs = conn.execute(
            text("SELECT mode, scope, status, requested_by_user_id FROM sync_run ORDER BY id")
        ).all()
    assert [tuple(r)[:3] for r in runs] == [("MANUAL", "MASTERS", "SUCCEEDED")] * 3
    assert all(r[3] is not None for r in runs)
    assert len(p.audit("MANUAL_SYNC_STARTED")) == 3
    items = hx.client.get("/api/masters/ITEM", headers=p.h).json()
    assert {i["source_id"] for i in items} >= {"MOCK-ITEM-TONER", "MOCK-ITEM-OLD"}


@pytest.mark.spec("AT-40.6.3", "AT-40.6.5", "S-26")
def test_sync_failure_is_logged_and_existing_data_kept(hx: Harness) -> None:
    p = Plan(hx)
    p.sync()
    hx.gateway.available = False
    r = hx.client.post("/api/sync/masters", headers=p.h)
    assert r.status_code == 503
    runs = hx.client.get("/api/sync/runs", headers=p.h).json()
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["error_details"] == {"reason": "HOSXP_UNAVAILABLE"}
    assert len(hx.client.get("/api/masters/DEPARTMENT", headers=p.h).json()) == 7  # kept


# ------------------------------------------------------------------ budgets
def test_budget_requires_synced_active_masters_and_is_unique(plan: Plan) -> None:
    assert plan.budget("MOCK-CAT-OFFICE", "1000").status_code == 201
    dup = plan.budget("MOCK-CAT-OFFICE", "5")
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "BUDGET_EXISTS"
    unknown = plan.budget("MOCK-CAT-NOPE", "5")
    assert unknown.status_code == 422 and unknown.json()["detail"]["code"] == "UNKNOWN_MASTER"
    assert plan.budget("MOCK-CAT-MED", "-1").status_code == 422  # negative amount


def test_budget_with_items_cannot_be_deleted(plan: Plan) -> None:
    bid = plan.budget("MOCK-CAT-OFFICE", "1000").json()["id"]
    assert plan.item(bid, "1000").status_code == 201
    r = plan.hx.client.delete(f"/api/plan-budgets/{bid}", headers=plan.h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "BUDGET_IN_USE"


# ------------------------------------------------------------------ plan items
@pytest.mark.spec("S-9.2", "D-14")
def test_plan_item_snapshots_master_and_reports_estimate_warning(plan: Plan) -> None:
    bid = plan.budget("MOCK-CAT-OFFICE", "1000").json()["id"]
    r = plan.item(bid, "1000", estimated_unit_price="90")  # 10 x 90 = 900 != 1000
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["item_code"], body["item_name"], body["unit"]) == ("I1", "Mock toner", "box")
    assert (body["fund_source_id"], body["budget_category_id"]) == (
        "MOCK-FUND-1",
        "MOCK-CAT-OFFICE",
    )
    assert [w["code"] for w in body["warnings"]] == ["AMOUNT_DIFFERS_FROM_ESTIMATE"]  # D-13


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"item_id": "MOCK-ITEM-OLD"}, "INACTIVE_MASTER"),
        ({"item_id": "MOCK-ITEM-NOPE"}, "UNKNOWN_MASTER"),
        ({"owner_department_id": "MOCK-DEP-OLD"}, "INACTIVE_MASTER"),
        ({"demands": [{"department_id": "MOCK-DEP-OR", "demand_qty": "1"}]}, "DEMAND_NOT_ALLOWED"),
        (
            {
                "plan_type": "ASSIGNED",
                "demands": [
                    {"department_id": "MOCK-DEP-OR", "demand_qty": "1"},
                    {"department_id": "MOCK-DEP-OR", "demand_qty": "2"},
                ],
            },
            "DUPLICATE_DEMAND",
        ),
    ],
)
def test_invalid_plan_item_input_is_rejected(plan: Plan, over: dict[str, Any], code: str) -> None:
    bid = plan.budget("MOCK-CAT-OFFICE", "1000").json()["id"]
    r = plan.item(bid, "1000", **over)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


@pytest.mark.spec("D-37")
def test_zero_or_negative_quantity_rejected_by_api_and_database(plan: Plan) -> None:
    bid = plan.budget("MOCK-CAT-OFFICE", "1000").json()["id"]
    assert plan.item(bid, "1000", planned_qty="0").status_code == 422  # API validation
    assert plan.item(bid, "1000").status_code == 201
    # The database refuses a negative quantity whatever the entry path. Zero is allowed there
    # since D-37 (an amendment may close an unused item), but plan entry still refuses it.
    with pytest.raises(IntegrityError), plan.hx.engine.begin() as conn:
        conn.execute(text("UPDATE plan_item SET planned_qty = :q"), {"q": "-1"})


@pytest.mark.spec("S-10.3", "AT-40.12.3")
def test_assigned_purchase_preserves_department_demand(plan: Plan) -> None:
    bid = plan.budget("MOCK-CAT-MED", "3000").json()["id"]
    r = plan.item(
        bid,
        "3000",
        plan_type="ASSIGNED",
        item_id="MOCK-ITEM-GLOVE",
        owner_department_id="MOCK-DEP-STORE",
        purchasing_department_id="MOCK-DEP-STORE",
        planned_qty="300",
        estimated_unit_price="10",
        demands=[
            {"department_id": "MOCK-DEP-WARD-A", "demand_qty": "100"},
            {"department_id": "MOCK-DEP-WARD-B", "demand_qty": "150"},
            {"department_id": "MOCK-DEP-OR", "demand_qty": "50"},
        ],
    )
    assert r.status_code == 201, r.text
    demands = {d["department_id"]: (d["demand_qty"], d["state"]) for d in r.json()["demands"]}
    assert demands["MOCK-DEP-WARD-B"] == ("150.0000", "PLANNED")
    assert len(demands) == 3


# ------------------------------------------------------------------ validation + activation
def _balanced_plan(plan: Plan) -> tuple[int, int]:
    b1 = plan.budget("MOCK-CAT-OFFICE", "1500").json()["id"]
    b2 = plan.budget("MOCK-CAT-MED", "3000").json()["id"]
    i1 = plan.item(b1, "1000").json()["id"]
    plan.item(b1, "500", owner_department_id="MOCK-DEP-OR")
    plan.item(b2, "3000", item_id="MOCK-ITEM-GLOVE", planned_qty="300", estimated_unit_price="10")
    return b1, i1


@pytest.mark.spec("S-9.1", "D-11", "D-12")
def test_validation_report_shows_envelope_per_exact_category(plan: Plan) -> None:
    b1 = plan.budget("MOCK-CAT-OFFICE", "1500").json()["id"]
    plan.budget("MOCK-CAT-MAT", "999")  # parent category: its children's items do NOT count
    plan.item(b1, "1000")
    r = plan.hx.client.get(f"/api/plan-years/{FY}/validation", headers=plan.h).json()
    assert r["can_activate"] is False
    env = {e["budget_category_id"]: (e["planned_total"], e["difference"]) for e in r["envelope"]}
    assert env["MOCK-CAT-OFFICE"] == ("1000.00", "-500.00")
    assert env["MOCK-CAT-MAT"] == ("0", "-999.00")
    assert {e["code"] for e in r["errors"]} == {"ENVELOPE_MISMATCH"}


@pytest.mark.spec("D-12", "S-18", "AT-40.5.4")
def test_activation_posts_plan_activated_once_per_item_atomically(plan: Plan) -> None:
    _, i1 = _balanced_plan(plan)
    assert plan.move("APPROVED").status_code == 200
    r = plan.move("ACTIVE")
    assert r.status_code == 200, r.text
    with plan.hx.engine.connect() as conn:
        rows = conn.execute(
            text("SELECT event_type, count(*) FROM plan_ledger GROUP BY event_type")
        ).all()
    assert [tuple(x) for x in rows] == [("PLAN_ACTIVATED", 3)]
    bal = plan.hx.client.get(f"/api/plan-items/{i1}/balance", headers=plan.h).json()
    assert (bal["approved_qty"], bal["approved_amount"]) == ("10.0000", "1000.00")
    assert (bal["remaining_qty"], bal["remaining_amount"]) == ("10.0000", "1000.00")
    [(_action, _, after)] = plan.audit("PLAN_ACTIVATED")
    assert after["plan_items"] == 3


@pytest.mark.spec("D-12", "S-9.1")
def test_invalid_plan_cannot_activate_and_nothing_is_posted(plan: Plan) -> None:
    b1 = plan.budget("MOCK-CAT-OFFICE", "1500").json()["id"]
    plan.item(b1, "1000")
    plan.move("APPROVED")
    r = plan.move("ACTIVE")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "PLAN_NOT_VALID"
    assert [d["code"] for d in detail["details"]] == ["ENVELOPE_MISMATCH"]
    with plan.hx.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM plan_ledger")).scalar_one() == 0
        state = conn.execute(text("SELECT state FROM plan_year")).scalar_one()
    assert state == "APPROVED"


# ------------------------------------------------------------------ D-15 corrections
@pytest.mark.spec("D-15")
def test_approved_plan_accepts_only_documented_corrections(plan: Plan) -> None:
    b1 = plan.budget("MOCK-CAT-OFFICE", "1500").json()["id"]
    item_id = plan.item(b1, "1000").json()["id"]
    plan.move("APPROVED")
    assert plan.move("ACTIVE").status_code == 409  # 1000 of 1500 entered

    # A plain edit is refused while APPROVED.
    r = plan.item(b1, "500", owner_department_id="MOCK-DEP-OR")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "CORRECTION_DETAILS_REQUIRED"
    # Reason without a source reference is refused too.
    r = plan.item(b1, "500", owner_department_id="MOCK-DEP-OR", correction_reason="typo")
    assert r.json()["detail"]["code"] == "CORRECTION_DETAILS_REQUIRED"

    # A documented correction that makes the system match the approved document is allowed.
    r = plan.item(
        b1,
        "500",
        owner_department_id="MOCK-DEP-OR",
        correction_reason="line 14 of the approved plan was not entered",
        source_reference="Director-approved plan FY2570, p.3 line 14",
    )
    assert r.status_code == 201, r.text
    assert plan.move("ACTIVE").status_code == 200

    [(_action, reason, after)] = plan.audit("PLAN_ITEM_CORRECTED")
    assert reason == "line 14 of the approved plan was not entered"
    assert after["correction"] == {
        "operation": "create",
        "source_reference": "Director-approved plan FY2570, p.3 line 14",
    }
    assert len(plan.audit("PLAN_ITEM_CREATED")) == 1  # the DRAFT-time item

    # After ACTIVE nothing can change, even with correction details (Plan Amendment only).
    r = plan.hx.client.delete(
        f"/api/plan-items/{item_id}",
        params={"correction_reason": "x", "source_reference": "y"},
        headers=plan.h,
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PLAN_YEAR_LOCKED"


def test_draft_edits_are_audited_with_before_and_after(plan: Plan) -> None:
    bid = plan.budget("MOCK-CAT-OFFICE", "1000").json()["id"]
    iid = plan.item(bid, "1000").json()["id"]
    body = {
        "plan_budget_id": bid,
        "plan_type": "DEPARTMENT",
        "item_id": "MOCK-ITEM-TONER",
        "owner_department_id": "MOCK-DEP-ENT",
        "planned_qty": "20",
        "estimated_unit_price": "50",
        "planned_amount": "1000",
    }
    assert (
        plan.hx.client.put(f"/api/plan-items/{iid}", json=body, headers=plan.h).status_code == 200
    )
    with plan.hx.engine.connect() as conn:
        before, after = conn.execute(
            text("SELECT before, after FROM audit_log WHERE action = 'PLAN_ITEM_UPDATED'")
        ).one()
    assert (before["planned_qty"], after["planned_qty"]) == ("10.0000", "20.0000")
    assert plan.hx.client.delete(f"/api/plan-items/{iid}", headers=plan.h).status_code == 204
    assert len(plan.audit("PLAN_ITEM_DELETED")) == 1


# ------------------------------------------------------------------ ledger integrity
@pytest.mark.spec("S-18.3", "S-30", "AT-40.5.3")
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE plan_ledger SET qty_delta = 999",
        "DELETE FROM plan_ledger",
        "TRUNCATE plan_ledger",
    ],
)
def test_plan_ledger_is_append_only(plan: Plan, sql: str) -> None:
    _balanced_plan(plan)
    plan.move("APPROVED")
    plan.move("ACTIVE")
    with pytest.raises(DBAPIError, match="append-only"), plan.hx.engine.begin() as conn:
        conn.execute(text(sql))


@pytest.mark.spec("S-18.2")
@pytest.mark.parametrize(
    ("event", "qty", "amount"),
    [
        ("PPR_CONFIRMED", 1, -1),  # consumption must be negative qty
        ("PLAN_AMENDMENT", 0, 0),
    ],
)
def test_ledger_sign_rules_are_enforced_by_the_database(
    plan: Plan, event: str, qty: int, amount: int
) -> None:
    _, i1 = _balanced_plan(plan)
    with pytest.raises(IntegrityError), plan.hx.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, amount_delta, "
                "idempotency_key, ppr_ref) VALUES (:i, :e, :q, :a, 'k-x', 'PPR-X')"
            ),
            {"i": i1, "e": event, "q": qty, "a": amount},
        )


def test_second_plan_activated_for_same_item_is_rejected(plan: Plan) -> None:
    _, i1 = _balanced_plan(plan)
    plan.move("APPROVED")
    plan.move("ACTIVE")
    with pytest.raises(IntegrityError), plan.hx.engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, amount_delta, "
                "idempotency_key) VALUES (:i, 'PLAN_ACTIVATED', 1, 1, 'another-key')"
            ),
            {"i": i1},
        )


# ------------------------------------------------------------------ department scoping
@pytest.mark.spec("S-22.1", "AT-40.10.1", "AT-40.10.2")
def test_requester_sees_only_own_department_plan(plan: Plan) -> None:
    _balanced_plan(plan)  # ENT toner, OR toner, ENT gloves
    ent = plan.hx.user("ent_staff", Role.REQUESTER, department="MOCK-DEP-ENT")
    it = plan.hx.user("it_staff", Role.REQUESTER, department="MOCK-DEP-IT")
    cfo = plan.hx.user("cfo", Role.CFO, department="MOCK-DEP-IT")

    def owners(token: str) -> list[str]:
        r = plan.hx.client.get(f"/api/plan-years/{FY}/items", headers=plan.hx.h(token))
        return sorted(i["owner_department_id"] for i in r.json())

    assert owners(ent) == ["MOCK-DEP-ENT", "MOCK-DEP-ENT"]
    assert owners(it) == []
    assert owners(cfo) == ["MOCK-DEP-ENT", "MOCK-DEP-ENT", "MOCK-DEP-OR"]
    assert owners(plan.token) == owners(cfo)  # planning sees all

    or_item = next(
        i["id"]
        for i in plan.hx.client.get(f"/api/plan-years/{FY}/items", headers=plan.h).json()
        if i["owner_department_id"] == "MOCK-DEP-OR"
    )
    for path in (f"/api/plan-items/{or_item}", f"/api/plan-items/{or_item}/balance"):
        assert plan.hx.client.get(path, headers=plan.hx.h(ent)).status_code == 404
        assert plan.hx.client.get(path, headers=plan.hx.h(cfo)).status_code == 200


@pytest.mark.spec("S-10.3", "S-22.1")
def test_demand_departments_can_see_the_assigned_item(plan: Plan) -> None:
    bid = plan.budget("MOCK-CAT-MED", "3000").json()["id"]
    plan.item(
        bid,
        "3000",
        plan_type="ASSIGNED",
        item_id="MOCK-ITEM-GLOVE",
        owner_department_id="MOCK-DEP-STORE",
        purchasing_department_id="MOCK-DEP-STORE",
        planned_qty="300",
        estimated_unit_price="10",
        demands=[{"department_id": "MOCK-DEP-WARD-A", "demand_qty": "300"}],
    )
    ward = plan.hx.user("ward_a", Role.REQUESTER, department="MOCK-DEP-WARD-A")
    r = plan.hx.client.get(f"/api/plan-years/{FY}/items", headers=plan.hx.h(ward))
    assert [i["plan_type"] for i in r.json()] == ["ASSIGNED"]


@pytest.mark.spec("AT-40.12.2", "D-39", "S-18.2")
@pytest.mark.parametrize("event", ["CENTRAL_STOCK_ISSUE", "CENTRAL_STOCK_RETURN"])
@pytest.mark.parametrize(("qty", "amount"), [(-1, 0), (1, 0), (-1, -5), (1, 5)])
def test_database_refuses_stock_movements_in_the_plan_ledger(
    plan: Plan, event: str, qty: int, amount: int
) -> None:
    """D-39 at the database layer: even an insert that bypasses the application, with the
    old D-01 "quantity only" shape, is refused by a named constraint (migration 0010)."""
    _, i1 = _balanced_plan(plan)
    with (
        pytest.raises(IntegrityError, match=r"no_stock_movement|known_event"),
        plan.hx.engine.begin() as conn,
    ):
        conn.execute(
            text(
                "INSERT INTO plan_ledger(plan_item_id, event_type, qty_delta, amount_delta, "
                "idempotency_key) VALUES (:i, :e, :q, :a, 'k-stock')"
            ),
            {"i": i1, "e": event, "q": qty, "a": amount},
        )
    with plan.hx.engine.connect() as conn:
        rule = conn.execute(
            text(
                "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname = 'ck_plan_ledger_no_stock_movement'"
            )
        ).scalar_one()
        assert "CENTRAL_STOCK_ISSUE" in rule and "CENTRAL_STOCK_RETURN" in rule
        assert (
            conn.execute(
                text("SELECT count(*) FROM plan_ledger WHERE event_type = :e"), {"e": event}
            ).scalar_one()
            == 0
        )


# ------------------------------------------------------------------ hospital size (Wave 11B)
HOSPITAL_ITEMS = 44_000  # the hospital's HOSxP has 43,707 items (IT, 2026-10-06)


@pytest.mark.spec("AT-40.16.1", "S-25.2", "S-25.4")
def test_a_hospital_size_catalogue_syncs_in_one_transaction(hx: Harness) -> None:
    # One INSERT of 44,000 items x 5 values is far above PostgreSQL's 65,535 parameters:
    # the first real sync failed with OperationalError and stored nothing.
    p = Plan(hx)
    hx.gateway.data.items = [
        Item(source_id=f"H-{n}", code=f"C{n}", name=f"Item {n}", unit="ea", active=True)
        for n in range(HOSPITAL_ITEMS)
    ]
    first = p.sync()
    assert first["status"] == "SUCCEEDED", first
    assert first["records_created"] >= HOSPITAL_ITEMS
    again = p.sync()
    assert (again["status"], again["records_created"], again["records_updated"]) == (
        "SUCCEEDED",
        0,
        0,
    )
    last = HOSPITAL_ITEMS - 1  # in the last batch
    hx.gateway.data.items[last] = Item(
        source_id=f"H-{last}", code=f"C{last}", name="renamed", unit="ea", active=False
    )
    changed = p.sync()
    assert (changed["records_created"], changed["records_updated"]) == (0, 1)
    with hx.engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM hosxp_item WHERE source_id LIKE 'H-%'"))
        assert n.scalar() == HOSPITAL_ITEMS
        row = conn.execute(
            text("SELECT name, active FROM hosxp_item WHERE source_id = :s"), {"s": f"H-{last}"}
        ).one()
    assert tuple(row) == ("renamed", False)


@pytest.mark.spec("AT-40.16.1", "S-25.4")
def test_a_failing_later_batch_keeps_every_master_as_it_was(hx: Harness) -> None:
    from ppr.infrastructure.db import plan_repositories

    p = Plan(hx)
    p.sync()
    kinds = ("FUND_SOURCE", "BUDGET_CATEGORY", "DEPARTMENT", "ITEM")
    before = {k: hx.client.get(f"/api/masters/{k}", headers=p.h).json() for k in kinds}
    hx.gateway.data.departments[0] = hx.gateway.data.departments[0].model_copy(
        update={"name": "renamed department"}
    )
    hx.gateway.data.items = [
        Item(source_id=f"B-{n}", code=f"B{n}", name=f"B {n}", unit="ea", active=True)
        for n in range(5)
    ] + [Item(source_id="B-bad", code="Bx", name="nul\x00", unit="ea", active=True)]
    # Batches of 2: B-0..B-3 are written before the last batch fails in PostgreSQL.
    old = plan_repositories.MASTER_UPSERT_BATCH
    plan_repositories.MASTER_UPSERT_BATCH = 2
    try:
        client = TestClient(hx.client.app, raise_server_exceptions=False)
        assert client.post("/api/sync/masters", headers=p.h).status_code == 500
    finally:
        plan_repositories.MASTER_UPSERT_BATCH = old
    runs = hx.client.get("/api/sync/runs", headers=p.h).json()
    assert runs[0]["status"] == "FAILED"
    after = {k: hx.client.get(f"/api/masters/{k}", headers=p.h).json() for k in kinds}
    assert after == before  # nothing of this run stays, not even the earlier batches
    with hx.engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM hosxp_item WHERE source_id LIKE 'B-%'"))
        assert n.scalar() == 0


@pytest.mark.spec("AT-40.16.1", "S-25.4")
def test_a_repeated_source_id_is_named_and_keeps_the_masters(hx: Harness) -> None:
    p = Plan(hx)
    p.sync()
    before = hx.client.get("/api/masters/ITEM", headers=p.h).json()
    hx.gateway.data.items = [
        Item(source_id=f"D-{n}", code=f"D{n}", name=f"D {n}", unit="ea", active=True)
        for n in range(5)
    ] + [Item(source_id="D-0", code="D0", name="again", unit="ea", active=True)]
    r = hx.client.post("/api/sync/masters", headers=p.h)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "DUPLICATE_SOURCE_ID"
    runs = hx.client.get("/api/sync/runs", headers=p.h).json()
    assert runs[0]["status"] == "FAILED"
    assert runs[0]["error_details"] == {
        "reason": "DUPLICATE_SOURCE_ID",
        "kind": "ITEM",
        "count": 1,
        "examples": ["D-0"],
    }
    assert hx.client.get("/api/masters/ITEM", headers=p.h).json() == before


def test_the_repository_refuses_a_repeated_source_id_too(db: Engine) -> None:
    # Defence in depth: batches must never hide a repeat that one statement refused.
    from ppr.infrastructure.db.plan_repositories import SqlMasterRepository
    from ppr.ports import MasterKind

    item = Item(source_id="R-1", code="R1", name="R", unit="ea", active=True)
    with db.connect() as conn, pytest.raises(ValueError, match="more than once"):
        SqlMasterRepository(conn).upsert(MasterKind.ITEM, [item, item])
