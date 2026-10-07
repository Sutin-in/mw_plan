"""Wave 1B use cases. Each runs inside one UnitOfWork, so the change and its audit
row commit together or not at all (spec §36, §38.1)."""

from __future__ import annotations

from ppr.application.errors import (
    AuthenticationFailedError,
    ConflictError,
    InvalidInputError,
    NotFoundError,
    ServiceError,
    UpstreamUnavailableError,
)
from ppr.application.plan_services import activate_plan
from ppr.domain.fiscal_year import (
    FiscalYearConfig,
    PlanYearState,
    can_transition_plan_year,
    fiscal_year_bounds,
)
from ppr.domain.roles import Role
from ppr.integration.hosxp.auth import AuthResult, HosxpAuthenticationAdapter
from ppr.ports import Actor, AuditEntry, PlanYearRecord, UnitOfWork, UserRecord

__all__ = [
    "AuthenticationFailedError",
    "ConflictError",
    "InvalidInputError",
    "NotFoundError",
    "ServiceError",
    "UpstreamUnavailableError",
]


# ------------------------------------------------------------------ login
def login(
    uow: UnitOfWork, adapter: HosxpAuthenticationAdapter, username: str, password: str
) -> UserRecord:
    """Verify credentials with HOSxP (via the adapter) and map to a local user.

    The password is passed straight to the adapter and never stored or logged.
    """
    result: AuthResult = adapter.authenticate(username, password)
    if not result.ok or result.user_source_id is None:
        raise AuthenticationFailedError("AUTH_FAILED", "invalid credentials or inactive account")
    profile = adapter.get_user_profile(result.user_source_id)
    if profile is None or not profile.active:
        raise AuthenticationFailedError("AUTH_FAILED", "invalid credentials or inactive account")
    user = uow.users.upsert_from_login(
        profile.source_id, profile.username, profile.display_name, profile.department_id
    )
    if not user.active:
        raise AuthenticationFailedError("AUTH_FAILED", "local account is disabled")
    return user


# ------------------------------------------------------------------ roles (§22.8, §36)
def grant_role(uow: UnitOfWork, actor: Actor, user_id: int, role: Role) -> None:
    target = uow.users.get(user_id)
    if target is None:
        raise NotFoundError("USER_NOT_FOUND", f"user {user_id} not found")
    if uow.users.grant_role(user_id, role, actor.user_id):
        uow.audit.write(
            AuditEntry(
                actor,
                "ROLE_GRANTED",
                "app_user",
                str(user_id),
                before={"roles": sorted(r.value for r in target.roles)},
                after={"roles": sorted({*(r.value for r in target.roles), role.value})},
            )
        )


def revoke_role(uow: UnitOfWork, actor: Actor, user_id: int, role: Role) -> None:
    target = uow.users.get(user_id)
    if target is None:
        raise NotFoundError("USER_NOT_FOUND", f"user {user_id} not found")
    if uow.users.revoke_role(user_id, role):
        uow.audit.write(
            AuditEntry(
                actor,
                "ROLE_REVOKED",
                "app_user",
                str(user_id),
                before={"roles": sorted(r.value for r in target.roles)},
                after={"roles": sorted(r.value for r in target.roles if r is not role)},
            )
        )


# ------------------------------------------------------------------ plan year (§7)
def create_plan_year(
    uow: UnitOfWork, actor: Actor, fiscal_year: int, cfg: FiscalYearConfig
) -> PlanYearRecord:
    if not 1000 <= fiscal_year <= 9999:
        raise ConflictError("INVALID_FISCAL_YEAR", "fiscal year must be 4 digits")
    if uow.plan_years.get(fiscal_year) is not None:
        raise ConflictError("PLAN_YEAR_EXISTS", f"plan year {fiscal_year} already exists")
    starts_on, ends_on = fiscal_year_bounds(fiscal_year, cfg)
    rec = PlanYearRecord(fiscal_year, PlanYearState.DRAFT, starts_on, ends_on)
    uow.plan_years.create(rec, actor.user_id)
    uow.audit.write(
        AuditEntry(
            actor,
            "PLAN_YEAR_CREATED",
            "plan_year",
            str(fiscal_year),
            after={
                "state": rec.state.value,
                "starts_on": starts_on.isoformat(),
                "ends_on": ends_on.isoformat(),
            },
        )
    )
    return rec


def transition_plan_year(
    uow: UnitOfWork,
    actor: Actor,
    fiscal_year: int,
    target: PlanYearState,
    reason: str | None,
) -> PlanYearRecord:
    current = uow.plan_years.get(fiscal_year, for_update=True)
    if current is None:
        raise NotFoundError("PLAN_YEAR_NOT_FOUND", f"plan year {fiscal_year} not found")
    if not can_transition_plan_year(current.state, target):
        raise ConflictError(
            "INVALID_TRANSITION", f"plan year cannot move from {current.state} to {target}"
        )
    if target is PlanYearState.ACTIVE:
        # D-12: validate the plan and post PLAN_ACTIVATED in this same transaction.
        activate_plan(uow, actor, fiscal_year)
    uow.plan_years.set_state(fiscal_year, target)
    uow.audit.write(
        AuditEntry(
            actor,
            "PLAN_YEAR_STATE_CHANGED",
            "plan_year",
            str(fiscal_year),
            before={"state": current.state.value},
            after={"state": target.value},
            reason=reason,
        )
    )
    return PlanYearRecord(fiscal_year, target, current.starts_on, current.ends_on)
