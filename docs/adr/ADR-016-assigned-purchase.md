# ADR-016 — Assigned Purchase (Wave 7B-2)

- **Status:** Proposed with Wave 7B-2 (2026-10-01)
- **Spec:** §10.3, §12, §20, §22.1; decisions D-09, D-22, D-30, D-37, D-38 (G-3 resolved).
  G-4 stays open; the HOSxP cancellation mapping stays empty (D-30).

## Decision

One pooled `ASSIGNED` Plan Item records each department's demand. The purchasing department's
PR draws on the item like any PPR (quantity and amount, same ledger); its PPR states how much it
buys for each demand department (*coverage*). Demand states are derived, never typed.

| D-38 | Implementation |
|---|---|
| matching | `plan.match_department` gives the purchasing department for `ASSIGNED` items too, so the purchaser's PRs match them (same HOSxP item and fund source). `match_key_issues` now covers `ASSIGNED` items: an `ASSIGNED` item and the purchaser's own `DEPARTMENT` item (or another pooled item) with the same key are refused at activation and amendment (`DUPLICATE_MATCH_KEY`). |
| A-1 blocking | `assigned.blocked_keys` → `PrevalidationContext.blocked`: a line of a department whose demand on an `ASSIGNED` item (same item, fund) is `PLANNED` or `INCLUDED_IN_CENTRAL_PURCHASE` gets `DEMAND_IN_CENTRAL_PURCHASE`, so the PR is not eligible for a draft or a confirmation. A department's own demand on an `ASSIGNED` item it buys never blocks it; demand on another purchaser's item blocks every line of that key (see "One demand, one purchase line"). `FULFILLED` or `CANCELLED` demand no longer blocks. |
| A-2 coverage | Table `ppr_coverage` (migration 0009): per PPR, `ASSIGNED` item and demand department, a *working* row (edited while the PPR is a draft or unlocked, `PUT /api/pprs/{id}/coverage`, REQUESTER of the PPR's department, audited `PPR_COVERAGE_UPDATED`) and a *confirmed* row. The coverage form of a draft or unlocked PPR follows the PR as confirmation will read it (live from HOSxP, when its department buys any `ASSIGNED` item; the PPR's own lines if HOSxP cannot be read). Confirmation and reconfirmation check the working coverage against the confirmed lines (`assigned.check_coverage`: present, positive, a department with open demand, adding up exactly to the line quantity, never above the department's remaining need = demand − what other PPRs in effect cover) and fail with `COVERAGE_INVALID` (409) and the details otherwise; it then becomes the confirmed coverage and is part of the version snapshot (`coverage`). A new draft gets a proposal when the quantity bought equals exactly what all departments still need. |
| state rule | `assigned.demand_state`: 0 → `CANCELLED`; covered by PPRs *in effect* (confirmed, locked / verified / under review / unlocked / closed — never cancelled) less than the demand → `PLANNED`; fully covered and every covering PPR verified → `FULFILLED`; otherwise `INCLUDED_IN_CENTRAL_PURCHASE`. |
| A-3 fulfilled | "Verified" = the version of the PPR in effect was verified by Procurement: `PROCUREMENT_VERIFIED`, or under PR-change review / unlocked / closed with that version verified. A fulfilled demand therefore stays fulfilled while a PR change is reviewed and while the PPR is unlocked (A-5); a reconfirmation makes a new, unverified version and the demand goes back to *included* until it is verified. |
| A-4 cancellation | A HOSxP cancellation (sync, D-30) moves the PPR to `CANCELLED`; its coverage no longer counts, so the demand returns to `PLANNED` (also from `FULFILLED`). |
| A-5 unlock / reconfirm | Unlocking copies the confirmed coverage to the working one; the confirmed coverage keeps counting, so the demand keeps its state. Reconfirmation replaces the confirmed coverage and recomputes. |
| A-6 amending demand | Plan Amendment accepts `demands` (existing `ASSIGNED` items: department, new quantity; a new department may be added) and `new_items[].demands`. Never below what PPRs in effect cover for that department (`BELOW_COVERED_DEMAND`); 0 = `CANCELLED`. History target `DEMAND` (before/after `{plan_item_id, department_id, demand_qty}`), shown in the amendment history (to organisation-wide roles and to that department) and the *Plan Amendment* report. A new `ASSIGNED` item needs its demand (`ASSIGNED_WITHOUT_DEMAND`). A demand total that differs from the planned quantity is a warning only (as before). |
| audit | Every state change: `DEMAND_STATE_CHANGED` on the demand row, with before/after and its cause (`{ppr_id, event, version}` / `{ppr_id, event, sync_run_id}` / `{plan_amendment_id, amendment_no}`). `refresh_demands` runs after confirmation / reconfirmation, verification, a sync state change or release, and a demand amendment. |

- Concurrency: one lock order everywhere — PPR row, Plan Items, then demand rows (one statement,
  id order). Confirmation (plan year FOR SHARE) locks, after its Plan Items, the demand rows of
  every `ASSIGNED` item it touches or that can block it (A-1) before it evaluates again;
  verification and synchronization lock the demand rows of the PPR's coverage; amendment (plan
  year FOR UPDATE) locks the changed items and then all `ASSIGNED` demand rows of the year and
  re-reads them, so the A-6 floor and the audited "before" states are current.
