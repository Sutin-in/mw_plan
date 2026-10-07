# ADR-011 — PR synchronization, HOSxP cancellation, invalid live evidence (Wave 6)

- **Status:** Accepted with Wave 6 (2026-09-29)
- **Spec:** §14.6, §16, §25, §26, §33, §40.6 – §40.8; decisions D-02, D-03, D-07, D-10, D-17,
  D-21, D-22, D-23, D-29, D-30, D-31. Gate G-4 stays **open**.

## 1. State × outcome matrix

The live PR is compared with the current confirmed version's PR snapshot through the ONE shared
classifier `domain/pr_changes.py`. Both sides use the ONE canonical serializer
`application/pr_snapshot.py`, which is also used for drafts and confirmation. "Review" means
`PR_CHANGED_REVIEW_REQUIRED`.

| Outcome | CONFIRMED_LOCKED | PROCUREMENT_VERIFIED | PR_CHANGED_REVIEW_REQUIRED | UNLOCKED_FOR_REVISION | Release |
|---|---|---|---|---|---|
| `UNCHANGED` | stay | stay | stay | stay | no |
| `NON_CRITICAL_CHANGE` | stay (audit + display status) | stay | stay | stay | no |
| `CRITICAL_CHANGE` / `IDENTITY_CHANGE` | → Review | → Review | stay | stay | no |
| `INVALID_EVIDENCE`, class `CONTROL` | → Review | → Review | stay | stay | no |
| `INVALID_EVIDENCE`, class `BINDING` / `STATUS` | stay | stay | stay | stay | no |
| `CANCELLED` (IT-mapped, reliably bound) | → CANCELLED | → CANCELLED | → CANCELLED | → CANCELLED | **once** |
| `NOT_FOUND`, `UNAVAILABLE`, `ERROR` | stay | stay | stay | stay | no |
| `SKIPPED_STATE_MOVED` | observation only | | | | no |

Scope rules:

- **Excluded from sync:** `DRAFT` is never selected and has no sync action. `CANCELLED` and
  `CLOSED` are excluded from the full and nightly sync. Nothing ever reaches `CLOSED`, which is
  also enforced by the database.
- **Governance re-check.** `PLAN_OFFICER` or `ADMIN` may re-check one `CANCELLED` PPR. This is
  observation only, with these outcomes:
  - `CANCELLED`: still cancelled; nothing else happens.
  - `ANOMALY`: a mapped status other than `CANCELLED`. Recorded with the code
    `CANCELLED_PR_REACTIVATED_EXTERNALLY`; there is no transition, no re-post and no PR reuse.
  - `NO_AUTHORITATIVE_STATUS`: the status is `UNKNOWN`, which is never an anomaly.
  - `NOT_FOUND`, `UNAVAILABLE`, `INVALID_EVIDENCE`: recorded as for any sync.
- **`UNKNOWN`, blank or unmapped status** means only "no authoritative cancellation status is
  available". It never cancels, closes, reactivates or causes a transition by itself.

## 2. Per-PPR sequence (`application/sync_services.py`)

1. **Short read, no lock.** Read the PPR, its bound PR number, state and current version, and
   that version's confirmed snapshot.
2. **HOSxP read outside any transaction.** Read the header; `observed_at` is the time HOSxP
   answered. Read the items only when the PR is bound, readable and not `CANCELLED`.
3. **Pure assessment** (`domain/pr_sync.assess`).
   - A control failure other than precision (total ≠ Σ items, no items, duplicate lines) gets
     exactly **one** immediate fresh read, still outside any lock.
   - The fresh read is used. The discarded first read is kept in `details.discarded_first_read`
     as technical evidence.
   - Precision (D-23) is never re-read and never rounded.
4. **One transaction.**
   - Lock the PPR row `FOR UPDATE`.
   - Re-check the state and version:
     - a new version was confirmed, or the PPR left the scope → `SKIPPED_STATE_MOVED`;
     - the same version in another open state → re-assessed against the locked state.
   - Lock plan items (in id order) only when a cancellation release will happen.
   - Write the state change, the `PPR_RELEASED` entries, the display status, the observation and
     the audit record together.
5. **Unexpected failure** of any step: recorded as that PPR's `ERROR` observation.
   - The stored text is only a safe code and message; the detail goes to the server log.
   - The run continues with the next PPR.

Run behaviour:

- A run stops after 3 consecutive `UNAVAILABLE` results (configurable). The PPRs not attempted
  are listed on the run and picked up by a retry run.
- A retry repeats the PPRs with outcome `UNAVAILABLE` or `ERROR`, or `INVALID_EVIDENCE` of class
  `BINDING`/`STATUS`, plus the ones not attempted.
- Only one `PPRS` (full) run can be `RUNNING`; this is a partial unique index. A second start is
  refused with `SYNC_ALREADY_RUNNING`.
- `RUNNING` runs older than 6 h (configurable) are closed as `FAILED` / `STALE_RUN` whenever any
  PR sync starts.
- Run status is `SUCCEEDED` when every attempt gave a trustworthy observation, `FAILED` when
  none did, and `PARTIAL` otherwise.

## 3. INVALID_EVIDENCE vs CANCELLED: precedence

The first rule that applies decides:

1. HOSxP unreachable → `UNAVAILABLE`.
2. Header query/configuration failure (the adapter's `HosxpConfigurationError`, including more
   than one row and a header that does not fit the contract) → `INVALID_EVIDENCE` / `BINDING`.
3. No header row → `NOT_FOUND`.
4. Unreliable header:
   - `pr_no` ≠ the bound PR, or `pr_id` ≠ the confirmed `pr_id` → `INVALID_EVIDENCE` / `BINDING`;
   - malformed status (a normalized status that did not come from a native value, or is not a
     known value) → `INVALID_EVIDENCE` / `STATUS`.
