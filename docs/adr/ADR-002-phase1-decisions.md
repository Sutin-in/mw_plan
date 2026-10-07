# ADR-002 — Phase 1 domain decisions (D-01 … D-10)

- **Status:** Accepted (2026-09-28)
- **Authoritative text:** spec §0 Decision Record. This ADR records how each decision is realized in code.
- **Scope:** D-01 … D-10 (Waves 1A/1B). The spec is now v1.12 with D-01 … D-31; D-11 … D-16 (Plan Core and plan-item uniqueness) are realized in [ADR-005](ADR-005-plan-core.md); D-17 (required PPR amount) and D-18 (fiscal-year validity) are realized with Waves 3/4.

| ID | Decision | Realized in (Wave 1A) | Tests |
|---|---|---|---|
| D-01 | Central stock issue/return change Plan **quantity only**; amount unchanged | `ledger.py`: `CENTRAL_STOCK_ISSUE` requires qty<0, amount==0; `CENTRAL_STOCK_RETURN` qty>0, amount==0; amount formula excludes stock | `test_ledger.py` |
| D-02 | Internal PPR state ≠ HOSxP PR status | `ppr_state.PprState` (7 values); `integration/hosxp/status.HosxpPrStatus` (6 values) — separate types | `test_ppr_state.py`, `test_hosxp_boundary.py` |
| D-03 | One HOSxP PR number → at most one **confirmed** PPR for life, even if cancelled | `pr_binding.PrBinding.BOUND` has no exit; prevalidation rejects `PR_ALREADY_BOUND` | `test_pr_binding.py`, `test_prevalidation.py` |
| D-04 | Category allocation at PPR **header** level: **PPR 1 → N `ppr_category_allocation` rows** | `allocation.validate_allocations`: same fund source, category once per PPR, Σ = required amount; rows carry no PR-item/plan-item reference and never touch the ledger | `test_allocation.py` |
| D-05 | Drafts are unnumbered, one working draft per PR, discardable; binding starts at `CONFIRMED_LOCKED` | `pr_binding`: `UNBOUND → DRAFT_IN_PROGRESS → (DISCARD → UNBOUND \| CONFIRM → BOUND)` | `test_pr_binding.py` |
| D-06 | PPR numbers unique, FY-scoped, yearly reset; **gaps allowed** | `ppr_number.format_ppr_number`, reference `FiscalYearSequencer` | `test_fiscal_year.py` |
| D-07 | `CLOSED` kept; no inbound transition; no manual close | `ppr_state.INTEGRATION_TBD_STATES`; no event targets `CLOSED` | `test_ppr_state.py` |
| D-08 | Fiscal-year boundaries are configuration; HOSxP `fiscal_year` not assumed | `FiscalYearConfig` + `config/fiscal_year.load_fiscal_year_config`; `PrHeader.fiscal_year` optional; prevalidation rejects an unresolved FY | `test_fiscal_year.py`, `test_fiscal_year_config.py`, `test_prevalidation.py` |
| D-09 | Reconfirm after revision is a requester attestation; Plan Officer/Admin unlock but never reconfirm | `ppr_state`: `RECONFIRM` roles = `{REQUESTER}`; same-department check deferred to API layer | `test_ppr_state.py` |
| D-10 | Only critical PR changes force review; no acknowledgement shortcut | `ppr_state`: no event leaves `PR_CHANGED_REVIEW_REQUIRED` except `UNLOCK` / `CANCEL_FROM_HOSXP`; critical-field classifier in Wave 6 | `test_ppr_state.py` |

## D-04 data shape (for Wave 1B/4)

```text
ppr (1) ──< ppr_category_allocation (N)
            ppr_id            FK → ppr
            budget_category_id FK → hosxp_budget_category
            amount            > 0
            UNIQUE (ppr_id, budget_category_id)
  CHECK (deferred / in confirm transaction): SUM(amount) = required PPR amount
  All categories under the PR's fund source.
Plan usage: ppr_item → plan_item → plan_ledger   (allocation rows never post to plan_ledger)
```

## D-03 / D-05 data shape (for Wave 1B/4)

The PR→PPR binding written in the confirmation transaction carries an **unconditional**
`UNIQUE (hosxp_pr_id)`. Drafts are constrained separately: at most one working draft per PR.
Exact physical tables are chosen in Wave 1B.
