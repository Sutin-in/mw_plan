"""Internal PPR state machine (spec §14, §15, §28; D-02, D-07)."""

from __future__ import annotations

import pytest

from ppr.domain.ppr_state import (
    INTEGRATION_TBD_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    Actor,
    PprEvent,
    PprState,
    TransitionError,
    apply_event,
    find_transition,
    is_editable,
    reachable_states,
)
from ppr.domain.roles import READ_ONLY_ROLES, Role
from ppr.integration.hosxp.status import HosxpPrStatus

SYSTEM = Actor.system()


@pytest.mark.spec("D-02", "S-28")
def test_exactly_the_seven_internal_states() -> None:
    assert {s.value for s in PprState} == {
        "DRAFT",
        "CONFIRMED_LOCKED",
        "PROCUREMENT_VERIFIED",
        "PR_CHANGED_REVIEW_REQUIRED",
        "UNLOCKED_FOR_REVISION",
        "CANCELLED",
        "CLOSED",
    }
    assert "PR_APPROVED" not in PprState.__members__
    assert "ACTIVE" not in PprState.__members__


@pytest.mark.spec("D-02")
def test_hosxp_pr_status_is_a_separate_type() -> None:
    assert not issubclass(HosxpPrStatus, PprState)
    assert not issubclass(PprState, HosxpPrStatus)
    # Shared spellings (DRAFT, CANCELLED) are still different types and never equal members.
    assert PprState.DRAFT is not HosxpPrStatus.DRAFT  # type: ignore[comparison-overlap]


@pytest.mark.spec("AT-40.4.2", "S-14.3")
def test_requester_confirms_draft_into_locked_state_with_revalidation() -> None:
    new, t = apply_event(PprState.DRAFT, PprEvent.CONFIRM, Actor.user(Role.REQUESTER))
    assert new is PprState.CONFIRMED_LOCKED
    assert t.requires_revalidation


@pytest.mark.spec("AT-40.4.1", "AT-40.4.3", "S-15.1")
def test_only_draft_and_unlocked_are_editable() -> None:
    assert is_editable(PprState.DRAFT)
    assert is_editable(PprState.UNLOCKED_FOR_REVISION)
    for s in set(PprState) - {PprState.DRAFT, PprState.UNLOCKED_FOR_REVISION}:
        assert not is_editable(s), s


@pytest.mark.spec("AT-40.4.4", "S-15.2")
@pytest.mark.parametrize("role", [Role.PLAN_OFFICER, Role.ADMIN])
@pytest.mark.parametrize(
    "source",
    [
        PprState.CONFIRMED_LOCKED,
        PprState.PROCUREMENT_VERIFIED,
        PprState.PR_CHANGED_REVIEW_REQUIRED,
    ],
)
def test_plan_officer_and_admin_can_unlock_with_reason(role: Role, source: PprState) -> None:
    new, t = apply_event(source, PprEvent.UNLOCK, Actor.user(role), reason="PR qty corrected")
    assert new is PprState.UNLOCKED_FOR_REVISION
    assert t.requires_reason


@pytest.mark.spec("S-15.2")
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_unlock_without_reason_is_rejected(reason: str | None) -> None:
    with pytest.raises(TransitionError, match="reason"):
        apply_event(
            PprState.CONFIRMED_LOCKED, PprEvent.UNLOCK, Actor.user(Role.ADMIN), reason=reason
        )


@pytest.mark.spec("AT-40.4.5", "S-15.2")
@pytest.mark.parametrize("role", [r for r in Role if r not in (Role.PLAN_OFFICER, Role.ADMIN)])
def test_other_roles_including_procurement_cannot_unlock(role: Role) -> None:
    with pytest.raises(TransitionError):
        apply_event(PprState.CONFIRMED_LOCKED, PprEvent.UNLOCK, Actor.user(role), reason="x")


@pytest.mark.spec("AT-40.4.6", "S-15.3", "D-09")
def test_requester_reconfirms_with_revalidation_and_new_version() -> None:
    new, t = apply_event(
        PprState.UNLOCKED_FOR_REVISION, PprEvent.RECONFIRM, Actor.user(Role.REQUESTER)
    )
    assert new is PprState.CONFIRMED_LOCKED
    assert t.requires_revalidation and t.creates_new_version


@pytest.mark.spec("D-09")
@pytest.mark.parametrize("role", [r for r in Role if r is not Role.REQUESTER])
def test_only_requester_may_reconfirm_not_unlockers(role: Role) -> None:
    # Unlock authority (Plan Officer / Admin) never implies reconfirm authority.
    with pytest.raises(TransitionError):
        apply_event(PprState.UNLOCKED_FOR_REVISION, PprEvent.RECONFIRM, Actor.user(role))


