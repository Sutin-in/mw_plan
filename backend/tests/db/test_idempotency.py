"""Wave 10A: repeated synchronization is idempotent (spec §25.5, §38.2; D-29, D-30, D-38).

Repeating a run, retrying a failed one, or synchronizing one PPR again never changes the plan
twice: no second release, no second state change, no second "PR changed" or demand-state audit
row, and nothing at all when HOSxP has not changed. Already covered elsewhere: one release for
repeated and concurrent cancellations, retry after HOSxP was unavailable (test_sync_api) and
repeated master synchronization (test_plan_core).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import pytest
from sqlalchemy import Engine, text
from test_assigned_api import ENT, WARD, Assigned
from test_sync_api import TONER, W

from api_harness import Harness


def _state(w: W) -> dict[str, Any]:
    """What a synchronization may change: ledger, PPR states, business audit actions."""
    rows = w.rows(
        "SELECT count(*), COALESCE(SUM(qty_delta), 0), COALESCE(SUM(amount_delta), 0) "
        "FROM plan_ledger"
    )
    ppr = w.rows("SELECT id, state, current_version, updated_at FROM ppr ORDER BY id")
    audit = Counter(
        a
        for (a,) in w.rows("SELECT action FROM audit_log")
        if "SYNC_" not in a  # the run itself is logged each time
    )
    return {"ledger": rows, "ppr": ppr, "audit": audit}


@pytest.fixture
def w(db: Engine) -> W:
    return W(db)


@pytest.mark.spec("S-25.5", "D-29", "AT-40.6.4")
def test_a_second_run_over_unchanged_prs_changes_nothing(w: W) -> None:
    for n in range(3):
        w.confirmed(f"PR-I{n}", (TONER, "1", "100"))
    assert w.sync_all().json()["run"]["status"] == "SUCCEEDED"
    before = _state(w)
    body = w.sync_all().json()
    assert body["run"]["status"] == "SUCCEEDED"
    assert {o["outcome"] for o in body["observations"]} == {"UNCHANGED"}
    assert _state(w) == before


@pytest.mark.spec("S-25.5", "D-29", "AT-40.7.1")
def test_a_pr_change_seen_again_is_not_recorded_again(w: W) -> None:
    p = w.confirmed("PR-CH", (TONER, "2", "100"))
    w.change("PR-CH", total_amount=w.header("PR-CH").total_amount + 1)
    first = w.sync_all().json()["observations"]
    assert [o["state_after"] for o in first] == ["PR_CHANGED_REVIEW_REQUIRED"]
    before = _state(w)
    # Seen again by the next run and by a one-PPR sync: nothing new is recorded.
    assert w.sync_all().status_code == 200
    assert w.sync_one(p["id"]).status_code == 200
    after = _state(w)
    assert after["ledger"] == before["ledger"]
    assert after["audit"]["PPR_PR_CHANGED"] == before["audit"]["PPR_PR_CHANGED"] == 1
    assert w.ppr(p["id"])["state"] == "PR_CHANGED_REVIEW_REQUIRED"


@pytest.mark.spec("S-25.5", "S-38.2", "D-30", "AT-40.8.2")
def test_a_retried_run_releases_a_cancellation_once(w: W) -> None:
    before = w.balance(TONER)
    p = w.confirmed("PR-RT", (TONER, "3", "100"))
    w.cancel_in_hosxp("PR-RT")
    w.gw.available = False
    failed = w.sync_all().json()["run"]
    assert failed["status"] == "FAILED"
    w.gw.available = True
    retry = w.sync_all(retry_of=failed["id"]).json()
    assert [o["outcome"] for o in retry["observations"]] == ["CANCELLED"]
    again = w.sync_all(retry_of=failed["id"])  # retrying the same failed run once more
    assert again.status_code in (200, 409)
    w.sync_all()
    assert len(w.releases(p["ppr_number"])) == 1
    assert w.balance(TONER) == before
    assert _state(w)["audit"]["PPR_CANCELLED_FROM_HOSXP"] == 1


@pytest.mark.spec("S-25.5", "D-38", "G-3", "D-30")
def test_demand_states_change_once_however_often_a_cancellation_is_seen(db: Engine) -> None:
    a = Assigned(Harness(db))
    d = a.draft("PR-DS", "30")
    assert a.confirm(d["id"]).status_code == 200
    a.cancel("PR-DS", d["id"])
    for _ in range(2):
        a.hx.client.post("/api/pr-sync", json={}, headers=a.plan.h)
        a.hx.client.post(f"/api/pprs/{d['id']}/sync", headers=a.plan.h)
        a.hx.client.post(f"/api/pprs/{d['id']}/recheck", headers=a.plan.h)
    assert a.states() == {ENT: "PLANNED", WARD: "PLANNED"}
    with a.hx.engine.connect() as c:
        rows = c.execute(
            text(
                "SELECT after->>'department_id', after->>'state_after' FROM audit_log "
                "WHERE action = 'DEMAND_STATE_CHANGED' ORDER BY id"
            )
        ).all()
    assert Counter(rows) == Counter(
        {
            (ENT, "INCLUDED_IN_CENTRAL_PURCHASE"): 1,
            (WARD, "INCLUDED_IN_CENTRAL_PURCHASE"): 1,
            (ENT, "PLANNED"): 1,
            (WARD, "PLANNED"): 1,
        }
    )
