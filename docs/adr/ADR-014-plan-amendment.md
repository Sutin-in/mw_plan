# ADR-014 — Plan Amendment (Wave 7A)

- **Status:** Proposed with Wave 7A (2026-09-30)
- **Spec:** §18, §19, §20, §22.1, §36, §37, §40.11; decisions D-11, D-15, D-23, D-37.
  Gates G-1 and G-3 stay **open** (Wave 7B); G-4 stays open.

## Decision

A Director-approved change to an ACTIVE plan is recorded as one **Plan Amendment**: a numbered,
append-only record with the approval document number and date, a reason, and the before/after
of every budget line and Plan Item it changes. Rules are those of D-37 (product-owner answers of
2026-09-30).

| Part | Where |
|---|---|
| Rules (pure): judge the whole amendment, compute before/after and the ledger entries | `domain/amendment.py` (`plan_amendment`, `ledger_entries`) |
| History tables (append-only, triggers), `plan_ledger.plan_amendment_id`, `plan_item.planned_qty >= 0` | migration `0008`, `infrastructure/db/schema_amendment.py`, `schema.py` |
| Persistence | `AmendmentRepository` (`infrastructure/db/amendment_repositories.py`), `PlanRepository.amend_item`, `LedgerRepository.append(..., plan_amendment_id)` |
| Use cases: preview (writes nothing), record, read, §22.1 scope | `application/amendment_services.py` |
| HTTP | `api/amendment_routes.py`: `POST /api/plan-years/{fy}/amendments/preview`, `POST /api/plan-years/{fy}/amendments`, `GET /api/plan-years/{fy}/amendments`, `GET /api/plan-amendments/{id}` |
| Permission | `PLAN_AMEND` = `PLAN_OFFICER`; reading uses `PLAN_READ` with the Plan Item scope |
| Report | `PLAN_AMENDMENT` (one row per change, screen and `.xlsx`) in `application/report_services.py` |
| Screens | `/plans/{fy}/amendments` (history), `/plans/{fy}/amendments/new` (form → check → record), `/plan-amendments/{id}` |

## Rules (D-37 7A)

- ACTIVE plan years only; the approval document number (≤ 100 characters) and a date not later
  than today are required, and a reason. Numbered 1, 2, … per plan year. A mistake is corrected by
  a further amendment; the history cannot be edited (database triggers).
- After the amendment every budget line balances (D-11). Existing lines may rise or fall; new
  lines (fund source / category not yet in the year) may be added with their items.
- Quantity, estimated price and amount of any item never go below what confirmed PPRs still hold
  (confirmed minus released); an unused item may go to 0 (closed: remaining 0, no new PPR);
  a quantity of 0 needs an amount of 0.
- Item data (HOSxP item, owner department, purchasing department, category) change only while
  the item is unused; the fund source and the plan type never change.
- `DEPARTMENT` / `CENTRAL` / `ASSIGNED` numbers may be amended; demand is not (G-3), and an
  `ASSIGNED` item keeps its purchasing department. A changed `ASSIGNED` quantity that no longer
  equals its demand total is a warning. New items may be `DEPARTMENT` or `CENTRAL`; a new
  `ASSIGNED` item waits for 7B.
- No two `DEPARTMENT` items may share (item, owner department, fund source) afterwards (A-9).

## Ledger and concurrency

- Each amended item posts one `PLAN_AMENDMENT` (the change of quantity and amount; nothing for a
  data-only change). A new item posts `PLAN_ACTIVATED` (0, 0) and then `PLAN_AMENDMENT` (its whole
  value), so "approved at activation + amendments" stays true for every item. Idempotency keys
  `amendment:{id}:item:{plan_item_id}` / `amendment:{id}:activate:{plan_item_id}`; every entry is
  checked by the domain ledger before it is written and carries `plan_amendment_id`.
