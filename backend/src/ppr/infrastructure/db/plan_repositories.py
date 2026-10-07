"""SQL implementations of the Wave 2 ports: masters, sync runs, plan, ledger."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Connection,
    Select,
    Table,
    and_,
    delete,
    func,
    insert,
    literal_column,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from ppr.application.errors import ConflictError
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.ledger import (
    LedgerBalance,
    LedgerEntry,
    LedgerEventType,
    balance_from_totals,
)
from ppr.domain.plan import DemandState, PlanType
from ppr.infrastructure.db.mockdata import is_mock_item, is_production
from ppr.infrastructure.db.schema import (
    hosxp_budget_category,
    hosxp_department,
    hosxp_fund_source,
    hosxp_item,
    plan_budget,
    plan_import,
    plan_item,
    plan_item_demand,
    plan_ledger,
    plan_year,
    sync_run,
)
from ppr.ports import (
    AmendedItem,
    BudgetRecord,
    DemandRecord,
    ImportedPlanItem,
    ItemSnapshot,
    MasterKind,
    MasterRecord,
    MasterRow,
    PlanImportRecord,
    PlanItemInput,
    PlanItemRecord,
    SyncRunRecord,
    UniquenessError,
)

MASTER_UPSERT_BATCH = 2000  # rows per INSERT: 2,000 x 5 values at most (10,000), far below 65,535

_MASTER_TABLES: dict[MasterKind, Table] = {
    MasterKind.DEPARTMENT: hosxp_department,
    MasterKind.ITEM: hosxp_item,
    MasterKind.FUND_SOURCE: hosxp_fund_source,
    MasterKind.BUDGET_CATEGORY: hosxp_budget_category,
}
_EXTRA_COLUMNS: dict[MasterKind, tuple[str, ...]] = {
    MasterKind.ITEM: ("unit",),
    MasterKind.BUDGET_CATEGORY: ("parent_source_id",),
}


class SqlMasterRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def upsert(self, kind: MasterKind, records: Sequence[MasterRecord]) -> tuple[int, int]:
        if not records:
            return (0, 0)
        table = _MASTER_TABLES[kind]
        cols = ("code", "name", "active", *_EXTRA_COLUMNS.get(kind, ()))
        rows = [{"source_id": r.source_id, **{c: getattr(r, c) for c in cols}} for r in records]
        # Wave 11B: one multi-row INSERT carries one parameter per value, and PostgreSQL
        # takes at most 65,535 (a hospital has tens of thousands of items). Batches of
        # MASTER_UPSERT_BATCH rows, all in the caller's one transaction: all or nothing.
        # One statement refused a repeated source_id; batches must not hide that.
        if len({r["source_id"] for r in rows}) != len(rows):
            raise ValueError(f"{kind.value}: HOSxP returned the same source_id more than once")
        flags: list[bool] = []
        for i in range(0, len(rows), MASTER_UPSERT_BATCH):
            base = pg_insert(table).values(rows[i : i + MASTER_UPSERT_BATCH])
            changed = or_(*(table.c[c].is_distinct_from(base.excluded[c]) for c in cols))
            stmt: Any = base.on_conflict_do_update(
                index_elements=[table.c.source_id],
                set_={**{c: base.excluded[c] for c in cols}, "synced_at": func.now()},
                where=changed,
            ).returning(literal_column("(xmax = 0)").label("inserted"))
            flags.extend(bool(r.inserted) for r in self._c.execute(stmt))
        created = sum(flags)
        return created, len(flags) - created

    @staticmethod
    def _row(kind: MasterKind, r: Any) -> MasterRow:
        return MasterRow(
            kind=kind,
            source_id=r.source_id,
            code=r.code,
            name=r.name,
            active=r.active,
            unit=getattr(r, "unit", None),
            parent_source_id=getattr(r, "parent_source_id", None),
        )

    def get(self, kind: MasterKind, source_id: str) -> MasterRow | None:
        table = _MASTER_TABLES[kind]
        row = self._c.execute(select(table).where(table.c.source_id == source_id)).one_or_none()
        return self._row(kind, row) if row else None

    def list(self, kind: MasterKind) -> list[MasterRow]:
        table = _MASTER_TABLES[kind]
        return [self._row(kind, r) for r in self._c.execute(select(table).order_by(table.c.code))]


class SqlSyncRunRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def start(
        self,
        mode: str,
        scope: str,
        requested_by: int | None,
        *,
        ppr_id: int | None = None,
        retry_of: int | None = None,
    ) -> int:
        try:
            with self._c.begin_nested():
                return int(
                    self._c.execute(
                        insert(sync_run)
                        .values(
                            mode=mode,
                            scope=scope,
                            requested_by_user_id=requested_by,
                            ppr_id=ppr_id,
                            retry_of_sync_run_id=retry_of,
                        )
                        .returning(sync_run.c.id)
                    ).scalar_one()
                )
        except IntegrityError as exc:
            raise UniquenessError("a PPR synchronization is already running") from exc

    def finish(
        self,
        run_id: int,
        status: str,
        *,
        checked: int = 0,
        created: int = 0,
        updated: int = 0,
        changed: int = 0,
        cancelled: int = 0,
        error: dict[str, Any] | None = None,
    ) -> None:
        self._c.execute(
            update(sync_run)
            .where(sync_run.c.id == run_id)
            .values(
                status=status,
                finished_at=func.now(),
                records_checked=checked,
                records_created=created,
                records_updated=updated,
                records_changed=changed,
                records_cancelled=cancelled,
                error_details=error,
            )
        )

    @staticmethod
    def _record(r: Any) -> SyncRunRecord:
        return SyncRunRecord(
            id=r.id,
            mode=r.mode,
            scope=r.scope,
            status=r.status,
            started_at=r.started_at,
            finished_at=r.finished_at,
            records_checked=r.records_checked,
            records_created=r.records_created,
            records_updated=r.records_updated,
            error_details=r.error_details,
            requested_by_user_id=r.requested_by_user_id,
            records_changed=r.records_changed,
            records_cancelled=r.records_cancelled,
            ppr_id=r.ppr_id,
            retry_of_sync_run_id=r.retry_of_sync_run_id,
        )

    def recent(self, limit: int) -> list[SyncRunRecord]:
        """Master-data synchronization runs (the PR runs have ``recent_ppr_runs``)."""
        rows = self._c.execute(
            select(sync_run)
            .where(sync_run.c.scope == "MASTERS")
            .order_by(sync_run.c.id.desc())
            .limit(limit)
        )
        return [self._record(r) for r in rows]

    def get(self, run_id: int) -> SyncRunRecord | None:
        row = self._c.execute(select(sync_run).where(sync_run.c.id == run_id)).first()
        return self._record(row) if row else None

    def recent_ppr_runs(self, limit: int) -> list[SyncRunRecord]:
        rows = self._c.execute(
            select(sync_run)
            .where(sync_run.c.scope != "MASTERS")
            .order_by(sync_run.c.id.desc())
            .limit(limit)
        )
        return [self._record(r) for r in rows]

    def latest_finished(self, scope: str) -> SyncRunRecord | None:
        row = self._c.execute(
            select(sync_run)
            .where(sync_run.c.scope == scope, sync_run.c.status != "RUNNING")
            .order_by(sync_run.c.id.desc())
            .limit(1)
        ).first()
        return self._record(row) if row else None

    def fail_stale(self, started_before: datetime) -> int:
        result = self._c.execute(
            update(sync_run)
            .where(sync_run.c.status == "RUNNING", sync_run.c.started_at < started_before)
            .values(
                status="FAILED",
                finished_at=func.greatest(func.now(), sync_run.c.started_at),
                error_details={"reason": "STALE_RUN"},
            )
        )
        return int(result.rowcount or 0)


class SqlPlanRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    # -------------------------------------------------------------- plan year
    def lock_year(self, fiscal_year: int) -> tuple[int, PlanYearState] | None:
        row = self._c.execute(
            select(plan_year.c.id, plan_year.c.state)
            .where(plan_year.c.fiscal_year == fiscal_year)
            .with_for_update()
        ).one_or_none()
        return (int(row.id), PlanYearState(row.state)) if row else None

    def year_of_budget(self, budget_id: int) -> int | None:
        return self._c.execute(
            select(plan_year.c.fiscal_year)
            .join(plan_budget, plan_budget.c.plan_year_id == plan_year.c.id)
            .where(plan_budget.c.id == budget_id)
        ).scalar_one_or_none()

    def year_of_item(self, item_id: int) -> int | None:
        return self._c.execute(
            select(plan_year.c.fiscal_year)
            .join(plan_item, plan_item.c.plan_year_id == plan_year.c.id)
            .where(plan_item.c.id == item_id)
        ).scalar_one_or_none()

    # -------------------------------------------------------------- budgets
    def _budget_query(self) -> Select[Any]:
        return select(
            plan_budget.c.id,
            plan_year.c.fiscal_year,
            plan_budget.c.fund_source_source_id,
            plan_budget.c.budget_category_source_id,
            plan_budget.c.approved_amount,
        ).join(plan_year, plan_year.c.id == plan_budget.c.plan_year_id)

    @staticmethod
    def _budget(r: Any) -> BudgetRecord:
        return BudgetRecord(
            r.id,
            r.fiscal_year,
            r.fund_source_source_id,
            r.budget_category_source_id,
            Decimal(r.approved_amount),
        )

    def budgets(self, fiscal_year: int) -> list[BudgetRecord]:
        rows = self._c.execute(
            self._budget_query()
            .where(plan_year.c.fiscal_year == fiscal_year)
            .order_by(plan_budget.c.fund_source_source_id, plan_budget.c.budget_category_source_id)
        )
        return [self._budget(r) for r in rows]

    def get_budget(self, budget_id: int) -> BudgetRecord | None:
        r = self._c.execute(self._budget_query().where(plan_budget.c.id == budget_id)).one_or_none()
        return self._budget(r) if r else None

    def create_budget(
        self, year_id: int, fund_source_id: str, category_id: str, amount: Decimal, by: int | None
    ) -> int:
        return int(
            self._c.execute(
                insert(plan_budget)
                .values(
                    plan_year_id=year_id,
                    fund_source_source_id=fund_source_id,
                    budget_category_source_id=category_id,
                    approved_amount=amount,
                    created_by_user_id=by,
                )
                .returning(plan_budget.c.id)
            ).scalar_one()
        )

    def update_budget_amount(self, budget_id: int, amount: Decimal) -> None:
        self._c.execute(
            update(plan_budget)
            .where(plan_budget.c.id == budget_id)
            .values(approved_amount=amount, updated_at=func.now())
        )

    def delete_budget(self, budget_id: int) -> None:
        self._c.execute(delete(plan_budget).where(plan_budget.c.id == budget_id))

    def budget_item_count(self, budget_id: int) -> int:
        return int(
            self._c.execute(
                select(func.count()).where(plan_item.c.plan_budget_id == budget_id)
            ).scalar_one()
        )

    # -------------------------------------------------------------- items
    def _demands(self, item_ids: list[int]) -> dict[int, tuple[DemandRecord, ...]]:
        out: dict[int, list[DemandRecord]] = {i: [] for i in item_ids}
        if not item_ids:
            return {}
        rows = self._c.execute(
            select(plan_item_demand)
            .where(plan_item_demand.c.plan_item_id.in_(item_ids))
            .order_by(plan_item_demand.c.department_source_id)
        )
        for r in rows:
            out[r.plan_item_id].append(
                DemandRecord(
                    r.department_source_id,
                    Decimal(r.demand_qty),
                    DemandState(r.state),
                    int(r.id),
                )
            )
        return {k: tuple(v) for k, v in out.items()}

    def _item_rows(self, where: Any) -> list[PlanItemRecord]:
        rows = self._c.execute(
            select(
                plan_item,
                plan_year.c.fiscal_year,
                plan_budget.c.fund_source_source_id,
                plan_budget.c.budget_category_source_id,
            )
            .join(plan_year, plan_year.c.id == plan_item.c.plan_year_id)
            .join(plan_budget, plan_budget.c.id == plan_item.c.plan_budget_id)
            .where(where)
            .order_by(plan_item.c.id)
        ).all()
        demands = self._demands([r.id for r in rows])

        def dec(v: Any) -> Decimal | None:
            return None if v is None else Decimal(v)

        return [
            PlanItemRecord(
                id=r.id,
                fiscal_year=r.fiscal_year,
                plan_budget_id=r.plan_budget_id,
                fund_source_id=r.fund_source_source_id,
                budget_category_id=r.budget_category_source_id,
                plan_type=PlanType(r.plan_type),
                item_id=r.item_source_id,
                item_code=r.item_code_snapshot,
                item_name=r.item_name_snapshot,
                unit=r.unit_snapshot,
                owner_department_id=r.owner_department_source_id,
                purchasing_department_id=r.purchasing_department_source_id,
                planned_qty=Decimal(r.planned_qty),
                estimated_unit_price=Decimal(r.estimated_unit_price),
                planned_amount=Decimal(r.planned_amount),
                quarters=(dec(r.q1), dec(r.q2), dec(r.q3), dec(r.q4)),
                note=r.note,
                demands=demands.get(r.id, ()),
                plan_import_id=r.plan_import_id,
                source_sheet=r.source_sheet,
                source_row=r.source_row,
            )
            for r in rows
        ]

    def items(self, fiscal_year: int, department_id: str | None = None) -> list[PlanItemRecord]:
        cond: Any = plan_year.c.fiscal_year == fiscal_year
        if department_id is not None:
            has_demand = (
                select(plan_item_demand.c.id)
                .where(
                    plan_item_demand.c.plan_item_id == plan_item.c.id,
                    plan_item_demand.c.department_source_id == department_id,
                )
                .exists()
            )
            # D-38 C-4: a CENTRAL item is seen by its purchasing department only (and by
            # organisation-wide roles, which pass no department), not by its owner field.
            cond = cond & or_(
                and_(
                    plan_item.c.plan_type != PlanType.CENTRAL.value,
                    plan_item.c.owner_department_source_id == department_id,
                ),
                plan_item.c.purchasing_department_source_id == department_id,
                has_demand,
            )
        return self._item_rows(cond)

    def get_item(self, item_id: int) -> PlanItemRecord | None:
        rows = self._item_rows(plan_item.c.id == item_id)
        return rows[0] if rows else None

    @staticmethod
    def _values(data: PlanItemInput, snapshot: ItemSnapshot) -> dict[str, Any]:
        q1, q2, q3, q4 = data.quarters
        return {
            "plan_budget_id": data.plan_budget_id,
            "plan_type": data.plan_type.value,
            "item_source_id": data.item_id,
            "item_code_snapshot": snapshot.code,
            "item_name_snapshot": snapshot.name,
            "unit_snapshot": snapshot.unit,
            "owner_department_source_id": data.owner_department_id,
            "purchasing_department_source_id": data.purchasing_department_id,
            "planned_qty": data.planned_qty,
            "estimated_unit_price": data.estimated_unit_price,
            "planned_amount": data.planned_amount,
            "q1": q1,
            "q2": q2,
            "q3": q3,
            "q4": q4,
            "note": data.note,
        }

    def _write_demands(self, item_id: int, data: PlanItemInput) -> None:
        self._c.execute(delete(plan_item_demand).where(plan_item_demand.c.plan_item_id == item_id))
        if data.demands:
            self._c.execute(
                insert(plan_item_demand),
                [
                    {
                        "plan_item_id": item_id,
                        "department_source_id": d.department_id,
                        "demand_qty": d.demand_qty,
                    }
                    for d in data.demands
                ],
            )

    def create_item(
        self, year_id: int, data: PlanItemInput, snapshot: ItemSnapshot, by: int | None
    ) -> int:
        item_id = int(
            self._c.execute(
                insert(plan_item)
                .values(plan_year_id=year_id, created_by_user_id=by, **self._values(data, snapshot))
                .returning(plan_item.c.id)
            ).scalar_one()
        )
        self._write_demands(item_id, data)
        return item_id

    def update_item(self, item_id: int, data: PlanItemInput, snapshot: ItemSnapshot) -> None:
        self._c.execute(
            update(plan_item)
            .where(plan_item.c.id == item_id)
            .values(updated_at=func.now(), **self._values(data, snapshot))
        )
        self._write_demands(item_id, data)

    def delete_item(self, item_id: int) -> None:
        self._c.execute(delete(plan_item).where(plan_item.c.id == item_id))

    def create_imported_items(
        self, year_id: int, import_id: int, rows: Sequence[ImportedPlanItem], by: int | None
    ) -> int:
        if not rows:
            return 0
        values = [
            {
                "plan_year_id": year_id,
                "plan_budget_id": r.plan_budget_id,
                "plan_type": r.plan_type.value,
                "item_source_id": r.item_id,
                "item_code_snapshot": r.snapshot.code,
                "item_name_snapshot": r.snapshot.name,
                "unit_snapshot": r.snapshot.unit,
                "owner_department_source_id": r.owner_department_id,
                "purchasing_department_source_id": r.purchasing_department_id,
                "planned_qty": r.planned_qty,
                "estimated_unit_price": r.estimated_unit_price,
                "planned_amount": r.planned_amount,
                "q1": r.quarters[0],
                "q2": r.quarters[1],
                "q3": r.quarters[2],
                "q4": r.quarters[3],
                "note": r.note,
                "created_by_user_id": by,
                "plan_import_id": import_id,
                "source_sheet": r.source_sheet,
                "source_row": r.source_row,
            }
            for r in rows
        ]
        self._c.execute(insert(plan_item), values)  # executemany: no parameter limit
        return len(values)

    def delete_imported_items(self, import_id: int) -> int:
        return int(
            self._c.execute(
                delete(plan_item).where(plan_item.c.plan_import_id == import_id)
            ).rowcount
        )

    def amend_item(self, item_id: int, change: AmendedItem) -> None:
        self._c.execute(
            update(plan_item)
            .where(plan_item.c.id == item_id)
            .values(
                updated_at=func.now(),
                plan_budget_id=change.plan_budget_id,
                item_source_id=change.item_id,
                item_code_snapshot=change.snapshot.code,
                item_name_snapshot=change.snapshot.name,
                unit_snapshot=change.snapshot.unit,
                owner_department_source_id=change.owner_department_id,
                purchasing_department_source_id=change.purchasing_department_id,
                planned_qty=change.planned_qty,
                estimated_unit_price=change.estimated_unit_price,
                planned_amount=change.planned_amount,
            )
        )


class SqlPlanImportRepository:
    """Wave 12A-1 (D-42): one row per imported file; a removal marks it, never deletes it."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def create(
        self,
        year_id: int,
        file_name: str,
        file_sha256: str,
        rows_imported: int,
        rows_skipped: int,
        total_amount: Decimal,
        by: int,
    ) -> int:
        return int(
            self._c.execute(
                insert(plan_import)
                .values(
                    plan_year_id=year_id,
                    file_name=file_name,
                    file_sha256=file_sha256,
                    rows_imported=rows_imported,
                    rows_skipped=rows_skipped,
                    total_amount=total_amount,
                    imported_by_user_id=by,
                )
                .returning(plan_import.c.id)
            ).scalar_one()
        )

    def _query(self) -> Select[Any]:
        return select(plan_import, plan_year.c.fiscal_year).join(
            plan_year, plan_year.c.id == plan_import.c.plan_year_id
        )

    @staticmethod
    def _record(r: Any) -> PlanImportRecord:
        return PlanImportRecord(
            id=r.id,
            fiscal_year=r.fiscal_year,
            file_name=r.file_name,
            file_sha256=r.file_sha256,
            rows_imported=r.rows_imported,
            rows_skipped=r.rows_skipped,
            total_amount=Decimal(r.total_amount),
            state=r.state,
            imported_by_user_id=r.imported_by_user_id,
            imported_at=r.imported_at,
            removed_by_user_id=r.removed_by_user_id,
            removed_at=r.removed_at,
            removed_reason=r.removed_reason,
        )

    def get(self, import_id: int) -> PlanImportRecord | None:
        row = self._c.execute(self._query().where(plan_import.c.id == import_id)).one_or_none()
        return self._record(row) if row else None

    def list(self, fiscal_year: int) -> list[PlanImportRecord]:
        rows = self._c.execute(
            self._query().where(plan_year.c.fiscal_year == fiscal_year).order_by(plan_import.c.id)
        ).all()
        return [self._record(r) for r in rows]

    def live_with_hash(self, fiscal_year: int, file_sha256: str) -> PlanImportRecord | None:
        row = self._c.execute(
            self._query().where(
                plan_year.c.fiscal_year == fiscal_year,
                plan_import.c.file_sha256 == file_sha256,
                plan_import.c.state == "IMPORTED",
            )
        ).one_or_none()
        return self._record(row) if row else None

    def mark_removed(self, import_id: int, by: int, reason: str) -> bool:
        return (
            self._c.execute(
                update(plan_import)
                .where(plan_import.c.id == import_id, plan_import.c.state == "IMPORTED")
                .values(
                    state="REMOVED",
                    removed_by_user_id=by,
                    removed_at=func.now(),
                    removed_reason=reason,
                )
            ).rowcount
            == 1
        )


