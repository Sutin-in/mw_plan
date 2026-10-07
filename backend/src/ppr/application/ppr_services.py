"""Wave 4 use cases: PPR draft, allocation, discard, confirm/reconfirm, unlock (spec §14,
§15, §17, §19, §28, §30, §35; D-03 ... D-06, D-09, D-16 ... D-19).

Rules enforced here:
* Every PR line uses the plan row its requester chose among the rows offered (D-43); the
  choices are kept with the PPR and re-checked at every confirmation. Nothing is remembered
  between PPRs and nothing is matched automatically.
* A draft is created only for an eligible PR (all-or-none, Wave 3 checks), unnumbered,
  at most one working draft per PR (D-05); it carries the PR lines from HOSxP only
  (§11.4) and ONE default allocation: the PR header category for the full required
  amount (D-19).
* Allocation categories are limited to the PR's own category and the Plan Budget
  categories of the same fiscal year and fund source (D-19); no HOSxP hierarchy is
  inferred. Several categories need an ``adjustment_reference`` at confirmation.
* Confirmation is one transaction (§14.3, §19): lock the PPR row and the Plan Item rows,
  re-fetch the PR live, re-run every check with the SERVER DATE of the confirmation
  (D-18), validate the allocation, assign the PPR number (D-06), create the lifetime PR
  binding (D-03), post PPR_CONFIRMED ledger usage, store an append-only version
  snapshot with a document code, lock the PPR, and audit. Any failure writes nothing.
* A draft whose PR changed in HOSxP since it was prepared cannot be confirmed; the
  requester discards it and prepares again (no silent change of what is confirmed).
* Only a REQUESTER confirms/reconfirms (D-09); only PLAN_OFFICER/ADMIN unlock, with a
  reason (§15.2). Reconfirmation releases the previous version's usage and posts the new
  usage atomically, as a new version; old versions stay (§15.3).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from ppr.application import assigned_services
from ppr.application.errors import (
    ConflictError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    UpstreamUnavailableError,
)
from ppr.application.pr_services import (
    PrevalidationReport,
    evaluate,
    fetch_pr,
)
from ppr.application.pr_snapshot import header_json, items_json, pr_snapshot
from ppr.domain.allocation import (
    CategoryAllocation,
    adjustment_reference_violation,
    is_multi_category,
    validate_allocations,
)
from ppr.domain.fiscal_year import FiscalYearConfig, fiscal_year_label
from ppr.domain.ledger import LedgerEntry, LedgerEventType, LedgerRuleError, PlanItemLedger
from ppr.domain.plan import PlanType
from ppr.domain.ppr_number import format_ppr_number
from ppr.domain.ppr_state import Actor as StateActor
from ppr.domain.ppr_state import PprEvent, PprState, TransitionError, apply_event, find_transition
from ppr.domain.pr_binding import PrBinding
from ppr.domain.pr_changes import FieldChange, critical_only, diff_pr, identity_changes
from ppr.domain.roles import Role
from ppr.domain.violations import Violation, ViolationCode
from ppr.integration.hosxp.contracts import PrHeader, PrItem
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.ports import (
    Actor,
    AuditEntry,
    MasterKind,
    PlanItemRecord,
    PprAllocationRecord,
    PprHeaderInput,
    PprItemRecord,
    PprRecord,
    PprScope,
    PprVersionRecord,
    UniquenessError,
    UnitOfWork,
)

ZERO = Decimal(0)


@dataclass(frozen=True)
class Caller:
    """Who acts: their id, roles and which PPRs they read (D-41)."""

    user_id: int
    roles: frozenset[Role]
    ppr_scope: PprScope | None  # None = every PPR; REQUESTER-only: the PPRs they created

    @property
    def actor(self) -> Actor:
        return Actor(self.user_id)


# ------------------------------------------------------------------ helpers
def _violations(vs: Iterable[Violation]) -> list[dict[str, Any]]:
    return [{"code": v.code.value, "message": v.message, "subject": v.subject} for v in vs]


def _not_eligible(report: PrevalidationReport) -> ConflictError:
    return ConflictError(
        "PR_NOT_ELIGIBLE",
        "the PR does not pass the plan checks: "
        + ", ".join(sorted({v.code.value for v in report.result.all_violations()})),
        details=_violations(report.result.all_violations()),
    )


def _load(uow: UnitOfWork, caller: Caller, ppr_id: int, *, for_update: bool) -> PprRecord:
    rec = uow.pprs.get(ppr_id, for_update=for_update)
    if (
        rec is None
        or rec.discarded_at is not None
        or (
            caller.ppr_scope is not None
            and rec.created_by_user_id != caller.ppr_scope.created_by_user_id
        )
    ):
        # Other users' PPRs answer 404 for a requester: no information leak (§22.1, D-41).
        raise NotFoundError("PPR_NOT_FOUND", f"PPR {ppr_id} not found")
    return rec


def _items_from(report: PrevalidationReport) -> list[PprItemRecord]:
    out = []
    for item, line in zip(report.items, report.result.lines, strict=True):
        assert line.matched_plan_item_id is not None  # eligible => every line matched
        out.append(
            PprItemRecord(
                item.pr_item_id,
                item.item_id,
                item.item_name,
                item.unit,
                item.qty,
                item.unit_price,
                item.amount,
                int(line.matched_plan_item_id),
            )
        )
    return out


def _header_input(h: PrHeader, required: Decimal) -> PprHeaderInput:
    assert h.fiscal_year is not None and h.department_id and h.fund_source_id
    return PprHeaderInput(
        pr_no=h.pr_no,
        fiscal_year=h.fiscal_year,
        department_id=h.department_id,
        fund_source_id=h.fund_source_id,
        pr_budget_category_id=h.budget_category_id,
        required_amount=required,
        hosxp_pr_status=h.status.value,
        hosxp_native_status=h.native_status,
    )


def eligible_categories(uow: UnitOfWork, rec: PprRecord) -> dict[str, str]:
    """D-19: category -> fund source for every category this PPR may use."""
    return _eligible(uow, rec.fiscal_year, rec.fund_source_id, rec.pr_budget_category_id)


def _eligible(
    uow: UnitOfWork, fiscal_year: int, fund_source_id: str, pr_category: str | None
) -> dict[str, str]:
    out = {
        b.budget_category_id: b.fund_source_id
        for b in uow.plans.budgets(fiscal_year)
        if b.fund_source_id == fund_source_id
    }
    if pr_category:
        out[pr_category] = fund_source_id
    return out


def _j(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _j(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_j(v) for v in value]
    return value


def _summary(rec: PprRecord) -> dict[str, Any]:
    out: dict[str, Any] = _j(
        {
            "state": rec.state.value,
            "ppr_number": rec.ppr_number,
            "version": rec.current_version,
            "required_amount": rec.required_amount,
            "allocations": [
                {"budget_category_id": a.budget_category_id, "amount": a.amount}
                for a in rec.allocations
            ],
            "adjustment_reference": rec.adjustment_reference,
        }
    )
    return out


def _require_creator(caller: Caller, rec: PprRecord) -> None:
    """D-09 as amended by D-41: requester actions on a PPR belong to the REQUESTER who
    created it (the PPR's department is the PR's, not the user's)."""
    if Role.REQUESTER not in caller.roles:
        raise ForbiddenError("REQUESTER_ONLY", "only requesters prepare, edit or confirm PPRs")
    if rec.created_by_user_id != caller.user_id:
        raise ForbiddenError(
            "NOT_THE_CREATOR", "only the requester who created this PPR may do this"
        )


def _changes(
    before: dict[str, Any], header: PrHeader, items: Sequence[PrItem]
) -> tuple[FieldChange, ...]:
    now = pr_snapshot(header, items)
    return diff_pr(before["header"], before["items"], now["header"], now["items"])


def _change_details(changes: Sequence[FieldChange]) -> list[dict[str, Any]]:
    return [c.as_dict() for c in changes]


def document_code(snapshot: dict[str, Any]) -> str:
    """Content fingerprint of a confirmed version (shown on the print, §35)."""
    canonical = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16].upper()
    return "-".join(digest[i : i + 4] for i in range(0, 16, 4))


