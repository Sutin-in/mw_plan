"""FastAPI application (composition root).

Authorization is enforced HERE, server-side, on every protected route (spec §22.9):
the caller's roles are loaded from the database per request and checked against
``ppr.domain.permissions``. Hiding UI elements is never relied upon.

Responses: 401 = not authenticated (missing/invalid/expired token, unknown or
disabled user); 403 = authenticated but not permitted.
"""

# NOTE: no `from __future__ import annotations` here - FastAPI must evaluate the
# Annotated[..., Depends(local_function)] hints at definition time.

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from time import monotonic
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from ppr.api import (
    amendment_routes,
    dashboard_routes,
    plan_import_routes,
    plan_routes,
    ppr_routes,
    pr_routes,
    procurement_routes,
    report_routes,
    sync_routes,
)
from ppr.application import services
from ppr.application.sync_services import SyncSettings
from ppr.auth.session import InvalidTokenError, SessionSigner
from ppr.domain.fiscal_year import FiscalYearConfig, PlanYearState, fiscal_year_label
from ppr.domain.permissions import Permission, is_allowed
from ppr.domain.roles import Role
from ppr.infrastructure.db.mockdata import mock_data_summary
from ppr.infrastructure.db.repositories import unit_of_work
from ppr.integration.hosxp.auth import HosxpAuthenticationAdapter
from ppr.integration.hosxp.errors import HosxpUnavailableError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.ports import Actor, PlanYearRecord, UserRecord


@dataclass(frozen=True)
class AppDeps:
    engine: Engine
    auth_adapter: HosxpAuthenticationAdapter
    signer: SessionSigner
    fiscal_year_config: FiscalYearConfig
    gateway: HosxpGateway
    # Server date used for fiscal-year rules (D-18); injectable for tests.
    today: Callable[[], date] = field(default=date.today)
    # Demonstration mode (D-26): shown on every page by the user interface.
    demo: bool = False
    # Production profile (Wave 10A): MOCK/DEMO data found in the database is announced on
    # every page (D-40).
    production: bool = False
    # PR synchronization tuning (Wave 6, D-29).
    sync_settings: SyncSettings = field(default_factory=SyncSettings)


# ------------------------------------------------------------------ schemas
class LoginIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginOut(BaseModel):
    token: str
    token_type: str = "Bearer"


class MeOut(BaseModel):
    id: int
    username: str
    display_name: str | None
    department_source_id: str | None
    roles: list[str]


class InfoOut(BaseModel):
    demo: bool
    current_fiscal_year: int
    today: date
    # D-40: production database holding MOCK/DEMO data (Thai text, shown on every page)
    mock_data_warning: str | None = None


class PlanYearOut(BaseModel):
    fiscal_year: int
    state: str
    starts_on: date
    ends_on: date


class PlanYearIn(BaseModel):
    fiscal_year: int


class TransitionIn(BaseModel):
    target: PlanYearState
    reason: str | None = None


class AuditOut(BaseModel):
    id: int
    occurred_at: str
    actor_kind: str
    actor_user_id: int | None
    action: str
    entity_type: str
    entity_id: str
    before: dict[str, object] | None
    after: dict[str, object] | None
    reason: str | None


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _plan_year_out(r: PlanYearRecord) -> PlanYearOut:
    return PlanYearOut(
        fiscal_year=r.fiscal_year, state=r.state.value, starts_on=r.starts_on, ends_on=r.ends_on
    )


