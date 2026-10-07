"""PR synchronization rules (spec §16, §25; D-02, D-03, D-10, D-17, D-22, D-23, D-29 ... D-31).

Pure decisions over ONE live observation of a bound PR. Inputs are the canonical PR
snapshot form (the JSON the confirmation stores, see ``application.pr_snapshot``), so
the live PR is compared with exactly what was confirmed, by the ONE shared classifier
(``domain.pr_changes``).

Precedence (D-31), first match decides:

1. HOSxP unavailable (connection / timeout)                  -> UNAVAILABLE
2. header query/configuration failure (incl. >1 row,
   unparseable header)                                       -> INVALID_EVIDENCE / BINDING
3. no header row                                             -> NOT_FOUND
4. binding unreliable (pr_no or pr_id differs)               -> INVALID_EVIDENCE / BINDING
   status unreadable / malformed                             -> INVALID_EVIDENCE / STATUS
5. normalized status CANCELLED (only via an IT mapping)      -> CANCELLED (even if the
   items or totals are invalid: nothing is taken from them)
6. items query/configuration failure                         -> INVALID_EVIDENCE / BINDING
7. control evidence fails (D-17, D-23, no items, duplicates) -> INVALID_EVIDENCE / CONTROL
8. identity change -> critical change -> non-critical change -> unchanged.

``UNKNOWN``, blank or unmapped status is not evidence that the PR is active: it only
means no authoritative cancellation status is available (D-30); it never by itself
causes a transition or an anomaly.

State effects (``decide``): BINDING / STATUS invalid evidence, NOT_FOUND and
UNAVAILABLE never change state. CRITICAL / IDENTITY change and CONTROL invalid evidence
move CONFIRMED_LOCKED / PROCUREMENT_VERIFIED to PR_CHANGED_REVIEW_REQUIRED and leave
the other states; nothing but CANCELLED releases plan usage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from ppr.domain.ppr_state import PLAN_USAGE_HELD_STATES, Actor, PprEvent, PprState, apply_event
from ppr.domain.pr_changes import FieldChange, critical_only, diff_pr, identity_changes
from ppr.domain.prevalidation import PrLine, PrRequest, precision_problems, total_violations


class SyncOutcome(StrEnum):
    UNCHANGED = "UNCHANGED"
    NON_CRITICAL_CHANGE = "NON_CRITICAL_CHANGE"
    CRITICAL_CHANGE = "CRITICAL_CHANGE"
    IDENTITY_CHANGE = "IDENTITY_CHANGE"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"
    CANCELLED = "CANCELLED"
    NOT_FOUND = "NOT_FOUND"
    UNAVAILABLE = "UNAVAILABLE"
    SKIPPED_STATE_MOVED = "SKIPPED_STATE_MOVED"
    ERROR = "ERROR"  # unexpected processing failure; this PPR's changes rolled back
    # Governance re-check of a CANCELLED PPR only (D-30):
    ANOMALY = "ANOMALY"
    NO_AUTHORITATIVE_STATUS = "NO_AUTHORITATIVE_STATUS"


class EvidenceClass(StrEnum):
    BINDING = "BINDING"  # the observation cannot be tied reliably to the bound PR
    STATUS = "STATUS"  # the PR status itself cannot be trusted
    CONTROL = "CONTROL"  # bound PR, but its content fails a control-evidence rule


class SyncScope(StrEnum):
    PPRS = "PPRS"  # every open confirmed-or-later PPR (manual "sync all" or nightly)
    PPR = "PPR"  # one open PPR
    PPR_RETRY = "PPR_RETRY"  # the PPRs whose attempt failed in an earlier run
    PPR_RECHECK = "PPR_RECHECK"  # governance re-check of one CANCELLED PPR


# D-29: the nightly/full and normal manual sync look at these states only.
SYNC_STATES: frozenset[PprState] = PLAN_USAGE_HELD_STATES
REVIEW_FROM: frozenset[PprState] = frozenset(
    {PprState.CONFIRMED_LOCKED, PprState.PROCUREMENT_VERIFIED}
)
# Outcomes that the governance list shows to PLAN_OFFICER / ADMIN.
GOVERNANCE_OUTCOMES: frozenset[SyncOutcome] = frozenset(
    {
        SyncOutcome.NOT_FOUND,
        SyncOutcome.INVALID_EVIDENCE,
        SyncOutcome.ANOMALY,
        SyncOutcome.ERROR,
    }
)
REACTIVATED_EXTERNALLY = "CANCELLED_PR_REACTIVATED_EXTERNALLY"

# Evidence issue codes (safe to show; they carry no HOSxP values).
HOSXP_QUERY_ERROR = "HOSXP_QUERY_ERROR"
HOSXP_ITEMS_QUERY_ERROR = "HOSXP_ITEMS_QUERY_ERROR"
PR_NO_MISMATCH = "PR_NO_MISMATCH"
PR_ID_MISMATCH = "PR_ID_MISMATCH"
PR_STATUS_UNREADABLE = "PR_STATUS_UNREADABLE"
PR_HAS_NO_ITEMS = "PR_HAS_NO_ITEMS"
PR_TOTAL_UNAVAILABLE = "PR_TOTAL_UNAVAILABLE"
PR_TOTAL_MISMATCH = "PR_TOTAL_MISMATCH"
PR_VALUE_PRECISION = "PR_VALUE_PRECISION"
DUPLICATE_PR_ITEM = "DUPLICATE_PR_ITEM"

_KNOWN_STATUSES = frozenset({"DRAFT", "ACTIVE", "APPROVED", "CANCELLED", "COMPLETED", "UNKNOWN"})
_CANCELLED = "CANCELLED"
_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EvidenceIssue:
    code: str
    evidence_class: EvidenceClass
    message: str
    field: str | None = None
    line: str | None = None  # PR item id

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "class": self.evidence_class.value,
            "message": self.message,
            "field": self.field,
            "line": self.line,
        }


class ReadFailure(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"  # HOSxP could not be reached / timed out
    HEADER_QUERY = "HEADER_QUERY"  # the header query failed or returned unusable rows
    ITEMS_QUERY = "ITEMS_QUERY"  # the items query failed or returned unusable rows


@dataclass(frozen=True)
class LiveRead:
    """One live read of a PR, in canonical snapshot form.

    ``items`` is None when the items were not read (the header was missing, unbound
    or cancelled, or reading failed).
    """

    header: Mapping[str, Any] | None
    items: tuple[Mapping[str, Any], ...] | None = None
    failure: ReadFailure | None = None
    failure_message: str | None = None


@dataclass(frozen=True)
class Assessment:
    outcome: SyncOutcome
    evidence_class: EvidenceClass | None = None
    issues: tuple[EvidenceIssue, ...] = ()
    changes: tuple[FieldChange, ...] = ()
    anomaly_code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    state_after: PprState
    event: PprEvent | None
    release: bool


# ------------------------------------------------------------------ evidence rules
def binding_issues(
    header: Mapping[str, Any], bound_pr_no: str, confirmed_pr_id: str | None
) -> list[EvidenceIssue]:
    """Can this header be trusted to be the bound PR, with a readable status?"""
    out: list[EvidenceIssue] = []
    if header.get("pr_no") != bound_pr_no:
        out.append(
            EvidenceIssue(
                PR_NO_MISMATCH,
                EvidenceClass.BINDING,
                "HOSxP returned a PR with a different PR number than the bound one",
                "pr_no",
            )
        )
    if confirmed_pr_id is None or header.get("pr_id") != confirmed_pr_id:
        out.append(
            EvidenceIssue(
                PR_ID_MISMATCH,
                EvidenceClass.BINDING,
                "the PR's HOSxP id differs from the id recorded at confirmation",
                "pr_id",
            )
        )
    status = header.get("status")
    native = header.get("native_status")
    if status not in _KNOWN_STATUSES or (
        status != _UNKNOWN and (native is None or not str(native).strip())
    ):
        # A normalized status that does not come from a native value (or is not one of
        # the known values) cannot have been produced by the verified mapping.
        out.append(
            EvidenceIssue(
                PR_STATUS_UNREADABLE,
                EvidenceClass.STATUS,
                "the PR status is malformed; it cannot be used",
                "status",
            )
        )
    return out


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def control_issues(
    header: Mapping[str, Any], items: Sequence[Mapping[str, Any]]
) -> list[EvidenceIssue]:
    """D-17 total, D-23 precision, no items, duplicate lines - as at confirmation."""
    out: list[EvidenceIssue] = []
    if not items:
        return [
            EvidenceIssue(PR_HAS_NO_ITEMS, EvidenceClass.CONTROL, "the PR has no items", "items")
        ]
    seen: set[str] = set()
    lines: list[PrLine] = []
    for it in items:
        raw_id = it.get("pr_item_id")
        if raw_id is None or not str(raw_id).strip():
            out.append(
                EvidenceIssue(
                    DUPLICATE_PR_ITEM,
                    EvidenceClass.CONTROL,
                    "a PR item has no line id, so it cannot be told apart from the others",
                    "pr_item_id",
                )
            )
            continue
        pid = str(raw_id)
        if pid in seen:
            out.append(
                EvidenceIssue(
                    DUPLICATE_PR_ITEM,
                    EvidenceClass.CONTROL,
                    f"PR item {pid} appears more than once",
                    "pr_item_id",
                    pid,
                )
            )
        seen.add(pid)
        qty, amount, price = _dec(it.get("qty")), _dec(it.get("amount")), _dec(it.get("unit_price"))
        for name in precision_problems(qty, amount, price):
            out.append(
                EvidenceIssue(
                    PR_VALUE_PRECISION,
                    EvidenceClass.CONTROL,
                    f"PR item {pid}: {name} has more decimals than supported; values are "
                    "never rounded (D-23)",
                    name.replace(" ", "_"),
                    pid,
                )
            )
        lines.append(PrLine(pid, str(it.get("item_id")), qty or Decimal(0), amount or Decimal(0)))
    request = PrRequest(
        str(header.get("pr_no")),
        None,
        None,
        tuple(lines),
        fiscal_year=None,
        header_total=_dec(header.get("total_amount")),
        cancelled_in_hosxp=False,
    )
    for v in total_violations(request):
        out.append(EvidenceIssue(v.code.value, EvidenceClass.CONTROL, v.message, "total_amount"))
    return out


def worth_one_fresh_read(issues: Sequence[EvidenceIssue]) -> bool:
    """A header/items inconsistency that a concurrent HOSxP edit can explain is read once
    more (D-29). Precision alone is not: it is never rounded, and re-reading changes
    nothing about how it is judged."""
    return any(
        i.evidence_class is EvidenceClass.CONTROL and i.code != PR_VALUE_PRECISION for i in issues
    )


# ------------------------------------------------------------------ assessment
def needs_items(read: LiveRead, bound_pr_no: str, confirmed_pr_id: str | None) -> bool:
    """Whether the items must be read: only for a bound, readable, non-cancelled PR."""
    h = read.header
    return (
        read.failure is None
        and h is not None
        and not binding_issues(h, bound_pr_no, confirmed_pr_id)
        and h.get("status") != _CANCELLED
    )


def _invalid(issues: Sequence[EvidenceIssue]) -> Assessment:
    classes = {i.evidence_class for i in issues}
    cls = (
        EvidenceClass.BINDING
        if EvidenceClass.BINDING in classes
        else EvidenceClass.STATUS
        if EvidenceClass.STATUS in classes
        else EvidenceClass.CONTROL
    )
    return Assessment(SyncOutcome.INVALID_EVIDENCE, cls, tuple(issues))


def assess(
    read: LiveRead,
    *,
    bound_pr_no: str,
    confirmed_header: Mapping[str, Any],
    confirmed_items: Sequence[Mapping[str, Any]],
) -> Assessment:
    """Classify one observation of an open confirmed-or-later PPR (D-29 ... D-31)."""
    if read.failure is ReadFailure.UNAVAILABLE:
        return Assessment(SyncOutcome.UNAVAILABLE)
    if read.failure is ReadFailure.HEADER_QUERY:
        return _invalid(
            [
                EvidenceIssue(
                    HOSXP_QUERY_ERROR,
                    EvidenceClass.BINDING,
                    read.failure_message or "the HOSxP PR query failed",
                )
            ]
        )
    header = read.header
    if header is None:
        return Assessment(SyncOutcome.NOT_FOUND)
    if issues := binding_issues(header, bound_pr_no, confirmed_header.get("pr_id")):
        return _invalid(issues)
    if header.get("status") == _CANCELLED:
        return Assessment(SyncOutcome.CANCELLED)
    if read.failure is ReadFailure.ITEMS_QUERY or read.items is None:
        return _invalid(
            [
                EvidenceIssue(
                    HOSXP_ITEMS_QUERY_ERROR,
                    EvidenceClass.BINDING,
                    read.failure_message or "the HOSxP PR items could not be read",
                    "items",
                )
            ]
        )
    if issues := control_issues(header, read.items):
        return _invalid(issues)
    changes = diff_pr(confirmed_header, confirmed_items, header, read.items)
    if identity_changes(changes):
        return Assessment(SyncOutcome.IDENTITY_CHANGE, changes=changes)
    if critical_only(changes):
        return Assessment(SyncOutcome.CRITICAL_CHANGE, changes=changes)
    if changes:
        return Assessment(SyncOutcome.NON_CRITICAL_CHANGE, changes=changes)
    return Assessment(SyncOutcome.UNCHANGED)


def assess_recheck(
    read: LiveRead, *, bound_pr_no: str, confirmed_header: Mapping[str, Any]
) -> Assessment:
    """Governance re-check of a CANCELLED PPR: observation only (D-30)."""
    if read.failure is ReadFailure.UNAVAILABLE:
        return Assessment(SyncOutcome.UNAVAILABLE)
    if read.failure is not None:
        return _invalid(
            [
                EvidenceIssue(
                    HOSXP_QUERY_ERROR,
                    EvidenceClass.BINDING,
                    read.failure_message or "the HOSxP PR query failed",
                )
            ]
        )
    header = read.header
    if header is None:
        return Assessment(SyncOutcome.NOT_FOUND)
    if issues := binding_issues(header, bound_pr_no, confirmed_header.get("pr_id")):
        return _invalid(issues)
    status = header.get("status")
    if status == _CANCELLED:
        return Assessment(SyncOutcome.CANCELLED)
    if status == _UNKNOWN:
        return Assessment(SyncOutcome.NO_AUTHORITATIVE_STATUS)
    return Assessment(SyncOutcome.ANOMALY, anomaly_code=REACTIVATED_EXTERNALLY)


# ------------------------------------------------------------------ state effect
_MOVES_TO_REVIEW = frozenset({SyncOutcome.CRITICAL_CHANGE, SyncOutcome.IDENTITY_CHANGE})


def decide(state: PprState, assessment: Assessment) -> Decision:
    """The state matrix of D-29 ... D-31. Only the state machine's system events are used;
    nothing reaches CLOSED (G-4)."""
    if state not in SYNC_STATES:
        return Decision(state, None, False)
    outcome = assessment.outcome
    if outcome is SyncOutcome.CANCELLED:
        target, _ = apply_event(state, PprEvent.CANCEL_FROM_HOSXP, Actor.system())
        return Decision(target, PprEvent.CANCEL_FROM_HOSXP, True)
    to_review = outcome in _MOVES_TO_REVIEW or (
        outcome is SyncOutcome.INVALID_EVIDENCE
        and assessment.evidence_class is EvidenceClass.CONTROL
    )
    if to_review and state in REVIEW_FROM:
        target, _ = apply_event(state, PprEvent.DETECT_PR_CHANGE, Actor.system())
        return Decision(target, PprEvent.DETECT_PR_CHANGE, False)
    return Decision(state, None, False)
