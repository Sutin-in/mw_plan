# ADR-007 — Real HOSxP connection: read-only SQL with a site query file

- **Status:** Accepted (2026-09-29). The product owner asked that the HOSxP connection be built and delegated the choice of integration method.
- **Spec:** §4, §24, §24.5, §25.1, §26, §39, §42, §47.

## Decision

The real `HosxpGateway` reads the hospital's HOSxP **database directly with read-only SQL** (MySQL/MariaDB or PostgreSQL). The code is generic; the HOSxP-specific SQL lives in a per-site file `hosxp_queries.toml`, one query per gateway operation, each returning columns named after this system's contracts.

The operations ("APIs") the system uses:

| Operation | Used for | Needed now |
|---|---|---|
| `get_pr(pr_no)` | PR header: number, date, **budget year**, department, fund source, budget category, requester, **header total**, native status | yes (Wave 3, 4, 6) |
| `get_pr_items(pr_no)` | all PR lines: item, qty, unit, unit price, amount | yes |
| `get_departments()` | department master | yes (master sync, D-14) |
| `get_items()` | item master | yes |
| `get_fund_sources()` | fund-source master ("ประเภทงบ") | yes |
| `get_budget_categories()` | budget-category master ("ชื่องบ"), with parent | yes |
| `get_budget_snapshot(fund, category)` | HOSxP budget figures (§24.4) | later |
| `get_stock_movements(date_from, date_to)` | central-store issues and returns, report only (D-39) | Wave 10B-1: own `[stock_movement]` section, used only when verified (ADR-018) |

Login through HOSxP credentials (`HosxpAuthenticationAdapter`) is a separate piece and is **not** part of this decision: how HOSxP authenticates users is unknown and will not be guessed.

## Why this method

- As far as we know HOSxP offers no general, documented API for third-party systems at this hospital; direct read-only database access is the usual way other systems integrate with it. If a vendor API is later confirmed, a second adapter can implement the same `HosxpGateway` without touching the core.
- Live reads satisfy §25.1 (no stale data for a new PPR).
- Keeping every table/column name in a site file satisfies §47 ("never guess"): the repository contains none; the file is written from the real schema (discovered with a metadata-only tool) and verified against a real PR (`hosxp-check`).

## Safety

1. **Read-only three times:** load-time check (one SELECT/WITH statement, no data- or session-changing keywords, exact parameters), READ ONLY transaction per query (plus a statement timeout), and a database account granted `SELECT` only. On MySQL the session stays read-only for the adapter's dedicated connections.
2. **Fail closed:** unreachable database → `HosxpUnavailableError`; wrong SQL, wrong columns, values that do not fit the contracts, or several headers for one PR number → `HosxpConfigurationError` (a subclass). Callers refuse in both cases.
3. **No data in errors:** messages name columns and error types, never values. The schema tool reads metadata only.
4. **Secrets:** the HOSxP URL lives only in `.env` / the environment; `hosxp-check` prints it redacted. `hosxp_queries.toml` and `hosxp_schema*.md` are git-ignored until reviewed.

## Verification

- Tests run the adapter against a made-up `mockhx_*` schema on **PostgreSQL and MariaDB** (CI runs both): contract mapping, status normalization, database-level read-only enforcement, column checks, unavailability, schema discovery without row data, the CLI tools, and the full Pre-PPR validation API end to end on the SQL adapter.
- Against real HOSxP: pending the read-only account and the site query file (`docs/HOSXP_CONNECTION.md`).
