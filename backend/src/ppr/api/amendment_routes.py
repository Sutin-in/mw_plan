"""Wave 7A routes: Plan Amendment (spec §20, D-37).

* ``POST /api/plan-years/{fy}/amendments/preview`` judges an amendment and writes nothing;
* ``POST /api/plan-years/{fy}/amendments`` records it (Plan Officer, ACTIVE year only);
* ``GET  /api/plan-years/{fy}/amendments`` and ``GET /api/plan-amendments/{id}`` read the
  history, scoped like reading Plan Items (§22.1).
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from pydantic import BaseModel, Field

from ppr.api.plan_routes import IssueOut
from ppr.api.ppr_routes import _http
from ppr.application import amendment_services as am
from ppr.application.errors import NotFoundError, ServiceError
from ppr.domain.amendment import AmendmentIssue, AmendmentPlan, DemandChange, ItemValues
from ppr.domain.permissions import Permission, sees_all_departments
from ppr.domain.plan import PlanType
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import Actor, AmendmentRecord, UserRecord

Money = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=2)]
AmendQty = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]
NewQty = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=4)]


# ------------------------------------------------------------------ schemas
class BudgetChangeIn(BaseModel):
    fund_source_id: str = Field(min_length=1)
    budget_category_id: str = Field(min_length=1)
    approved_amount: Money


class ItemChangeIn(BaseModel):
    """Only the fields sent are changed; the fund source and plan type never change."""

    plan_item_id: int
    item_id: str | None = Field(default=None, min_length=1)
    owner_department_id: str | None = Field(default=None, min_length=1)
    purchasing_department_id: str | None = None
    budget_category_id: str | None = Field(default=None, min_length=1)
    planned_qty: AmendQty | None = None
    estimated_unit_price: Money | None = None
    planned_amount: Money | None = None
    # D-43: a row without a HOSxP item, while it is unused
    item_name: str | None = Field(default=None, min_length=1, max_length=500)
    unit: str | None = Field(default=None, min_length=1, max_length=100)

    def to_request(self) -> am.ItemChangeRequest:
        sent = self.model_fields_set

        def keep(name: str) -> Any:
            value = getattr(self, name)
            if name == "purchasing_department_id":  # "" or null clears it
                return (value or None) if name in sent else am.KEEP
            return value if value is not None else am.KEEP

        return am.ItemChangeRequest(
            plan_item_id=self.plan_item_id,
            item_id=keep("item_id"),
            owner_department_id=keep("owner_department_id"),
            purchasing_department_id=keep("purchasing_department_id"),
            budget_category_id=keep("budget_category_id"),
            planned_qty=keep("planned_qty"),
            estimated_unit_price=keep("estimated_unit_price"),
            planned_amount=keep("planned_amount"),
            item_name=keep("item_name"),
            unit=keep("unit"),
        )


class DemandIn(BaseModel):
    """One department's demand on an Assigned Purchase item (D-38 A-6; 0 cancels it)."""

    department_id: str = Field(min_length=1)
    demand_qty: AmendQty


class DemandChangeIn(DemandIn):
    plan_item_id: int


class NewItemIn(BaseModel):
    ref: str = Field(min_length=1, max_length=50)
    plan_type: PlanType
    item_id: str | None = Field(default=None, min_length=1)  # D-43: optional
    item_name: str | None = Field(default=None, max_length=500)  # required without item_id
    unit: str | None = Field(default=None, max_length=100)
    owner_department_id: str = Field(min_length=1)
    purchasing_department_id: str | None = None
    fund_source_id: str = Field(min_length=1)
    budget_category_id: str = Field(min_length=1)
    planned_qty: NewQty
    estimated_unit_price: Money
    planned_amount: Money
    note: str | None = Field(default=None, max_length=1000)
    demands: list[DemandIn] = Field(default_factory=list, max_length=500)