# ------------------------------------------------------------------ draft
def prepare_draft(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    caller: Caller,
    pr_no: str,
    choices: Mapping[str, int] | None = None,
) -> PprRecord:
    if Role.REQUESTER not in caller.roles:
        raise ForbiddenError("REQUESTER_ONLY", "only requesters prepare PPRs (D-09)")
    wanted = dict(choices or {})
    # D-41: a requester enters a PR of any department; the PPR takes the PR's department.
    header, items = fetch_pr(gateway, pr_no)
    _known_lines(wanted, items)
    report = evaluate(
        uow,
        pr_no,
        header,
        items,
        fiscal_year_label(today, fy_config),
        binding=uow.pprs.binding_state(pr_no),
        choices=wanted,
    )
    if not report.result.eligible or header is None:
        raise _not_eligible(report)
    required = report.result.required_amount
    try:
        ppr_id = uow.pprs.create_draft(
            _header_input(header, required), pr_snapshot(header, items), caller.user_id
        )
    except UniquenessError as exc:
        raise ConflictError("DRAFT_ALREADY_EXISTS", str(exc)) from exc
    uow.pprs.replace_items(ppr_id, _items_from(report))
    uow.pprs.set_choices(
        ppr_id,
        {k: _label(report.plan_items[str(pid)]) for k, pid in wanted.items()},
        caller.user_id,
    )
    default = (
        [PprAllocationRecord(header.budget_category_id, required)]
        if header.budget_category_id and required > ZERO
        else []
    )
    uow.pprs.replace_allocations(ppr_id, default, None)
    rec = uow.pprs.get(ppr_id)
    assert rec is not None
    assigned_services.propose_default(uow, rec, _assigned_drawn(report))  # D-38 A-2
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_DRAFT_CREATED",
            "ppr",
            str(ppr_id),
            after={**_summary(rec), "choices": wanted},
        )
    )
    return rec


