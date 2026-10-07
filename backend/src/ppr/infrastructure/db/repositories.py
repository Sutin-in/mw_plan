"""SQLAlchemy Core implementations of the application ports (``ppr.ports``)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, Engine, delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.roles import Role
from ppr.infrastructure.db.schema import app_user, audit_log, plan_year, user_role
from ppr.ports import AuditEntry, AuditRecord, PlanYearRecord, UserRecord


class SqlAuditWriter:
    """Insert-only. There is deliberately no update/delete method (and the DB forbids it)."""

    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def write(self, entry: AuditEntry) -> None:
        self._c.execute(
            insert(audit_log).values(
                actor_kind=entry.actor.kind,
                actor_user_id=entry.actor.user_id,
                action=entry.action,
                entity_type=entry.entity_type,
                entity_id=entry.entity_id,
                before=entry.before,
                after=entry.after,
                reason=entry.reason,
            )
        )

    def recent(self, limit: int) -> list[AuditRecord]:
        rows = self._c.execute(
            select(audit_log).order_by(audit_log.c.id.desc()).limit(limit)
        ).mappings()
        return [AuditRecord(**dict(r)) for r in rows]

    def search(
        self,
        since: datetime | None,
        until: datetime | None,
        action: str | None,
        entity_type: str | None,
        limit: int,
    ) -> list[AuditRecord]:
        q = select(audit_log)
        if since is not None:
            q = q.where(audit_log.c.occurred_at >= since)
        if until is not None:
            q = q.where(audit_log.c.occurred_at < until)
        if action:
            q = q.where(audit_log.c.action == action)
        if entity_type:
            q = q.where(audit_log.c.entity_type == entity_type)
        rows = self._c.execute(q.order_by(audit_log.c.id.desc()).limit(limit)).mappings()
        return [AuditRecord(**dict(r)) for r in rows]

    def for_entity(self, entity_type: str, entity_id: str) -> list[AuditRecord]:
        rows = self._c.execute(
            select(audit_log)
            .where(audit_log.c.entity_type == entity_type, audit_log.c.entity_id == entity_id)
            .order_by(audit_log.c.id)
        ).mappings()
        return [AuditRecord(**dict(r)) for r in rows]


class SqlUserRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    def _roles(self, user_id: int) -> frozenset[Role]:
        rows: list[str] = list(
            self._c.execute(
                select(user_role.c.role_code).where(user_role.c.user_id == user_id)
            ).scalars()
        )
        return frozenset(Role(r) for r in rows)

    def _record(self, row: Any) -> UserRecord:
        return UserRecord(
            id=row.id,
            hosxp_source_id=row.hosxp_source_id,
            username=row.username,
            display_name=row.display_name,
            department_source_id=row.department_source_id,
            active=row.active,
            roles=self._roles(row.id),
        )

    def upsert_from_login(
        self,
        hosxp_source_id: str,
        username: str,
        display_name: str | None,
        department_source_id: str | None,
    ) -> UserRecord:
        base = pg_insert(app_user).values(
            hosxp_source_id=hosxp_source_id,
            username=username,
            display_name=display_name,
            department_source_id=department_source_id,
            last_login_at=func.now(),
        )
        stmt = base.on_conflict_do_update(
            index_elements=[app_user.c.hosxp_source_id],
            set_={
                "username": base.excluded.username,
                "display_name": base.excluded.display_name,
                "department_source_id": base.excluded.department_source_id,
                "last_login_at": func.now(),
            },
        ).returning(app_user)
        return self._record(self._c.execute(stmt).one())

    def get(self, user_id: int) -> UserRecord | None:
        row = self._c.execute(select(app_user).where(app_user.c.id == user_id)).one_or_none()
        return self._record(row) if row else None

    def get_by_username(self, username: str) -> UserRecord | None:
        row = self._c.execute(select(app_user).where(app_user.c.username == username)).one_or_none()
        return self._record(row) if row else None

    def with_role(self, role: Role) -> list[UserRecord]:
        rows = self._c.execute(
            select(app_user)
            .join(user_role, user_role.c.user_id == app_user.c.id)
            .where(user_role.c.role_code == role.value)
            .order_by(app_user.c.username)
        )
        return [self._record(r) for r in rows]

    # rowcount is driver-dependent (psycopg may report -1), so use RETURNING.
    def grant_role(self, user_id: int, role: Role, granted_by: int | None) -> bool:
        inserted = self._c.execute(
            pg_insert(user_role)
            .values(user_id=user_id, role_code=role.value, granted_by_user_id=granted_by)
            .on_conflict_do_nothing()
            .returning(user_role.c.user_id)
        ).first()
        return inserted is not None

    def revoke_role(self, user_id: int, role: Role) -> bool:
        deleted = self._c.execute(
            delete(user_role)
            .where(user_role.c.user_id == user_id, user_role.c.role_code == role.value)
            .returning(user_role.c.user_id)
        ).first()
        return deleted is not None


class SqlPlanYearRepository:
    def __init__(self, conn: Connection) -> None:
        self._c = conn

    @staticmethod
    def _record(row: Any) -> PlanYearRecord:
        return PlanYearRecord(row.fiscal_year, PlanYearState(row.state), row.starts_on, row.ends_on)

    def list(self) -> list[PlanYearRecord]:
        rows = self._c.execute(select(plan_year).order_by(plan_year.c.fiscal_year.desc()))
        return [self._record(r) for r in rows]

    def get(self, fiscal_year: int, *, for_update: bool = False) -> PlanYearRecord | None:
        stmt = select(plan_year).where(plan_year.c.fiscal_year == fiscal_year)
        if for_update:
            stmt = stmt.with_for_update()
        row = self._c.execute(stmt).one_or_none()
        return self._record(row) if row else None

    def create(self, record: PlanYearRecord, created_by: int | None) -> None:
        self._c.execute(
            insert(plan_year).values(
                fiscal_year=record.fiscal_year,
                state=record.state.value,
                starts_on=record.starts_on,
                ends_on=record.ends_on,
                created_by_user_id=created_by,
            )
        )

    def set_state(self, fiscal_year: int, state: PlanYearState) -> None:
        self._c.execute(
            update(plan_year)
            .where(plan_year.c.fiscal_year == fiscal_year)
            .values(state=state.value, updated_at=func.now())
        )


class SqlUnitOfWork:
    def __init__(self, conn: Connection) -> None:
        from ppr.infrastructure.db.alert_repositories import SqlAlertSettingRepository
        from ppr.infrastructure.db.amendment_repositories import SqlAmendmentRepository
        from ppr.infrastructure.db.appointment_repositories import SqlAppointmentRepository
        from ppr.infrastructure.db.assigned_repositories import SqlAssignedRepository
        from ppr.infrastructure.db.plan_repositories import (
            SqlLedgerRepository,
            SqlMasterRepository,
            SqlPlanImportRepository,
            SqlPlanRepository,
            SqlSyncRunRepository,
        )
        from ppr.infrastructure.db.ppr_repositories import SqlPprRepository
        from ppr.infrastructure.db.sync_repositories import SqlSyncObservationRepository

        self.connection = conn
        self.audit = SqlAuditWriter(conn)
        self.users = SqlUserRepository(conn)
        self.plan_years = SqlPlanYearRepository(conn)
        self.masters = SqlMasterRepository(conn)
        self.sync_runs = SqlSyncRunRepository(conn)
        self.plans = SqlPlanRepository(conn)
        self.ledger = SqlLedgerRepository(conn)
        self.pprs = SqlPprRepository(conn)
        self.appointments = SqlAppointmentRepository(conn)
        self.sync_observations = SqlSyncObservationRepository(conn)
        self.alert_settings = SqlAlertSettingRepository(conn)
        self.amendments = SqlAmendmentRepository(conn)
        self.assigned = SqlAssignedRepository(conn)
        self.plan_imports = SqlPlanImportRepository(conn)


@contextmanager
def unit_of_work(engine: Engine) -> Iterator[SqlUnitOfWork]:
    """Commit on success, roll back on any exception."""
    with engine.begin() as conn:
        yield SqlUnitOfWork(conn)
