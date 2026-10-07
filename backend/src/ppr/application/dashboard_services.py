"""Wave 8A: dashboards, derived in-app alerts and their settings (spec §31, §34; D-32, D-33).

Read only: nothing here posts to the ledger, changes a PPR or plan state, or calls HOSxP.
The one write is an ADMIN changing an alert threshold (audited, §36).

Scopes (§22.1) are exactly those of reading the underlying record:
* PPR figures and PPR alerts: ``Caller.ppr_scope`` (as search and the queue; D-41: a
  REQUESTER-only user counts the PPRs they created);
* plan figures and plan alerts: the Plan Item department filter of ``PlanRepository.items``
  (owner, purchaser or demand department) - the same scope as ``GET /api/plan-years/{fy}/items``;
* ``SYNC_FAILURE`` only for callers that follow synchronization.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ppr.application.errors import InvalidInputError
from ppr.application.ppr_services import Caller
from ppr.application.sync_services import failure_status
from ppr.domain.alerts import (
    INACTIVITY_STATES,
    SETTING_RANGES,
    AlertSetting,
    AlertType,
    PlanLevel,
    in_range,
    inactive_cutoff,
    plan_level,
    sync_failed,
)
from ppr.domain.fiscal_year import PlanYearState
from ppr.domain.ledger import LedgerBalance
from ppr.domain.plan import responsible_department
from ppr.domain.ppr_state import PprState
from ppr.ports import (
    Actor,
    AlertSettingRecord,
    AuditEntry,
    PlanItemRecord,
    PprRecord,
    PprSearch,
    SyncRunRecord,
    UnitOfWork,
)

ZERO = Decimal(0)
ALERT_LIST_LIMIT = 500  # per PPR alert type; the counts are always exact
NEAR_EXHAUSTION_LIMIT = 20


# ------------------------------------------------------------------ settings (D-33)
def alert_settings(uow: UnitOfWork) -> dict[AlertSetting, AlertSettingRecord]:
    return uow.alert_settings.all()


def inactive_days(uow: UnitOfWork) -> int:
    return uow.alert_settings.all()[AlertSetting.PPR_INACTIVE_DAYS].value


def set_alert_setting(
    uow: UnitOfWork, actor: Actor, code: AlertSetting, value: int, reason: str | None
) -> AlertSettingRecord:
    assert actor.user_id is not None
    reason = (reason or "").strip()
    if not reason:
        raise InvalidInputError("REASON_REQUIRED", "changing an alert threshold needs a reason")
    if not in_range(code, value):
        low, high = SETTING_RANGES[code]
        raise InvalidInputError(
            "SETTING_OUT_OF_RANGE", f"{code.value} must be between {low} and {high}"
        )
    before = uow.alert_settings.all()[code]
    uow.alert_settings.set(code, value, actor.user_id)
    after = uow.alert_settings.all()[code]
    uow.audit.write(
        AuditEntry(
            actor,
            "ALERT_SETTING_CHANGED",
            "alert_setting",
            code.value,
            before={"value": before.value},
            after={"value": after.value},
            reason=reason,
        )
    )
    return after


# ------------------------------------------------------------------ plan balances
@dataclass(frozen=True)
class ItemStatus:
    """One Plan Item with its balance. Before the plan year is ACTIVE there is no ledger
    yet: the planned values are shown and nothing is used."""

    item: PlanItemRecord
    year_state: PlanYearState
    planned_qty: Decimal  # approved + amendments (current plan)
    planned_amount: Decimal
    used_qty: Decimal  # confirmed PPR usage minus releases (never stock movements, D-39)
    used_amount: Decimal
    remaining_qty: Decimal
    remaining_amount: Decimal
    level: PlanLevel
    balance: LedgerBalance | None


def _status(
    rec: PlanItemRecord, state: PlanYearState, b: LedgerBalance | None, low_percent: int
) -> ItemStatus:
    if b is None:
        return ItemStatus(
            rec,
            state,
            rec.planned_qty,
            rec.planned_amount,
            ZERO,
            ZERO,
            rec.planned_qty,
            rec.planned_amount,
            PlanLevel(None, None, None),
            None,
        )
    planned_q = b.approved_qty + b.amendment_qty
    planned_a = b.approved_amount + b.amendment_amount
    # Alerts only for a plan in use (D-33); a CLOSED year shows its balances, no alerts.
    active = state is PlanYearState.ACTIVE
    level = plan_level(b, low_percent) if active else PlanLevel(None, None, None)
    return ItemStatus(
        rec,
        state,
        planned_q,
        planned_a,
        planned_q - b.remaining_qty,
        planned_a - b.remaining_amount,
        b.remaining_qty,
        b.remaining_amount,
        level,
        b,
    )


def item_statuses(
    uow: UnitOfWork, fiscal_year: int, plan_scope: str | None, low_percent: int | None = None
) -> list[ItemStatus]:
    """Every Plan Item of the year the caller may read, with its balance."""
    year = uow.plan_years.get(fiscal_year)
    if year is None:
        return []
    if low_percent is None:
        low_percent = uow.alert_settings.all()[AlertSetting.PLAN_LOW_PERCENT].value
    items = uow.plans.items(fiscal_year, department_id=plan_scope)
    balances = uow.ledger.balances([r.id for r in items])
    return [_status(r, year.state, balances.get(r.id), low_percent) for r in items]


# ------------------------------------------------------------------ alerts (D-33)
@dataclass(frozen=True)
class Alert:
    type: AlertType
    subject: str  # "ppr" | "plan_item" | "sync_run"
    subject_id: int
    fiscal_year: int | None = None
    department_id: str | None = None
    since: datetime | None = None
    # PPR alerts
    ppr_number: str | None = None
    pr_no: str | None = None
    state: str | None = None
    days_inactive: int | None = None
    # plan alerts
    item_id: str | None = None
    item_code: str | None = None
    item_name: str | None = None
    unit: str | None = None
    remaining_qty: Decimal | None = None
    remaining_amount: Decimal | None = None
    remaining_qty_percent: Decimal | None = None
    remaining_amount_percent: Decimal | None = None
    # sync alerts
    sync_scope: str | None = None
    sync_status: str | None = None
    sync_failed: int | None = None  # D-36: failed PPRs of the run
    sync_pending: int | None = None  # D-36: of which still outstanding


@dataclass(frozen=True)
class AlertList:
    alerts: list[Alert]
    counts: dict[str, int]
    inactive_days: int
    low_percent: int
    truncated: bool = False


def _ppr_alert(t: AlertType, p: PprRecord, now: datetime) -> Alert:
    return Alert(
        type=t,
        subject="ppr",
        subject_id=p.id,
        fiscal_year=p.fiscal_year,
        department_id=p.department_id,
        since=p.updated_at,
        ppr_number=p.ppr_number,
        pr_no=p.pr_no,
        state=p.state.value,
        days_inactive=max(0, (now - p.updated_at).days),
    )


def _plan_alerts(statuses: Iterable[ItemStatus]) -> list[Alert]:
    out = []
    for s in statuses:
        if s.level.alert is None:
            continue
        r = s.item
        out.append(
            Alert(
                type=s.level.alert,
                subject="plan_item",
                subject_id=r.id,
                fiscal_year=r.fiscal_year,
                department_id=responsible_department(
                    r.plan_type, r.owner_department_id, r.purchasing_department_id
                ),
                item_id=r.item_id,
                item_code=r.item_code,
                item_name=r.item_name,
                unit=r.unit,
                remaining_qty=s.remaining_qty,
                remaining_amount=s.remaining_amount,
                remaining_qty_percent=s.level.remaining_qty_percent,
                remaining_amount_percent=s.level.remaining_amount_percent,
            )
        )
    # Exhausted first, then the lowest remaining share.
    out.sort(key=lambda a: (a.type is not AlertType.PLAN_EXHAUSTED, _lowest(a), a.subject_id))
    return out


def _lowest(a: Alert) -> Decimal:
    pcts = [p for p in (a.remaining_qty_percent, a.remaining_amount_percent) if p is not None]
    return min(pcts) if pcts else Decimal(100)


def _sync_alert(uow: UnitOfWork, run: SyncRunRecord | None, *, masters: bool) -> Alert | None:
    if run is None or not sync_failed(run.status, masters=masters):
        return None
    failed = pending = None
    if not masters:
        # D-36: a PR-sync failure clears when every failed PPR was resolved by later syncs.
        fs = failure_status(uow, run)
        if fs.failed and not fs.remaining:
            return None
        failed, pending = len(fs.failed), len(fs.remaining)
    return Alert(
        type=AlertType.SYNC_FAILURE,
        subject="sync_run",
        subject_id=run.id,
        since=run.finished_at or run.started_at,
        sync_scope=run.scope,
        sync_status=run.status,
        sync_failed=failed,
        sync_pending=pending,
    )


def active_years(uow: UnitOfWork) -> list[int]:
    return [y.fiscal_year for y in uow.plan_years.list() if y.state is PlanYearState.ACTIVE]


def alerts(
    uow: UnitOfWork,
    caller: Caller,
    plan_scope: str | None,
    sees_sync: bool,
    now: datetime,
    *,
    with_lists: bool = True,
    statuses_cache: dict[int, list[ItemStatus]] | None = None,
) -> AlertList:
    """Every alert the caller may see (D-33). Counts are always exact; each list is capped
    at ``ALERT_LIST_LIMIT`` (the longest-idle PPRs first). ``with_lists=False`` returns the
    counts only (dashboard). ``statuses_cache`` reuses plan balances already computed."""
    settings = uow.alert_settings.all()
    days = settings[AlertSetting.PPR_INACTIVE_DAYS].value
    low = settings[AlertSetting.PLAN_LOW_PERCENT].value
    scope = caller.ppr_scope
    cutoff = inactive_cutoff(now, days)

    inactive_q = PprSearch(
        states=tuple(sorted(INACTIVITY_STATES)),
        inactive_before=cutoff,
        limit=ALERT_LIST_LIMIT,
        oldest_first=True,
    )
    changed_q = PprSearch(
        states=(PprState.PR_CHANGED_REVIEW_REQUIRED,), limit=ALERT_LIST_LIMIT, oldest_first=True
    )
    counts = {t.value: 0 for t in AlertType}
    counts[AlertType.PPR_INACTIVE.value] = uow.pprs.count(inactive_q, scope)
    counts[AlertType.PR_CHANGED.value] = uow.pprs.count(changed_q, scope)

    sync: list[Alert] = []
    if sees_sync:
        for run, masters in (
            (uow.sync_runs.latest_finished("PPRS"), False),
            (uow.sync_runs.latest_finished("MASTERS"), True),
        ):
            a = _sync_alert(uow, run, masters=masters)
            if a is not None:
                sync.append(a)
    counts[AlertType.SYNC_FAILURE.value] = len(sync)

    plan: list[Alert] = []
    for fy in active_years(uow):
        cached = statuses_cache.get(fy) if statuses_cache is not None else None
        plan += _plan_alerts(
            cached if cached is not None else item_statuses(uow, fy, plan_scope, low)
        )
    plan.sort(key=lambda a: (a.type is not AlertType.PLAN_EXHAUSTED, _lowest(a), a.subject_id))
    for a in plan:
        counts[a.type.value] += 1

    if not with_lists:
        return AlertList([], counts, days, low, False)
    changed = [_ppr_alert(AlertType.PR_CHANGED, p, now) for p in uow.pprs.find(changed_q, scope)]
    inactive = [
        _ppr_alert(AlertType.PPR_INACTIVE, p, now) for p in uow.pprs.find(inactive_q, scope)
    ]
    shown_plan = plan[:ALERT_LIST_LIMIT]
    truncated = (
        counts[AlertType.PPR_INACTIVE.value] > len(inactive)
        or counts[AlertType.PR_CHANGED.value] > len(changed)
        or len(plan) > len(shown_plan)
    )
    return AlertList(sync + changed + inactive + shown_plan, counts, days, low, truncated)


# ------------------------------------------------------------------ dashboard (§31)
@dataclass(frozen=True)
class Totals:
    planned_amount: Decimal = ZERO
    used_amount: Decimal = ZERO
    remaining_amount: Decimal = ZERO
    items: int = 0
    low: int = 0
    exhausted: int = 0

    def add(self, s: ItemStatus) -> Totals:
        return Totals(
            self.planned_amount + s.planned_amount,
            self.used_amount + s.used_amount,
            self.remaining_amount + s.remaining_amount,
            self.items + 1,
            self.low + (s.level.alert is AlertType.PLAN_LOW),
            self.exhausted + (s.level.alert is AlertType.PLAN_EXHAUSTED),
        )


@dataclass(frozen=True)
class Dashboard:
    fiscal_year: int
    plan_year_state: PlanYearState | None
    department_scope: str | None  # the plan scope (None = every department)
    own_pprs_only: bool  # D-41: PPR figures are the caller's own PPRs
    totals: Totals
    by_department: dict[str, Totals]
    by_category: dict[tuple[str, str], Totals]  # (fund source, budget category)
    ppr_counts: dict[str, int]
    near_exhaustion: list[Alert]
    alerts: AlertList
    recent: list[PprRecord] = field(default_factory=list)


def dashboard(
    uow: UnitOfWork,
    caller: Caller,
    plan_scope: str | None,
    sees_sync: bool,
    fiscal_year: int,
    now: datetime,
) -> Dashboard:
    year = uow.plan_years.get(fiscal_year)
    low = uow.alert_settings.all()[AlertSetting.PLAN_LOW_PERCENT].value
    statuses = item_statuses(uow, fiscal_year, plan_scope, low)
    # The dashboard shows alert counts only; the list is on the alerts page.
    alert_list = alerts(
        uow,
        caller,
        plan_scope,
        sees_sync,
        now,
        with_lists=False,
        statuses_cache={fiscal_year: statuses},
    )

    totals = Totals()
    by_dept: dict[str, Totals] = {}
    by_cat: dict[tuple[str, str], Totals] = {}
    for s in statuses:
        totals = totals.add(s)
        d = _dept(s)
        by_dept[d] = by_dept.get(d, Totals()).add(s)
        k = (s.item.fund_source_id, s.item.budget_category_id)
        by_cat[k] = by_cat.get(k, Totals()).add(s)

    by_state = uow.pprs.count_by_state(fiscal_year, caller.ppr_scope)
    counts = {st.value: by_state.get(st.value, 0) for st in PprState}
    near = _plan_alerts(statuses)[:NEAR_EXHAUSTION_LIMIT]
    recent = uow.pprs.find(PprSearch(fiscal_year=fiscal_year, limit=10), caller.ppr_scope)
    return Dashboard(
        fiscal_year,
        year.state if year else None,
        plan_scope,
        caller.ppr_scope is not None,
        totals,
        dict(sorted(by_dept.items())),
        dict(sorted(by_cat.items())),
        counts,
        near,
        alert_list,
        recent,
    )


def balances_for_year(
    uow: UnitOfWork, fiscal_year: int, plan_scope: str | None
) -> Sequence[ItemStatus]:
    return item_statuses(uow, fiscal_year, plan_scope)


def _dept(s: ItemStatus) -> str:
    """The department an item is counted under (D-38 C-4: purchaser of a CENTRAL item)."""
    r = s.item
    return responsible_department(r.plan_type, r.owner_department_id, r.purchasing_department_id)
