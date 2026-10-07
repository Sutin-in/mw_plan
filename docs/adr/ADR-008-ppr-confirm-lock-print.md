# ADR-008 — PPR draft, confirmation, lock, versions and print (Wave 4)

- **Status:** Accepted with Wave 4 (2026-09-29)
- **Spec:** §11.3, §14, §15, §16, §17, §19, §28, §29.3, §30, §35, §36; decisions D-03 … D-06, D-09, D-10, D-16 … D-23.

## Flow

```text
POST /api/pprs {pr_no}          eligible PR (Wave 3 checks) -> DRAFT, unnumbered (D-05)
                                one default allocation = PR category x required amount (D-19)
PUT  /api/pprs/{id}/allocations categories: PR's own + Plan Budget lines, same FY + fund (D-19)
POST /api/pprs/{id}/discard     draft only, audited; the PR is free again (D-05)
POST /api/pprs/{id}/confirm     DRAFT -> CONFIRMED_LOCKED (or UNLOCKED_FOR_REVISION -> reconfirm)
POST /api/pprs/{id}/unlock      PLAN_OFFICER / ADMIN, reason required (§15.2)
GET  /api/pprs/{id}/print       A4 page from a confirmed version snapshot (D-20)
```

## Confirmation (one transaction, §14.3 / §19)

1. Lock the PPR row; check the transition and role (REQUESTER only, D-09) and that the caller is a requester **of the PR's live department**.
2. Re-fetch the PR **live** and compare it field by field with a stored PR snapshot, using the **one** classifier of `domain/pr_changes` (the D-10 rules, shared with the Wave 6 sync):
   - **First confirmation (D-21):** compared with the PR as retrieved when the draft was prepared (`ppr.draft_pr`). A **critical** change (item composition/identity, quantity, amount, unit price together with a quantity/amount change, PR total, fund source, budget category, department, fiscal year, a status change to or from `CANCELLED`) refuses with `PR_CHANGED_SINCE_DRAFT` and a field-level list; the requester discards and prepares again. **Non-critical** changes (names, unit labels, requester, date, native status text, a price change with unchanged quantity and amount, a status change that does not affect validity) do not block: every validation still runs, the differences are stored in the snapshot (`pr_changes`) and the audit, and the **latest live values** are confirmed.
   - **Reconfirmation (D-22):** compared with the PR of the last confirmed version. Changed lines, quantities, prices, amounts and budget category are accepted after full revalidation and recorded; a changed **fiscal year, department or fund source** refuses with `PR_IDENTITY_CHANGED` ("needs governance review") — nothing is cancelled, replaced or re-posted.
3. Lock the plan-year row `FOR SHARE` (the year cannot close meanwhile) and the matched Plan Item rows `FOR UPDATE` in id order; re-run every Wave 3 check against the ledger **after** the locks, with the **server date of the confirmation** (D-18). Matched items must be among the locked ones.
4. Validate the allocation: eligible categories, each once, Σ = required amount (D-17), `adjustment_reference` when more than one category (D-19).
5. First confirmation: next number of the fiscal year (`PPR-<FY>-<6 digits>`, D-06), lifetime binding of the PR number (D-03, primary key, trigger-protected).
6. Ledger: per Plan Item, `PPR_CONFIRMED` (−qty, −amount); on reconfirmation first `PPR_RELEASED` for what the previous version still holds. Domain rules forbid negative remaining.
7. Append-only `ppr_version` snapshot: PR as retrieved, items with matched Plan Items and remaining before/after, allocations, adjustment reference, names at confirmation, confirmer; `document_code` = first 16 hex of SHA-256 over the canonical snapshot (a content fingerprint).
8. Audit `PPR_CONFIRMED` / `PPR_RECONFIRMED`. Any failure rolls everything back.

Verified by an independent review; its findings (header changes after the draft, plan-year lock, lock-set check, order-independent comparison, unit-price precision) are fixed and covered by regression tests. A mutation check showed the concurrency test fails when the row locks are removed.

## Data (migration `0004_ppr_core`)

`ppr` (state, number iff confirmed, separate `hosxp_pr_status` — D-02, `draft_pr` snapshot for D-21), `ppr_item` (PR lines from HOSxP only; unit price at 8 decimals), `ppr_category_allocation` (header level, unique category, amount > 0), `ppr_version` (append-only), `ppr_binding` (PR number primary key, append-only), `ppr_sequence`; partial unique index = one working draft per PR.

Precision (D-23): quantity ≤ 4 decimals, amount ≤ 2, unit price ≤ 8 (stored at 8). Anything beyond is refused in pre-validation (`PR_VALUE_PRECISION`, naming the PR line), never rounded.

## Access

`PPR_READ` all roles (REQUESTER-only: own department, others' PPRs answer 404); `PPR_REQUEST` REQUESTER (service also requires the requester's own department); `PPR_UNLOCK` PLAN_OFFICER, ADMIN.

## Not in this wave

Head-of-Procurement verification (Wave 5), PR change detection and cancellation with release (Wave 6), user interface (Wave 5). The print endpoint needs the bearer token, so browsers will open it through the UI.