- Each demand's covered quantity (PPRs in effect) is returned with the Plan Item (`covered`) and
  shown on the plan-year page and in the amendment form (the floor of A-6).
- Migration 0009: `ppr_coverage`; demand quantity may be 0 and `(demand_qty = 0) = (state =
  'CANCELLED')`; amendment history accepts target `DEMAND`. Migrations 0001–0008 untouched.
- Screens: PPR page section "ซื้อแทนหน่วยงาน" (coverage form / confirmed coverage, each demand's
  state); plan-year page lists each department's demand and state; the amendment form edits demand
  rows of `ASSIGNED` items and the demand of a new `ASSIGNED` item; Thai labels for the new codes.
- Demo: the store's glove PR `MOCK-PR-{fy}-1008` (30 × 30), not in plan until an `ASSIGNED` item
  is added by amendment.

## Kept restrictions (not changed by D-38)

- An `ASSIGNED` item keeps its purchasing department (`ASSIGNED_PURCHASER_CHANGED`): its coverage
  belongs to the purchaser's PPRs. To move the purchase, reduce the item and add a new one.
- Demand states are only derived; there is no manual override.

- **One demand, one purchase line** (product owner, 2026-10-01, after review): a department's
  need for one HOSxP item and fund source is claimed by at most one Assigned Purchase at a time.
  - Activation and Plan Amendment refuse open demand (above 0) of the same department on two
    `ASSIGNED` items with the same item and fund (`DUPLICATE_DEMAND_CLAIM`); a demand of 0 claims
    nothing, so a need can be moved by setting it to 0 on one item and adding it on the other.
  - A plan made before the rule that still holds such a pair cannot cover that department on
    either item: confirmation refuses it (`COVERAGE_DEMAND_CLAIMED_ELSEWHERE`) until the plan is
    amended.
  - A-1 has no exemption: a department with open demand on another purchaser's `ASSIGNED` item is
    blocked for that item and fund even on a line matched to an `ASSIGNED` item it buys itself.
    Only its own demand on the item it buys never blocks it.

## Consequences

- A department with open demand can no longer prepare a PPR for that item and fund from its own
  plan; its PR shows `DEMAND_IN_CENTRAL_PURCHASE` until the demand is fulfilled or cancelled.
- A PPR drawing on an `ASSIGNED` item cannot be confirmed without coverage; the allocation rules
  (D-19) are unchanged.
- An `ASSIGNED` item activated before 7B-2 has demand rows already (they were required) and starts
  matching its purchaser's PRs; if the purchaser also plans the same item and fund itself, those
  PR lines fail closed (`AMBIGUOUS_PLAN_MATCH`) until the plan is amended.

## Review (2026-10-01)

An independent review of the branch found 3 medium and 4 low issues. Fixed: the coverage form of an
unlocked PPR followed its old lines instead of the live PR; the "verified" rule was inconsistent
between PR-change review and unlock; demand rows were locked in two steps in confirmation (deadlock
risk with verification); the amendment read demand states before locking them; the covered
quantity was not shown; the A-1 check read demand states without a lock. The seventh, the A-1 exemption
for lines matched to an `ASSIGNED` item, was replaced by the "one demand, one purchase line" rule
above (product owner decision).

## Verification evidence (Wave 7B-2, 2026-10-01)

| Where | Commit | Result |
|---|---|---|
| Cloud (Linux, PostgreSQL 16.13) | `e4150ba` | `tools/check.py` 9/9, 1,265 passed |
| Cloud end-to-end (`tools/dev/verify_wave7b2.py`, demo mode) | `e4150ba` | 21/21 on a fresh development database; 10/10 on a repeat run (the steps that write are skipped once done) |
| Cloud, after the "one demand, one purchase line" rule | `df5f5e1` | `tools/check.py` 9/9, 1,270 passed; end-to-end 21/21 on a fresh development database, 10/10 on a repeat run |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `4bc6e3b` (before the one-demand rule) | `tools/check.py` 9/9, 1,245 passed, 20 skipped (MariaDB connector tests, no local MariaDB); end-to-end 21/21 (the steps that write ran here) |
| Windows dev machine | `b2e2eb6` (final) | `tools/check.py` 9/9, 1,250 passed, 20 skipped; end-to-end 10/10 (repeat run; the writing steps were already done) |
| Cloud, `main` after fast-forward merge | `8e6b695` | `tools/check.py` 9/9, 1,270 passed (re-run after the local PostgreSQL service was restarted; the first run could not reach it) |

**Test data left in the Windows development database (FY 2570):** a Plan Amendment adding an
Assigned Purchase item (gloves 30 × 30, bought by Mock Store for Mock ENT 20 and Mock OR 10, MED
line +900), and one store PPR from `MOCK-PR-2570-1008` covering both demands, confirmed and verified
(both demands fulfilled). Demo data (mock HOSxP), kept as evidence. The database is at schema 0009;
the folder is on branch `wave7b2-assigned`.