- The plan year row is locked `FOR UPDATE` for the whole transaction (PPR confirmation holds it
  `FOR SHARE`), and the amended items are locked, so no confirmation can draw on the plan while it
  is amended (§19). Header, history, current values, ledger and the `PLAN_AMENDED` audit row are
  one transaction.

## Stale forms

After ACTIVE the plan changes only through amendments, so the form sends the number of the
latest amendment it was built on (`base_amendment_no`); the API refuses the whole amendment
(`PLAN_CHANGED_SINCE_FORM`) if another one was recorded since. The form also compares each field
with the value it showed (hidden fields), never with the plan as it is now, so a field the user
did not touch is never sent. An item's current department or category is always offered in its
drop-down even if HOSxP has since closed it.

## History content

Each item's before/after carries its item code, name and unit as recorded, so the history and the
*Plan Amendment* report do not change when HOSxP later renames an item.

## Scope of reading (§22.1)

Organisation-wide roles see every amendment. A requester-only user sees only item changes whose
owner or purchasing department (before or after) is their own, and no budget-line changes; an
amendment with nothing visible is hidden (404 on the detail).

## Consequences

- `plan_item.planned_qty > 0` is relaxed to `>= 0` in the database (plan entry through the API
  still requires > 0). The test that asserted the old database rule now asserts `-1` is refused.
- No Central / Assigned behaviour changes: no Central or Assigned PR matching, no demand-state
  changes and no stock-issue reading until G-1 / G-3 are frozen (Wave 7B).
- The amendment form sends only the fields that differ from what it showed; the API judges the
  whole amendment again when it is recorded.
- Q1–Q4 monitoring figures are not amended (they never block; §9.4). The approval date has no
  lower bound (a Director may approve before the year starts).

## Decision — used Plan Items keep their category and purchasing department (product owner, 2026-10-01)

The category or purchasing department of a **used** Plan Item cannot be changed by an amendment
(option a, chosen by the product owner). Reasons:

- **No ambiguity in PR matching.** The way D-37 changes a used item (reduce it to what is used,
  add a new item) would give two `DEPARTMENT` items with the same item, owner department and fund
  source; PR matching would then find two candidates (A-1 / A-9 fail closed), so PPRs for that
  item would be blocked or, if matching were loosened, could draw on the wrong item.
- **Auditability of the original item.** Confirmed PPRs, their ledger entries and the reports
  point at the item as it was used; its category and purchaser stay what those records say.

The refusal is `DATA_CHANGE_AFTER_USE` (and `DUPLICATE_DEPARTMENT_ITEM` for the reduce-and-add
path with the same key). No workaround is added in Wave 7A. The numbers of a used item (quantity,
estimated price, amount, never below what is used) can still be amended.

## Verification evidence (Wave 7A, 2026-10-01)

| Where | Commit | Result |
|---|---|---|
| Cloud (Linux, PostgreSQL 16.13) | `bc22086` | `tools/check.py` 9/9, 1,217 passed |
| Cloud end-to-end (`tools/dev/verify_wave7a.py`, demo mode, run twice) | `bc22086` | 20/20 both runs |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `bc22086` | `pip install -e backend[dev]`, `migrate` to 0008, `tools/check.py` 9/9, 1,197 passed, 20 skipped (MariaDB connector tests, no local MariaDB) |
| Windows end-to-end | `bc22086` | 20/20 |
| Cloud, `main` after fast-forward merge | `d36ed48` | `tools/check.py` 9/9, 1217 passed |

**Test data left in the Windows development database.** The Windows end-to-end run recorded one
demo amendment in `ppr_dev`: fiscal year 2570, amendment no. 1, document `E2E-7A-…`, moving
100 baht between Plan Items 5 and 6 of the demo OFFICE line (9,900.00 / 10,100.00; line total
25,000.00 unchanged). It is test data in a demo database (mock HOSxP), kept as evidence and not
deleted (history is append-only by design); remove it only by recreating `ppr_dev` if a later test
needs a clean plan.
