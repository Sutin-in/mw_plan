"""PR-number binding lifecycle (D-03, D-05).

* A DRAFT is unnumbered and does NOT consume the lifetime binding.
* At most one working draft per PR at a time.
* A draft may be discarded before confirmation; the PR is then free for a new draft.
* Reaching CONFIRMED_LOCKED binds the PR number permanently - cancellation never unbinds.
"""

from __future__ import annotations

from enum import StrEnum

from ppr.domain.violations import Violation, ViolationCode


class PrBinding(StrEnum):
    UNBOUND = "UNBOUND"
    DRAFT_IN_PROGRESS = "DRAFT_IN_PROGRESS"
    BOUND = "BOUND"  # permanently bound to one confirmed PPR (even if later CANCELLED)


class BindingEvent(StrEnum):
    START_DRAFT = "START_DRAFT"
    DISCARD_DRAFT = "DISCARD_DRAFT"
    CONFIRM = "CONFIRM"


class BindingError(Exception):
    def __init__(self, violation: Violation) -> None:
        super().__init__(violation.message)
        self.violation = violation


_NEXT: dict[tuple[PrBinding, BindingEvent], PrBinding] = {
    (PrBinding.UNBOUND, BindingEvent.START_DRAFT): PrBinding.DRAFT_IN_PROGRESS,
    (PrBinding.DRAFT_IN_PROGRESS, BindingEvent.DISCARD_DRAFT): PrBinding.UNBOUND,
    (PrBinding.DRAFT_IN_PROGRESS, BindingEvent.CONFIRM): PrBinding.BOUND,
}


def binding_violation(binding: PrBinding, pr_no: str | None = None) -> Violation | None:
    """Why a new draft cannot be started for this PR, or None if it can."""
    if binding is PrBinding.BOUND:
        return Violation(
            ViolationCode.PR_ALREADY_BOUND,
            "PR number is permanently bound to a confirmed PPR; "
            "restarting procurement requires a new HOSxP PR number",
            pr_no,
        )
    if binding is PrBinding.DRAFT_IN_PROGRESS:
        return Violation(
            ViolationCode.DRAFT_ALREADY_EXISTS,
            "a working draft already exists for this PR; continue or discard it first",
            pr_no,
        )
    return None


def next_binding(binding: PrBinding, event: BindingEvent) -> PrBinding:
    try:
        return _NEXT[(binding, event)]
    except KeyError:
        if event is BindingEvent.START_DRAFT:
            v = binding_violation(binding)
            assert v is not None
            raise BindingError(v) from None
        raise BindingError(
            Violation(
                ViolationCode.INVALID_BINDING_TRANSITION,
                f"event {event} is not allowed when binding is {binding}",
            )
        ) from None
