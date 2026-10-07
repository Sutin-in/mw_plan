"""SQL implementation of the PPR repository (Wave 4) with search and verification (Wave 5B)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, String, cast, delete, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from ppr.domain.ppr_state import PprState
from ppr.domain.pr_binding import PrBinding
from ppr.infrastructure.db.schema import (
    app_user,
    plan_item,
    plan_year,
    ppr,
    ppr_binding,
    ppr_category_allocation,
    ppr_item,
    ppr_line_choice,
    ppr_sequence,
    ppr_verification,
    ppr_version,
)
from ppr.ports import (
    PprAllocationRecord,
    PprHeaderInput,
    PprItemRecord,
    PprRecord,
    PprScope,
    PprSearch,
    PprVersionRecord,
    UniquenessError,
    VerificationRecord,
)


def _items_query() -> Any:
    """PPR lines with the plan row each uses (D-43: shown with the line)."""
    return select(
        ppr_item,
        plan_item.c.item_name_snapshot.label("plan_item_name"),
        plan_item.c.unit_snapshot.label("plan_item_unit"),
        plan_item.c.item_code_snapshot.label("plan_item_code"),
    ).join(plan_item, plan_item.c.id == ppr_item.c.plan_item_id)


class SqlPprRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    # ------------------------------------------------------------------ reads
    def binding_state(self, pr_no: str) -> PrBinding:
        if self._c.execute(select(ppr_binding.c.pr_no).where(ppr_binding.c.pr_no == pr_no)).first():
            return PrBinding.BOUND
        draft = self._c.execute(
            select(ppr.c.id).where(
                ppr.c.pr_no == pr_no,
                ppr.c.state == PprState.DRAFT.value,
                ppr.c.discarded_at.is_(None),
            )
        ).first()
        return PrBinding.DRAFT_IN_PROGRESS if draft else PrBinding.UNBOUND

    def get(self, ppr_id: int, *, for_update: bool = False) -> PprRecord | None:
        q = select(ppr).where(ppr.c.id == ppr_id)
        if for_update:
            q = q.with_for_update()
        row = self._c.execute(q).first()
        return self._record(row) if row else None

    def search(self, scope: PprScope | None, limit: int) -> list[PprRecord]:
        q = select(ppr).where(ppr.c.discarded_at.is_(None)).order_by(ppr.c.id.desc())
        if scope is not None:
            q = q.where(ppr.c.created_by_user_id == scope.created_by_user_id)
        return self._records(list(self._c.execute(q.limit(limit))))

    def find(self, search: PprSearch, scope: PprScope | None) -> list[PprRecord]:
        q = self._filtered(select(ppr), search, scope)
        if search.oldest_first:
            q = q.order_by(ppr.c.updated_at.asc(), ppr.c.id.asc())
        else:
            q = q.order_by(ppr.c.updated_at.desc(), ppr.c.id.desc())
        rows = list(self._c.execute(q.limit(search.limit)))
        return self._records(rows)

    def _records(self, rows: list[Any]) -> list[PprRecord]:
        """Many PPRs with their items and allocations in three queries (lists, reports)."""
        ids = [r.id for r in rows]
        items: dict[int, list[Any]] = {i: [] for i in ids}
        allocs: dict[int, list[Any]] = {i: [] for i in ids}
        if ids:
            for i in self._c.execute(
                _items_query().where(ppr_item.c.ppr_id.in_(ids)).order_by(ppr_item.c.id)
            ):
                items[i.ppr_id].append(i)
            for a in self._c.execute(
                select(ppr_category_allocation)
                .where(ppr_category_allocation.c.ppr_id.in_(ids))
                .order_by(ppr_category_allocation.c.id)
            ):
                allocs[a.ppr_id].append(a)
        return [self._record(r, items[r.id], allocs[r.id]) for r in rows]

    def count(self, search: PprSearch, scope: PprScope | None) -> int:
        q = self._filtered(select(func.count()).select_from(ppr), search, scope)
        return int(self._c.execute(q).scalar_one())

    def count_by_state(self, fiscal_year: int, scope: PprScope | None) -> dict[str, int]:
        q = self._filtered(
            select(ppr.c.state, func.count()).select_from(ppr),
            PprSearch(fiscal_year=fiscal_year),
            scope,
        ).group_by(ppr.c.state)
        return {str(state): int(n) for state, n in self._c.execute(q)}

    def first_confirmations(self, ppr_ids: Sequence[int]) -> dict[int, datetime]:
        ids = sorted({int(i) for i in ppr_ids})
        if not ids:
            return {}
        rows = self._c.execute(
            select(ppr_version.c.ppr_id, ppr_version.c.confirmed_at).where(
                ppr_version.c.ppr_id.in_(ids), ppr_version.c.version == 1
            )
        )
        return {int(i): at for i, at in rows}

    def last_verifications(self, ppr_ids: Sequence[int]) -> dict[int, datetime]:
        ids = sorted({int(i) for i in ppr_ids})
        if not ids:
            return {}
        rows = self._c.execute(
            select(ppr_verification.c.ppr_id, func.max(ppr_verification.c.verified_at))
            .where(ppr_verification.c.ppr_id.in_(ids))
            .group_by(ppr_verification.c.ppr_id)
        )
        return {int(i): at for i, at in rows}

    def _filtered(self, q: Any, search: PprSearch, scope: PprScope | None) -> Any:
        s = search
        q = q.where(ppr.c.discarded_at.is_(None))
        if scope is not None:  # D-41: a requester reads the PPRs they created
            q = q.where(ppr.c.created_by_user_id == scope.created_by_user_id)

        def like(col: Any, text: str | None) -> None:
            nonlocal q
            if text and text.strip():
                q = q.where(col.ilike(f"%{_escape(text.strip())}%", escape="\\"))

        like(ppr.c.pr_no, s.pr_no)
        like(ppr.c.ppr_number, s.ppr_number)
        if s.department_id:
            q = q.where(ppr.c.department_source_id == s.department_id)
        if s.fund_source_id:
            q = q.where(ppr.c.fund_source_source_id == s.fund_source_id)
        if s.fiscal_year is not None:
            q = q.where(ppr.c.fiscal_year == s.fiscal_year)
        if s.states:
            q = q.where(ppr.c.state.in_([st.value for st in s.states]))
        if s.budget_category_id:
            q = q.where(
                or_(
                    ppr.c.pr_budget_category_source_id == s.budget_category_id,
                    exists().where(
                        ppr_category_allocation.c.ppr_id == ppr.c.id,
                        ppr_category_allocation.c.budget_category_source_id == s.budget_category_id,
                    ),
                )
            )
        if s.item and s.item.strip():
            pattern = f"%{_escape(s.item.strip())}%"
            q = q.where(
                exists().where(
                    ppr_item.c.ppr_id == ppr.c.id,
                    or_(
                        ppr_item.c.item_source_id.ilike(pattern, escape="\\"),
                        ppr_item.c.item_name.ilike(pattern, escape="\\"),
                    ),
                )
            )
        if s.requester and s.requester.strip():
            pattern = f"%{_escape(s.requester.strip())}%"
            pr_requester = cast(ppr.c.draft_pr["header"]["requester_name"].astext, String)
            q = q.where(
                or_(
                    pr_requester.ilike(pattern, escape="\\"),
                    exists().where(
                        app_user.c.id == ppr.c.created_by_user_id,
                        or_(
                            app_user.c.display_name.ilike(pattern, escape="\\"),
                            app_user.c.username.ilike(pattern, escape="\\"),
                        ),
                    ),
                )
            )
        if s.confirmed_only or s.confirmed_from is not None or s.confirmed_to is not None:
            q = q.where(ppr.c.ppr_number.is_not(None))
        if s.confirmed_from is not None or s.confirmed_to is not None:
            first = select(ppr_version.c.confirmed_at).where(
                ppr_version.c.ppr_id == ppr.c.id, ppr_version.c.version == 1
            )
            if s.confirmed_from is not None:
                q = q.where(exists(first.where(ppr_version.c.confirmed_at >= s.confirmed_from)))
            if s.confirmed_to is not None:
                q = q.where(exists(first.where(ppr_version.c.confirmed_at < s.confirmed_to)))
        if s.created_from is not None:
            q = q.where(ppr.c.created_at >= s.created_from)
        if s.created_to is not None:
            q = q.where(ppr.c.created_at < s.created_to)
        if s.inactive_before is not None:
            q = q.where(ppr.c.updated_at < s.inactive_before)
        return q

    def add_verification(
        self, ppr_id: int, version: int, appointment_id: int, verified_by: int, verified_on: date
    ) -> None:
        try:
            with self._c.begin_nested():
                self._c.execute(
                    insert(ppr_verification).values(
                        ppr_id=ppr_id,
                        version=version,
                        appointment_id=appointment_id,
                        verified_by_user_id=verified_by,
                        verified_on=verified_on,
                    )
                )
        except IntegrityError as exc:
            raise UniquenessError("this PPR version is already verified") from exc

    def verifications(self, ppr_id: int) -> list[VerificationRecord]:
        rows = self._c.execute(
            select(ppr_verification)
            .where(ppr_verification.c.ppr_id == ppr_id)
            .order_by(ppr_verification.c.version)
        )
        return [
            VerificationRecord(
                r.ppr_id, r.version, r.appointment_id, r.verified_by_user_id, r.verified_at
            )
            for r in rows
        ]

    def _record(
        self, r: Any, item_rows: list[Any] | None = None, alloc_rows: list[Any] | None = None
    ) -> PprRecord:
        if item_rows is None:
            item_rows = list(
                self._c.execute(
                    _items_query().where(ppr_item.c.ppr_id == r.id).order_by(ppr_item.c.id)
                )
            )
        if alloc_rows is None:
            alloc_rows = list(
                self._c.execute(
                    select(ppr_category_allocation)
                    .where(ppr_category_allocation.c.ppr_id == r.id)
                    .order_by(ppr_category_allocation.c.id)
                )
            )
        items = tuple(
            PprItemRecord(
                i.pr_item_id,
                i.item_source_id,
                i.item_name,
                i.unit,
                Decimal(i.qty),
                Decimal(i.unit_price),
                Decimal(i.amount),
                i.plan_item_id,
                i.plan_item_name,
                i.plan_item_unit,
                i.plan_item_code,
            )
            for i in item_rows
        )
        allocs = tuple(
            PprAllocationRecord(a.budget_category_source_id, Decimal(a.amount)) for a in alloc_rows
        )
        return PprRecord(
            id=r.id,
            pr_no=r.pr_no,
            fiscal_year=r.fiscal_year,
            department_id=r.department_source_id,
            fund_source_id=r.fund_source_source_id,
            pr_budget_category_id=r.pr_budget_category_source_id,
            state=PprState(r.state),
            ppr_number=r.ppr_number,
            current_version=r.current_version,
            required_amount=Decimal(r.required_amount),
            adjustment_reference=r.adjustment_reference,
            hosxp_pr_status=r.hosxp_pr_status,
            hosxp_native_status=r.hosxp_native_status,
            draft_pr=dict(r.draft_pr),
            created_by_user_id=r.created_by_user_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
            discarded_at=r.discarded_at,
            items=items,
            allocations=allocs,
        )

    def versions(self, ppr_id: int) -> list[PprVersionRecord]:
        rows = self._c.execute(
            select(ppr_version)
            .where(ppr_version.c.ppr_id == ppr_id)
            .order_by(ppr_version.c.version)
        )
        return [
            PprVersionRecord(
                r.ppr_id,
                r.version,
                dict(r.snapshot),
                r.document_code,
                r.confirmed_by_user_id,
                r.confirmed_at,
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ writes
    @staticmethod
    def _header_values(h: PprHeaderInput) -> dict[str, Any]:
        return {
            "pr_no": h.pr_no,
            "fiscal_year": h.fiscal_year,
            "department_source_id": h.department_id,
            "fund_source_source_id": h.fund_source_id,
            "pr_budget_category_source_id": h.pr_budget_category_id,
            "required_amount": h.required_amount,
            "hosxp_pr_status": h.hosxp_pr_status,
            "hosxp_native_status": h.hosxp_native_status,
        }

    def create_draft(
        self, header: PprHeaderInput, draft_pr: dict[str, Any], created_by: int
    ) -> int:
        try:
            with self._c.begin_nested():
                return int(
                    self._c.execute(
                        insert(ppr)
                        .values(
                            **self._header_values(header),
                            draft_pr=draft_pr,
                            created_by_user_id=created_by,
                        )
                        .returning(ppr.c.id)
                    ).scalar_one()
                )
        except IntegrityError as exc:
            raise UniquenessError("a working draft already exists for this PR") from exc

    def _touch(self, ppr_id: int, **values: Any) -> None:
        self._c.execute(
            update(ppr).where(ppr.c.id == ppr_id).values(**values, updated_at=func.now())
        )

    def update_header(self, ppr_id: int, header: PprHeaderInput) -> None:
        self._touch(ppr_id, **self._header_values(header))

    def replace_items(self, ppr_id: int, items: Sequence[PprItemRecord]) -> None:
        self._c.execute(delete(ppr_item).where(ppr_item.c.ppr_id == ppr_id))
        if items:
            self._c.execute(
                insert(ppr_item),
                [
                    {
                        "ppr_id": ppr_id,
                        "pr_item_id": i.pr_item_id,
                        "item_source_id": i.item_id,
                        "item_name": i.item_name,
                        "unit": i.unit,
                        "qty": i.qty,
                        "unit_price": i.unit_price,
                        "amount": i.amount,
                        "plan_item_id": i.plan_item_id,
                    }
                    for i in items
                ],
            )

    def choices(self, ppr_id: int) -> dict[str, int]:
        rows = self._c.execute(
            select(ppr_line_choice.c.pr_item_id, ppr_line_choice.c.plan_item_id).where(
                ppr_line_choice.c.ppr_id == ppr_id
            )
        ).all()
        return {r.pr_item_id: int(r.plan_item_id) for r in rows}

    def chosen_labels(self, ppr_id: int) -> dict[str, tuple[int, str, str]]:
        rows = self._c.execute(
            select(
                ppr_line_choice.c.pr_item_id,
                ppr_line_choice.c.plan_item_id,
                ppr_line_choice.c.plan_item_name,
                ppr_line_choice.c.plan_item_unit,
            ).where(ppr_line_choice.c.ppr_id == ppr_id)
        ).all()
        return {
            r.pr_item_id: (int(r.plan_item_id), r.plan_item_name, r.plan_item_unit) for r in rows
        }

    def set_choices(
        self, ppr_id: int, choices: Mapping[str, tuple[int, str, str]], by: int
    ) -> None:
        self._c.execute(delete(ppr_line_choice).where(ppr_line_choice.c.ppr_id == ppr_id))
        if choices:
            self._c.execute(
                insert(ppr_line_choice),
                [
                    {
                        "ppr_id": ppr_id,
                        "pr_item_id": k,
                        "plan_item_id": pid,
                        "plan_item_name": name,
                        "plan_item_unit": unit,
                        "chosen_by_user_id": by,
                    }
                    for k, (pid, name, unit) in sorted(choices.items())
                ],
            )

    def replace_allocations(
        self,
        ppr_id: int,
        allocations: Sequence[PprAllocationRecord],
        adjustment_reference: str | None,
    ) -> None:
        self._c.execute(
            delete(ppr_category_allocation).where(ppr_category_allocation.c.ppr_id == ppr_id)
        )
        if allocations:
            self._c.execute(
                insert(ppr_category_allocation),
                [
                    {
                        "ppr_id": ppr_id,
                        "budget_category_source_id": a.budget_category_id,
                        "amount": a.amount,
                    }
                    for a in allocations
                ],
            )
        self._touch(ppr_id, adjustment_reference=adjustment_reference)

    def discard(self, ppr_id: int) -> None:
        self._touch(ppr_id, discarded_at=func.now())

    def set_state(self, ppr_id: int, state: PprState) -> None:
        self._touch(ppr_id, state=state.value)

    def set_hosxp_status(self, ppr_id: int, status: str, native_status: str | None) -> None:
        # Display fields only (D-02): not a business change, so updated_at is kept.
        self._c.execute(
            update(ppr)
            .where(ppr.c.id == ppr_id)
            .values(hosxp_pr_status=status, hosxp_native_status=native_status)
        )

    def mark_confirmed(self, ppr_id: int, ppr_number: str, version: int) -> None:
        self._touch(
            ppr_id,
            state=PprState.CONFIRMED_LOCKED.value,
            ppr_number=ppr_number,
            current_version=version,
        )

    def next_sequence(self, fiscal_year: int) -> int:
        stmt = (
            pg_insert(ppr_sequence)
            .values(fiscal_year=fiscal_year, last_value=1)
            .on_conflict_do_update(
                index_elements=[ppr_sequence.c.fiscal_year],
                set_={"last_value": ppr_sequence.c.last_value + 1},
            )
            .returning(ppr_sequence.c.last_value)
        )
        return int(self._c.execute(stmt).scalar_one())

    def bind(self, pr_no: str, ppr_id: int) -> None:
        try:
            with self._c.begin_nested():
                self._c.execute(insert(ppr_binding).values(pr_no=pr_no, ppr_id=ppr_id))
        except IntegrityError as exc:
            raise UniquenessError("the PR is already bound to a confirmed PPR") from exc

    def add_version(
        self,
        ppr_id: int,
        version: int,
        snapshot: dict[str, Any],
        document_code: str,
        confirmed_by: int,
    ) -> None:
        self._c.execute(
            insert(ppr_version).values(
                ppr_id=ppr_id,
                version=version,
                snapshot=snapshot,
                document_code=document_code,
                confirmed_by_user_id=confirmed_by,
            )
        )

    def lock_plan_year_shared(self, fiscal_year: int) -> None:
        self._c.execute(
            select(plan_year.c.id)
            .where(plan_year.c.fiscal_year == fiscal_year)
            .with_for_update(read=True)
        ).all()

    def lock_plan_items(self, plan_item_ids: Sequence[int]) -> None:
        ids = sorted(set(plan_item_ids))
        if ids:
            self._c.execute(
                select(plan_item.c.id)
                .where(plan_item.c.id.in_(ids))
                .order_by(plan_item.c.id)
                .with_for_update()
            ).all()


def _escape(text: str) -> str:
    """Make LIKE wildcards in user input literal."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
