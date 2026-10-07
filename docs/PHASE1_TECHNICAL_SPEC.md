# PHASE1_TECHNICAL_SPEC.md

## Hospital Plan & PPR Control System — Phase 1 Technical Specification

**Status:** Pre-Coding Design Baseline / Source of Truth  
**Version:** 1.17-draft (incorporates Decision Record D-01 … D-38 and design gates G-1 … G-4, 2026-09-30; initial ledger event named `PLAN_ACTIVATED` (D-12); A-1/A-9 promoted to D-16; G-2 resolved by D-17; fiscal-year validity D-18; allocation D-19; print D-20; draft changes D-21; reconfirmation boundary D-22; precision D-23; user-interface stack D-24; HOSxP password check D-25; Wave 5 split and demo mode D-26; Head of Procurement appointment D-27; verification authority D-28; PR synchronization D-29; HOSxP cancellation D-30; invalid live evidence D-31; Wave 8 split D-32; alerts D-33; plan screens D-34; reports D-35; sync-failure alert after retry D-36; Wave 7 split, Plan Amendment rules D-37; Central Pool and Assigned Purchase, G-1 and G-3 resolved D-38)  
**Scope:** Phase 1 only  
**Explicitly out of scope:** PlanFin (moved to Phase 2)

> **Precedence:** Where any section below conflicts with §0 Decision Record, §0 governs.

---

## 0. Decision Record (binding clarifications, 2026-09-28)

### D-01 Central stock issue/return affects Plan QUANTITY only

In Phase 1, central stock issue/return affects Plan Quantity only.
Plan Amount is **not** reduced again for a stock issue, because the purchasing amount was already consumed when the central stock was procured through PR/PPR. This avoids double counting.

| Ledger event | Qty | Amount |
|---|---|---|
| `CENTRAL_STOCK_ISSUE` | decreases | unchanged (0) |
| `CENTRAL_STOCK_RETURN` | increases | unchanged (0) |

Affects: §10.2, §18.2, §18.3, §27, §40.12.

> **Superseded by D-39 (2026-10-02).** Stock issues and returns are never posted to the Plan Ledger and change neither plan quantity nor plan amount; the two event types are refused by the application and by the database. The table above is kept as history only.

### D-02 Internal PPR state is separate from HOSxP PR status

These are two independent fields and must never be merged.

**Internal PPR state** (owned by this system, explicit state machine):

- `DRAFT`
- `CONFIRMED_LOCKED`
- `PROCUREMENT_VERIFIED`
- `PR_CHANGED_REVIEW_REQUIRED`
- `UNLOCKED_FOR_REVISION`
- `CANCELLED`
- `CLOSED`

**HOSxP PR status** (external, synchronized, normalized by the adapter; stored separately, e.g. `hosxp_pr_status`):

- `DRAFT`
- `ACTIVE`
- `APPROVED`
- `CANCELLED`
- `COMPLETED`
- `UNKNOWN`

`PR_APPROVED` and `ACTIVE` are **not** internal PPR states. The native-code → normalized-status mapping remains TBD (§47).

Affects: §14.5, §24.5, §28.

### D-03 One PR number = one PPR for the lifetime of that PR number

A HOSxP PR number may have at most **one confirmed PPR ever**, including a PPR later in `CANCELLED` state.
The binding is created when the PPR first reaches `CONFIRMED_LOCKED` (see D-05 for drafts).
A cancelled PR must not receive a new PPR under the same PR number.
If procurement must restart after cancellation: **new HOSxP PR number → new PPR**.

Enforcement: the PR-to-PPR binding created at confirmation carries an unconditional database uniqueness constraint on the HOSxP PR identifier (not a partial/"active-only" index). Exact physical design is settled in Wave 1B/4.

Affects: §11.3, §12, §30, §38.2, §40.1.

### D-04 Multi-budget-category allocation is at PPR HEADER level

Category allocation is recorded once per PPR, not per PR Item.

Relationship: **PPR 1 → N `ppr_category_allocation` rows.**

```text
PR total      = 100,000
Fund source   = one fund source
Category A    =  70,000
Category B    =  30,000
Allocation Σ  = 100,000  (must equal PPR/PR total)
```

Rules:

- Every allocation row belongs to the same PPR.
- All categories must be under the same PR fund source.
- A category may appear **only once** per PPR.
- `SUM(allocation.amount)` = required PPR amount = sum of the PR item amounts; the PR header total must equal it (D-17).
- Users are **not** required to split individual PR Items across categories.
- Allocation rows do **not** independently post Plan Ledger usage.
- Plan usage remains item-based: **PR Item → Plan Item → Plan Ledger**.

Affects: §17, §29.3 (`ppr_item_allocation` → `ppr_category_allocation`), §31.5, §40.3.

### D-05 Draft does not bind the PR number (resolves H-2)

- A `DRAFT` is **unnumbered** and does **not** consume the D-03 lifetime binding.
- Only **one working draft** per PR may exist at a time.
- An abandoned draft may be **discarded** before confirmation (audited); a new draft may then be started for the same PR.
- Once `CONFIRMED_LOCKED` is reached, the PR number is permanently bound to that PPR (D-03).
- If that confirmed PPR is later `CANCELLED`, the same PR number may never receive another PPR.
- Restarting procurement requires a new HOSxP PR number.

### D-06 PPR numbering is unique, not gapless (resolves H-5)

PPR numbers must be unique, system-generated, fiscal-year scoped, and reset each fiscal year (`PPR-<FY>-<6-digit seq>`). Gapless numbering is **not** required; no gapless serialization mechanism is to be built.

### D-07 `CLOSED` is integration-TBD (resolves H-6)

`CLOSED` stays in the state model, but **no transition into it is defined yet**. It must ultimately derive from a verified terminal HOSxP process/status. Until real HOSxP status evidence exists, there is no automatic mapping and **no manual close action**; any manual close requires explicit future approval.

### D-08 Fiscal-year boundaries are configuration (resolves H-7)

Fiscal-year start and labeling are configuration, not hard-coded. A HOSxP PR `fiscal_year` field may be mapped through the adapter **only if** its existence is verified; it is not assumed.

### D-09 Reconfirm after revision is a requester attestation (resolves A-4)

- `PLAN_OFFICER` or `ADMIN` may **unlock** a PPR and perform their governance/review functions.
- The final **reconfirm** must be performed by an authorized `REQUESTER` / department staff **of the requesting department**. *(Amended by D-41: by the `REQUESTER` who created the PPR.)*
- `PLAN_OFFICER` and `ADMIN` must **not** reconfirm on behalf of the requesting department merely because they hold unlock authority.

Reason: unlock/governance authority and requester attestation remain separate responsibilities.
The role rule is enforced in the domain state machine; the *same-department* check is enforced server-side when the API layer exists (Wave 1B/5).

### D-10 Only critical PR changes force review (resolves A-5)

- **Critical** PR change → `PR_CHANGED_REVIEW_REQUIRED`.
- **Non-critical** change (metadata) → logged/audited only; **no PPR state change**.
- A PPR in `PR_CHANGED_REVIEW_REQUIRED` has **no acknowledgement shortcut** that bypasses revalidation. The only path back is:

```text
PR_CHANGED_REVIEW_REQUIRED
  → UNLOCK (Plan Officer / Admin, with reason)
  → UNLOCKED_FOR_REVISION → full revalidation
  → RECONFIRM (Requester of the requesting department)
  → CONFIRMED_LOCKED
```

Critical (control-sensitive) fields, per §16 / §40.7:

- Item ID / PR item composition (items added, removed or re-identified)
- Quantity
- Unit price where it changes amount/control semantics
- Amount
- PR total
- Fund source
- Budget category
- PR status where it affects validity/cancellation

Everything else is non-critical. The shared PR change classifier is implemented in `domain/pr_changes.py`. It is already used by Wave 4 confirmation logic and will also be reused by Wave 6 synchronization.

> Clarified by D-21 / D-22 (2026-09-29): fiscal year and requesting department are identity-boundary fields and are always treated as critical. The same classifier also decides, before confirmation, whether a draft must be prepared again (D-21).

### D-11 Budget envelope is checked per exact budget category (Wave 2)

For §9.1, each approved plan budget line — (fiscal year, fund source, budget category) — must equal the sum of the planned amounts of the Plan Items attached to **that exact category**. Amounts are **not** rolled up from child categories into parents.

### D-12 Envelope validation and `PLAN_ACTIVATED` posting happen at activation (Wave 2)

- The budget envelope (§9.1) and plan integrity are validated when a plan year moves `APPROVED → ACTIVE`. Activation is refused if any check fails.
- On successful activation, one `PLAN_ACTIVATED` ledger entry per Plan Item (planned quantity and planned amount) is posted **in the same transaction** as the state change and its audit record.
- Plan budgets and Plan Items may be created, changed or removed freely while the plan year is `DRAFT`; while `APPROVED` only as audited data-entry corrections (D-15); after `ACTIVE`, only through Plan Amendment (§20, Wave 7).
- Naming (pre-Wave-3 hardening, product-owner decision): Director approval = plan-year state `APPROVED`; operational activation = plan-year state `ACTIVE`; initial ledger event = `PLAN_ACTIVATED`; audit action = `PLAN_ACTIVATED`. The event was first shipped as `PLAN_APPROVED` (migration 0002) and is relabelled by migration 0003; no business rule changed.

### D-13 Planned amount is not forced to equal quantity × estimated price (Wave 2)

A Plan Item's planned amount is entered and stored as approved. It is **not** required to equal planned quantity × estimated unit price (the price is an estimate, §9.3). A mismatch is reported as a **warning**, never a blocking error.

### D-14 Master data comes from HOSxP through the gateway (Wave 2)

Plan Items reference HOSxP masters (item, department, fund source, budget category) by HOSxP source id (§8). In Wave 2 an authorized user can run a **manual master sync** through `HosxpGateway` (currently the mock), recorded in `sync_run`. Nightly sync and PR sync remain in Wave 6. Master records are never invented locally.

### D-15 Data-entry correction while APPROVED (Wave 2)

`APPROVED` means the Director has already approved the business plan. If activation fails because the **system-entered data** does not reconcile with that approved plan, the Plan Officer may correct the data while the plan year remains `APPROVED`:

- a correction may only make the system **match the approved source document**;
- every correction requires a **reason** and a **reference to the approved source document**, and is audited as a correction (`PLAN_BUDGET_CORRECTED`, `PLAN_ITEM_CORRECTED`, …);
- `ACTIVE` stays blocked until validation passes;
- a change to an approved business value (item, quantity, amount, budget category, fund source, or any other approved value) is **not** a correction: it requires Plan Amendment and Director approval (§20, Wave 7);
- the plan year is **not** sent back to `DRAFT` for data-entry reconciliation.

The system cannot judge business meaning; accountability rests on the mandatory reason, the source reference and the audit trail. After `ACTIVE`, plan data is locked (Plan Amendment only).

### D-16 One DEPARTMENT Plan Item per fiscal year + department + fund source + HOSxP item (product-owner decision before Wave 3)

> **Matching superseded by D-43 (2026-10-07):** a PR line is no longer matched to a Plan Item automatically; the requester chooses the row on every PPR. The uniqueness rule below still applies to rows that carry a HOSxP item.

Within one fiscal year, the same HOSxP item must **not** have more than one `DEPARTMENT` Plan Item for the same department and the same fund source, **even if the budget categories differ**:

```text
Fiscal Year + Department + Fund Source + HOSxP Item = one DEPARTMENT Plan Item
```

- Reason: the Plan Item controls the approved quantity and amount for that item.
- If more than one budget category is needed to fund a purchase because the original category is short, this is represented at the PPR header level by **PPR 1 → N `ppr_category_allocation`** (D-04). The Plan Item is **not** duplicated to represent funding from another budget category.
- Consequences (formerly working assumptions A-1 and A-9, now decided):
  - PR line matching for DEPARTMENT plans uses (HOSxP item, owner department, fund source) within the resolved fiscal year; 0 matches → `NOT_IN_PLAN`, more than one → `AMBIGUOUS_PLAN_MATCH` (fail closed).
  - Activation is refused if two DEPARTMENT Plan Items share (HOSxP item, owner department, fund source) in one fiscal year (`DUPLICATE_DEPARTMENT_ITEM`).
- Central and Assigned matching remain under G-1 / G-3.

### D-17 Required PPR amount = sum of PR item amounts; header total must match (resolves G-2)

Product-owner decision (2026-09-29), option A:

- The **required PPR amount** is the **sum of the PR item (line) amounts** retrieved from HOSxP. This is the same basis as the per-line plan checks (PR Item → Plan Item) and the amount that `SUM(ppr_category_allocation.amount)` must equal (D-04).
- The HOSxP **PR header total** is used as an integrity check only. It must equal the sum of the line amounts **exactly (to the satang)**. If it does not, the PR is treated as inconsistent: pre-validation fails and the PPR **cannot be confirmed**; the user is told to correct the PR in HOSxP first. The system never picks one of the two figures and never adjusts either.
- Amount basis: at the PR stage there is no quotation, VAT or discount yet; PR unit prices and line amounts are estimates that **already include VAT**, on the same VAT-inclusive basis as the approved plan amounts. (Both confirmed by the product owner, 2026-09-29.) The system does **not** calculate VAT.
- VAT, quotations and actual purchase amounts become known only at PO, which is outside Phase 1.
- Evidence: one real PR (50 lines) in which every line amount = quantity × unit price, the sum of lines equals the header total exactly, and the VAT field is blank.

### D-18 A plan is valid only within its own fiscal year (product-owner decision, 2026-09-29)

- A fiscal year's plan **expires when that fiscal year ends**. After that, no new PPR may be issued against it, even if the plan year has not yet been moved to `CLOSED`.
- A PPR may be issued only if the **budget year shown on the HOSxP PR** ("ปีงบประมาณ") is the **current fiscal year** at the time of PPR creation (computed from the configured fiscal-year boundaries, D-08, using the server date), and that year's plan is `ACTIVE`.
- The rule is checked at pre-validation and again at confirmation, so a PPR cannot be confirmed after its fiscal year has ended. The server date at the moment of the action decides (confirmed by the product owner, 2026-09-29): e.g. a PPR for a budget-year-2569 PR started on 30/09/2569 but confirmed on 01/10/2569 is **not** confirmable, because the 2569 plan has expired.
- If the PR's budget year is not the current fiscal year, the PR is rejected for PPR (no PPR is created). Example: a PR dated 22/09/2569 (fiscal year 2569) whose budget year is 2568 → rejected, because the 2568 plan has expired.
- Evidence for D-08: the budget-year field is visible on the real HOSxP PR screen. Its source in the HOSxP data is still verified only when the real adapter is built (Wave 9); the mock carries it as a contract field until then.
- HOSxP is not changed: the PR may still proceed in HOSxP; it simply has no PPR.

