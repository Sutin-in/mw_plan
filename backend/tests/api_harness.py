"""Shared API test harness (mock HOSxP auth + gateway, real PostgreSQL)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from ppr.api.app import AppDeps, create_app
from ppr.auth.session import SessionSigner
from ppr.config.fiscal_year import DEFAULT_FISCAL_YEAR
from ppr.domain.roles import Role
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.integration.hosxp.contracts import HosxpUserProfile, PrHeader, PrItem
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.integration.hosxp.mock import MockHosxpAuthenticationAdapter, MockHosxpGateway
from ppr.integration.hosxp.mock.fixtures import sample_data
from ppr.integration.hosxp.status import HosxpPrStatus

SECRET = "test-secret-" + "x" * 40
PASSWORD = "Mock-Pass-123!"
FY = 2570
IN_FY2570 = date(2026, 10, 15)  # a server date inside fiscal year 2570
_AUTO: Any = object()


def same_item_choices(
    client: TestClient, pr_no: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
    """D-43 test convenience: for each line of the PR, the offered plan row that carries
    the line's own HOSxP item, when exactly one does. (The system never does this by
    itself - the requester always chooses; tests pick the evident row.)"""
    r = client.get(f"/api/prs/{pr_no}/prevalidation", headers=headers)
    if r.status_code != 200:
        return []
    body = r.json()
    options = {o["id"]: o for o in body["options"]}
    out = []
    for line in body["lines"]:
        same = [p for p in line["offered"] if options[p]["item_id"] == line["item_id"]]
        if len(same) == 1:
            out.append({"pr_item_id": line["pr_item_id"], "plan_item_id": same[0]})
    return out


def check_pr(client: TestClient, pr_no: str, headers: dict[str, str]) -> Any:
    """GET the PR check with the same-item choices of ``same_item_choices`` (D-43)."""
    picked = same_item_choices(client, pr_no, headers)
    return client.get(
        f"/api/prs/{pr_no}/prevalidation",
        params=[("choice", f"{c['pr_item_id']}:{c['plan_item_id']}") for c in picked],
        headers=headers,
    )


class Harness:
    def __init__(self, engine: Engine, gateway: HosxpGateway | None = None) -> None:
        self.engine = engine
        self.adapter = MockHosxpAuthenticationAdapter()
        self.gateway: Any = gateway if gateway is not None else MockHosxpGateway(sample_data())
        self.signer = SessionSigner(SECRET, timedelta(minutes=30))
        self.today = IN_FY2570  # tests may move the server date (D-18)
        self.client = TestClient(
            create_app(
                AppDeps(
                    engine,
                    self.adapter,
                    self.signer,
                    DEFAULT_FISCAL_YEAR,
                    self.gateway,
                    today=lambda: self.today,
                )
            )
        )

    def user(self, username: str, *roles: Role, department: str | None = "MOCK-DEP-ENT") -> str:
        """Create a mock HOSxP user, log in through the API, grant roles; return token."""
        self.adapter.add_user(
            HosxpUserProfile(
                source_id=f"MOCK-{username}", username=username, department_id=department
            ),
            PASSWORD,
        )
        token = self.login(username)
        with unit_of_work(self.engine) as uow:
            u = uow.users.get_by_username(username)
            assert u is not None
            for r in roles:
                uow.users.grant_role(u.id, r, None)
        return token

    def login(self, username: str, password: str = PASSWORD) -> str:
        r = self.client.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        return str(r.json()["token"])

    @staticmethod
    def h(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def audit_actions(self) -> list[tuple[str, str, str]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT action, entity_type, entity_id FROM audit_log ORDER BY id")
            )
            return [tuple(r) for r in rows]  # type: ignore[misc]

    def same_item_choices(self, pr_no: str, headers: dict[str, str]) -> list[dict[str, Any]]:
        return same_item_choices(self.client, pr_no, headers)

    def create_ppr(self, pr_no: str, headers: dict[str, str], **extra: Any) -> Any:
        """POST /api/pprs choosing, for each line, the plan row of the same HOSxP item."""
        return self.client.post(
            "/api/pprs",
            json={"pr_no": pr_no, "choices": self.same_item_choices(pr_no, headers), **extra},
            headers=headers,
        )

    def rechoose(self, ppr_id: int, pr_no: str, headers: dict[str, str]) -> Any:
        """PUT the same-item choices again (after the PR's lines changed, D-43)."""
        return self.client.put(
            f"/api/pprs/{ppr_id}/choices",
            json={"choices": self.same_item_choices(pr_no, headers)},
            headers=headers,
        )

    def put_pr(
        self,
        pr_no: str,
        *lines: tuple[str, str, str],
        dept: str | None = "MOCK-DEP-ENT",
        fund: str | None = "MOCK-FUND-1",
        budget_year: int | None = FY,
        total: Any = _AUTO,
        status: HosxpPrStatus = HosxpPrStatus.ACTIVE,
    ) -> None:
        """Place a PR in the mock HOSxP. Each line is (item id, qty, unit price); the
        line amount is qty x price and, by default, the header total is their sum."""
        items = [
            PrItem(
                pr_item_id=f"{pr_no}-{n}",
                item_id=item,
                qty=Decimal(q),
                unit_price=Decimal(p),
                amount=(Decimal(q) * Decimal(p)).quantize(Decimal("0.01")),
            )
            for n, (item, q, p) in enumerate(lines, 1)
        ]
        header_total = sum((i.amount for i in items), Decimal(0)) if total is _AUTO else total
        self.gateway.replace_pr(
            PrHeader(
                pr_id=f"ID-{pr_no}",
                pr_no=pr_no,
                pr_date=self.today,
                fiscal_year=budget_year,
                department_id=dept,
                fund_source_id=fund,
                budget_category_id="MOCK-CAT-OFFICE",
                total_amount=header_total,
                status=status,
                # mock native value (never a real HOSxP code); UNKNOWN has none
                native_status=None if status is HosxpPrStatus.UNKNOWN else f"MOCK-{status.value}",
            ),
            items,
        )


class Plan:
    """Small driver for the plan API as a Plan Officer (fiscal year ``FY``)."""

    def __init__(self, hx: Harness) -> None:
        self.hx = hx
        self.token = hx.user("officer", Role.PLAN_OFFICER)
        self.h = hx.h(self.token)

    def sync(self) -> dict[str, Any]:
        r = self.hx.client.post("/api/sync/masters", headers=self.h)
        assert r.status_code == 200, r.text
        return dict(r.json())

    def year(self) -> None:
        r = self.hx.client.post("/api/plan-years", json={"fiscal_year": FY}, headers=self.h)
        assert r.status_code == 201, r.text

    def budget(self, cat: str, amount: str, fund: str = "MOCK-FUND-1", **extra: Any) -> Any:
        return self.hx.client.post(
            f"/api/plan-years/{FY}/budgets",
            json={
                "fund_source_id": fund,
                "budget_category_id": cat,
                "approved_amount": amount,
                **extra,
            },
            headers=self.h,
        )

    def item(self, budget_id: int, amount: str, **over: Any) -> Any:
        body: dict[str, Any] = {
            "plan_budget_id": budget_id,
            "plan_type": "DEPARTMENT",
            "item_id": "MOCK-ITEM-TONER",
            "owner_department_id": "MOCK-DEP-ENT",
            "planned_qty": "10",
            "estimated_unit_price": str(Decimal(amount) / 10),
            "planned_amount": amount,
        }
        body.update(over)
        return self.hx.client.post(f"/api/plan-years/{FY}/items", json=body, headers=self.h)

    def move(self, target: str) -> Any:
        return self.hx.client.post(
            f"/api/plan-years/{FY}/transition", json={"target": target}, headers=self.h
        )

    def audit(self, like: str) -> list[tuple[Any, ...]]:
        with self.hx.engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT action, reason, after FROM audit_log WHERE action LIKE :a ORDER BY id"
                ),
                {"a": like},
            ).all()
        return [tuple(r) for r in rows]