def create_app(deps: AppDeps) -> FastAPI:
    app = FastAPI(title="PPR Control - Phase 1", version="0.1.0")

    # -------------------------------------------------------------- auth dependencies
    def current_user(
        authorization: Annotated[str | None, Header()] = None,
    ) -> UserRecord:
        if not authorization or not authorization.startswith("Bearer "):
            raise _error(status.HTTP_401_UNAUTHORIZED, "NOT_AUTHENTICATED", "missing token")
        try:
            claims = deps.signer.verify(authorization.removeprefix("Bearer ").strip())
        except InvalidTokenError as exc:
            raise _error(status.HTTP_401_UNAUTHORIZED, "NOT_AUTHENTICATED", str(exc)) from exc
        with unit_of_work(deps.engine) as uow:
            user = uow.users.get(claims.user_id)
        if user is None or not user.active:
            raise _error(status.HTTP_401_UNAUTHORIZED, "NOT_AUTHENTICATED", "unknown user")
        return user

    def require(permission: Permission):  # type: ignore[no-untyped-def]
        def checker(user: Annotated[UserRecord, Depends(current_user)]) -> UserRecord:
            if not is_allowed(user.roles, permission):
                raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", f"requires {permission.value}")
            return user

        return checker

    # -------------------------------------------------------------- public
    @app.get("/api/health")
    def health() -> JSONResponse:
        """For monitoring and the restart procedure: 200 only when the database answers.
        Names no account, version or record."""
        try:
            with deps.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    mock_cache: dict[str, Any] = {}

    def mock_warning() -> str | None:
        """D-40, production only; counted at most once a minute (every page asks)."""
        if not deps.production:
            return None
        now = monotonic()
        if now - mock_cache.get("at", -1e9) > 60:
            try:
                with deps.engine.connect() as conn:
                    found = mock_data_summary(conn)
                mock_cache.update(at=now, text=found.thai() if found.found else None)
            except SQLAlchemyError:
                # Keep the last answer and wait a minute before trying again: a database
                # that is down must not be retried on every page (health reports it).
                mock_cache["at"] = now
        return mock_cache.get("text")

    @app.get("/api/info", response_model=InfoOut)
    def info() -> InfoOut:
        today = deps.today()
        return InfoOut(
            demo=deps.demo,
            current_fiscal_year=fiscal_year_label(today, deps.fiscal_year_config),
            today=today,
            mock_data_warning=mock_warning(),
        )

    @app.post("/api/auth/login", response_model=LoginOut)
    def login(body: LoginIn) -> LoginOut:
        try:
            with unit_of_work(deps.engine) as uow:
                user = services.login(uow, deps.auth_adapter, body.username, body.password)
        except services.AuthenticationFailedError as exc:
            raise _error(status.HTTP_401_UNAUTHORIZED, exc.code, str(exc)) from exc
        except HosxpUnavailableError as exc:
            raise _error(
                status.HTTP_503_SERVICE_UNAVAILABLE, "HOSXP_UNAVAILABLE", "HOSxP is unavailable"
            ) from exc
        return LoginOut(token=deps.signer.issue(user.id))

    # -------------------------------------------------------------- authenticated
    @app.get("/api/me", response_model=MeOut)
    def me(user: Annotated[UserRecord, Depends(current_user)]) -> MeOut:
        return MeOut(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            department_source_id=user.department_source_id,
            roles=sorted(r.value for r in user.roles),
        )

    @app.get("/api/plan-years", response_model=list[PlanYearOut])
    def list_plan_years(
        _: Annotated[UserRecord, Depends(require(Permission.PLAN_YEAR_READ))],
    ) -> list[PlanYearOut]:
        with unit_of_work(deps.engine) as uow:
            return [_plan_year_out(r) for r in uow.plan_years.list()]

    @app.post("/api/plan-years", response_model=PlanYearOut, status_code=201)
    def create_plan_year(
        body: PlanYearIn,
        user: Annotated[UserRecord, Depends(require(Permission.PLAN_YEAR_MANAGE))],
    ) -> PlanYearOut:
        try:
            with unit_of_work(deps.engine) as uow:
                rec = services.create_plan_year(
                    uow, Actor(user.id), body.fiscal_year, deps.fiscal_year_config
                )
        except services.ConflictError as exc:
            raise _error(status.HTTP_409_CONFLICT, exc.code, str(exc)) from exc
        return _plan_year_out(rec)

    @app.post("/api/plan-years/{fiscal_year}/transition", response_model=PlanYearOut)
    def transition_plan_year(
        fiscal_year: int,
        body: TransitionIn,
        user: Annotated[UserRecord, Depends(require(Permission.PLAN_YEAR_MANAGE))],
    ) -> PlanYearOut:
        try:
            with unit_of_work(deps.engine) as uow:
                rec = services.transition_plan_year(
                    uow, Actor(user.id), fiscal_year, body.target, body.reason
                )
        except services.NotFoundError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, exc.code, str(exc)) from exc
        except services.ConflictError as exc:
            detail: dict[str, object] = {"code": exc.code, "message": str(exc)}
            if exc.details is not None:
                detail["details"] = exc.details
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail) from exc
        return _plan_year_out(rec)

    @app.get("/api/audit-log", response_model=list[AuditOut])
    def audit_log(
        _: Annotated[UserRecord, Depends(require(Permission.AUDIT_READ))],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> list[AuditOut]:
        with unit_of_work(deps.engine) as uow:
            rows = uow.audit.recent(limit)
        return [
            AuditOut(
                id=r.id,
                occurred_at=r.occurred_at.isoformat(),
                actor_kind=r.actor_kind,
                actor_user_id=r.actor_user_id,
                action=r.action,
                entity_type=r.entity_type,
                entity_id=r.entity_id,
                before=r.before,
                after=r.after,
                reason=r.reason,
            )
            for r in rows
        ]

    @app.put("/api/admin/users/{user_id}/roles/{role}", status_code=204)
    def grant_role(
        user_id: int,
        role: Role,
        admin: Annotated[UserRecord, Depends(require(Permission.USER_ROLE_MANAGE))],
    ) -> None:
        try:
            with unit_of_work(deps.engine) as uow:
                services.grant_role(uow, Actor(admin.id), user_id, role)
        except services.NotFoundError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, exc.code, str(exc)) from exc

    @app.delete("/api/admin/users/{user_id}/roles/{role}", status_code=204)
    def revoke_role(
        user_id: int,
        role: Role,
        admin: Annotated[UserRecord, Depends(require(Permission.USER_ROLE_MANAGE))],
    ) -> None:
        try:
            with unit_of_work(deps.engine) as uow:
                services.revoke_role(uow, Actor(admin.id), user_id, role)
        except services.NotFoundError as exc:
            raise _error(status.HTTP_404_NOT_FOUND, exc.code, str(exc)) from exc

    plan_routes.register(app, deps, current_user, require)
    pr_routes.register(app, deps, require)
    procurement_routes.register(app, deps, require)
    ppr_routes.register(app, deps, require)
    sync_routes.register(app, deps, require)
    dashboard_routes.register(app, deps, require)
    report_routes.register(app, deps, require)
    amendment_routes.register(app, deps, require)
    plan_import_routes.register(app, deps, require)
    return app
