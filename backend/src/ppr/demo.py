"""Demonstration mode (spec D-26): try the system before HOSxP is connected.

Composition-level module (like ``ppr.bootstrap``): it may know the mock HOSxP. It builds

* a mock HOSxP whose PRs are dated today and belong to the CURRENT fiscal year, and
* mock HOSxP users with one shared, clearly labelled demo password,

and seeds the development database (idempotently) with those users' roles and an
``ACTIVE`` plan for the current fiscal year. Every identifier is ``MOCK-*``; nothing
here describes real HOSxP codes. It must never run against a real HOSxP (the server
refuses demo mode in ``sql`` mode) or on a production server.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from sqlalchemy import Engine

from ppr.application import plan_services, procurement_services, services
from ppr.domain.fiscal_year import (
    FiscalYearConfig,
    PlanYearState,
    fiscal_year_bounds,
    fiscal_year_label,
)
from ppr.domain.plan import PlanType
from ppr.domain.roles import Role
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.integration.hosxp.contracts import (
    HosxpUserProfile,
    PrHeader,
    PrItem,
    StockMovement,
    StockMovementKind,
    StockMovementSource,
    StockVerification,
)
from ppr.integration.hosxp.mock import MockHosxpAuthenticationAdapter, MockHosxpGateway
from ppr.integration.hosxp.mock.fixtures import sample_data
from ppr.integration.hosxp.status import HosxpPrStatus
from ppr.ports import SYSTEM_ACTOR, Actor, AuditEntry, PlanItemInput

DEMO_PASSWORD = "demo1234"  # demo only; shown on the login page in demo mode
ENT, OR = "MOCK-DEP-ENT", "MOCK-DEP-OR"
FUND = "MOCK-FUND-1"
OFFICE, MED = "MOCK-CAT-OFFICE", "MOCK-CAT-MED"
TONER, PAPER, GLOVE = "MOCK-ITEM-TONER", "MOCK-ITEM-PAPER", "MOCK-ITEM-GLOVE"


@dataclass(frozen=True)
class DemoUser:
    username: str
    display_name: str
    department_id: str | None
    roles: tuple[Role, ...]


DEMO_USERS: tuple[DemoUser, ...] = (
    DemoUser("demo_req_ent", "ผู้ขอ แผนก ENT (ทดลอง)", ENT, (Role.REQUESTER,)),
    DemoUser("demo_req_or", "ผู้ขอ ห้องผ่าตัด (ทดลอง)", OR, (Role.REQUESTER,)),
    DemoUser("demo_plan", "เจ้าหน้าที่แผน (ทดลอง)", None, (Role.PLAN_OFFICER,)),
    # Wave 7B-1 (D-38): a requester of the central store, for Central Pool PRs.
    DemoUser("demo_req_store", "ผู้ขอ คลังพัสดุกลาง (ทดลอง)", "MOCK-DEP-STORE", (Role.REQUESTER,)),
    DemoUser("demo_proc", "เจ้าหน้าที่พัสดุ (ทดลอง)", "MOCK-DEP-STORE", (Role.PROCUREMENT,)),
    DemoUser("demo_finance", "การเงิน (ทดลอง)", None, (Role.FINANCE,)),
    DemoUser("demo_exec", "ผู้บริหาร (ทดลอง)", None, (Role.EXECUTIVE,)),
    DemoUser("demo_admin", "ผู้ดูแลระบบ (ทดลอง)", None, (Role.ADMIN,)),
    # Wave 5B (D-27/D-28): the appointed head of this fiscal year, and last year's head.
    DemoUser(
        "demo_head",
        "หัวหน้าเจ้าหน้าที่พัสดุ ปีนี้ (ทดลอง)",
        "MOCK-DEP-STORE",
        (Role.PROCUREMENT, Role.HEAD_OF_PROCUREMENT),
    ),
    DemoUser(
        "demo_head_old",
        "หัวหน้าพัสดุ ปีก่อน (ทดลอง)",
        "MOCK-DEP-STORE",
        (Role.PROCUREMENT, Role.HEAD_OF_PROCUREMENT),
    ),
)

ITEM_LABELS = {
    TONER: ("Mock toner", "box"),
    PAPER: ("Mock paper", "ream"),
    GLOVE: ("Mock gloves", "pack"),
}

# (item, planned qty, unit price, owner department, category) - fund MOCK-FUND-1
PLAN_LINES: tuple[tuple[str, str, str, str, str], ...] = (
    (TONER, "100", "100.00", ENT, OFFICE),
    (PAPER, "200", "50.00", ENT, OFFICE),
    (GLOVE, "500", "30.00", ENT, MED),
    (PAPER, "100", "50.00", OR, OFFICE),
)


def _pr(
    pr_no: str,
    today: date,
    fiscal_year: int,
    department: str,
    category: str,
    lines: list[tuple[str, str, str]],
    *,
    total: str | None = None,
) -> tuple[PrHeader, list[PrItem]]:
    items = [
        PrItem(
            pr_item_id=f"{pr_no}-{n}",
            item_id=item,
            item_name=ITEM_LABELS[item][0],
            unit=ITEM_LABELS[item][1],
            qty=Decimal(q),
            unit_price=Decimal(p),
            amount=(Decimal(q) * Decimal(p)).quantize(Decimal("0.01")),
        )
        for n, (item, q, p) in enumerate(lines, 1)
    ]
    header = PrHeader(
        pr_id=f"MOCK-ID-{pr_no}",
        pr_no=pr_no,
        pr_date=today,
        fiscal_year=fiscal_year,
        department_id=department,
        fund_source_id=FUND,
        budget_category_id=category,
        total_amount=Decimal(total) if total is not None else sum(i.amount for i in items),
        requester_name="ผู้ขอทดลอง (demo)",
        status=HosxpPrStatus.ACTIVE,
        # A mock native value (not a HOSxP code): a normalized status always comes from
        # a native value, as with the real adapter's mapping.
        native_status="MOCK-ACTIVE",
    )
    return header, items


def demo_pr_no(fy: int, n: int) -> str:
    """PR numbers carry the fiscal year, so a new year gets PRs never bound before (D-03)."""
    return f"MOCK-PR-{fy}-{n}"


def demo_prs(today: date, fy: int) -> list[tuple[PrHeader, list[PrItem]]]:
    """Mock PRs dated ``today`` (each shows one behaviour of the system)."""
    n = functools.partial(demo_pr_no, fy)
    return [
        # ordinary PR of ENT, office supplies
        _pr(n(1001), today, fy, ENT, OFFICE, [(TONER, "2", "100"), (PAPER, "5", "50")]),
        # ordinary PR of ENT, medical supplies
        _pr(n(1002), today, fy, ENT, MED, [(GLOVE, "20", "30")]),
        # header total differs from the sum of the items (D-17 -> not eligible)
        _pr(n(1003), today, fy, ENT, OFFICE, [(TONER, "1", "100")], total="107.00"),
        # previous fiscal year (D-18 -> not eligible)
        _pr(n(1004), today, fy - 1, ENT, OFFICE, [(TONER, "1", "100")]),
        # PR of another department (OR): only an OR requester may prepare it
        _pr(n(1005), today, fy, OR, OFFICE, [(PAPER, "10", "50")]),
        # more than the plan has left (toner planned 100)
        _pr(n(1006), today, fy, ENT, OFFICE, [(TONER, "150", "100")]),
        # PR of the central store (D-38): matches a Central Pool item purchased by the store,
        # once the plan has one (NOT_IN_PLAN before)
        _pr(n(1007), today, fy, "MOCK-DEP-STORE", OFFICE, [(TONER, "5", "100")]),
        # PR of the central store buying gloves for other departments (D-38 Assigned
        # Purchase): matches an ASSIGNED item purchased by the store, once the plan has one
        _pr(n(1008), today, fy, "MOCK-DEP-STORE", MED, [(GLOVE, "30", "30")]),
    ]


# D-39: MOCK issues / returns of the central store (made up, like every demo record). The
# demonstration marks its stand-in query as verified so the Central Plan Usage report can show
# them; a real site shows nothing until IT records its own verification.
DEMO_STOCK_SOURCE = StockMovementSource(
    problem=None,
    verification=StockVerification("MOCK (ข้อมูลจำลอง)", date(2026, 10, 1), "MOCK-DEMO"),
    kind_map=MappingProxyType(
        {"MOCK-ISSUE": StockMovementKind.ISSUE, "MOCK-RETURN": StockMovementKind.RETURN}
    ),
)


def demo_stock_movements(today: date) -> list[StockMovement]:
    rows = [  # (id, department, item, qty, kind)
        ("MOCK-SM-1", ENT, TONER, "2", "MOCK-ISSUE"),
        ("MOCK-SM-2", OR, TONER, "1", "MOCK-ISSUE"),
        ("MOCK-SM-3", ENT, TONER, "1", "MOCK-RETURN"),
        ("MOCK-SM-4", ENT, GLOVE, "20", "MOCK-ISSUE"),
        ("MOCK-SM-5", OR, GLOVE, "10", "MOCK-ISSUE"),
    ]
    return [
        StockMovement(
            movement_id=i,
            movement_date=today,
            department_id=d,
            item_id=item,
            qty=Decimal(q),
            native_kind=k,
        )
        for i, d, item, q, k in rows
    ]


def demo_gateway(today: date, fy: int) -> MockHosxpGateway:
    data = sample_data()
    prs = demo_prs(today, fy)
    data.prs = {h.pr_no: h for h, _ in prs}
    data.pr_items = {h.pr_no: items for h, items in prs}
    data.stock_movements = demo_stock_movements(today)
    data.stock_source = DEMO_STOCK_SOURCE
    return MockHosxpGateway(data)


def demo_auth() -> MockHosxpAuthenticationAdapter:
    adapter = MockHosxpAuthenticationAdapter()
    for u in DEMO_USERS:
        adapter.add_user(
            HosxpUserProfile(
                source_id=f"MOCK-USER-{u.username}",
                username=u.username,
                display_name=u.display_name,
                department_id=u.department_id,
            ),
            DEMO_PASSWORD,
        )
    return adapter


@dataclass(frozen=True)
class SeedReport:
    fiscal_year: int
    users: int
    roles_granted: int
    plan_created: bool


def seed(
    engine: Engine,
    adapter: MockHosxpAuthenticationAdapter,
    gateway: MockHosxpGateway,
    cfg: FiscalYearConfig,
    today: Callable[[], date] = date.today,
) -> SeedReport:
    """Idempotent: users/roles always ensured; the plan only if the year has none."""
    fy = fiscal_year_label(today(), cfg)
    granted = 0
    ids: dict[str, int] = {}
    for u in DEMO_USERS:
        with unit_of_work(engine) as uow:
            rec = services.login(uow, adapter, u.username, DEMO_PASSWORD)
            ids[u.username] = rec.id
            for role in u.roles:
                if uow.users.grant_role(rec.id, role, None):
                    granted += 1
                    uow.audit.write(
                        AuditEntry(
                            SYSTEM_ACTOR,
                            "ROLE_GRANTED",
                            "app_user",
                            str(rec.id),
                            after={"roles": [role.value]},
                            reason="demo mode seed (D-26)",
                        )
                    )

    officer = Actor(ids["demo_plan"])
    with unit_of_work(engine) as uow:
        existing = uow.plan_years.get(fy)
    if existing is not None:
        # A seed interrupted after creating the plan is completed, never left half-way.
        _advance_to_active(engine, officer, fy)
        _ensure_appointments(engine, ids, fy, cfg)
        return SeedReport(fy, len(DEMO_USERS), granted, False)

    plan_services.sync_masters(lambda: unit_of_work(engine), gateway, officer)
    with unit_of_work(engine) as uow:
        services.create_plan_year(uow, officer, fy, cfg)
        budgets: dict[str, int] = {}
        for cat in sorted({c for *_, c in PLAN_LINES}):
            total = sum(
                (Decimal(q) * Decimal(p) for _, q, p, _, c in PLAN_LINES if c == cat), Decimal(0)
            )
            budgets[cat] = plan_services.create_budget(uow, officer, fy, FUND, cat, total).id
        for item, q, p, dept, cat in PLAN_LINES:
            plan_services.create_plan_item(
                uow,
                officer,
                fy,
                PlanItemInput(
                    plan_budget_id=budgets[cat],
                    plan_type=PlanType.DEPARTMENT,
                    item_id=item,
                    owner_department_id=dept,
                    purchasing_department_id=None,
                    planned_qty=Decimal(q),
                    estimated_unit_price=Decimal(p),
                    planned_amount=Decimal(q) * Decimal(p),
                ),
            )
    _advance_to_active(engine, officer, fy)
    _ensure_appointments(engine, ids, fy, cfg)
    return SeedReport(fy, len(DEMO_USERS), granted, True)


def _ensure_appointments(
    engine: Engine, ids: dict[str, int], fy: int, cfg: FiscalYearConfig
) -> None:
    """demo_head for the whole current fiscal year, demo_head_old for the previous one -
    each only if that fiscal year has no appointment yet (never touches real history)."""
    admin = Actor(ids["demo_admin"])
    for user, year in (("demo_head", fy), ("demo_head_old", fy - 1)):
        with unit_of_work(engine) as uow:
            if uow.appointments.list(year):
                continue
            start, _ = fiscal_year_bounds(year, cfg)
            procurement_services.create_appointment(uow, admin, ids[user], year, start, None, cfg)


def _advance_to_active(engine: Engine, officer: Actor, fy: int) -> None:
    """DRAFT -> APPROVED -> ACTIVE, from whatever state the demo plan year is in."""
    step = {
        PlanYearState.DRAFT: PlanYearState.APPROVED,
        PlanYearState.APPROVED: PlanYearState.ACTIVE,
    }
    while True:
        with unit_of_work(engine) as uow:
            year = uow.plan_years.get(fy)
            target = step.get(year.state) if year is not None else None
            if target is None:
                return
            services.transition_plan_year(uow, officer, fy, target, "demo mode seed (D-26)")
