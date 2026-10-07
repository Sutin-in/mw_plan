# ADR-012 — Dashboards, derived alerts and plan screens (Wave 8A)

- **Status:** Proposed with Wave 8A (2026-09-30)
- **Spec:** §22.1, §31.1, §31.2, §31.7 – §31.9, §34, §36; decisions D-11 … D-16, D-32, D-33, D-34.
  Gates G-1, G-3 (Wave 7) and G-4 stay **open**; nothing here depends on them.

## 1. What 8A adds

| Part | Where | Notes |
|---|---|---|
| Alert rules (pure) | `domain/alerts.py` | Inactivity, low / exhausted plan, sync failure. No I/O. |
| Alert settings | migration `0007_alert_setting`, `alert_setting` table | Two rows seeded: `PPR_INACTIVE_DAYS` = 30, `PLAN_LOW_PERCENT` = 20. |
| Balances for many items | `LedgerRepository.balances(plan_item_ids)` | One grouped query; same formula as `domain/ledger.py` (§18.3, D-01). |
| Dashboards and alerts | `application/dashboard_services.py` | Read only: no ledger posting, no state change, no HOSxP call. |
| API | `api/dashboard_routes.py` | `GET /api/dashboard`, `GET /api/alerts`, `GET/PUT /api/alert-settings`, `GET /api/plan-years/{fy}/balances`. |
| Screens | Express views `dashboard`, `alerts`, `plans`, `plan_year`, `plan_item_form`, `alert_settings` | Plan screens call the Wave 2 API only (D-34). |

## 2. Permissions

| Permission | Roles | Used by |
|---|---|---|
| `DASHBOARD_READ` | every role | dashboard, alert list (content is scoped) |
| `ALERT_SETTING_MANAGE` | `ADMIN` | changing thresholds (reason required, audited) |

Existing permissions keep their meaning: `PLAN_MANAGE` / `PLAN_YEAR_MANAGE` (Plan Officer) for
plan changes, `SYNC_RUN` for master synchronization, `PLAN_READ` for balances.

## 3. Scope (§22.1)

- PPR figures and PPR alerts use the caller's PPR department scope (`Caller.department_scope`),
  exactly like search and the queue: a requester-only user sees their own department.
- Plan figures and plan alerts use the Plan Item visibility of `plan_routes._visible`: owner,
  purchaser or demand department for a requester-only user; everything for other roles.
- `SYNC_FAILURE` needs `SYNC_READ`.

## 4. Alerts are derived (D-33)

Computed on every request from PPR rows, ledger balances and sync runs. There is no alert
table, no acknowledgement and no background job, so an alert disappears as soon as its cause
is gone. A plan alert considers `ACTIVE` plan years only. The low-plan rule compares remaining
quantity and remaining amount separately with `threshold % × (approved + amendments)`; zero
remaining is `PLAN_EXHAUSTED`, not `PLAN_LOW`.

## 5. Consequences

- The queue's *inactive* bucket now reads the stored inactivity threshold instead of a server
  constant, so the dashboard and the queue always agree.
- Dashboard totals are sums of item balances; quantities are only shown per item (quantities of
  different items are not added up).
- LINE / e-mail delivery (Phase 2) would read the same derived alerts; nothing in 8A sends data
  outside the application.
