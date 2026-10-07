"""Wave 5B: Head of Procurement appointments, verification, queue, search, timeline.

Spec §14.4, §22.3, §22.4, §23, §31.7, §32, §33; decisions D-27, D-28.

* Appointments (§23, D-27): Admin creates, ends early or revokes; never deletes; audited.
* Verification (§14.4, D-28): ``CONFIRMED_LOCKED`` -> ``PROCUREMENT_VERIFIED`` by a user with
  the HEAD_OF_PROCUREMENT role AND an appointment effective on the server date, of that
  date's fiscal year. The version the user saw must still be current. Records the
  appointment and version (append-only) and audits. No ledger posting, no HOSxP call.
* Search (§32) and timeline (§33) use the same department scope as reading one PPR, so
  they never reveal a PPR the caller could not open. The timeline is built only from this
  system's own records (audit log, confirmed versions, verifications) - never from HOSxP.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from ppr.application import assigned_services
from ppr.application.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
)
from ppr.application.ppr_services import Caller, _authorize, _load
from ppr.domain.appointment import (
    AppointmentRuleError,
    Period,
    authorizes,
    check_new_end,
    check_no_overlap,
    resolve_range,
)
from ppr.domain.fiscal_year import FiscalYearConfig, fiscal_year_label
from ppr.domain.ppr_state import PprEvent, PprState
from ppr.domain.roles import Role
from ppr.ports import (
    Actor,
    AppointmentRecord,
    AuditEntry,
    AuditRecord,
    PprRecord,
    PprSearch,
    UniquenessError,
    UnitOfWork,
    VerificationRecord,
)

DEFAULT_INACTIVE_DAYS = 30  # spec §34, configurable


def _appointment_payload(a: AppointmentRecord) -> dict[str, Any]:
    return {
        "user_id": a.user_id,
        "fiscal_year": a.fiscal_year,
        "effective_from": a.effective_from.isoformat(),
        "effective_to": a.effective_to.isoformat() if a.effective_to else None,
        "status": a.status,
    }


def _rule(exc: AppointmentRuleError) -> Exception:
    if exc.code == "APPOINTMENT_OVERLAP":
        return ConflictError(exc.code, str(exc))
    return InvalidInputError(exc.code, str(exc))


def _active_periods(uow: UnitOfWork, exclude: int | None = None) -> list[Period]:
    return [
        Period(a.id, a.effective_from, a.effective_to)
        for a in uow.appointments.list()
        if a.status == "ACTIVE" and a.id != exclude
    ]


# ------------------------------------------------------------------ appointments (§23)
def list_appointments(uow: UnitOfWork, fiscal_year: int | None = None) -> list[AppointmentRecord]:
    return uow.appointments.list(fiscal_year)


def create_appointment(
    uow: UnitOfWork,
    actor: Actor,
    user_id: int,
    fiscal_year: int,
    effective_from: date,
    effective_to: date | None,
    cfg: FiscalYearConfig,
) -> AppointmentRecord:
    assert actor.user_id is not None
    user = uow.users.get(user_id)
    if user is None:
        raise NotFoundError("USER_NOT_FOUND", f"user {user_id} not found")
    if not user.active or Role.HEAD_OF_PROCUREMENT not in user.roles:
        raise InvalidInputError(
            "USER_NOT_ELIGIBLE",
            "only an active user holding the HEAD_OF_PROCUREMENT role can be appointed",
        )
    try:
        start, end = resolve_range(fiscal_year, effective_from, effective_to, cfg)
    except AppointmentRuleError as exc:
        raise _rule(exc) from exc
    uow.appointments.lock()
    try:
        check_no_overlap(start, end, _active_periods(uow))
        # Never cover dates that already hold verification evidence under another
        # appointment (through the API a revoked one never has any; this also guards
        # legacy or bypassed data so the history stays consistent).
        if uow.appointments.verifications_between(start, end):
            raise ConflictError(
                "APPOINTMENT_OVERLAPS_EVIDENCE",
                "PPRs were already verified on these dates under another appointment",
            )
        new_id = uow.appointments.create(user_id, fiscal_year, start, end, actor.user_id)
    except AppointmentRuleError as exc:
        raise _rule(exc) from exc
    except UniquenessError as exc:  # the database's own overlap rule (D-27)
        raise ConflictError("APPOINTMENT_OVERLAP", str(exc)) from exc
    rec = uow.appointments.get(new_id)
    assert rec is not None
    uow.audit.write(
        AuditEntry(
            actor,
            "APPOINTMENT_CREATED",
            "role_appointment",
            str(new_id),
            after=_appointment_payload(rec),
        )
    )
    return rec


def end_appointment(
    uow: UnitOfWork,
    actor: Actor,
    appointment_id: int,
    new_end: date,
    reason: str | None,
    today: date,
) -> AppointmentRecord:
    reason = (reason or "").strip()
    if not reason:
        raise InvalidInputError("REASON_REQUIRED", "ending an appointment early needs a reason")
    uow.appointments.lock()
    rec = uow.appointments.get(appointment_id, for_update=True)
    if rec is None:
        raise NotFoundError("APPOINTMENT_NOT_FOUND", f"appointment {appointment_id} not found")
    if rec.status != "ACTIVE":
        raise ConflictError("APPOINTMENT_NOT_ACTIVE", "only an ACTIVE appointment can be ended")
    try:
        check_new_end(
            Period(rec.id, rec.effective_from, rec.effective_to),
            new_end,
            today,
            uow.appointments.last_verification_day(rec.id),
        )
    except AppointmentRuleError as exc:
        raise _rule(exc) from exc
    uow.appointments.set_effective_to(rec.id, new_end)
    after = uow.appointments.get(rec.id)
    assert after is not None
    uow.audit.write(
        AuditEntry(
            actor,
            "APPOINTMENT_ENDED",
            "role_appointment",
            str(rec.id),
            before=_appointment_payload(rec),
            after=_appointment_payload(after),
            reason=reason,
        )
    )
    return after


def revoke_appointment(
    uow: UnitOfWork, actor: Actor, appointment_id: int, reason: str | None
) -> AppointmentRecord:
    assert actor.user_id is not None
    reason = (reason or "").strip()
    if not reason:
        raise InvalidInputError("REASON_REQUIRED", "revoking an appointment needs a reason")
    uow.appointments.lock()
    rec = uow.appointments.get(appointment_id, for_update=True)
    if rec is None:
        raise NotFoundError("APPOINTMENT_NOT_FOUND", f"appointment {appointment_id} not found")
    if rec.status != "ACTIVE":
        raise ConflictError("APPOINTMENT_NOT_ACTIVE", "the appointment is already revoked")
    # D-27: revoking means "entered in error". An appointment that has authorized any
    # verification keeps its authority evidence: end it early or take it to governance.
    # (A verification in flight holds this row FOR SHARE, so it is committed and counted
    # here, or it waits and then sees REVOKED.)
    if uow.appointments.last_verification_day(rec.id) is not None:
        raise ConflictError(
            "APPOINTMENT_IN_USE",
            "this appointment has already authorized PPR verifications and cannot be revoked; "
            "end it early instead, or take it to governance review",
        )
    uow.appointments.revoke(rec.id, actor.user_id, reason)
    after = uow.appointments.get(rec.id)
    assert after is not None
    uow.audit.write(
        AuditEntry(
            actor,
            "APPOINTMENT_REVOKED",
            "role_appointment",
            str(rec.id),
            before=_appointment_payload(rec),
            after=_appointment_payload(after),
            reason=reason,
        )
    )
    return after


def current_appointment(
    uow: UnitOfWork,
    user_id: int,
    roles: frozenset[Role],
    today: date,
    cfg: FiscalYearConfig,
    *,
    lock: bool = False,
) -> AppointmentRecord | None:
    """The appointment that lets this user verify today (D-28), or None. With ``lock`` the
    row is read FOR SHARE: a concurrent end/revoke commits first and is seen here."""
    if Role.HEAD_OF_PROCUREMENT not in roles:
        return None
    a = uow.appointments.effective_for(user_id, today, lock=lock)
    if a is None or not authorizes(
        a.status, a.fiscal_year, a.effective_from, a.effective_to, today, cfg
    ):
        return None
    return a


# ------------------------------------------------------------------ verification (§14.4)
def verify(
    uow: UnitOfWork,
    caller: Caller,
    ppr_id: int,
    expected_version: int,
    today: date,
    cfg: FiscalYearConfig,
) -> PprRecord:
    rec = _load(uow, caller, ppr_id, for_update=True)
    _authorize(rec.state, PprEvent.VERIFY_PROCUREMENT, caller.roles)
    if expected_version != rec.current_version:
        raise ConflictError(
            "PPR_VERSION_CHANGED",
            f"the PPR is now version {rec.current_version}, not {expected_version}; "
            "review the current version before verifying",
        )
    appointment = current_appointment(uow, caller.user_id, caller.roles, today, cfg, lock=True)
    if appointment is None:
        raise ForbiddenError(
            "NOT_APPOINTED",
            f"you are not the appointed Head of Procurement on {today.isoformat()} "
            f"(fiscal year {fiscal_year_label(today, cfg)}) (§23, D-28)",
        )
    try:
        uow.pprs.add_verification(
            rec.id, rec.current_version, appointment.id, caller.user_id, today
        )
    except UniquenessError as exc:
        raise ConflictError("ALREADY_VERIFIED", str(exc)) from exc
    uow.pprs.set_state(rec.id, PprState.PROCUREMENT_VERIFIED)
    assigned_services.refresh_demands(  # D-38 A-3
        uow,
        uow.assigned.coverage(rec.id, confirmed=True),
        caller.actor,
        {
            "ppr_id": rec.id,
            "event": PprEvent.VERIFY_PROCUREMENT.value,
            "version": rec.current_version,
        },
    )
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_VERIFIED",
            "ppr",
            str(rec.id),
            before={"state": rec.state.value, "version": rec.current_version},
            after={
                "state": PprState.PROCUREMENT_VERIFIED.value,
                "version": rec.current_version,
                "ppr_number": rec.ppr_number,
                "appointment_id": appointment.id,
                "appointment_fiscal_year": appointment.fiscal_year,
            },
        )
    )
    after = uow.pprs.get(rec.id)
    assert after is not None
    return after


def verifications(uow: UnitOfWork, caller: Caller, ppr_id: int) -> list[VerificationRecord]:
    _load(uow, caller, ppr_id, for_update=False)
    return uow.pprs.verifications(ppr_id)


# ------------------------------------------------------------------ search (§32) and queue (§31.7)
def search(uow: UnitOfWork, caller: Caller, criteria: PprSearch) -> list[PprRecord]:
    return uow.pprs.find(criteria, caller.ppr_scope)


QUEUE_BUCKETS: dict[str, tuple[PprState, ...]] = {
    "waiting": (PprState.CONFIRMED_LOCKED,),
    "verified": (PprState.PROCUREMENT_VERIFIED,),
    "changed": (PprState.PR_CHANGED_REVIEW_REQUIRED,),
    "unlocked": (PprState.UNLOCKED_FOR_REVISION,),
    "cancelled": (PprState.CANCELLED,),
}
# Cases still open for Procurement; drafts are the requester's own work (not in the queue).
OPEN_STATES: tuple[PprState, ...] = (
    PprState.CONFIRMED_LOCKED,
    PprState.PROCUREMENT_VERIFIED,
    PprState.PR_CHANGED_REVIEW_REQUIRED,
    PprState.UNLOCKED_FOR_REVISION,
)


@dataclass(frozen=True)
class Queue:
    bucket: str
    items: list[PprRecord]
    counts: dict[str, int]
    inactive_days: int
    can_verify: bool
    appointment: AppointmentRecord | None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


def queue(
    uow: UnitOfWork,
    caller: Caller,
    bucket: str,
    criteria: PprSearch,
    now: datetime,
    today: date,
    cfg: FiscalYearConfig,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
) -> Queue:
    if bucket not in (*QUEUE_BUCKETS, "inactive"):
        raise InvalidInputError("UNKNOWN_BUCKET", f"unknown queue {bucket!r}")
    cutoff = now - timedelta(days=inactive_days)
    base = {k: v for k, v in criteria.__dict__.items() if k not in ("states", "inactive_before")}
    if bucket == "inactive":
        chosen = PprSearch(**base, states=OPEN_STATES, inactive_before=cutoff)
    else:
        chosen = PprSearch(**base, states=QUEUE_BUCKETS[bucket])
    items = uow.pprs.find(chosen, caller.ppr_scope)
    # Counts use the same filters and scope as the list, per bucket.
    counts = {
        k: uow.pprs.count(PprSearch(**base, states=v), caller.ppr_scope)
        for k, v in QUEUE_BUCKETS.items()
    }
    counts["inactive"] = uow.pprs.count(
        PprSearch(**base, states=OPEN_STATES, inactive_before=cutoff), caller.ppr_scope
    )
    appointment = current_appointment(uow, caller.user_id, caller.roles, today, cfg)
    return Queue(bucket, items, counts, inactive_days, appointment is not None, appointment, now)


# ------------------------------------------------------------------ timeline (§33)
TIMELINE_ACTIONS = {
    "PPR_DRAFT_CREATED",
    "PPR_ALLOCATION_UPDATED",
    "PPR_DRAFT_DISCARDED",
    "PPR_CONFIRMED",
    "PPR_RECONFIRMED",
    "PPR_UNLOCKED",
    "PPR_VERIFIED",
    "PPR_PRINT_VIEWED",
    # Wave 6: state changes made by synchronization (system actions).
    "PPR_PR_CHANGED",
    "PPR_CANCELLED_FROM_HOSXP",
}
# Wave 6 (D-29): what HOSxP showed, as actually observed by a sync ("observed in HOSxP"),
# shown apart from system actions. Unchanged and unsuccessful reads are not history.
TIMELINE_OBSERVATIONS = {
    "NON_CRITICAL_CHANGE",
    "CRITICAL_CHANGE",
    "IDENTITY_CHANGE",
    "INVALID_EVIDENCE",
    "CANCELLED",
    "NOT_FOUND",
    "ANOMALY",
    "NO_AUTHORITATIVE_STATUS",
}


@dataclass(frozen=True)
class TimelineEvent:
    at: datetime
    action: str
    actor_user_id: int | None
    actor_name: str | None
    version: int | None
    state_before: str | None
    state_after: str | None
    reason: str | None
    details: dict[str, Any]
    source: str = "APP"  # APP = recorded by this system; HOSXP = observed in HOSxP


@dataclass(frozen=True)
class Timeline:
    ppr: PprRecord
    events: list[TimelineEvent]
    last_activity_at: datetime
    days_inactive: int
    verifications: list[VerificationRecord]


def _version_of(r: AuditRecord) -> int | None:
    for part in (r.after, r.before):
        if part and isinstance(part.get("version"), int):
            return int(part["version"])
    return None


def _details(r: AuditRecord) -> dict[str, Any]:
    after = r.after or {}
    keep = (
        "ppr_number",
        "document_code",
        "appointment_id",
        "appointment_fiscal_year",
        "allocations",
        "adjustment_reference",
        "pr_changes",
        "required_amount",
        "outcome",
        "evidence_class",
        "evidence_issues",
        "released",
        "sync_run_id",
    )
    return {k: after[k] for k in keep if k in after}


def timeline(uow: UnitOfWork, caller: Caller, ppr_id: int, now: datetime) -> Timeline:
    rec = _load(uow, caller, ppr_id, for_update=False)
    names: dict[int, str | None] = {}

    def name(uid: int | None) -> str | None:
        if uid is None:
            return None
        if uid not in names:
            u = uow.users.get(uid)
            names[uid] = (u.display_name or u.username) if u else None
        return names[uid]

    events = [
        TimelineEvent(
            at=r.occurred_at,
            action=r.action,
            actor_user_id=r.actor_user_id,
            actor_name=name(r.actor_user_id),
            version=_version_of(r),
            state_before=(r.before or {}).get("state"),
            state_after=(r.after or {}).get("state"),
            reason=r.reason,
            details=_details(r),
        )
        for r in uow.audit.for_entity("ppr", str(rec.id))
        if r.action in TIMELINE_ACTIONS
    ]
    previous: tuple[Any, ...] | None = None
    for o in reversed(uow.sync_observations.for_ppr(rec.id, 500)):  # oldest first
        if o.outcome not in TIMELINE_OBSERVATIONS:
            continue
        # The same finding night after night is shown once, where it was first seen.
        key = (o.outcome, o.state_after, repr(o.evidence_issues), repr(o.changes), o.hosxp_status)
        if key == previous:
            continue
        previous = key
        events.append(
            TimelineEvent(
                at=o.observed_at or o.recorded_at,
                action=f"HOSXP_{o.outcome}",
                actor_user_id=None,
                actor_name=None,
                version=o.compared_version,
                state_before=o.state_before,
                state_after=o.state_after,
                reason=None,
                details={
                    k: v
                    for k, v in (
                        ("outcome", o.outcome),
                        ("evidence_class", o.evidence_class),
                        ("evidence_issues", o.evidence_issues),
                        ("pr_changes", o.changes),
                        ("hosxp_status", o.hosxp_status),
                        ("anomaly_code", o.anomaly_code),
                        ("released", o.released or None),
                        ("sync_run_id", o.sync_run_id),
                    )
                    if v is not None
                },
                source="HOSXP",
            )
        )
    events.sort(key=lambda e: e.at)
    last = rec.updated_at
    days = max(0, (now - last).days)
    return Timeline(rec, events, last, days, uow.pprs.verifications(rec.id))


def record_print(uow: UnitOfWork, caller: Caller, rec: PprRecord, version: int) -> None:
    """Opening the printable page of a version is timeline evidence (§33, §35). The system
    cannot know whether paper was actually printed, so it records the print view."""
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_PRINT_VIEWED",
            "ppr",
            str(rec.id),
            after={"version": version, "state": rec.state.value, "ppr_number": rec.ppr_number},
        )
    )


def users_named(uow: UnitOfWork, ids: Sequence[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for uid in set(ids):
        u = uow.users.get(uid)
        if u is not None:
            out[uid] = u.display_name or u.username
    return out
