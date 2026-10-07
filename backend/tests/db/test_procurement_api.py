"""Wave 5B through the API: appointments, verification, queue, search, timeline.

Spec §14.4, §22.3, §22.4, §23, §31.7, §32, §33, §40.9, §40.10; D-27, D-28.
Server date: 2026-10-15 (fiscal year 2570) unless a test moves it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from sqlalchemy import Engine, text
from test_ppr_api import GLOVE, PAPER, TONER, World

from api_harness import FY, Harness
from ppr.domain.roles import Role

FY_START, FY_END = "2026-10-01", "2027-09-30"


class Proc:
    def __init__(self, w: World) -> None:
        self.w = w
        self.hx = w.hx
        self.admin = self.hx.h(self.hx.user("admin", Role.ADMIN))
        self.head = self.hx.h(self.hx.user("head", Role.HEAD_OF_PROCUREMENT, Role.PROCUREMENT))
        self.clerk = self.hx.h(self.hx.user("clerk", Role.PROCUREMENT))
        self.head2 = self.hx.h(self.hx.user("head2", Role.HEAD_OF_PROCUREMENT))
        self.plan = w.plan.h

    def uid(self, username: str) -> int:
        with self.hx.engine.connect() as c:
            return int(
                c.execute(
                    text("SELECT id FROM app_user WHERE username = :u"), {"u": username}
                ).scalar_one()
            )

    def appoint(
        self, username: str, start: str = FY_START, end: str | None = None, fy: int = FY
    ) -> Any:
        body: dict[str, Any] = {
            "user_id": self.uid(username),
            "fiscal_year": fy,
            "effective_from": start,
        }
        if end:
            body["effective_to"] = end
        return self.hx.client.post("/api/appointments", json=body, headers=self.admin)

    def confirmed(self, pr_no: str, *lines: tuple[str, str, str]) -> dict[str, Any]:
        d = self.w.draft(pr_no, *lines)
        r = self.w.confirm(d["id"])
        assert r.status_code == 200, r.text
        return dict(r.json())

    def verify(
        self, ppr: dict[str, Any], headers: dict[str, str], version: int | None = None
    ) -> Any:
        return self.hx.client.post(
            f"/api/pprs/{ppr['id']}/verify",
            json={"version": version or ppr["version"]},
            headers=headers,
        )


@pytest.fixture
def p(db: Engine) -> Proc:
    return Proc(World(Harness(db)))


def _code(r: Any) -> str:
    return str(r.json()["detail"]["code"])


# ------------------------------------------------------------------ appointments (§23, D-27)
@pytest.mark.spec("S-23", "D-27", "AT-40.9.5")
def test_admin_appoints_and_every_change_is_audited_never_deleted(p: Proc) -> None:
    r = p.appoint("head")
    assert r.status_code == 201, r.text
    a = r.json()
    assert (a["effective_to"], a["status"], a["effective_today"]) == (FY_END, "ACTIVE", True)
    ended = p.hx.client.post(
        f"/api/appointments/{a['id']}/end",
        json={"effective_to": "2027-03-31", "reason": "handover"},
        headers=p.admin,
    )
    assert ended.status_code == 200 and ended.json()["effective_to"] == "2027-03-31"
    revoked = p.hx.client.post(
        f"/api/appointments/{a['id']}/revoke",
        json={"reason": "entered by mistake"},
        headers=p.admin,
    )
    assert revoked.json()["status"] == "REVOKED"
    acts = [x for x in p.hx.audit_actions() if x[1] == "role_appointment"]
    assert [x[0] for x in acts] == [
        "APPOINTMENT_CREATED",
        "APPOINTMENT_ENDED",
        "APPOINTMENT_REVOKED",
    ]
    listed = p.hx.client.get("/api/appointments", headers=p.clerk).json()
    assert [x["id"] for x in listed] == [a["id"]]  # history kept


@pytest.mark.spec("D-27", "AT-40.9.4")
def test_appointments_never_overlap_and_stay_inside_the_fiscal_year(p: Proc) -> None:
    assert p.appoint("head", end="2027-03-31").status_code == 201
    assert _code(p.appoint("head2", start="2027-03-31")) == "APPOINTMENT_OVERLAP"
    assert p.appoint("head2", start="2027-04-01").status_code == 201  # the next day
    assert _code(p.appoint("head2", start="2026-09-30")) == "APPOINTMENT_OUTSIDE_FISCAL_YEAR"
    # only an active user holding the HEAD_OF_PROCUREMENT role can be appointed
    assert _code(p.appoint("clerk", start="2027-04-01")) == "USER_NOT_ELIGIBLE"


@pytest.mark.spec("D-27")
def test_the_database_refuses_overlapping_active_appointments(p: Proc, db: Engine) -> None:
    assert p.appoint("head").status_code == 201
    with pytest.raises(Exception, match="no_overlapping_active"), db.begin() as c:
        c.execute(
            text(
                "INSERT INTO role_appointment (user_id, role_code, fiscal_year, "
                "effective_from, effective_to) VALUES (:u, 'HEAD_OF_PROCUREMENT', 2570, "
                "'2027-01-01', '2027-01-31')"
            ),
            {"u": p.uid("head2")},
        )


@pytest.mark.spec("S-22.8", "AT-40.10.6")
@pytest.mark.parametrize("who", ["head", "clerk", "plan"])
def test_only_admin_manages_appointments(p: Proc, who: str) -> None:
    headers = {"head": p.head, "clerk": p.clerk, "plan": p.plan}[who]
    body = {"user_id": p.uid("head"), "fiscal_year": FY, "effective_from": FY_START}
    assert p.hx.client.post("/api/appointments", json=body, headers=headers).status_code == 403


# ------------------------------------------------------------------ verification (§14.4, D-28)
@pytest.mark.spec("S-14.4", "D-28", "AT-40.9.1", "AT-40.9.5")
def test_the_appointed_head_verifies_and_it_is_recorded_and_audited(p: Proc) -> None:
    appt = p.appoint("head").json()
    ppr = p.confirmed("PR-V1", (TONER, "2", "100"))
    before = p.w.balance(TONER)
    r = p.verify(ppr, p.head)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["version"]) == ("PROCUREMENT_VERIFIED", 1)
    assert p.w.balance(TONER) == before  # no ledger effect
    with p.hx.engine.connect() as c:
        row = c.execute(text("SELECT version, appointment_id FROM ppr_verification")).one()
        ledger = c.execute(
            text("SELECT count(*) FROM plan_ledger WHERE ppr_ref = :n"), {"n": ppr["ppr_number"]}
        ).scalar_one()
    assert tuple(row) == (1, appt["id"])
    assert ledger == 1  # only the confirmation posting
    assert ("PPR_VERIFIED", "ppr", str(ppr["id"])) in p.hx.audit_actions()


@pytest.mark.spec("D-28", "AT-40.9.2", "AT-40.10.3")
def test_procurement_staff_cannot_verify_unless_appointed(p: Proc) -> None:
    p.appoint("head")
    ppr = p.confirmed("PR-V2", (TONER, "1", "100"))
    assert p.verify(ppr, p.clerk).status_code == 403  # no HEAD role
    r = p.verify(ppr, p.head2)  # holds the role but is not appointed
    assert (r.status_code, _code(r)) == (403, "NOT_APPOINTED")
    for other in (p.plan, p.admin, p.w.req):
        assert p.verify(ppr, other).status_code == 403


@pytest.mark.spec("D-28", "AT-40.9.3")
def test_previous_year_head_cannot_verify_in_the_new_year(p: Proc) -> None:
    p.appoint("head", start="2025-10-01", fy=2569)  # FY2569 head only
    ppr = p.confirmed("PR-V3", (TONER, "1", "100"))
    r = p.verify(ppr, p.head)
    assert (r.status_code, _code(r)) == (403, "NOT_APPOINTED")


@pytest.mark.spec("D-28", "AT-40.9.4")
def test_mid_year_change_respects_the_effective_date(p: Proc) -> None:
    p.appoint("head", end="2026-10-15")
    successor = p.hx.h(p.hx.user("head_b", Role.HEAD_OF_PROCUREMENT))
    p.appoint("head_b", start="2026-10-16")
    one = p.confirmed("PR-V4", (TONER, "1", "100"))
    two = p.confirmed("PR-V5", (PAPER, "1", "50"))
    assert _code(p.verify(one, successor)) == "NOT_APPOINTED"  # not yet effective
    assert p.verify(one, p.head).status_code == 200
    p.hx.today = date(2026, 10, 16)
    assert _code(p.verify(two, p.head)) == "NOT_APPOINTED"  # ended yesterday
    assert p.verify(two, successor).status_code == 200


@pytest.mark.spec("D-28", "S-28")
def test_only_a_confirmed_current_version_can_be_verified_once(p: Proc) -> None:
    p.appoint("head")
    draft = p.w.draft("PR-V6", (TONER, "1", "100"))
    assert _code(p.verify({"id": draft["id"], "version": 1}, p.head)) == "INVALID_STATE"
    ppr = p.confirmed("PR-V7", (TONER, "1", "100"))
    assert _code(p.verify(ppr, p.head, version=2)) == "PPR_VERSION_CHANGED"
    assert p.verify(ppr, p.head).status_code == 200
    assert _code(p.verify(ppr, p.head)) == "INVALID_STATE"  # already verified


@pytest.mark.spec("D-28", "D-09", "AT-40.4.4")
def test_after_unlock_and_reconfirm_the_new_version_needs_verifying_again(p: Proc) -> None:
    p.appoint("head")
    ppr = p.confirmed("PR-V8", (TONER, "1", "100"))
    assert p.verify(ppr, p.head).status_code == 200
    r = p.hx.client.post(f"/api/pprs/{ppr['id']}/unlock", json={"reason": "fix"}, headers=p.plan)
    assert r.json()["state"] == "UNLOCKED_FOR_REVISION"
    assert _code(p.verify(ppr, p.head)) == "INVALID_STATE"
    again = p.w.confirm(ppr["id"]).json()
    assert (again["state"], again["version"]) == ("CONFIRMED_LOCKED", 2)
    assert _code(p.verify(again, p.head, version=1)) == "PPR_VERSION_CHANGED"
    assert p.verify(again, p.head).status_code == 200
    with p.hx.engine.connect() as c:
        rows = c.execute(text("SELECT version FROM ppr_verification ORDER BY version")).all()
    assert [r[0] for r in rows] == [1, 2]


@pytest.mark.spec("D-28")
def test_verification_evidence_cannot_be_changed(p: Proc, db: Engine) -> None:
    p.appoint("head")
    ppr = p.confirmed("PR-V9", (TONER, "1", "100"))
    p.verify(ppr, p.head)
    for sql in ("UPDATE ppr_verification SET version = 5", "DELETE FROM ppr_verification"):
        with pytest.raises(Exception, match="append-only"), db.begin() as c:
            c.execute(text(sql))


def test_two_verifications_at_once_record_one(p: Proc) -> None:
    import threading

    p.appoint("head")
    ppr = p.confirmed("PR-V10", (TONER, "1", "100"))
    results: list[int] = []

    def go() -> None:
        results.append(p.verify(ppr, p.head).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409]
    with p.hx.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ppr_verification")).scalar_one() == 1


# ------------------------------------------------------------------ queue (§31.7)
@pytest.mark.spec("S-31.7", "AT-40.10.3")
def test_queue_buckets_counts_and_who_sees_the_verify_action(p: Proc) -> None:
    p.appoint("head")
    a = p.confirmed("PR-Q1", (TONER, "1", "100"))
    b = p.confirmed("PR-Q2", (PAPER, "1", "50"))
    p.w.draft("PR-Q3", (GLOVE, "1", "30"))  # drafts are not in the queue
    p.verify(b, p.head)
    r0 = p.hx.client.get("/api/procurement/queue?bucket=waiting", headers=p.clerk)
    assert r0.status_code == 200, r0.text
    q = r0.json()
    assert [i["pr_no"] for i in q["items"]] == ["PR-Q1"]
    assert (q["counts"]["waiting"], q["counts"]["verified"], q["can_verify"]) == (1, 1, False)
    h = p.hx.client.get("/api/procurement/queue?bucket=verified", headers=p.head).json()
    assert [i["id"] for i in h["items"]] == [b["id"]] and h["can_verify"] is True
    assert h["appointment"]["user_name"] == "head"
    assert a["id"] not in [i["id"] for i in h["items"]]
    assert p.hx.client.get("/api/procurement/queue", headers=p.w.req).status_code == 403


@pytest.mark.spec("S-31.7", "S-34")
def test_inactive_bucket_uses_the_threshold(p: Proc, db: Engine) -> None:
    old = p.confirmed("PR-Q4", (TONER, "1", "100"))
    p.confirmed("PR-Q5", (PAPER, "1", "50"))
    with db.begin() as c:
        c.execute(
            text("UPDATE ppr SET updated_at = now() - interval '31 days' WHERE id = :i"),
            {"i": old["id"]},
        )
    q = p.hx.client.get("/api/procurement/queue?bucket=inactive", headers=p.clerk).json()
    assert [i["id"] for i in q["items"]] == [old["id"]] and q["counts"]["inactive"] == 1


# ------------------------------------------------------------------ search (§32)
@pytest.mark.spec("S-32", "AT-40.10.1", "AT-40.10.3")
def test_search_filters_and_respects_the_department_scope(p: Proc) -> None:
    p.confirmed("PR-S1", (TONER, "1", "100"))
    p.confirmed("PR-S2", (GLOVE, "2", "30"))
    other = p.hx.h(p.hx.user("req_or", Role.REQUESTER, department="MOCK-DEP-OR"))

    def find(headers: dict[str, str], **q: Any) -> list[str]:
        r = p.hx.client.get("/api/ppr-search", params=q, headers=headers)
        assert r.status_code == 200, r.text
        return sorted(i["pr_no"] for i in r.json())

    assert find(p.clerk) == ["PR-S1", "PR-S2"]
    assert find(p.clerk, item="glove") == ["PR-S2"]
    assert find(p.clerk, item="MOCK-ITEM-TONER") == ["PR-S1"]
    assert find(p.clerk, pr_no="s1") == ["PR-S1"]
    assert find(p.clerk, budget_category_id="MOCK-CAT-OFFICE") == ["PR-S1", "PR-S2"]
    assert find(p.clerk, requester="req_ent") == ["PR-S1", "PR-S2"]  # preparing user
    assert find(p.clerk, state="CONFIRMED_LOCKED", fiscal_year=FY) == ["PR-S1", "PR-S2"]
    assert find(p.clerk, fiscal_year=2569) == []
    assert find(p.clerk, department_id="MOCK-DEP-OR") == []
    assert find(p.w.req) == ["PR-S1", "PR-S2"]  # own department
    assert find(other) == []  # another department: nothing
    assert find(other, department_id="MOCK-DEP-ENT") == []  # cannot widen its scope
    assert find(p.clerk, pr_no="%") == []  # wildcards are literal


# ------------------------------------------------------------------ timeline (§33)
@pytest.mark.spec("S-33", "S-36")
def test_timeline_tells_who_did_what_from_the_systems_own_records(p: Proc) -> None:
    p.appoint("head")
    ppr = p.confirmed("PR-T1", (TONER, "1", "100"))
    p.verify(ppr, p.head)
    assert p.hx.client.get(f"/api/pprs/{ppr['id']}/print", headers=p.clerk).status_code == 200
    t = p.hx.client.get(f"/api/pprs/{ppr['id']}/timeline", headers=p.w.req).json()
    actions = [e["action"] for e in t["events"]]
    assert actions == ["PPR_DRAFT_CREATED", "PPR_CONFIRMED", "PPR_VERIFIED", "PPR_PRINT_VIEWED"]
    by = {e["action"]: e for e in t["events"]}
    assert by["PPR_VERIFIED"]["actor_name"] == "head"
    assert by["PPR_VERIFIED"]["state_after"] == "PROCUREMENT_VERIFIED"
    assert by["PPR_PRINT_VIEWED"]["version"] == 1
    assert [v["version"] for v in t["verifications"]] == [1]
    assert t["days_inactive"] == 0
    assert not any(a.startswith(("HOSXP", "PR_")) for a in actions)  # nothing fabricated


@pytest.mark.spec("S-33", "AT-40.10.1")
def test_timeline_of_another_departments_ppr_is_not_found(p: Proc) -> None:
    ppr = p.confirmed("PR-T2", (TONER, "1", "100"))
    other = p.hx.h(p.hx.user("req_or", Role.REQUESTER, department="MOCK-DEP-OR"))
    assert p.hx.client.get(f"/api/pprs/{ppr['id']}/timeline", headers=other).status_code == 404


@pytest.mark.spec("D-28", "S-31.7")
def test_timeline_reports_whether_this_caller_may_verify_now(p: Proc) -> None:
    p.appoint("head")
    ppr = p.confirmed("PR-T3", (TONER, "1", "100"))

    def can(headers: dict[str, str]) -> bool:
        r = p.hx.client.get(f"/api/pprs/{ppr['id']}/timeline", headers=headers)
        return bool(r.json()["can_verify"])

    assert can(p.head) is True
    assert can(p.clerk) is False and can(p.w.req) is False
    p.verify(ppr, p.head)
    assert can(p.head) is False  # already verified


@pytest.mark.spec("D-27", "D-28")
def test_verify_racing_an_early_end_never_records_an_uncovered_verification(
    p: Proc, db: Engine
) -> None:
    import threading
    import time as _time

    appt = p.appoint("head").json()
    ppr = p.confirmed("PR-R1", (TONER, "1", "100"))
    results: dict[str, int] = {}
    with db.connect() as blocker:
        tx = blocker.begin()
        # Simulate an early end in progress: row locked, effective_to moved to yesterday.
        blocker.execute(text("LOCK TABLE role_appointment IN SHARE ROW EXCLUSIVE MODE"))
        blocker.execute(
            text("UPDATE role_appointment SET effective_to = '2026-10-14' WHERE id = :i"),
            {"i": appt["id"]},
        )

        def go() -> None:
            results["verify"] = p.verify(ppr, p.head).status_code

        th = threading.Thread(target=go)
        th.start()
        _time.sleep(1.0)
        assert th.is_alive()  # waits for the ending transaction (FOR SHARE)
        tx.commit()
        th.join(10)
    assert results["verify"] == 403  # re-read: no longer effective today
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ppr_verification")).scalar_one() == 0


@pytest.mark.spec("S-31.7")
def test_queue_counts_follow_the_filters(p: Proc) -> None:
    p.confirmed("PR-C1", (TONER, "1", "100"))
    p.confirmed("PR-C2", (GLOVE, "1", "30"))
    q = p.hx.client.get("/api/procurement/queue?bucket=waiting&item=glove", headers=p.clerk).json()
    assert q["counts"]["waiting"] == 1 and [i["pr_no"] for i in q["items"]] == ["PR-C2"]


@pytest.mark.spec("S-32")
def test_search_by_creation_date_uses_whole_local_days(p: Proc) -> None:
    from datetime import datetime as _dt

    p.confirmed("PR-D1", (TONER, "1", "100"))
    today = _dt.now().astimezone().date().isoformat()  # created now (real, local clock)
    found = p.hx.client.get(
        "/api/ppr-search", params={"created_from": today, "created_to": today}, headers=p.clerk
    ).json()
    assert [i["pr_no"] for i in found] == ["PR-D1"]


# ------------------------------------------------------------------ D-27 revoke vs end early
def _revoke(p: Proc, appt_id: int, reason: str = "entered in error") -> Any:
    return p.hx.client.post(
        f"/api/appointments/{appt_id}/revoke", json={"reason": reason}, headers=p.admin
    )


def _end(p: Proc, appt_id: int, day: str, reason: str = "handover") -> Any:
    return p.hx.client.post(
        f"/api/appointments/{appt_id}/end",
        json={"effective_to": day, "reason": reason},
        headers=p.admin,
    )


@pytest.mark.spec("D-27", "AT-40.9.5")
def test_an_appointment_that_authorized_a_verification_cannot_be_revoked(p: Proc) -> None:
    appt = p.appoint("head").json()
    ppr = p.confirmed("PR-X1", (TONER, "1", "100"))
    assert p.verify(ppr, p.head).status_code == 200
    r = _revoke(p, appt["id"])
    assert (r.status_code, _code(r)) == (409, "APPOINTMENT_IN_USE")
    listed = {a["id"]: a for a in p.hx.client.get("/api/appointments", headers=p.admin).json()}
    assert listed[appt["id"]]["status"] == "ACTIVE"
    assert listed[appt["id"]]["last_verification_on"] == "2026-10-15"
    assert "APPOINTMENT_REVOKED" not in [a for a, _, _ in p.hx.audit_actions()]
    # the handover path still works: end it early, never before its last use
    assert _code(_end(p, appt["id"], "2026-10-14")) == "APPOINTMENT_END_TOO_EARLY"
    assert _end(p, appt["id"], "2026-10-15").status_code == 200
    assert p.appoint("head2", start="2026-10-16").status_code == 201


@pytest.mark.spec("D-27")
def test_an_unused_appointment_can_be_revoked_and_its_dates_reused(p: Proc) -> None:
    wrong = p.appoint("head2").json()
    assert _code(_revoke(p, wrong["id"], reason=" ")) == "REASON_REQUIRED"
    r = _revoke(p, wrong["id"])
    assert r.status_code == 200 and r.json()["status"] == "REVOKED"
    assert r.json()["revoke_reason"] == "entered in error"
    assert p.appoint("head").status_code == 201  # corrected appointment, same dates
    ids = [a["id"] for a in p.hx.client.get("/api/appointments", headers=p.admin).json()]
    assert wrong["id"] in ids  # the revoked row stays in history


@pytest.mark.spec("D-27")
def test_no_new_appointment_over_dates_holding_verification_evidence(p: Proc, db: Engine) -> None:
    appt = p.appoint("head").json()
    ppr = p.confirmed("PR-X2", (TONER, "1", "100"))
    assert p.verify(ppr, p.head).status_code == 200
    with db.begin() as c:  # a revocation that bypassed the service (never via the API)
        c.execute(
            text(
                "UPDATE role_appointment SET status = 'REVOKED', revoked_at = now() WHERE id = :i"
            ),
            {"i": appt["id"]},
        )
    r = p.appoint("head2", start="2026-10-01", end="2026-10-31")
    assert (r.status_code, _code(r)) == (409, "APPOINTMENT_OVERLAPS_EVIDENCE")
    assert p.appoint("head2", start="2026-10-16").status_code == 201  # after the evidence


@pytest.mark.spec("D-27", "D-28")
def test_revoke_racing_a_verification_keeps_the_evidence(p: Proc, db: Engine) -> None:
    import threading
    import time as _time

    appt = p.appoint("head").json()
    ppr = p.confirmed("PR-X3", (TONER, "1", "100"))
    results: dict[str, int] = {}
    with db.connect() as verifier:
        tx = verifier.begin()
        # A verification in flight: appointment row held FOR SHARE, evidence written.
        verifier.execute(
            text("SELECT id FROM role_appointment WHERE id = :i FOR SHARE"), {"i": appt["id"]}
        )
        verifier.execute(
            text(
                "INSERT INTO ppr_verification (ppr_id, version, appointment_id, "
                "verified_by_user_id, verified_on) VALUES (:p, 1, :a, :u, '2026-10-15')"
            ),
            {"p": ppr["id"], "a": appt["id"], "u": p.uid("head")},
        )

        def go() -> None:
            results["revoke"] = _revoke(p, appt["id"]).status_code

        th = threading.Thread(target=go)
        th.start()
        _time.sleep(1.0)
        assert th.is_alive()  # waits for the verification to finish
        tx.commit()
        th.join(10)
        assert not th.is_alive()
    assert results.get("revoke") == 409
    listed = p.hx.client.get("/api/appointments", headers=p.admin).json()
    assert listed[0]["status"] == "ACTIVE"


@pytest.mark.spec("D-27", "D-28")
def test_verification_racing_a_revoke_is_refused(p: Proc, db: Engine) -> None:
    import threading
    import time as _time

    appt = p.appoint("head").json()
    ppr = p.confirmed("PR-X4", (TONER, "1", "100"))
    results: dict[str, Any] = {}
    with db.connect() as admin:
        tx = admin.begin()
        # A revoke in flight (the service's own locks and update, not yet committed).
        admin.execute(text("LOCK TABLE role_appointment IN SHARE ROW EXCLUSIVE MODE"))
        admin.execute(
            text("SELECT id FROM role_appointment WHERE id = :i FOR UPDATE"), {"i": appt["id"]}
        )
        admin.execute(
            text(
                "UPDATE role_appointment SET status = 'REVOKED', revoked_at = now(), "
                "revoke_reason = 'x' WHERE id = :i"
            ),
            {"i": appt["id"]},
        )

        def go() -> None:
            try:
                results["r"] = p.verify(ppr, p.head)
            except Exception as exc:  # pragma: no cover - surfaced below
                results["error"] = exc

        th = threading.Thread(target=go)
        th.start()
        _time.sleep(1.0)
        assert th.is_alive()  # the real /verify waits for the revoke
        tx.commit()
        th.join(10)
        assert not th.is_alive()
    assert "error" not in results, results.get("error")
    assert (results["r"].status_code, _code(results["r"])) == (403, "NOT_APPOINTED")
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM ppr_verification")).scalar_one() == 0


@pytest.mark.spec("D-27")
def test_evidence_check_includes_both_range_ends(p: Proc, db: Engine) -> None:
    appt = p.appoint("head").json()
    ppr = p.confirmed("PR-X5", (TONER, "1", "100"))
    assert p.verify(ppr, p.head).status_code == 200  # verified_on 2026-10-15
    with db.begin() as c:
        c.execute(
            text(
                "UPDATE role_appointment SET status = 'REVOKED', revoked_at = now() WHERE id = :i"
            ),
            {"i": appt["id"]},
        )
    for start, end in (("2026-10-15", "2026-10-20"), ("2026-10-01", "2026-10-15")):
        assert _code(p.appoint("head2", start=start, end=end)) == "APPOINTMENT_OVERLAPS_EVIDENCE"
    assert p.appoint("head2", start="2026-10-01", end="2026-10-14").status_code == 201


@pytest.mark.spec("AT-40.13.3", "S-32", "D-06")
def test_an_old_years_ppr_stays_searchable_in_the_new_year(p: Proc) -> None:
    old = p.confirmed("PR-OLD", (TONER, "1", "100"))
    p.hx.today = date(2027, 11, 2)  # fiscal year 2571 (D-06)

    def find(headers: dict[str, str], **q: Any) -> list[str]:
        r = p.hx.client.get("/api/ppr-search", params=q, headers=headers)
        assert r.status_code == 200, r.text
        return [i["pr_no"] for i in r.json()]

    assert find(p.clerk, fiscal_year=FY) == ["PR-OLD"]
    assert find(p.clerk, pr_no="PR-OLD") == ["PR-OLD"]
    assert find(p.w.req, fiscal_year=FY) == ["PR-OLD"]  # its own department too
    r = p.hx.client.get(f"/api/pprs/{old['id']}", headers=p.clerk)
    assert r.status_code == 200 and r.json()["fiscal_year"] == FY
    assert p.hx.client.get(f"/api/pprs/{old['id']}/print", headers=p.clerk).status_code == 200