def _label(row: PlanItemRecord) -> tuple[int, str, str]:
    return (row.id, row.item_name, row.unit)


def _known_lines(choices: Mapping[str, int], items: Sequence[PrItem]) -> None:
    """A choice names a line the PR has (never kept for a line it does not have)."""
    lines = {i.pr_item_id for i in items}
    if unknown := sorted(k for k in choices if k not in lines):
        raise InvalidInputError(
            "CHOICE_NOT_A_PR_LINE",
            "the PR has no line " + ", ".join(unknown),
            [
                {"code": "CHOICE_NOT_A_PR_LINE", "message": "no such PR line", "subject": k}
                for k in unknown
            ],
        )


def choice_report(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    caller: Caller,
    ppr_id: int,
    trial: Mapping[str, int] | None = None,
) -> tuple[dict[str, int], PrevalidationReport]:
    """D-43: a draft or unlocked PPR's lines, read live, with the rows each may choose and
    the checks as confirmation would run them - with the stored choices, or with ``trial``
    choices (nothing is stored). Returns (stored choices, report)."""
    rec = _load(uow, caller, ppr_id, for_update=False)
    if rec.state not in (PprState.DRAFT, PprState.UNLOCKED_FOR_REVISION):
        raise ConflictError("PPR_LOCKED", f"PPR is {rec.state.value}; its choices are final")
    stored = uow.pprs.choices(rec.id)
    header, items = fetch_pr(gateway, rec.pr_no)
    own = rec.ppr_number if rec.state is PprState.UNLOCKED_FOR_REVISION else None
    report = evaluate(
        uow,
        rec.pr_no,
        header,
        items,
        fiscal_year_label(today, fy_config),
        binding=PrBinding.UNBOUND,
        own_ppr_ref=own,
        choices=stored if trial is None else trial,
    )
    return stored, report


