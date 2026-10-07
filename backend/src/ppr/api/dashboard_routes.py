"""Wave 8A routes: dashboards, derived alerts, alert settings, plan balances (D-32 ... D-34).

Every route declares one permission. Content is scoped server-side exactly like reading the
underlying PPR or Plan Item (§22.1); the UI never filters for security.
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field

from ppr.api.ppr_routes import PprOut, _caller, _http, _out
from ppr.application import dashboard_services as ds
from ppr.application.errors import NotFoundError, ServiceError
from ppr.domain.alerts import SETTING_RANGES, AlertSetting
from ppr.domain.fiscal_year import fiscal_year_label
from ppr.domain.permissions import Permission, is_allowed, sees_all_departments
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import Actor, AlertSettingRecord, UserRecord


# ------------------------------------------------------------------ schemas
class AlertOut(BaseModel):
    type: str
    subject: str
    subject_id: int
    fiscal_year: int | None
    department_id: str | None
    since: str | None
    ppr_number: str | None
    pr_no: str | None
    state: str | None
    days_inactive: int | None
    item_id: str | None
    item_code: str | None
    item_name: str | None
    unit: str | None
    remaining_qty: Decimal | None
    remaining_amount: Decimal | None
    remaining_qty_percent: Decimal | None
    remaining_amount_percent: Decimal | None
    sync_scope: str | None
    sync_status: str | None
    sync_failed: int | None
    sync_pending: int | None


class AlertListOut(BaseModel):
    alerts: list[AlertOut]
    counts: dict[str, int]
    inactive_days: int
    low_percent: int
    truncated: bool


class TotalsOut(BaseModel):
    planned_amount: Decimal
    used_amount: Decimal
    remaining_amount: Decimal
    items: int
    low: int
    exhausted: int


class DepartmentTotalsOut(TotalsOut):
    department_id: str


class CategoryTotalsOut(TotalsOut):
    fund_source_id: str
    budget_category_id: str


class DashboardOut(BaseModel):
    fiscal_year: int
    plan_year_state: str | None
    department_scope: str | None
    own_pprs_only: bool = False
    totals: TotalsOut
    by_department: list[DepartmentTotalsOut]
    by_category: list[CategoryTotalsOut]
    ppr_counts: dict[str, int]
    near_exhaustion: list[AlertOut]
    alerts: AlertListOut
    recent: list[PprOut]


class ItemBalanceOut(BaseModel):
    plan_item_id: int
    fiscal_year: int
    plan_year_state: str
    plan_type: str
    item_id: str | None  # None: imported row not yet bound (D-42)
    item_code: str | None
    item_name: str
    unit: str
    owner_department_id: str
    purchasing_department_id: str | None
    fund_source_id: str
    budget_category_id: str
    planned_qty: Decimal
    planned_amount: Decimal
    used_qty: Decimal
    used_amount: Decimal
    remaining_qty: Decimal
    remaining_amount: Decimal
    alert: str | None
    remaining_qty_percent: Decimal | None
    remaining_amount_percent: Decimal | None


class SettingOut(BaseModel):
    code: str
    value: int
    min: int
    max: int
    updated_at: str | None
    updated_by_user_id: int | None


class SettingIn(BaseModel):
    value: int
    reason: str | None = Field(default=None, max_length=1000)


def _pct(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.01"))


def _alert(a: ds.Alert) -> AlertOut:
    return AlertOut(
        type=a.type.value,
        subject=a.subject,
        subject_id=a.subject_id,
        fiscal_year=a.fiscal_year,
        department_id=a.department_id,
        since=a.since.isoformat() if a.since else None,
        ppr_number=a.ppr_number,
        pr_no=a.pr_no,
        state=a.state,
        days_inactive=a.days_inactive,
        item_id=a.item_id,
        item_code=a.item_code,
        item_name=a.item_name,
        unit=a.unit,
        remaining_qty=a.remaining_qty,
        remaining_amount=a.remaining_amount,
        remaining_qty_percent=_pct(a.remaining_qty_percent),
        remaining_amount_percent=_pct(a.remaining_amount_percent),
        sync_scope=a.sync_scope,
        sync_status=a.sync_status,
        sync_failed=a.sync_failed,
        sync_pending=a.sync_pending,
    )


def _alert_list(al: ds.AlertList) -> AlertListOut:
    return AlertListOut(
        alerts=[_alert(a) for a in al.alerts],
        counts=al.counts,
        inactive_days=al.inactive_days,
        low_percent=al.low_percent,
        truncated=al.truncated,
    )


def _totals(t: ds.Totals) -> dict[str, Any]:
    return {
        "planned_amount": t.planned_amount,
        "used_amount": t.used_amount,
        "remaining_amount": t.remaining_amount,
        "items": t.items,
        "low": t.low,
        "exhausted": t.exhausted,
    }


def _balance(s: ds.ItemStatus) -> ItemBalanceOut:
    r = s.item
    return ItemBalanceOut(
        plan_item_id=r.id,
        fiscal_year=r.fiscal_year,
        plan_year_state=s.year_state.value,
        plan_type=r.plan_type.value,
        item_id=r.item_id,
        item_code=r.item_code,
        item_name=r.item_name,
        unit=r.unit,
        owner_department_id=r.owner_department_id,
        purchasing_department_id=r.purchasing_department_id,
        fund_source_id=r.fund_source_id,
        budget_category_id=r.budget_category_id,
        planned_qty=s.planned_qty,
        planned_amount=s.planned_amount,
        used_qty=s.used_qty,
        used_amount=s.used_amount,
        remaining_qty=s.remaining_qty,
        remaining_amount=s.remaining_amount,
        alert=s.level.alert.value if s.level.alert else None,
        remaining_qty_percent=_pct(s.level.remaining_qty_percent),
        remaining_amount_percent=_pct(s.level.remaining_amount_percent),
    )


def _setting(rec: AlertSettingRecord) -> SettingOut:
    low, high = SETTING_RANGES[rec.code]
    return SettingOut(
        code=rec.code.value,
        value=rec.value,
        min=low,
        max=high,
        updated_at=rec.updated_at.isoformat() if rec.updated_at else None,
        updated_by_user_id=rec.updated_by_user_id,
    )


def plan_scope(user: UserRecord) -> str | None:
    """Plan Item scope of ``GET /api/plan-years/{fy}/items`` (§22.1)."""
    return None if sees_all_departments(user.roles) else (user.department_source_id or "")


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    Viewer = Annotated[UserRecord, Depends(require(Permission.DASHBOARD_READ))]  # noqa: N806
    PlanReader = Annotated[UserRecord, Depends(require(Permission.PLAN_READ))]  # noqa: N806
    SettingAdmin = Annotated[  # noqa: N806
        UserRecord, Depends(require(Permission.ALERT_SETTING_MANAGE))
    ]
    cfg = deps.fiscal_year_config

    def sees_sync(user: UserRecord) -> bool:
        return is_allowed(user.roles, Permission.SYNC_READ)

    @app.get("/api/dashboard", response_model=DashboardOut)
    def dashboard(
        user: Viewer,
        fiscal_year: Annotated[int | None, Query(ge=2400, le=2700)] = None,
    ) -> DashboardOut:
        fy = fiscal_year if fiscal_year is not None else fiscal_year_label(deps.today(), cfg)
        with unit_of_work(deps.engine) as uow:
            d = ds.dashboard(
                uow, _caller(user), plan_scope(user), sees_sync(user), fy, datetime.now(UTC)
            )
        return DashboardOut(
            fiscal_year=d.fiscal_year,
            plan_year_state=d.plan_year_state.value if d.plan_year_state else None,
            department_scope=d.department_scope,
            own_pprs_only=d.own_pprs_only,
            totals=TotalsOut(**_totals(d.totals)),
            by_department=[
                DepartmentTotalsOut(department_id=k, **_totals(v))
                for k, v in d.by_department.items()
            ],
            by_category=[
                CategoryTotalsOut(fund_source_id=f, budget_category_id=c, **_totals(v))
                for (f, c), v in d.by_category.items()
            ],
            ppr_counts=d.ppr_counts,
            near_exhaustion=[_alert(a) for a in d.near_exhaustion],
            alerts=_alert_list(d.alerts),
            recent=[_out(r) for r in d.recent],
        )

    @app.get("/api/alerts", response_model=AlertListOut)
    def list_alerts(user: Viewer) -> AlertListOut:
        with unit_of_work(deps.engine) as uow:
            al = ds.alerts(uow, _caller(user), plan_scope(user), sees_sync(user), datetime.now(UTC))
        return _alert_list(al)

    @app.get("/api/plan-years/{fiscal_year}/balances", response_model=list[ItemBalanceOut])
    def balances(fiscal_year: int, user: PlanReader) -> list[ItemBalanceOut]:
        with unit_of_work(deps.engine) as uow:
            if uow.plan_years.get(fiscal_year) is None:
                raise _http(
                    NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
                )
            rows = ds.balances_for_year(uow, fiscal_year, plan_scope(user))
        return [_balance(s) for s in rows]

    @app.get("/api/alert-settings", response_model=list[SettingOut])
    def settings(_: Viewer) -> list[SettingOut]:
        with unit_of_work(deps.engine) as uow:
            recs = ds.alert_settings(uow)
        return [_setting(r) for r in recs.values()]

    @app.put("/api/alert-settings/{code}", response_model=SettingOut)
    def change_setting(code: AlertSetting, body: SettingIn, user: SettingAdmin) -> SettingOut:
        try:
            with unit_of_work(deps.engine) as uow:
                rec = ds.set_alert_setting(uow, Actor(user.id), code, body.value, body.reason)
        except ServiceError as exc:
            raise _http(exc) from exc
        return _setting(rec)
