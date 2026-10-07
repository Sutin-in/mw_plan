# ADR-006 — PR retrieval and Pre-PPR validation (Wave 3)

- **Status:** Accepted with Wave 3 (2026-09-29)
- **Spec:** §11, §12, §13, §22.1, §25.1, §26, §40.1, §40.2, §40.6, §40.13; decisions D-16, D-17, D-18.

## What was built

`GET /api/prs/{pr_no}/prevalidation` (permission `PPR_PREVALIDATE`: REQUESTER, PLAN_OFFICER).

1. **Live retrieval** (§25.1): the PR header and **all** its items are read from HOSxP through `HosxpGateway` (mock today) on every call. Nothing is cached. If HOSxP cannot be reached the API answers **503 `HOSXP_UNAVAILABLE`**; it never falls back to earlier data (§26). Other functions (plan, audit, …) keep working.
2. **Read-only**: pre-validation writes nothing: no PPR, draft, ledger or audit row (tested by comparing every table's row count). It is not one of the audited actions of §36.
3. **Rules** (pure domain, `domain/prevalidation.py`):

| Rule | Result code |
|---|---|
| PR not found | `PR_NOT_FOUND` |
| D-18: PR has no budget year | `FISCAL_YEAR_UNKNOWN` |
| D-18: PR budget year < current fiscal year (plan expired) | `PLAN_YEAR_EXPIRED` |
| D-18: PR budget year > current fiscal year | `FISCAL_YEAR_NOT_CURRENT` |
| No plan for the current year / plan not ACTIVE | `PLAN_YEAR_NOT_FOUND` / `FISCAL_YEAR_NOT_ACTIVE` |
| PR cancelled in HOSxP (A-13) | `PR_CANCELLED_IN_HOSXP` |
| PR without department / unknown or missing fund source | `PR_MISSING_DEPARTMENT` / `UNKNOWN_FUND_SOURCE` |
| D-17: header total missing / ≠ sum of item amounts | `PR_TOTAL_UNAVAILABLE` / `PR_TOTAL_MISMATCH` |
| D-16: no / several matching DEPARTMENT Plan Items | `NOT_IN_PLAN` / `AMBIGUOUS_PLAN_MATCH` |
| §13: quantity or amount above the ledger-derived remaining (lines on one Plan Item in aggregate) | `QTY_EXCEEDED`, `AMOUNT_EXCEEDED`, `QTY_EXHAUSTED`, `AMOUNT_EXHAUSTED` |

   All-or-none: the PR is `eligible` only if the header and every line pass. The **required amount** returned is the sum of the item amounts (D-17), never the header figure.
4. **Current fiscal year** = configured boundaries (D-08) applied to the **server date** (`AppDeps.today`, injectable for tests). A plan expires when its fiscal year ends, even while still `ACTIVE` (D-18).
5. When the fiscal-year check fails, the lines are listed but **not** judged against a plan (`lines_checked = false`), because "not in plan" or "exhausted" would be misleading; the header states the reason. Other header failures still check every line.
6. **Department scope** (§22.1): a user whose only role is REQUESTER may validate PRs of their own department only (403 `PR_OTHER_DEPARTMENT`); Plan Officers see every department.

## Deliberate limits

- PR binding (D-03/D-05) needs the PPR tables of Wave 4. Until then no PR can be bound, so the binding is `UNBOUND` by construction; Wave 4 replaces this with the database lookup and its unconditional unique constraint.
- Only DEPARTMENT Plan Items are matched. Central Pool / Assigned Purchase matching stays gated (G-1, G-3), so such PR lines report `NOT_IN_PLAN`.
- The D-18 re-check at confirmation, header allocation (D-04) and PPR creation are Wave 4.
- Mock sample PRs `MOCK-PR-0001…0003` (`mock/fixtures.py`) cover an ordinary PR, a D-17 total mismatch and a D-18 expired budget year.
