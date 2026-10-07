"""SQL implementation of the Wave 7B-2 Assigned Purchase port (D-38 A-2 ... A-6)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Connection, and_, delete, exists, insert, select, update

from ppr.domain.plan import DemandState
from ppr.domain.ppr_state import PprState
from ppr.infrastructure.db.schema import plan_item_demand, ppr, ppr_verification
from ppr.infrastructure.db.schema_assigned import ppr_coverage
from ppr.ports import ConfirmedCoverage


class SqlAssignedRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def coverage(self, ppr_id: int, *, confirmed: bool) -> dict[int, dict[str, Decimal]]:
        out: dict[int, dict[str, Decimal]] = {}
        rows = self._c.execute(
            select(ppr_coverage)
            .where(ppr_coverage.c.ppr_id == ppr_id, ppr_coverage.c.confirmed.is_(confirmed))
            .order_by(ppr_coverage.c.plan_item_id, ppr_coverage.c.department_source_id)
        )
        for r in rows:
            out.setdefault(int(r.plan_item_id), {})[r.department_source_id] = Decimal(r.qty)
        return out

    def replace_coverage(
        self, ppr_id: int, coverage: dict[int, dict[str, Decimal]], *, confirmed: bool
    ) -> None:
        self._c.execute(
            delete(ppr_coverage).where(
                ppr_coverage.c.ppr_id == ppr_id, ppr_coverage.c.confirmed.is_(confirmed)
            )
        )
        rows = [
            {
                "ppr_id": ppr_id,
                "plan_item_id": pid,
                "department_source_id": dept,
                "qty": qty,
                "confirmed": confirmed,
            }
            for pid, depts in sorted(coverage.items())
            for dept, qty in sorted(depts.items())
            if qty > 0
        ]
        if rows:
            self._c.execute(insert(ppr_coverage), rows)

    def confirmed_for_items(self, plan_item_ids: Sequence[int]) -> list[ConfirmedCoverage]:
        ids = sorted({int(i) for i in plan_item_ids})
        if not ids:
            return []
        verified = exists().where(
            and_(
                ppr_verification.c.ppr_id == ppr.c.id,
                ppr_verification.c.version == ppr.c.current_version,
            )
        )
        rows = self._c.execute(
            select(
                ppr_coverage.c.ppr_id,
                ppr_coverage.c.plan_item_id,
                ppr_coverage.c.department_source_id,
                ppr_coverage.c.qty,
                ppr.c.state,
                verified.label("verified"),
            )
            .join(ppr, ppr.c.id == ppr_coverage.c.ppr_id)
            .where(ppr_coverage.c.plan_item_id.in_(ids), ppr_coverage.c.confirmed.is_(True))
            .order_by(ppr_coverage.c.id)
        )
        return [
            ConfirmedCoverage(
                int(r.ppr_id),
                int(r.plan_item_id),
                r.department_source_id,
                Decimal(r.qty),
                PprState(r.state),
                bool(r.verified),
            )
            for r in rows
        ]

    def set_demand(self, plan_item_id: int, department_id: str, qty: Decimal) -> int:
        existing = self._c.execute(
            select(plan_item_demand.c.id, plan_item_demand.c.state).where(
                plan_item_demand.c.plan_item_id == plan_item_id,
                plan_item_demand.c.department_source_id == department_id,
            )
        ).one_or_none()
        # The caller recomputes the state (refresh_demands); until then it must agree with
        # the quantity (0 <=> CANCELLED), otherwise it is kept.
        if qty == 0:
            state = DemandState.CANCELLED
        elif existing is None or existing.state == DemandState.CANCELLED.value:
            state = DemandState.PLANNED
        else:
            state = DemandState(existing.state)
        if existing is None:
            return int(
                self._c.execute(
                    insert(plan_item_demand)
                    .values(
                        plan_item_id=plan_item_id,
                        department_source_id=department_id,
                        demand_qty=qty,
                        state=state.value,
                    )
                    .returning(plan_item_demand.c.id)
                ).scalar_one()
            )
        self._c.execute(
            update(plan_item_demand)
            .where(plan_item_demand.c.id == existing.id)
            .values(demand_qty=qty, state=state.value)
        )
        return int(existing.id)

    def set_demand_state(self, demand_id: int, state: DemandState) -> None:
        self._c.execute(
            update(plan_item_demand)
            .where(plan_item_demand.c.id == demand_id)
            .values(state=state.value)
        )

    def lock_demands(self, plan_item_ids: Sequence[int]) -> None:
        ids = sorted({int(i) for i in plan_item_ids})
        if ids:
            self._c.execute(
                select(plan_item_demand.c.id)
                .where(plan_item_demand.c.plan_item_id.in_(ids))
                .order_by(plan_item_demand.c.id)
                .with_for_update()
            )
