"""Wave 12A-1 routes: import a plan from Excel (D-42).

* ``GET  /api/plan-years/{fy}/import-template`` - the template, with the HOSxP code lists;
* ``POST /api/plan-years/{fy}/imports/preview`` - body: the .xlsx file; checks, writes nothing;
* ``POST /api/plan-years/{fy}/imports`` - body: the same file; imports all rows or none;
* ``GET  /api/plan-years/{fy}/imports`` - the year's imports;
* ``POST /api/plan-imports/{id}/remove`` - remove an import (DRAFT year, with a reason).

Importing and removing need ``PLAN_MANAGE`` (Plan Officer, Admin); listing ``PLAN_READ``.
The file is sent as the raw request body (``application/octet-stream``); its name travels
in the ``file_name`` query parameter and only labels the import.
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

import hashlib
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ppr.api.plan_import_xlsx import (
    MAX_FILE_BYTES,
    ImportFileError,
    TemplateData,
    build_template,
    read_workbook,
)
from ppr.api.plan_routes import _http
from ppr.application import plan_import_services as pis
from ppr.application.errors import ConflictError, NotFoundError, ServiceError
from ppr.domain.permissions import Permission
from ppr.domain.plan_import import ImportIssue
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import Actor, MasterKind, PlanImportRecord, UserRecord

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_ISSUES_SHOWN = 500
FileName = Annotated[str, Query(min_length=1, max_length=200)]
DEFAULT_NAME = "plan.xlsx"


class IssueOut(BaseModel):
    code: str
    message: str
    sheet: str | None
    row: int | None
    column: str | None


class BudgetTotalOut(BaseModel):
    fund_source_id: str
    budget_category_id: str
    approved_amount: Decimal
    already_planned: Decimal
    imported: Decimal
    after_import: Decimal
    difference: Decimal
    rows: int


class ImportOut(BaseModel):
    id: int
    fiscal_year: int
    file_name: str
    file_sha256: str
    rows_imported: int
    rows_skipped: int
    total_amount: Decimal
    state: str
    imported_by_user_id: int
    imported_at: datetime
    removed_by_user_id: int | None
    removed_at: datetime | None
    removed_reason: str | None


class PreviewOut(BaseModel):
    fiscal_year: int
    file_name: str
    file_sha256: str
    can_import: bool
    rows_to_import: int
    total_amount: Decimal
    errors_total: int
    warnings_total: int
    skipped_total: int
    errors: list[IssueOut]
    warnings: list[IssueOut]
    skipped: list[IssueOut]
    budgets: list[BudgetTotalOut]
    duplicate_of: ImportOut | None


class RemoveIn(BaseModel):
    reason: str = Field(min_length=1, max_length=pis.MAX_REASON)


def _issue(i: ImportIssue) -> IssueOut:
    return IssueOut(**pis.issue_payload(i))


def _import_out(r: PlanImportRecord) -> ImportOut:
    return ImportOut(**r.__dict__)


def _file_error(exc: ImportFileError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": exc.code, "message": str(exc)},
    )


async def _body(request: Request) -> bytes:
    """The uploaded file, refused beyond MAX_FILE_BYTES without reading it all."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_FILE_BYTES:
        raise _file_error(ImportFileError("FILE_TOO_LARGE", "ไฟล์ใหญ่เกิน 10 MB"))
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise _file_error(ImportFileError("FILE_TOO_LARGE", "ไฟล์ใหญ่เกิน 10 MB"))
        chunks.append(chunk)
    if not size:
        raise _file_error(ImportFileError("EMPTY_FILE", "ไม่ได้แนบไฟล์"))
    return b"".join(chunks)