5. Mapped `CANCELLED` → `CANCELLED`, cancel and release, **even if the items or totals are
   invalid**. The items are not read.
6. Items query failure → `INVALID_EVIDENCE` / `BINDING`.
7. Control evidence fails → `INVALID_EVIDENCE` / `CONTROL`. It carries the exact code, field and
   line, and no classifier diff.
8. Otherwise, in order: identity change → critical change → non-critical change → unchanged.

Rules 2, 4 and 6 never change the state (confirmed by the product owner). A query or
configuration fault can hit many PPRs at once and must not cause mass transitions.

## 4. Migration 0006

**`ppr_sync_observation`** is append-only: the `ppr_reject_evidence_mutation` trigger blocks
UPDATE, DELETE and TRUNCATE. Its columns:

- `sync_run_id`, `ppr_id`, `pr_no`, `compared_version`
- `observed_at`, `recorded_at`
- `outcome`, `evidence_class`
- `hosxp_status`, `native_status`
- `changes`, `evidence_issues`, `live_pr` (canonical form only), `details`
- `state_before`, `state_after`, `released`
- `anomaly_code`, `error_code`, `error_message`

CHECK constraints:

- known outcome, class and states;
- a class if and only if the outcome is `INVALID_EVIDENCE`;
- a release only with a `CANCELLED` outcome and state;
- `state_after <> 'CLOSED'`.

Indexes:

- unique partial `(ppr_id) WHERE released`: a second release is impossible at database level;
- `(ppr_id, id)` and `(sync_run_id)`;
- a partial governance index.

**`sync_run`** gains:

- `ppr_id` and `retry_of_sync_run_id` (FKs), with CHECKs tying them to the scopes `PPR` /
  `PPR_RECHECK` and `PPR_RETRY`;
- the partial unique index `uq_sync_run_one_running_pprs`.

Scope stays free text, as in 0001. The application uses `MASTERS`, `PPRS`, `PPR`, `PPR_RETRY`
and `PPR_RECHECK`.

Unchanged:

- `plan_ledger`: the release key is `release:{PPR number}:{plan item}` from
  `PlanItemLedger.release_entry_for`, unique by `idempotency_key`.
- Migrations 0001 – 0005.

## 5. Permissions and endpoints

| Endpoint | Permission | Roles |
|---|---|---|
| `POST /api/pr-sync` (`{}`, `{"retry_of": id}`, optional `"background": true`) | `SYNC_RUN` | PLAN_OFFICER, ADMIN |
| `POST /api/pprs/{id}/sync` (open PPRs; drafts refused `PPR_NOT_SYNCABLE`) | `SYNC_RUN` | PLAN_OFFICER, ADMIN |
| `POST /api/pprs/{id}/recheck` (`CANCELLED` only) | `SYNC_RUN` | PLAN_OFFICER, ADMIN |
| `GET /api/pr-sync/governance` (the latest observation per PPR while it needs attention) | `SYNC_RUN` | PLAN_OFFICER, ADMIN |
| `GET /api/pr-sync/runs`, `/api/pr-sync/runs/{id}` | `SYNC_READ` (new) | PLAN_OFFICER, ADMIN, PROCUREMENT, HEAD_OF_PROCUREMENT |
| `GET /api/pprs/{id}/observations`, timeline "observed in HOSxP" entries | `PPR_READ` + department scope | anyone who can read the PPR |
| Review exit (unlock) | `PPR_UNLOCK` | unchanged |

Notes:

- `/api/sync/runs` (master data) now lists `MASTERS` runs only.
- The user interface starts a full run in the background and follows the run page, which
  refreshes itself. One-PPR sync waits up to 3 minutes.
- **Nightly.** `python -m ppr.cli sync-prs --mode NIGHTLY` needs the real SQL HOSxP mode.
  - Exit codes: 0 = succeeded, 1 = partial or failed, 2 = configuration problem, 3 = already
    running.
  - Windows Task Scheduler owns the schedule. 02:00 is only an example; hospital IT sets the
    time.

## 6. Timeline and UI

- **Timeline:**
  - The timeline shows the system actions `PPR_PR_CHANGED` and `PPR_CANCELLED_FROM_HOSXP`.
  - It also shows "observed in HOSxP" entries (`source: HOSXP`) for non-unchanged observations.
  - The same finding repeated night after night is shown once.
- **UI:**
  - A menu entry "ซิงค์ PR กับ HOSxP" leads to runs, the retry button, the governance list and
    the last successful full sync time (§26).
  - PPR detail shows the latest observation, a sync button (open PPRs), a re-check button
    (cancelled PPRs) and review guidance.
  - Unlock is offered from every state the state machine allows.

## 7. Real-HOSxP evidence still required

1. **D-30 cancellation status:**
   - the query, table and column;
   - real cancelled PR examples;
   - the native values;
   - that the status is authoritative;
   - whether cancellation is final or reversible;
   - verifier and date.

   The mapping ships empty, so no cancellation can happen until this evidence exists.
2. **Stability and uniqueness of the PR's internal `pr_id` and number.** The binding check relies
   on them.
3. **Whether HOSxP deletes or renumbers PRs.** The behaviour is already decided (`NOT_FOUND`, no
   action).
4. **D-25 login evidence** (separate, unchanged).
5. **G-4 terminal/`CLOSED` mapping** (open; not part of Wave 6).
6. **Production schedule time and acceptable load** (hospital IT).

Not included in Wave 6: nightly master-data sync and budget/central-stock sync (§25.3 lists them
for the nightly job; master sync stays manual; central stock is Wave 7, not started).