class AmendmentIn(BaseModel):
    approval_document_no: str = Field(min_length=1, max_length=100)
    approval_date: date
    reason: str = Field(min_length=1, max_length=2000)
    budgets: list[BudgetChangeIn] = Field(default_factory=list, max_length=500)
    items: list[ItemChangeIn] = Field(default_factory=list, max_length=2000)
    new_items: list[NewItemIn] = Field(default_factory=list, max_length=500)
    demands: list[DemandChangeIn] = Field(default_factory=list, max_length=2000)
    base_amendment_no: int | None = Field(default=None, ge=0)

    def to_request(self) -> am.AmendmentRequest:
        return am.AmendmentRequest(
            approval_document_no=self.approval_document_no,
            approval_date=self.approval_date,
            reason=self.reason,
            budgets=tuple(
                am.BudgetChangeRequest(b.fund_source_id, b.budget_category_id, b.approved_amount)
                for b in self.budgets
            ),
            items=tuple(i.to_request() for i in self.items),
            new_items=tuple(
                am.NewItemRequest(
                    ref=n.ref,
                    plan_type=n.plan_type,
                    item_id=n.item_id or None,
                    item_name=n.item_name,
                    unit=n.unit,
                    owner_department_id=n.owner_department_id,
                    purchasing_department_id=n.purchasing_department_id or None,
                    fund_source_id=n.fund_source_id,
                    budget_category_id=n.budget_category_id,
                    planned_qty=n.planned_qty,
                    estimated_unit_price=n.estimated_unit_price,
                    planned_amount=n.planned_amount,
                    note=n.note,
                    demands=tuple((d.department_id, d.demand_qty) for d in n.demands),
                )
                for n in self.new_items
            ),
            demands=tuple(
                DemandChange(d.plan_item_id, d.department_id, d.demand_qty) for d in self.demands
            ),
            base_amendment_no=self.base_amendment_no,
        )


class ValuesOut(BaseModel):
    plan_type: PlanType
    item_id: str | None  # None: a row without a HOSxP item (D-43)
    owner_department_id: str
    purchasing_department_id: str | None
    fund_source_id: str
    budget_category_id: str
    planned_qty: Decimal
    estimated_unit_price: Decimal
    planned_amount: Decimal
    item_name: str  # D-43
    unit: str


class BudgetDiffOut(BaseModel):
    fund_source_id: str
    budget_category_id: str
    before: Decimal | None
    after: Decimal


class ItemDiffOut(BaseModel):
    plan_item_id: int
    item_code: str
    item_name: str
    unit: str
    used_qty: Decimal
    used_amount: Decimal
    before: ValuesOut
    after: ValuesOut
    qty_delta: Decimal
    amount_delta: Decimal


class DemandDiffOut(BaseModel):
    target: str  # plan item id, or the new item's ref
    department_id: str
    before: Decimal | None
    after: Decimal


class NewItemOut(BaseModel):
    ref: str
    values: ValuesOut
    demands: list[DemandIn]


class PreviewOut(BaseModel):
    ok: bool
    errors: list[IssueOut]
    warnings: list[IssueOut]
    budgets: list[BudgetDiffOut]
    items: list[ItemDiffOut]
    new_items: list[NewItemOut]
    demands: list[DemandDiffOut]


class ChangeOut(BaseModel):
    target: str
    target_id: int
    change_kind: str
    before: dict[str, Any] | None
    after: dict[str, Any]


class AmendmentOut(BaseModel):
    id: int
    fiscal_year: int
    amendment_no: int
    approval_document_no: str
    approval_date: date
    reason: str
    created_by_user_id: int
    created_at: str
    changes: list[ChangeOut]


def _values(v: ItemValues) -> ValuesOut:
    return ValuesOut(
        plan_type=v.plan_type,
        item_id=v.item_id,
        owner_department_id=v.owner_department_id,
        purchasing_department_id=v.purchasing_department_id,
        fund_source_id=v.fund_source_id,
        budget_category_id=v.budget_category_id,
        planned_qty=v.planned_qty,
        estimated_unit_price=v.estimated_unit_price,
        planned_amount=v.planned_amount,
        item_name=v.item_name,
        unit=v.unit,
    )


def _issue(i: AmendmentIssue) -> IssueOut:
    return IssueOut(code=i.code.value, message=i.message, subject=i.subject)


