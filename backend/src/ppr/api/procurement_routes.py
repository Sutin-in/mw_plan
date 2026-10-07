"""Wave 5B routes: appointments, verification, procurement queue, search, timeline.

Permissions (server-side, spec §22.9):
* ``GET  /api/ppr-search``               PPR_READ, same department scope as reading one PPR
* ``GET  /api/pprs/{id}/timeline``       PPR_READ, same scope (other departments: 404);
                                         since Wave 6 it includes "observed in HOSxP" entries
* ``GET  /api/procurement/queue``        PROCUREMENT_QUEUE_READ
* ``POST /api/pprs/{id}/verify``         PPR_VERIFY + the caller's current appointment (D-28)
* ``GET  /api/appointments``             APPOINTMENT_READ
* ``POST /api/appointments``, ``/{id}/end``, ``/{id}/revoke``   APPOINTMENT_MANAGE (Admin)
"""

# NOTE: no `from __future__ import annotations` - FastAPI evaluates the Annotated hints.

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field

from ppr.api.ppr_routes import PprOut, _caller, _http, _out
from ppr.application import dashboard_services
from ppr.application import procurement_services as psvc
from ppr.application.errors import ServiceError
from ppr.domain.appointment import authorizes
from ppr.domain.permissions import Permission
from ppr.domain.ppr_state import PprState
from ppr.domain.roles import Role
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.ports import Actor, AppointmentRecord, PprSearch, UserRecord


class AppointmentIn(BaseModel):
    user_id: int
    fiscal_year: int = Field(ge=1000, le=9999)
    effective_from: date
    effective_to: date | None = None


class AppointmentEndIn(BaseModel):
    effective_to: date
    reason: str | None = None


class ReasonIn(BaseModel):
    reason: str | None = None


class VerifyIn(BaseModel):
    version: int = Field(ge=1)


class AppointmentOut(BaseModel):
    id: int
    user_id: int
    user_name: str | None
    user_has_role: bool
    fiscal_year: int
    effective_from: date
    effective_to: date | None
    status: str
    effective_today: bool
    # Last server date this appointment authorized a verification (None = never used);
    # a used appointment cannot be revoked (D-27).
    last_verification_on: date | None
    created_by_user_id: int | None
    created_at: datetime
    revoked_at: datetime | None
    revoke_reason: str | None


class CandidateOut(BaseModel):
    id: int
    username: str
    display_name: str | None


class QueueOut(BaseModel):
    bucket: str
    counts: dict[str, int]
    inactive_days: int
    can_verify: bool
    appointment: AppointmentOut | None
    items: list[PprOut]


class VerificationOut(BaseModel):
    version: int
    appointment_id: int
    verified_by_user_id: int
    verified_by_name: str | None
    verified_at: datetime


class TimelineEventOut(BaseModel):
    at: datetime
    action: str
    actor_user_id: int | None
    actor_name: str | None
    version: int | None
    state_before: str | None
    state_after: str | None
    reason: str | None
    details: dict[str, Any]
    # APP = recorded by this system; HOSXP = observed in HOSxP by a synchronization (D-29)
    source: str


class TimelineOut(BaseModel):
    ppr_id: int
    state: str
    version: int
    last_activity_at: datetime
    days_inactive: int
    events: list[TimelineEventOut]
    verifications: list[VerificationOut]
    # Whether THIS caller may verify this PPR right now (state + role + appointment, D-28).
    can_verify: bool


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time.min).astimezone()