class SqlLedgerRepository:
    """Insert-only access to plan_ledger (the database also forbids UPDATE/DELETE)."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def append(
        self,
        entry: LedgerEntry,
        actor_user_id: int | None,
        plan_amendment_id: int | None = None,
    ) -> None:
        # D-40: in production, MOCK / DEMO data never reaches the Plan Ledger.
        if is_production(self._c) and is_mock_item(self._c, int(entry.plan_item_id)):
            raise ConflictError(
                "MOCK_DATA_IN_PRODUCTION",
                "this Plan Item is MOCK (made-up) data: production never posts it to the "
                "Plan Ledger (D-40)",
            )
        self._c.execute(
            insert(plan_ledger).values(
                plan_item_id=int(entry.plan_item_id),
                event_type=entry.event_type.value,
                qty_delta=entry.qty_delta,
                amount_delta=entry.amount_delta,
                idempotency_key=entry.idempotency_key,
                ppr_ref=entry.ppr_ref,
                actor_user_id=actor_user_id,
                plan_amendment_id=plan_amendment_id,
            )
        )

    def plan_items_of_ppr(self, ppr_ref: str) -> list[int]:
        rows = self._c.execute(
            select(plan_ledger.c.plan_item_id)
            .where(plan_ledger.c.ppr_ref == ppr_ref)
            .distinct()
            .order_by(plan_ledger.c.plan_item_id)
        )
        return [int(r[0]) for r in rows]

    def balances(self, plan_item_ids: Sequence[int]) -> dict[int, LedgerBalance]:
        ids = sorted({int(i) for i in plan_item_ids})
        if not ids:
            return {}
        rows = self._c.execute(
            select(
                plan_ledger.c.plan_item_id,
                plan_ledger.c.event_type,
                func.sum(plan_ledger.c.qty_delta),
                func.sum(plan_ledger.c.amount_delta),
            )
            .where(plan_ledger.c.plan_item_id.in_(ids))
            .group_by(plan_ledger.c.plan_item_id, plan_ledger.c.event_type)
        )
        totals: dict[int, dict[LedgerEventType, tuple[Decimal, Decimal]]] = {}
        for item_id, event, qty, amount in rows:
            totals.setdefault(int(item_id), {})[LedgerEventType(event)] = (
                Decimal(qty),
                Decimal(amount),
            )
        return {item_id: balance_from_totals(t) for item_id, t in totals.items()}

    def entries(self, plan_item_id: int) -> tuple[LedgerEntry, ...]:
        # Explicit columns: this read also runs on databases migrated only part-way (tests of
        # earlier migrations), which lack later columns such as plan_amendment_id.
        rows = self._c.execute(
            select(
                plan_ledger.c.plan_item_id,
                plan_ledger.c.event_type,
                plan_ledger.c.qty_delta,
                plan_ledger.c.amount_delta,
                plan_ledger.c.idempotency_key,
                plan_ledger.c.ppr_ref,
            )
            .where(plan_ledger.c.plan_item_id == plan_item_id)
            .order_by(plan_ledger.c.id)
        )
        return tuple(
            LedgerEntry(
                str(r.plan_item_id),
                LedgerEventType(r.event_type),
                Decimal(r.qty_delta),
                Decimal(r.amount_delta),
                r.idempotency_key,
                r.ppr_ref,
            )
            for r in rows
        )
