"""Wave 8B routes: reports and their Excel files (D-35).

Every route declares ``REPORT_READ``; the audit report also needs ``AUDIT_READ`` (checked by
the service). Content is scoped server-side like reading the underlying records (§22.1).
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Response
from pydantic import BaseModel

from ppr.api import report_xlsx
from ppr.api.dashboard_routes import plan_scope
from ppr.api.ppr_routes import _caller, _http
from ppr.application import report_services as rs
from ppr.application.errors import ServiceError
from ppr.domain.fiscal_year import fiscal_year_label
from ppr.domain.permissions import Permission, is_allowed
from ppr.domain.ppr_state import PprState
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import Actor, UserRecord


class ColumnOut(BaseModel):
    key: str
    label: str
    type: str


class ReportInfoOut(BaseModel):
    code: str
    title: str
    kind: str
    filters: list[str]
    states: list[str]
    available: bool


class ReportOut(BaseModel):
    code: str
    title: str
    kind: str
    filters: list[str]
    columns: list[ColumnOut]
    rows: list[dict[str, Any]]
    truncated: bool
    applied: list[tuple[str, str]]
    notes: list[str]
    generated_at: datetime


def _json(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, datetime | date):
        return v.isoformat()
    return v


@dataclass
class FilterArgs:
    fiscal_year: Annotated[int | None, Query(ge=2400, le=2700)] = None
    department_id: Annotated[str | None, Query(max_length=100)] = None
    fund_source_id: Annotated[str | None, Query(max_length=100)] = None
    budget_category_id: Annotated[str | None, Query(max_length=100)] = None
    item: Annotated[str | None, Query(max_length=100)] = None
    state: PprState | None = None
    date_from: date | None = None
    date_to: date | None = None
    action: Annotated[str | None, Query(max_length=100)] = None
    entity_type: Annotated[str | None, Query(max_length=100)] = None

    def to_filters(self) -> rs.Filters:
        def s(v: str | None) -> str | None:
            return v.strip() or None if v else None

        return rs.Filters(
            fiscal_year=self.fiscal_year,
            department_id=s(self.department_id),
            fund_source_id=s(self.fund_source_id),
            budget_category_id=s(self.budget_category_id),
            item=s(self.item),
            state=self.state,
            date_from=self.date_from,
            date_to=self.date_to,
            action=s(self.action),
            entity_type=s(self.entity_type),
        )


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    Reader = Annotated[UserRecord, Depends(require(Permission.REPORT_READ))]  # noqa: N806
    Args = Annotated[FilterArgs, Depends()]  # noqa: N806
    cfg = deps.fiscal_year_config

    def can_audit(user: UserRecord) -> bool:
        return is_allowed(user.roles, Permission.AUDIT_READ)

    def build(uow: Any, code: str, user: UserRecord, args: FilterArgs) -> rs.Report:
        d = rs.definition(code)
        f = args.to_filters()
        if d.kind in ("plan", "amendment") and f.fiscal_year is None:
            # Plan and amendment reports default to the current fiscal year (D-18 labels).
            f = rs.Filters(**{**f.__dict__, "fiscal_year": fiscal_year_label(deps.today(), cfg)})
        return rs.run(
            uow,
            code,
            _caller(user),
            plan_scope(user),
            can_audit(user),
            f,
            datetime.now(UTC),
            deps.gateway,  # Central Plan Usage reads stock movements live (D-39)
            deps.today(),
        )

    @app.get("/api/reports", response_model=list[ReportInfoOut])
    def list_reports(user: Reader) -> list[ReportInfoOut]:
        return [
            ReportInfoOut(
                code=d.code,
                title=d.title,
                kind=d.kind,
                filters=list(d.filters),
                states=[st.value for st in rs.states_of(d.code)],
                available=d.kind != "audit" or can_audit(user),
            )
            for d in rs.REPORTS.values()
        ]

    @app.get("/api/reports/{code}", response_model=ReportOut)
    def report(code: str, user: Reader, args: Args) -> ReportOut:
        try:
            with unit_of_work(deps.engine) as uow:
                r = build(uow, code, user, args)
        except ServiceError as exc:
            raise _http(exc) from exc
        d = r.definition
        return ReportOut(
            code=d.code,
            title=d.title,
            kind=d.kind,
            filters=list(d.filters),
            columns=[ColumnOut(key=c.key, label=c.label, type=c.type.value) for c in d.columns],
            rows=[{k: _json(v) for k, v in row.items()} for row in r.rows],
            truncated=r.truncated,
            applied=r.filters,
            notes=r.notes,
            generated_at=r.generated_at,
        )

    @app.get("/api/reports/{code}/xlsx")
    def report_file(code: str, user: Reader, args: Args) -> Response:
        try:
            with unit_of_work(deps.engine) as uow:
                r = build(uow, code, user, args)
                content = report_xlsx.build(r)  # built first: only a delivered file is audited
                rs.record_export(uow, Actor(user.id), r)
        except ServiceError as exc:
            raise _http(exc) from exc
        stamp = r.generated_at.astimezone(rs.HOSPITAL_TZ)
        name = f"{r.definition.code}_{stamp:%Y%m%d_%H%M}.xlsx"
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