def set_choices(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    caller: Caller,
    ppr_id: int,
    choices: Mapping[str, int],
) -> PprRecord:
    """D-43: the creator chooses again the plan row of lines of a draft or unlocked PPR (a
    wrong choice is corrected after a Plan Officer unlock). The lines named are changed, the
    others keep their choice; a row a named line may not use is refused now, and a kept choice
    the line may no longer use (its unit or the plan changed) is dropped. Every other check
    (remaining balance, all lines chosen) is enforced at confirmation, like allocations.
    Nothing is posted to the ledger here."""
    rec = _load(uow, caller, ppr_id, for_update=True)
    _require_creator(caller, rec)
    if rec.state not in (PprState.DRAFT, PprState.UNLOCKED_FOR_REVISION):
        raise ConflictError("PPR_LOCKED", f"PPR is {rec.state.value}; it cannot be edited")
    header, items = fetch_pr(gateway, rec.pr_no)  # the lines as confirmation will read them
    if header is None:
        raise ConflictError("PR_NOT_FOUND", f"PR {rec.pr_no} no longer exists in HOSxP")
    _known_lines(choices, items)
    lines = {i.pr_item_id for i in items}
    before = uow.pprs.choices(rec.id)
    merged = {k: v for k, v in before.items() if k in lines} | {
        k: int(v) for k, v in choices.items()
    }
    own = rec.ppr_number if rec.state is PprState.UNLOCKED_FOR_REVISION else None
    report = evaluate(
        uow,
        rec.pr_no,
        header,
        items,
        fiscal_year_label(today, fy_config),
        binding=PrBinding.UNBOUND,
        own_ppr_ref=own,
        choices=merged,
    )
    if not report.result.lines_checked:
        # The lines cannot be checked (PR header / fiscal year problems): nothing is stored
        # that has not been checked against the rows offered.
        raise _not_eligible(report)
    not_allowed = {
        line.pr_item_id: [
            v for v in line.violations if v.code is ViolationCode.PLAN_ROW_NOT_ALLOWED
        ]
        for line in report.result.lines
    }
    if refused := [v for k in choices for v in not_allowed.get(k, [])]:
        raise InvalidInputError(
            "PLAN_ROW_NOT_ALLOWED", "; ".join(v.message for v in refused), _violations(refused)
        )
    after_choices = {k: v for k, v in merged.items() if not not_allowed.get(k)}
    # A line named now takes its row's name and unit as they read now; a kept line keeps
    # those it was chosen with (a row renamed since is still caught at confirmation).
    old_labels = uow.pprs.chosen_labels(rec.id)
    uow.pprs.set_choices(
        rec.id,
        {
            k: _label(report.offered[str(pid)]) if k in choices else old_labels[k]
            for k, pid in after_choices.items()
        },
        caller.user_id,
    )
    # D-38 A-2: working coverage of an Assigned Purchase row no longer chosen goes with it
    # (the purchaser states coverage for the rows the PPR now draws on).
    drawn = _assigned_drawn(report)
    working = uow.assigned.coverage(rec.id, confirmed=False)
    kept = {p: dict(v) for p, v in working.items() if p in drawn}
    if kept != {p: dict(v) for p, v in working.items()}:
        uow.assigned.replace_coverage(rec.id, kept, confirmed=False)
    if rec.state is PprState.DRAFT:
        # A draft holds no plan usage: its lines follow the new choices at once. An unlocked
        # PPR keeps the lines of its confirmed version (whose usage reconfirmation releases)
        # until it is reconfirmed.
        by_line = {i.pr_item_id: i for i in rec.items}
        uow.pprs.replace_items(
            rec.id,
            [
                PprItemRecord(
                    i.pr_item_id,
                    i.item_id,
                    i.item_name,
                    i.unit,
                    i.qty,
                    i.unit_price,
                    i.amount,
                    after_choices.get(i.pr_item_id, by_line[i.pr_item_id].plan_item_id),
                )
                for i in rec.items
            ],
        )
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_CHOICES_UPDATED",
            "ppr",
            str(rec.id),
            before={"choices": before},
            after={"choices": after_choices},
        )
    )
    after = uow.pprs.get(rec.id)
    assert after is not None
    return after


def set_allocations(
    uow: UnitOfWork,
    caller: Caller,
    ppr_id: int,
    allocations: Sequence[PprAllocationRecord],
    adjustment_reference: str | None,
) -> PprRecord:
    rec = _load(uow, caller, ppr_id, for_update=True)
    _require_creator(caller, rec)
    if rec.state not in (PprState.DRAFT, PprState.UNLOCKED_FOR_REVISION):
        raise ConflictError("PPR_LOCKED", f"PPR is {rec.state.value}; it cannot be edited")
    problems = [
        v
        for v in validate_allocations(
            [CategoryAllocation(a.budget_category_id, a.amount) for a in allocations],
            pr_fund_source_id=rec.fund_source_id,
            category_fund_source=eligible_categories(uow, rec),
            required_amount=rec.required_amount,
        )
        # The total may be unbalanced while editing; it is enforced at confirmation.
        if v.code is not ViolationCode.ALLOCATION_SUM_MISMATCH
    ]
    if problems:
        raise InvalidInputError(
            "ALLOCATION_INVALID", "; ".join(v.message for v in problems), _violations(problems)
        )
    ref = (adjustment_reference or "").strip() or None
    uow.pprs.replace_allocations(ppr_id, allocations, ref)
    after = uow.pprs.get(ppr_id)
    assert after is not None
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_ALLOCATION_UPDATED",
            "ppr",
            str(ppr_id),
            before=_summary(rec),
            after=_summary(after),
        )
    )
    return after


