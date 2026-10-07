"""Pre-PPR validation with the all-or-none rule (spec §12, §13, D-03, D-05, D-16-D-18).

If even ONE PR item fails, the result is not eligible and no PPR may be created.
Every item is still evaluated so the UI can explain each failure.

Inputs are domain values; ``application.pr_services`` converts HOSxP contracts into
them. Plan row (D-43, superseding the D-16 automatic match): the requester chooses, for every
PR line, the Plan Item it uses among the rows offered - the current fiscal year's rows that
answer the PR's department (owner of a DEPARTMENT item, purchaser of a CENTRAL / ASSIGNED
item, D-38) and fund source, counted in the line's unit. None offered -> NOT_IN_PLAN; no
choice -> PLAN_ROW_NOT_CHOSEN; a choice outside them -> PLAN_ROW_NOT_ALLOWED. A department
with open demand on an Assigned Purchase item that carries a HOSxP item cannot buy that
item itself (D-38 A-1, DEMAND_IN_CENTRAL_PURCHASE).

Fiscal year (D-18): the PR's budget year must be the current fiscal year and that
year's plan must be ACTIVE; a plan expires when its fiscal year ends.
Required amount (D-17): the sum of the PR line amounts; the PR header total must equal
it exactly, otherwise the PR is not eligible.

Several PR lines that choose the same Plan Item are checked against that Plan Item's
remaining balance in aggregate, so split lines cannot bypass the hard control.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import KW_ONLY, dataclass, field, replace
from decimal import Decimal

from ppr.domain.fiscal_year import PlanYearState, new_ppr_allowed
from ppr.domain.hard_control import Remaining, check_hard_control
from ppr.domain.pr_binding import PrBinding, binding_violation
from ppr.domain.violations import Violation, ViolationCode


@dataclass(frozen=True)
class PrLine:
    pr_item_id: str
    item_id: str
    qty: Decimal
    amount: Decimal
    unit_price: Decimal | None = None  # checked for precision only (D-23); never a control
    unit: str | None = None  # D-43: the line's unit (PR line, else HOSxP item); None unknown


@dataclass(frozen=True)
class PrRequest:
    pr_no: str
    department_id: str | None
    fund_source_id: str | None
    lines: tuple[PrLine, ...]
    _: KW_ONLY
    fiscal_year: int | None  # the PR's budget year as shown in HOSxP (D-18)
    header_total: Decimal | None  # the PR header total (D-17)
    cancelled_in_hosxp: bool  # normalized HOSxP status is CANCELLED (A-13)

    @property
    def lines_total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))


@dataclass(frozen=True)
class PlanItemCandidate:
    plan_item_id: str
    item_id: str | None  # the row's HOSxP item, if any (D-43: never used to match)
    department_id: str  # the department whose PRs it answers (owner / purchaser, D-38)
    fund_source_id: str
    remaining: Remaining
    unit: str = ""  # the row's unit; offered only to a line counted in the same unit


@dataclass(frozen=True)
class PrevalidationContext:
    fiscal_year_state: PlanYearState | None  # state of the CURRENT year's plan; None = none
    binding: PrBinding
    known_fund_source_ids: frozenset[str]
    plan_items: tuple[PlanItemCandidate, ...]  # items of the current year a PR line may match
    _: KW_ONLY
    current_fiscal_year: int  # from the configured boundaries and the server date (D-08)
    # D-38 A-1: (HOSxP item, fund source) the PR's department may not buy while its demand on
    # an Assigned Purchase item bought by another department is open - by any line, including
    # one matched to an Assigned Purchase item the department buys itself (product owner,
    # 2026-10-01: one demand is never claimed by two purchase lines at the same time).
    blocked: frozenset[tuple[str, str]] = frozenset()
    # D-43: the plan row the requester chose for each PR line (pr_item_id -> plan_item_id)
    choices: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LineResult:
    pr_item_id: str
    item_id: str
    requested_qty: Decimal
    requested_amount: Decimal
    matched_plan_item_id: str | None
    remaining: Remaining | None
    violations: tuple[Violation, ...]
    offered: tuple[str, ...] = ()  # D-43: the plan rows this line may choose

    @property
    def passed(self) -> bool:
        return not self.violations


@dataclass(frozen=True)
class PrevalidationResult:
    pr_no: str
    header_violations: tuple[Violation, ...]
    lines: tuple[LineResult, ...]
    required_amount: Decimal = Decimal(0)  # D-17: sum of the PR line amounts
    lines_checked: bool = True  # False when the fiscal year/plan itself is not usable

    @property
    def eligible(self) -> bool:
        """All-or-none: eligible only if the header and EVERY line pass."""
        return not self.header_violations and all(line.passed for line in self.lines)

    def all_violations(self) -> tuple[Violation, ...]:
        out = list(self.header_violations)
        for line in self.lines:
            out.extend(line.violations)
        return tuple(out)

    def ensure_eligible(self) -> None:
        if not self.eligible:
            raise PrevalidationFailedError(self)


class PrevalidationFailedError(Exception):
    def __init__(self, result: PrevalidationResult) -> None:
        super().__init__(
            f"PR {result.pr_no} is not eligible for a PPR: "
            + ", ".join(sorted({v.code for v in result.all_violations()}))
        )
        self.result = result


def prevalidate(
    pr: PrRequest | None, ctx: PrevalidationContext, *, pr_no: str
) -> PrevalidationResult:
    if pr is None:
        return PrevalidationResult(
            pr_no,
            (Violation(ViolationCode.PR_NOT_FOUND, f"PR {pr_no} was not found in HOSxP", pr_no),),
            (),
        )

    header: list[Violation] = []
    fy_violations = _fiscal_year_violations(pr, ctx)
    header.extend(fy_violations)
    if pr.cancelled_in_hosxp:
        header.append(
            Violation(ViolationCode.PR_CANCELLED_IN_HOSXP, "the PR is cancelled in HOSxP", pr.pr_no)
        )
    if (bv := binding_violation(ctx.binding, pr.pr_no)) is not None:
        header.append(bv)
    if pr.department_id is None:
        header.append(
            Violation(ViolationCode.PR_MISSING_DEPARTMENT, "the PR has no department", pr.pr_no)
        )
    if pr.fund_source_id is None or pr.fund_source_id not in ctx.known_fund_source_ids:
        what = (
            "has no fund source"
            if pr.fund_source_id is None
            else (f"fund source {pr.fund_source_id!r} is not a known HOSxP fund source")
        )
        header.append(Violation(ViolationCode.UNKNOWN_FUND_SOURCE, f"the PR {what}", pr.pr_no))
    if not pr.lines:
        header.append(Violation(ViolationCode.PR_HAS_NO_ITEMS, "PR has no items", pr.pr_no))
    else:
        header.extend(total_violations(pr))

    if fy_violations:
        # No usable plan for this PR: checking its lines against a plan would only
        # produce misleading NOT_IN_PLAN / EXHAUSTED messages. The header says why.
        unchecked = tuple(
            LineResult(ln.pr_item_id, ln.item_id, ln.qty, ln.amount, None, None, ())
            for ln in pr.lines
        )
        return PrevalidationResult(
            pr.pr_no, tuple(header), unchecked, pr.lines_total, lines_checked=False
        )
    lines = _validate_lines(pr, ctx.plan_items, ctx.choices)
    if ctx.blocked:
        lines = tuple(_blocked(pr, line, ctx) for line in lines)
    return PrevalidationResult(pr.pr_no, tuple(header), lines, pr.lines_total)


def _blocked(pr: PrRequest, line: LineResult, ctx: PrevalidationContext) -> LineResult:
    """D-38 A-1: a department whose demand on an Assigned Purchase item of another purchaser is
    still open (planned or included) cannot buy that item itself (same HOSxP item and fund
    source), through its own plan or through an Assigned Purchase item it buys."""
    if (line.item_id, pr.fund_source_id or "") not in ctx.blocked:
        return line
    v = Violation(
        ViolationCode.DEMAND_IN_CENTRAL_PURCHASE,
        f"item {line.item_id} is bought for this department through an Assigned Purchase "
        "(its demand is still open); it cannot be bought separately (D-38)",
        line.pr_item_id,
    )
    return replace(line, violations=(*line.violations, v))


def _fiscal_year_violations(pr: PrRequest, ctx: PrevalidationContext) -> list[Violation]:
    """D-18: the PR's budget year must be the current fiscal year, with an ACTIVE plan."""
    current = ctx.current_fiscal_year
    if pr.fiscal_year is None:
        return [Violation(ViolationCode.FISCAL_YEAR_UNKNOWN, "the PR has no budget year", pr.pr_no)]
    if pr.fiscal_year < current:
        return [
            Violation(
                ViolationCode.PLAN_YEAR_EXPIRED,
                f"the PR budget year is {pr.fiscal_year}; the {pr.fiscal_year} plan expired "
                f"when its fiscal year ended (current fiscal year {current})",
                pr.pr_no,
            )
        ]
    if pr.fiscal_year > current:
        return [
            Violation(
                ViolationCode.FISCAL_YEAR_NOT_CURRENT,
                f"the PR budget year {pr.fiscal_year} is not the current fiscal year {current}",
                pr.pr_no,
            )
        ]
    if ctx.fiscal_year_state is None:
        return [
            Violation(
                ViolationCode.PLAN_YEAR_NOT_FOUND,
                f"there is no plan for fiscal year {current}",
                pr.pr_no,
            )
        ]
    if not new_ppr_allowed(ctx.fiscal_year_state):
        return [
            Violation(
                ViolationCode.FISCAL_YEAR_NOT_ACTIVE,
                f"the {current} plan is {ctx.fiscal_year_state}, not ACTIVE",
                pr.pr_no,
            )
        ]
    return []


