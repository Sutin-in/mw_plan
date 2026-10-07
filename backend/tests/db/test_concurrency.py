"""Wave 10A: concurrency on a real PostgreSQL (spec §19, §38.1, §38.2; D-03, D-37, D-38).

Each test makes two or more requests overlap for real (threads, a barrier or a transaction held
open on purpose) and checks that the result is one of the serial outcomes: never a plan used
beyond its quantity or amount, never two numbers / bindings / versions for one confirmation,
never a demand covered twice, and never a deadlock (PostgreSQL would abort one request with
an error, which the tests treat as a failure).

Concurrency already covered elsewhere: two confirmations racing for one Plan Item
(test_ppr_api), two verifications / verification against an appointment change
(test_procurement_api), synchronization against confirmation and two cancellations
(test_sync_api).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from test_assigned_api import ENT, WARD, Assigned
from test_ppr_api import PAPER, TONER, World

from api_harness import FY, IN_FY2570, Harness
from ppr.application import amendment_services as am
from ppr.application.errors import ConflictError
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.integration.hosxp.contracts import HosxpPrStatus
from ppr.ports import Actor


def _parallel(*calls: Callable[[], Any], timeout: float = 30) -> list[Any]:
    """Run the calls at the same moment (a barrier releases them together)."""
    barrier = threading.Barrier(len(calls), timeout=10)
    results: list[Any] = [None] * len(calls)
    errors: list[BaseException] = []

    def run(i: int, call: Callable[[], Any]) -> None:
        try:
            barrier.wait()
            results[i] = call()
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout)
    assert not any(t.is_alive() for t in threads), "a request did not finish (deadlock?)"
    assert not errors, errors
    return results


def _code(r: Any) -> str:
    return str(r.json()["detail"]["code"]) if r.status_code >= 400 else "OK"


def _waits_for_a_lock(engine: Engine, seconds: float = 20) -> bool:
    """True once another session of this database is blocked on a lock (pg_stat_activity)."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with engine.connect() as c:
            n = c.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                )
            ).scalar_one()
        if n:
            return True
        time.sleep(0.1)
    return False


def _uid(engine: Engine, username: str) -> int:
    with engine.connect() as c:
        return int(
            c.execute(
                text("SELECT id FROM app_user WHERE username = :u"), {"u": username}
            ).scalar_one()
        )


@pytest.fixture
def w(db: Engine) -> World:
    return World(Harness(db))


@pytest.fixture
def a(db: Engine) -> Assigned:
    return Assigned(Harness(db))


# ------------------------------------------------------------------ confirmation
@pytest.mark.spec("S-38.1", "S-38.2", "D-03")
def test_a_double_submitted_confirmation_confirms_once(w: World) -> None:
    d = w.draft("PR-DBL", (TONER, "2", "100"))
    results = _parallel(lambda: w.confirm(d["id"]), lambda: w.confirm(d["id"]))
    assert sorted(_code(r) for r in results) == ["INVALID_STATE", "OK"]
    assert w.count("ppr_version") == 1 and w.count("ppr_binding") == 1
    with w.hx.engine.connect() as c:
        rows = c.execute(
            text("SELECT count(*) FROM plan_ledger WHERE event_type = 'PPR_CONFIRMED'")
        ).scalar_one()
    assert rows == 1
    assert w.balance(TONER) == ("8.0000", "800.00")


@pytest.mark.spec("S-17", "S-38.2", "AT-40.1.5")
def test_parallel_confirmations_get_distinct_consecutive_numbers(w: World) -> None:
    drafts = [w.draft(f"PR-NUM-{i}", (PAPER, "1", "50")) for i in range(6)]
    results = _parallel(*(lambda d=d: w.confirm(d["id"]) for d in drafts))
    assert [_code(r) for r in results] == ["OK"] * 6
    numbers = sorted(r.json()["ppr_number"] for r in results)
    assert len(set(numbers)) == 6
    assert [int(n.rsplit("-", 1)[1]) for n in numbers] == [1, 2, 3, 4, 5, 6]
    assert w.balance(PAPER) == ("4.0000", "200.00")


# ------------------------------------------------------------------ amendment
def _hold_amendment(
    engine: Engine, officer: int, req: am.AmendmentRequest, while_held: Callable[[], None]
) -> None:
    """Record an amendment and keep its transaction open while ``while_held`` runs."""
    with unit_of_work(engine) as uow:
        am.amend_plan(uow, Actor(officer), FY, req, IN_FY2570)
        while_held()


