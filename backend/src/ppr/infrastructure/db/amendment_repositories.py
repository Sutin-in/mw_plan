"""SQL implementation of the Wave 7A Plan Amendment port (append-only history, D-37)."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, func, insert, null, select, true

from ppr.infrastructure.db.schema import plan_year
from ppr.infrastructure.db.schema_amendment import plan_amendment, plan_amendment_change
from ppr.ports import AmendmentChangeRecord, AmendmentRecord


class SqlAmendmentRepository:
    """Insert-only access to the amendment history (the database forbids UPDATE/DELETE)."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def next_number(self, year_id: int) -> int:
        current: int | None = self._c.execute(
            select(func.max(plan_amendment.c.amendment_no)).where(
                plan_amendment.c.plan_year_id == year_id
            )
        ).scalar_one()
        return int(current or 0) + 1

    def create(
        self,
        year_id: int,
        amendment_no: int,
        approval_document_no: str,
        approval_date: date,
        reason: str,
        by: int,
    ) -> int:
        return int(
            self._c.execute(
                insert(plan_amendment)
                .values(
                    plan_year_id=year_id,
                    amendment_no=amendment_no,
                    approval_document_no=approval_document_no,
                    approval_date=approval_date,
                    reason=reason,
                    created_by_user_id=by,
                )
                .returning(plan_amendment.c.id)
            ).scalar_one()
        )

    def add_change(self, amendment_id: int, change: AmendmentChangeRecord) -> None:
        self._c.execute(
            insert(plan_amendment_change).values(
                plan_amendment_id=amendment_id,
                target=change.target,
                target_id=change.target_id,
                change_kind=change.change_kind,
                before=change.before if change.before is not None else null(),
                after=change.after,
            )
        )

    def _records(self, where: Any) -> list[AmendmentRecord]:
        heads = self._c.execute(
            select(plan_amendment, plan_year.c.fiscal_year)
            .join(plan_year, plan_year.c.id == plan_amendment.c.plan_year_id)
            .where(where)
            .order_by(plan_amendment.c.created_at.desc(), plan_amendment.c.id.desc())
        ).all()
        ids = [int(h.id) for h in heads]
        changes: dict[int, list[AmendmentChangeRecord]] = {i: [] for i in ids}
        if ids:
            for r in self._c.execute(
                select(plan_amendment_change)
                .where(plan_amendment_change.c.plan_amendment_id.in_(ids))
                .order_by(plan_amendment_change.c.id)
            ):
                changes[int(r.plan_amendment_id)].append(
                    AmendmentChangeRecord(
                        r.target, int(r.target_id), r.change_kind, r.before, r.after
                    )
                )
        return [
            AmendmentRecord(
                id=int(h.id),
                fiscal_year=int(h.fiscal_year),
                amendment_no=int(h.amendment_no),
                approval_document_no=h.approval_document_no,
                approval_date=h.approval_date,
                reason=h.reason,
                created_by_user_id=int(h.created_by_user_id),
                created_at=h.created_at,
                changes=tuple(changes[int(h.id)]),
            )
            for h in heads
        ]

    def get(self, amendment_id: int) -> AmendmentRecord | None:
        rows = self._records(plan_amendment.c.id == amendment_id)
        return rows[0] if rows else None

    def list(self, fiscal_year: int | None = None) -> list[AmendmentRecord]:
        where = plan_year.c.fiscal_year == fiscal_year if fiscal_year is not None else true()
        return self._records(where)
