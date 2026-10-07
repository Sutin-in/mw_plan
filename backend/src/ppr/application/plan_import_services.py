"""Plan import from Excel (Wave 12A-1, D-42): preview, import, remove, list.

The Excel file itself is read by the API layer (``ppr.api.plan_import_xlsx``); this module
gets its two sheets as plain rows, checks them with ``ppr.domain.plan_import`` against the
synchronized HOSxP masters and the plan year, and writes them all or nothing.

* Only a **DRAFT** plan year takes an import or a removal (D-42; an APPROVED plan is
  corrected item by item, D-15, and an ACTIVE one only by amendment).
* The same file (SHA-256) is not imported twice into a year while that import stands.
* One audit entry per import or removal; each Plan Item keeps its import, sheet and row.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ppr.application.errors import ConflictError, InvalidInputError, NotFoundError
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.plan_import import (
    ExistingItem,
    ImportCode,
    ImportIssue,
    ImportReport,
    MasterFacts,
    SheetRow,
    check_import,
)
from ppr.ports import (
    Actor,
    AuditEntry,
    ImportedPlanItem,
    ItemSnapshot,
    MasterKind,
    PlanImportRecord,
    UnitOfWork,
)

MAX_REASON = 1000


@dataclass(frozen=True)
class ImportFile:
    """An uploaded workbook, already read into rows (the API reads the Excel file)."""

    file_name: str
    sha256: str
    headings: tuple[SheetRow, ...]
    rows: tuple[SheetRow, ...]


@dataclass(frozen=True)
class ImportPreview:
    fiscal_year: int
    file_name: str
    sha256: str
    report: ImportReport
    duplicate_of: PlanImportRecord | None


def _year(uow: UnitOfWork, fiscal_year: int, *, lock: bool) -> int:
    if lock:
        locked = uow.plans.lock_year(fiscal_year)
        if locked is None:
            raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
        year_id, state = locked
    else:
        year = uow.plan_years.get(fiscal_year)
        if year is None:
            raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
        state = year.state
        year_id = 0
    if state is not PlanYearState.DRAFT:
        raise ConflictError(
            "PLAN_YEAR_NOT_DRAFT",
            f"plan year {fiscal_year} is {state}: a plan is imported (or an import removed) "
            "only while the year is DRAFT (D-42)",
        )
    return year_id


def _masters(uow: UnitOfWork) -> MasterFacts:
    def active(kind: MasterKind) -> dict[str, bool]:
        return {m.source_id: m.active for m in uow.masters.list(kind)}

    return MasterFacts(
        departments=active(MasterKind.DEPARTMENT),
        fund_sources=active(MasterKind.FUND_SOURCE),
        budget_categories=active(MasterKind.BUDGET_CATEGORY),
    )


def _check(uow: UnitOfWork, fiscal_year: int, f: ImportFile) -> ImportReport:
    budgets = [
        (b.id, b.fund_source_id, b.budget_category_id, b.approved_amount)
        for b in uow.plans.budgets(fiscal_year)
    ]
    existing = [
        ExistingItem(
            r.fund_source_id,
            r.budget_category_id,
            r.planned_amount,
        )
        for r in uow.plans.items(fiscal_year)
    ]
    return check_import(f.headings, f.rows, _masters(uow), budgets, existing)


def preview_import(uow: UnitOfWork, fiscal_year: int, f: ImportFile) -> ImportPreview:
    """Check a file and say what an import would do; writes nothing."""
    _year(uow, fiscal_year, lock=False)
    return ImportPreview(
        fiscal_year,
        f.file_name,
        f.sha256,
        _check(uow, fiscal_year, f),
        uow.plan_imports.live_with_hash(fiscal_year, f.sha256),
    )


def issue_payload(i: ImportIssue) -> dict[str, Any]:
    return {
        "code": i.code.value,
        "message": i.message,
        "sheet": i.sheet,
        "row": i.row,
        "column": i.column,
    }


def import_plan(uow: UnitOfWork, actor: Actor, fiscal_year: int, f: ImportFile) -> PlanImportRecord:
    """Import a checked file into a DRAFT plan year - all rows or none (D-42)."""
    if actor.user_id is None:
        raise InvalidInputError("USER_REQUIRED", "an import must name who imports it")
    year_id = _year(uow, fiscal_year, lock=True)
    if (dup := uow.plan_imports.live_with_hash(fiscal_year, f.sha256)) is not None:
        raise ConflictError(
            "DUPLICATE_FILE",
            f"this file was already imported into {fiscal_year} (import {dup.id}, "
            f"{dup.file_name}); remove that import first to import it again",
        )
    report = _check(uow, fiscal_year, f)
    if not report.ok:
        raise ConflictError(
            "IMPORT_HAS_ERRORS",
            f"the file has {len(report.errors)} error(s); nothing was imported",
            details=[issue_payload(i) for i in report.errors[:200]],
        )
    import_id = uow.plan_imports.create(
        year_id,
        f.file_name,
        f.sha256,
        len(report.rows),
        len(report.skipped),
        report.total_amount,
        actor.user_id,
    )
    items = []
    for row in report.rows:
        items.append(
            ImportedPlanItem(
                plan_budget_id=row.plan_budget_id,
                plan_type=row.plan_type,
                item_id=None,  # D-43: the plan holds no HOSxP item codes
                snapshot=ItemSnapshot(None, row.name, row.unit),
                owner_department_id=row.owner_department_id,
                purchasing_department_id=row.purchasing_department_id,
                planned_qty=row.planned_qty,
                estimated_unit_price=row.estimated_unit_price,
                planned_amount=row.planned_amount,
                quarters=row.quarters,
                note=row.note,
                source_sheet=row.source_sheet,
                source_row=row.source_row,
            )
        )
    written = uow.plans.create_imported_items(year_id, import_id, items, actor.user_id)
    rec = uow.plan_imports.get(import_id)
    assert rec is not None and written == len(report.rows)
    uow.audit.write(
        AuditEntry(
            actor,
            "PLAN_IMPORTED",
            "plan_import",
            str(import_id),
            after={
                "fiscal_year": fiscal_year,
                "file_name": f.file_name,
                "file_sha256": f.sha256,
                "rows_imported": written,
                "rows_skipped": len(report.skipped),
                "warnings": len(report.warnings),
                "total_amount": str(report.total_amount),
                "budgets": [
                    {
                        "fund_source_id": b.fund_source_id,
                        "budget_category_id": b.budget_category_id,
                        "imported": str(b.imported),
                        "rows": b.rows,
                    }
                    for b in report.budgets
                    if b.rows
                ],
            },
        )
    )
    return rec


def remove_import(uow: UnitOfWork, actor: Actor, import_id: int, reason: str) -> PlanImportRecord:
    """Delete the Plan Items of one import while the year is DRAFT; the import record and
    the audit trail stay (D-42)."""
    if actor.user_id is None:
        raise InvalidInputError("USER_REQUIRED", "a removal must name who removes it")
    reason = (reason or "").strip()
    if not reason:
        raise InvalidInputError("REASON_REQUIRED", "say why the import is removed")
    if len(reason) > MAX_REASON:
        raise InvalidInputError("REASON_TOO_LONG", f"at most {MAX_REASON} characters")
    found = uow.plan_imports.get(import_id)
    if found is None:
        raise NotFoundError("IMPORT_NOT_FOUND", f"import {import_id} not found")
    _year(uow, found.fiscal_year, lock=True)
    rec = uow.plan_imports.get(import_id)  # read again under the year lock (double click)
    assert rec is not None
    if rec.state != "IMPORTED":
        raise ConflictError("IMPORT_ALREADY_REMOVED", f"import {import_id} was already removed")
    deleted = uow.plans.delete_imported_items(import_id)
    if not uow.plan_imports.mark_removed(import_id, actor.user_id, reason):
        raise ConflictError("IMPORT_ALREADY_REMOVED", f"import {import_id} was already removed")
    after = uow.plan_imports.get(import_id)
    assert after is not None
    uow.audit.write(
        AuditEntry(
            actor,
            "PLAN_IMPORT_REMOVED",
            "plan_import",
            str(import_id),
            before={"state": rec.state, "rows_imported": rec.rows_imported},
            after={"state": after.state, "plan_items_deleted": deleted},
            reason=reason,
        )
    )
    return after


def list_imports(uow: UnitOfWork, fiscal_year: int) -> list[PlanImportRecord]:
    if uow.plan_years.get(fiscal_year) is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
    return uow.plan_imports.list(fiscal_year)


__all__ = [
    "ImportCode",
    "ImportFile",
    "ImportPreview",
    "import_plan",
    "issue_payload",
    "list_imports",
    "preview_import",
    "remove_import",
]