def discard_draft(uow: UnitOfWork, caller: Caller, ppr_id: int, reason: str | None = None) -> None:
    """The creator discards their draft; a Plan Officer or Admin may discard an abandoned
    draft of someone else with a reason (D-41) - a draft holds no plan usage, and until it
    is discarded nobody else can make a PPR of that PR (D-05)."""
    rec = _load(uow, caller, ppr_id, for_update=True)
    mine = Role.REQUESTER in caller.roles and rec.created_by_user_id == caller.user_id
    governance = not mine and bool(caller.roles & {Role.PLAN_OFFICER, Role.ADMIN})
    if not governance:
        _require_creator(caller, rec)
    if rec.state is not PprState.DRAFT:
        raise ConflictError("NOT_A_DRAFT", "only a draft can be discarded")
    why = (reason or "").strip()
    if governance and not why:
        raise InvalidInputError("REASON_REQUIRED", "give the reason for discarding this draft")
    uow.pprs.discard(ppr_id)
    after: dict[str, Any] = {"discarded": True, "pr_no": rec.pr_no}
    if governance:
        after |= {"by_governance": True, "created_by": rec.created_by_user_id}
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_DRAFT_DISCARDED",
            "ppr",
            str(ppr_id),
            before=_summary(rec),
            after=after,
            reason=why if governance else None,
        )
    )


