# ADR-003 — Open design gates and working assumptions

- **Status:** Accepted (2026-09-28). Gates stay open until explicitly resolved by the product owner.

## Design gates (must be resolved before the named wave)

| Gate | Topic | Current handling | Blocks |
|---|---|---|---|
| **G-1** (H-3) | Central Pool: relationship between qty purchased into central stock via PR/PPR and qty later issued from stock (risk of double-counting quantity) | D-01 preserved; `StockIssue` contract only; **no Central Pool posting behavior** anywhere. *Resolved by D-38 (ADR-015); stock movements made report-only by D-39 (ADR-018, `StockMovement` contract replaces `StockIssue`).* | Wave 7 |
| **G-2** (H-4) | HOSxP header total vs sum of PR item amounts | **RESOLVED → spec D-17** (2026-09-29): required amount = sum of PR item amounts (VAT-inclusive estimates at PR stage; VAT known only at PO, outside Phase 1); header total must equal it exactly or the PPR cannot be confirmed. Until Waves 3/4 implement it, `validate_allocations` still takes `required_amount` from the caller | Wave 3/4 (implementation) |
| **G-3** (H-8) | Assigned Purchase matching and ledger posting | Not implemented | Wave 7 |
| **G-4** (D-07) | Which verified terminal HOSxP status/process closes a PPR | `CLOSED` has no inbound transition; no manual close | Wave 6/9 |

## Working assumptions in Wave 1A (reversible, flagged for review)

| ID | Assumption | Where | Why it is safe for now |
|---|---|---|---|
| A-1 | **DECIDED → spec D-16** (2026-09-28): the same HOSxP item may not appear twice for the same department + fund source + fiscal year, even under different budget categories; extra funding is PPR 1 → N category allocation. A PR line matches a **department** Plan Item by (HOSxP item id, owner department, fund source) within the resolved fiscal year; 0 matches → `NOT_IN_PLAN`, >1 → `AMBIGUOUS_PLAN_MATCH` | `prevalidation._match` | Fails closed; Central/Assigned matching deferred to G-1/G-3 |
| A-2 | Multiple PR lines on the same Plan Item are checked **in aggregate** | `prevalidation._validate_lines` | Prevents bypassing hard control by splitting lines |
| A-3 | Requested qty must be > 0; requested amount ≥ 0 | `hard_control` | Spec does not define zero/negative lines |
| A-4 | **DECIDED → spec D-09** (2026-09-28): `CONFIRM` and `RECONFIRM` are `REQUESTER`-only; Plan Officer/Admin unlock but never reconfirm. Same-department check is server-side (Wave 1B/5) | `ppr_state.TRANSITIONS` | — |
| A-5 | **DECIDED → spec D-10** (2026-09-28): only critical changes enter `PR_CHANGED_REVIEW_REQUIRED`; non-critical changes are audit-only; no acknowledgement shortcut — exit only via UNLOCK → revalidation → requester RECONFIRM (or HOSxP cancellation). Classifier built in Wave 6 | `ppr_state.TRANSITIONS` | — |
| A-6 | PR cancelled in HOSxP while only a DRAFT exists → the draft is discarded (binding event), not moved to `CANCELLED` | `pr_binding`, `ppr_state` | DRAFT holds no plan usage; nothing to release |
| A-7 | Budget category → fund source relationship is supplied as a lookup | `allocation.validate_allocations` | Normalized HOSxP contract (§24.1) has no such field; source is TBD (§47) |
| A-9 | **DECIDED → spec D-16** (2026-09-28). Two DEPARTMENT Plan Items with the same (HOSxP item, owner department, fund source) in one year block activation | `domain/plan.check_activation` | Otherwise PR matching (A-1) would be ambiguous for ever; fails early |
| A-10 | An ASSIGNED Plan Item must name a purchasing department and at least one demand department before activation; demand total ≠ planned qty is only a warning | `domain/plan.check_activation` | §10.3 requires both; equality is not stated |
| A-11 | Q1–Q4 that do not add up to planned qty or amount produce a warning only | `domain/plan.item_warnings` | §9.4: quarters are monitoring only |
| A-12 | Manual master sync upserts and counts changes but never deactivates or deletes local mirror records that HOSxP no longer returns | `plan_services.sync_masters` | Deletion semantics of HOSxP masters are unknown (§47) |
| A-8 | Deployment default fiscal year = 1 Oct start, labeled by the B.E. year in which it ends (FY2570 = 2026-10-01 … 2027-09-30) | `config/fiscal_year.DEFAULT_FISCAL_YEAR` | Overridable by `PPR_FY_*` settings (D-08) |
| A-13 | A PR whose normalized HOSxP status is `CANCELLED` is rejected for a PPR (`PR_CANCELLED_IN_HOSXP`). Only the normalized status is used; the native-status mapping is still TBD (§24.5), so with no verified mapping every status is `UNKNOWN` and nothing is rejected on status | `domain/prevalidation`, `application/pr_services` | A cancelled PR cannot consume plan; fails closed only on a verified cancellation |
| A-14 | **DECIDED → spec D-21** (2026-09-29, modified): before confirming a draft, the live PR is compared with the draft snapshot using the D-10 classifier; only **critical** changes block (discard and prepare again); non-critical changes are recorded and the latest live values are confirmed | `domain/pr_changes`, `application/ppr_services.confirm` | — |
| A-15 | **DECIDED → spec D-22** (2026-09-29): reconfirmation may take changed lines, quantities, prices, amounts and budget category after full revalidation; a changed fiscal year, department or fund source returns `PR_IDENTITY_CHANGED` for governance review (no automatic cancel or replacement) | `application/ppr_services.confirm` | — |
| A-16 | **DECIDED → spec D-23** (2026-09-29): quantity ≤ 4 decimals, amount ≤ 2, unit price ≤ 8; anything beyond fails validation (`PR_VALUE_PRECISION`, naming the PR line), never rounded | `domain/prevalidation` | — |
