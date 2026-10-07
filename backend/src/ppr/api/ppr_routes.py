"""Wave 4 routes: PPR draft, allocation, discard, confirm/reconfirm, unlock, versions, print.

Permissions (server-side): PPR_READ (all roles; REQUESTER-only users read the PPRs they
created, others answer 404 - D-41), PPR_REQUEST (REQUESTER; the service additionally
requires the requester who created the PPR, D-09 as amended by D-41; also for the Assigned
Purchase coverage, D-38 A-2), PPR_UNLOCK (PLAN_OFFICER, ADMIN; §15.2). Discarding a draft:
its creator, or a Plan Officer / Admin with a reason (D-41: an abandoned draft).
Plan rows (D-43): the creator chooses the plan row of every line when preparing the draft
and may choose again while it is a draft or unlocked (PPR_REQUEST); the choice view is a
PPR_READ.
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ppr.api.ppr_print import render_ppr
from ppr.api.pr_routes import MAX_CHOICES, PrevalidationOut, parse_choices
from ppr.api.pr_routes import _out as prevalidation_out
from ppr.application import ppr_services as svc
from ppr.application import procurement_services as psvc
from ppr.application.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    ServiceError,
    UpstreamUnavailableError,
)
from ppr.domain.permissions import Permission, sees_all_departments
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import PprAllocationRecord, PprRecord, PprScope, UserRecord

Money = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]


class ChoiceIn(BaseModel):
    """D-43: the plan row a PR line uses."""

    pr_item_id: str = Field(min_length=1, max_length=100)
    plan_item_id: int = Field(gt=0, le=2**63 - 1)


def _choices(rows: list[ChoiceIn]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        if r.pr_item_id in out:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "DUPLICATE_CHOICE",
                    "message": f"PR line {r.pr_item_id} is chosen twice",
                },
            )
        out[r.pr_item_id] = r.plan_item_id
    return out


class CreateIn(BaseModel):
    pr_no: str = Field(min_length=1)
    choices: list[ChoiceIn] = Field(default_factory=list, max_length=MAX_CHOICES)


class ChoicesIn(BaseModel):
    choices: list[ChoiceIn] = Field(max_length=MAX_CHOICES)


class ChoicesOut(BaseModel):
    stored: list[ChoiceIn]  # what is saved with the PPR
    check: PrevalidationOut  # the lines read live, checked with stored (or trial) choices


class AllocationIn(BaseModel):
    budget_category_id: str = Field(min_length=1)
    amount: Money


class AllocationsIn(BaseModel):
    allocations: list[AllocationIn]
    adjustment_reference: str | None = None


Qty = Annotated[Decimal, Field(ge=0, max_digits=18, decimal_places=4)]


class CoverageRowIn(BaseModel):
    plan_item_id: int
    department_id: str = Field(min_length=1)
    qty: Qty  # 0 = not bought for this department


class CoverageIn(BaseModel):
    coverage: list[CoverageRowIn] = Field(max_length=2000)


class DemandLineOut(BaseModel):
    department_id: str
    demand_qty: Decimal
    covered_by_others: Decimal
    state: str
    working: Decimal
    confirmed: Decimal


class ItemCoverageOut(BaseModel):
    plan_item_id: int
    item_code: str
    item_name: str
    unit: str
    drawn_qty: Decimal
    lines: list[DemandLineOut]


class DiscardIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)  # required for governance (D-41)


class UnlockIn(BaseModel):
    reason: str | None = None


class PprItemOut(BaseModel):
    pr_item_id: str
    item_id: str
    item_name: str | None
    unit: str | None
    qty: Decimal
    unit_price: Decimal
    amount: Decimal
    plan_item_id: int
    plan_item_name: str | None  # D-43: the plan row the line uses
    plan_item_unit: str | None
    plan_item_code: str | None


class AllocationOut(BaseModel):
    budget_category_id: str
    amount: Decimal


class PprOut(BaseModel):
    id: int
    pr_no: str
    fiscal_year: int
    department_id: str
    fund_source_id: str
    pr_budget_category_id: str | None
    state: str
    ppr_number: str | None
    created_by_user_id: int  # D-41: who prepared it (only they confirm it)
    version: int
    required_amount: Decimal
    allocated_amount: Decimal
    multi_category: bool
    adjustment_reference: str | None
    hosxp_pr_status: str
    created_at: datetime
    updated_at: datetime
    items: list[PprItemOut]
    allocations: list[AllocationOut]


class VersionOut(BaseModel):
    version: int
    document_code: str
    confirmed_by_user_id: int
    confirmed_at: datetime
    snapshot: dict[str, Any]


def _out(r: PprRecord) -> PprOut:
    return PprOut(
        id=r.id,
        pr_no=r.pr_no,
        fiscal_year=r.fiscal_year,
        department_id=r.department_id,
        fund_source_id=r.fund_source_id,
        pr_budget_category_id=r.pr_budget_category_id,
        state=r.state.value,
        ppr_number=r.ppr_number,
        created_by_user_id=r.created_by_user_id,
        version=r.current_version,
        required_amount=r.required_amount,
        allocated_amount=sum((a.amount for a in r.allocations), Decimal(0)),
        multi_category=len(r.allocations) > 1,
        adjustment_reference=r.adjustment_reference,
        hosxp_pr_status=r.hosxp_pr_status,
        created_at=r.created_at,
        updated_at=r.updated_at,
        items=[
            PprItemOut(
                pr_item_id=i.pr_item_id,
                item_id=i.item_id,
                item_name=i.item_name,
                unit=i.unit,
                qty=i.qty,
                unit_price=i.unit_price,
                amount=i.amount,
                plan_item_id=i.plan_item_id,
                plan_item_name=i.plan_item_name,
                plan_item_unit=i.plan_item_unit,
                plan_item_code=i.plan_item_code,
            )
            for i in r.items
        ],
        allocations=[
            AllocationOut(budget_category_id=a.budget_category_id, amount=a.amount)
            for a in r.allocations
        ],
    )


def _http(exc: ServiceError) -> HTTPException:
    code = {
        NotFoundError: status.HTTP_404_NOT_FOUND,
        ConflictError: status.HTTP_409_CONFLICT,
        InvalidInputError: status.HTTP_422_UNPROCESSABLE_CONTENT,
        ForbiddenError: status.HTTP_403_FORBIDDEN,
        UpstreamUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    }.get(type(exc), status.HTTP_400_BAD_REQUEST)
    detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if exc.details is not None:
        detail["details"] = exc.details
    return HTTPException(status_code=code, detail=detail)


def _caller(user: UserRecord) -> svc.Caller:
    roles = frozenset(user.roles)
    # D-41: a user whose only role is REQUESTER reads the PPRs they created.
    scope = None if sees_all_departments(roles) else PprScope(user.id)
    return svc.Caller(user.id, roles, scope)


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    Reader = Annotated[UserRecord, Depends(require(Permission.PPR_READ))]  # noqa: N806
    Requester = Annotated[UserRecord, Depends(require(Permission.PPR_REQUEST))]  # noqa: N806
    Unlocker = Annotated[UserRecord, Depends(require(Permission.PPR_UNLOCK))]  # noqa: N806
    Discarder = Annotated[UserRecord, Depends(require(Permission.PPR_DISCARD))]  # noqa: N806

    def run(fn: Callable[..., Any], *args: Any) -> Any:
        try:
            with unit_of_work(deps.engine) as uow:
                return fn(uow, *args)
        except ServiceError as exc:
            raise _http(exc) from exc

    @app.post("/api/pprs", response_model=PprOut, status_code=201)
    def create_draft(body: CreateIn, user: Requester) -> PprOut:
        rec = run(
            svc.prepare_draft,
            deps.gateway,
            deps.fiscal_year_config,
            deps.today(),
            _caller(user),
            body.pr_no,
            _choices(body.choices),
        )
        return _out(rec)

    @app.get("/api/pprs", response_model=list[PprOut])
    def list_pprs(user: Reader, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> list[PprOut]:
        return [_out(r) for r in run(svc.list_pprs, _caller(user), limit)]

    @app.get("/api/pprs/{ppr_id}", response_model=PprOut)
    def get_ppr(ppr_id: int, user: Reader) -> PprOut:
        return _out(run(svc.get_ppr, _caller(user), ppr_id))

    @app.get("/api/pprs/{ppr_id}/eligible-categories", response_model=list[str])
    def eligible_categories(ppr_id: int, user: Reader) -> list[str]:
        def fn(uow: Any, caller: svc.Caller) -> list[str]:
            return sorted(svc.eligible_categories(uow, svc.get_ppr(uow, caller, ppr_id)))

        return list(run(fn, _caller(user)))

    @app.put("/api/pprs/{ppr_id}/allocations", response_model=PprOut)
    def set_allocations(ppr_id: int, body: AllocationsIn, user: Requester) -> PprOut:
        allocs = [PprAllocationRecord(a.budget_category_id, a.amount) for a in body.allocations]
        rec = run(svc.set_allocations, _caller(user), ppr_id, allocs, body.adjustment_reference)
        return _out(rec)

    def _coverage_out(items: Any) -> list[ItemCoverageOut]:
        return [
            ItemCoverageOut(
                plan_item_id=i.plan_item_id,
                item_code=i.item_code,
                item_name=i.item_name,
                unit=i.unit,
                drawn_qty=i.drawn_qty,
                lines=[DemandLineOut(**vars(x)) for x in i.lines],
            )
            for i in items
        ]

    @app.get("/api/pprs/{ppr_id}/coverage", response_model=list[ItemCoverageOut])
    def get_coverage(ppr_id: int, user: Reader) -> list[ItemCoverageOut]:
        rec = run(
            svc.coverage, deps.gateway, deps.fiscal_year_config, deps.today(), _caller(user), ppr_id
        )
        return _coverage_out(rec)

    @app.put("/api/pprs/{ppr_id}/coverage", response_model=list[ItemCoverageOut])
    def set_coverage(ppr_id: int, body: CoverageIn, user: Requester) -> list[ItemCoverageOut]:
        wanted: dict[int, dict[str, Decimal]] = {}
        for row in body.coverage:
            depts = wanted.setdefault(row.plan_item_id, {})
            if row.department_id in depts:
                raise _http(
                    InvalidInputError("DUPLICATE_ROW", "a department is given twice for an item")
                )
            depts[row.department_id] = row.qty
        rec = run(
            svc.set_coverage,
            deps.gateway,
            deps.fiscal_year_config,
            deps.today(),
            _caller(user),
            ppr_id,
            wanted,
        )
        return _coverage_out(rec)

    @app.get("/api/pprs/{ppr_id}/choices", response_model=ChoicesOut)
    def get_choices(
        ppr_id: int,
        user: Reader,
        choice: Annotated[list[str] | None, Query()] = None,
    ) -> ChoicesOut:
        trial = parse_choices(choice) if choice else None
        stored, rep = run(
            svc.choice_report,
            deps.gateway,
            deps.fiscal_year_config,
            deps.today(),
            _caller(user),
            ppr_id,
            trial,
        )
        return ChoicesOut(
            stored=[ChoiceIn(pr_item_id=k, plan_item_id=v) for k, v in sorted(stored.items())],
            check=prevalidation_out(rep),
        )

    @app.put("/api/pprs/{ppr_id}/choices", response_model=PprOut)
    def set_choices(ppr_id: int, body: ChoicesIn, user: Requester) -> PprOut:
        rec = run(
            svc.set_choices,
            deps.gateway,
            deps.fiscal_year_config,
            deps.today(),
            _caller(user),
            ppr_id,
            _choices(body.choices),
        )
        return _out(rec)

    @app.post("/api/pprs/{ppr_id}/discard", status_code=204)
    def discard(ppr_id: int, user: Discarder, body: DiscardIn | None = None) -> None:
        # The creator (REQUESTER), or a Plan Officer / Admin with a reason (D-41); the
        # service decides - every other caller is refused there.
        run(svc.discard_draft, _caller(user), ppr_id, body.reason if body else None)

    @app.post("/api/pprs/{ppr_id}/confirm", response_model=PprOut)
    def confirm(ppr_id: int, user: Requester) -> PprOut:
        rec = run(
            svc.confirm,
            deps.gateway,
            deps.fiscal_year_config,
            deps.today(),
            _caller(user),
            ppr_id,
        )
        return _out(rec)

    @app.post("/api/pprs/{ppr_id}/unlock", response_model=PprOut)
    def unlock(ppr_id: int, body: UnlockIn, user: Unlocker) -> PprOut:
        return _out(run(svc.unlock, _caller(user), ppr_id, body.reason))

    @app.get("/api/pprs/{ppr_id}/versions", response_model=list[VersionOut])
    def versions(ppr_id: int, user: Reader) -> list[VersionOut]:
        return [
            VersionOut(
                version=v.version,
                document_code=v.document_code,
                confirmed_by_user_id=v.confirmed_by_user_id,
                confirmed_at=v.confirmed_at,
                snapshot=v.snapshot,
            )
            for v in run(svc.versions, _caller(user), ppr_id)
        ]

    @app.get("/api/pprs/{ppr_id}/print", response_class=HTMLResponse)
    def print_ppr(
        ppr_id: int,
        user: Reader,
        version: Annotated[int | None, Query(ge=1)] = None,
    ) -> HTMLResponse:
        def fn(uow: Any, caller: svc.Caller) -> tuple[PprRecord, list[Any]]:
            return svc.get_ppr(uow, caller, ppr_id), svc.versions(uow, caller, ppr_id)

        rec, vs = run(fn, _caller(user))
        if not vs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "NOT_CONFIRMED", "message": "a draft has no printable PPR"},
            )
        wanted = version if version is not None else vs[-1].version
        chosen = next((v for v in vs if v.version == wanted), None)
        if chosen is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "VERSION_NOT_FOUND", "message": f"no version {wanted}"},
            )
        # Wave 5B: the printed version is timeline evidence (§33, §35).
        run(lambda uow, caller: psvc.record_print(uow, caller, rec, chosen.version), _caller(user))
        html = render_ppr(
            chosen.snapshot,
            chosen.document_code,
            current_state=rec.state,
            current_version=rec.current_version,
        )
        return HTMLResponse(html)