@pytest.mark.spec("D-10", "S-16")
def test_pr_changed_has_no_acknowledgement_shortcut() -> None:
    allowed = {t.event for t in TRANSITIONS if PprState.PR_CHANGED_REVIEW_REQUIRED in t.sources}
    assert allowed == {PprEvent.UNLOCK, PprEvent.CANCEL_FROM_HOSXP}
    for e in (PprEvent.CONFIRM, PprEvent.RECONFIRM, PprEvent.VERIFY_PROCUREMENT):
        for role in Role:
            with pytest.raises(TransitionError):
                apply_event(PprState.PR_CHANGED_REVIEW_REQUIRED, e, Actor.user(role))


@pytest.mark.spec("D-09", "D-10", "S-28")
def test_required_path_after_critical_pr_change() -> None:
    state, _ = apply_event(PprState.PROCUREMENT_VERIFIED, PprEvent.DETECT_PR_CHANGE, SYSTEM)
    assert state is PprState.PR_CHANGED_REVIEW_REQUIRED
    state, _ = apply_event(
        state, PprEvent.UNLOCK, Actor.user(Role.PLAN_OFFICER), reason="HOSxP qty changed"
    )
    assert state is PprState.UNLOCKED_FOR_REVISION
    state, t = apply_event(state, PprEvent.RECONFIRM, Actor.user(Role.REQUESTER))
    assert state is PprState.CONFIRMED_LOCKED
    assert t.requires_revalidation


@pytest.mark.spec("AT-40.9.2", "S-14.4")
def test_only_head_of_procurement_role_can_verify() -> None:
    new, _ = apply_event(
        PprState.CONFIRMED_LOCKED,
        PprEvent.VERIFY_PROCUREMENT,
        Actor.user(Role.HEAD_OF_PROCUREMENT),
    )
    assert new is PprState.PROCUREMENT_VERIFIED
    for role in set(Role) - {Role.HEAD_OF_PROCUREMENT}:
        with pytest.raises(TransitionError):
            apply_event(PprState.CONFIRMED_LOCKED, PprEvent.VERIFY_PROCUREMENT, Actor.user(role))


@pytest.mark.spec("AT-40.10.4")
def test_read_only_roles_cannot_trigger_any_transition() -> None:
    for role in READ_ONLY_ROLES:
        for t in TRANSITIONS:
            for src in t.sources:
                with pytest.raises(TransitionError):
                    apply_event(src, t.event, Actor.user(role), reason="x")


@pytest.mark.spec("S-16", "S-14.6")
@pytest.mark.parametrize("event", [PprEvent.DETECT_PR_CHANGE, PprEvent.CANCEL_FROM_HOSXP])
def test_sync_events_cannot_be_raised_by_users(event: PprEvent) -> None:
    for role in Role:
        with pytest.raises(TransitionError, match="synchronization"):
            apply_event(PprState.CONFIRMED_LOCKED, event, Actor.user(role))


@pytest.mark.spec("S-16")
def test_pr_change_detection_moves_to_review_required() -> None:
    for src in (PprState.CONFIRMED_LOCKED, PprState.PROCUREMENT_VERIFIED):
        new, _ = apply_event(src, PprEvent.DETECT_PR_CHANGE, SYSTEM)
        assert new is PprState.PR_CHANGED_REVIEW_REQUIRED


@pytest.mark.spec("S-14.6")
def test_hosxp_cancellation_cancels_every_confirmed_non_terminal_state() -> None:
    for src in (
        PprState.CONFIRMED_LOCKED,
        PprState.PROCUREMENT_VERIFIED,
        PprState.PR_CHANGED_REVIEW_REQUIRED,
        PprState.UNLOCKED_FOR_REVISION,
    ):
        new, _ = apply_event(src, PprEvent.CANCEL_FROM_HOSXP, SYSTEM)
        assert new is PprState.CANCELLED


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for s in TERMINAL_STATES:
        for e in PprEvent:
            assert find_transition(s, e) is None


@pytest.mark.spec("D-07", "G-4")
def test_closed_has_no_inbound_transition_and_no_manual_close_event() -> None:
    assert PprState.CLOSED in INTEGRATION_TBD_STATES
    assert all(t.target is not PprState.CLOSED for t in TRANSITIONS)
    assert PprState.CLOSED not in reachable_states()
    assert not any("CLOSE" in e.value for e in PprEvent)


def test_every_other_state_is_reachable_from_draft() -> None:
    assert reachable_states() == frozenset(PprState) - {PprState.CLOSED}


def test_invalid_event_for_state_is_rejected() -> None:
    with pytest.raises(TransitionError, match="not allowed"):
        apply_event(PprState.DRAFT, PprEvent.VERIFY_PROCUREMENT, Actor.user(Role.ADMIN))
