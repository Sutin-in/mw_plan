"""Internal PPR state machine (spec §14, §15, §28; D-02, D-07).

Seven internal states. HOSxP PR status is a SEPARATE field (see
``ppr.integration.hosxp.status``) and is never a value of ``PprState``.

* ``CLOSED`` has NO inbound transition yet: it must derive from a verified terminal
  HOSxP status (D-07 / gate G-4). There is deliberately no manual close event.
* Discarding a DRAFT is a binding event (``pr_binding``), not a state transition:
  a discarded draft has no successor state.
* D-09: only REQUESTER confirms/reconfirms; unlock authority never implies reconfirm.
* D-10: PR_CHANGED_REVIEW_REQUIRED has no acknowledgement shortcut.
* Role checks here are rule-level. The Head-of-Procurement *appointment* check
  (spec §23) and server-side enforcement are application/API concerns (Wave 5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ppr.domain.roles import Role


class PprState(StrEnum):
    DRAFT = "DRAFT"
    CONFIRMED_LOCKED = "CONFIRMED_LOCKED"
    PROCUREMENT_VERIFIED = "PROCUREMENT_VERIFIED"
    PR_CHANGED_REVIEW_REQUIRED = "PR_CHANGED_REVIEW_REQUIRED"
    UNLOCKED_FOR_REVISION = "UNLOCKED_FOR_REVISION"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


class PprEvent(StrEnum):
    CONFIRM = "CONFIRM"
    VERIFY_PROCUREMENT = "VERIFY_PROCUREMENT"
    DETECT_PR_CHANGE = "DETECT_PR_CHANGE"  # sync only
    UNLOCK = "UNLOCK"
    RECONFIRM = "RECONFIRM"
    CANCEL_FROM_HOSXP = "CANCEL_FROM_HOSXP"  # sync only


@dataclass(frozen=True)
class Transition:
    event: PprEvent
    sources: frozenset[PprState]
    target: PprState
    roles: frozenset[Role]  # empty => system (sync) only
    requires_reason: bool = False
    requires_revalidation: bool = False
    creates_new_version: bool = False

    @property
    def system_only(self) -> bool:
        return not self.roles


_CONFIRMED_ACTIVE = frozenset(
    {
        PprState.CONFIRMED_LOCKED,
        PprState.PROCUREMENT_VERIFIED,
        PprState.PR_CHANGED_REVIEW_REQUIRED,
        PprState.UNLOCKED_FOR_REVISION,
    }
)

TRANSITIONS: tuple[Transition, ...] = (
    Transition(
        PprEvent.CONFIRM,
        frozenset({PprState.DRAFT}),
        PprState.CONFIRMED_LOCKED,
        frozenset({Role.REQUESTER}),
        requires_revalidation=True,
    ),
    Transition(
        PprEvent.VERIFY_PROCUREMENT,
        frozenset({PprState.CONFIRMED_LOCKED}),
        PprState.PROCUREMENT_VERIFIED,
        frozenset({Role.HEAD_OF_PROCUREMENT}),
    ),
    Transition(
        PprEvent.DETECT_PR_CHANGE,
        frozenset({PprState.CONFIRMED_LOCKED, PprState.PROCUREMENT_VERIFIED}),
        PprState.PR_CHANGED_REVIEW_REQUIRED,
        frozenset(),
    ),
    Transition(
        PprEvent.UNLOCK,
        frozenset(
            {
                PprState.CONFIRMED_LOCKED,
                PprState.PROCUREMENT_VERIFIED,
                PprState.PR_CHANGED_REVIEW_REQUIRED,
            }
        ),
        PprState.UNLOCKED_FOR_REVISION,
        frozenset({Role.PLAN_OFFICER, Role.ADMIN}),
        requires_reason=True,
    ),
    Transition(
        PprEvent.RECONFIRM,
        frozenset({PprState.UNLOCKED_FOR_REVISION}),
        PprState.CONFIRMED_LOCKED,
        frozenset({Role.REQUESTER}),  # D-09: requester attestation only
        requires_revalidation=True,
        creates_new_version=True,
    ),
    Transition(PprEvent.CANCEL_FROM_HOSXP, _CONFIRMED_ACTIVE, PprState.CANCELLED, frozenset()),
)

TERMINAL_STATES: frozenset[PprState] = frozenset({PprState.CANCELLED, PprState.CLOSED})
EDITABLE_STATES: frozenset[PprState] = frozenset({PprState.DRAFT, PprState.UNLOCKED_FOR_REVISION})
# States in which the PPR's plan usage is held in the ledger.
PLAN_USAGE_HELD_STATES: frozenset[PprState] = _CONFIRMED_ACTIVE
# States whose inbound transitions are not yet defined (integration-TBD, D-07 / G-4).
INTEGRATION_TBD_STATES: frozenset[PprState] = frozenset({PprState.CLOSED})


class TransitionError(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    """Who triggers an event: a user with a role, or the synchronization process."""

    role: Role | None = None
    is_system: bool = False

    @staticmethod
    def system() -> Actor:
        return Actor(is_system=True)

    @staticmethod
    def user(role: Role) -> Actor:
        return Actor(role=role)


def find_transition(state: PprState, event: PprEvent) -> Transition | None:
    for t in TRANSITIONS:
        if t.event is event and state in t.sources:
            return t
    return None


def apply_event(
    state: PprState, event: PprEvent, actor: Actor, *, reason: str | None = None
) -> tuple[PprState, Transition]:
    t = find_transition(state, event)
    if t is None:
        raise TransitionError(f"event {event} is not allowed from state {state}")
    if t.system_only:
        if not actor.is_system:
            raise TransitionError(f"event {event} can only be raised by synchronization")
    else:
        if actor.is_system or actor.role not in t.roles:
            raise TransitionError(
                f"role {actor.role} may not perform {event}; allowed: "
                + ", ".join(sorted(r.value for r in t.roles))
            )
    if t.requires_reason and not (reason and reason.strip()):
        raise TransitionError(f"event {event} requires a reason")
    return t.target, t


def is_editable(state: PprState) -> bool:
    """Locked PPRs cannot be edited (spec §15.1)."""
    return state in EDITABLE_STATES


def reachable_states() -> frozenset[PprState]:
    reached = {PprState.DRAFT}
    frontier = [PprState.DRAFT]
    while frontier:
        s = frontier.pop()
        for t in TRANSITIONS:
            if s in t.sources and t.target not in reached:
                reached.add(t.target)
                frontier.append(t.target)
    return frozenset(reached)