# ------------------------------------------------------------------ confirm / reconfirm
def confirm(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    caller: Caller,
    ppr_id: int,
) -> PprRecord:
    rec = _load(uow, caller, ppr_id, for_update=True)
    event = PprEvent.CONFIRM if rec.state is PprState.DRAFT else PprEvent.RECONFIRM
    _authorize(rec.state, event, caller.roles)
    _require_creator(caller, rec)

    header, items = fetch_pr(gateway, rec.pr_no)  # live, never stale (§14.3, §25.1)
    if header is None:
        raise ConflictError("PR_NOT_FOUND", f"PR {rec.pr_no} no longer exists in HOSxP")
    if event is PprEvent.CONFIRM:
        # D-21: compare with the PR as it was when the draft was prepared.
        changes = _changes(rec.draft_pr, header, items)
        if critical := critical_only(changes):
            raise ConflictError(
                "PR_CHANGED_SINCE_DRAFT",
                "the PR changed in HOSxP in a control-sensitive way after this draft was "
                "prepared (" + ", ".join(sorted({c.field for c in critical})) + "); discard "
                "the draft and prepare it again",
                details=_change_details(critical),
            )
        if uow.pprs.binding_state(rec.pr_no) is PrBinding.BOUND:
            raise ConflictError("PR_ALREADY_BOUND", "the PR is already bound to a PPR")
    else:
        # D-22: compare with the PR of the last confirmed version.
        previous = uow.pprs.versions(rec.id)[-1].snapshot
        changes = _changes({"header": previous["pr"], "items": previous["pr_items"]}, header, items)
        if moved := identity_changes(changes):
            raise ConflictError(
                "PR_IDENTITY_CHANGED",
                "the PR's " + ", ".join(sorted(c.field for c in moved)) + " changed in "
                "HOSxP; this PPR cannot be reconfirmed and needs governance review",
                details=_change_details(moved),
            )

    current_fy = fiscal_year_label(today, fy_config)
    own_ref = rec.ppr_number if event is PprEvent.RECONFIRM else None

    # Serialize with plan-year transitions (the year cannot close mid-confirmation) and
    # with every other confirmation drawing on the same Plan Items (§19); then evaluate
    # against the ledger as it is AFTER the locks are held.
    uow.pprs.lock_plan_year_shared(current_fy)
    labels = uow.pprs.chosen_labels(rec.id)  # D-43: the requester's plan row for every line
    chosen = {line: pid for line, (pid, _, _) in labels.items()}
    first = evaluate(
        uow,
        rec.pr_no,
        header,
        items,
        current_fy,
        binding=PrBinding.UNBOUND,
        own_ppr_ref=own_ref,
        choices=chosen,
    )
    locked = {int(p) for p in first.plan_items} | {i.plan_item_id for i in rec.items}
    uow.pprs.lock_plan_items(sorted(locked))
    # D-38: the demand rows this confirmation reads (coverage, A-1 blocking) are locked once,
    # in id order, before the second evaluation - the same order verification and
    # synchronization use, so they never wait on each other in a cycle.
    uow.assigned.lock_demands(
        sorted(locked | set(assigned_services.assigned_items_for_pr(uow, current_fy, header)))
    )
    report = evaluate(
        uow,
        rec.pr_no,
        header,
        items,
        current_fy,
        binding=PrBinding.UNBOUND,
        own_ppr_ref=own_ref,
        choices=chosen,
    )
    if not {int(p) for p in report.plan_items} <= locked:
        raise ConflictError("PLAN_CHANGED_DURING_CONFIRM", "the plan changed; please try again")
    if not report.result.eligible:
        raise _not_eligible(report)
    # D-43: a row renamed or re-united since it was chosen is chosen again, never used blind.
    renamed = [
        {
            "code": "PLAN_ROW_CHANGED",
            "message": f"plan row {pid} was chosen as {name!r} ({unit!r}) and now reads "
            f"{row.item_name!r} ({row.unit!r})",
            "subject": line,
        }
        for line, (pid, name, unit) in sorted(labels.items())
        if (row := report.plan_items.get(str(pid))) is not None
        and (row.item_name.strip(), row.unit.strip()) != (name.strip(), unit.strip())
    ]
    if renamed:
        raise ConflictError(
            "PLAN_ROW_CHANGED",
            "a chosen plan row changed its name or unit since it was chosen: choose it again",
            details=renamed,
        )
    required = report.result.required_amount

    allocations = [CategoryAllocation(a.budget_category_id, a.amount) for a in rec.allocations]
    alloc_problems = list(
        validate_allocations(
            allocations,
            pr_fund_source_id=rec.fund_source_id,
            # Eligibility uses the LIVE PR category (a revision may have changed it).
            category_fund_source=_eligible(
                uow, rec.fiscal_year, rec.fund_source_id, header.budget_category_id
            ),
            required_amount=required,
        )
    )
    if (v := adjustment_reference_violation(allocations, rec.adjustment_reference)) is not None:
        alloc_problems.append(v)
    if alloc_problems:
        raise ConflictError(
            "ALLOCATION_INVALID",
            "; ".join(v.message for v in alloc_problems),
            details=_violations(alloc_problems),
        )

    # D-38 A-2: what each ASSIGNED Plan Item is bought for, checked against the lines.
    coverage = assigned_services.check_for_confirmation(
        uow, rec.id, _assigned_drawn(report), current_fy
    )

    version = rec.current_version + 1
    if event is PprEvent.CONFIRM:
        number = format_ppr_number(current_fy, uow.pprs.next_sequence(current_fy))
        try:
            uow.pprs.bind(rec.pr_no, rec.id)
        except UniquenessError as exc:
            raise ConflictError("PR_ALREADY_BOUND", str(exc)) from exc
    else:
        assert rec.ppr_number is not None
        number = rec.ppr_number

    # Ledger: per Plan Item, release the previous version (reconfirm) then post usage.
    usage: dict[int, tuple[Decimal, Decimal]] = {}
    for line in report.result.lines:
        assert line.matched_plan_item_id is not None
        pid = int(line.matched_plan_item_id)
        q, a = usage.get(pid, (ZERO, ZERO))
        usage[pid] = (q + line.requested_qty, a + line.requested_amount)
    before_after: dict[int, dict[str, Decimal]] = {}
    touched = sorted(set(usage) | {i.plan_item_id for i in rec.items})
    for pid in touched:
        ledger = PlanItemLedger(str(pid), uow.ledger.entries(pid))
        entries: list[LedgerEntry] = []
        if own_ref is not None:
            oq, oa = ledger.outstanding_for_ppr(own_ref)
            if oq > ZERO or oa > ZERO:
                entries.append(
                    LedgerEntry(
                        str(pid),
                        LedgerEventType.PPR_RELEASED,
                        oq,
                        oa,
                        idempotency_key=f"release:{rec.id}:v{rec.current_version}:{pid}",
                        ppr_ref=own_ref,
                    )
                )
        if pid in usage:
            q, a = usage[pid]
            entries.append(
                LedgerEntry(
                    str(pid),
                    LedgerEventType.PPR_CONFIRMED,
                    -q,
                    -a,
                    idempotency_key=f"confirm:{rec.id}:v{version}:{pid}",
                    ppr_ref=number,
                )
            )
        start = ledger.balance
        try:
            for e in entries:
                ledger = ledger.append(e)  # domain rules: never negative (§19)
        except LedgerRuleError as exc:
            raise ConflictError("PLAN_OVERSUBSCRIBED", str(exc)) from exc
        for e in entries:
            uow.ledger.append(e, caller.user_id)
        before_after[pid] = {
            "remaining_qty_before": start.remaining_qty,
            "remaining_amount_before": start.remaining_amount,
            "remaining_qty_after": ledger.balance.remaining_qty,
            "remaining_amount_after": ledger.balance.remaining_amount,
        }

    new_items = _items_from(report)
    uow.pprs.update_header(rec.id, _header_input(header, required))
    uow.pprs.replace_items(rec.id, new_items)
    uow.pprs.mark_confirmed(rec.id, number, version)

    confirmed_at = datetime.now(UTC)
    plans = report.plan_items

    def name(kind: MasterKind, source_id: str | None) -> str | None:
        row = uow.masters.get(kind, source_id) if source_id else None
        return row.name if row else None

    confirmer = uow.users.get(caller.user_id)
    snapshot = _j(
        {
            "ppr_number": number,
            "version": version,
            "event": event.value,
            "confirmed_at": confirmed_at.isoformat(),
            "confirmed_by_user_id": caller.user_id,
            "fiscal_year": current_fy,
            # The PR in the canonical form shared with drafts and sync (pr_snapshot).
            "pr": header_json(header),
            "pr_items": items_json(items),
            # D-21 / D-22: every difference found (a draft can only get here with
            # non-critical ones; a reconfirmation may carry revised lines).
            "pr_changes": _change_details(changes),
            "required_amount": required,
            "items": [
                {
                    "pr_item_id": i.pr_item_id,
                    "item_id": i.item_id,
                    "item_name": i.item_name,
                    "unit": i.unit,
                    "qty": i.qty,
                    "unit_price": i.unit_price,
                    "amount": i.amount,
                    "plan_item_id": i.plan_item_id,
                    "plan_item_code": plans[str(i.plan_item_id)].item_code,
                    "plan_item_name": plans[str(i.plan_item_id)].item_name,
                    "plan_item_unit": plans[str(i.plan_item_id)].unit,  # D-43
                    "planned_qty": plans[str(i.plan_item_id)].planned_qty,
                    "planned_amount": plans[str(i.plan_item_id)].planned_amount,
                    **before_after[i.plan_item_id],
                }
                for i in new_items
            ],
            "allocations": [
                {"budget_category_id": a.budget_category_id, "amount": a.amount}
                for a in rec.allocations
            ],
            "multi_category": is_multi_category(allocations),
            "adjustment_reference": rec.adjustment_reference,
            **({"coverage": assigned_services.coverage_json(coverage)} if coverage else {}),
            "validation": "PASSED",
            # Names as they were at confirmation, so the printed evidence never changes.
            "names": {
                "department": name(MasterKind.DEPARTMENT, rec.department_id),
                "fund_source": name(MasterKind.FUND_SOURCE, rec.fund_source_id),
                "categories": {
                    a.budget_category_id: name(MasterKind.BUDGET_CATEGORY, a.budget_category_id)
                    for a in rec.allocations
                },
                "confirmed_by": (confirmer.display_name or confirmer.username)
                if confirmer
                else None,
            },
        }
    )
    code = document_code(snapshot)
    uow.pprs.add_version(rec.id, version, snapshot, code, caller.user_id)
    assigned_services.confirm_coverage(uow, rec.id, coverage)
    assigned_services.refresh_demands(
        uow, touched, caller.actor, {"ppr_id": rec.id, "event": event.value, "version": version}
    )
    after = uow.pprs.get(rec.id)
    assert after is not None
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_CONFIRMED" if event is PprEvent.CONFIRM else "PPR_RECONFIRMED",
            "ppr",
            str(rec.id),
            before=_summary(rec),
            after={
                **_summary(after),
                "document_code": code,
                "pr_changes": _change_details(changes),
            },
        )
    )
    return after