### D-19 PPR budget-category allocation: default, eligible categories, adjustment reference (product-owner decision, 2026-09-29)

- **Default:** when a PR is first prepared as a PPR (draft), the system creates **one** allocation row: the PR's current HOSxP budget category ("ชื่องบ" on the PR header) for the **full required amount** (D-17). The user adds further categories only when needed.
- **Eligible additional categories:** only categories that have a Plan Budget line in the **same fiscal year** and for the **same fund source** as the PR, in the approved plan. No HOSxP budget-category hierarchy is inferred or invented.
- **More than one category:** confirmation requires a non-empty **`adjustment_reference`**: free text proving that the necessary HOSxP budget/category adjustment has already been handled (it need not be a formal document number). It is stored in the PPR version snapshot and in the audit trail, and shown on the printed PPR.
- D-04 rules still apply: one fund source, each category at most once, Σ allocation = required amount, allocations never post to the Plan Ledger.

### D-20 Printed PPR is an A4 browser-printable page (product-owner decision, 2026-09-29)

The PPR document (§35) is an A4 HTML page with print CSS; the browser's "Print / Save as PDF" is sufficient in Phase 1 (no separate PDF engine). At minimum it shows: PPR number, version, PR number, fiscal year, requesting department / requester, fund source, PR items with their matched Plan Items, category allocations, requested quantity/amount, remaining quantity/amount, validation status, `adjustment_reference` when applicable, a verification code / document reference, and signature areas.

### D-21 PR changes between draft and confirmation: critical vs non-critical (product-owner decision, 2026-09-29; replaces working assumption A-14)

Before a `DRAFT` is confirmed, the PR is fetched **live** from HOSxP and compared field by field with the PR snapshot stored when the draft was prepared. The comparison uses the **same classification as D-10** (one classifier, shared with the Wave 6 sync):

- **Critical** (control-sensitive) → confirmation is **blocked**; the requester discards the draft and prepares it again:
  - PR item composition / item identity (lines added, removed or re-identified; item id changed)
  - quantity
  - unit price where it changes amount/control semantics (i.e. together with a change of that line's quantity or amount)
  - item amount
  - required PR/PPR amount (PR header total)
  - fund source, budget category
  - requesting department, fiscal year (identity-boundary fields, D-22)
  - PR status where it affects validity or cancellation (a change to or from `CANCELLED`)
- **Non-critical** (metadata, e.g. item name, unit label, requester name, PR date, a unit price change that leaves quantity and amount unchanged, a status change that does not affect validity) → does **not** force the draft to be discarded. Confirmation proceeds **if every current validation still passes**; the detected differences are recorded in the audit trail and in the confirmation snapshot, and the snapshot contains the **latest live PR values**. Stale draft data is never used at confirmation.

### D-22 Reconfirmation after unlock: what may change (product-owner decision, 2026-09-29; confirms working assumption A-15)

- After a PPR has been unlocked, reconfirmation may accept changed PR lines, item quantities, unit prices, item amounts and budget category, but **only after full revalidation** against the current live HOSxP PR, the current `ACTIVE` plan, the current Plan Ledger balances, the allocation rules (D-04, D-19) and every other confirmation rule. The differences from the previous confirmed version are recorded in the audit trail and the new version snapshot.
- **Identity-boundary fields** of an existing PPR — **fiscal year, requesting department, fund source** — must not change. If any of them changed in HOSxP, the existing PPR is **not** reconfirmed: the system returns a clear business-rule error (`PR_IDENTITY_CHANGED`) and the case requires **governance review**. The system does not cancel the PPR, does not create a replacement PPR and does not invent a next workflow. The PR-number lifetime binding (D-03) still applies.
- Identity-boundary fields are always critical changes (for drafts, D-21, and for confirmed PPRs under D-10).

### D-23 Precision of values received from HOSxP (product-owner decision, 2026-09-29; confirms working assumption A-16)

Control values from HOSxP are never rounded silently. Phase 1 supports: **quantity** at most 4 decimal places, **amount** at most 2 decimal places, **unit price** preserved up to 8 decimal places. A PR line exceeding any of these fails validation explicitly (`PR_VALUE_PRECISION`), naming the offending PR line; the system does not round and continue.

### D-24 User-interface stack (product-owner decision, 2026-09-29; closes the open frontend choice)

- The user interface is a **Node.js + Express** web application that renders server-side HTML pages (Thai language) and calls the existing Python/FastAPI API. It is a thin presentation layer ("backend for frontend"):
  - every business rule, validation, state transition, ledger posting and authorization decision stays in the Python API, which remains the only component that talks to PostgreSQL and to HOSxP;
  - the Express application keeps the API session token in a signed, HTTP-only cookie that page scripts cannot read, and forwards it on every API call; every form carries an anti-forgery (CSRF) token; hiding a button is never the control (§22.9) — the API refuses what a role may not do;
  - the Express application never reads the database directly and never calls HOSxP.
- The printed PPR (D-20) is the API's print page, shown through the Express application.

### D-25 HOSxP password check (product-owner statement, 2026-09-29; narrows §21)

- The product owner states that HOSxP stores user passwords as an **MD5 hash**. Login is therefore verified by computing the MD5 hash of the password the user typed and comparing it with the stored hash read from HOSxP through the **read-only** account.
- Which table and columns hold the username, the hash, the user's department and the active flag are **not** assumed: they come from the site query file (queries `get_login_user` and `get_user_profile`, ADR-007 pattern), written from the hospital's HOSxP schema by IT.
- **Provisional compatibility behaviour (not verified against a real HOSxP).** Until the evidence below is recorded, the adapter:
  - hashes the password as its **UTF-8** bytes;
  - expects the stored value to be a **hexadecimal MD5 digest** and compares digests **case-insensitively** (surrounding spaces ignored);
  - takes "active" from whatever **site-provided field** the query returns as `active`.
  This behaviour exists only so the system can be built and tested; it is **not** evidence that HOSxP authentication works this way.
- **Go-live gate (real-HOSxP evidence required).** Login against the hospital's real HOSxP must not go live until all four of the following are verified on that hospital's HOSxP and recorded (the evidence, who verified it and when):
  1. **Query / table / column mapping** — the site query file's `get_login_user` and `get_user_profile` read the correct table(s) and column(s) for username, user id, password hash, display name, department and account status, checked with real accounts.
  2. **Stored password-hash format** — whether the stored value is a raw 32-character hexadecimal MD5 digest or another representation or wrapper (for example another encoding of the digest, or a digest with added material), and any prefix, suffix, letter-case or whitespace semantics.
  3. **Password text encoding before hashing** — which character encoding HOSxP applies to the password before hashing, confirmed with at least one account whose password contains Thai characters.
  4. **Account-active semantics** — exactly which HOSxP value(s) mean an account is active/enabled or inactive/disabled, including locked, expired or deleted accounts, and how the site query maps them to `active`.
  If any item differs from the provisional behaviour, the adapter is changed to match the evidence (with tests) before go-live. No HOSxP table name, column name, status value or active-flag value is assumed in this specification or in code.
- This application never stores, logs or displays a password or a password hash; it only compares, then issues its own session (§21). Nothing is written to HOSxP.
- Until the site query file is available, login runs against the mock HOSxP only (D-26).

### D-26 Wave 5 split and demonstration mode (product-owner decision, 2026-09-29)

- **Wave 5A:** runnable servers (API and Express), the PPR screens (prepare draft with pre-validation, allocation, confirm, discard, unlock, versions, print), login, and a **demonstration mode**.
- **Wave 5B:** Head of Procurement verification with the annual appointment check (§14.4, §22.4, §23), search (§32) and the tracking timeline (§33).
- **Demonstration mode** exists so the system can be tried before HOSxP is connected. It runs only with the mock HOSxP (it refuses to start in `sql` mode), is enabled only by an explicit switch, uses clearly labelled demo users and `MOCK-*` data, and seeds an `ACTIVE` plan for the current fiscal year in the development database. Every page shows that the system is in demonstration mode. It must never be enabled on a production server.

### D-27 Head of Procurement appointment: one at a time, inside its fiscal year (product-owner decision, 2026-09-29; details §23)

- An appointment (§23) names a user, a fiscal year, `effective_from`, `effective_to` (optional) and a status (`ACTIVE` / `REVOKED`), with who created it and when.
- **No overlap:** on any date at most **one** `ACTIVE` Head of Procurement appointment is effective. A new appointment may start only after the previous one ends; to hand over mid-year, the current appointment is ended first (its `effective_to` is set), then the new one is created.
- **Inside the fiscal year:** `effective_from` and `effective_to` must fall within the appointment's fiscal year (D-08 boundaries). An appointment without `effective_to` ends at the end of that fiscal year.
- Appointments are managed by `ADMIN` (§22.8) and only for an active user holding the `HEAD_OF_PROCUREMENT` role. They are never deleted; every change requires a reason and is audited. History is kept so that a past verification can always show which appointment authorized it. There are two distinct changes (product-owner clarification, 2026-09-29):
  - **End early** — an operational handover. It only shortens the appointment. So that a new appointee can start today with date-level appointments, the previous appointment may end **yesterday** (server date). It is never shortened to before its `effective_from`, nor to before the latest date on which it was actually used to verify a PPR.
  - **Revoke** — the appointment record itself was entered or issued in error. An appointment that has already authorized **any** PPR verification **cannot be revoked** through the normal workflow (`APPOINTMENT_IN_USE`); the appointment and all authority evidence are preserved, and the case goes to end-early or governance review instead. An appointment never used for a verification may be revoked; the row stays in history and its dates become available for a corrected appointment.
- No appointment is created over dates that already hold verification evidence under another appointment (`APPOINTMENT_OVERLAPS_EVIDENCE`), so history never shows an apparently competing appointee for a day on which a verification was authorized.

### D-28 Procurement verification authority (product-owner decision, 2026-09-29; details §14.4, §22.4, §40.9)

- Verification ("ตรวจสอบและยืนยันแล้ว", `CONFIRMED_LOCKED` → `PROCUREMENT_VERIFIED`) requires, checked server-side at the moment of verification: the `HEAD_OF_PROCUREMENT` role **and** an `ACTIVE` appointment of that user that is effective on the **server date of verification** and whose fiscal year is the fiscal year of that date. The PPR's own fiscal year does not select the appointment (a PPR confirmed in one fiscal year and verified in the next needs the new year's appointee).
- A `PROCUREMENT` user without such an appointment cannot verify; a previous-year appointment never authorizes a verification dated in a later fiscal year.
- The verification records the user, date/time, the appointment, and the PPR version verified (§14.4), and is audited. It verifies the **current** version only; after unlock and reconfirmation the new version must be verified again.
- Verification changes no Plan Ledger amount, reads and writes nothing in HOSxP, and implies nothing about HOSxP PR approval or PO status. The paper signature remains outside the system.

### D-29 PR synchronization: scope, sequence, evidence, permissions (product-owner decision, 2026-09-29; Wave 6; details §16, §25, §26)