def total_violations(pr: PrRequest) -> list[Violation]:
    """D-17: the header total must equal the sum of the line amounts, exactly."""
    if pr.header_total is None:
        return [
            Violation(
                ViolationCode.PR_TOTAL_UNAVAILABLE,
                "the PR header total is not available, so it cannot be checked",
                pr.pr_no,
            )
        ]
    if pr.header_total != pr.lines_total:
        return [
            Violation(
                ViolationCode.PR_TOTAL_MISMATCH,
                f"the PR header total {pr.header_total} differs from the sum of its items "
                f"{pr.lines_total}; correct the PR in HOSxP first",
                pr.pr_no,
            )
        ]
    return []


def _same_unit(a: str | None, b: str) -> bool:
    return a is not None and a.strip() != "" and a.strip() == b.strip()


def _offered(
    pr: PrRequest, line: PrLine, plan_items: Iterable[PlanItemCandidate]
) -> list[PlanItemCandidate]:
    """D-43: the rows a line may choose - the PR's department and fund source, same unit."""
    return [
        p
        for p in plan_items
        if p.department_id == pr.department_id
        and p.fund_source_id == pr.fund_source_id
        and _same_unit(line.unit, p.unit)
    ]


def _validate_lines(
    pr: PrRequest,
    plan_items: tuple[PlanItemCandidate, ...],
    choices: Mapping[str, str],
) -> tuple[LineResult, ...]:
    seen: set[str] = set()
    per_line: list[tuple[PrLine, PlanItemCandidate | None, list[Violation], tuple[str, ...]]] = []
    totals: dict[str, tuple[Decimal, Decimal]] = {}

    for line in pr.lines:
        vs: list[Violation] = []
        too_precise = precision_problems(line.qty, line.amount, line.unit_price)
        if too_precise:
            vs.append(
                Violation(
                    ViolationCode.PR_VALUE_PRECISION,
                    f"PR item {line.pr_item_id}: {', '.join(too_precise)} has more decimals "
                    "than supported (quantity 4, amount 2, unit price 8); values are never "
                    "rounded (D-23)",
                    line.pr_item_id,
                )
            )
        if line.pr_item_id in seen:
            vs.append(
                Violation(
                    ViolationCode.DUPLICATE_PR_ITEM,
                    f"PR item {line.pr_item_id} appears more than once",
                    line.pr_item_id,
                )
            )
        seen.add(line.pr_item_id)

        offered = _offered(pr, line, plan_items)
        by_id = {p.plan_item_id: p for p in offered}
        chosen = choices.get(line.pr_item_id)
        matched: PlanItemCandidate | None = None
        if not offered:
            unit = (line.unit or "").strip() or "?"
            vs.append(
                Violation(
                    ViolationCode.NOT_IN_PLAN,
                    f"no plan row of this department and fund source is counted in {unit!r} "
                    f"(PR item {line.pr_item_id}, HOSxP item {line.item_id})",
                    line.pr_item_id,
                )
            )
            if chosen is not None:  # a choice is never kept for a line offered nothing
                vs.append(
                    Violation(
                        ViolationCode.PLAN_ROW_NOT_ALLOWED,
                        f"plan row {chosen} is not one PR item {line.pr_item_id} may use "
                        "(no row is offered to it)",
                        line.pr_item_id,
                    )
                )
        elif chosen is None:
            vs.append(
                Violation(
                    ViolationCode.PLAN_ROW_NOT_CHOSEN,
                    f"choose the plan row PR item {line.pr_item_id} uses (D-43)",
                    line.pr_item_id,
                )
            )
        elif chosen not in by_id:
            vs.append(
                Violation(
                    ViolationCode.PLAN_ROW_NOT_ALLOWED,
                    f"plan row {chosen} is not one PR item {line.pr_item_id} may use (another "
                    "department, fund source or unit, or not in the active plan)",
                    line.pr_item_id,
                )
            )
        else:
            matched = by_id[chosen]
            q, a = totals.get(matched.plan_item_id, (Decimal(0), Decimal(0)))
            totals[matched.plan_item_id] = (q + line.qty, a + line.amount)
        per_line.append((line, matched, vs, tuple(p.plan_item_id for p in offered)))

    results: list[LineResult] = []
    for line, matched, vs, offered_ids in per_line:
        remaining = matched.remaining if matched else None
        if matched is not None:
            # Validate the line itself (sign checks) and the aggregate on its plan item.
            vs.extend(
                v
                for v in check_hard_control(
                    line.qty, line.amount, matched.remaining, subject=line.pr_item_id
                )
                if v.code
                in (ViolationCode.INVALID_REQUESTED_QTY, ViolationCode.INVALID_REQUESTED_AMOUNT)
            )
            if not vs:
                agg_qty, agg_amount = totals[matched.plan_item_id]
                vs.extend(
                    check_hard_control(
                        agg_qty, agg_amount, matched.remaining, subject=line.pr_item_id
                    )
                )
        results.append(
            LineResult(
                line.pr_item_id,
                line.item_id,
                line.qty,
                line.amount,
                matched.plan_item_id if matched else None,
                remaining,
                tuple(vs),
                offered_ids,
            )
        )
    return tuple(results)


