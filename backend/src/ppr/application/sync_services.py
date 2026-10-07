"""Wave 6 use cases: PR synchronization, HOSxP cancellation, governance re-check
(spec §16, §25, §26; D-02, D-03, D-10, D-29 ... D-31).

Per PPR (D-29), HOSxP is never read while a database lock is held:

1. short read (no lock): the PPR, its bound PR number, state, current version and the
   confirmed PR snapshot of that version;
2. live read of the PR header (and, when the PR is bound, readable and not cancelled,
   its items) OUTSIDE any transaction; ``observed_at`` is when HOSxP answered;
3. a pure assessment (``domain.pr_sync``); a header/items inconsistency that a
   concurrent HOSxP edit can explain is read ONCE more, still outside any lock;
4. one transaction: lock the PPR row, re-check state / version / release; a PPR that
   moved meanwhile is re-assessed against its locked state, or skipped
   (``SKIPPED_STATE_MOVED``) when a new version was confirmed or it left the scope;
5. plan items are locked only when a cancellation release will happen; the state
   change, ``PPR_RELEASED`` ledger entries, display status, observation and audit
   commit together or not at all.

One transaction per PPR: one PPR's failure never undoes another's result. Runs stop
early after several consecutive "HOSxP unavailable" results, so a down HOSxP does not
cost one timeout per PPR; the PPRs not attempted are listed on the run and retried by a
retry run.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from ppr.application import assigned_services
from ppr.application.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    ServiceError,
)
from ppr.application.pr_snapshot import header_json, items_json
from ppr.domain.ledger import LedgerRuleError, PlanItemLedger
from ppr.domain.ppr_state import PprEvent, PprState
from ppr.domain.pr_sync import (
    GOVERNANCE_OUTCOMES,
    SYNC_STATES,
    Assessment,
    EvidenceClass,
    LiveRead,
    ReadFailure,
    SyncOutcome,
    SyncScope,
    assess,
    assess_recheck,
    decide,
    needs_items,
    worth_one_fresh_read,
)
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpUnavailableError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.ports import (
    SYSTEM_ACTOR,
    Actor,
    AuditEntry,
    ObservationInput,
    ObservationRecord,
    PprRecord,
    PprScope,
    SyncRunRecord,
    SyncTarget,
    UniquenessError,
    UnitOfWork,
    UnitOfWorkFactory,
)

_log = logging.getLogger(__name__)
ZERO = Decimal(0)
MANUAL = "MANUAL"
NIGHTLY = "NIGHTLY"
# Retry repeats attempts that did not produce a trustworthy observation.
RETRY_OUTCOMES = (SyncOutcome.UNAVAILABLE.value, SyncOutcome.ERROR.value)
RETRY_INVALID_CLASSES = (EvidenceClass.BINDING.value, EvidenceClass.STATUS.value)
_PROBLEM_OUTCOMES = frozenset({SyncOutcome.UNAVAILABLE, SyncOutcome.ERROR})
_UNRELIABLE = frozenset({EvidenceClass.BINDING, EvidenceClass.STATUS})


@dataclass(frozen=True)
class SyncSettings:
    # Stop a run after this many consecutive "HOSxP unavailable" results.
    max_consecutive_unavailable: int = 3
    # A RUNNING run older than this is treated as crashed (marked FAILED).
    stale_after: timedelta = timedelta(hours=6)


@dataclass(frozen=True)
class RunResult:
    run: SyncRunRecord
    observations: list[ObservationRecord]


@dataclass
class _Tally:
    checked: int = 0
    updated: int = 0
    changed: int = 0
    cancelled: int = 0
    problems: int = 0
    trusted: int = 0
    not_attempted: list[int] = field(default_factory=list)
    aborted: bool = False


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ live read
def _read(
    gateway: HosxpGateway,
    pr_no: str,
    confirmed_pr_id: str | None,
    clock: Clock,
    *,
    with_items: bool,
) -> tuple[LiveRead, datetime | None]:
    """Read the PR live, outside any transaction. ``observed_at`` is when HOSxP last
    answered (None when it could not be reached at all)."""
    try:
        header = gateway.get_pr(pr_no)
    except HosxpConfigurationError as exc:  # before its base class: HOSxP DID answer
        return LiveRead(None, failure=ReadFailure.HEADER_QUERY, failure_message=str(exc)), clock()
    except HosxpUnavailableError:
        return LiveRead(None, failure=ReadFailure.UNAVAILABLE), None
    observed_at = clock()
    if header is None:
        return LiveRead(None), observed_at
    hj = header_json(header)
    read = LiveRead(hj)
    if not with_items or not needs_items(read, pr_no, confirmed_pr_id):
        return read, observed_at
    try:
        items = gateway.get_pr_items(pr_no)
    except HosxpConfigurationError as exc:
        return (
            LiveRead(hj, failure=ReadFailure.ITEMS_QUERY, failure_message=str(exc)),
            clock(),
        )
    except HosxpUnavailableError:
        return LiveRead(hj, failure=ReadFailure.UNAVAILABLE), observed_at
    return LiveRead(hj, tuple(items_json(items))), clock()


def _live_pr(read: LiveRead) -> dict[str, Any] | None:
    if read.header is None:
        return None
    return {
        "header": dict(read.header),
        "items": None if read.items is None else [dict(i) for i in read.items],
    }


@dataclass(frozen=True)
class _Observed:
    read: LiveRead
    observed_at: datetime | None
    details: dict[str, Any] | None


def _observe(
    gateway: HosxpGateway,
    pr_no: str,
    confirmed: Mapping[str, Any],
    clock: Clock,
    *,
    recheck: bool,
) -> _Observed:
    pr_id = confirmed["pr"].get("pr_id")
    read, at = _read(gateway, pr_no, pr_id, clock, with_items=not recheck)
    if recheck:
        return _Observed(read, at, None)
    first = assess(
        read,
        bound_pr_no=pr_no,
        confirmed_header=confirmed["pr"],
        confirmed_items=confirmed["pr_items"],
    )
    if (
        first.outcome is SyncOutcome.INVALID_EVIDENCE
        and first.evidence_class is EvidenceClass.CONTROL
        and worth_one_fresh_read(first.issues)
    ):
        # D-29: header and items are separate queries; one immediate fresh read (still
        # outside any lock) tells a concurrent HOSxP edit from genuinely bad evidence.
        again, again_at = _read(gateway, pr_no, pr_id, clock, with_items=True)
        discarded = {
            "discarded_first_read": {
                "observed_at": at.isoformat() if at else None,
                "evidence_issues": [i.as_dict() for i in first.issues],
                "live_pr": _live_pr(read),
            }
        }
        return _Observed(again, again_at, discarded)
    return _Observed(read, at, None)


# ------------------------------------------------------------------ one PPR
def _confirmed_snapshot(uow: UnitOfWork, rec: PprRecord) -> dict[str, Any] | None:
    for v in reversed(uow.pprs.versions(rec.id)):
        if v.version == rec.current_version:
            return v.snapshot
    return None


def _release(uow: UnitOfWork, rec: PprRecord) -> list[dict[str, Any]]:
    """Release exactly what the PPR number still holds, per plan item (D-30)."""
    assert rec.ppr_number is not None
    number = rec.ppr_number
    plan_items = uow.ledger.plan_items_of_ppr(number)
    uow.pprs.lock_plan_items(plan_items)
    released: list[dict[str, Any]] = []
    for pid in plan_items:
        ledger = PlanItemLedger(str(pid), uow.ledger.entries(pid))
        entry = ledger.release_entry_for(number)
        if entry is None:
            continue
        before = ledger.balance
        try:
            after = ledger.append(entry).balance  # domain rules: never over-release
        except LedgerRuleError as exc:
            raise ConflictError("RELEASE_REFUSED", str(exc)) from exc
        uow.ledger.append(entry, None)
        released.append(
            {
                "plan_item_id": pid,
                "qty": str(entry.qty_delta),
                "amount": str(entry.amount_delta),
                "idempotency_key": entry.idempotency_key,
                "remaining_qty_before": str(before.remaining_qty),
                "remaining_amount_before": str(before.remaining_amount),
                "remaining_qty_after": str(after.remaining_qty),
                "remaining_amount_after": str(after.remaining_amount),
            }
        )
    return released


def _apply(
    uow: UnitOfWork,
    run_id: int,
    target: SyncTarget,
    observed: _Observed,
    *,
    recheck: bool,
) -> tuple[ObservationInput, list[dict[str, Any]]]:
    rec = uow.pprs.get(target.ppr_id, for_update=True)
    if rec is None:  # pragma: no cover - PPRs are never deleted
        raise NotFoundError("PPR_NOT_FOUND", f"PPR {target.ppr_id} not found")
    read, at = observed.read, observed.observed_at
    header = read.header or {}
    base = ObservationInput(
        sync_run_id=run_id,
        ppr_id=rec.id,
        pr_no=rec.pr_no,
        outcome=SyncOutcome.SKIPPED_STATE_MOVED.value,
        state_before=rec.state,
        state_after=rec.state,
        compared_version=rec.current_version,
        observed_at=at,
        details=observed.details,
        live_pr=_live_pr(read),
        hosxp_status=header.get("status"),
        native_status=header.get("native_status"),
    )

    moved = (
        rec.state is not PprState.CANCELLED
        if recheck
        else rec.state not in SYNC_STATES or rec.current_version != target.version
    )
    snapshot = None if moved else _confirmed_snapshot(uow, rec)
    if moved or snapshot is None:
        # Another worker or user acted while HOSxP was being read (a new version was
        # confirmed, or the PPR left the scope): this evidence no longer applies.
        uow.sync_observations.add(base)
        return base, []

    a: Assessment
    if recheck:
        a = assess_recheck(read, bound_pr_no=rec.pr_no, confirmed_header=snapshot["pr"])
    else:
        a = assess(
            read,
            bound_pr_no=rec.pr_no,
            confirmed_header=snapshot["pr"],
            confirmed_items=snapshot["pr_items"],
        )
    decision = decide(rec.state, a) if not recheck else None
    state_after = decision.state_after if decision else rec.state

    released: list[dict[str, Any]] = []
    did_release = False
    if decision is not None and decision.release:
        if uow.sync_observations.released_for(rec.id):  # pragma: no cover - state guards it
            raise ConflictError("ALREADY_RELEASED", "this PPR's plan usage was already released")
        released = _release(uow, rec)
        did_release = True
    if state_after is not rec.state:
        uow.pprs.set_state(rec.id, state_after)
    if state_after is not rec.state or did_release:
        # D-38 A-3 / A-4: demands this PPR covered follow its new state.
        assigned_services.refresh_demands(
            uow,
            uow.assigned.coverage(rec.id, confirmed=True),
            SYSTEM_ACTOR,
            {
                "ppr_id": rec.id,
                "event": decision.event.value if decision and decision.event else "SYNC",
                "sync_run_id": run_id,
            },
        )

    reliable = a.outcome not in (
        SyncOutcome.UNAVAILABLE,
        SyncOutcome.NOT_FOUND,
        SyncOutcome.SKIPPED_STATE_MOVED,
    ) and not (a.outcome is SyncOutcome.INVALID_EVIDENCE and a.evidence_class in _UNRELIABLE)
    if reliable and read.header is not None:
        status = str(read.header.get("status"))
        native = read.header.get("native_status")
        if status != rec.hosxp_pr_status or native != rec.hosxp_native_status:
            uow.pprs.set_hosxp_status(rec.id, status, native)

    obs = replace(
        base,
        outcome=a.outcome.value,
        state_after=state_after,
        evidence_class=a.evidence_class.value if a.evidence_class else None,
        changes=[c.as_dict() for c in a.changes] or None,
        evidence_issues=[i.as_dict() for i in a.issues] or None,
        released=did_release,
        anomaly_code=a.anomaly_code,
    )
    try:
        obs_id = uow.sync_observations.add(obs)
    except UniquenessError as exc:
        raise ConflictError("ALREADY_RELEASED", str(exc)) from exc
    _audit(uow, rec, obs, obs_id, decision.event if decision else None, released)
    return obs, released


_AUDITED = {
    SyncOutcome.NON_CRITICAL_CHANGE: "PPR_PR_NON_CRITICAL_CHANGE",
    SyncOutcome.NOT_FOUND: "PPR_PR_NOT_FOUND",
    SyncOutcome.INVALID_EVIDENCE: "PPR_PR_INVALID_EVIDENCE",
    SyncOutcome.ANOMALY: "PPR_PR_ANOMALY",
}


def _audit(
    uow: UnitOfWork,
    rec: PprRecord,
    obs: ObservationInput,
    obs_id: int,
    event: PprEvent | None,
    released: list[dict[str, Any]],
) -> None:
    if event is PprEvent.CANCEL_FROM_HOSXP:
        action = "PPR_CANCELLED_FROM_HOSXP"
    elif event is PprEvent.DETECT_PR_CHANGE:
        action = "PPR_PR_CHANGED"
    elif (a := _AUDITED.get(SyncOutcome(obs.outcome))) is not None:
        action = a
    else:
        return
    after: dict[str, Any] = {
        "state": obs.state_after.value,
        "version": rec.current_version,
        "sync_run_id": obs.sync_run_id,
        "observation_id": obs_id,
        "outcome": obs.outcome,
        "observed_at": obs.observed_at.isoformat() if obs.observed_at else None,
    }
    if obs.evidence_class:
        after["evidence_class"] = obs.evidence_class
    if obs.evidence_issues:
        after["evidence_issues"] = obs.evidence_issues
    if obs.changes:
        after["pr_changes"] = obs.changes
    if obs.anomaly_code:
        after["anomaly_code"] = obs.anomaly_code
    if event is PprEvent.CANCEL_FROM_HOSXP:
        after["released"] = released
    uow.audit.write(
        AuditEntry(
            SYSTEM_ACTOR,
            action,
            "ppr",
            str(rec.id),
            before={"state": obs.state_before.value, "version": rec.current_version},
            after=after,
        )
    )


def _sync_target(
    uow_factory: UnitOfWorkFactory,
    gateway: HosxpGateway,
    run_id: int,
    target: SyncTarget,
    clock: Clock,
    *,
    recheck: bool,
) -> ObservationInput:
    observed_at: datetime | None = None
    try:
        # 1. short read, no lock held afterwards
        with uow_factory() as uow:
            rec = uow.pprs.get(target.ppr_id)
            snapshot = _confirmed_snapshot(uow, rec) if rec is not None else None
        if rec is None or snapshot is None or rec.current_version != target.version:
            return _record_plain(
                uow_factory, run_id, target, SyncOutcome.SKIPPED_STATE_MOVED, None, None
            )
        # 2.-3. live read and assessment, outside any transaction
        observed = _observe(gateway, rec.pr_no, snapshot, clock, recheck=recheck)
        observed_at = observed.observed_at
        # 4.-5. one atomic transaction
        with uow_factory() as uow:
            obs, _ = _apply(uow, run_id, target, observed, recheck=recheck)
        return obs
    except Exception as exc:
        # Anything unexpected is this PPR's ERROR, never the run's end; details stay in
        # the server log, the stored message is a safe one (D-29).
        if isinstance(exc, ServiceError):
            code, message = exc.code, str(exc)[:500]
        else:
            _log.exception("PR sync of PPR %s failed", target.ppr_id)
            code, message = type(exc).__name__, "unexpected error; see the server log"
        return _record_plain(
            uow_factory, run_id, target, SyncOutcome.ERROR, code, message, observed_at=observed_at
        )


def _record_plain(
    uow_factory: UnitOfWorkFactory,
    run_id: int,
    target: SyncTarget,
    outcome: SyncOutcome,
    error_code: str | None,
    error_message: str | None,
    *,
    observed_at: datetime | None = None,
) -> ObservationInput:
    """Record an attempt that changed nothing (in its own small transaction)."""
    with uow_factory() as uow:
        rec = uow.pprs.get(target.ppr_id)
        state = rec.state if rec is not None else target.state
        obs = ObservationInput(
            sync_run_id=run_id,
            ppr_id=target.ppr_id,
            pr_no=target.pr_no,
            outcome=outcome.value,
            state_before=state,
            state_after=state,
            compared_version=rec.current_version if rec is not None else None,
            observed_at=observed_at,
            error_code=error_code,
            error_message=error_message,
        )
        uow.sync_observations.add(obs)
    return obs


# ------------------------------------------------------------------ runs
def _targets(
    uow: UnitOfWork, scope: SyncScope, ppr_id: int | None, retry_of: int | None
) -> list[SyncTarget]:
    if scope is SyncScope.PPRS:
        return uow.sync_observations.sync_targets(sorted(SYNC_STATES))
    if scope in (SyncScope.PPR, SyncScope.PPR_RECHECK):
        assert ppr_id is not None
        rec = uow.pprs.get(ppr_id)
        if rec is None or rec.discarded_at is not None:
            raise NotFoundError("PPR_NOT_FOUND", f"PPR {ppr_id} not found")
        if scope is SyncScope.PPR and rec.state not in SYNC_STATES:
            raise ConflictError(
                "PPR_NOT_SYNCABLE",
                f"a {rec.state.value} PPR is not synchronized (drafts never; a cancelled "
                "PPR only by a governance re-check)",
            )
        if scope is SyncScope.PPR_RECHECK and rec.state is not PprState.CANCELLED:
            raise ConflictError("NOT_CANCELLED", "only a cancelled PPR can be re-checked")
        return [SyncTarget(rec.id, rec.pr_no, rec.state, rec.current_version)]
    assert retry_of is not None
    previous = uow.sync_runs.get(retry_of)
    if previous is None or previous.scope == "MASTERS":
        raise NotFoundError("SYNC_RUN_NOT_FOUND", f"PR synchronization run {retry_of} not found")
    if previous.status == "RUNNING":
        raise ConflictError("RUN_STILL_RUNNING", "that run has not finished yet")
    if previous.scope == SyncScope.PPR_RECHECK.value:
        raise ConflictError("RUN_NOT_RETRYABLE", "a governance re-check is repeated by hand")
    ids = set(uow.sync_observations.retry_targets(retry_of, RETRY_OUTCOMES, RETRY_INVALID_CLASSES))
    ids |= {int(i) for i in (previous.error_details or {}).get("not_attempted", [])}
    out = []
    for pid in sorted(ids):
        rec = uow.pprs.get(pid)
        if rec is not None and rec.state in SYNC_STATES:  # still open: retry it
            out.append(SyncTarget(rec.id, rec.pr_no, rec.state, rec.current_version))
    return out


@dataclass(frozen=True)
class StartedRun:
    run_id: int
    scope: SyncScope
    targets: list[SyncTarget]
    actor: Actor
    settings: SyncSettings


def start_sync(
    uow_factory: UnitOfWorkFactory,
    *,
    mode: str,
    scope: SyncScope,
    requested_by: int | None,
    ppr_id: int | None = None,
    retry_of: int | None = None,
    settings: SyncSettings | None = None,
    clock: Clock = _utc_now,
) -> StartedRun:
    """Validate the request, select the targets and record the RUNNING run."""
    settings = settings or SyncSettings()
    if mode not in (MANUAL, NIGHTLY):
        raise InvalidInputError("UNKNOWN_MODE", f"unknown sync mode {mode!r}")
    if mode == MANUAL and requested_by is None:
        raise InvalidInputError("REQUESTER_REQUIRED", "a manual sync must name who requested it")
    if (scope in (SyncScope.PPR, SyncScope.PPR_RECHECK)) != (ppr_id is not None):
        raise InvalidInputError("PPR_REQUIRED", "a one-PPR synchronization names the PPR")
    if (scope is SyncScope.PPR_RETRY) != (retry_of is not None):
        raise InvalidInputError("RUN_REQUIRED", "a retry names the run it repeats")

    actor = Actor(requested_by)
    with uow_factory() as uow:
        # Runs left RUNNING by a crashed process are closed first (every scope).
        uow.sync_runs.fail_stale(clock() - settings.stale_after)
        targets = _targets(uow, scope, ppr_id, retry_of)
        try:
            run_id = uow.sync_runs.start(
                mode, scope.value, requested_by, ppr_id=ppr_id, retry_of=retry_of
            )
        except UniquenessError as exc:
            raise ConflictError("SYNC_ALREADY_RUNNING", str(exc)) from exc
        uow.audit.write(
            AuditEntry(
                actor,
                "PR_SYNC_STARTED",
                "sync_run",
                str(run_id),
                after={
                    "mode": mode,
                    "scope": scope.value,
                    "ppr_id": ppr_id,
                    "retry_of_sync_run_id": retry_of,
                    "targets": len(targets),
                },
            )
        )
    return StartedRun(run_id, scope, targets, actor, settings)


def execute_sync(
    uow_factory: UnitOfWorkFactory,
    gateway: HosxpGateway,
    started: StartedRun,
    *,
    clock: Clock = _utc_now,
) -> RunResult:
    """Observe every target (one transaction each) and finish the run."""
    run_id, targets, settings = started.run_id, started.targets, started.settings
    tally = _Tally()
    streak = 0
    i = 0
    try:
        for i, target in enumerate(targets):
            obs = _sync_target(
                uow_factory,
                gateway,
                run_id,
                target,
                clock,
                recheck=started.scope is SyncScope.PPR_RECHECK,
            )
            _count(tally, obs)
            streak = streak + 1 if obs.outcome == SyncOutcome.UNAVAILABLE.value else 0
            if streak >= settings.max_consecutive_unavailable and i + 1 < len(targets):
                tally.aborted = True
                tally.not_attempted = [t.ppr_id for t in targets[i + 1 :]]
                break
    except Exception as exc:
        _log.exception("PR sync run %s failed", run_id)
        with uow_factory() as uow:
            uow.sync_runs.finish(
                run_id,
                "FAILED",
                checked=tally.checked,
                updated=tally.updated,
                changed=tally.changed,
                cancelled=tally.cancelled,
                # everything from the failing PPR on is left for a retry run
                error={
                    "reason": type(exc).__name__,
                    "not_attempted": [t.ppr_id for t in targets[i:]],
                },
            )
        raise

    status = _status(tally, len(targets))
    error: dict[str, Any] | None = None
    if tally.aborted:
        error = {"reason": "HOSXP_UNAVAILABLE", "not_attempted": tally.not_attempted}
    with uow_factory() as uow:
        uow.sync_runs.finish(
            run_id,
            status,
            checked=tally.checked,
            updated=tally.updated,
            changed=tally.changed,
            cancelled=tally.cancelled,
            error=error,
        )
        finished = uow.sync_runs.get(run_id)
        assert finished is not None
        failed = failed_ppr_ids(uow, finished) if status != "SUCCEEDED" else []
        uow.audit.write(
            AuditEntry(
                started.actor,
                "PR_SYNC_FINISHED",
                "sync_run",
                str(run_id),
                after={
                    "status": status,
                    "checked": tally.checked,
                    "changed": tally.changed,
                    "cancelled": tally.cancelled,
                    "problems": tally.problems,
                    "not_attempted": len(tally.not_attempted),
                    "failed_ppr_ids": failed[:500],  # D-36: the failure stays in the history
                },
            )
        )
        if started.scope is SyncScope.PPR_RETRY:
            # D-36: the correction is recorded against the run that failed.
            origin = root_run(uow, finished)
            fs = failure_status(uow, origin)
            uow.audit.write(
                AuditEntry(
                    started.actor,
                    "PR_SYNC_RETRY_RESULT",
                    "sync_run",
                    str(origin.id),
                    after={
                        "retry_sync_run_id": run_id,
                        "failed": len(fs.failed),
                        "resolved": len(fs.resolved),
                        "remaining": len(fs.remaining),
                        "remaining_ppr_ids": list(fs.remaining[:500]),
                    },
                )
            )
        run = uow.sync_runs.get(run_id)
        observations = uow.sync_observations.for_run(run_id, None)
    assert run is not None
    return RunResult(run, observations)


def run_sync(
    uow_factory: UnitOfWorkFactory,
    gateway: HosxpGateway,
    *,
    mode: str,
    scope: SyncScope,
    requested_by: int | None,
    ppr_id: int | None = None,
    retry_of: int | None = None,
    settings: SyncSettings | None = None,
    clock: Clock = _utc_now,
) -> RunResult:
    """Run one PR synchronization to the end (manual or nightly). Always recorded."""
    started = start_sync(
        uow_factory,
        mode=mode,
        scope=scope,
        requested_by=requested_by,
        ppr_id=ppr_id,
        retry_of=retry_of,
        settings=settings,
        clock=clock,
    )
    return execute_sync(uow_factory, gateway, started, clock=clock)


def _count(t: _Tally, obs: ObservationInput) -> None:
    t.checked += 1
    outcome = SyncOutcome(obs.outcome)
    if outcome is SyncOutcome.NON_CRITICAL_CHANGE:
        t.updated += 1
    if obs.state_after is PprState.PR_CHANGED_REVIEW_REQUIRED and obs.state_before in (
        PprState.CONFIRMED_LOCKED,
        PprState.PROCUREMENT_VERIFIED,
    ):
        t.changed += 1
    if obs.released:
        t.cancelled += 1
    unreliable = outcome is SyncOutcome.INVALID_EVIDENCE and obs.evidence_class in (
        RETRY_INVALID_CLASSES
    )
    if outcome in _PROBLEM_OUTCOMES or unreliable:
        t.problems += 1
    else:
        t.trusted += 1


def _status(t: _Tally, total: int) -> str:
    if total == 0:
        return "SUCCEEDED"
    if t.problems == 0 and not t.aborted:
        return "SUCCEEDED"
    if t.trusted == 0:
        return "FAILED"
    return "PARTIAL"


# ------------------------------------------------------------------ D-36: failures and their retry
@dataclass(frozen=True)
class FailureStatus:
    """The failed PPRs of one run and which of them later syncs resolved (D-36)."""

    run: SyncRunRecord
    failed: tuple[int, ...]
    resolved: tuple[int, ...]
    remaining: tuple[int, ...]


RESOLVING_SCOPES = (SyncScope.PPR.value, SyncScope.PPR_RETRY.value)


def failed_ppr_ids(uow: UnitOfWork, run: SyncRunRecord) -> list[int]:
    """Exactly the PPRs a retry of ``run`` repeats: untrustworthy attempts + not attempted."""
    ids = set(uow.sync_observations.retry_targets(run.id, RETRY_OUTCOMES, RETRY_INVALID_CLASSES))
    ids |= {int(i) for i in (run.error_details or {}).get("not_attempted", [])}
    return sorted(ids)


def _trustworthy(outcome: str, evidence_class: str | None) -> bool:
    if outcome in RETRY_OUTCOMES:
        return False
    return not (
        outcome == SyncOutcome.INVALID_EVIDENCE.value and evidence_class in RETRY_INVALID_CLASSES
    )


def failure_status(uow: UnitOfWork, run: SyncRunRecord) -> FailureStatus:
    failed = failed_ppr_ids(uow, run)
    still_open = []
    resolved = []
    for pid in failed:
        rec = uow.pprs.get(pid)
        if rec is None or rec.state not in SYNC_STATES:
            resolved.append(pid)  # out of scope: nothing left to repeat
        else:
            still_open.append(pid)
    latest = uow.sync_observations.latest_after(run.id, still_open, RESOLVING_SCOPES)
    remaining = []
    for pid in still_open:
        seen = latest.get(pid)
        if seen is not None and _trustworthy(*seen):
            resolved.append(pid)
        else:
            remaining.append(pid)
    return FailureStatus(run, tuple(failed), tuple(sorted(resolved)), tuple(remaining))


def root_run(uow: UnitOfWork, run: SyncRunRecord) -> SyncRunRecord:
    """The run a chain of retries started from."""
    seen = {run.id}
    while run.retry_of_sync_run_id is not None:
        parent = uow.sync_runs.get(run.retry_of_sync_run_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        run = parent
    return run


# ------------------------------------------------------------------ reads
def observations_for(uow: UnitOfWork, ppr_id: int, limit: int = 200) -> list[ObservationRecord]:
    return uow.sync_observations.for_ppr(ppr_id, limit)


def governance(uow: UnitOfWork, limit: int = 200) -> list[ObservationRecord]:
    """PPRs whose LATEST observation needs attention (one row per PPR, newest first)."""
    return uow.sync_observations.governance(sorted(o.value for o in GOVERNANCE_OUTCOMES), limit)


def runs(uow: UnitOfWork, limit: int = 50) -> list[SyncRunRecord]:
    return uow.sync_runs.recent_ppr_runs(limit)


def run_detail(
    uow: UnitOfWork, run_id: int, scope: PprScope | None
) -> tuple[SyncRunRecord, list[ObservationRecord]]:
    run = uow.sync_runs.get(run_id)
    if run is None or run.scope == "MASTERS":
        raise NotFoundError("SYNC_RUN_NOT_FOUND", f"PR synchronization run {run_id} not found")
    return run, uow.sync_observations.for_run(run_id, scope)


def pending_targets(uow: UnitOfWork) -> Sequence[SyncTarget]:
    """What a full synchronization would look at now (for display)."""
    return uow.sync_observations.sync_targets(sorted(SYNC_STATES))
