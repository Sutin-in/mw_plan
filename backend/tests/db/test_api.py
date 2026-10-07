"""API: server-side authorization, audited plan-year governance, login via the adapter.

Spec §21, §22 (incl. §22.9), §36, §39, §40.10.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, inspect, text

from api_harness import PASSWORD, Harness
from ppr.auth.session import SessionSigner
from ppr.domain.permissions import ROLE_PERMISSIONS, Permission
from ppr.domain.roles import Role
from ppr.infrastructure.db import repositories


@pytest.fixture
def hx(db: Engine) -> Harness:
    return Harness(db)


_ITEM_BODY: dict[str, object] = {
    "plan_budget_id": 1,
    "plan_type": "DEPARTMENT",
    "item_id": "MOCK-ITEM-TONER",
    "owner_department_id": "MOCK-DEP-ENT",
    "planned_qty": "10",
    "estimated_unit_price": "10",
    "planned_amount": "100",
}

# (method, path, json, permission) - every protected endpoint must be listed here.
_AMEND: dict[str, object] = {
    "approval_document_no": "D-1",
    "approval_date": "2026-10-01",
    "reason": "r",
}

ENDPOINTS: list[tuple[str, str, dict[str, object] | None, Permission]] = [
    ("GET", "/api/plan-years", None, Permission.PLAN_YEAR_READ),
    ("POST", "/api/plan-years", {"fiscal_year": 2570}, Permission.PLAN_YEAR_MANAGE),
    (
        "POST",
        "/api/plan-years/2570/transition",
        {"target": "APPROVED"},
        Permission.PLAN_YEAR_MANAGE,
    ),
    ("GET", "/api/audit-log", None, Permission.AUDIT_READ),
    ("PUT", "/api/admin/users/1/roles/REQUESTER", None, Permission.USER_ROLE_MANAGE),
    ("DELETE", "/api/admin/users/1/roles/REQUESTER", None, Permission.USER_ROLE_MANAGE),
    # ---- Wave 2
    ("POST", "/api/sync/masters", None, Permission.SYNC_RUN),
    ("GET", "/api/sync/runs", None, Permission.SYNC_RUN),
    ("GET", "/api/masters/ITEM", None, Permission.PLAN_READ),
    ("GET", "/api/plan-years/2570/budgets", None, Permission.PLAN_READ),
    (
        "POST",
        "/api/plan-years/2570/budgets",
        {
            "fund_source_id": "MOCK-FUND-1",
            "budget_category_id": "MOCK-CAT-OFFICE",
            "approved_amount": "100",
        },
        Permission.PLAN_MANAGE,
    ),
    ("PUT", "/api/plan-budgets/1", {"approved_amount": "100"}, Permission.PLAN_MANAGE),
    ("DELETE", "/api/plan-budgets/1", None, Permission.PLAN_MANAGE),
    ("GET", "/api/plan-years/2570/items", None, Permission.PLAN_READ),
    ("POST", "/api/plan-years/2570/items", _ITEM_BODY, Permission.PLAN_MANAGE),
    ("GET", "/api/plan-items/1", None, Permission.PLAN_READ),
    ("PUT", "/api/plan-items/1", _ITEM_BODY, Permission.PLAN_MANAGE),
    ("DELETE", "/api/plan-items/1", None, Permission.PLAN_MANAGE),
    ("GET", "/api/plan-items/1/balance", None, Permission.PLAN_READ),
    ("GET", "/api/plan-years/2570/validation", None, Permission.PLAN_MANAGE),
    # ---- Wave 3
    ("GET", "/api/prs/MOCK-PR-NONE/prevalidation", None, Permission.PPR_PREVALIDATE),
    # ---- Wave 4
    ("POST", "/api/pprs", {"pr_no": "MOCK-PR-NONE"}, Permission.PPR_REQUEST),
    ("GET", "/api/pprs", None, Permission.PPR_READ),
    ("GET", "/api/pprs/1", None, Permission.PPR_READ),
    ("GET", "/api/pprs/1/eligible-categories", None, Permission.PPR_READ),
    (
        "PUT",
        "/api/pprs/1/allocations",
        {"allocations": [{"budget_category_id": "MOCK-CAT-OFFICE", "amount": "1"}]},
        Permission.PPR_REQUEST,
    ),
    ("POST", "/api/pprs/1/discard", None, Permission.PPR_DISCARD),
    ("POST", "/api/pprs/1/confirm", None, Permission.PPR_REQUEST),
    ("POST", "/api/pprs/1/unlock", {"reason": "x"}, Permission.PPR_UNLOCK),
    ("GET", "/api/pprs/1/versions", None, Permission.PPR_READ),
    ("GET", "/api/pprs/1/print", None, Permission.PPR_READ),
    # ---- Wave 12A-2 (D-43)
    ("GET", "/api/pprs/1/choices", None, Permission.PPR_READ),
    (
        "PUT",
        "/api/pprs/1/choices",
        {"choices": [{"pr_item_id": "1", "plan_item_id": 1}]},
        Permission.PPR_REQUEST,
    ),
    # ---- Wave 5B
    ("GET", "/api/ppr-search", None, Permission.PPR_READ),
    ("GET", "/api/pprs/1/timeline", None, Permission.PPR_READ),
    ("GET", "/api/procurement/queue", None, Permission.PROCUREMENT_QUEUE_READ),
    ("POST", "/api/pprs/1/verify", {"version": 1}, Permission.PPR_VERIFY),
    ("GET", "/api/appointments", None, Permission.APPOINTMENT_READ),
    (
        "POST",
        "/api/appointments",
        {"user_id": 1, "fiscal_year": 2570, "effective_from": "2026-10-01"},
        Permission.APPOINTMENT_MANAGE,
    ),
    (
        "POST",
        "/api/appointments/1/end",
        {"effective_to": "2027-01-01", "reason": "x"},
        Permission.APPOINTMENT_MANAGE,
    ),
    ("POST", "/api/appointments/1/revoke", {"reason": "x"}, Permission.APPOINTMENT_MANAGE),
    ("GET", "/api/appointments/candidates", None, Permission.APPOINTMENT_MANAGE),
    # ---- Wave 6 (D-29)
    ("POST", "/api/pr-sync", {}, Permission.SYNC_RUN),
    ("POST", "/api/pprs/1/sync", None, Permission.SYNC_RUN),
    ("POST", "/api/pprs/1/recheck", None, Permission.SYNC_RUN),
    ("GET", "/api/pr-sync/governance", None, Permission.SYNC_RUN),
    ("GET", "/api/pr-sync/runs", None, Permission.SYNC_READ),
    ("GET", "/api/pr-sync/runs/1", None, Permission.SYNC_READ),
    ("GET", "/api/pprs/1/observations", None, Permission.PPR_READ),
    # ---- Wave 8A (D-32 ... D-34)
    ("GET", "/api/dashboard", None, Permission.DASHBOARD_READ),
    ("GET", "/api/alerts", None, Permission.DASHBOARD_READ),
    ("GET", "/api/alert-settings", None, Permission.DASHBOARD_READ),
    (
        "PUT",
        "/api/alert-settings/PLAN_LOW_PERCENT",
        {"value": 10, "reason": "x"},
        Permission.ALERT_SETTING_MANAGE,
    ),
    ("GET", "/api/plan-years/2570/balances", None, Permission.PLAN_READ),
    # ---- Wave 8B (D-35)
    ("GET", "/api/reports", None, Permission.REPORT_READ),
    ("GET", "/api/reports/PPR_REGISTER", None, Permission.REPORT_READ),
    ("GET", "/api/reports/PPR_REGISTER/xlsx", None, Permission.REPORT_READ),
    ("GET", "/api/plan-years/2570/amendments", None, Permission.PLAN_READ),
    ("GET", "/api/plan-amendments/1", None, Permission.PLAN_READ),
    ("POST", "/api/plan-years/2570/amendments", _AMEND, Permission.PLAN_AMEND),
    ("POST", "/api/plan-years/2570/amendments/preview", _AMEND, Permission.PLAN_AMEND),
    # ---- Wave 12A-1 (D-42)
    ("GET", "/api/plan-years/2570/import-template", None, Permission.PLAN_MANAGE),
    ("POST", "/api/plan-years/2570/imports/preview", None, Permission.PLAN_MANAGE),
    ("POST", "/api/plan-years/2570/imports", None, Permission.PLAN_MANAGE),
    ("GET", "/api/plan-years/2570/imports", None, Permission.PLAN_READ),
    ("POST", "/api/plan-imports/1/remove", {"reason": "x"}, Permission.PLAN_MANAGE),
    # ---- Wave 7B-2 (D-38 A-2)
    ("GET", "/api/pprs/1/coverage", None, Permission.PPR_READ),
    ("PUT", "/api/pprs/1/coverage", {"coverage": []}, Permission.PPR_REQUEST),
]


def test_every_protected_route_is_in_the_authz_matrix(hx: Harness) -> None:
    # /api/info: demo flag + current fiscal year for the login page (D-26); no user data.
    public = {"/api/health", "/api/info", "/api/auth/login", "/api/me"}
    routes = {
        (m, r.path)  # type: ignore[attr-defined]
        for r in hx.client.app.routes  # type: ignore[attr-defined]
        if getattr(r, "path", "").startswith("/api/")
        for m in r.methods  # type: ignore[attr-defined]
    }
    protected = {(m, p) for m, p in routes if p not in public}

    def matches(template: str, path: str) -> bool:
        return re.fullmatch(re.sub(r"\{[^/]+\}", "[^/]+", template), path) is not None

    covered = {
        (m, t) for m, t in protected for em, ep, _, _ in ENDPOINTS if em == m and matches(t, ep)
    }
    assert covered == protected, f"routes missing from ENDPOINTS: {sorted(protected - covered)}"
    for em, ep, _, _ in ENDPOINTS:
        assert sum(1 for m, t in protected if m == em and matches(t, ep)) == 1, (em, ep)


@pytest.mark.spec("AT-40.10.6", "S-22.9", "S-39")
@pytest.mark.parametrize(("method", "path", "body", "perm"), ENDPOINTS)
def test_unauthenticated_calls_are_rejected(
    hx: Harness, method: str, path: str, body: dict[str, object] | None, perm: Permission
) -> None:
    for headers in ({}, {"Authorization": "Bearer nonsense"}, {"Authorization": "Basic abc"}):
        r = hx.client.request(method, path, json=body, headers=headers)
        assert r.status_code == 401, (method, path, headers)


@pytest.mark.spec("AT-40.10.4", "AT-40.10.6", "S-22.9", "S-39")
@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize(("method", "path", "body", "perm"), ENDPOINTS)
def test_authorization_matrix_is_enforced_server_side(
    hx: Harness,
    role: Role,
    method: str,
    path: str,
    body: dict[str, object] | None,
    perm: Permission,
) -> None:
    token = hx.user(f"u-{role.value.lower()}", role)
    r = hx.client.request(method, path, json=body, headers=hx.h(token))
    if role in ROLE_PERMISSIONS[perm]:
        assert r.status_code not in (401, 403), (role, method, path, r.text)
    else:
        assert r.status_code == 403, (role, method, path, r.text)
        assert r.json()["detail"]["code"] == "FORBIDDEN"


@pytest.mark.spec("AT-40.10.4")
@pytest.mark.parametrize("role", [Role.FINANCE, Role.CFO, Role.EXECUTIVE])
def test_read_only_roles_can_read_but_never_mutate(hx: Harness, role: Role) -> None:
    token = hx.user("reader", role)
    assert hx.client.get("/api/plan-years", headers=hx.h(token)).status_code == 200
    for method, path, body, _ in ENDPOINTS:
        if method != "GET":
            assert (
                hx.client.request(method, path, json=body, headers=hx.h(token)).status_code == 403
            )


@pytest.mark.spec("AT-40.10.5", "S-22.8")
def test_admin_privileges_do_not_include_plan_governance(hx: Harness) -> None:
    admin = hx.user("admin1", Role.ADMIN)
    r = hx.client.post("/api/plan-years", json={"fiscal_year": 2570}, headers=hx.h(admin))
    assert r.status_code == 403


def test_user_without_roles_can_only_see_self(hx: Harness) -> None:
    token = hx.user("newbie")
    me = hx.client.get("/api/me", headers=hx.h(token))
    assert me.status_code == 200 and me.json()["roles"] == []
    assert hx.client.get("/api/plan-years", headers=hx.h(token)).status_code == 403


@pytest.mark.spec("S-22.9")
def test_role_revocation_takes_effect_immediately(hx: Harness) -> None:
    admin = hx.user("admin1", Role.ADMIN)
    officer = hx.user("officer1", Role.PLAN_OFFICER)
    uid = hx.client.get("/api/me", headers=hx.h(officer)).json()["id"]
    assert hx.client.get("/api/audit-log", headers=hx.h(officer)).status_code == 200
    r = hx.client.delete(f"/api/admin/users/{uid}/roles/PLAN_OFFICER", headers=hx.h(admin))
    assert r.status_code == 204
    # Same (still valid) token, but roles are re-read per request.
    assert hx.client.get("/api/audit-log", headers=hx.h(officer)).status_code == 403


@pytest.mark.spec("S-36")
def test_role_changes_are_audited(hx: Harness) -> None:
    admin = hx.user("admin1", Role.ADMIN)
    target = hx.user("staff1")
    uid = hx.client.get("/api/me", headers=hx.h(target)).json()["id"]
    assert (
        hx.client.put(f"/api/admin/users/{uid}/roles/REQUESTER", headers=hx.h(admin)).status_code
        == 204
    )
    assert (
        hx.client.put(f"/api/admin/users/{uid}/roles/REQUESTER", headers=hx.h(admin)).status_code
        == 204
    )
    assert (
        hx.client.delete(f"/api/admin/users/{uid}/roles/REQUESTER", headers=hx.h(admin)).status_code
        == 204
    )
    actions = [a for a in hx.audit_actions() if a[1] == "app_user"]
    # Idempotent re-grant writes no second audit row.
    assert actions == [
        ("ROLE_GRANTED", "app_user", str(uid)),
        ("ROLE_REVOKED", "app_user", str(uid)),
    ]


def test_role_change_for_unknown_user_is_404(hx: Harness) -> None:
    admin = hx.user("admin1", Role.ADMIN)
    assert hx.client.put("/api/admin/users/99999/roles/CFO", headers=hx.h(admin)).status_code == 404


# ------------------------------------------------------------------ plan year governance
@pytest.mark.spec("S-7", "S-36", "D-08")
def test_plan_year_lifecycle_is_governed_and_audited(hx: Harness) -> None:
    officer = hx.user("officer1", Role.PLAN_OFFICER)
    r = hx.client.post("/api/plan-years", json={"fiscal_year": 2570}, headers=hx.h(officer))
    assert r.status_code == 201
    assert r.json() == {
        "fiscal_year": 2570,
        "state": "DRAFT",
        "starts_on": "2026-10-01",
        "ends_on": "2027-09-30",
    }
    dup = hx.client.post("/api/plan-years", json={"fiscal_year": 2570}, headers=hx.h(officer))
    assert dup.status_code == 409 and dup.json()["detail"]["code"] == "PLAN_YEAR_EXISTS"

    def move(target: str) -> int:
        return hx.client.post(
            "/api/plan-years/2570/transition",
            json={"target": target, "reason": f"to {target}"},
            headers=hx.h(officer),
        ).status_code

    assert move("ACTIVE") == 409  # must be APPROVED first
    assert move("APPROVED") == 200
    # D-12: an empty plan cannot be activated (full activation is tested in test_plan_core).
    assert move("ACTIVE") == 409
    with hx.engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT action, before->>'state', after->>'state', reason, actor_kind "
                "FROM audit_log WHERE entity_type = 'plan_year' ORDER BY id"
            )
        ).all()
    assert [tuple(r) for r in rows] == [
        ("PLAN_YEAR_CREATED", None, "DRAFT", None, "USER"),
        ("PLAN_YEAR_STATE_CHANGED", "DRAFT", "APPROVED", "to APPROVED", "USER"),
    ]


def test_transition_of_missing_plan_year_is_404(hx: Harness) -> None:
    officer = hx.user("officer1", Role.PLAN_OFFICER)
    r = hx.client.post(
        "/api/plan-years/2599/transition", json={"target": "APPROVED"}, headers=hx.h(officer)
    )
    assert r.status_code == 404


@pytest.mark.spec("S-36", "S-38.1")
def test_change_and_audit_commit_together_or_not_at_all(
    hx: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    officer = hx.user("officer1", Role.PLAN_OFFICER)

    def broken_write(self: object, entry: object) -> None:
        raise RuntimeError("audit store failure")

    monkeypatch.setattr(repositories.SqlAuditWriter, "write", broken_write)
    client = TestClient(hx.client.app, raise_server_exceptions=False)
    r = client.post("/api/plan-years", json={"fiscal_year": 2570}, headers=hx.h(officer))
    assert r.status_code == 500
    with hx.engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM plan_year")).scalar_one() == 0


# ------------------------------------------------------------------ login / identity
@pytest.mark.spec("S-21", "S-39")
def test_login_goes_through_adapter_and_never_stores_passwords(hx: Harness) -> None:
    hx.user("nurse1", Role.REQUESTER)
    bad = hx.client.post("/api/auth/login", json={"username": "nurse1", "password": "wrong"})
    assert bad.status_code == 401
    cols = {c["name"] for c in inspect(hx.engine).get_columns("app_user")}
    assert not any("pass" in c or "hash" in c for c in cols)
    with hx.engine.connect() as conn:
        dump = " ".join(
            str(v)
            for table in ("app_user", "audit_log", "user_role")
            for row in conn.execute(text(f"SELECT * FROM {table}"))
            for v in row
        )
    assert PASSWORD not in dump and "wrong" not in dump


@pytest.mark.spec("S-26")
def test_login_when_hosxp_is_down_returns_503(hx: Harness) -> None:
    hx.user("nurse1")
    hx.adapter.available = False
    r = hx.client.post("/api/auth/login", json={"username": "nurse1", "password": PASSWORD})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "HOSXP_UNAVAILABLE"


def test_expired_and_forged_tokens_are_rejected(hx: Harness) -> None:
    hx.user("officer1", Role.PLAN_OFFICER)
    uid = hx.client.get("/api/me", headers=hx.h(hx.login("officer1"))).json()["id"]
    expired = hx.signer.issue(uid, now=datetime.now(UTC) - timedelta(hours=2))
    forged = SessionSigner("another-secret-" + "y" * 40, timedelta(minutes=30)).issue(uid)
    for token in (expired, forged):
        assert hx.client.get("/api/me", headers=hx.h(token)).status_code == 401


def test_disabled_local_user_is_rejected_even_with_valid_token(hx: Harness) -> None:
    token = hx.user("officer1", Role.PLAN_OFFICER)
    with hx.engine.begin() as conn:
        conn.execute(text("UPDATE app_user SET active = false WHERE username = 'officer1'"))
    assert hx.client.get("/api/me", headers=hx.h(token)).status_code == 401


def test_health(hx: Harness) -> None:
    assert hx.client.get("/api/health").json() == {"status": "ok", "database": "ok"}


# ------------------------------------------------------------------ CLI bootstrap
@pytest.mark.spec("S-36")
def test_cli_bootstrap_admin_is_audited_as_system(
    hx: Harness, db_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ppr import cli

    hx.user("firstadmin")
    monkeypatch.setenv("PPR_DATABASE_URL", db_url)
    assert cli.main(["grant-role", "--username", "firstadmin", "--role", "ADMIN"]) == 0
    assert cli.main(["grant-role", "--username", "nobody", "--role", "ADMIN"]) == 1
    with hx.engine.connect() as conn:
        row = conn.execute(
            text("SELECT action, actor_kind, actor_user_id, reason FROM audit_log")
        ).one()
    assert tuple(row) == ("ROLE_GRANTED", "SYSTEM", None, "bootstrap via CLI")
