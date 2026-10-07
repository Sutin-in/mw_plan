"""Wave 3 route: live PR retrieval + Pre-PPR validation (spec §11, §12, §13, §25.1, §26).

``GET /api/prs/{pr_no}/prevalidation`` is read-only: it retrieves the PR live from HOSxP,
validates it with the requester's plan-row choices (D-43,
``?choice=pr_item_id:plan_item_id``, repeatable) and writes nothing; the answer lists, per
line, the plan rows it may choose. HOSxP unavailable -> 503 (never stale data). A requester
may validate a PR of any department (D-41): the PPR takes the PR's department.
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, status
from pydantic import BaseModel

from ppr.application import pr_services
from ppr.application.errors import ForbiddenError, ServiceError, UpstreamUnavailableError
from ppr.domain.permissions import Permission
from ppr.domain.violations import Violation
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import UserRecord


class ViolationOut(BaseModel):
    code: str
    message: str
    subject: str | None


class PrHeaderOut(BaseModel):
    pr_no: str
    pr_date: date | None
    budget_year: int | None
    department_id: str | None
    fund_source_id: str | None
    budget_category_id: str | None
    requester_name: str | None
    header_total: Decimal | None
    hosxp_status: str


class PrLineOut(BaseModel):
    pr_item_id: str
    item_id: str
    item_code: str | None
    item_name: str | None
    qty: Decimal
    unit: str | None
    unit_price: Decimal
    amount: Decimal
    matched_plan_item_id: int | None
    plan_remaining_qty: Decimal | None
    plan_remaining_amount: Decimal | None
    passed: bool
    violations: list[ViolationOut]
    offered: list[int]  # D-43: the plan rows this line may choose


class PlanRowOptionOut(BaseModel):
    """A plan row offered to at least one line (D-43), with its remaining balance."""

    id: int
    item_name: str
    unit: str
    item_id: str | None  # the row's HOSxP item, if it has one (shown; never matched, D-43)
    item_code: str | None
    plan_type: str
    budget_category_id: str
    owner_department_id: str
    remaining_qty: Decimal
    remaining_amount: Decimal


class PrevalidationOut(BaseModel):
    pr_no: str
    found: bool
    current_fiscal_year: int
    eligible: bool
    lines_checked: bool
    required_amount: Decimal
    header: PrHeaderOut | None
    header_violations: list[ViolationOut]
    lines: list[PrLineOut]
    options: list[PlanRowOptionOut]


MAX_CHOICES = 500


def parse_choices(raw: list[str]) -> dict[str, int]:
    """``pr_item_id:plan_item_id`` pairs (D-43). A malformed pair is refused (400), never
    silently ignored; the last pair for a PR line wins."""
    if len(raw) > MAX_CHOICES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_CHOICE", "message": "too many choices"},
        )
    out: dict[str, int] = {}
    for pair in raw:
        line, sep, plan = pair.rpartition(":")
        if not sep or not line or not plan.isdecimal() or len(plan) > 18:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "BAD_CHOICE", "message": "choice must be pr_item_id:plan_item_id"},
            )
        out[line] = int(plan)
    return out


def _v(v: Violation) -> ViolationOut:
    return ViolationOut(code=v.code.value, message=v.message, subject=v.subject)


def _out(rep: pr_services.PrevalidationReport) -> PrevalidationOut:
    res = rep.result
    h = rep.header
    header = (
        PrHeaderOut(
            pr_no=h.pr_no,
            pr_date=h.pr_date,
            budget_year=h.fiscal_year,
            department_id=h.department_id,
            fund_source_id=h.fund_source_id,
            budget_category_id=h.budget_category_id,
            requester_name=h.requester_name,
            header_total=h.total_amount,
            hosxp_status=h.status.value,
        )
        if h is not None
        else None
    )
    lines = [
        PrLineOut(
            pr_item_id=item.pr_item_id,
            item_id=item.item_id,
            item_code=item.item_code,
            item_name=item.item_name,
            qty=item.qty,
            unit=item.unit,
            unit_price=item.unit_price,
            amount=item.amount,
            matched_plan_item_id=(
                int(line.matched_plan_item_id) if line.matched_plan_item_id else None
            ),
            plan_remaining_qty=line.remaining.qty if line.remaining else None,
            plan_remaining_amount=line.remaining.amount if line.remaining else None,
            passed=line.passed and res.lines_checked,
            violations=[_v(v) for v in line.violations],
            offered=[int(p) for p in line.offered],
        )
        for item, line in zip(rep.items, res.lines, strict=True)
    ]
    return PrevalidationOut(
        pr_no=rep.pr_no,
        found=h is not None,
        current_fiscal_year=rep.current_fiscal_year,
        eligible=res.eligible,
        lines_checked=res.lines_checked,
        required_amount=res.required_amount,
        header=header,
        header_violations=[_v(v) for v in res.header_violations],
        lines=lines,
        options=[
            PlanRowOptionOut(
                id=rec.id,
                item_name=rec.item_name,
                unit=rec.unit,
                item_id=rec.item_id,
                item_code=rec.item_code,
                plan_type=rec.plan_type.value,
                budget_category_id=rec.budget_category_id,
                owner_department_id=rec.owner_department_id,
                remaining_qty=rep.offered_remaining[pid].qty,
                remaining_amount=rep.offered_remaining[pid].amount,
            )
            for pid, rec in sorted(rep.offered.items(), key=lambda kv: kv[1].id)
        ],
    )


def _http(exc: ServiceError) -> HTTPException:
    code = {
        ForbiddenError: status.HTTP_403_FORBIDDEN,
        UpstreamUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    }.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return HTTPException(status_code=code, detail={"code": exc.code, "message": str(exc)})


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    Validator = Annotated[UserRecord, Depends(require(Permission.PPR_PREVALIDATE))]  # noqa: N806

    @app.get("/api/prs/{pr_no}/prevalidation", response_model=PrevalidationOut)
    def prevalidate_pr(
        pr_no: str,
        user: Validator,
        choice: Annotated[list[str] | None, Query()] = None,
    ) -> PrevalidationOut:
        # D-41: a requester checks a PR of any department (the PPR takes the PR's).
        try:
            with unit_of_work(deps.engine) as uow:
                rep = pr_services.prevalidate_pr(
                    uow,
                    deps.gateway,
                    deps.fiscal_year_config,
                    deps.today(),
                    pr_no,
                    choices=parse_choices(choice or []),
                )
        except ServiceError as exc:
            raise _http(exc) from exc
        return _out(rep)
