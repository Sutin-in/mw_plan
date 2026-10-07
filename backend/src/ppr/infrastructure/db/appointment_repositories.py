"""SQL implementation of Head of Procurement appointments (§23, D-27; PostgreSQL)."""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import Connection, func, insert, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from ppr.infrastructure.db.schema import ppr_verification, role_appointment
from ppr.ports import AppointmentRecord, UniquenessError

HEAD = "HEAD_OF_PROCUREMENT"


class SqlAppointmentRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    @staticmethod
    def _record(r: Any) -> AppointmentRecord:
        return AppointmentRecord(
            id=r.id,
            user_id=r.user_id,
            fiscal_year=r.fiscal_year,
            effective_from=r.effective_from,
            effective_to=r.effective_to,
            status=r.status,
            created_by_user_id=r.created_by_user_id,
            created_at=r.created_at,
            revoked_at=r.revoked_at,
            revoked_by_user_id=r.revoked_by_user_id,
            revoke_reason=r.revoke_reason,
        )

    def lock(self) -> None:
        # Writers take this lock; readers are not blocked.
        self._c.execute(text("LOCK TABLE role_appointment IN SHARE ROW EXCLUSIVE MODE"))

    def list(self, fiscal_year: int | None = None) -> list[AppointmentRecord]:
        q = select(role_appointment).order_by(
            role_appointment.c.effective_from.desc(), role_appointment.c.id.desc()
        )
        if fiscal_year is not None:
            q = q.where(role_appointment.c.fiscal_year == fiscal_year)
        return [self._record(r) for r in self._c.execute(q)]

    def get(self, appointment_id: int, *, for_update: bool = False) -> AppointmentRecord | None:
        q = select(role_appointment).where(role_appointment.c.id == appointment_id)
        if for_update:
            q = q.with_for_update()
        row = self._c.execute(q).first()
        return self._record(row) if row else None

    def create(
        self,
        user_id: int,
        fiscal_year: int,
        effective_from: date,
        effective_to: date,
        created_by: int,
    ) -> int:
        try:
            with self._c.begin_nested():
                return int(
                    self._c.execute(
                        insert(role_appointment)
                        .values(
                            user_id=user_id,
                            role_code=HEAD,
                            fiscal_year=fiscal_year,
                            effective_from=effective_from,
                            effective_to=effective_to,
                            status="ACTIVE",
                            created_by_user_id=created_by,
                        )
                        .returning(role_appointment.c.id)
                    ).scalar_one()
                )
        except IntegrityError as exc:
            raise UniquenessError("the dates overlap another active appointment") from exc

    def set_effective_to(self, appointment_id: int, effective_to: date) -> None:
        self._c.execute(
            update(role_appointment)
            .where(role_appointment.c.id == appointment_id)
            .values(effective_to=effective_to)
        )

    def revoke(self, appointment_id: int, revoked_by: int, reason: str) -> None:
        self._c.execute(
            update(role_appointment)
            .where(role_appointment.c.id == appointment_id)
            .values(
                status="REVOKED",
                revoked_at=func.now(),
                revoked_by_user_id=revoked_by,
                revoke_reason=reason,
            )
        )

    def effective_for(
        self, user_id: int, day: date, *, lock: bool = False
    ) -> AppointmentRecord | None:
        q = select(role_appointment).where(
            role_appointment.c.user_id == user_id,
            role_appointment.c.status == "ACTIVE",
            role_appointment.c.effective_from <= day,
            or_(
                role_appointment.c.effective_to.is_(None),
                role_appointment.c.effective_to >= day,
            ),
        )
        if lock:
            q = q.with_for_update(read=True)
        row = self._c.execute(q).first()
        return self._record(row) if row else None

    def verifications_between(self, start: date, end: date) -> int:
        return int(
            self._c.execute(
                select(func.count()).where(
                    ppr_verification.c.verified_on >= start,
                    ppr_verification.c.verified_on <= end,
                )
            ).scalar_one()
        )

    def last_verification_day(self, appointment_id: int) -> date | None:
        day: date | None = self._c.execute(
            select(func.max(ppr_verification.c.verified_on)).where(
                ppr_verification.c.appointment_id == appointment_id
            )
        ).scalar()
        return day
