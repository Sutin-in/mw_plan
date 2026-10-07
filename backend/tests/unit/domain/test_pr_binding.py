"""PR-number binding lifecycle (D-03, D-05)."""

from __future__ import annotations

import pytest

from ppr.domain.pr_binding import (
    BindingError,
    BindingEvent,
    PrBinding,
    binding_violation,
    next_binding,
)
from ppr.domain.violations import ViolationCode


@pytest.mark.spec("D-05")
def test_draft_does_not_bind_and_is_unique_per_pr() -> None:
    b = next_binding(PrBinding.UNBOUND, BindingEvent.START_DRAFT)
    assert b is PrBinding.DRAFT_IN_PROGRESS
    with pytest.raises(BindingError) as e:
        next_binding(b, BindingEvent.START_DRAFT)
    assert e.value.violation.code is ViolationCode.DRAFT_ALREADY_EXISTS


@pytest.mark.spec("AT-40.1.5", "D-05")
def test_discarded_draft_frees_pr_for_a_new_draft() -> None:
    b = next_binding(PrBinding.UNBOUND, BindingEvent.START_DRAFT)
    b = next_binding(b, BindingEvent.DISCARD_DRAFT)
    assert b is PrBinding.UNBOUND
    assert next_binding(b, BindingEvent.START_DRAFT) is PrBinding.DRAFT_IN_PROGRESS


@pytest.mark.spec("D-03", "D-05")
def test_confirmation_binds_permanently() -> None:
    b = next_binding(PrBinding.DRAFT_IN_PROGRESS, BindingEvent.CONFIRM)
    assert b is PrBinding.BOUND
    for event in BindingEvent:
        with pytest.raises(BindingError):
            next_binding(b, event)
    v = binding_violation(b, "MOCK-PR-1")
    assert v is not None and v.code is ViolationCode.PR_ALREADY_BOUND


def test_cannot_confirm_or_discard_without_a_draft() -> None:
    for event in (BindingEvent.CONFIRM, BindingEvent.DISCARD_DRAFT):
        with pytest.raises(BindingError) as e:
            next_binding(PrBinding.UNBOUND, event)
        assert e.value.violation.code is ViolationCode.INVALID_BINDING_TRANSITION


def test_unbound_pr_has_no_binding_violation() -> None:
    assert binding_violation(PrBinding.UNBOUND) is None
