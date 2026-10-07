"""MOCK / DEMO data in a production database (Wave 10B-2, D-40).

Everything this project makes up carries a recognisable mark: HOSxP stand-in records have
source ids starting ``MOCK-`` and demonstration users are named ``demo_*`` (``demo.py``,
``integration/hosxp/mock``). A production database should hold none of them; if it does
(for instance a database that was once used for a demonstration), D-40 decides:

* production still starts, but WARNS at start-up, in ``ops-status`` / ``status.ps1``, on
  every page of the user interface and in the go-live checklist (``first-start --check``);
* MOCK data never reaches the Plan Ledger in production: ``SqlLedgerRepository.append``
  refuses any entry on a Plan Item whose HOSxP item is ``MOCK-*`` when the connection belongs
  to a production engine (``production_engine``).

Production login always goes through HOSxP; demonstration users cannot sign in there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, Select, func, select

from ppr.infrastructure.db.schema import (
    app_user,
    hosxp_budget_category,
    hosxp_department,
    hosxp_fund_source,
    hosxp_item,
    plan_item,
    plan_ledger,
    ppr,
)

MOCK_PREFIX = "MOCK-"
DEMO_USER_PREFIX = "demo_"
PROFILE_OPTION = "ppr_profile"  # an engine execution option set by ``production_engine``


def production_engine(engine: Engine) -> Engine:
    """The same engine, marked so that its connections apply the production-only rules."""
    return engine.execution_options(**{PROFILE_OPTION: "production"})


def is_production(conn: Connection) -> bool:
    return conn.get_execution_options().get(PROFILE_OPTION) == "production"


def is_mock_item(conn: Connection, plan_item_id: int) -> bool:
    source = conn.execute(
        select(plan_item.c.item_source_id).where(plan_item.c.id == plan_item_id)
    ).scalar()
    return source is not None and str(source).startswith(MOCK_PREFIX)


@dataclass(frozen=True)
class MockDataSummary:
    demo_users: int = 0
    mock_masters: int = 0
    mock_plan_items: int = 0
    mock_ledger_entries: int = 0
    mock_pprs: int = 0

    @property
    def found(self) -> bool:
        return any(
            (
                self.demo_users,
                self.mock_masters,
                self.mock_plan_items,
                self.mock_ledger_entries,
                self.mock_pprs,
            )
        )

    def describe(self) -> str:
        parts = [
            (self.demo_users, "demo users (demo_*)"),
            (self.mock_masters, "HOSxP master rows MOCK-*"),
            (self.mock_plan_items, "Plan Items of MOCK-* items"),
            (self.mock_ledger_entries, "Plan Ledger entries on those items"),
            (self.mock_pprs, "PPRs of MOCK-* PRs"),
        ]
        return ", ".join(f"{n:,} {what}" for n, what in parts if n) or "none"

    def thai(self) -> str:
        """The warning shown on every page in production (D-40)."""
        return (
            "คำเตือน: ฐานข้อมูลระบบจริงมีข้อมูลทดลอง (MOCK/DEMO) ปนอยู่ - "
            "ข้อมูลที่มีรหัสขึ้นต้น MOCK- ไม่ใช่ข้อมูลจาก HOSxP จริง และไม่ถูกนำไปลงบัญชีแผน "
            "แจ้งผู้ดูแลระบบให้ตรวจสอบ"
        )


def mock_data_summary(conn: Connection) -> MockDataSummary:
    """Counts only (no names, no records)."""

    def count(stmt: Select[Any]) -> int:
        return int(conn.execute(stmt).scalar() or 0)

    masters = sum(
        count(select(func.count()).select_from(t).where(t.c.source_id.startswith(MOCK_PREFIX)))
        for t in (hosxp_department, hosxp_item, hosxp_fund_source, hosxp_budget_category)
    )
    mock_items = select(plan_item.c.id).where(plan_item.c.item_source_id.startswith(MOCK_PREFIX))
    return MockDataSummary(
        demo_users=count(
            select(func.count())
            .select_from(app_user)
            .where(app_user.c.username.startswith(DEMO_USER_PREFIX, autoescape=True))
        ),
        mock_masters=masters,
        mock_plan_items=count(select(func.count()).select_from(mock_items.subquery())),
        mock_ledger_entries=count(
            select(func.count())
            .select_from(plan_ledger)
            .where(plan_ledger.c.plan_item_id.in_(mock_items))
        ),
        mock_pprs=count(
            select(func.count()).select_from(ppr).where(ppr.c.pr_no.startswith(MOCK_PREFIX))
        ),
    )
