"""Application roles (spec §22). Identity comes from HOSxP; authorization is ours."""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    REQUESTER = "REQUESTER"
    PLAN_OFFICER = "PLAN_OFFICER"
    PROCUREMENT = "PROCUREMENT"
    HEAD_OF_PROCUREMENT = "HEAD_OF_PROCUREMENT"
    FINANCE = "FINANCE"
    CFO = "CFO"
    EXECUTIVE = "EXECUTIVE"
    ADMIN = "ADMIN"


READ_ONLY_ROLES: frozenset[Role] = frozenset({Role.FINANCE, Role.CFO, Role.EXECUTIVE})