QTY_STEP = Decimal("0.0001")  # the plan ledger records quantities with 4 decimals
MONEY_STEP = Decimal("0.01")  # and money with 2
PRICE_STEP = Decimal("0.00000001")  # unit prices are preserved up to 8 decimals (D-23)


def _fits(value: Decimal, step: Decimal) -> bool:
    """True when ``value`` can be stored at ``step`` precision without rounding."""
    return value.is_finite() and value == value.quantize(step)


def precision_problems(
    qty: Decimal | None, amount: Decimal | None, unit_price: Decimal | None
) -> list[str]:
    """D-23: the names of the values that do not fit the supported precision (never
    rounded). Shared by pre-validation/confirmation and PR synchronization."""
    return [
        name
        for name, value, step in (
            ("quantity", qty, QTY_STEP),
            ("amount", amount, MONEY_STEP),
            ("unit price", unit_price, PRICE_STEP),
        )
        if value is not None and not _fits(value, step)
    ]


def check_lines_originate_from_hosxp(
    submitted_pr_item_ids: Iterable[str], hosxp_pr_item_ids: Iterable[str]
) -> tuple[Violation, ...]:
    """PR items may only come from HOSxP; manual additions are rejected (spec §11.4)."""
    allowed = set(hosxp_pr_item_ids)
    return tuple(
        Violation(
            ViolationCode.MANUAL_PR_ITEM,
            f"PR item {pid} does not exist in the HOSxP PR; items cannot be added manually",
            pid,
        )
        for pid in submitted_pr_item_ids
        if pid not in allowed
    )