- **Scope.** The nightly/full sync and the normal manual sync inspect open confirmed-or-later PPRs only: `CONFIRMED_LOCKED`, `PROCUREMENT_VERIFIED`, `PR_CHANGED_REVIEW_REQUIRED`, `UNLOCKED_FOR_REVISION`, in every fiscal year. They exclude `DRAFT`, `CANCELLED` and `CLOSED`. **`DRAFT` PPRs are never synchronized** and have no sync action: they hold no plan usage, and confirmation already re-validates against live HOSxP (D-21). A `CANCELLED` PPR is looked at only by an explicit, observation-only **governance re-check** of that one PPR by `PLAN_OFFICER` / `ADMIN` (D-30); it is never included in the nightly scan. `CLOSED` has no inbound transition and receives no sync logic (G-4).
- **Live evidence, no lock held over HOSxP I/O.** For each PPR: (1) read the PPR id, PR number, state and current version; (2) fetch the live PR header (and, when needed, the items) **outside** any database transaction, recording the actual `observed_at`; (3) validate and normalize the evidence — when the only failure is a header/items inconsistency that a concurrent HOSxP edit can explain (total ≠ Σ items, no items, duplicate or inconsistent item lines), fetch header and items **once more** immediately, still outside any lock, and use that fresh observation; if it is valid the inconsistent first read causes no state change (it may be kept as technical evidence); if still invalid, D-31 applies; there is never more than one re-fetch, and a precision violation (D-23) is not re-fetched and never rounded; (4) open one transaction, lock the PPR row `FOR UPDATE` and re-read state, version and PR binding; (5) if the PPR moved meanwhile, re-evaluate the observation against the new state or record `SKIPPED_STATE_MOVED`; (6) lock plan items only when a cancellation release will occur; (7) write the state transition, any ledger release, the observation and the audit evidence, and commit — all or nothing. One transaction per PPR: one PPR's failure never rolls back another's result.
- **Shared classifier.** Change detection uses `domain/pr_changes.py` (the same `diff_pr` / `critical_only` / `identity_changes` used by D-21 and D-22) against the current confirmed version's PR snapshot, with the live PR serialized by the same canonical snapshot serializer used at confirmation. No second classifier exists.
- **Outcomes.** Critical change (and identity change, D-22) → `PR_CHANGED_REVIEW_REQUIRED` from `CONFIRMED_LOCKED` / `PROCUREMENT_VERIFIED`; the PPR number, versions and snapshot are kept; no plan release; no automatic adoption or reconfirmation; exit only by unlock → full revalidation → requester reconfirmation → procurement verification of the new version. Non-critical change → audited, display status updated, no state change. HOSxP unavailable → nothing changes; the failure is recorded and retried later.
- **Evidence.** Every attempt is traceable: the run (`sync_run`: mode `MANUAL` / `NIGHTLY`, requester, start/end, counts, status `SUCCEEDED` / `PARTIAL` / `FAILED`) and one append-only observation per PPR attempt (observed time, normalized and native status, classification, diff, sanitized live PR in the canonical snapshot form, state before/after, whether a release occurred, a safe error code and message). No credentials, raw rows or unrelated data are stored.
- **Timeline.** HOSxP-derived entries appear only when actually observed by a sync and are shown as "observed in HOSxP", distinct from system actions. Unchanged observations are not shown on the timeline.
- **Permissions.** Only `PLAN_OFFICER` and `ADMIN` trigger a sync (all open PPRs, one PPR, a retry of a run's failures, or the governance re-check of one cancelled PPR). `PROCUREMENT` and `HEAD_OF_PROCUREMENT` may view sync runs, observations, queue effects and the latest observed HOSxP state; anyone who can read a PPR may view that PPR's observations. Governance items (`NOT_FOUND`, `INVALID_EVIDENCE`, anomalies) are listed for `PLAN_OFFICER` / `ADMIN`.
- **Scheduling.** The application provides a command (`python -m ppr.cli sync-prs --mode NIGHTLY`); the hospital's scheduler (Windows Task Scheduler) owns the schedule. 02:00 local time is only a deployment example; the production time is set by hospital IT so that it does not collide with HOSxP backup, replication, maintenance or other overnight jobs. It is not a business rule.

### D-30 HOSxP PR cancellation (product-owner decision, 2026-09-29; Wave 6; details §14.6, §24.5, §40.8)

- **Only an explicitly mapped status cancels.** A PPR moves to `CANCELLED` (`CANCEL_FROM_HOSXP`) and its plan usage is released only when the PR's native status is mapped by hospital IT, in the site query file, to normalized `CANCELLED`. `UNKNOWN`, blank, unmapped and `NOT_FOUND` never cancel and never release. The system ships with an **empty** cancellation mapping, so no cancellation can occur until the go-live evidence below exists. No HOSxP table, column or status value is assumed.
- **Go-live evidence gate (REAL-HOSXP EVIDENCE REQUIRED):** the query/table/column that supplies the native PR status; one or more real cancelled PR examples; the native cancelled value(s); confirmation that this status is authoritative for cancellation; whether cancellation is final or can be administratively reversed in HOSxP; who verified it and when.
- **Release.** Cancellation releases, per Plan Item, exactly what the PPR number still holds in the Plan Ledger (confirmed minus already released) as `PPR_RELEASED` entries with deterministic idempotency keys — it reverses the application's own confirmed usage and never recalculates usage from the live PR. The state transition, ledger entries, observation and audit evidence commit in one transaction. Repeated observations, concurrent workers, retries and crashes can never release twice (terminal state, row lock, zero outstanding after release, unique ledger key, unique released-observation per PPR). Remaining quantity and amount never go negative.
- **PR binding unchanged (D-03).** A cancelled PPR keeps its number and history; the PR number is never reusable; a new procurement needs a new HOSxP PR.
- **Not found.** A bound PR that HOSxP does not return is recorded as `NOT_FOUND`: no state change, no release, surfaced to `PLAN_OFFICER` / `ADMIN` for governance investigation. Repeated `NOT_FOUND` never becomes a cancellation; no disappearance threshold exists.
- **Cancelled, then seen as not cancelled.** Once cancelled and released, a PPR stays `CANCELLED`. Such a PPR is not scanned nightly; `PLAN_OFFICER` / `ADMIN` may explicitly re-check one cancelled PPR (observation only). If HOSxP then reports the bound PR with a mapped status other than `CANCELLED`, the PPR stays `CANCELLED`, no transition, no ledger re-post, no PR-number reuse; the anomaly `CANCELLED_PR_REACTIVATED_EXTERNALLY` is recorded and shown in governance and history. If HOSxP still reports `CANCELLED`, the re-check is recorded and nothing else happens (no second release).
- **Unknown is not active.** An `UNKNOWN`, blank or unmapped native status is not evidence that the PR is active; it means only that no authoritative cancellation status is available from this observation. Field-level change detection still runs when binding and evidence are valid, but such a status never by itself cancels, closes, reactivates or causes any status-derived transition or anomaly.
- **Separate from G-4.** This decision maps cancellation only. Which HOSxP status (if any) means terminal completion / `CLOSED` remains gate G-4 (open): `CLOSED` is not inferred from PO creation, receiving, procurement verification or any status until decided.

### D-31 Invalid live evidence versus cancellation (product-owner decision, 2026-09-29; Wave 6)

- **Control-evidence failure.** When the live PR of a confirmed-or-later PPR fails a control-evidence rule — header total unavailable or ≠ Σ item amounts (D-17), unsupported precision (D-23), no items, duplicate PR line ids — the observation is recorded as `INVALID_EVIDENCE` with the exact code(s) and the affected field/line, plus the sanitized evidence. It is **not** presented as a classifier diff. State: `CONFIRMED_LOCKED` / `PROCUREMENT_VERIFIED` → `PR_CHANGED_REVIEW_REQUIRED`; `PR_CHANGED_REVIEW_REQUIRED` and `UNLOCKED_FOR_REVISION` stay (reconfirmation keeps failing until the live evidence is valid). No plan release.
- **Cancellation precedence.** If the observation is reliably bound to the same PR and its native status is mapped to `CANCELLED` (D-30), cancellation and release proceed even when the item/header control evidence is invalid: the release reverses the application's own confirmed usage and uses nothing from the malformed PR.
- **Unreliable binding or status — no state change (confirmed 2026-09-29).** If the observation cannot be reliably bound to the same PR or its status cannot be trusted — more than one header row, a site-query/configuration failure, bound `pr_no` mismatch, confirmed `pr_id` mismatch, unparseable header, unreadable/malformed status, or an items-query failure that prevents a reliable comparison — the system records `INVALID_EVIDENCE` (class binding/status), surfaces it to `PLAN_OFFICER` / `ADMIN` governance, and does **not** change the PPR state, cancel or release. A query/configuration fault can affect many PPRs at once and must not cause mass business-state transitions. An unmapped or `UNKNOWN` status is not "unreliable" (D-30).
- **Transient inconsistency.** Control-evidence failures other than precision get at most one immediate fresh fetch outside any lock before D-31 is applied (D-29).

### D-32 Wave 8 split and scope (product-owner decision, 2026-09-30)

- Wave order after Wave 6 is **8 → 7 → 9 → 10**; every wave (and each part of a split wave) still stops for approval.
- **Wave 8A:** role dashboards — Requester (§31.2), Plan Control Center (§31.8) and Executive (§31.9); the Procurement dashboard is the existing queue (§31.7) — the in-app alerts of D-33 and their settings, and the plan management screens of D-34.
- **Wave 8B:** the reports of §37 with Excel export (`.xlsx` produced by the API), **except** the *Plan Amendment* and *Central Plan Usage* reports, which move to Wave 7 and are built with those features. Until then the dashboards show no Amendment, Central Plan or Assigned Purchase figures (those items of §31.8 / §31.9 arrive with Wave 7).
- Dashboards only read: they post nothing to the ledger, change no state and never call HOSxP. Every figure uses the same department scope as reading the underlying PPR or Plan Item (§22.1): a user whose only role is `REQUESTER` sees their own department.

### D-33 Alerts: in-app, derived, configurable thresholds (product-owner decision, 2026-09-30; details §34)

- **Channel.** Phase 1 alerts are shown **inside the application only** (dashboard and alert list). Nothing is sent outside the hospital network. LINE (and any other push channel) is **Phase 2**.
- **Derived, never stored.** An alert is computed from the current data each time it is shown; there is no alert record and no acknowledgement. When the cause is gone (e.g. the PPR is cancelled or closed, the plan year is no longer `ACTIVE`, a later synchronization succeeded) the alert disappears by itself, so cancelled/closed cases never remain as stale alerts (§34).
- **Types.**
  - `PPR_INACTIVE` — an open confirmed-or-later PPR (`CONFIRMED_LOCKED`, `PROCUREMENT_VERIFIED`, `PR_CHANGED_REVIEW_REQUIRED`, `UNLOCKED_FOR_REVISION`; the same set as the queue's *inactive* bucket) whose last change is older than the inactivity threshold. Drafts hold no plan usage and do not alert.
  - `PR_CHANGED` — a PPR in `PR_CHANGED_REVIEW_REQUIRED`.
  - `PLAN_LOW` — a Plan Item of an `ACTIVE` plan year whose remaining quantity or remaining amount is above zero and at most the low-plan threshold percent of its current plan (approved plus amendments).
  - `PLAN_EXHAUSTED` — a Plan Item of an `ACTIVE` plan year whose remaining quantity or remaining amount is zero.
  - `SYNC_FAILURE` — the latest full PR synchronization run ended `FAILED` or `PARTIAL`, or the latest master-data synchronization run ended `FAILED`.
- **Defaults and configuration.** Inactivity threshold **30 days** (1–365); low-plan threshold **20 %** (1–99). Both are stored in the database, changed only by `ADMIN` with a reason, and every change is audited (`ALERT_SETTING_CHANGED`, §36 "configuration change"). The procurement queue's *inactive* bucket uses the same inactivity threshold.
- **Visibility.** A PPR alert is shown only to users who may read that PPR; a plan alert only to users who may read that Plan Item (same scopes as §22.1); `SYNC_FAILURE` only to roles that follow synchronization (`SYNC_READ`).
- Alerts never block or replace a hard control (§34).

### D-34 Plan management screens in Wave 8A (product-owner decision, 2026-09-30)

- Wave 8A adds screens for what the API already offers: plan years (create, approve, activate with the D-12 validation report, close), budget lines, Plan Items, the activation validation report, master-data synchronization and Plan Item balances.
- The screens add **no plan rule**: every change is the existing API call and every refusal is the API's answer (D-11 … D-16). While a plan year is `APPROVED`, a change asks for the correction reason and source reference of D-15. After `ACTIVE` the screens offer no edit (Plan Amendment is Wave 7).
- Only `PLAN_OFFICER` sees the change actions (`PLAN_MANAGE`, `PLAN_YEAR_MANAGE`); master synchronization follows `SYNC_RUN`; every role may read, with the §22.1 department scope.

### D-35 Wave 8B reports and Excel export (product-owner decision, 2026-09-30; details §37)

- **Reports** (nine; *Plan Amendment* and *Central Plan Usage* are Wave 7, D-32):
  - *Plan Summary* — per budget line (fund source, budget category): approved budget, total of its Plan Items, used, remaining.
  - *Plan Remaining* — per Plan Item: planned / used / remaining quantity and amount, remaining share and low/exhausted flag (D-33 threshold).
  - *Department Utilization* — per owner department; *Budget Category Utilization* — per budget category (all fund sources).
  - *PPR Register* — every PPR that has been confirmed at least once (has a PPR number), in any later state.
  - *Pending PPR* — PPRs **not yet verified by Procurement**: `DRAFT`, `CONFIRMED_LOCKED`, `UNLOCKED_FOR_REVISION`, `PR_CHANGED_REVIEW_REQUIRED`.
  - *Cancelled PPR* — `CANCELLED`; *PR Changed* — `PR_CHANGED_REVIEW_REQUIRED`.
  - *Audit Report* — the audit log (§36).
- **Filters** (where they apply): fiscal year, department, fund source, budget category, item, status, and a date range. For the PPR reports the date range is the **first confirmation date** (version 1); drafts have none and are excluded when a date range is given. For the audit report it is the event time.
- **Who.** Every role may run the reports, with the §22.1 scope (a user whose only role is `REQUESTER` sees their own department). The Audit Report needs `AUDIT_READ` (Plan Officer, Admin).
- **Excel.** Each report is shown on screen and can be downloaded as `.xlsx` produced by the API from the same rows (numbers stored as numbers, Thai headings, the filters written above the table). A report is capped at 5,000 rows and says so when cut. Each download is audited (`REPORT_EXPORTED`, with the report and filters). Reports never change plan or PPR data and never call HOSxP.

### D-36 Sync-failure alert after a retry (product-owner decision, 2026-09-30; amends D-33)

- For the latest finished full PR synchronization run that ended `FAILED` or `PARTIAL`, its **failed PPRs** are those whose attempt did not give a trustworthy observation (`UNAVAILABLE`, `ERROR`, invalid evidence of class `BINDING` / `STATUS`) plus the PPRs the run did not attempt — exactly the set a retry run repeats.
- A failed PPR is **resolved** when a later finished retry run or one-PPR synchronization observed it with a trustworthy result, or when it is no longer in synchronization scope (e.g. cancelled or closed) so there is nothing left to repeat.
- The `SYNC_FAILURE` alert **disappears only when every failed PPR of that run is resolved and no other error of the run remains**. Until then it stays and shows how many PPRs are still outstanding. A run failure with no identifiable failed PPR keeps the alert until a later full run.
- **Audit history.** The end of every PR synchronization run records the failed PPRs (`PR_SYNC_FINISHED`, with their ids). The end of every retry run records the result against the original run (`PR_SYNC_RETRY_RESULT`: failed, resolved, still outstanding), so both the failure and its correction stay in the audit log.
- The master-data `SYNC_FAILURE` rule of D-33 is unchanged (cleared by a later successful master synchronization).

### D-37 Wave 7: Plan Amendment rules; Central Pool and Assigned Purchase directions (G-1, G-3 not yet frozen) (product-owner decision, 2026-09-30)

- **Wave 7 is split.** **7A — Plan Amendment** (this decision, frozen) is built first. **7B — Central Pool and Assigned Purchase** is built only after G-1 and G-3 are frozen: the directions below were agreed on 2026-09-30, but their detailed decision points are still open, so nothing of 7B is implemented until the product owner freezes them.
- **Plan Amendment (§20).** Allowed only while the plan year is `ACTIVE`. Recorded by `PLAN_OFFICER` only, and only with the number and date of the Director's approval document (the approval itself stays on paper; no electronic signature in Phase 1). Every amendment keeps each budget line balanced (D-11): when a category's approved amount rises, the increase goes to existing Plan Items (amount / estimated price) or to new Plan Items **in the same amendment**, and the items of that category always sum to its new approved amount. Transfers of quantity or amount are allowed only between Plan Items of the **same fund source**. Nothing may be reduced below what is already used (§20). Each amendment posts `PLAN_AMENDMENT` ledger entries and keeps before/after history.
- **Plan Amendment details (7A, product-owner answers 2026-09-30).**
  - One amendment is one atomic set of changes with a reason, the approval document number and its date; it is numbered 1, 2, … within the plan year and takes effect when recorded. A mistaken amendment is corrected by a further amendment, never by editing it.
  - **Budget lines:** the approved amount of an existing line may rise or fall; a **new budget line** (a fund source / category not yet in the plan year) may be added, together with its Plan Items, in the same amendment. After every amendment every budget line is balanced (D-11).
  - **Plan Items, numbers:** quantity, estimated unit price and planned amount may rise or fall, but never below what is used (§20). An unused item may be reduced to **zero**, which closes it: it stays in the plan and its history, its remaining is zero, so no new PPR can use it. A quantity of zero requires an amount of zero.
  - **Plan Items, other data:** owner department, HOSxP item, budget category and purchasing department may be changed **only while the item is unused** (no confirmed usage held). A used item is changed by reducing it to what is used and adding a new item in the same amendment. The **fund source never changes** (transfers stay within one fund source); the plan type never changes.
  - **Plan types:** numbers and data of `DEPARTMENT`, `CENTRAL` and `ASSIGNED` items may be amended. **Assigned-purchase demand is not amended in 7A** (it belongs to G-3), so a new `ASSIGNED` item (which needs demand) waits for 7B; new `DEPARTMENT` and `CENTRAL` items are allowed. An `ASSIGNED` item keeps its purchasing department.
  - **Ledger:** each amended item posts one `PLAN_AMENDMENT` entry with the change of quantity and amount (none when only data changed); a new item posts `PLAN_ACTIVATED` of zero followed by `PLAN_AMENDMENT` of its whole value, so its approved figures stay "0 at activation + amendments". Every change is kept as before/after history and audited (`PLAN_AMENDED`).
- **G-1 Central Pool — agreed direction, not frozen: purchase-based control (option 1-C).** The central store's PR/PPR against a `CENTRAL` Plan Item consumes **both quantity and amount**, like any PPR, which is what prevents buying more than planned or buying twice. Issues from the central store to departments happen in HOSxP; this system only **reads and shows** them (dashboard / report) and never posts them to the ledger, so nothing is counted twice. `CENTRAL_STOCK_ISSUE` / `CENTRAL_STOCK_RETURN` (D-01) stay defined but unused in Phase 1 (D-39 later made them impossible to post). This replaces the stock-issue deduction of §10.2. A PR matches a `CENTRAL` item only when it comes from that item's purchasing department; a PR line that matches both a `DEPARTMENT` and a `CENTRAL` item is refused as ambiguous (fail closed, as today).
- **G-3 Assigned Purchase — agreed direction, not frozen: one pooled item (option 3-A).** Only a requester of the item's purchasing department prepares the PPR; it consumes the whole `ASSIGNED` item (quantity and amount). A department that has demand on that item cannot confirm a PPR for the same HOSxP item and fund source in that fiscal year. Demand states: `PLANNED` → `INCLUDED_IN_CENTRAL_PURCHASE` when the purchaser's confirmed usage covers the item's total demand (back to `PLANNED` if that usage is released) → `FULFILLED` when every PPR that covers it is verified by Procurement (not tied to PR closure, G-4) ; `CANCELLED` when an amendment sets that department's demand to zero. Every demand-state change is audited with the PPR that caused it.
- Until G-1 / G-3 are frozen, Wave 7A does not change Central or Assigned behaviour: no Central or Assigned PR matching, no demand-state changes, no stock-issue reading.
- The two reports moved from Wave 8 are built in Wave 7: *Plan Amendment* in 7A, *Central Plan Usage* in 7B; *Central Plan Usage* shows the PPR usage and, when IT provides the stock-issue query, the issues read from HOSxP (display only).

### D-38 Wave 7B: Central Pool (resolves G-1) and Assigned Purchase (resolves G-3) (product-owner answers, 2026-10-01: 1A 2A 3A 4A 5B 6B 7A 8A 9A 10A)

Builds on the directions of D-37 (1-C purchase-based control, 3-A one pooled item).

**G-1 Central Pool**

- **C-1 (A) Purchasing department per item.** Each `CENTRAL` Plan Item names its purchasing department (the central store: supplies, pharmacy, …); it is required for `CENTRAL` items. A PR line matches a `CENTRAL` item only when the PR's department is that item's purchasing department, with the same HOSxP item and fund source.
- **C-2 (A) Ambiguity fails closed.** A PR line that matches both a `DEPARTMENT` item and a `CENTRAL` item is refused as ambiguous (as today, A-1); the plan is corrected (by Plan Amendment) before the PPR is made.
- **C-3 (A) Stock issues are shown, never posted.** Issues from the central store to departments are read from HOSxP and shown (dashboard, *Central Plan Usage* report) only after IT provides a verified, read-only query; no table or field is assumed until then, and they never enter the ledger (`CENTRAL_STOCK_ISSUE` / `CENTRAL_STOCK_RETURN` stay unused in Phase 1). Until the query exists the report shows PPR usage only. **Made precise by D-39.**
- **C-4 (A) Visibility.** A `CENTRAL` item is visible to its purchasing department and to organisation-wide roles; other departments do not see it (§22.1).

**G-3 Assigned Purchase**

- **A-1 (B) Blocking while demand is open.** A department with demand on an `ASSIGNED` item cannot confirm a PPR for the same HOSxP item and fund source in that fiscal year while its demand is `PLANNED` or `INCLUDED_IN_CENTRAL_PURCHASE`; once its demand is `FULFILLED` or `CANCELLED` the block ends.
- **A-2 (B) The purchaser states whom each PPR line buys for.** On a PPR line drawing on an `ASSIGNED` item, the purchasing department enters the quantity bought for each demand department; the quantities must add up exactly to the line's quantity, and none may exceed what that department's demand still needs. A demand becomes `INCLUDED_IN_CENTRAL_PURCHASE` when confirmed PPRs cover its whole quantity; partly covered demand stays `PLANNED` and shows the covered quantity.
- **A-3 (A) Fulfilled.** A demand becomes `FULFILLED` when it is fully covered and every PPR covering it has been verified by Procurement (`PROCUREMENT_VERIFIED`); not tied to PR closure (G-4).
- **A-4 (A) Cancellation.** When a PPR covering a demand is cancelled or its usage released, its coverage is released and the demand returns to `PLANNED` (also from `FULFILLED`).
- **A-5 (A) Reconfirmation.** While a covering PPR is unlocked for revision (D-22) the demand keeps its state; the coverage entered at reconfirmation replaces the old one and the state is recomputed then.
- **A-6 (A) Amending demand.** Demand is amended by Plan Amendment (D-37: Plan Officer, approval document): a department's demand may rise or fall but never below what PPRs still in effect cover for that demand — the quantity allocated to that demand by confirmed PPRs that are not cancelled, including those Procurement has not yet verified (product owner, 2026-10-01). To go lower, the PPR coverage concerned must first be changed (unlock and reconfirm) or the PPR cancelled; reduced to 0 it becomes `CANCELLED`; departments may be added. New `ASSIGNED` items, with their demand, may be added by amendment.
- Every demand-state change and every coverage is audited with the PPR or amendment that caused it.
- **One demand, one purchase line** (product owner, 2026-10-01): a department's demand for one HOSxP item and fund source may be claimed or covered by only one Assigned Purchase at a time — open demand of the same department on two `ASSIGNED` items with the same item and fund is refused (activation, Plan Amendment), and A-1 is never bypassed because a PPR draws on an `ASSIGNED` item of another purchaser (ADR-016).

### D-39 Stock issues and returns: HOSxP data for reporting only, never Plan Ledger transactions (product-owner decision, 2026-10-02; refines D-38 C-3, supersedes D-01)

- **Responsibilities.** The PPR System controls plan usage and issues PPRs against PRs in HOSxP. The store, stock issues, stock returns and stock balance belong to HOSxP, which stays their source of truth.
- **Report only.** Issues and returns of the central store that come from a **verified** HOSxP query may be shown in the *Central Plan Usage* report. They are read-only HOSxP data for reporting and analysis, not Plan Ledger transactions.
- **Never in the Plan Ledger.** They are never posted to the Plan Ledger and change neither plan quantity nor plan amount, directly or indirectly. `CENTRAL_STOCK_ISSUE` and `CENTRAL_STOCK_RETURN` cannot be posted, neither by the application nor by the database (a constraint refuses them). Remaining quantity and amount are those of §18.3 without any stock term.
- **No stock ledger here.** The system keeps no copy of stock movements (no stock ledger, no stored issues): the report reads them from HOSxP when it is run.
- **"Used" (in the report)** = issues from the central store to a department − returns from that department to the store, as recorded in HOSxP. How a return is recorded in HOSxP is confirmed by IT; until then no return kind is assumed.
- **Verified query only.** Figures appear only when the site's query file holds the stock-movement query together with its verification record (who verified it, when, reference) and the mapping of every HOSxP movement kind it uses to *issue* or *return*, written by IT. No HOSxP table, field, movement kind or native status is guessed. Without a verified query, when the query fails or HOSxP cannot be reached, the report shows "ยังไม่มีข้อมูล" and the rest of the system is unaffected. A movement of a kind that is not mapped makes the figures of its item and department (and the item's total) "ยังไม่มีข้อมูล"; the report says how the figures are incomplete.
- **Columns.** Per HOSxP item: planned, purchased (PPR), issued, returned, net used, purchased but not issued (purchased − net used), plan not yet purchased (remaining); with one row per receiving department.
- **Fund sources are not split.** HOSxP stock movements carry no fund source. When one HOSxP item is in several `CENTRAL` Plan Items (different fund sources), stock figures are shown once for the item, marked "ไม่สามารถแบ่งยอดตามแหล่งเงินได้จากข้อมูล HOSxP"; they are never divided (no guessing, no pro-rata) and never posted against any fund. Splitting needs a new decision if HOSxP ever provides deterministic fund data.
- **Unknown is never zero; ambiguity is never allocated** (product owner, 2026-10-02): "Unknown must never be coerced to zero, and ambiguous consumption must never be allocated by inference." A figure HOSxP does not determine is shown as "ยังไม่มีข้อมูล", never as 0. HOSxP issues of an item are put on its central plan line only when that line is the item's only plan line of the year (any plan type); when the item is also in other plan lines (other funds, other purchasers, `DEPARTMENT` or `ASSIGNED` items) the issues are shown once for the item, and "bought but not issued" is computed only when every plan line of the item is a `CENTRAL` line the reader sees, otherwise it is "ยังไม่มีข้อมูล".
- **Visibility.** The purchasing department (central store) and organisation-wide roles see totals and every receiving department (C-4). Any other department sees only its own issues, returns and net used for the items it received, with no other Central Plan details (no planned or purchased figures, fund source or purchaser), and never another department's figures.

### D-40 Go-live tools, MOCK/DEMO data in production, production sign-in (product owner, 2026-10-02; Wave 10B-2)

- **Go-live checklist.** The final approver is the product owner (เจ้าของระบบ). Every item carries evidence that can be traced afterwards (the saved output of a command, or a named document). The checklist ends with the items that are still OPEN - G-4, the PR cancellation status mapping (D-30) and HOSxP stock issues / returns (D-39) while not verified - and an OPEN item is never shown, counted or implied as PASS.
- **Check commands.** `ppr.cli hosxp-verify` (after IT connects HOSxP) and `ppr.cli first-start --check` (before the first production start) only read; each line is PASS / FAIL / WARN / OPEN / TODO / INFO, the overall result names any OPEN / WARN / TODO instead of saying PASS, and the output carries no names of people, passwords or connection secrets.
- **MOCK / DEMO data in production** is a WARNING, not a failure: shown at start-up, in `ops-status` / `status.ps1`, on every page of the user interface and in the go-live checklist. Production still refuses demonstration mode. MOCK data is never presented as real HOSxP data without that warning, and MOCK / DEMO data never affects the Plan Ledger: in production, no ledger entry is posted for a Plan Item whose HOSxP item is MOCK.
- **Production sign-in** goes through HOSxP authentication only (D-25 adapter); when HOSxP authentication fails or HOSxP cannot be reached the sign-in is refused - there is no fallback to demonstration authentication.

### D-41 Sign-in on a separate HOSxP server; the PPR's department is the PR's; requesters read their own PPRs (product owner, 2026-10-05; Wave 11A; amends D-09, §21, §22.1)

At the hospital the HOSxP user accounts are on one PostgreSQL server (the "login" server) and the stock / PR data on another; the two use different department codes, and neither holds a usable link from a user to a stock department (IT, 2026-10-05). Therefore:

- **Second read-only connection for sign-in.** `PPR_HOSXP_AUTH_DATABASE_URL`, when set, is used only by the site queries `get_login_user` and `get_user_profile` (D-25); every other query (PR, masters, stock movements) keeps using `PPR_HOSXP_DATABASE_URL`. Unset = one connection, as before. Both accounts are read-only and both are checked by `hosxp-check` and `hosxp-verify`. When the login server cannot be reached, sign-in answers `HOSXP_UNAVAILABLE` (503) - no fallback (D-40).
- **A user needs no department.** `get_login_user` / `get_user_profile` may return no department. Roles are still granted one by one by an `ADMIN`; signing in without a role gives no access.
- **The PPR's department is the PR's department in HOSxP** (`department_id` of the PR, stock server codes). A `REQUESTER` may prepare a PPR for a PR of any department; the PPR draws on that department's plan exactly as before (plan matching, CENTRAL / ASSIGNED purchasing-department rules and every hard control are unchanged). Who prepared it is recorded on the PPR and in the audit trail.
- **Only the requester who created a PPR** may edit its allocations, set coverage, discard it, confirm or reconfirm it (D-09: the attestation is now the creator's, not "a requester of the requesting department"). Nobody else confirms in their place. When that person is absent: a confirmed PPR is unlocked by the Plan Officer / Admin as before (§15.2) and is cancelled only from HOSxP (D-30); a **draft** - which holds no plan usage but keeps the PR from any other PPR (D-05) - may be discarded by a `PLAN_OFFICER` or `ADMIN` (the roles that unlock) with a **required reason**, recorded in the audit trail with who created it.
- **A user whose only role is `REQUESTER` reads only the PPRs they created** - one PPR, its versions and timeline, lists, search, dashboard counts, alerts, synchronization observations and PPR reports. Plan data stays scoped by department (§22.1): a requester without a department sees no plan data except the plan rows offered to the lines of the PR they entered (name, unit, HOSxP code if any, plan type and remaining balance; D-43). Every other role keeps organisation-wide reading.
- **Unchanged:** HOSxP is only read; the password hash is never stored, logged or shown; D-25 remains OPEN until a real HOSxP user signs in (the login query, hash format, encoding and active flag are recorded as evidence).

### D-42 The plan is imported from Excel without HOSxP codes; a requester binds a PR item to a plan row at the first PPR; Head of Procurement reviews the binding (product owner, 2026-10-07; Waves 12A-1 / 12A-2; amends D-16)

The hospital's annual plan is kept in Excel by department, with item names and units as the planning team writes them and **no HOSxP item codes**; entering some 4,000 rows one by one with a code each is not practical (planning team files, 2026-10-06). Therefore:

- **Import (Wave 12A-1).** A `PLAN_OFFICER` / `ADMIN` downloads the import template of a plan year (the synchronized HOSxP departments, fund sources and budget categories are listed in it), fills it and imports it into a **DRAFT** plan year. The file is checked completely first (preview) and imported **all or nothing**: any error imports nothing. Headings carry the HOSxP codes of the owner department, purchasing department, fund source and budget category and the plan type - the system never guesses a code from a name. Only `DEPARTMENT` and `CENTRAL` items are imported; `ASSIGNED` items (per-department demand) are entered on screen as before. A row with quantity and amount both 0 is skipped and listed. The **budget line** (fund source x budget category) must already exist with its approved amount, entered from the approval document - the import never creates one, so the envelope check at approval (§9) still compares the plan rows with the real approved budget. The file name, its SHA-256 and every row's source sheet and row are kept; the same file cannot be imported twice into a year; an import may be removed (with a reason) while the year is DRAFT. Quarters are kept as given (amount or quantity; monitoring only, §9).
- **Rows without a code.** An imported row has no HOSxP item until it is bound; it is a Plan Item in every other way (budget line, plan type, departments, quantity, price, amount, ledger at activation, amendment). A row the planning team already knows the code of may carry it in the file (**pre-binding by the Plan Officer**): the code must be an active synchronized HOSxP item whose unit equals the row's unit, and it is then an ordinary Plan Item.
> **Binding, review and permanent bindings superseded by D-43 (2026-10-07):** nothing is bound or remembered; the requester chooses the plan row of every line on every PPR, and the import template has no item column. The import itself (Wave 12A-1) is unchanged.

- **Binding (Wave 12A-2).** A PR line whose item is not yet planned for the PR's department and fund source asks the requester to **choose a plan row** of that department, fund source and fiscal year. Rows bound to that item in the previous fiscal year (same row name and department) are listed first; the system never chooses. A row whose unit differs from the HOSxP unit cannot be chosen - the Plan Officer converts the row to the HOSxP unit first (plan amendment). No suitable row means the item is not in the plan (`NOT_IN_PLAN`). The chosen binding is **pending** and is used at once (hard control and ledger as before); other PRs may use a pending binding. One row may be bound to several items; within a fiscal year one item is bound to at most one row per department and fund source.
- **Review.** Head of Procurement verification of a PPR shows its new bindings. Verifying makes them **permanent**: later PRs of that item, department and fund source match the row automatically (D-16 now: the item of the row **or** a binding that is not rejected). Rejecting a binding (with a reason) removes it, releases the plan usage of every PPR that used it and returns those PPRs to their requesters to choose again.
- **Changing a permanent binding** is a Plan Officer action through plan amendment with a reason, allowed only while no PPR in effect uses it. A requester never changes a permanent binding.
- **Unchanged:** the hard control, the all-or-none rule, the Plan Ledger, amendments and every audit; HOSxP is only read.

### D-43 The requester maps every PR line to a plan row on every PPR; the plan holds no HOSxP item codes (product owner, 2026-10-07; Wave 12A-2; supersedes D-16 matching and the binding parts of D-42)

The plan is kept by the planning team's own names and units, without HOSxP item codes, and the product owner prefers a requester's explicit choice on each PPR to any remembered or automatic match (2026-10-07). Therefore:

- **No automatic matching.** For every line of the PR, the requester chooses the plan row it uses, on the PR check page, before a draft is created; the choice is kept with the PPR and may be changed while the PPR is a draft or unlocked for revision. Nothing is remembered for the next PR (the D-42 "permanent binding", its review and the previous-year suggestions are dropped). A plan row carrying a HOSxP item is chosen the same way (no automatic match either).
- **Rows offered** for a line: the Plan Items of the current fiscal year's ACTIVE plan that answer the PR's department (owner of a DEPARTMENT item, purchaser of a CENTRAL / ASSIGNED item, D-38) and the PR's fund source, **counted in the unit of the PR line** (the PR line's unit, else the HOSxP item's unit; exact text, surrounding spaces ignored). A line with no such row is `NOT_IN_PLAN`; a line without a choice is `PLAN_ROW_NOT_CHOSEN`; a choice outside the rows offered is `PLAN_ROW_NOT_ALLOWED`. Several lines may choose the same row; the hard control is applied to their sum (unchanged).
- **Review.** The Head of Procurement verifies the PPR as before; the PPR page and print show each PR line with the plan row it uses. A wrong choice is corrected by the Plan Officer unlocking the PPR (§15.2) and the requester choosing again and reconfirming.
- **No HOSxP item in the plan.** The import template has no item column; a Plan Item entered on screen or added by amendment may have no HOSxP item and then carries its own name and unit (`ck_plan_item_code_or_import` is dropped). A row's name and unit may be changed only while it is unused (the Plan Officer converts a row to the HOSxP unit before it is used, D-42).
- **What a requester sees.** The PR check lists, per line, the rows offered with their name, unit, HOSxP code (if any), plan type and remaining balance - the plan data a requester needs to choose (D-41). The PPR page and its print show each line with the plan row it uses; an unlocked PPR keeps the rows of its confirmed version until it is reconfirmed.
- **Assigned Purchase items keep a HOSxP item** (D-38 A-1 recognizes one department's demand on the PR by the item). The rules that a HOSxP item is planned once per department and fund source apply only to rows that carry one.
- **Unchanged:** the hard control, the all-or-none rule, the Plan Ledger, D-38 A-1 / A-2 for Assigned Purchase items that carry a HOSxP item, verification, unlocking, reconfirmation and every audit; HOSxP is only read.

### Open design gates (must be resolved before the named wave)

| Gate | Topic | Blocks |
|---|---|---|
| G-1 (H-3) | **RESOLVED → D-38** (2026-10-01): purchase-based control, purchaser per item, ambiguity fails closed, stock issues shown only. | — |
| G-2 (H-4) | **RESOLVED → D-17** (2026-09-29): required amount = sum of PR item amounts; header total must match exactly or the PPR cannot be confirmed. | — |
| G-3 (H-8) | **RESOLVED → D-38** (2026-10-01): one pooled item; purchaser states coverage per department; blocking while demand open. | — |
| G-4 (D-07) | Terminal HOSxP status → `CLOSED`. | Wave 6/9 |

---

## 1. Purpose

This document is the authoritative technical and business specification for Phase 1 of the Hospital Plan & PPR Control System.

The system is designed to bridge the gap between the hospital's approved annual procurement/usage plan and the existing HOSxP procurement process, without modifying or replacing HOSxP.

The core control question is:

> For every PR, which approved plan does it use, under which fund source and budget category, how much quantity and amount remain, and what is the current PPR/PR status?

The system must reduce duplicate data entry, enforce plan controls, support auditability, and provide operational visibility for requesting departments, Planning, Procurement, Finance/CFO, Executives, and Administrators.

---

## 2. Phase 1 Scope

Phase 1 includes:

- Annual Plan management
- Plan Item management
- Department Plan
- Central Plan
- Assigned Purchase
- HOSxP master synchronization
- HOSxP PR retrieval
- PR → PPR workflow
- PR Item to Plan Item validation
- Hard control of both quantity and amount
- Multi-budget-category allocation under one fund source
- PPR numbering
- PPR confirmation and locking
- Head of Procurement verification
- PPR print document
- PPR/PR search and tracking
- PR change detection
- PR cancellation synchronization
- Plan usage ledger
- Plan Amendment
- Fiscal year locking
- Nightly synchronization
- Manual synchronization
- Sync logs
- Audit logs
- Role and permission controls
- Annual Head of Procurement assignment
- Alerts
- Dashboards by role
- Reports
- Excel export
- Central stock issue integration where applicable
- Acceptance tests and regression tests

---

## 3. Explicit Out of Scope for Phase 1

The following must not be implemented as Phase 1 requirements:

- PlanFin
- GL / Microsoft Access integration
- PlanFin Excel import
- PlanFin Actual / Variance / Forecast
- PO creation
- PO modification
- Replacement of HOSxP PO workflow
- Payment workflow
- Accounting replacement
- Inventory replacement
- Full electronic signature
- Executive electronic approval of PR
- Direct modification of HOSxP data unless explicitly supported by an approved HOSxP integration contract

PlanFin is deferred to **Phase 2**.

---

## 4. System Boundary and Sources of Truth

### 4.1 HOSxP remains Source of Truth for

- PR
- PR Items
- HOSxP user identity
- Department master
- Item master
- Fund source master
- Budget category master
- HOSxP budget status
- PR status
- Existing procurement process
- PO workflow
- Existing stock issue / inventory transactions
- Existing budget deduction/reservation performed by HOSxP

### 4.2 New System becomes Source of Truth for

- Approved Annual Plan
- Plan Items
- Plan ownership
- Plan usage control
- PPR
- PPR state and version
- PPR-to-Plan relationship
- Plan Ledger
- Plan Amendments
- Central Plan demand tracking
- Assigned Purchase tracking
- Annual Head of Procurement assignment
- Search/tracking metadata
- Alerts
- Audit trail
- Sync log

### 4.3 Excel policy

After plan approval and migration into the new system:

> **The new system is the master.**

Excel remains in use during the transition period primarily as:

- Export
- Report
- Working copy outside the system
- Distribution format

The intended Phase 1 direction is:

```text
System Database → Export Excel
```

not:

```text
Excel ↔ System Database
```

Changes to approved plans must be performed in the system through the governed amendment process.

---

## 5. Core Terminology

### PR — Purchase Requisition

The procurement requisition created in HOSxP.

### PPR — Plan Purchase Request

Thai working name:

> **คำขอใช้แผนประกอบ PR**

PPR is not a second PR. It is the plan-control record that verifies which approved plan the HOSxP PR consumes.

### Plan Item

An approved plan line linked to a HOSxP Item ID, with planned quantity and planned amount.

### Plan Ledger

An append-only transaction history used to derive plan usage and remaining quantity/amount.

---

## 6. Annual Plan Governance

### 6.1 Annual planning process

Current business process:

```text
Departments prepare plan
        ↓
Planning Group consolidates
        ↓
Director reviews and approves
        ↓
Approved plan becomes active in new system
        ↓
Excel may be exported for parallel/transition use
```

### 6.2 Mid-year plan changes

Plan changes during the fiscal year are allowed.

Required governance:

```text
Plan change requested
        ↓
Director approves
        ↓
Planning Group updates the system
        ↓
Audit Log + Plan Amendment history
```

Planning Group can edit approved plans only under governed amendment rules and all changes must be logged.

---

## 7. Fiscal Year Rules

Each plan belongs to one fiscal year.

Suggested plan-year states:

- `DRAFT`
- `APPROVED`
- `ACTIVE`
- `CLOSED`

Rules:

- New PPRs may only be created against an active fiscal year.
- After the fiscal year is closed, historical data remains searchable and trackable.
- Old fiscal-year PR/PPR records may continue to be followed to completion.
- New PPRs may not be created retrospectively against a closed fiscal year.
- A plan expires when its fiscal year ends; the PR's budget year must be the current fiscal year (D-18).

### PPR numbering

Format:

```text
PPR-<fiscal-year>-<sequence>
```

Example:

```text
PPR-2570-000001
```

Sequence resets at the beginning of each fiscal year.

Numbers must be unique; gapless numbering is not required (D-06). Fiscal-year boundaries are configuration (D-08).

---

## 8. HOSxP Master Data

The following masters must be synchronized from HOSxP:

- Department
- Item
- Fund Source
- Budget Category

The new system must not invent independent master identities for these data.

All synchronized master records must preserve HOSxP source identifiers.

### 8.1 Budget category hierarchy

Budget category structure must follow HOSxP.

The system must support hierarchical categories using a parent-child structure and must not hard-code a fixed number of hierarchy levels.

Example:

```text
Operating Expense
  └─ Materials
      └─ Office Supplies
```

Actual HOSxP hierarchy is authoritative.

---

## 9. Annual Budget and Plan Structure

### 9.1 Budget envelope

For each applicable budget category, the total approved Plan Item amount under that category must equal the approved category-level planned budget.

Validation before plan activation:

```text
SUM(Plan Item approved amount within category)
=
Approved category budget
```

A plan must not be activated if this equality does not hold.

### 9.2 Plan Item minimum attributes

Each Plan Item should include at least:

- Fiscal year
- Plan budget / budget category reference
- Fund source
- HOSxP Item ID
- Item code/name snapshot where useful
- Owner department
- Responsible purchasing department where applicable
- Plan type
- Planned quantity
- Estimated unit price
- Planned amount
- Q1 amount or quantity where applicable
- Q2
- Q3
- Q4
- Status
- Created/updated metadata

### 9.3 Estimated price rule

Planned unit price is an **estimated price**, not a hard constraint.

The hard controls are:

- Remaining quantity
- Remaining amount

Therefore a PR may have a unit price above or below the estimated plan price as long as both hard controls pass.

### 9.4 Q1–Q4 rule

Quarter data is for monitoring only.

Quarter values must **not** block PPR creation if the annual plan still has sufficient quantity and amount.

---

## 10. Plan Types

The system must support at least three plan types.

### 10.1 Department Plan

A Plan Item belongs to one department.

The same HOSxP Item may appear in plans of multiple departments.

Example:

```text
Toner A
ENT = 50,000
OR  = 30,000
IT  = 100,000
```

These are distinct Plan Items.

### 10.2 Central Pool

A hospital-level central item can be consumed by multiple departments.

Some Central Pool consumption occurs through HOSxP stock issue transactions rather than a new PR/PPR.

Example:

```text
Central Plan = 1,000 units

Ward A issues 100
Ward B issues 200

Remaining = 700
```

~~The system should synchronize applicable stock issues from HOSxP and reduce Central Plan quantity accordingly.~~ Replaced by D-38 / D-39: the central store's PPR consumes the Central Plan (quantity and amount); issues and returns stay in HOSxP and are only shown in the *Central Plan Usage* report, never posted (the example above is read as "used 300 of what the store bought", not as a plan deduction).

### 10.3 Assigned Purchase

Multiple departments may have individual demand, but one designated department is assigned to purchase on behalf of the group.

The system must preserve per-department demand.

Example:

```text
Ward A demand = 100
Ward B demand = 150
Ward C demand = 50

Assigned purchasing department purchases total = 300
```

Once demand has been fulfilled by the assigned central purchase, participating departments must be prevented from purchasing the same planned demand again.

Suggested demand states:

- `PLANNED`
- `INCLUDED_IN_CENTRAL_PURCHASE`
- `FULFILLED`
- `CANCELLED`

---

## 11. PR Rules

### 11.1 Fund source

A PR uses one fund source.

### 11.2 Budget category in HOSxP

A PR in HOSxP is created with one HOSxP budget category.

There is a business exception when funds in the selected category are insufficient:

- The PR still uses the same fund source.
- Additional funding may be documented as coming from other budget categories under the same fund source.
- The necessary HOSxP budget/category adjustment must be known and handled before PPR confirmation.
- The PPR must clearly document that multiple budget categories contributed to the PR.

### 11.3 One PR = one PPR

A HOSxP PR number may have at most one confirmed PPR for its entire lifetime, including a PPR later cancelled (D-03). A working draft does not bind the PR number (D-05).

Database-level uniqueness must enforce this rule unconditionally.

If procurement restarts after cancellation, a new HOSxP PR number is required, which then receives a new PPR.

### 11.4 PR Item source

PR Items must come only from HOSxP.

Users must not be able to manually add PR Items to a PPR.

---

## 12. Pre-PPR Validation

User flow:

```text
User opens "ขอใช้แผน (PPR)"
        ↓
Enters HOSxP PR No.
        ↓
System retrieves live PR Header + all PR Items
        ↓
System checks every PR Item against approved plan
```

Pre-PPR validation must verify at least:

- PR exists
- Fiscal year is valid
- Fiscal year is active
- PR has never been bound to a confirmed PPR, including one later `CANCELLED` (D-03)
- No other working draft exists for this PR (D-05)
- Fund source is valid
- Every PR Item maps to an approved Plan Item
- Requested quantity does not exceed remaining quantity
- Requested amount does not exceed remaining amount
- Multi-category allocation is valid where used
- All relevant categories belong to the same fund source

### Critical all-or-none rule

If even **one PR Item** is not in the approved plan:

> **No PPR record may be created.**

The system must not create a partial PPR.

The UI must explain which Item failed and why.

---

## 13. Hard Plan Control

A PPR request is valid only if both conditions are true:

```text
Requested Qty <= Remaining Qty
AND
Requested Amount <= Remaining Amount
```

Examples:

### Quantity exceeded

```text
Remaining Qty = 10
Remaining Amount = 30,000

Requested Qty = 11
Requested Amount = 20,000
```

Result:

> Blocked because quantity is exceeded.

### Amount exceeded

```text
Remaining Qty = 10
Remaining Amount = 30,000

Requested Qty = 5
Requested Amount = 40,000
```

Result:

> Blocked because amount is exceeded.

### Quantity exhausted while money remains

If planned quantity is fully consumed, no additional PPR may be created even if money remains.

### Amount exhausted while quantity remains

If planned amount is fully consumed, no additional PPR may be created even if quantity remains.

---

## 14. PPR Lifecycle

### 14.1 Pre-PPR

No PPR exists until every required validation passes.

### 14.2 Draft

After all Pre-PPR validations pass, the system may create a `DRAFT`.

Draft:

- May be reviewed
- May be edited within allowed fields
- Does not represent a final confirmed PPR
- Has not yet completed final plan-use confirmation

### 14.3 Confirmed and Locked

When the requester or authorized department staff presses:

> **ยืนยันขอใช้แผน**

the system must:

- Re-fetch/revalidate critical state
- Recalculate remaining quantity
- Recalculate remaining amount
- Create PPR number
- Save PR snapshot
- Create Plan Ledger entries
- Lock the PPR
- Write audit log
- Commit transaction atomically

Suggested state:

- `CONFIRMED_LOCKED`

### 14.4 Procurement verification

The appointed Head of Procurement can open PPR details and press:

> **ตรวจสอบและยืนยันแล้ว**

Suggested state:

- `PROCUREMENT_VERIFIED`

The action must record:

- User
- Date/time
- Applicable annual/effective appointment
- PPR version
- Audit event

Paper signature remains part of the existing process.

### 14.5 PR approval/status tracking

The system should read PR approval/status automatically from HOSxP.

Per D-02, HOSxP PR status is stored as a separate external field (normalized per §24.5) and is **not** an internal PPR state. HOSxP status changes are shown on the PPR timeline and may trigger internal transitions (e.g. HOSxP `CANCELLED` → PPR `CANCELLED`; HOSxP `COMPLETED` → eligible for PPR `CLOSED`), but the two values are never merged.

Exact status mapping is TBD until the real HOSxP status model is inspected.

### 14.6 PPR cancellation

If PR is cancelled in HOSxP:

```text
PR cancelled in HOSxP
        ↓
Sync detects cancellation
        ↓
PPR becomes CANCELLED
        ↓
Plan usage is released exactly once
```

The new system must not independently reverse HOSxP budget; HOSxP manages its own budget effect.

---

## 15. PPR Lock / Unlock / Revision

### 15.1 Lock rule

After PPR confirmation:

> PPR is locked.

Ordinary users cannot edit it.

### 15.2 Unlock authority

Only:

- Planning Group / Plan Officer
- Admin

may unlock a PPR.

Unlock must require and record:

- Reason
- User
- Timestamp
- Previous state
- New state
- Version reference

### 15.3 Revision

After unlock and change:

- Full validation must run again.
- Old version/history must remain available.
- Old data must never be silently overwritten without audit evidence.

---

## 16. PR Change Detection

When a PPR is confirmed, the system must store a snapshot of the HOSxP PR.

Nightly or manual sync compares:

```text
Confirmed PPR PR Snapshot
VS
Current HOSxP PR
```

Critical fields to compare include at least:

- PR Item IDs
- Item IDs
- Quantity
- Unit price
- Amount
- PR total
- Fund source
- Budget category
- PR status

If a critical change is detected:

> `PR_CHANGED_REVIEW_REQUIRED`

The UI must clearly show:

> 🔴 PR Changed — ต้องตรวจใหม่

The system must display a field-level diff where possible.

It must not silently overwrite the original PPR snapshot.

Planning Group reviews the change.

---

## 17. Multi-Budget-Category Allocation

Business requirement:

A PR has one fund source, but PPR may document funding from multiple budget categories under that same fund source in exceptional cases.

Example:

```text
PR amount = 100,000
Fund source = เงินบำรุง

Category A = 70,000
Category B = 30,000

Total = 100,000
```

Allocation level (D-04): allocation is recorded at **PPR header level**, not per PR Item. Users are not required to split PR Items across categories. Plan usage remains item-based (PR Item → Plan Item → Plan Ledger); category allocation is a separate funding/audit record and does not post to the Plan Ledger.

Rules:

- All allocation categories must belong to the PR fund source.
- Allocation sum must equal the PPR/PR total.
- Cross-fund-source allocation is prohibited.
- PPR output must clearly indicate when money was drawn from more than one category.
- Any required HOSxP budget adjustment must be resolved before PPR confirmation.

---

## 18. Plan Ledger

The new system must not create a second HOSxP budget ledger.

Instead it maintains a **Plan Usage Ledger** for approved Plan Items.

### 18.1 Source-of-truth separation

HOSxP Budget:

- Category-level budget
- HOSxP PR deduction/reservation
- HOSxP remaining budget

New Plan Ledger:

- Planned quantity
- Planned amount
- Used quantity
- Used amount
- Released quantity
- Released amount
- Remaining quantity
- Remaining amount

### 18.2 Suggested event types

- `PLAN_ACTIVATED` (initial entry per Plan Item, posted at `APPROVED → ACTIVE` — D-12)
- `PPR_CONFIRMED`
- `PPR_RELEASED`
- `PLAN_AMENDMENT`

`CENTRAL_STOCK_ISSUE` and `CENTRAL_STOCK_RETURN` (D-01) are **not** Plan Ledger events: D-39 forbids posting them (application and database).

### 18.3 Remaining must be derived

The system must not allow administrators to directly overwrite remaining values.

Remaining must be derived from valid plan transactions.

Conceptually:

```text
Remaining Qty
=
Approved Qty
+ Approved Amendment Qty
- Active PPR Qty
+ Released Qty
```

```text
Remaining Amount
=
Approved Amount
+ Approved Amendment Amount
- Active PPR Amount
+ Released Amount
```

Stock issues and returns appear in neither formula (D-39).

Exact implementation may use a ledger with projections/cached balances for performance, but the ledger is the auditable source.

---

## 19. Concurrency Requirement

The system must prevent oversubscription when multiple users confirm PPRs at the same time.

Example:

```text
Remaining Qty = 10

User A requests 10
User B requests 10
at the same time
```

Only one confirmation may succeed.

The other must fail after a final atomic revalidation.

Implementation must use appropriate transaction isolation, row locking, optimistic concurrency, or another proven atomic strategy.

Plan quantity and amount must never become negative because of race conditions.

---

## 20. Plan Amendment

Plan changes after approval must not be silent edits.

Required process:

```text
Director approves change
        ↓
Planning Group performs amendment
        ↓
System records before/after
        ↓
Plan Ledger reflects amendment
        ↓
Audit Log records action
```

Plan Amendment must support at least:

- Increase quantity
- Decrease quantity
- Increase amount
- Decrease amount
- Add new Plan Item
- Other approved transfers/adjustments where business rules permit

Rules:

- Quantity may not be reduced below already-used quantity.
- Amount may not be reduced below already-used amount.
- Historical versions must remain available.
- Excel export must reflect the latest approved plan state.

---

## 21. Authentication

Users should log in using their existing HOSxP identity.

The actual HOSxP authentication mechanism is **TBD**.

> Narrowed by D-25 (2026-09-29): the product owner states HOSxP stores an MD5 password hash; tables and columns come from the site query file. The current adapter behaviour is provisional; go-live requires real-HOSxP evidence for the query mapping, the stored hash format, the password encoding and the account-active semantics (D-25 go-live gate).

Do not invent:

- HOSxP auth API
- Session scheme
- Password hashing scheme
- Token type
- Direct database authentication

until verified from real system evidence.

Required abstraction:

```text
HosxpAuthenticationAdapter
```

Example conceptual methods:

```text
authenticate(username, password)
get_user_profile()
```

The new application must never store HOSxP passwords in plaintext.

---

## 22. Authorization / Roles

Identity comes from HOSxP, but authorization is controlled by the new system.

Required roles:

- `REQUESTER`
- `PLAN_OFFICER`
- `PROCUREMENT`
- `HEAD_OF_PROCUREMENT`
- `FINANCE`
- `CFO`
- `EXECUTIVE`
- `ADMIN`

### 22.1 Requester / department staff

Can:

- See own work
- See plan/PPR information for their department
- Enter PR No.
- Retrieve PR
- Review PR Items against plan
- Create/confirm PPR where authorized
- Search and track department PPRs

*D-41 (2026-10-05):* a requester enters a PR of any department; the PPR takes the PR's department. A user whose only role is `REQUESTER` reads only the PPRs they created, and only the creator edits, confirms or reconfirms a PPR. Plan data stays scoped by the user's department (none when HOSxP gives none).

### 22.2 Plan Officer

Can:

- See all departments
- Manage plan
- Perform approved amendments
- Unlock PPR
- Review PR Changed cases
- Review audit and sync status

All plan edits must be audited.

### 22.3 Procurement

Can:

- Search all relevant PPRs
- View details
- Track cases
- Review data

Cannot perform Head-of-Procurement verification unless appointed.

### 22.4 Head of Procurement

Can perform all Procurement viewing functions plus:

> **ตรวจสอบและยืนยันแล้ว**

subject to current annual/effective appointment.

### 22.5 Finance / Accounting

Read-only for Plan/PPR with applicable organization-wide visibility.

### 22.6 CFO

Read-only plus dashboards and drill-down.

### 22.7 Executive / Director

Read-only dashboard and PPR detail/tracking.

No Phase 1 electronic PR approval.

### 22.8 Admin

Can manage:

- Role mapping
- Annual appointment
- Sync configuration
- Master mapping where allowed
- Unlock
- System configuration

Admin must not bypass the Plan Amendment process to directly change approved plan amounts.

### 22.9 Server-side enforcement

Authorization must be enforced at the API/server level.

Hiding a button in the UI is not sufficient.

Unauthorized direct API requests must be rejected.

---

## 23. Annual Head of Procurement Appointment

Head of Procurement changes annually and may change during the year.

The system must allow configuration by:

- User
- Fiscal year
- Effective from
- Effective to
- Status
- Created by/at

No upload of appointment order document is required in Phase 1.

> Decided by D-27 / D-28 (2026-09-29): no overlapping appointments, dates inside the appointment's fiscal year, authority judged on the server date of verification.

Historical PPR must preserve which appointed Head of Procurement verified the PPR at the time.

---

## 24. HOSxP Integration Contract

The system must use an adapter layer.

Architecture:

```text
HOSxP
   ↓
HOSxP Adapter
   ↓
Normalized Domain Contract
   ↓
Plan / PPR Core
```

Do not hard-code unverified HOSxP details into core business logic.

### 24.1 Master contracts

Normalized data should support:

#### Department

- source_id
- code
- name
- active

#### Item

- source_id
- code
- name
- unit
- active

#### Fund Source

- source_id
- code
- name
- active

#### Budget Category

- source_id
- code
- name
- parent_source_id
- active

### 24.2 PR Header contract

Required if available from HOSxP:

- pr_id
- pr_no
- pr_date
- fiscal_year
- department_id
- requester_id/name
- fund_source_id
- budget_category_id
- total_amount
- status
- last_modified_at

### 24.3 PR Item contract

- pr_item_id
- item_id
- item_code/name
- qty
- unit
- unit_price
- amount

### 24.4 Budget snapshot contract

Where HOSxP provides the data:

- fund_source
- budget_category
- approved budget
- used/reserved
- remaining
- as-of timestamp

UI must distinguish:

> **HOSxP Budget Remaining**

from:

> **Plan Item Remaining**

They are different values.

### 24.5 PR status normalization

Do not make core code depend directly on HOSxP native status codes.

Adapter should normalize to an application-level **HOSxP PR status** (a field separate from the internal PPR state — D-02) such as:

- `DRAFT`
- `ACTIVE`
- `APPROVED`
- `CANCELLED`
- `COMPLETED`
- `UNKNOWN`

Actual mapping is TBD.

---

## 25. HOSxP Synchronization

The system must support:

### 25.1 On-demand retrieval

When user enters PR No.:

> Query HOSxP live.

New PPR must not be created using stale cached PR data if HOSxP is unavailable.

### 25.2 Manual Sync

Authorized users can trigger synchronization manually.

Examples:

- PR detail
- Plan/Admin integration screen

### 25.3 Nightly Sync

Automatic daily nighttime synchronization.

At minimum, it should be able to inspect:

- PR status
- PR changes
- PR cancellation
- Master changes
- Budget data where available
- Central stock issues where applicable

Exact schedule is configurable; no business requirement fixes a specific hour.

### 25.4 Sync logging

Every run must record at least:

- sync_run_id
- mode: `MANUAL` / `NIGHTLY`
- requested_by if manual
- started_at
- finished_at
- scope
- records_checked
- records_created
- records_updated
- records_changed
- records_cancelled
- status
- error details

### 25.5 Idempotency

Repeated synchronization must not create duplicates or double-release plan usage.

Examples:

- Sync same PR twice → one logical PR/PPR relationship
- Detect cancellation repeatedly → release plan once only

---

## 26. HOSxP Downtime Behavior

If HOSxP is unavailable:

### New PPR

> Must not be created from stale cached PR data.

### Existing system functions

Should remain available where safe:

- View existing PPR
- View plan
- View dashboard based on last synchronized data
- Search historical records
- View audit logs

UI should show last successful synchronization time when relevant.

---

## 27. Central Stock Integration

Stock issues and returns of the central store live in HOSxP, which stays their source of truth (D-39). This system only reads them, when a report is run, through a verified read-only query; it never stores them and never posts them to the Plan Ledger.

Normalized contract (one stock movement, as returned by the site's query):

- movement_id
- movement_date
- department_id (the receiving or returning department)
- item_id
- qty (positive)
- native_kind (the HOSxP movement kind, as received)
- last_modified_at (if available)

The adapter turns `native_kind` into *issue* or *return* only through the mapping IT writes in the site query file; an unmapped kind is not counted. `item_id` and `department_id` are the same identifiers as the item and department masters, and `qty` is in the item's unit as in the item master (the unit of PR lines), so that issues can be set against what PPRs bought; IT confirms both when verifying the query. Actual HOSxP API/table/field mapping remains TBD (IT evidence).

---

## 28. PPR State Model

Internal PPR states (D-02):

- `DRAFT`
- `CONFIRMED_LOCKED`
- `PROCUREMENT_VERIFIED`
- `PR_CHANGED_REVIEW_REQUIRED`
- `UNLOCKED_FOR_REVISION`
- `CANCELLED`
- `CLOSED`

HOSxP PR status (`DRAFT` / `ACTIVE` / `APPROVED` / `CANCELLED` / `COMPLETED` / `UNKNOWN`) is a separate synchronized field and is not part of this state machine.

Exact transitions must be implemented as an explicit state machine, not arbitrary status edits.

### Key transition examples

```text
Pre-validation passes
        ↓
DRAFT
        ↓ Confirm
CONFIRMED_LOCKED
        ↓ Head Procurement verifies
PROCUREMENT_VERIFIED
        ↓ (HOSxP PR status field updates independently via sync: ACTIVE → APPROVED → COMPLETED)
        ↓ (no transition defined yet — integration-TBD, D-07 / G-4; no manual close action)
CLOSED
```

Exception:

```text
Confirmed PPR
        ↓ PR changed in HOSxP
PR_CHANGED_REVIEW_REQUIRED
        ↓ Plan/Admin unlocks
UNLOCKED_FOR_REVISION
        ↓ Revalidate / reconfirm
CONFIRMED_LOCKED
```

Cancellation:

```text
PR cancelled in HOSxP
        ↓ Sync
CANCELLED
        ↓
Release Plan usage exactly once
```

---

## 29. Core Data Model

Exact physical schema may vary, but these domain entities are required.

### 29.1 HOSxP mirrors

- `hosxp_department`
- `hosxp_item`
- `hosxp_fund_source`
- `hosxp_budget_category`

### 29.2 Planning

- `plan_year`
- `plan_budget`
- `plan_item`
- `plan_item_demand`

### 29.3 PPR

- `ppr` (includes internal `state` and separate `hosxp_pr_status` — D-02)
- `ppr_item`
- `ppr_category_allocation` (header-level — D-04)
- PR snapshot/version data

### 29.4 Ledger

- `plan_ledger`

### 29.5 Governance

- `plan_amendment`
- `plan_amendment_detail`
- `role_appointment`

### 29.6 Identity/authorization

- local user reference linked to HOSxP identity
- `role`
- `user_role`

### 29.7 Integration

- `sync_run`
- `sync_change`

### 29.8 Audit/alerts

- `audit_log`
- `alert_rule`
- `alert`

---

## 30. Data Model Constraints

At minimum:

- One HOSxP PR number can be bound to at most one confirmed PPR ever, including cancelled (D-03); at most one working draft per PR at a time (D-05).
- Category allocation belongs to PPR header; Σ allocation = PPR/PR total (D-04).
- Central stock ledger events carry amount delta = 0 (D-01).
- PPR items must reference HOSxP PR Items.
- Plan Items must reference HOSxP Item IDs.
- Same HOSxP Item may appear in different department plans.
- PPR sequence must be unique within fiscal year.
- Ledger events must be immutable through normal application UI.
- Cross-fund allocation must be rejected.
- Confirming PPR must be atomic.
- Cancellation release must be idempotent.
- Closed fiscal-year plan must reject new PPR creation.

---

## 31. UI / UX Requirements

### 31.1 Navigation

Role-sensitive navigation may include:

- Dashboard
- Annual Plan
- ขอใช้แผน (PPR)
- PR/PPR Tracking
- Procurement
- Plan Amendment
- Reports
- Sync / Audit
- System Settings

Only relevant menus should be shown per role.

### 31.2 Requester Dashboard

Should show at least:

- My plan
- Department plan
- Planned amount
- Used amount
- Remaining amount
- Planned quantity
- Used quantity
- Remaining quantity
- Active PPR
- Pending/stale PPR
- Alerts

### 31.3 Create PPR

Primary flow:

```text
Enter PR No.
        ↓
Retrieve from HOSxP
        ↓
Review PR Header + Items
        ↓
Validate against Plan
        ↓
Confirm
        ↓
Print
```

Do not make users re-enter data already available from HOSxP.

### 31.4 Validation result UI

For every PR Item, show clearly:

- PR Item
- Requested Qty
- Requested Amount
- Matched Plan Item
- Remaining Qty
- Remaining Amount
- Validation result
- Reason if failed

If any Item fails, no PPR is created.

### 31.5 Multi-category allocation UI

Must clearly show:

- Fund source
- Categories contributing
- Amount from each category
- Total allocation
- Warning/label that multiple categories are used
- Allocation is entered once for the whole PPR, not per PR Item (D-04)

### 31.6 PPR Detail

Should be usable by Requester, Planning, Procurement, CFO, Executive based on permission.

Show:

- PPR No.
- PR No.
- Fiscal year
- Department
- Requester
- Fund source
- HOSxP category
- PPR category allocation
- PR Items
- Plan Items
- Qty/Amount
- Remaining
- Status
- Version
- Timeline
- Audit-relevant events

### 31.7 Head of Procurement Queue

Must provide:

- Waiting for review
- Verified
- PR Changed
- Cancelled
- Inactive > threshold
- Search/filter

Only appointed Head of Procurement sees the verification action.

### 31.8 Plan Control Center

Planning Group should see:

- Plan utilization
- Remaining by department/item/category
- PR Changed queue
- Amendments
- Unlock/review cases
- Central Plan
- Assigned Purchase
- Sync errors
- Alerts

### 31.9 Executive Dashboard

Read-only, management-focused:

- Total plan
- Used
- Remaining
- PPR volume
- Plan items/categories near exhaustion
- Pending/stale PPR
- Amendments
- Exceptions
- Drill-down to PPR detail

---

## 32. Search

Global/role-scoped search must support at least:

- PR No.
- PPR No.
- Requester name
- Department
- Item ID/name
- Budget category
- Fund source
- Date
- Status

Users must not need to remember a PR number to find a case.

Authorization must limit search results according to role.

---

## 33. Tracking Timeline

Every PPR should have a chronological timeline where possible.

Example:

```text
PR created in HOSxP
PPR validation
PPR confirmed
PPR locked
PPR printed
Head of Procurement verified
PR approved in HOSxP
PR changed
PR cancelled
PPR cancelled
Plan usage released
```

The user should be able to answer:

> Where is this case now, what happened, who acted, and how long has it been inactive?

---

## 34. Alerts

Default stale/inactive threshold:

> **30 days**

Threshold must be configurable.

Potential alert types:

- PPR inactive
- PR Changed
- Sync failure
- Plan quantity low
- Plan amount low
- Plan exhausted

Low-plan visual thresholds may be configurable; they are alerts only and must not replace hard controls.

Cancelled/Closed cases should not remain as stale alerts.

---

## 35. PPR Document

Printed PPR should include at least:

- PPR No.
- PR No.
- Fiscal year
- Department
- Requester
- Fund source
- PR Items
- Plan Items
- Budget category/allocation
- Planned/used/remaining quantity
- Planned/used/remaining amount
- Validation result
- Confirmation date
- Version
- QR code or verification reference if implemented

Paper signature remains in Phase 1.

The system should support version evidence so a materially changed PPR cannot silently retain an old printed meaning.

---

## 36. Audit Trail

Every critical action must answer:

- Who
- Did what
- To which entity
- When
- Before value/state
- After value/state
- Reason where required

Audit at minimum:

- Plan create/update
- Plan Amendment
- PPR create
- PPR confirm
- PPR unlock
- PPR reconfirm
- Head Procurement verify
- PR Change review
- Cancellation
- Annual appointment change
- Role change
- Manual sync
- Configuration change

Audit log must not be editable through normal application UI.

---

## 37. Reporting / Export

Phase 1 reports should include at least:

- Plan Summary
- Plan Remaining
- Department Utilization
- Budget Category utilization
- PPR Register
- Pending PPR
- Cancelled PPR
- PR Changed
- Plan Amendment
- Central Plan Usage
- Audit Report

Filters should support where applicable:

- Fiscal year
- Department
- Fund source
- Budget category
- Item
- Date range
- Status

Excel export is required.

---

## 38. Reliability / Transaction Rules

### 38.1 Atomic PPR confirmation

PPR confirmation must be one atomic database transaction.

Conceptually:

```text
BEGIN

re-fetch/revalidate critical state
recalculate remaining
validate all PR items
validate allocations
generate PPR number
write PPR + items + ledger
save PR snapshot
lock PPR
write audit

COMMIT
```

Any failure:

```text
ROLLBACK
```

No partial PPR must remain.

### 38.2 Duplicate protection

System must prevent:

- Duplicate PPR for same PR
- Duplicate sync-created entities
- Duplicate cancellation release
- Duplicate stock-issue ledger events

### 38.3 Backup / restore

Before production, backup and restore must be tested end-to-end.

Having only a backup script is not sufficient.

---

## 39. Security Requirements

- HOSxP password must not be stored in plaintext.
- Server-side authorization is mandatory.
- Least privilege should be applied.
- Audit critical actions.
- Sensitive integration credentials must be stored securely.
- UI hiding is not a security control.
- Read-only roles must not mutate protected resources by direct API access.
- Admin must not bypass Plan Amendment governance for approved plan amounts.

---

## 40. Acceptance Test Baseline

### 40.1 PR / PPR Core

- PR exists → retrieve Header + all Items.
- PR not found → no PPR.
- PR already has PPR → reject duplicate.
- PR whose PPR was cancelled → reject new PPR for the same PR number (D-03).
- Draft discarded before confirmation → a new draft may be started for the same PR (D-05).
- Second working draft for the same PR → reject (D-05).
- All Items in Plan → eligible.
- One Item missing from Plan → no PPR record.
- User attempts to add PR Item manually → reject.
- Closed fiscal year → no new PPR.

### 40.2 Quantity / Amount

- Qty and Amount both within remaining → pass.
- Qty exceeds → block.
- Amount exceeds → block.
- Qty exactly remaining → pass if amount also passes.
- Amount exactly remaining → pass if qty also passes.
- Estimated unit price differs but totals within limits → pass.
- Qty exhausted while money remains → block further PPR.
- Amount exhausted while qty remains → block further PPR.

### 40.3 Fund / Category

- Single fund source → pass.
- Multiple categories under same fund → supported.
- Cross-fund allocation → reject.
- Allocation sum does not equal required amount → reject.
- Multi-category allocation must be visible in document/audit.
- Allocation is header-level; no per-item category split is required (D-04).
- Same category appearing twice in one PPR → reject (D-04).
- Category allocation does not change Plan Ledger balances (D-04).

### 40.4 Lock / Version

- Draft can be edited.
- Confirm creates PPR number and locks.
- Ordinary user cannot edit locked PPR.
- Plan/Admin can unlock with audit.
- Procurement cannot unlock.
- Reconfirm reruns all validations.
- Old version/history preserved.

### 40.5 Ledger

- Confirm reduces Plan remaining correctly.
- Cancel releases Plan qty and amount.
- Admin cannot directly overwrite remaining.
- Remaining matches ledger.
- Concurrent confirmation cannot oversubscribe Plan.

### 40.6 Sync

- Nightly sync logs run.
- Manual sync logs run.
- HOSxP unavailable → existing data preserved; new PPR not created from stale PR.
- Repeated sync creates no duplicates.
- Partial failure is visible and logged.

### 40.7 PR Changed

Changes to critical fields trigger review:

- Qty
- Amount
- Item
- Fund source
- Budget category
- PR total/status where relevant

No critical change → no false positive.

### 40.8 Cancellation

- PR cancelled in HOSxP → PPR cancelled after sync.
- Plan usage released correctly.
- Repeated cancellation sync does not release twice.

### 40.9 Head of Procurement

- Current appointed head can verify.
- Procurement staff cannot verify unless appointed.
- Previous-year head cannot verify new-year cases unless newly appointed.
- Mid-year appointment change respects effective date.
- Verification is audited.

### 40.10 Role / Permission

- Requester sees own + department scope.
- Planning sees all and can governed-edit Plan.
- Procurement sees queue/detail.
- Finance/CFO/Executive are read-only.
- Admin config privileges do not bypass Plan Amendment.
- Direct unauthorized API call returns authorization failure.

### 40.11 Plan Amendment

- Increase qty/amount → ledger updated.
- Reduce below used qty → reject.
- Reduce below used amount → reject.
- Add new item → preserve amendment history.
- No silent plan edit after approval.
- Export reflects current approved state.

### 40.12 Central Plan

- Issues and returns read through a verified HOSxP query are shown correctly in the Central Plan Usage report (issued, returned, net used, per department) (D-39).
- Stock issues and returns never change Plan Ledger quantity or amount; the Plan Ledger refuses them at the application and database layers (D-39).
- Department demand is preserved for Assigned Purchase.
- Fulfilled central demand cannot be purchased again without approved change.
- A stock-movement query that is not configured, not verified or fails validation shows "ยังไม่มีข้อมูล" and does not affect the Plan Ledger (D-39).
- Repeating the report, the synchronization or the stock read changes no plan or PPR data (D-39).
- HOSxP stays the source of truth for stock movements: nothing is stored here and the report always reflects HOSxP as read (D-39).

### 40.13 Fiscal year

- Active year → PPR creation allowed.
- Closed year → PPR creation rejected.
- Old PPR remains searchable.
- New year PPR sequence resets.

### 40.14 Go-live readiness

- `hosxp-verify` checks the connected HOSxP read-only and reports PASS / FAIL / OPEN; an OPEN item is never reported as PASS (D-40).
- `first-start --check` reports the readiness of a production server without changing anything (D-40).
- MOCK / DEMO data in a production database is a warning at start-up, in `ops-status` and on every page, never a failure, and never reaches the Plan Ledger (D-40).
- Production signs in through HOSxP only; there is no fallback to demonstration authentication (D-40).

### 40.15 Separate sign-in server, PPR department from the PR

- With `PPR_HOSXP_AUTH_DATABASE_URL` set, sign-in reads only the login server and every other HOSxP query only the stock server; when the login server is down sign-in answers 503 with no fallback (D-41).
- A requester without a department prepares and confirms a PPR for a PR of any department; the PPR takes the PR's department and draws on that department's plan (D-41).
- Only the requester who created a PPR edits, discards, confirms or reconfirms it; another requester is refused; a Plan Officer / Admin may discard someone else's draft only with a reason (D-41).
- A user whose only role is `REQUESTER` reads only the PPRs they created, on every read route and report (D-41).
- `hosxp-check` and `hosxp-verify` test both connections and verify the login account is read-only too (D-41).

### 40.16 Hospital-size master sync, passwords never shown

- A manual master sync of a hospital-size catalogue (tens of thousands of items) succeeds in one transaction; a repeated sync changes nothing; a failure keeps the previous master data (§25.2, §25.4; Wave 11B).
- Every tool that prints a database URL hides its password, whatever characters the password holds (`#`, `@`, `/`, `?`, `:`) (§36; Wave 11B).

### 40.17 Plan import from Excel (Wave 12A-1)

- The import template of a plan year lists the synchronized active HOSxP departments, fund sources and budget categories and the import headings; a file filled in it is previewed with every error, warning and skipped row before anything is written (D-42).
- An import is all or nothing: any error (unknown or inactive code, `ASSIGNED` or unknown plan type, missing budget line, CENTRAL heading without purchasing department, bad number, too many decimals, quantity 0 with an amount) imports nothing (D-42); the template has no item column and an item column left from an older template is not read (warning, D-43).
- A valid import creates, in a DRAFT plan year only, one Plan Item per row (rows with quantity and amount 0 skipped), keeps the file name, its SHA-256 and each row's source sheet and row, refuses the same file twice, and writes one audit entry (D-42).
- Imported rows carry no HOSxP code and are matched to no PR by themselves (a requester chooses them, D-43), are kept out of the duplicate checks, count in the budget envelope and are activated into the Plan Ledger like any Plan Item (D-42).
- An import is removed with a reason only while the plan year is DRAFT; its Plan Items go, the import record and the audit trail stay (D-42).

### 40.18 Plan row chosen by the requester (Wave 12A-2)

- For every PR line the check page offers exactly the current ACTIVE plan's rows that answer the PR's department and fund source in the line's unit; a line with none is `NOT_IN_PLAN`, without a choice `PLAN_ROW_NOT_CHOSEN`, with a choice outside them `PLAN_ROW_NOT_ALLOWED`; nothing is matched automatically (D-43).
- A draft is created only with a valid choice for every line; the hard control applies per chosen row to the sum of the lines choosing it; confirmation posts usage to the chosen rows (D-43).
- Choices are changed only by the PPR's creator while it is a draft or unlocked for revision, each change audited; reconfirmation uses the new choices; nothing is remembered for another PR (D-43).
- The PPR page and print show each PR line with its chosen plan row (D-43).
- A Plan Item may be entered on screen or added by amendment without a HOSxP item, with its own name and unit; name and unit change only while the row is unused; the import template has no item column (D-43).

---

## 41. Implementation Architecture

Recommended high-level structure:

```text
                    HOSxP
        ┌─────────────┼─────────────┐
        │             │             │
      Master          PR         Stock Issue
        │             │             │
        └──────── HOSxP Adapter ────┘
                      │
                      ▼
               Integration Layer
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
    Plan Core       PPR Core    Central Plan
        │             │             │
        └─────── Plan Ledger ───────┘
                      │
               Search / Tracking
                      │
          Dashboard / Report / Alert
                      │
                 Audit / Log
```

Core business logic must depend on normalized interfaces, not raw HOSxP schema.

---

## 42. Suggested HOSxP Gateway Interface

Initial implementation should use a mock adapter until real HOSxP contracts are verified.

Conceptual interface:

```text
HosxpGateway
- authenticate(...)
- get_user_profile(...)
- get_pr(pr_no)
- get_pr_items(pr_no)
- get_department(...)
- get_item(...)
- get_fund_source(...)
- get_budget_category(...)
- get_budget_snapshot(...)
- get_stock_movements(date_from, date_to)   # D-39: read-only, report only
```

Do not infer real endpoint names from this interface.

Recommended implementations:

```text
MockHosxpGateway
RealHosxpGateway   # later, after verified mapping
```

---

## 43. Implementation Work Packages

### M01 Foundation

- Database/migrations
- Config
- Fiscal year
- Core domain structure

### M02 Identity & Role

- HOSxP authentication abstraction
- User profile mapping
- RBAC
- Annual appointment

### M03 HOSxP Adapter

- Master sync
- PR retrieval
- Budget snapshot
- Stock issue abstraction

### M04 Plan Core

- Annual plan
- Plan budget
- Plan Item
- Department/Central/Assigned plan types
- Excel export

### M05 PPR Core

- Pre-validation
- Draft
- Confirm
- Lock
- Multi-category allocation
- PPR document

### M06 Plan Ledger

- Plan usage ledger
- Remaining calculation
- Concurrency control
- Release logic

### M07 Governance

- Plan Amendment
- Unlock/revision
- Head Procurement verification

### M08 Sync & Audit

- Manual sync
- Nightly sync
- Change detector
- Cancellation
- Sync log
- Audit log

### M09 UI / Dashboard

- Requester dashboard
- Create PPR
- PPR detail
- Procurement queue
- Planning control center
- Executive dashboard
- Search/tracking

### M10 Reporting & Verification

- Alerts
- Reports
- Excel export
- Acceptance tests
- Regression tests
- Backup/restore verification

---

## 44. Recommended Implementation Waves

### Wave 1 — Foundation

- Project/domain foundation
- Database schema/migrations
- Fiscal year
- RBAC foundation
- Audit foundation
- HOSxP interfaces
- Mock HOSxP adapter

### Wave 2 — Plan Core

- Annual Plan
- Budget envelope
- Plan Item
- Plan types
- Plan Ledger foundation

### Wave 3 — PR Retrieval / Pre-PPR Validation

- Live PR retrieval abstraction
- PR Item normalization
- Item-to-plan matching
- Qty/Amount checks
- No-record-on-failure behavior

### Wave 4 — PPR Confirm / Lock / Print

- Draft
- Atomic confirmation
- PPR numbering
- PR snapshot
- Ledger posting
- Locking/version
- PPR printable document

### Wave 5 — Procurement / Search / Tracking

- Head Procurement queue
- Verification
- Global search
- Timeline

### Wave 6 — Sync / PR Change / Cancellation

- Manual sync
- Nightly sync
- Diff detection
- Review-required state
- Automatic PPR cancellation
- Idempotent Plan release

### Wave 7 — Amendment / Central Plan

Split by D-37:

- **7A:** Plan Amendment and the *Plan Amendment* report
- **7B (after G-1 / G-3 are frozen):** Central Pool, Assigned Purchase, stock issue integration abstraction, the *Central Plan Usage* report

### Wave 8 — Dashboard / Reports / Alerts

Split by D-32 (built before Wave 7):

- **8A:** role dashboards, in-app derived alerts and their settings (D-33), plan management screens (D-34)
- **8B:** reports and Excel export (D-35; Plan Amendment and Central Plan Usage reports move to Wave 7)

### Wave 9 — Real HOSxP Adapter / Hardening

Only after verified HOSxP evidence is available:

- Real authentication mapping
- Real master APIs
- Real PR API
- Real status mapping
- Real budget snapshot
- Real stock issue mapping

### Wave 10 — Full Verification

- Full acceptance suite
- Regression suite
- Concurrency tests
- Authorization tests
- Sync idempotency tests
- Backup/restore drill
- Go-live readiness review

---

## 45. Definition of Done

A work item is not complete merely because code exists.

Minimum Definition of Done:

```text
Implementation
+
Unit tests
+
Integration tests where applicable
+
Acceptance tests
+
Authorization tests where applicable
+
Audit/log behavior where applicable
+
Relevant regression tests
+
Documentation
+
Evidence of test results
```

Mandatory tests must pass before a wave is marked complete.

---

## 46. Design / Validation Principles

1. Do not invent HOSxP APIs, table names, status codes, or authentication details.
2. Prefer stable source IDs over name matching.
3. Preserve historical evidence; do not silently overwrite confirmed records.
4. Hard controls are quantity and amount.
5. Q1–Q4 are monitoring only.
6. HOSxP and Plan remaining are different concepts and must be displayed separately.
7. New system must not duplicate HOSxP budget deduction logic.
8. Use transaction-safe confirmation.
9. Make synchronization idempotent.
10. Make authorization server-side.
11. Make critical actions auditable.
12. Use mock adapters until real integration evidence exists.
13. PlanFin must not leak back into Phase 1.

---

## 47. Known TBDs / Required Real-System Evidence

The following remain intentionally unresolved until real evidence is supplied:

### HOSxP

- Authentication mechanism
- API endpoints
- Table names if database access is used
- Exact field names
- Native status codes
- PR approval state mapping
- Budget snapshot fields
- Stock issue fields
- Last-modified/change indicators
- API reliability/rate limits
- Source of the PR budget-year field in HOSxP data (visible on the PR screen; D-08, D-18)
- Budget category → fund source relationship (needed for D-04 same-fund check)
- Terminal status that closes a PPR (G-4)

### Deployment

- Production database engine (Phase 1 target: PostgreSQL; native install must remain possible, Docker optional)
- Hosting environment
- Reverse proxy
- TLS/certificate management
- Backup destination
- Monitoring stack

### UI details

- Final visual design
- Branding
- Exact responsive breakpoints
- Final PPR print layout

These TBDs must not be filled with guesses.

---

## 48. Phase 1 Design Freeze Summary

Business design is considered sufficiently defined for implementation of core application using mock/adapter-based integrations.

Frozen/approved areas:

- System boundary
- Annual plan governance
- Plan Item
- Plan types
- Fiscal year rules
- PPR 1:1 with PR
- PR Items from HOSxP only
- No PPR if any Item is outside Plan
- Qty + Amount hard control
- Estimated price behavior
- Q1–Q4 monitoring-only behavior
- One fund source per PR
- Multi-category PPR allocation under same fund source
- PPR confirmation and lock
- Plan/Admin unlock
- Annual Head of Procurement assignment
- Procurement verification
- PR Changed review workflow
- Cancellation workflow
- Plan Ledger
- Nightly + manual sync
- Sync log
- Audit
- Search/tracking
- Role permissions
- Central Pool
- Assigned Purchase
- Plan Amendment
- Excel export direction
- PlanFin deferred to Phase 2

Core implementation may begin without waiting for the real HOSxP adapter, provided the integration boundary is respected.

---

## 49. Handoff Instruction for Coding Agents

Any coding agent taking over this project must:

1. Read this document before modifying code.
2. Inspect the existing repository and tests.
3. Reuse existing Orchestra, DA, UX/UI, validation, evidence, and governance components where applicable.
4. Do not reimplement equivalent components without justification.
5. Do not invent missing HOSxP facts.
6. Implement wave-by-wave.
7. Run tests after each wave.
8. Report exact changed files and exact test results.
9. Do not mark a wave complete if mandatory tests fail.
10. Keep PlanFin out of Phase 1.

Before first code change, the coding agent should produce:

- Repository assessment
- Existing vs missing capability map
- Proposed architecture mapped to the actual repository
- Dependency graph
- Implementation wave plan
- Expected changed files
- Risks/conflicts
- Tests/validators to run

Only after that assessment is reviewed should implementation begin.

---

# End of Phase 1 Technical Specification
