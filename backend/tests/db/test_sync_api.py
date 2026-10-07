"""Wave 6: PR synchronization, HOSxP cancellation, invalid evidence, governance re-check.

Spec §14.6, §16, §25, §26, §40.6 ... §40.8; D-02, D-03, D-29, D-30, D-31.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from api_harness import FY, Harness, Plan
from ppr.application import sync_services as sync
from ppr.cli import run_nightly
from ppr.domain.ppr_state import PprState
from ppr.domain.pr_sync import SyncScope
from ppr.domain.roles import Role
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.integration.hosxp.contracts import PrHeader, PrItem
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpUnavailableError
from ppr.integration.hosxp.mock import MockHosxpGateway
from ppr.integration.hosxp.mock.fixtures import sample_data
from ppr.integration.hosxp.status import HosxpPrStatus

TONER, PAPER = "MOCK-ITEM-TONER", "MOCK-ITEM-PAPER"
OFFICE = "MOCK-CAT-OFFICE"


class ScriptedGateway(MockHosxpGateway):
    """Mock HOSxP with hooks: block a read, fail a read, or answer a scripted sequence."""

    def __init__(self) -> None:
        super().__init__(sample_data())
        self.before_get_pr: Any = None  # callable(pr_no) run inside get_pr
        self.header_errors: dict[str, Exception] = {}
        self.items_script: dict[str, list[Sequence[PrItem]]] = {}
        self.header_script: dict[str, list[PrHeader | None]] = {}

    def get_pr(self, pr_no: str) -> PrHeader | None:
        self._enter("get_pr")
        if self.before_get_pr is not None:
            self.before_get_pr(pr_no)
        if pr_no in self.header_errors:
            raise self.header_errors[pr_no]
        if self.header_script.get(pr_no):
            return self.header_script[pr_no].pop(0)
        return self.data.prs.get(pr_no)

    def get_pr_items(self, pr_no: str) -> Sequence[PrItem]:
        self._enter("get_pr_items")
        if self.items_script.get(pr_no):
            return tuple(self.items_script[pr_no].pop(0))
        return tuple(self.data.pr_items.get(pr_no, ()))


class W:
    """FY2570 ACTIVE plan (toner 10 @ 100, paper 10 @ 50) and confirmed PPRs."""

    def __init__(self, db: Engine) -> None:
        self.gw = ScriptedGateway()
        self.hx = hx = Harness(db, self.gw)
        self.plan = p = Plan(hx)
        p.sync()
        p.year()
        office = p.budget(OFFICE, "1500").json()["id"]
        assert p.item(office, "1000").status_code == 201
        assert p.item(office, "500", item_id=PAPER).status_code == 201
        assert p.move("APPROVED").status_code == 200
        assert p.move("ACTIVE").status_code == 200
        self.req = hx.h(hx.user("req_ent", Role.REQUESTER))
        self.officer = p.h
        self.admin = hx.h(hx.user("admin", Role.ADMIN))
        self.proc = hx.h(hx.user("proc", Role.PROCUREMENT))
        self.head = hx.h(hx.user("head", Role.HEAD_OF_PROCUREMENT))
        items = hx.client.get(f"/api/plan-years/{FY}/items", headers=p.h).json()
        self.pid = {i["item_id"]: i["id"] for i in items}

    # -------------------------------------------------------------- helpers
    def confirmed(self, pr_no: str, *lines: tuple[str, str, str]) -> dict[str, Any]:
        self.hx.put_pr(pr_no, *lines)
        r = self.hx.create_ppr(pr_no, self.req)
        assert r.status_code == 201, r.text
        c = self.hx.client.post(f"/api/pprs/{r.json()['id']}/confirm", headers=self.req)
        assert c.status_code == 200, c.text
        return dict(c.json())

    def header(self, pr_no: str) -> PrHeader:
        return self.gw.data.prs[pr_no]

    def change(self, pr_no: str, items: Sequence[PrItem] | None = None, **over: Any) -> None:
        h = self.header(pr_no).model_copy(update=over)
        self.gw.replace_pr(h, list(items if items is not None else self.gw.data.pr_items[pr_no]))

    def cancel_in_hosxp(self, pr_no: str, **over: Any) -> None:
        # A mock native value; the real mapping is empty until IT evidence exists (D-30).
        self.change(pr_no, status=HosxpPrStatus.CANCELLED, native_status="MOCK-CANCELLED", **over)

    def sync_all(self, headers: dict[str, str] | None = None, **body: Any) -> Any:
        return self.hx.client.post("/api/pr-sync", json=body, headers=headers or self.officer)

    def sync_one(self, ppr_id: int, headers: dict[str, str] | None = None) -> Any:
        return self.hx.client.post(f"/api/pprs/{ppr_id}/sync", headers=headers or self.officer)

    def ppr(self, ppr_id: int) -> dict[str, Any]:
        r = self.hx.client.get(f"/api/pprs/{ppr_id}", headers=self.officer)
        return dict(r.json())

    def balance(self, item: str) -> tuple[str, str]:
        j = self.hx.client.get(
            f"/api/plan-items/{self.pid[item]}/balance", headers=self.officer
        ).json()
        return j["remaining_qty"], j["remaining_amount"]

    def rows(self, sql: str, **params: Any) -> list[Any]:
        with self.hx.engine.connect() as c:
            return list(c.execute(text(sql), params).all())

    def releases(self, ppr_number: str) -> list[Any]:
        return self.rows(
            "SELECT plan_item_id, qty_delta, amount_delta, idempotency_key FROM plan_ledger "
            "WHERE event_type = 'PPR_RELEASED' AND ppr_ref = :r ORDER BY id",
            r=ppr_number,
        )

    def unlock(self, ppr_id: int) -> None:
        r = self.hx.client.post(
            f"/api/pprs/{ppr_id}/unlock", json={"reason": "review"}, headers=self.officer
        )
        assert r.status_code == 200, r.text


@pytest.fixture
def w(db: Engine) -> W:
    return W(db)


def _only(result: Any) -> dict[str, Any]:
    body = result.json()
    assert result.status_code == 200, body
    assert len(body["observations"]) == 1, body
    return dict(body["observations"][0])


# ================================================================== basics / logging
@pytest.mark.spec("D-29", "AT-40.6.2", "AT-40.6.4", "S-25.4")
def test_manual_sync_logs_run_and_unchanged_observation(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    r = w.sync_all()
    body = r.json()
    assert body["run"]["mode"] == "MANUAL" and body["run"]["scope"] == "PPRS"
    assert body["run"]["status"] == "SUCCEEDED" and body["run"]["checked"] == 1
    assert body["run"]["requested_by_user_id"] is not None
    (obs,) = body["observations"]
    assert (obs["ppr_id"], obs["outcome"], obs["state_after"]) == (
        p["id"],
        "UNCHANGED",
        "CONFIRMED_LOCKED",
    )
    assert obs["observed_at"] is not None and obs["compared_version"] == 1
    # live_pr: the canonical snapshot form, identical to what the confirmation stored.
    snap = w.rows("SELECT snapshot FROM ppr_version WHERE ppr_id = :i", i=p["id"])[0][0]
    assert obs["live_pr"] == {"header": snap["pr"], "items": snap["pr_items"]}
    # Repeating the sync creates a new observation, never a duplicate state/ledger effect.
    ledger = w.rows("SELECT count(*) FROM plan_ledger")[0][0]
    assert w.sync_all().json()["observations"][0]["outcome"] == "UNCHANGED"
    assert w.rows("SELECT count(*) FROM plan_ledger")[0][0] == ledger
    assert w.ppr(p["id"])["state"] == "CONFIRMED_LOCKED"


@pytest.mark.spec("D-29", "D-02")
def test_drafts_are_never_synchronized(w: W) -> None:
    w.hx.put_pr("PR-D", (TONER, "1", "100"))
    d = w.hx.create_ppr("PR-D", w.req).json()
    body = w.sync_all().json()
    assert body["observations"] == [] and body["run"]["checked"] == 0
    r = w.sync_one(d["id"])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PPR_NOT_SYNCABLE"
    assert w.ppr(d["id"])["state"] == "DRAFT"


# ================================================================== PR changed
@pytest.mark.spec("D-29", "D-10", "S-16", "AT-40.7.1", "AT-40.7.2")
def test_critical_change_moves_to_review_and_never_releases(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    before = w.balance(TONER)
    items = w.gw.data.pr_items["PR-1"]
    changed = [items[0].model_copy(update={"qty": Decimal(3), "amount": Decimal("300.00")})]
    w.change("PR-1", changed, total_amount=Decimal("300.00"))
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["state_before"], obs["state_after"]) == (
        "CRITICAL_CHANGE",
        "CONFIRMED_LOCKED",
        "PR_CHANGED_REVIEW_REQUIRED",
    )
    assert {c["field"] for c in obs["changes"] if c["critical"]} >= {"qty", "amount"}
    assert obs["released"] is False
    assert w.balance(TONER) == before  # critical change never releases
    assert w.releases(p["ppr_number"]) == []
    after = w.ppr(p["id"])
    assert (after["state"], after["version"], after["ppr_number"]) == (
        "PR_CHANGED_REVIEW_REQUIRED",
        1,
        p["ppr_number"],
    )
    assert ("PPR_PR_CHANGED", "ppr", str(p["id"])) in w.hx.audit_actions()
    # Still in review: a second sync keeps it there (the transition is not repeated).
    obs2 = _only(w.sync_one(p["id"]))
    assert (obs2["state_before"], obs2["state_after"]) == ("PR_CHANGED_REVIEW_REQUIRED",) * 2


@pytest.mark.spec("D-29", "D-02")
def test_non_critical_change_is_audited_and_keeps_state(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.change("PR-1", requester_name="renamed in HOSxP")
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["state_after"]) == ("NON_CRITICAL_CHANGE", "CONFIRMED_LOCKED")
    assert ("PPR_PR_NON_CRITICAL_CHANGE", "ppr", str(p["id"])) in w.hx.audit_actions()


@pytest.mark.spec("D-29", "D-02")
def test_display_status_updated_without_touching_business_timestamp(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.change("PR-1", status=HosxpPrStatus.APPROVED, native_status="MOCK-APPROVED")
    updated = w.ppr(p["id"])["updated_at"]
    obs = _only(w.sync_one(p["id"]))
    assert obs["outcome"] == "NON_CRITICAL_CHANGE"
    after = w.ppr(p["id"])
    assert after["hosxp_pr_status"] == "APPROVED" and after["updated_at"] == updated


# ================================================================== cancellation
@pytest.mark.spec("D-30", "S-14.6", "AT-40.8.1", "AT-40.8.2", "AT-40.8.3", "S-25.5")
def test_cancellation_releases_exactly_once(w: W) -> None:
    before = w.balance(TONER)
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    assert w.balance(TONER) != before
    w.cancel_in_hosxp("PR-1")
    body = w.sync_all().json()
    (obs,) = body["observations"]
    assert (obs["outcome"], obs["state_after"], obs["released"]) == ("CANCELLED", "CANCELLED", True)
    assert body["run"]["cancelled"] == 1
    assert w.balance(TONER) == before  # usage released correctly
    rel = w.releases(p["ppr_number"])
    assert [(r[0], Decimal(r[1]), Decimal(r[2]), r[3]) for r in rel] == [
        (
            w.pid[TONER],
            Decimal(2),
            Decimal("200.00"),
            f"release:{p['ppr_number']}:{w.pid[TONER]}",
        )
    ]
    after = w.ppr(p["id"])
    assert (after["state"], after["ppr_number"]) == ("CANCELLED", p["ppr_number"])
    # Repeated sync: a cancelled PPR is out of scope; nothing is released again.
    assert w.sync_all().json()["observations"] == []
    r = w.sync_one(p["id"])
    assert r.status_code == 409
    recheck = _only(w.hx.client.post(f"/api/pprs/{p['id']}/recheck", headers=w.officer))
    assert (recheck["outcome"], recheck["released"]) == ("CANCELLED", False)
    assert len(w.releases(p["ppr_number"])) == 1
    assert w.balance(TONER) == before
    # D-03: the PR stays bound; a new PPR for it is refused.
    r = w.hx.create_ppr("PR-1", w.req)
    assert r.status_code == 409
    actions = [a for a, _, e in w.hx.audit_actions() if e == str(p["id"])]
    assert "PPR_CANCELLED_FROM_HOSXP" in actions


@pytest.mark.spec("D-30", "D-22")
def test_cancellation_after_reconfirmation_releases_what_is_still_held(w: W) -> None:
    before_t, before_p = w.balance(TONER), w.balance(PAPER)
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.unlock(p["id"])
    items = [
        PrItem(
            pr_item_id="PR-1-9",
            item_id=PAPER,
            qty=Decimal(3),
            unit_price=Decimal(50),
            amount=Decimal("150.00"),
        )
    ]
    w.change("PR-1", items, total_amount=Decimal("150.00"))
    a = w.hx.client.put(
        f"/api/pprs/{p['id']}/allocations",
        json={"allocations": [{"budget_category_id": OFFICE, "amount": "150.00"}]},
        headers=w.req,
    )
    assert a.status_code == 200, a.text
    # D-43: the PR's new line needs its plan row chosen before reconfirmation
    assert w.hx.rechoose(p["id"], "PR-1", w.req).status_code == 200
    c = w.hx.client.post(f"/api/pprs/{p['id']}/confirm", headers=w.req)
    assert c.status_code == 200, c.text
    w.cancel_in_hosxp("PR-1")
    obs = _only(w.sync_one(p["id"]))
    assert obs["released"] is True and obs["compared_version"] == 2
    assert (w.balance(TONER), w.balance(PAPER)) == (before_t, before_p)
    # Only the paper line was still held; the toner usage was released by reconfirmation.
    assert [r[0] for r in w.releases(p["ppr_number"])] == [w.pid[TONER], w.pid[PAPER]]
    keys = [r[3] for r in w.releases(p["ppr_number"])]
    assert keys[1] == f"release:{p['ppr_number']}:{w.pid[PAPER]}"


@pytest.mark.spec("D-31", "D-30")
def test_cancellation_wins_over_invalid_control_evidence(w: W) -> None:
    before = w.balance(TONER)
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.cancel_in_hosxp("PR-1", total_amount=Decimal("999.00"))  # malformed total
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["released"]) == ("CANCELLED", True)
    assert w.balance(TONER) == before


@pytest.mark.spec("D-31", "D-30")
def test_unreliable_binding_never_cancels_nor_changes_state(w: W) -> None:
    before = w.balance(TONER)
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    held = w.balance(TONER)
    w.cancel_in_hosxp("PR-1", pr_id="ANOTHER-INTERNAL-ID")
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["evidence_class"]) == ("INVALID_EVIDENCE", "BINDING")
    assert "PR_ID_MISMATCH" in {i["code"] for i in obs["evidence_issues"]}
    assert (obs["state_after"], obs["released"]) == ("CONFIRMED_LOCKED", False)
    assert w.balance(TONER) == held != before
    gov = w.hx.client.get("/api/pr-sync/governance", headers=w.officer).json()
    assert [g["id"] for g in gov] == [obs["id"]]
    # The display status is not taken from evidence that cannot be tied to the PR.
    assert w.ppr(p["id"])["hosxp_pr_status"] == "ACTIVE"


@pytest.mark.spec("D-31")
def test_query_configuration_failure_changes_no_state(w: W) -> None:
    a = w.confirmed("PR-1", (TONER, "1", "100"))
    b = w.confirmed("PR-2", (TONER, "1", "100"))
    w.gw.header_errors["PR-1"] = HosxpConfigurationError("get_pr returned 2 rows for one PR")
    w.gw.header_errors["PR-2"] = HosxpConfigurationError("get_pr: missing column(s)")
    body = w.sync_all().json()
    assert {
        (o["outcome"], o["evidence_class"], o["state_after"]) for o in body["observations"]
    } == {("INVALID_EVIDENCE", "BINDING", "CONFIRMED_LOCKED")}
    assert body["run"]["status"] == "FAILED"  # nothing trustworthy was observed
    assert {w.ppr(a["id"])["state"], w.ppr(b["id"])["state"]} == {"CONFIRMED_LOCKED"}


@pytest.mark.spec("D-30")
def test_not_found_is_governance_only(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    held = w.balance(TONER)
    del w.gw.data.prs["PR-1"]
    for _ in range(3):  # repeated NOT_FOUND never becomes a cancellation
        obs = _only(w.sync_one(p["id"]))
        assert (obs["outcome"], obs["state_after"], obs["released"]) == (
            "NOT_FOUND",
            "CONFIRMED_LOCKED",
            False,
        )
    assert w.balance(TONER) == held
    gov = w.hx.client.get("/api/pr-sync/governance", headers=w.admin).json()
    assert [(g["ppr_id"], g["outcome"]) for g in gov] == [(p["id"], "NOT_FOUND")]  # one row
    t = w.hx.client.get(f"/api/pprs/{p['id']}/timeline", headers=w.req).json()
    assert [e["action"] for e in t["events"]].count("HOSXP_NOT_FOUND") == 1  # shown once
    # Once the PR is found again, the PPR leaves the governance list.
    w.hx.put_pr("PR-1", (TONER, "2", "100"))
    assert _only(w.sync_one(p["id"]))["outcome"] == "UNCHANGED"
    assert w.hx.client.get("/api/pr-sync/governance", headers=w.admin).json() == []


# ================================================================== invalid evidence
@pytest.mark.spec("D-31", "D-17")
def test_control_invalid_evidence_moves_locked_to_review_and_blocks_reconfirmation(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    held = w.balance(TONER)
    w.change("PR-1", total_amount=Decimal("250.00"))
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["evidence_class"], obs["state_after"]) == (
        "INVALID_EVIDENCE",
        "CONTROL",
        "PR_CHANGED_REVIEW_REQUIRED",
    )
    assert [(i["code"], i["field"]) for i in obs["evidence_issues"]] == [
        ("PR_TOTAL_MISMATCH", "total_amount")
    ]
    assert obs["changes"] is None  # not presented as a classifier diff
    assert obs["details"]["discarded_first_read"]["evidence_issues"][0]["code"] == (
        "PR_TOTAL_MISMATCH"
    )
    # choices+draft+confirm, then one read + ONE re-read
    assert w.gw.calls.count("get_pr") == 3 + 2
    assert w.balance(TONER) == held
    w.unlock(p["id"])
    obs = _only(w.sync_one(p["id"]))
    assert (obs["state_before"], obs["state_after"]) == ("UNLOCKED_FOR_REVISION",) * 2
    c = w.hx.client.post(f"/api/pprs/{p['id']}/confirm", headers=w.req)
    assert c.status_code == 409  # reconfirmation keeps failing while the evidence is bad


@pytest.mark.spec("D-31", "D-23")
def test_precision_is_not_reread_and_moves_verified_to_review(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    with unit_of_work(w.hx.engine) as uow:
        uow.pprs.set_state(p["id"], PprState.PROCUREMENT_VERIFIED)
    items = w.gw.data.pr_items["PR-1"]
    bad = [items[0].model_copy(update={"amount": Decimal("200.001")})]
    w.change("PR-1", bad, total_amount=Decimal("200.001"))
    calls = len(w.gw.calls)
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["evidence_class"], obs["state_after"]) == (
        "INVALID_EVIDENCE",
        "CONTROL",
        "PR_CHANGED_REVIEW_REQUIRED",
    )
    assert {(i["code"], i["line"]) for i in obs["evidence_issues"]} == {
        ("PR_VALUE_PRECISION", "PR-1-1")
    }
    assert w.gw.calls[calls:] == ["get_pr", "get_pr_items"]  # no re-read for precision
    assert obs["details"] is None


@pytest.mark.spec("D-29", "D-31")
def test_transient_inconsistency_is_resolved_by_one_fresh_read(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.gw.items_script["PR-1"] = [[]]  # first read: header, then (mid-edit) no items
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["state_after"]) == ("UNCHANGED", "CONFIRMED_LOCKED")
    first = obs["details"]["discarded_first_read"]
    assert [i["code"] for i in first["evidence_issues"]] == ["PR_HAS_NO_ITEMS"]
    assert first["live_pr"]["items"] == []


# ================================================================== unavailable / retry
@pytest.mark.spec("D-29", "S-26", "AT-40.6.3", "AT-40.6.5")
def test_unavailable_changes_nothing_and_run_stops_early_then_retry(w: W) -> None:
    ps = [w.confirmed(f"PR-{n}", (TONER, "1", "100")) for n in range(1, 6)]
    w.gw.available = False
    body = w.sync_all().json()
    run = body["run"]
    assert run["status"] == "FAILED"
    assert [o["outcome"] for o in body["observations"]] == ["UNAVAILABLE"] * 3
    assert run["not_attempted"] == 2
    assert run["error_details"]["reason"] == "HOSXP_UNAVAILABLE"
    assert all(o["observed_at"] is None for o in body["observations"])
    assert {w.ppr(p["id"])["state"] for p in ps} == {"CONFIRMED_LOCKED"}
    w.gw.available = True
    retry = w.sync_all(retry_of=run["id"]).json()
    assert (
        retry["run"]["scope"] == "PPR_RETRY" and retry["run"]["retry_of_sync_run_id"] == run["id"]
    )
    assert sorted(o["ppr_id"] for o in retry["observations"]) == sorted(p["id"] for p in ps)
    assert retry["run"]["status"] == "SUCCEEDED"


@pytest.mark.spec("D-29", "AT-40.6.5")
def test_partial_run_is_visible(w: W) -> None:
    a = w.confirmed("PR-1", (TONER, "1", "100"))
    w.confirmed("PR-2", (TONER, "1", "100"))
    w.gw.header_errors["PR-1"] = HosxpUnavailableError("down for PR-1")
    body = w.sync_all().json()
    assert body["run"]["status"] == "PARTIAL"
    detail = w.hx.client.get(f"/api/pr-sync/runs/{body['run']['id']}", headers=w.proc).json()
    assert {(o["ppr_id"], o["outcome"]) for o in detail["observations"]} >= {
        (a["id"], "UNAVAILABLE")
    }


# ================================================================== governance re-check
@pytest.mark.spec("D-30", "D-03")
def test_recheck_of_cancelled_ppr_reactivated_externally_is_an_anomaly(w: W) -> None:
    before = w.balance(TONER)
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.cancel_in_hosxp("PR-1")
    assert _only(w.sync_one(p["id"]))["outcome"] == "CANCELLED"
    w.change("PR-1", status=HosxpPrStatus.ACTIVE, native_status="MOCK-ACTIVE")
    obs = _only(w.hx.client.post(f"/api/pprs/{p['id']}/recheck", headers=w.admin))
    assert (obs["outcome"], obs["anomaly_code"], obs["state_after"], obs["released"]) == (
        "ANOMALY",
        "CANCELLED_PR_REACTIVATED_EXTERNALLY",
        "CANCELLED",
        False,
    )
    assert w.ppr(p["id"])["state"] == "CANCELLED"
    assert w.balance(TONER) == before  # no re-post of plan usage
    assert len(w.releases(p["ppr_number"])) == 1
    assert "ANOMALY" in {
        g["outcome"] for g in w.hx.client.get("/api/pr-sync/governance", headers=w.officer).json()
    }
    # UNKNOWN status is never an anomaly (D-30).
    w.change("PR-1", status=HosxpPrStatus.UNKNOWN, native_status=None)
    obs = _only(w.hx.client.post(f"/api/pprs/{p['id']}/recheck", headers=w.admin))
    assert (obs["outcome"], obs["anomaly_code"]) == ("NO_AUTHORITATIVE_STATUS", None)


@pytest.mark.spec("D-30")
def test_recheck_only_for_cancelled_pprs_and_nightly_skips_cancelled(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    r = w.hx.client.post(f"/api/pprs/{p['id']}/recheck", headers=w.officer)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_CANCELLED"
    w.cancel_in_hosxp("PR-1")
    run_nightly(w.hx.engine, w.gw)
    n = len(w.gw.calls)
    assert run_nightly(w.hx.engine, w.gw) == 0
    assert w.gw.calls[n:] == []  # the cancelled PPR is not scanned nightly


# ================================================================== nightly + permissions
@pytest.mark.spec("D-29", "AT-40.6.1", "S-25.3")
def test_nightly_command_logs_a_nightly_run(w: W) -> None:
    w.confirmed("PR-1", (TONER, "2", "100"))
    assert run_nightly(w.hx.engine, w.gw) == 0
    (mode, scope, status, by) = w.rows(
        "SELECT mode, scope, status, requested_by_user_id FROM sync_run "
        "WHERE scope <> 'MASTERS' ORDER BY id DESC LIMIT 1"
    )[0]
    assert (mode, scope, status, by) == ("NIGHTLY", "PPRS", "SUCCEEDED", None)
    runs = w.hx.client.get("/api/pr-sync/runs", headers=w.head).json()
    assert runs[0]["mode"] == "NIGHTLY"


@pytest.mark.spec("D-29", "S-22.9")
def test_only_plan_officer_and_admin_trigger_sync(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    for h in (w.proc, w.head, w.req):
        assert w.sync_all(h).status_code == 403
        assert w.sync_one(p["id"], h).status_code == 403
        assert w.hx.client.post(f"/api/pprs/{p['id']}/recheck", headers=h).status_code == 403
        assert w.hx.client.get("/api/pr-sync/governance", headers=h).status_code == 403
    assert w.sync_all(w.admin).status_code == 200
    for h in (w.proc, w.head, w.officer, w.admin):
        assert w.hx.client.get("/api/pr-sync/runs", headers=h).status_code == 200
    assert w.hx.client.get("/api/pr-sync/runs", headers=w.req).status_code == 403
    obs = w.hx.client.get(f"/api/pprs/{p['id']}/observations", headers=w.req)
    assert obs.status_code == 200 and len(obs.json()) == 1  # own department's PPR
    other = w.hx.h(w.hx.user("req_or", Role.REQUESTER, department="MOCK-DEP-OR"))
    assert w.hx.client.get(f"/api/pprs/{p['id']}/observations", headers=other).status_code == 404


# ================================================================== runs + evidence integrity
@pytest.mark.spec("D-29", "S-25.5")
def test_one_full_run_at_a_time_and_stale_runs_are_closed(w: W) -> None:
    w.confirmed("PR-1", (TONER, "2", "100"))
    with w.hx.engine.begin() as c:
        c.execute(
            text("INSERT INTO sync_run (mode, scope, status) VALUES ('NIGHTLY','PPRS','RUNNING')")
        )
    r = w.sync_all()
    assert r.status_code == 409 and r.json()["detail"]["code"] == "SYNC_ALREADY_RUNNING"
    with w.hx.engine.begin() as c:
        c.execute(
            text(
                "UPDATE sync_run SET started_at = now() - interval '7 hours' "
                "WHERE status = 'RUNNING'"
            )
        )
    assert w.sync_all().status_code == 200
    stale = w.rows("SELECT status, error_details FROM sync_run WHERE mode = 'NIGHTLY'")
    assert stale == [("FAILED", {"reason": "STALE_RUN"})]


@pytest.mark.spec("D-29", "D-30")
def test_observations_are_append_only_and_release_is_unique(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.cancel_in_hosxp("PR-1")
    obs = _only(w.sync_one(p["id"]))
    for stmt in (
        "UPDATE ppr_sync_observation SET outcome = 'UNCHANGED'",
        "DELETE FROM ppr_sync_observation",
    ):
        with pytest.raises(DBAPIError), w.hx.engine.begin() as c:
            c.execute(text(stmt))
    with pytest.raises(DBAPIError), w.hx.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO ppr_sync_observation (sync_run_id, ppr_id, pr_no, outcome, "
                "state_before, state_after, released) VALUES (:r, :p, 'PR-1', 'CANCELLED', "
                "'CONFIRMED_LOCKED', 'CANCELLED', true)"
            ),
            {"r": obs["sync_run_id"], "p": p["id"]},
        )
    with pytest.raises(DBAPIError), w.hx.engine.begin() as c:  # never CLOSED (G-4)
        c.execute(
            text(
                "INSERT INTO ppr_sync_observation (sync_run_id, ppr_id, pr_no, outcome, "
                "state_before, state_after) VALUES (:r, :p, 'PR-1', 'UNCHANGED', "
                "'CANCELLED', 'CLOSED')"
            ),
            {"r": obs["sync_run_id"], "p": p["id"]},
        )


# ================================================================== concurrency
@pytest.mark.spec("D-29")
def test_hosxp_is_read_without_holding_the_ppr_lock(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    reading, proceed = threading.Event(), threading.Event()
    lock_ok: list[bool] = []

    def block(_: str) -> None:
        reading.set()
        assert proceed.wait(10)

    w.gw.before_get_pr = block
    result: list[Any] = []
    t = threading.Thread(target=lambda: result.append(w.sync_one(p["id"])))
    t.start()
    assert reading.wait(10)
    # While the sync waits for HOSxP, the PPR row can be locked at once by someone else.
    with w.hx.engine.begin() as c:
        c.execute(text("SET LOCAL lock_timeout = '1s'"))
        c.execute(text("SELECT id FROM ppr WHERE id = :i FOR UPDATE NOWAIT"), {"i": p["id"]})
        lock_ok.append(True)
    proceed.set()
    t.join(20)
    assert lock_ok == [True]
    assert result[0].status_code == 200


@pytest.mark.spec("D-29")
def test_state_moved_during_read_is_reassessed_against_the_locked_state(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    items = w.gw.data.pr_items["PR-1"]
    w.change(
        "PR-1",
        [items[0].model_copy(update={"qty": Decimal(3), "amount": Decimal("300.00")})],
        total_amount=Decimal("300.00"),
    )
    reading, proceed = threading.Event(), threading.Event()

    first = [True]

    def block(_: str) -> None:
        if first.pop() if first else False:  # only the sync's own (first) read waits
            reading.set()
            assert proceed.wait(10)

    w.gw.before_get_pr = block
    result: list[Any] = []
    t = threading.Thread(target=lambda: result.append(w.sync_one(p["id"])), name="sync")
    t.start()
    assert reading.wait(10)
    w.unlock(p["id"])  # the PPR moves while HOSxP is being read
    proceed.set()
    t.join(20)
    obs = _only(result[0])
    assert (obs["outcome"], obs["state_before"], obs["state_after"]) == (
        "CRITICAL_CHANGE",
        "UNLOCKED_FOR_REVISION",
        "UNLOCKED_FOR_REVISION",
    )


@pytest.mark.spec("D-29", "D-22")
def test_new_version_confirmed_during_read_is_skipped(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.unlock(p["id"])
    reading, proceed = threading.Event(), threading.Event()

    first = [True]

    def block(_: str) -> None:
        if first.pop() if first else False:  # only the sync's own (first) read waits
            reading.set()
            assert proceed.wait(10)

    w.gw.before_get_pr = block
    result: list[Any] = []
    t = threading.Thread(target=lambda: result.append(w.sync_one(p["id"])), name="sync")
    t.start()
    assert reading.wait(10)
    c = w.hx.client.post(f"/api/pprs/{p['id']}/confirm", headers=w.req)  # version 2
    assert c.status_code == 200, c.text
    proceed.set()
    t.join(20)
    obs = _only(result[0])
    assert (obs["outcome"], obs["state_after"], obs["compared_version"]) == (
        "SKIPPED_STATE_MOVED",
        "CONFIRMED_LOCKED",
        2,
    )


@pytest.mark.spec("D-30", "AT-40.8.3", "S-25.5")
def test_concurrent_cancellation_syncs_release_once(w: W) -> None:
    before = w.balance(TONER)
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.cancel_in_hosxp("PR-1")
    barrier = threading.Barrier(2, timeout=10)

    def both_read(_: str) -> None:
        barrier.wait()  # both workers are reading HOSxP before either applies

    w.gw.before_get_pr = both_read
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(w.sync_one(p["id"])), name=f"sync{i}")
        for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    outcomes = sorted(_only(r)["outcome"] for r in results)
    assert outcomes == ["CANCELLED", "SKIPPED_STATE_MOVED"]
    assert len(w.releases(p["ppr_number"])) == 1
    assert w.balance(TONER) == before
    assert w.rows("SELECT count(*) FROM ppr_sync_observation WHERE released")[0][0] == 1


def test_service_rejects_inconsistent_requests(w: W) -> None:
    from ppr.application.errors import InvalidInputError

    def factory() -> Any:
        return unit_of_work(w.hx.engine)

    with pytest.raises(InvalidInputError):
        sync.run_sync(factory, w.gw, mode="MANUAL", scope=SyncScope.PPRS, requested_by=None)
    with pytest.raises(InvalidInputError):
        sync.run_sync(factory, w.gw, mode="NIGHTLY", scope=SyncScope.PPR, requested_by=None)
    ok = sync.run_sync(
        factory,
        w.gw,
        mode="NIGHTLY",
        scope=SyncScope.PPRS,
        requested_by=None,
        clock=lambda: datetime.now(UTC) + timedelta(seconds=1),
    )
    assert ok.run.status == "SUCCEEDED"


@pytest.mark.spec("D-29", "S-33")
def test_timeline_shows_observed_in_hosxp_apart_from_system_actions(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.sync_one(p["id"])  # UNCHANGED: not history
    items = w.gw.data.pr_items["PR-1"]
    w.change(
        "PR-1",
        [items[0].model_copy(update={"qty": Decimal(3), "amount": Decimal("300.00")})],
        total_amount=Decimal("300.00"),
    )
    w.sync_one(p["id"])
    t = w.hx.client.get(f"/api/pprs/{p['id']}/timeline", headers=w.req).json()
    rows = [(e["action"], e["source"]) for e in t["events"]]
    assert ("HOSXP_CRITICAL_CHANGE", "HOSXP") in rows
    assert ("PPR_PR_CHANGED", "APP") in rows
    assert not any(a == "HOSXP_UNCHANGED" for a, _ in rows)
    hx = next(e for e in t["events"] if e["action"] == "HOSXP_CRITICAL_CHANGE")
    assert hx["actor_user_id"] is None and hx["state_after"] == "PR_CHANGED_REVIEW_REQUIRED"
    assert any(c["critical"] for c in hx["details"]["pr_changes"])
    q = w.hx.client.get("/api/procurement/queue?bucket=changed", headers=w.proc).json()
    assert [i["id"] for i in q["items"]] == [p["id"]]


@pytest.mark.spec("D-29")
def test_unexpected_error_is_that_ppr_only_with_a_safe_message(w: W) -> None:
    a = w.confirmed("PR-1", (TONER, "1", "100"))
    b = w.confirmed("PR-2", (TONER, "1", "100"))
    w.gw.header_errors["PR-1"] = RuntimeError("secret connection detail postgres://u:p@h")
    body = w.sync_all().json()
    got = {o["ppr_id"]: o for o in body["observations"]}
    assert got[a["id"]]["outcome"] == "ERROR"
    assert got[a["id"]]["error_code"] == "RuntimeError"
    assert "secret" not in (got[a["id"]]["error_message"] or "")
    assert got[b["id"]]["outcome"] == "UNCHANGED"  # the run went on
    assert body["run"]["status"] == "PARTIAL"
    retry = w.sync_all(retry_of=body["run"]["id"]).json()
    assert [o["ppr_id"] for o in retry["observations"]] == [a["id"]]


@pytest.mark.spec("D-31")
def test_items_query_failure_changes_no_state(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))

    def broken(pr_no: str) -> Sequence[PrItem]:
        raise HosxpConfigurationError("get_pr_items: missing column(s) ['qty']")

    w.gw.get_pr_items = broken  # type: ignore[method-assign]
    obs = _only(w.sync_one(p["id"]))
    assert (obs["outcome"], obs["evidence_class"], obs["state_after"]) == (
        "INVALID_EVIDENCE",
        "BINDING",
        "CONFIRMED_LOCKED",
    )
    assert obs["evidence_issues"][0]["code"] == "HOSXP_ITEMS_QUERY_ERROR"


@pytest.mark.spec("D-29")
def test_live_pr_is_exactly_the_canonical_serializer_output(w: W) -> None:
    from ppr.application.pr_snapshot import pr_snapshot

    p = w.confirmed("PR-1", (TONER, "2", "100"))
    w.change("PR-1", requester_name="x")
    obs = _only(w.sync_one(p["id"]))
    assert obs["live_pr"] == pr_snapshot(w.header("PR-1"), w.gw.data.pr_items["PR-1"])
    assert set(obs["live_pr"]) == {"header", "items"}


@pytest.mark.spec("D-29")
def test_stale_single_ppr_run_is_closed_by_the_next_run(w: W) -> None:
    p = w.confirmed("PR-1", (TONER, "2", "100"))
    with w.hx.engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO sync_run (mode, scope, status, requested_by_user_id, ppr_id, "
                "started_at) SELECT 'MANUAL', 'PPR', 'RUNNING', created_by_user_id, id, "
                "now() - interval '7 hours' FROM ppr WHERE id = :i"
            ),
            {"i": p["id"]},
        )
    assert w.sync_one(p["id"]).status_code == 200
    assert w.rows("SELECT status FROM sync_run WHERE scope = 'PPR' ORDER BY id") == [
        ("FAILED",),
        ("SUCCEEDED",),
    ]


@pytest.mark.spec("D-29", "S-25.2")
def test_background_run_answers_at_once_and_finishes_on_the_server(w: W) -> None:
    import time

    p = w.confirmed("PR-1", (TONER, "2", "100"))
    r = w.sync_all(background=True)
    assert r.status_code == 200
    run = r.json()["run"]
    assert r.json()["observations"] == [] and run["status"] in ("RUNNING", "SUCCEEDED")
    for _ in range(100):
        d = w.hx.client.get(f"/api/pr-sync/runs/{run['id']}", headers=w.proc).json()
        if d["run"]["status"] != "RUNNING":
            break
        time.sleep(0.1)
    assert d["run"]["status"] == "SUCCEEDED"
    assert [o["ppr_id"] for o in d["observations"]] == [p["id"]]


def test_master_sync_screens_list_master_runs_only(w: W) -> None:
    w.confirmed("PR-1", (TONER, "2", "100"))
    w.sync_all()
    runs = w.hx.client.get("/api/sync/runs", headers=w.officer).json()
    assert {r["scope"] for r in runs} == {"MASTERS"}


# ================================================================== D-36: alert after a retry
def _sync_alerts(w: W) -> list[dict[str, Any]]:
    got = w.hx.client.get("/api/alerts", headers=w.officer).json()
    return [a for a in got["alerts"] if a["type"] == "SYNC_FAILURE" and a["sync_scope"] == "PPRS"]


def _audit(w: W, action: str) -> list[Any]:
    return w.rows("SELECT entity_id, after FROM audit_log WHERE action = :a ORDER BY id", a=action)


@pytest.mark.spec("D-36", "D-33", "S-36")
def test_the_alert_counts_down_with_retries_and_clears_when_all_are_resolved(w: W) -> None:
    a = w.confirmed("PR-1", (TONER, "1", "100"))
    b = w.confirmed("PR-2", (TONER, "1", "100"))
    w.confirmed("PR-3", (TONER, "1", "100"))
    w.gw.header_errors["PR-1"] = HosxpUnavailableError("down")
    w.gw.header_errors["PR-2"] = HosxpUnavailableError("down")
    run = w.sync_all().json()["run"]
    assert run["status"] == "PARTIAL"
    [alert] = _sync_alerts(w)
    assert (alert["sync_failed"], alert["sync_pending"]) == (2, 2)
    finished = _audit(w, "PR_SYNC_FINISHED")[-1]
    assert sorted(finished[1]["failed_ppr_ids"]) == sorted([a["id"], b["id"]])

    del w.gw.header_errors["PR-1"]  # retry fixes one of the two
    retry = w.sync_all(retry_of=run["id"]).json()["run"]
    assert retry["status"] == "PARTIAL"
    [alert] = _sync_alerts(w)
    assert (alert["sync_failed"], alert["sync_pending"]) == (2, 1)
    result = _audit(w, "PR_SYNC_RETRY_RESULT")[-1]
    assert result[0] == str(run["id"])
    assert (result[1]["resolved"], result[1]["remaining"]) == (1, 1)
    assert result[1]["remaining_ppr_ids"] == [b["id"]]

    w.gw.header_errors.clear()  # a retry of the retry fixes the rest
    w.sync_all(retry_of=retry["id"])
    assert _sync_alerts(w) == []
    last = _audit(w, "PR_SYNC_RETRY_RESULT")[-1]
    assert last[0] == str(run["id"]) and last[1]["remaining"] == 0
    # The failure itself stays in the history.
    assert _audit(w, "PR_SYNC_FINISHED")[0][1]["failed_ppr_ids"]


@pytest.mark.spec("D-36")
def test_a_one_ppr_sync_or_leaving_scope_also_resolves_and_bad_retries_do_not(w: W) -> None:
    a = w.confirmed("PR-1", (TONER, "1", "100"))
    b = w.confirmed("PR-2", (TONER, "1", "100"))
    w.confirmed("PR-3", (TONER, "1", "100"))
    w.gw.header_errors["PR-1"] = HosxpUnavailableError("down")
    w.gw.header_errors["PR-2"] = HosxpUnavailableError("down")
    run = w.sync_all().json()["run"]
    w.sync_all(retry_of=run["id"])  # still down: nothing resolved
    assert _sync_alerts(w)[0]["sync_pending"] == 2
    del w.gw.header_errors["PR-1"]
    assert w.sync_one(a["id"]).status_code == 200
    assert _sync_alerts(w)[0]["sync_pending"] == 1
    with w.hx.engine.begin() as c:  # PR-2 leaves the synchronization scope
        c.execute(text("UPDATE ppr SET state = 'CANCELLED' WHERE id = :i"), {"i": b["id"]})
    assert _sync_alerts(w) == []