def register(
    app: FastAPI,
    deps: Any,
    require: Callable[[Permission], Callable[..., UserRecord]],
) -> None:
    Reader = Annotated[UserRecord, Depends(require(Permission.PPR_READ))]  # noqa: N806
    QueueUser = Annotated[UserRecord, Depends(require(Permission.PROCUREMENT_QUEUE_READ))]  # noqa: N806
    Verifier = Annotated[UserRecord, Depends(require(Permission.PPR_VERIFY))]  # noqa: N806
    ApptReader = Annotated[UserRecord, Depends(require(Permission.APPOINTMENT_READ))]  # noqa: N806
    ApptAdmin = Annotated[UserRecord, Depends(require(Permission.APPOINTMENT_MANAGE))]  # noqa: N806
    cfg = deps.fiscal_year_config

    def run(fn: Callable[..., Any], *args: Any) -> Any:
        try:
            with unit_of_work(deps.engine) as uow:
                return fn(uow, *args)
        except ServiceError as exc:
            raise _http(exc) from exc

    def appt_out(uow: Any, a: AppointmentRecord) -> AppointmentOut:
        u = uow.users.get(a.user_id)
        today = deps.today()
        return AppointmentOut(
            id=a.id,
            user_id=a.user_id,
            user_name=(u.display_name or u.username) if u else None,
            user_has_role=bool(u and Role.HEAD_OF_PROCUREMENT in u.roles),
            fiscal_year=a.fiscal_year,
            effective_from=a.effective_from,
            effective_to=a.effective_to,
            status=a.status,
            last_verification_on=uow.appointments.last_verification_day(a.id),
            effective_today=authorizes(
                a.status, a.fiscal_year, a.effective_from, a.effective_to, today, cfg
            ),
            created_by_user_id=a.created_by_user_id,
            created_at=a.created_at,
            revoked_at=a.revoked_at,
            revoke_reason=a.revoke_reason,
        )

    def criteria(
        pr_no: str | None = None,
        ppr_number: str | None = None,
        requester: str | None = None,
        department_id: str | None = None,
        item: str | None = None,
        budget_category_id: str | None = None,
        fund_source_id: str | None = None,
        fiscal_year: int | None = None,
        state: Annotated[list[PprState] | None, Query()] = None,
        created_from: date | None = None,
        created_to: date | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> PprSearch:
        return PprSearch(
            pr_no=pr_no,
            ppr_number=ppr_number,
            requester=requester,
            department_id=department_id or None,
            item=item,
            budget_category_id=budget_category_id or None,
            fund_source_id=fund_source_id or None,
            fiscal_year=fiscal_year,
            states=tuple(state or ()),
            # Whole server-local days, independent of the database time zone.
            created_from=_day_start(created_from) if created_from else None,
            created_to=_day_start(created_to + timedelta(days=1)) if created_to else None,
            limit=limit,
        )

    SearchArgs = Annotated[PprSearch, Depends(criteria)]  # noqa: N806

    # ------------------------------------------------------------------ search (§32)
    @app.get("/api/ppr-search", response_model=list[PprOut])
    def search_pprs(user: Reader, args: SearchArgs) -> list[PprOut]:
        return [_out(r) for r in run(psvc.search, _caller(user), args)]

    # ------------------------------------------------------------------ timeline (§33)
    @app.get("/api/pprs/{ppr_id}/timeline", response_model=TimelineOut)
    def ppr_timeline(ppr_id: int, user: Reader) -> TimelineOut:
        def fn(uow: Any, caller: Any) -> TimelineOut:
            t = psvc.timeline(uow, caller, ppr_id, datetime.now(UTC))
            names = psvc.users_named(uow, [v.verified_by_user_id for v in t.verifications])
            can_verify = t.ppr.state is PprState.CONFIRMED_LOCKED and (
                psvc.current_appointment(uow, caller.user_id, caller.roles, deps.today(), cfg)
                is not None
            )
            return TimelineOut(
                ppr_id=t.ppr.id,
                state=t.ppr.state.value,
                version=t.ppr.current_version,
                last_activity_at=t.last_activity_at,
                days_inactive=t.days_inactive,
                events=[TimelineEventOut(**e.__dict__) for e in t.events],
                verifications=[
                    VerificationOut(
                        version=v.version,
                        appointment_id=v.appointment_id,
                        verified_by_user_id=v.verified_by_user_id,
                        verified_by_name=names.get(v.verified_by_user_id),
                        verified_at=v.verified_at,
                    )
                    for v in t.verifications
                ],
                can_verify=can_verify,
            )

        return run(fn, _caller(user))  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ queue (§31.7)
    @app.get("/api/procurement/queue", response_model=QueueOut)
    def procurement_queue(
        user: QueueUser,
        args: SearchArgs,
        bucket: Annotated[str, Query()] = "waiting",
    ) -> QueueOut:
        def fn(uow: Any, caller: Any) -> QueueOut:
            # D-33: the same stored inactivity threshold as the alerts.
            days = dashboard_services.inactive_days(uow)
            q = psvc.queue(uow, caller, bucket, args, datetime.now(UTC), deps.today(), cfg, days)
            return QueueOut(
                bucket=q.bucket,
                counts=q.counts,
                inactive_days=q.inactive_days,
                can_verify=q.can_verify,
                appointment=appt_out(uow, q.appointment) if q.appointment else None,
                items=[_out(r) for r in q.items],
            )

        return run(fn, _caller(user))  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ verification (§14.4)
    @app.post("/api/pprs/{ppr_id}/verify", response_model=PprOut)
    def verify_ppr(ppr_id: int, body: VerifyIn, user: Verifier) -> PprOut:
        rec = run(psvc.verify, _caller(user), ppr_id, body.version, deps.today(), cfg)
        return _out(rec)

    # ------------------------------------------------------------------ appointments (§23)
    @app.get("/api/appointments", response_model=list[AppointmentOut])
    def list_appointments(
        _: ApptReader, fiscal_year: Annotated[int | None, Query()] = None
    ) -> list[AppointmentOut]:
        def fn(uow: Any) -> list[AppointmentOut]:
            return [appt_out(uow, a) for a in psvc.list_appointments(uow, fiscal_year)]

        return run(fn)  # type: ignore[no-any-return]

    @app.post("/api/appointments", response_model=AppointmentOut, status_code=201)
    def create_appointment(body: AppointmentIn, admin: ApptAdmin) -> AppointmentOut:
        def fn(uow: Any) -> AppointmentOut:
            a = psvc.create_appointment(
                uow,
                Actor(admin.id),
                body.user_id,
                body.fiscal_year,
                body.effective_from,
                body.effective_to,
                cfg,
            )
            return appt_out(uow, a)

        return run(fn)  # type: ignore[no-any-return]

    @app.post("/api/appointments/{appointment_id}/end", response_model=AppointmentOut)
    def end_appointment(
        appointment_id: int, body: AppointmentEndIn, admin: ApptAdmin
    ) -> AppointmentOut:
        def fn(uow: Any) -> AppointmentOut:
            a = psvc.end_appointment(
                uow,
                Actor(admin.id),
                appointment_id,
                body.effective_to,
                body.reason,
                deps.today(),
            )
            return appt_out(uow, a)

        return run(fn)  # type: ignore[no-any-return]

    @app.post("/api/appointments/{appointment_id}/revoke", response_model=AppointmentOut)
    def revoke_appointment(appointment_id: int, body: ReasonIn, admin: ApptAdmin) -> AppointmentOut:
        def fn(uow: Any) -> AppointmentOut:
            return appt_out(
                uow, psvc.revoke_appointment(uow, Actor(admin.id), appointment_id, body.reason)
            )

        return run(fn)  # type: ignore[no-any-return]

    @app.get("/api/appointments/candidates", response_model=list[CandidateOut])
    def appointment_candidates(_: ApptAdmin) -> list[CandidateOut]:
        """Users holding the HEAD_OF_PROCUREMENT role (the role is required to verify)."""

        def fn(uow: Any) -> list[CandidateOut]:
            return [
                CandidateOut(id=u.id, username=u.username, display_name=u.display_name)
                for u in uow.users.with_role(Role.HEAD_OF_PROCUREMENT)
                if u.active
            ]

        return run(fn)  # type: ignore[no-any-return]