def _preview(p: am.AmendmentPreview) -> PreviewOut:
    plan: AmendmentPlan = p.plan
    zero = Decimal(0)
    return PreviewOut(
        ok=plan.ok,
        errors=[_issue(e) for e in plan.errors],
        warnings=[_issue(w) for w in plan.warnings],
        budgets=[
            BudgetDiffOut(
                fund_source_id=d.fund_source_id,
                budget_category_id=d.budget_category_id,
                before=d.before,
                after=d.after,
            )
            for d in plan.budget_diffs
        ],
        items=[
            ItemDiffOut(
                plan_item_id=d.plan_item_id,
                item_code=p.item_labels.get(d.plan_item_id, ("", "", ""))[0],
                item_name=p.item_labels.get(d.plan_item_id, ("", "", ""))[1],
                unit=p.item_labels.get(d.plan_item_id, ("", "", ""))[2],
                used_qty=p.used.get(d.plan_item_id, (zero, zero))[0],
                used_amount=p.used.get(d.plan_item_id, (zero, zero))[1],
                before=_values(d.before),
                after=_values(d.after),
                qty_delta=d.qty_delta,
                amount_delta=d.amount_delta,
            )
            for d in plan.item_diffs
        ],
        new_items=[
            NewItemOut(
                ref=n.ref,
                values=_values(n.values),
                demands=[DemandIn(department_id=d, demand_qty=q) for d, q in n.demands],
            )
            for n in plan.new_items
        ],
        demands=[
            DemandDiffOut(
                target=d.target, department_id=d.department_id, before=d.before, after=d.after
            )
            for d in plan.demand_diffs
        ],
    )


def _out(r: AmendmentRecord) -> AmendmentOut:
    return AmendmentOut(
        id=r.id,
        fiscal_year=r.fiscal_year,
        amendment_no=r.amendment_no,
        approval_document_no=r.approval_document_no,
        approval_date=r.approval_date,
        reason=r.reason,
        created_by_user_id=r.created_by_user_id,
        created_at=r.created_at.isoformat(),
        changes=[
            ChangeOut(
                target=c.target,
                target_id=c.target_id,
                change_kind=c.change_kind,
                before=c.before,
                after=c.after,
            )
            for c in r.changes
        ],
    )


def _scope(user: UserRecord) -> str | None:
    return None if sees_all_departments(user.roles) else (user.department_source_id or "")


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    Reader = Annotated[UserRecord, Depends(require(Permission.PLAN_READ))]  # noqa: N806
    Amender = Annotated[UserRecord, Depends(require(Permission.PLAN_AMEND))]  # noqa: N806

    @app.post("/api/plan-years/{fiscal_year}/amendments/preview", response_model=PreviewOut)
    def preview(fiscal_year: int, body: AmendmentIn, _: Amender) -> PreviewOut:
        try:
            with unit_of_work(deps.engine) as uow:
                p = am.preview_amendment(uow, fiscal_year, body.to_request(), deps.today())
        except ServiceError as exc:
            raise _http(exc) from exc
        return _preview(p)

    @app.post(
        "/api/plan-years/{fiscal_year}/amendments", response_model=AmendmentOut, status_code=201
    )
    def amend(fiscal_year: int, body: AmendmentIn, user: Amender) -> AmendmentOut:
        try:
            with unit_of_work(deps.engine) as uow:
                rec = am.amend_plan(
                    uow, Actor(user.id), fiscal_year, body.to_request(), deps.today()
                )
        except ServiceError as exc:
            raise _http(exc) from exc
        return _out(rec)

    @app.get("/api/plan-years/{fiscal_year}/amendments", response_model=list[AmendmentOut])
    def list_amendments(fiscal_year: int, user: Reader) -> list[AmendmentOut]:
        try:
            with unit_of_work(deps.engine) as uow:
                recs = am.list_amendments(uow, fiscal_year)
        except ServiceError as exc:
            raise _http(exc) from exc
        return [_out(r) for r in am.scoped(recs, _scope(user))]

    @app.get("/api/plan-amendments/{amendment_id}", response_model=AmendmentOut)
    def get_amendment(amendment_id: int, user: Reader) -> AmendmentOut:
        try:
            with unit_of_work(deps.engine) as uow:
                rec = am.get_amendment(uow, amendment_id)
            visible = am.scoped([rec], _scope(user))
            if not visible:  # outside the caller's scope: indistinguishable from absent
                raise NotFoundError("AMENDMENT_NOT_FOUND", f"amendment {amendment_id} not found")
        except ServiceError as exc:
            raise _http(exc) from exc
        return _out(visible[0])
