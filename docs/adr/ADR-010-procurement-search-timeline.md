# ADR-010 — Head of Procurement appointment and verification, search, timeline (Wave 5B)

- **Status:** Accepted with Wave 5B (2026-09-29)
- **Spec:** §14.4, §22.3, §22.4, §22.8, §23, §28, §31.6, §31.7, §32, §33, §34, §36, §40.9, §40.10; decisions D-02, D-07, D-09, D-27, D-28.

## Appointments (§23, D-27)

`role_appointment` (table since migration 0001) is now used. Migration 0005 adds revocation
evidence (`revoked_at`, `revoked_by_user_id`, `revoke_reason`) and a PostgreSQL GiST
**exclusion constraint** so that two `ACTIVE` appointments can never cover the same day,
even if the service check were bypassed. The service (`domain/appointment.py` +
`application/procurement_services.py`) additionally:

- keeps both dates inside the appointment's fiscal year; no end date → the year's last day;
- serializes changes (`LOCK TABLE ... SHARE ROW EXCLUSIVE`);
- appoints only an active user holding the `HEAD_OF_PROCUREMENT` role;
- lets `ADMIN` create, **end early** (handover: only shorten; may end yesterday so a successor
  starts today; never before its start or its last day of use for a verification) and
  **revoke** (see below);
- never deletes; every change is audited (`APPOINTMENT_CREATED/ENDED/REVOKED`).
- **Revoke** = entered/issued in error. Refused (`APPOINTMENT_IN_USE`) once the appointment
  has authorized any verification — evidence is preserved; use end-early or governance review.
  An unused appointment may be revoked (reason required); the row stays and its dates become
  free for a corrected appointment.
- A new appointment is refused over dates that already hold verification evidence under
  another appointment (`APPOINTMENT_OVERLAPS_EVIDENCE`).
- Concurrency: verification reads the appointment `FOR SHARE`; end/revoke lock it
  `FOR UPDATE`. A revoke racing a verification either sees the committed evidence and is
  refused, or commits first so the verification re-reads a revoked row and is refused.

Candidates are users holding the `HEAD_OF_PROCUREMENT` role; the list flags an appointee
who has since lost the role (such a user cannot verify).

## Verification (§14.4, D-28)

`POST /api/pprs/{id}/verify {version}` — permission `PPR_VERIFY` (role
`HEAD_OF_PROCUREMENT`), then in one transaction with the PPR row locked:

1. the PPR is visible to the caller and in a state where the state machine allows
   `VERIFY_PROCUREMENT` (`CONFIRMED_LOCKED` only);
2. the version the user reviewed is still current (`PPR_VERSION_CHANGED` otherwise);
3. the caller has an `ACTIVE` appointment effective on the **server date**, of that date's
   fiscal year (`NOT_APPOINTED` otherwise), read `FOR SHARE` so that a concurrent end or
   revocation is waited for and re-checked;
4. `ppr_verification` row (append-only, unique per PPR version: user, time, the server
   date used for the check (`verified_on`), appointment, version) + state `PROCUREMENT_VERIFIED` + audit `PPR_VERIFIED`.

No ledger posting, no HOSxP call, nothing inferred about PR approval or PO. After unlock
and reconfirmation the new version returns to `CONFIRMED_LOCKED` and is verified again.
`CLOSED` still has no inbound transition (D-07, G-4 open).

## Queue (§31.7) and search (§32)

- `GET /api/procurement/queue?bucket=` — `waiting` (CONFIRMED_LOCKED), `verified`,
  `changed` (PR_CHANGED_REVIEW_REQUIRED — filled by Wave 6), `unlocked`, `cancelled`,
  `inactive` (open cases with no change for `inactive_days`, default 30, §34), plus the same
  filters as search and counts per bucket (with the same filters and scope). It also says whether the caller may verify today.
  Permission `PROCUREMENT_QUEUE_READ`: PROCUREMENT, HEAD_OF_PROCUREMENT, PLAN_OFFICER,
  ADMIN, CFO, EXECUTIVE.
- `GET /api/ppr-search` — PR No., PPR No., requester (PR requester name or the preparing
  user), department, item id/name, budget category (PR or allocation), fund source, fiscal
  year (all years), state, created date range. Permission `PPR_READ` and **the same
  department scope as reading one PPR** (a REQUESTER-only user never sees another
  department, also not by passing `department_id`). LIKE wildcards in input are literal.

## Timeline (§33)

`GET /api/pprs/{id}/timeline` (`PPR_READ`, same scope) lists the PPR's own audit events —
draft created, allocation changed, draft discarded, confirmed, unlocked, reconfirmed,
verified, print view opened (new `PPR_PRINT_VIEWED` audit with the version; the system
cannot know whether paper was printed) — with who, when,
version, state change and reason, plus the verifications and "days since the last change"
(`ppr.updated_at`). It contains **no HOSxP events**: none are observed before Wave 6 sync.
It also reports `can_verify` for the caller, used only to show the button.

## User interface

Menu: รายการ PPR · ค้นหา · คิวพัสดุ · การแต่งตั้งหัวหน้าพัสดุ (links follow roles; the API
decides). PPR detail gains the timeline and the "ตรวจสอบและยืนยันแล้ว" button.
Demo mode adds `demo_head` (appointed for the current fiscal year) and `demo_head_old`
(appointed for the previous one only).
