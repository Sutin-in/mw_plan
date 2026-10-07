"""Wave 3 use case: live PR retrieval and Pre-PPR validation (spec §11, §12, §13, §25.1, §26).

* The PR header and ALL its items are read live from HOSxP through ``HosxpGateway`` on
  every call. Nothing is cached, so a PPR can never be based on stale PR data; if HOSxP
  cannot be reached the call fails (``UpstreamUnavailableError``) - it never falls back.
* Pre-validation is read-only: it writes nothing (no PPR, no draft, no ledger, no audit
  row - pre-validation is not one of the audited actions in §36).
* Rules applied (in ``domain.prevalidation``): the plan row the requester chose for each
  line among the rows offered (D-43), quantity AND amount hard control against the ledger-derived
  remaining balance (lines on the same Plan Item in aggregate), D-17 required amount /
  header-total check, D-18 fiscal-year validity, all-or-none.
* Any requester may validate a PR of any department (D-41).

PR binding (D-03/D-05) comes from the PPR tables (Wave 4): a PR already bound to a
confirmed PPR, or with a working draft, is reported as such.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from ppr.application.assigned_services import blocked_for
from ppr.application.errors import UpstreamUnavailableError
from ppr.domain.fiscal_year import FiscalYearConfig, PlanYearState, fiscal_year_label
from ppr.domain.hard_control import Remaining
from ppr.domain.ledger import PlanItemLedger
from ppr.domain.plan import match_department
from ppr.domain.pr_binding import PrBinding
from ppr.domain.prevalidation import (
    PlanItemCandidate,
    PrevalidationContext,
    PrevalidationResult,
    PrLine,
    PrRequest,
    prevalidate,
)
from ppr.integration.hosxp.contracts import PrHeader, PrItem
from ppr.integration.hosxp.errors import HosxpUnavailableError
from ppr.integration.hosxp.gateway import HosxpGateway
from ppr.integration.hosxp.status import HosxpPrStatus
from ppr.ports import MasterKind, PlanItemRecord, UnitOfWork


@dataclass(frozen=True)
class PrevalidationReport:
    """What the caller sees: the live PR as retrieved, and the validation result."""

    pr_no: str
    header: PrHeader | None  # None = the PR does not exist in HOSxP
    items: tuple[PrItem, ...]
    current_fiscal_year: int
    result: PrevalidationResult
    plan_items: dict[str, PlanItemRecord]  # chosen plan items by id, for display
    # D-43: every plan row offered to some line, and its remaining balance, for the choice
    offered: dict[str, PlanItemRecord] = field(default_factory=dict)
    offered_remaining: dict[str, Remaining] = field(default_factory=dict)


def fetch_pr(gateway: HosxpGateway, pr_no: str) -> tuple[PrHeader | None, tuple[PrItem, ...]]:
    """Live PR header and ALL items (spec §12, §25.1); never cached, never stale."""
    try:
        header = gateway.get_pr(pr_no)
        items = tuple(gateway.get_pr_items(pr_no)) if header is not None else ()
    except HosxpUnavailableError as exc:
        raise UpstreamUnavailableError(
            "HOSXP_UNAVAILABLE", "HOSxP is unavailable; a PPR cannot be based on stale data"
        ) from exc
    return header, items


def prevalidate_pr(
    uow: UnitOfWork,
    gateway: HosxpGateway,
    fy_config: FiscalYearConfig,
    today: date,
    pr_no: str,
    choices: Mapping[str, int] | None = None,
) -> PrevalidationReport:
    """Retrieve PR ``pr_no`` live and validate it for a PPR with the requester's plan-row
    choices (D-43). Any department's PR (D-41): the PPR takes the PR's department."""
    header, items = fetch_pr(gateway, pr_no)
    return evaluate(
        uow,
        pr_no,
        header,
        items,
        fiscal_year_label(today, fy_config),
        binding=uow.pprs.binding_state(pr_no),
        choices=choices,
    )


def evaluate(
    uow: UnitOfWork,
    pr_no: str,
    header: PrHeader | None,
    items: tuple[PrItem, ...],
    current_fy: int,
    *,
    binding: PrBinding,
    own_ppr_ref: str | None = None,
    choices: Mapping[str, int] | None = None,
) -> PrevalidationReport:
    """Validate retrieved PR data against the current plan and ledger.

    ``own_ppr_ref`` (reconfirmation): that PPR's own held usage is added back to the
    remaining balance, so a PPR is never blocked by itself.
    """
    if header is None:
        result = prevalidate(None, _empty_context(current_fy), pr_no=pr_no)
        return PrevalidationReport(pr_no, None, (), current_fy, result, {})

    def unit_of(i: PrItem) -> str | None:
        # D-43: the PR line's own unit, else the HOSxP item's; None when neither is known.
        if i.unit and i.unit.strip():
            return i.unit
        master = uow.masters.get(MasterKind.ITEM, i.item_id)
        return master.unit if master is not None and master.unit else None

    request = PrRequest(
        header.pr_no,
        header.department_id,
        header.fund_source_id,
        tuple(
            PrLine(i.pr_item_id, i.item_id, i.qty, i.amount, i.unit_price, unit_of(i))
            for i in items
        ),
        fiscal_year=header.fiscal_year,
        header_total=header.total_amount,
        cancelled_in_hosxp=header.status is HosxpPrStatus.CANCELLED,
    )

    year = uow.plan_years.get(current_fy)
    candidates: dict[str, PlanItemRecord] = {}
    blocked: frozenset[tuple[str, str]] = frozenset()
    if (
        year is not None
        and year.state is PlanYearState.ACTIVE
        and header.fiscal_year == current_fy
        and header.department_id is not None
    ):
        visible = uow.plans.items(current_fy, header.department_id)
        # D-38 A-1: an item / fund the department has planned or included demand on is
        # bought by the purchasing department, not by the department itself.
        blocked = blocked_for(visible, header.department_id)
        for rec in visible:
            # D-38 / D-43: DEPARTMENT items answer their owner's PRs, CENTRAL and ASSIGNED
            # items their purchasing department's; the requester chooses among them.
            dept = match_department(
                rec.plan_type, rec.owner_department_id, rec.purchasing_department_id
            )
            if dept == header.department_id and rec.fund_source_id == header.fund_source_id:
                candidates[str(rec.id)] = rec

    plan_candidates = []
    remaining: dict[str, Remaining] = {}
    for pid, rec in candidates.items():
        ledger = PlanItemLedger(pid, uow.ledger.entries(rec.id))
        bal = ledger.balance
        own_q, own_a = ledger.outstanding_for_ppr(own_ppr_ref) if own_ppr_ref else (0, 0)
        remaining[pid] = Remaining(bal.remaining_qty + own_q, bal.remaining_amount + own_a)
        plan_candidates.append(
            PlanItemCandidate(
                pid,
                rec.item_id,
                header.department_id or "",  # the department it answers (owner / purchaser)
                rec.fund_source_id,
                remaining[pid],
                rec.unit,
            )
        )

    ctx = PrevalidationContext(
        year.state if year is not None else None,
        binding,
        frozenset(r.source_id for r in uow.masters.list(MasterKind.FUND_SOURCE) if r.active),
        tuple(plan_candidates),
        current_fiscal_year=current_fy,
        blocked=blocked,
        choices={k: str(v) for k, v in (choices or {}).items()},
    )
    result = prevalidate(request, ctx, pr_no=pr_no)
    matched = {
        line.matched_plan_item_id: candidates[line.matched_plan_item_id]
        for line in result.lines
        if line.matched_plan_item_id is not None
    }
    offered = {pid: candidates[pid] for line in result.lines for pid in line.offered}
    return PrevalidationReport(
        pr_no,
        header,
        items,
        current_fy,
        result,
        matched,
        offered,
        {pid: remaining[pid] for pid in offered},
    )


def _empty_context(current_fy: int) -> PrevalidationContext:
    return PrevalidationContext(
        None, PrBinding.UNBOUND, frozenset(), (), current_fiscal_year=current_fy
    )