def _import_file(data: bytes, file_name: str) -> pis.ImportFile:
    wb = read_workbook(data)  # ImportFileError for a file that is not a usable template
    return pis.ImportFile(
        file_name=file_name.strip() or DEFAULT_NAME,
        sha256=hashlib.sha256(data).hexdigest(),
        headings=wb.headings,
        rows=wb.rows,
    )


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    engine = deps.engine
    Reader = Annotated[UserRecord, Depends(require(Permission.PLAN_READ))]  # noqa: N806
    Manager = Annotated[UserRecord, Depends(require(Permission.PLAN_MANAGE))]  # noqa: N806

    @app.get("/api/plan-years/{fiscal_year}/import-template")
    def import_template(fiscal_year: int, _: Manager) -> Response:
        with unit_of_work(engine) as uow:
            if uow.plan_years.get(fiscal_year) is None:
                raise _http(
                    NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
                )

            def active(kind: MasterKind) -> list[tuple[str, str]]:
                return [
                    (m.source_id, m.name)
                    for m in sorted(uow.masters.list(kind), key=lambda m: m.name)
                    if m.active
                ]

            names = {
                k: {m.source_id: m.name for m in uow.masters.list(k)}
                for k in (MasterKind.FUND_SOURCE, MasterKind.BUDGET_CATEGORY)
            }
            data = TemplateData(
                fiscal_year,
                active(MasterKind.DEPARTMENT),
                active(MasterKind.FUND_SOURCE),
                active(MasterKind.BUDGET_CATEGORY),
                [
                    (
                        b.fund_source_id,
                        names[MasterKind.FUND_SOURCE].get(b.fund_source_id, ""),
                        b.budget_category_id,
                        names[MasterKind.BUDGET_CATEGORY].get(b.budget_category_id, ""),
                        b.approved_amount,
                    )
                    for b in uow.plans.budgets(fiscal_year)
                ],
            )
        content = build_template(data)
        return Response(
            content=content,
            media_type=XLSX,
            headers={
                "Content-Disposition": f'attachment; filename="plan_import_{fiscal_year}.xlsx"'
            },
        )

    def _preview(fiscal_year: int, data: bytes, file_name: str) -> PreviewOut:
        try:
            f = _import_file(data, file_name)
        except ImportFileError as exc:
            raise _file_error(exc) from exc
        try:
            with unit_of_work(engine) as uow:
                p = pis.preview_import(uow, fiscal_year, f)
        except ServiceError as exc:
            raise _http(exc) from exc
        r = p.report
        return PreviewOut(
            fiscal_year=fiscal_year,
            file_name=p.file_name,
            file_sha256=p.sha256,
            can_import=r.ok and p.duplicate_of is None,
            rows_to_import=len(r.rows),
            total_amount=r.total_amount,
            errors_total=len(r.errors),
            warnings_total=len(r.warnings),
            skipped_total=len(r.skipped),
            errors=[_issue(i) for i in r.errors[:MAX_ISSUES_SHOWN]],
            warnings=[_issue(i) for i in r.warnings[:MAX_ISSUES_SHOWN]],
            skipped=[_issue(i) for i in r.skipped[:MAX_ISSUES_SHOWN]],
            budgets=[
                BudgetTotalOut(
                    fund_source_id=b.fund_source_id,
                    budget_category_id=b.budget_category_id,
                    approved_amount=b.approved_amount,
                    already_planned=b.already_planned,
                    imported=b.imported,
                    after_import=b.after_import,
                    difference=b.difference,
                    rows=b.rows,
                )
                for b in r.budgets
            ],
            duplicate_of=_import_out(p.duplicate_of) if p.duplicate_of else None,
        )

    @app.post("/api/plan-years/{fiscal_year}/imports/preview", response_model=PreviewOut)
    async def preview(
        fiscal_year: int, request: Request, _: Manager, file_name: FileName = DEFAULT_NAME
    ) -> PreviewOut:
        data = await _body(request)
        return await run_in_threadpool(_preview, fiscal_year, data, file_name)

    def _import(
        fiscal_year: int, data: bytes, file_name: str, sha: str | None, user_id: int
    ) -> ImportOut:
        try:
            f = _import_file(data, file_name)
        except ImportFileError as exc:
            raise _file_error(exc) from exc
        if sha is not None and sha != f.sha256:
            raise _http(
                ConflictError(
                    "FILE_CHANGED", "the file is not the one that was previewed; preview it again"
                )
            )
        try:
            with unit_of_work(engine) as uow:
                rec = pis.import_plan(uow, Actor(user_id), fiscal_year, f)
        except ServiceError as exc:
            raise _http(exc) from exc
        return _import_out(rec)

    @app.post(
        "/api/plan-years/{fiscal_year}/imports",
        response_model=ImportOut,
        status_code=status.HTTP_201_CREATED,
    )
    async def import_plan(
        fiscal_year: int,
        request: Request,
        user: Manager,
        file_name: FileName = DEFAULT_NAME,
        expected_sha256: Annotated[str | None, Query(pattern="^[0-9a-f]{64}$")] = None,
    ) -> ImportOut:
        data = await _body(request)
        return await run_in_threadpool(
            _import, fiscal_year, data, file_name, expected_sha256, user.id
        )

    @app.get("/api/plan-years/{fiscal_year}/imports", response_model=list[ImportOut])
    def list_imports(fiscal_year: int, _: Reader) -> list[ImportOut]:
        try:
            with unit_of_work(engine) as uow:
                return [_import_out(r) for r in pis.list_imports(uow, fiscal_year)]
        except ServiceError as exc:
            raise _http(exc) from exc

    @app.post("/api/plan-imports/{import_id}/remove", response_model=ImportOut)
    def remove_import(import_id: int, body: RemoveIn, user: Manager) -> ImportOut:
        try:
            with unit_of_work(engine) as uow:
                rec = pis.remove_import(uow, Actor(user.id), import_id, body.reason)
        except ServiceError as exc:
            raise _http(exc) from exc
        return _import_out(rec)
