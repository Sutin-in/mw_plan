"""Demonstration mode and the server composition root (D-24, D-26).

The demo seed is exercised on the test database directly (``server.build`` itself
refuses demo mode on a ``*_test`` database, which is tested below).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from api_harness import check_pr, same_item_choices
from ppr import demo
from ppr.api.app import AppDeps, create_app
from ppr.auth.session import SessionSigner
from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR
from ppr.config.settings import Settings, SettingsError
from ppr.server import build, require_current_schema

TODAY = date(2026, 10, 15)  # inside FY2570
FY = 2570
SECRET = "demo-test-secret-" + "x" * 40


class Demo:
    def __init__(self, db: Engine) -> None:
        self.gateway = demo.demo_gateway(TODAY, FY)
        self.auth = demo.demo_auth()
        self.report = demo.seed(db, self.auth, self.gateway, DEFAULT_FISCAL_YEAR, lambda: TODAY)
        self.client = TestClient(
            create_app(
                AppDeps(
                    db,
                    self.auth,
                    SessionSigner(SECRET, timedelta(minutes=30)),
                    DEFAULT_FISCAL_YEAR,
                    self.gateway,
                    today=lambda: TODAY,
                    demo=True,
                )
            )
        )

    def h(self, username: str) -> dict[str, str]:
        r = self.client.post(
            "/api/auth/login", json={"username": username, "password": demo.DEMO_PASSWORD}
        )
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    def pr(self, n: int) -> str:
        return demo.demo_pr_no(FY, n)


@pytest.fixture
def d(db: Engine) -> Demo:
    return Demo(db)


@pytest.mark.spec("D-26")
def test_demo_seed_creates_users_roles_and_an_active_plan_for_the_current_year(d: Demo) -> None:
    assert (d.report.fiscal_year, d.report.plan_created) == (FY, True)
    me = d.client.get("/api/me", headers=d.h("demo_plan")).json()
    assert me["roles"] == ["PLAN_OFFICER"]
    years = d.client.get("/api/plan-years", headers=d.h("demo_plan")).json()
    assert [(y["fiscal_year"], y["state"]) for y in years] == [(FY, "ACTIVE")]
    info = d.client.get("/api/info").json()
    assert info == {
        "demo": True,
        "current_fiscal_year": FY,
        "today": TODAY.isoformat(),
        "mock_data_warning": None,  # D-40: production only
    }


@pytest.mark.spec("D-26")
def test_demo_seed_is_idempotent(d: Demo, db: Engine) -> None:
    again = demo.seed(db, d.auth, d.gateway, DEFAULT_FISCAL_YEAR, lambda: TODAY)
    assert (again.plan_created, again.roles_granted) == (False, 0)
    with db.connect() as c:
        assert c.execute(text("SELECT count(*) FROM plan_year")).scalar_one() == 1
        assert c.execute(text("SELECT count(*) FROM app_user")).scalar_one() == len(demo.DEMO_USERS)


@pytest.mark.spec("D-26", "D-17", "D-18", "D-09")
def test_demo_prs_show_each_behaviour(d: Demo) -> None:
    ent = d.h("demo_req_ent")

    def check(n: int, headers: dict[str, str] = ent) -> Any:
        return check_pr(d.client, d.pr(n), headers)

    assert check(1001).json()["eligible"] is True
    assert check(1002).json()["eligible"] is True
    mismatch = check(1003).json()
    assert mismatch["eligible"] is False
    assert "PR_TOTAL_MISMATCH" in {v["code"] for v in mismatch["header_violations"]}
    expired = check(1004).json()
    assert expired["eligible"] is False
    assert "PLAN_YEAR_EXPIRED" in {v["code"] for v in expired["header_violations"]}
    assert check(1005).status_code == 200  # another department's PR: allowed (D-41)
    assert check(1005, d.h("demo_req_or")).json()["eligible"] is True
    assert check(1006).json()["eligible"] is False  # more than the plan has left
    # the store's PR (D-38): the demo plan has no Central Pool item until one is added
    store = check(1007, d.h("demo_req_store")).json()
    assert store["eligible"] is False
    assert "NOT_IN_PLAN" in {v["code"] for line in store["lines"] for v in line["violations"]}
    # ... nor an Assigned Purchase item for the store's glove PR (D-38 A-2) until one is added
    gloves = check(1008, d.h("demo_req_store")).json()
    assert "NOT_IN_PLAN" in {v["code"] for line in gloves["lines"] for v in line["violations"]}


@pytest.mark.spec("D-26", "AT-40.4.2")
def test_a_demo_requester_can_prepare_and_confirm_a_ppr(d: Demo) -> None:
    ent = d.h("demo_req_ent")
    draft = d.client.post(
        "/api/pprs",
        json={"pr_no": d.pr(1001), "choices": same_item_choices(d.client, d.pr(1001), ent)},
        headers=ent,
    )
    assert draft.status_code == 201, draft.text
    done = d.client.post(f"/api/pprs/{draft.json()['id']}/confirm", headers=ent)
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "CONFIRMED_LOCKED" and done.json()["ppr_number"]


@pytest.mark.spec("D-26")
def test_demo_pr_numbers_carry_the_fiscal_year() -> None:
    assert demo.demo_pr_no(2570, 1001) != demo.demo_pr_no(2571, 1001)
    prs = demo.demo_prs(TODAY, FY)
    assert all(h.pr_date == TODAY for h, _ in prs)
    assert {h.fiscal_year for h, _ in prs} == {FY, FY - 1}


# ------------------------------------------------------------------ server.build guards
def _settings(**over: Any) -> Settings:
    base = Settings(
        database_url="postgresql+psycopg://u:p@localhost:5432/ppr_dev",
        test_database_url=None,
        session_secret="s" * 40,
        session_ttl_minutes=60,
    )
    return Settings(**{**base.__dict__, **over})


@pytest.mark.spec("D-26")
@pytest.mark.parametrize(
    ("over", "demo_flag", "message"),
    [
        ({"hosxp_mode": "sql"}, True, "only with the mock HOSxP"),
        ({"database_url": "postgresql+psycopg://u:p@localhost/ppr_test"}, True, "_test"),
        ({}, False, "mock HOSxP has no users"),
        ({"session_secret": "short"}, True, "PPR_SESSION_SECRET"),
    ],
)
def test_server_refuses_unsafe_or_incomplete_configurations(
    over: dict[str, Any], demo_flag: bool, message: str
) -> None:
    with pytest.raises(SettingsError, match=message):
        build(_settings(**over), demo=demo_flag, env={})


@pytest.mark.spec("D-26")
@pytest.mark.parametrize(
    "over",
    [
        {"database_url": "postgresql+psycopg://u:p@localhost/PPR_TEST"},
        {"test_database_url": "postgresql+psycopg://u:p@localhost:5432/ppr_dev"},
    ],
)
def test_demo_refuses_any_test_database(over: dict[str, Any]) -> None:
    with pytest.raises(SettingsError, match="_test"):
        build(_settings(**over), demo=True, env={})


@pytest.mark.spec("D-25")
def test_sql_mode_refuses_a_query_file_without_the_login_queries(tmp_path: Path) -> None:
    import mockhx_schema

    body = mockhx_schema.QUERIES_TOML.split("[queries.get_login_user]")[0]
    f = tmp_path / "q.toml"
    f.write_text(body, encoding="utf-8")
    s = _settings(hosxp_mode="sql", hosxp_queries_file=f, hosxp_database_url="mysql://x@h/db")
    with pytest.raises(SettingsError, match="get_login_user, get_user_profile"):
        build(s, demo=False, env={})
    with pytest.raises(SettingsError, match="not found"):
        build(
            _settings(hosxp_mode="sql", hosxp_queries_file=tmp_path / "none.toml"),
            demo=False,
            env={},
        )


@pytest.mark.spec("D-26")
def test_an_interrupted_demo_seed_is_completed_on_the_next_start(
    db: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, auth = demo.demo_gateway(TODAY, FY), demo.demo_auth()
    monkeypatch.setattr(demo, "_advance_to_active", lambda *a: None)  # "crash" before approval
    assert demo.seed(db, auth, gw, DEFAULT_FISCAL_YEAR, lambda: TODAY).plan_created
    monkeypatch.undo()
    with db.connect() as c:
        assert c.execute(text("SELECT state FROM plan_year")).scalar_one() == "DRAFT"
    assert not demo.seed(db, auth, gw, DEFAULT_FISCAL_YEAR, lambda: TODAY).plan_created
    with db.connect() as c:
        assert c.execute(text("SELECT state FROM plan_year")).scalar_one() == "ACTIVE"


def test_server_refuses_a_database_that_is_not_fully_migrated(empty_db: Engine) -> None:
    with pytest.raises(SettingsError, match="migrate"):
        require_current_schema(empty_db)


def test_server_accepts_a_fully_migrated_database(db: Engine) -> None:
    require_current_schema(db)
