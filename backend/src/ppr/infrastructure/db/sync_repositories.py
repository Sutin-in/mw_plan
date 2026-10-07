"""SQL implementation of the PR-synchronization observation repository (Wave 6, D-29)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import Connection, func, insert, or_, select
from sqlalchemy.exc import IntegrityError

from ppr.domain.ppr_state import PprState
from ppr.infrastructure.db.schema import ppr, ppr_sync_observation, sync_run
from ppr.ports import ObservationInput, ObservationRecord, PprScope, SyncTarget, UniquenessError

_o = ppr_sync_observation


class SqlSyncObservationRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def add(self, obs: ObservationInput) -> int:
        values = {
            "sync_run_id": obs.sync_run_id,
            "ppr_id": obs.ppr_id,
            "pr_no": obs.pr_no,
            "compared_version": obs.compared_version,
            "observed_at": obs.observed_at,
            "outcome": obs.outcome,
            "evidence_class": obs.evidence_class,
            "hosxp_status": obs.hosxp_status,
            "native_status": obs.native_status,
            "changes": obs.changes,
            "evidence_issues": obs.evidence_issues,
            "live_pr": obs.live_pr,
            "details": obs.details,
            "state_before": obs.state_before.value,
            "state_after": obs.state_after.value,
            "released": obs.released,
            "anomaly_code": obs.anomaly_code,
            "error_code": obs.error_code,
            "error_message": obs.error_message,
        }
        try:
            with self._c.begin_nested():
                return int(
                    self._c.execute(insert(_o).values(**values).returning(_o.c.id)).scalar_one()
                )
        except IntegrityError as exc:
            diag = getattr(getattr(exc, "orig", None), "diag", None)
            if getattr(diag, "constraint_name", None) == "uq_ppr_sync_observation_released":
                raise UniquenessError("this PPR's plan usage was already released") from exc
            raise

    def _select(self) -> Any:
        return select(_o, ppr.c.ppr_number, ppr.c.department_source_id).join(
            ppr, ppr.c.id == _o.c.ppr_id
        )

    @staticmethod
    def _record(r: Any) -> ObservationRecord:
        return ObservationRecord(
            id=r.id,
            sync_run_id=r.sync_run_id,
            ppr_id=r.ppr_id,
            pr_no=r.pr_no,
            compared_version=r.compared_version,
            observed_at=r.observed_at,
            recorded_at=r.recorded_at,
            outcome=r.outcome,
            evidence_class=r.evidence_class,
            hosxp_status=r.hosxp_status,
            native_status=r.native_status,
            changes=r.changes,
            evidence_issues=r.evidence_issues,
            live_pr=r.live_pr,
            details=r.details,
            state_before=r.state_before,
            state_after=r.state_after,
            released=r.released,
            anomaly_code=r.anomaly_code,
            error_code=r.error_code,
            error_message=r.error_message,
            ppr_number=r.ppr_number,
            department_id=r.department_source_id,
        )

    def for_ppr(self, ppr_id: int, limit: int = 200) -> list[ObservationRecord]:
        q = self._select().where(_o.c.ppr_id == ppr_id).order_by(_o.c.id.desc()).limit(limit)
        return [self._record(r) for r in self._c.execute(q)]

    def for_run(self, run_id: int, scope: PprScope | None) -> list[ObservationRecord]:
        q = self._select().where(_o.c.sync_run_id == run_id)
        if scope is not None:  # D-41
            q = q.where(ppr.c.created_by_user_id == scope.created_by_user_id)
        return [self._record(r) for r in self._c.execute(q.order_by(_o.c.id))]

    def latest_after(
        self, run_id: int, ppr_ids: Sequence[int], scopes: Sequence[str]
    ) -> dict[int, tuple[str, str | None]]:
        ids = sorted({int(i) for i in ppr_ids})
        if not ids:
            return {}
        latest = (
            select(func.max(_o.c.id))
            .select_from(_o.join(sync_run, sync_run.c.id == _o.c.sync_run_id))
            .where(
                _o.c.ppr_id.in_(ids),
                sync_run.c.id > run_id,
                sync_run.c.scope.in_(list(scopes)),
                sync_run.c.status != "RUNNING",
            )
            .group_by(_o.c.ppr_id)
        )
        rows = self._c.execute(
            select(_o.c.ppr_id, _o.c.outcome, _o.c.evidence_class).where(_o.c.id.in_(latest))
        )
        return {int(i): (str(o), c) for i, o, c in rows}

    def governance(self, outcomes: Sequence[str], limit: int) -> list[ObservationRecord]:
        # The latest observation of each PPR; listed only while it still needs attention,
        # so a PR that stays NOT_FOUND night after night is one row, and a resolved one
        # (a later normal observation) leaves the list.
        latest = select(func.max(_o.c.id)).group_by(_o.c.ppr_id)
        q = (
            self._select()
            .where(
                _o.c.id.in_(latest),
                or_(_o.c.outcome.in_(list(outcomes)), _o.c.anomaly_code.is_not(None)),
            )
            .order_by(_o.c.id.desc())
            .limit(limit)
        )
        return [self._record(r) for r in self._c.execute(q)]

    def released_for(self, ppr_id: int) -> bool:
        q = select(_o.c.id).where(_o.c.ppr_id == ppr_id, _o.c.released.is_(True))
        return self._c.execute(q).first() is not None

    def sync_targets(self, states: Sequence[PprState]) -> list[SyncTarget]:
        q = (
            select(ppr.c.id, ppr.c.pr_no, ppr.c.state, ppr.c.current_version)
            .where(
                ppr.c.state.in_([s.value for s in states]),
                ppr.c.discarded_at.is_(None),
                ppr.c.current_version > 0,
            )
            .order_by(ppr.c.id)
        )
        return [
            SyncTarget(r.id, r.pr_no, PprState(r.state), r.current_version)
            for r in self._c.execute(q)
        ]

    def retry_targets(
        self, run_id: int, outcomes: Sequence[str], invalid_classes: Sequence[str]
    ) -> list[int]:
        q = (
            select(_o.c.ppr_id)
            .where(
                _o.c.sync_run_id == run_id,
                or_(
                    _o.c.outcome.in_(list(outcomes)),
                    (_o.c.outcome == "INVALID_EVIDENCE")
                    & _o.c.evidence_class.in_(list(invalid_classes)),
                ),
            )
            .distinct()
            .order_by(_o.c.ppr_id)
        )
        return [int(r[0]) for r in self._c.execute(q)]
