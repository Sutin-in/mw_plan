"""Wave 2 routes: master sync, masters, plan budgets, Plan Items, validation, balances.

Every route declares one permission (checked server-side by ``require``). Read routes
apply department scoping: a user whose only role is REQUESTER sees their own
department's Plan Items only (spec §22.1).
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field

from ppr.application import assigned_services
from ppr.application import plan_services as ps
from ppr.application.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    ServiceError,
    UpstreamUnavailableError,
)
from ppr.domain.permissions import Permission, sees_all_departments
from ppr.domain.plan import DemandState, Issue, PlanType, visible_to_department
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import (
    Actor,
    BudgetRecord,
    Correction,
    DemandInput,
    MasterKind,
    PlanItemInput,
    PlanItemRecord,
    UserRecord,
)

Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]
Qty = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]
OptQty = Annotated[Decimal | None, Field(default=None, ge=0, max_digits=18, decimal_places=4)]


# ------------------------------------------------------------------ schemas
class SyncOut(BaseModel):
    run_id: int
    status: str
    records_checked: int
    records_created: int
    records_updated: int
    per_kind: dict[str, dict[str, int]]


class SyncRunOut(BaseModel):
    id: int
    mode: str
    scope: str
    status: str
    started_at: str
    finished_at: str | None
    records_checked: int
    records_created: int
    records_updated: int
    error_details: dict[str, Any] | None


class MasterOut(BaseModel):
    source_id: str
    code: str
    name: str
    active: bool
    unit: str | None = None
    parent_source_id: str | None = None


class CorrectionFields(BaseModel):
    """Required only when the plan year is APPROVED (data-entry correction, D-15)."""

    correction_reason: str | None = None
    source_reference: str | None = None

    def correction(self) -> Correction | None:
        return _correction(self.correction_reason, self.source_reference)


def _correction(reason: str | None, source_reference: str | None) -> Correction | None:
    if reason is None and source_reference is None:
        return None
    return Correction(reason or "", source_reference or "")


class BudgetIn(CorrectionFields):
    fund_source_id: str = Field(min_length=1)
    budget_category_id: str = Field(min_length=1)
    approved_amount: Money


class BudgetAmountIn(CorrectionFields):
    approved_amount: Money


class BudgetOut(BaseModel):
    id: int
    fiscal_year: int
    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal


class DemandIn(BaseModel):
    department_id: str = Field(min_length=1)
    demand_qty: Qty


class DemandOut(BaseModel):
    department_id: str
    demand_qty: Decimal
    state: DemandState
    # D-38 A-2 / A-6: what PPRs in effect cover of it (also the floor of a demand amendment)
    covered: Decimal = Decimal(0)


class PlanItemIn(CorrectionFields):
    plan_budget_id: int
    plan_type: PlanType
    # D-43: optional; a row without a HOSxP item carries its own name and unit
    item_id: str | None = Field(default=None, min_length=1)
    item_name: str | None = Field(default=None, max_length=500)
    unit: str | None = Field(default=None, max_length=100)
    owner_department_id: str = Field(min_length=1)
    purchasing_department_id: str | None = None
    planned_qty: Qty
    estimated_unit_price: Money
    planned_amount: Money
    q1: OptQty = None
    q2: OptQty = None
    q3: OptQty = None
    q4: OptQty = None
    note: str | None = None
    demands: list[DemandIn] = Field(default_factory=list)

    def to_input(self) -> PlanItemInput:
        return PlanItemInput(
            plan_budget_id=self.plan_budget_id,
            plan_type=self.plan_type,
            item_id=self.item_id,
            owner_department_id=self.owner_department_id,
            purchasing_department_id=self.purchasing_department_id,
            planned_qty=self.planned_qty,
            estimated_unit_price=self.estimated_unit_price,
            planned_amount=self.planned_amount,
            quarters=(self.q1, self.q2, self.q3, self.q4),
            note=self.note,
            demands=tuple(DemandInput(d.department_id, d.demand_qty) for d in self.demands),
            item_name=self.item_name,
            unit=self.unit,
        )


class IssueOut(BaseModel):
    code: str
    message: str
    subject: str | None


class PlanItemOut(BaseModel):
    id: int
    fiscal_year: int
    plan_budget_id: int
    fund_source_id: str
    budget_category_id: str
    plan_type: PlanType
    item_id: str | None
    item_code: str | None
    item_name: str
    unit: str
    owner_department_id: str
    purchasing_department_id: str | None
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    q1: Decimal | None
    q2: Decimal | None
    q3: Decimal | None
    q4: Decimal | None
    note: str | None
    demands: list[DemandOut]
    warnings: list[IssueOut]
    plan_import_id: int | None = None  # D-42: the import a row came from
    source_sheet: str | None = None
    source_row: int | None = None


class EnvelopeOut(BaseModel):
    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal
    planned_total: Decimal
    difference: Decimal
    balanced: bool


class ValidationOut(BaseModel):
    fiscal_year: int
    can_activate: bool
    envelope: list[EnvelopeOut]
    errors: list[IssueOut]
    warnings: list[IssueOut]


class BalanceOut(BaseModel):
    plan_item_id: int
    approved_qty: Decimal
    approved_amount: Decimal
    amendment_qty: Decimal
    amendment_amount: Decimal
    used_qty: Decimal
    used_amount: Decimal
    released_qty: Decimal
    released_amount: Decimal
    remaining_qty: Decimal
    remaining_amount: Decimal


def _issue(i: Issue) -> IssueOut:
    return IssueOut(code=i.code.value, message=i.message, subject=i.subject)


def _budget_out(b: BudgetRecord) -> BudgetOut:
    return BudgetOut(
        id=b.id,
        fiscal_year=b.fiscal_year,
        fund_source_id=b.fund_source_id,
        budget_category_id=b.budget_category_id,
        approved_amount=b.approved_amount,
    )


def _item_out(
    r: PlanItemRecord, covered: Mapping[tuple[int, str], Decimal] | None = None
) -> PlanItemOut:
    q1, q2, q3, q4 = r.quarters
    return PlanItemOut(
        id=r.id,
        fiscal_year=r.fiscal_year,
        plan_budget_id=r.plan_budget_id,
        fund_source_id=r.fund_source_id,
        budget_category_id=r.budget_category_id,
        plan_type=r.plan_type,
        item_id=r.item_id,
        item_code=r.item_code,
        item_name=r.item_name,
        unit=r.unit,
        owner_department_id=r.owner_department_id,
        purchasing_department_id=r.purchasing_department_id,
        planned_qty=r.planned_qty,
        estimated_unit_price=r.estimated_unit_price,
        planned_amount=r.planned_amount,
        q1=q1,
        q2=q2,
        q3=q3,
        q4=q4,
        note=r.note,
        demands=[
            DemandOut(
                department_id=d.department_id,
                demand_qty=d.demand_qty,
                state=d.state,
                covered=(covered or {}).get((r.id, d.department_id), Decimal(0)),
            )
            for d in r.demands
        ],
        warnings=[_issue(w) for w in ps.warnings_for(r)],
        plan_import_id=r.plan_import_id,
        source_sheet=r.source_sheet,
        source_row=r.source_row,
    )


def _http(exc: ServiceError) -> HTTPException:
    code = {
        NotFoundError: status.HTTP_404_NOT_FOUND,
        ConflictError: status.HTTP_409_CONFLICT,
        InvalidInputError: status.HTTP_422_UNPROCESSABLE_CONTENT,
        UpstreamUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    }.get(type(exc), status.HTTP_400_BAD_REQUEST)
    detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if exc.details is not None:
        detail["details"] = exc.details
    return HTTPException(status_code=code, detail=detail)


def _covered(uow: Any, rows: Sequence[PlanItemRecord]) -> dict[tuple[int, str], Decimal]:
    ids = [r.id for r in rows if r.plan_type is PlanType.ASSIGNED]
    return assigned_services.covered_elsewhere(uow, ids, None) if ids else {}


def _visible(user: UserRecord, rec: PlanItemRecord) -> bool:
    if sees_all_departments(user.roles):
        return True
    dept = user.department_source_id
    return dept is not None and visible_to_department(
        dept,
        rec.plan_type,
        rec.owner_department_id,
        rec.purchasing_department_id,
        [d.department_id for d in rec.demands],
    )


def register(
    app: FastAPI,
    deps: Any,
    current_user: Callable[..., UserRecord],
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    engine = deps.engine

    def uow_factory():  # type: ignore[no-untyped-def]
        return unit_of_work(engine)

    Reader = Annotated[UserRecord, Depends(require(Permission.PLAN_READ))]  # noqa: N806
    Manager = Annotated[UserRecord, Depends(require(Permission.PLAN_MANAGE))]  # noqa: N806
    Syncer = Annotated[UserRecord, Depends(require(Permission.SYNC_RUN))]  # noqa: N806

    # -------------------------------------------------------------- sync + masters
    @app.post("/api/sync/masters", response_model=SyncOut)
    def sync_masters(user: Syncer) -> SyncOut:
        try:
            result = ps.sync_masters(uow_factory, deps.gateway, Actor(user.id))
        except ServiceError as exc:
            raise _http(exc) from exc
        run = result.run
        return SyncOut(
            run_id=run.id,
            status=run.status,
            records_checked=run.records_checked,
            records_created=run.records_created,
            records_updated=run.records_updated,
            per_kind={k: {"created": c, "updated": u} for k, (c, u) in result.per_kind.items()},
        )

    @app.get("/api/sync/runs", response_model=list[SyncRunOut])
    def sync_runs(_: Syncer, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[SyncRunOut]:
        with unit_of_work(engine) as uow:
            runs = uow.sync_runs.recent(limit)
        return [
            SyncRunOut(
                id=r.id,
                mode=r.mode,
                scope=r.scope,
                status=r.status,
                started_at=r.started_at.isoformat(),
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
                records_checked=r.records_checked,
                records_created=r.records_created,
                records_updated=r.records_updated,
                error_details=r.error_details,
            )
            for r in runs
        ]

    @app.get("/api/masters/{kind}", response_model=list[MasterOut])
    def masters(kind: MasterKind, _: Reader) -> list[MasterOut]:
        with unit_of_work(engine) as uow:
            rows = uow.masters.list(kind)
        return [
            MasterOut(
                source_id=r.source_id,
                code=r.code,
                name=r.name,
                active=r.active,
                unit=r.unit,
                parent_source_id=r.parent_source_id,
            )
            for r in rows
        ]

    # -------------------------------------------------------------- budgets
    @app.get("/api/plan-years/{fiscal_year}/budgets", response_model=list[BudgetOut])
    def list_budgets(fiscal_year: int, _: Reader) -> list[BudgetOut]:
        with unit_of_work(engine) as uow:
            return [_budget_out(b) for b in uow.plans.budgets(fiscal_year)]

    @app.post("/api/plan-years/{fiscal_year}/budgets", response_model=BudgetOut, status_code=201)
    def create_budget(fiscal_year: int, body: BudgetIn, user: Manager) -> BudgetOut:
        try:
            with unit_of_work(engine) as uow:
                rec = ps.create_budget(
                    uow,
                    Actor(user.id),
                    fiscal_year,
                    body.fund_source_id,
                    body.budget_category_id,
                    body.approved_amount,
                    body.correction(),
                )
        except ServiceError as exc:
            raise _http(exc) from exc
        return _budget_out(rec)

    @app.put("/api/plan-budgets/{budget_id}", response_model=BudgetOut)
    def update_budget(budget_id: int, body: BudgetAmountIn, user: Manager) -> BudgetOut:
        try:
            with unit_of_work(engine) as uow:
                rec = ps.update_budget(
                    uow, Actor(user.id), budget_id, body.approved_amount, body.correction()
                )
        except ServiceError as exc:
            raise _http(exc) from exc
        return _budget_out(rec)

    @app.delete("/api/plan-budgets/{budget_id}", status_code=204)
    def delete_budget(
        budget_id: int,
        user: Manager,
        correction_reason: str | None = None,
        source_reference: str | None = None,
    ) -> None:
        try:
            with unit_of_work(engine) as uow:
                ps.delete_budget(
                    uow,
                    Actor(user.id),
                    budget_id,
                    _correction(correction_reason, source_reference),
                )
        except ServiceError as exc:
            raise _http(exc) from exc

    # -------------------------------------------------------------- plan items
    @app.get("/api/plan-years/{fiscal_year}/items", response_model=list[PlanItemOut])
    def list_items(fiscal_year: int, user: Reader) -> list[PlanItemOut]:
        scope = None if sees_all_departments(user.roles) else (user.department_source_id or "")
        with unit_of_work(engine) as uow:
            rows = uow.plans.items(fiscal_year, department_id=scope)
            covered = _covered(uow, rows)
        return [_item_out(r, covered) for r in rows]

    @app.post("/api/plan-years/{fiscal_year}/items", response_model=PlanItemOut, status_code=201)
    def create_item(fiscal_year: int, body: PlanItemIn, user: Manager) -> PlanItemOut:
        try:
            with unit_of_work(engine) as uow:
                rec = ps.create_plan_item(
                    uow, Actor(user.id), fiscal_year, body.to_input(), body.correction()
                )
        except ServiceError as exc:
            raise _http(exc) from exc
        return _item_out(rec)

    @app.get("/api/plan-items/{item_id}", response_model=PlanItemOut)
    def get_item(item_id: int, user: Reader) -> PlanItemOut:
        with unit_of_work(engine) as uow:
            rec = uow.plans.get_item(item_id)
            covered = _covered(uow, [rec] if rec is not None else [])
        if rec is None or not _visible(user, rec):
            # Same answer for "missing" and "not yours": no information leak.
            raise _http(NotFoundError("PLAN_ITEM_NOT_FOUND", f"plan item {item_id} not found"))
        return _item_out(rec, covered)

    @app.put("/api/plan-items/{item_id}", response_model=PlanItemOut)
    def update_item(item_id: int, body: PlanItemIn, user: Manager) -> PlanItemOut:
        try:
            with unit_of_work(engine) as uow:
                rec = ps.update_plan_item(
                    uow, Actor(user.id), item_id, body.to_input(), body.correction()
                )
        except ServiceError as exc:
            raise _http(exc) from exc
        return _item_out(rec)

    @app.delete("/api/plan-items/{item_id}", status_code=204)
    def delete_item(
        item_id: int,
        user: Manager,
        correction_reason: str | None = None,
        source_reference: str | None = None,
    ) -> None:
        try:
            with unit_of_work(engine) as uow:
                ps.delete_plan_item(
                    uow, Actor(user.id), item_id, _correction(correction_reason, source_reference)
                )
        except ServiceError as exc:
            raise _http(exc) from exc

    @app.get("/api/plan-items/{item_id}/balance", response_model=BalanceOut)
    def item_balance(item_id: int, user: Reader) -> BalanceOut:
        with unit_of_work(engine) as uow:
            rec = uow.plans.get_item(item_id)
            if rec is None or not _visible(user, rec):
                raise _http(NotFoundError("PLAN_ITEM_NOT_FOUND", f"plan item {item_id} not found"))
            b = ps.item_balance(uow, item_id)
        return BalanceOut(
            plan_item_id=item_id,
            approved_qty=b.approved_qty,
            approved_amount=b.approved_amount,
            amendment_qty=b.amendment_qty,
            amendment_amount=b.amendment_amount,
            used_qty=b.ppr_used_qty,
            used_amount=b.ppr_used_amount,
            released_qty=b.released_qty,
            released_amount=b.released_amount,
            remaining_qty=b.remaining_qty,
            remaining_amount=b.remaining_amount,
        )

    # -------------------------------------------------------------- validation
    @app.get("/api/plan-years/{fiscal_year}/validation", response_model=ValidationOut)
    def validation(fiscal_year: int, _: Manager) -> ValidationOut:
        try:
            with unit_of_work(engine) as uow:
                report = ps.validate_plan(uow, fiscal_year)
        except ServiceError as exc:
            raise _http(exc) from exc
        return ValidationOut(
            fiscal_year=fiscal_year,
            can_activate=report.can_activate,
            envelope=[
                EnvelopeOut(
                    fund_source_id=e.fund_source_id,
                    budget_category_id=e.budget_category_id,
                    approved_amount=e.approved_amount,
                    planned_total=e.planned_total,
                    difference=e.difference,
                    balanced=e.balanced,
                )
                for e in report.envelope
            ],
            errors=[_issue(i) for i in report.errors],
            warnings=[_issue(i) for i in report.warnings],
        )

    _ = current_user  # kept for symmetry with app.py; routes use `require`
