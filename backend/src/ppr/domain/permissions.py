"""Role -> permission policy (spec §22). Enforced server-side by the API (§22.9).

Wave 1B covers the permissions its endpoints need. Later waves add permissions here;
every protected endpoint must declare exactly one permission.

Notes tied to the spec:
* FINANCE, CFO and EXECUTIVE hold read permissions only (§22.5-22.7).
* ADMIN manages roles/configuration but is NOT granted plan-management permissions,
  so admin rights cannot bypass plan governance (§22.8, §39).
* A HEAD_OF_PROCUREMENT *role* is necessary but not sufficient for verification; the
  current appointment is checked separately (§23, Wave 5).
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from ppr.domain.roles import Role


class Permission(StrEnum):
    PLAN_YEAR_READ = "PLAN_YEAR_READ"
    PLAN_YEAR_MANAGE = "PLAN_YEAR_MANAGE"
    AUDIT_READ = "AUDIT_READ"
    USER_ROLE_MANAGE = "USER_ROLE_MANAGE"
    PLAN_READ = "PLAN_READ"
    PLAN_MANAGE = "PLAN_MANAGE"
    SYNC_RUN = "SYNC_RUN"
    PPR_PREVALIDATE = "PPR_PREVALIDATE"
    PPR_READ = "PPR_READ"
    PPR_REQUEST = "PPR_REQUEST"
    PPR_UNLOCK = "PPR_UNLOCK"
    PPR_DISCARD = "PPR_DISCARD"
    PROCUREMENT_QUEUE_READ = "PROCUREMENT_QUEUE_READ"
    PPR_VERIFY = "PPR_VERIFY"
    APPOINTMENT_READ = "APPOINTMENT_READ"
    APPOINTMENT_MANAGE = "APPOINTMENT_MANAGE"
    SYNC_READ = "SYNC_READ"
    DASHBOARD_READ = "DASHBOARD_READ"
    ALERT_SETTING_MANAGE = "ALERT_SETTING_MANAGE"
    REPORT_READ = "REPORT_READ"
    PLAN_AMEND = "PLAN_AMEND"


_ALL_ROLES = frozenset(Role)

ROLE_PERMISSIONS: dict[Permission, frozenset[Role]] = {
    Permission.PLAN_YEAR_READ: _ALL_ROLES,
    Permission.PLAN_YEAR_MANAGE: frozenset({Role.PLAN_OFFICER}),
    Permission.AUDIT_READ: frozenset({Role.PLAN_OFFICER, Role.ADMIN}),
    Permission.USER_ROLE_MANAGE: frozenset({Role.ADMIN}),
    Permission.PLAN_READ: _ALL_ROLES,
    Permission.PLAN_MANAGE: frozenset({Role.PLAN_OFFICER}),
    Permission.SYNC_RUN: frozenset({Role.PLAN_OFFICER, Role.ADMIN}),
    # §22.1: requesters enter a PR No. of any department (D-41), retrieve it and review its
    # items against the plan; §22.2: Plan Officers see all departments.
    Permission.PPR_PREVALIDATE: frozenset({Role.REQUESTER, Role.PLAN_OFFICER}),
    # Wave 4. Reading PPRs: everyone (REQUESTER-only users: the PPRs they created, D-41).
    # Preparing a PPR for any department's PR, editing, confirming: REQUESTER; editing and
    # confirming only by the one who created it (D-09 as amended by D-41 - checked in the
    # service). Unlocking: Plan Officer or Admin (§15.2).
    Permission.PPR_READ: _ALL_ROLES,
    Permission.PPR_REQUEST: frozenset({Role.REQUESTER}),
    Permission.PPR_UNLOCK: frozenset({Role.PLAN_OFFICER, Role.ADMIN}),
    # D-41: a draft is discarded by its creator, or - abandoned - by Planning / Admin with a
    # reason (the service decides which).
    Permission.PPR_DISCARD: frozenset({Role.REQUESTER, Role.PLAN_OFFICER, Role.ADMIN}),
    # Wave 5B. Search and timeline use PPR_READ with the same department scope as reading
    # one PPR. The queue: Procurement works it (§22.3, §31.7); Planning, Admin and
    # management follow it. PPR_VERIFY is necessary but NOT sufficient: the service also
    # requires the user's current appointment (§23, D-28). Appointments: Admin (§22.8).
    Permission.PROCUREMENT_QUEUE_READ: frozenset(
        {
            Role.PROCUREMENT,
            Role.HEAD_OF_PROCUREMENT,
            Role.PLAN_OFFICER,
            Role.ADMIN,
            Role.CFO,
            Role.EXECUTIVE,
        }
    ),
    Permission.PPR_VERIFY: frozenset({Role.HEAD_OF_PROCUREMENT}),
    Permission.APPOINTMENT_READ: frozenset(
        {Role.ADMIN, Role.PLAN_OFFICER, Role.PROCUREMENT, Role.HEAD_OF_PROCUREMENT}
    ),
    Permission.APPOINTMENT_MANAGE: frozenset({Role.ADMIN}),
    # Wave 6 (D-29): SYNC_RUN (above) triggers master and PR synchronization, retries,
    # one-PPR sync, the cancelled-PPR re-check and sees the governance list. Procurement
    # follows the synchronization results but never triggers a state change.
    Permission.SYNC_READ: frozenset(
        {Role.PLAN_OFFICER, Role.ADMIN, Role.PROCUREMENT, Role.HEAD_OF_PROCUREMENT}
    ),
    # Wave 8A (D-32, D-33): every role has a dashboard and alerts, scoped like reading the
    # underlying PPR / Plan Item (§22.1). Thresholds are configuration: ADMIN (§22.8).
    Permission.DASHBOARD_READ: _ALL_ROLES,
    Permission.ALERT_SETTING_MANAGE: frozenset({Role.ADMIN}),
    # Wave 8B (D-35): every role runs reports with the §22.1 scope; the audit report also
    # needs AUDIT_READ (checked by the report service).
    Permission.REPORT_READ: _ALL_ROLES,
    # Wave 7A (D-37): the Planning Group records a Director-approved amendment; reading the
    # amendment history uses PLAN_READ with the §22.1 department scope.
    Permission.PLAN_AMEND: frozenset({Role.PLAN_OFFICER}),
}

# Roles that see the plan of every department (spec §22). A user whose only role is
# REQUESTER sees Plan Items of their own department (owner, purchaser or demand) only.
ORG_WIDE_PLAN_ROLES: frozenset[Role] = _ALL_ROLES - {Role.REQUESTER}


def is_allowed(roles: Iterable[Role], permission: Permission) -> bool:
    allowed = ROLE_PERMISSIONS[permission]
    return any(r in allowed for r in roles)


def sees_all_departments(roles: Iterable[Role]) -> bool:
    return any(r in ORG_WIDE_PLAN_ROLES for r in roles)