def _assigned_drawn(report: PrevalidationReport) -> dict[int, Decimal]:
    """Quantity the PR's lines buy on each ASSIGNED Plan Item they match (D-38 A-2)."""
    drawn: dict[int, Decimal] = {}
    for line in report.result.lines:
        mid = line.matched_plan_item_id
        if mid is not None and report.plan_items[mid].plan_type is PlanType.ASSIGNED:
            drawn[int(mid)] = drawn.get(int(mid), ZERO) + line.requested_qty
    return drawn


def _drawn_now(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    rec: PprRecord,
) -> dict[int, Decimal] | None:
    """For a draft or unlocked PPR: what its PR, read live as confirmation will read it, buys
    on ASSIGNED items. None (use the PPR's own lines) when the PPR is not editable, its
    department buys no ASSIGNED item, or HOSxP cannot be read now."""
    if rec.state not in (PprState.DRAFT, PprState.UNLOCKED_FOR_REVISION):
        return None
    current_fy = fiscal_year_label(today, fy_config)
    if not any(
        r.plan_type is PlanType.ASSIGNED and r.purchasing_department_id == rec.department_id
        for r in uow.plans.items(current_fy, rec.department_id)
    ):
        return None
    try:
        header, items = fetch_pr(gateway, rec.pr_no)
    except UpstreamUnavailableError:
        return None
    if header is None:
        return None
    own = rec.ppr_number if rec.state is PprState.UNLOCKED_FOR_REVISION else None
    report = evaluate(
        uow,
        rec.pr_no,
        header,
        items,
        current_fy,
        binding=PrBinding.UNBOUND,
        own_ppr_ref=own,
        choices=uow.pprs.choices(rec.id),
    )
    return _assigned_drawn(report)


