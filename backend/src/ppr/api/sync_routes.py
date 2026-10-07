"""Wave 6 routes: PR synchronization (D-29 ... D-31).

Permissions (server-side, spec §22.9):
* ``POST /api/pr-sync``                  SYNC_RUN (PLAN_OFFICER, ADMIN): all open PPRs, or a
                                         retry of a run (``{"retry_of": run_id}``)
* ``POST /api/pprs/{id}/sync``          SYNC_RUN: one open PPR (never a draft)
* ``POST /api/pprs/{id}/recheck``       SYNC_RUN: governance re-check of one CANCELLED
                                         PPR (observation only)
* ``GET  /api/pr-sync/governance``     SYNC_RUN: NOT_FOUND, invalid evidence, anomalies,
                                         errors
* ``GET  /api/pr-sync/runs``, ``/{id}``    SYNC_READ (also PROCUREMENT, HEAD_OF_PROCUREMENT);
                                         run detail lists only PPRs the caller may read
* ``GET  /api/pprs/{id}/observations``  PPR_READ with the same department scope as the PPR

A synchronization request runs to completion before it answers; HOSxP is read outside
every database lock.
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field

from ppr.api.ppr_routes import _caller, _http
from ppr.application import ppr_services as svc
from ppr.application import sync_services as sync
from ppr.application.errors import ServiceError
from ppr.domain.permissions import Permission
from ppr.domain.pr_sync import SyncScope
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import ObservationRecord, SyncRunRecord, UserRecord

_log = logging.getLogger(__name__)


def _execute_quietly(factory: Any, gateway: Any, started: sync.StartedRun) -> None:
    """Background run: the run row records the outcome; nothing to answer to."""
    try:
        sync.execute_sync(factory, gateway, started)
    except Exception:  # the run is already marked FAILED by execute_sync
        _log.exception("background PR sync run %s failed", started.run_id)


class SyncAllIn(BaseModel):
    retry_of: int | None = Field(default=None, ge=1)
    # true: answer at once with the RUNNING run; the synchronization continues on the
    # server (a full run can take longer than a web request may wait).
    background: bool = False


class RunOut(BaseModel):
    id: int
    mode: str
    scope: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    requested_by_user_id: int | None
    ppr_id: int | None
    retry_of_sync_run_id: int | None
    checked: int
    changed: int
    cancelled: int
    non_critical: int
    not_attempted: int
    error_details: dict[str, Any] | None


class ObservationOut(BaseModel):
    id: int
    sync_run_id: int
    ppr_id: int
    ppr_number: str | None
    pr_no: str
    compared_version: int | None
    observed_at: datetime | None
    recorded_at: datetime
    outcome: str
    evidence_class: str | None
    hosxp_status: str | None
    native_status: str | None
    changes: list[dict[str, Any]] | None
    evidence_issues: list[dict[str, Any]] | None
    live_pr: dict[str, Any] | None
    details: dict[str, Any] | None
    state_before: str
    state_after: str
    released: bool
    anomaly_code: str | None
    error_code: str | None
    error_message: str | None


class RunDetailOut(BaseModel):
    run: RunOut
    observations: list[ObservationOut]


def _run_out(r: SyncRunRecord) -> RunOut:
    err = r.error_details or {}
    return RunOut(
        id=r.id,
        mode=r.mode,
        scope=r.scope,
        status=r.status,
        started_at=r.started_at,
        finished_at=r.finished_at,
        requested_by_user_id=r.requested_by_user_id,
        ppr_id=r.ppr_id,
        retry_of_sync_run_id=r.retry_of_sync_run_id,
        checked=r.records_checked,
        changed=r.records_changed,
        cancelled=r.records_cancelled,
        non_critical=r.records_updated,
        not_attempted=len(err.get("not_attempted", [])),
        error_details=r.error_details,
    )


def _obs_out(o: ObservationRecord) -> ObservationOut:
    return ObservationOut(
        id=o.id,
        sync_run_id=o.sync_run_id,
        ppr_id=o.ppr_id,
        ppr_number=o.ppr_number,
        pr_no=o.pr_no,
        compared_version=o.compared_version,
        observed_at=o.observed_at,
        recorded_at=o.recorded_at,
        outcome=o.outcome,
        evidence_class=o.evidence_class,
        hosxp_status=o.hosxp_status,
        native_status=o.native_status,
        changes=o.changes,
        evidence_issues=o.evidence_issues,
        live_pr=o.live_pr,
        details=o.details,
        state_before=o.state_before,
        state_after=o.state_after,
        released=o.released,
        anomaly_code=o.anomaly_code,
        error_code=o.error_code,
        error_message=o.error_message,
    )


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    Runner = Annotated[UserRecord, Depends(require(Permission.SYNC_RUN))]  # noqa: N806
    Viewer = Annotated[UserRecord, Depends(require(Permission.SYNC_READ))]  # noqa: N806
    Reader = Annotated[UserRecord, Depends(require(Permission.PPR_READ))]  # noqa: N806
    settings: sync.SyncSettings = getattr(deps, "sync_settings", None) or sync.SyncSettings()

    def factory() -> Any:
        return unit_of_work(deps.engine)

    def read(fn: Callable[..., Any], *args: Any) -> Any:
        try:
            with unit_of_work(deps.engine) as uow:
                return fn(uow, *args)
        except ServiceError as exc:
            raise _http(exc) from exc

    def start(
        user: UserRecord, scope: SyncScope, *, background: bool = False, **kw: Any
    ) -> RunDetailOut:
        try:
            started = sync.start_sync(
                factory,
                mode=sync.MANUAL,
                scope=scope,
                requested_by=user.id,
                settings=settings,
                **kw,
            )
            if background:
                threading.Thread(
                    target=_execute_quietly,
                    args=(factory, deps.gateway, started),
                    name=f"pr-sync-{started.run_id}",
                    daemon=True,
                ).start()
                with unit_of_work(deps.engine) as uow:
                    run = uow.sync_runs.get(started.run_id)
                assert run is not None
                return RunDetailOut(run=_run_out(run), observations=[])
            result = sync.execute_sync(factory, deps.gateway, started)
        except ServiceError as exc:
            raise _http(exc) from exc
        return RunDetailOut(
            run=_run_out(result.run), observations=[_obs_out(o) for o in result.observations]
        )

    @app.post("/api/pr-sync", response_model=RunDetailOut)
    def sync_all(body: SyncAllIn, user: Runner) -> RunDetailOut:
        if body.retry_of is not None:
            return start(
                user, SyncScope.PPR_RETRY, background=body.background, retry_of=body.retry_of
            )
        return start(user, SyncScope.PPRS, background=body.background)

    @app.post("/api/pprs/{ppr_id}/sync", response_model=RunDetailOut)
    def sync_one(ppr_id: int, user: Runner) -> RunDetailOut:
        return start(user, SyncScope.PPR, ppr_id=ppr_id)

    @app.post("/api/pprs/{ppr_id}/recheck", response_model=RunDetailOut)
    def recheck(ppr_id: int, user: Runner) -> RunDetailOut:
        return start(user, SyncScope.PPR_RECHECK, ppr_id=ppr_id)

    @app.get("/api/pr-sync/governance", response_model=list[ObservationOut])
    def governance_list(
        _: Runner, limit: Annotated[int, Query(ge=1, le=500)] = 200
    ) -> list[ObservationOut]:
        return [_obs_out(o) for o in read(sync.governance, limit)]

    @app.get("/api/pr-sync/runs", response_model=list[RunOut])
    def list_runs(_: Viewer, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> list[RunOut]:
        return [_run_out(r) for r in read(sync.runs, limit)]

    @app.get("/api/pr-sync/runs/{run_id}", response_model=RunDetailOut)
    def run_detail(run_id: int, user: Viewer) -> RunDetailOut:
        run, observations = read(sync.run_detail, run_id, _caller(user).ppr_scope)
        return RunDetailOut(run=_run_out(run), observations=[_obs_out(o) for o in observations])

    @app.get("/api/pprs/{ppr_id}/observations", response_model=list[ObservationOut])
    def observations(
        ppr_id: int, user: Reader, limit: Annotated[int, Query(ge=1, le=500)] = 200
    ) -> list[ObservationOut]:
        def fn(uow: Any, caller: svc.Caller) -> list[ObservationRecord]:
            svc.get_ppr(uow, caller, ppr_id)  # scope: other departments answer 404
            return sync.observations_for(uow, ppr_id, limit)

        return [_obs_out(o) for o in read(fn, _caller(user))]