@pytest.mark.spec("S-19", "D-37", "S-20")
def test_a_confirmation_waits_for_an_amendment_and_sees_the_amended_plan(w: World) -> None:
    d = w.draft("PR-AMW", (TONER, "8", "100"))
    officer = _uid(w.hx.engine, "officer")
    result: list[Any] = []

    def confirm_now() -> None:
        t = threading.Thread(target=lambda: result.append(w.confirm(d["id"])))
        t.start()
        assert _waits_for_a_lock(w.hx.engine), "the confirmation must wait for the amendment"
        assert t.is_alive()
        result.append(t)

    req = am.AmendmentRequest(
        approval_document_no="D-W",
        approval_date=IN_FY2570,
        reason="r",
        items=(am.ItemChangeRequest(w.pid(TONER), planned_qty=Decimal(5)),),
    )
    _hold_amendment(w.hx.engine, officer, req, confirm_now)
    thread = result.pop()
    thread.join(30)
    (r,) = result
    assert r.status_code == 409  # judged against the amended plan: 8 > 5
    assert "QTY_EXCEEDED" in {x["code"] for x in r.json()["detail"]["details"]}
    assert w.balance(TONER) == ("5.0000", "1000.00")


@pytest.mark.spec("D-37", "S-20")
def test_two_amendments_from_the_same_form_record_one(w: World) -> None:
    officer = _uid(w.hx.engine, "officer")
    base = am.AmendmentRequest(
        approval_document_no="D-1",
        approval_date=IN_FY2570,
        reason="r",
        items=(am.ItemChangeRequest(w.pid(PAPER), estimated_unit_price=Decimal(40)),),
        base_amendment_no=0,
    )
    outcome: list[str] = []

    def second() -> None:
        def run() -> None:
            try:
                with unit_of_work(w.hx.engine) as uow:
                    am.amend_plan(uow, Actor(officer), FY, base, IN_FY2570)
                outcome.append("OK")
            except ConflictError as exc:
                outcome.append(exc.code)

        t = threading.Thread(target=run)
        t.start()
        assert _waits_for_a_lock(w.hx.engine), "the second amendment must wait for the first"
        assert t.is_alive()
        outcome.append("waiting")
        threads.append(t)

    threads: list[threading.Thread] = []
    _hold_amendment(w.hx.engine, officer, base, second)
    threads[0].join(30)
    assert outcome == ["waiting", "PLAN_CHANGED_SINCE_FORM"]
    assert w.count("plan_amendment") == 1


# ------------------------------------------------------------------ Assigned Purchase
@pytest.mark.spec("D-38", "G-3", "S-19")
def test_two_purchases_never_cover_one_demand_beyond_its_need(a: Assigned) -> None:
    d1 = a.draft("PR-CV1", "10")
    d2 = a.draft("PR-CV2", "10")
    assert a.put_coverage(d1["id"], ent="10").status_code == 200
    assert a.put_coverage(d2["id"], ent="10").status_code == 200
    results = _parallel(lambda: a.confirm(d1["id"]), lambda: a.confirm(d2["id"]))
    assert sorted(_code(r) for r in results) == ["COVERAGE_INVALID", "OK"]
    refused = next(r for r in results if r.status_code == 409)
    assert {x["code"] for x in refused.json()["detail"]["details"]} == {"COVERAGE_EXCEEDS_NEED"}
    with a.hx.engine.connect() as c:
        covered = c.execute(
            text(
                "SELECT COALESCE(SUM(qty), 0) FROM ppr_coverage "
                "WHERE confirmed AND department_source_id = :d"
            ),
            {"d": ENT},
        ).scalar_one()
    assert covered == 10
    assert a.states() == {ENT: "INCLUDED_IN_CENTRAL_PURCHASE", WARD: "PLANNED"}


@pytest.mark.spec("D-38", "G-3", "S-19", "D-30")
def test_verification_and_cancellation_of_covering_pprs_do_not_deadlock(a: Assigned) -> None:
    for round_no in range(3):
        d1 = a.draft(f"PR-VX{round_no}", "10")
        d2 = a.draft(f"PR-VY{round_no}", "20")
        assert a.put_coverage(d1["id"], ent="10").status_code == 200
        assert a.put_coverage(d2["id"], ward="20").status_code == 200
        assert a.confirm(d1["id"]).status_code == 200
        assert a.confirm(d2["id"]).status_code == 200
        ppr1 = a.hx.client.get(f"/api/pprs/{d1['id']}", headers=a.store).json()
        gw: Any = a.hx.gateway
        h = gw.data.prs[f"PR-VY{round_no}"]
        gw.replace_pr(
            h.model_copy(
                update={"status": HosxpPrStatus.CANCELLED, "native_status": "MOCK-CANCELLED"}
            ),
            list(gw.data.pr_items[f"PR-VY{round_no}"]),
        )
        v, s = _parallel(
            lambda p=ppr1: a.verify(p),
            lambda i=d2["id"]: a.hx.client.post(f"/api/pprs/{i}/sync", headers=a.plan.h),
        )
        assert (v.status_code, s.status_code) == (200, 200), (v.text, s.text)
        assert a.states() == {ENT: "FULFILLED", WARD: "PLANNED"}
        # free the plan for the next round: cancel the verified one too
        a.cancel(f"PR-VX{round_no}", d1["id"])
        assert a.states() == {ENT: "PLANNED", WARD: "PLANNED"}
