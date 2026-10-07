# ADR-005 — Plan Core (Wave 2)

- **Status:** Accepted with Wave 2 (2026-09-28)
- **Spec:** §8, §9, §10, §18, §22.1, §25.2/§25.4, §29.2, §29.4; decisions D-11 … D-16.

## Decisions realized

| Decision | Realization |
|---|---|
| D-11 envelope per exact category | `domain/plan.check_activation`: Σ Plan Item planned amount per (fund source, budget category) must equal that line's approved amount; no roll-up from child categories |
| D-12 validate + post at activation | `APPROVED → ACTIVE` calls `plan_services.activate_plan` inside the same transaction: validation must pass, then one `PLAN_ACTIVATED` ledger entry per Plan Item (idempotency key `activate:<plan_item_id>`) and a `PLAN_ACTIVATED` audit row; any failure rolls everything back |
| D-13 amount ≠ qty × price allowed | Reported as warning `AMOUNT_DIFFERS_FROM_ESTIMATE` on every Plan Item response and in the validation report; never blocks |
| D-14 masters via gateway | `POST /api/sync/masters` (PLAN_OFFICER, ADMIN) pulls fund sources, budget categories, departments and items through `HosxpGateway` (mock today), upserts by source id, logs a `MANUAL` `sync_run` (also on failure) and audits `MANUAL_SYNC_STARTED` |
| D-16 one DEPARTMENT Plan Item per year + department + fund + item | `domain/plan.check_activation` refuses activation with `DUPLICATE_DEPARTMENT_ITEM` when two DEPARTMENT Plan Items share (item, owner department, fund source), regardless of budget category (formerly A-9); the PR matcher in `domain/prevalidation` (formerly A-1) relies on this uniqueness |
| D-15 corrections while APPROVED | Edits in `APPROVED` require `correction_reason` + `source_reference`; audited as `PLAN_BUDGET_CORRECTED` / `PLAN_ITEM_CORRECTED` with the reason and source reference; `ACTIVE`/`CLOSED` are locked (`PLAN_YEAR_LOCKED`) |

## Data model (migration `0002_plan_core`)

```text
plan_year 1──N plan_budget (fund_source, budget_category, approved_amount)   UNIQUE per year+fund+category
plan_budget 1──N plan_item  (composite FK guarantees same plan year)
plan_item 1──N plan_item_demand (ASSIGNED only; state PLANNED/INCLUDED_IN_CENTRAL_PURCHASE/FULFILLED/CANCELLED)
plan_item 1──N plan_ledger  (append-only: trigger blocks UPDATE/DELETE/TRUNCATE)
```

- Masters are referenced by HOSxP **source id** with foreign keys to the mirror tables, so a Plan Item cannot point at an unsynchronized record. Item code, name and unit are **snapshotted** on the Plan Item.
- `plan_ledger` CHECK constraints mirror `domain/ledger.py`, including D-01 (stock events carry amount 0); a partial unique index (`uq_plan_ledger_one_activation`) allows only one `PLAN_ACTIVATED` per Plan Item; `idempotency_key` is unique.
- Money `NUMERIC(18,2)`, quantities `NUMERIC(18,4)`.

## Ledger event naming (migration `0003_plan_activated_event`, pre-Wave-3 hardening)

Wave 2 shipped the initial ledger event as `PLAN_APPROVED`, although it is posted at `APPROVED → ACTIVE`. By product-owner decision the event is now `PLAN_ACTIVATED`, so that:

| Meaning | Represented by |
|---|---|
| Director approval of the business plan | plan-year state `APPROVED` |
| Operational activation | plan-year state `ACTIVE` |
| Initial ledger event | `PLAN_ACTIVATED` |
| Audit action | `PLAN_ACTIVATED` |

Migration 0003 (0002 is untouched) drops and recreates `ck_plan_ledger_known_event`, `ck_plan_ledger_event_signs` and the partial unique index (renamed `uq_plan_ledger_one_approval` → `uq_plan_ledger_one_activation`), and relabels existing rows in place: event `PLAN_APPROVED` → `PLAN_ACTIVATED`, activation key `approve:<id>` → `activate:<id>`; ids, deltas, actors and timestamps are kept. The row-level append-only trigger is disabled only around that one `UPDATE` and re-enabled inside the same transaction (never dropped). Downgrade reverses it exactly. No business rule changed. Tests: `tests/db/test_migration_0003.py`.

## Concurrency
Every budget/item change and the activation lock the `plan_year` row (`SELECT … FOR UPDATE`), so an edit cannot interleave with activation. PPR-level concurrency (§19) comes with Wave 4.

## Access
- `PLAN_READ` (all roles) with department scoping: a user whose only role is REQUESTER sees Plan Items their department owns, purchases, or has demand on; other items answer 404 (no information leak).
- `PLAN_MANAGE` (PLAN_OFFICER only): budgets, items, validation report, plan-year transitions. ADMIN has no plan management (§22.8).
- `SYNC_RUN` (PLAN_OFFICER, ADMIN).

## Not in this wave
Plan Amendment (Wave 7), Central Pool stock posting and Assigned Purchase fulfilment (gates G-1, G-3), Excel export (Wave 8), nightly sync (Wave 6).