def _authorize(state: PprState, event: PprEvent, roles: frozenset[Role]) -> None:
    t = find_transition(state, event)
    if t is None:
        raise ConflictError("INVALID_STATE", f"{event.value} is not allowed in state {state.value}")
    role = next((r for r in sorted(roles) if r in t.roles), None)
    if role is None:
        raise ForbiddenError(
            "ROLE_NOT_ALLOWED",
            f"{event.value} needs one of: " + ", ".join(sorted(r.value for r in t.roles)),
        )
    try:
        apply_event(state, event, StateActor.user(role))
    except TransitionError as exc:  # pragma: no cover - guarded above
        raise ConflictError("INVALID_STATE", str(exc)) from exc


# ------------------------------------------------------------------ unlock
def unlock(uow: UnitOfWork, caller: Caller, ppr_id: int, reason: str | None) -> PprRecord:
    rec = _load(uow, caller, ppr_id, for_update=True)
    t = find_transition(rec.state, PprEvent.UNLOCK)
    if t is None:
        raise ConflictError("INVALID_STATE", f"a {rec.state.value} PPR cannot be unlocked")
    role = next((r for r in sorted(caller.roles) if r in t.roles), None)
    if role is None:
        raise ForbiddenError("ROLE_NOT_ALLOWED", "only Plan Officers or Admins unlock (§15.2)")
    try:
        target, _ = apply_event(rec.state, PprEvent.UNLOCK, StateActor.user(role), reason=reason)
    except TransitionError as exc:
        raise InvalidInputError("REASON_REQUIRED", str(exc)) from exc
    uow.pprs.set_state(rec.id, target)
    assigned_services.start_revision(uow, rec.id)  # D-38 A-5: demands keep their state
    uow.audit.write(
        AuditEntry(
            caller.actor,
            "PPR_UNLOCKED",
            "ppr",
            str(rec.id),
            before={"state": rec.state.value, "version": rec.current_version},
            after={"state": target.value, "version": rec.current_version},
            reason=(reason or "").strip(),
        )
    )
    after = uow.pprs.get(rec.id)
    assert after is not None
    return after


# ------------------------------------------------------------------ reads
def coverage(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    caller: Caller,
    ppr_id: int,
) -> list[assigned_services.ItemCoverage]:
    """D-38 A-2: whom the PPR's Assigned Purchase lines are bought for."""
    rec = _load(uow, caller, ppr_id, for_update=False)
    return assigned_services.coverage_view(
        uow, rec, _drawn_now(uow, gateway, fy_config, today, rec)
    )


def set_coverage(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    caller: Caller,
    ppr_id: int,
    wanted: Mapping[int, Mapping[str, Decimal]],
) -> list[assigned_services.ItemCoverage]:
    rec = _load(uow, caller, ppr_id, for_update=True)
    _require_creator(caller, rec)
    drawn = _drawn_now(uow, gateway, fy_config, today, rec)
    assigned_services.set_coverage(uow, caller.actor, rec, wanted, drawn)
    return assigned_services.coverage_view(uow, rec, drawn)


def get_ppr(uow: UnitOfWork, caller: Caller, ppr_id: int) -> PprRecord:
    return _load(uow, caller, ppr_id, for_update=False)


def list_pprs(uow: UnitOfWork, caller: Caller, limit: int = 100) -> list[PprRecord]:
    return uow.pprs.search(caller.ppr_scope, limit)


def versions(uow: UnitOfWork, caller: Caller, ppr_id: int) -> list[PprVersionRecord]:
    _load(uow, caller, ppr_id, for_update=False)
    return uow.pprs.versions(ppr_id)
