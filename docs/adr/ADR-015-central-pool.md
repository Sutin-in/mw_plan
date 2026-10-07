# ADR-015 — Central Pool (Wave 7B-1)

- **Status:** Proposed with Wave 7B-1 (2026-10-01)
- **Spec:** §10.2, §12, §18, §22.1, §37; decisions D-01, D-16, D-37, D-38 (G-1 resolved).
  G-3 (Assigned Purchase) is Wave 7B-2; G-4 stays open.

## Decision

A `CENTRAL` Plan Item is controlled by purchase (D-37 1-C, D-38): the central store's PR and PPR
consume its quantity and amount like any PPR; issues from the store to departments are never posted.

| D-38 | Implementation |
|---|---|
| C-1 purchasing department per item | `plan.match_department`: the owner of a `DEPARTMENT` item, the purchasing department of a `CENTRAL` item. `pr_services.evaluate` offers `CENTRAL` items to PRs of their purchasing department (same HOSxP item and fund source). Activation refuses a `CENTRAL` item without a purchaser (`CENTRAL_WITHOUT_PURCHASER`), and so does Plan Amendment. |
| C-2 ambiguity fails closed | Matching is unchanged: two candidates → `AMBIGUOUS_PLAN_MATCH` (A-1). To keep such plans from arising, activation and amendment refuse a `DEPARTMENT` and a `CENTRAL` item (or two `CENTRAL` items) with the same item, matching department and fund source (`DUPLICATE_MATCH_KEY`). |
| C-3 stock issues shown only | *(Superseded in detail by D-39 / ADR-018: the two events are now refused by the application and the database, and the report shows verified issues, returns and net use.)* No ledger posting; `CENTRAL_STOCK_ISSUE` / `CENTRAL_STOCK_RETURN` stay unused. The *Central Plan Usage* report has the column "ยอดเบิกจากคลังให้หน่วยงาน" with "ยังไม่มีข้อมูล (รอ IT ยืนยัน query จาก HOSxP)" until IT confirms a read-only query; no HOSxP table or field is assumed. |
| C-4 visibility | `PlanRepository.items(fy, department)` and `plan.visible_to_department` (one rule): a `CENTRAL` item is seen by its purchasing department only (its owner field gives no view); organisation-wide roles see all. Single-item reads and balances, the amendment history and the *Plan Amendment* report follow the same rule. Dashboards, alerts and plan reports count a `CENTRAL` item under its purchasing department (`plan.responsible_department`). |

- Report `CENTRAL_PLAN_USAGE` ("การใช้แผนกลาง", plan kind, `.xlsx` like every report): one row per
  `CENTRAL` item with planned, used (the store's PPRs) and remaining quantity and amount; the
  department filter means the purchasing department.
- Demo mode gains a store requester (`demo_req_store`) and a store PR (`MOCK-PR-{fy}-1007`), which
  is not in plan until a Central Pool item is added (by Plan Amendment).
- No migration (schema 0008 already allows everything).

## Consequences

- A plan made before 7B-1 that holds a colliding `DEPARTMENT` and `CENTRAL` item keeps working for
  every other item; PRs for the colliding item fail closed until the plan is amended.
- `CENTRAL` items now appear only to their purchasing department: a requester of the owner
  department no longer sees a `CENTRAL` item it does not buy.
- A `CENTRAL` item activated before 7B-1 without a purchasing department matches no PR and is
  seen only by organisation-wide roles; the Plan Officer sets its purchaser by Plan Amendment
  (an amendment that touches it is refused until it has one).
- When Assigned Purchase is matched (Wave 7B-2), `match_department` and `match_key_issues` must
  include `ASSIGNED` items.

## Verification evidence (Wave 7B-1, 2026-10-01)

| Where | Commit | Result |
|---|---|---|
| Cloud (Linux, PostgreSQL 16.13) | `3377d31` | `tools/check.py` 9/9, 1,231 passed |
| Cloud end-to-end (`tools/dev/verify_wave7b1.py`, demo mode) | `3377d31` | 17/17 on a fresh development database; 11/11 on a repeat run (the steps that write are skipped once done) |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `5c7ecdb` | `tools/check.py` 9/9, 1,211 passed, 20 skipped (MariaDB connector tests, no local MariaDB); the run took 7 h 38 min because the computer slept during it |
| Windows end-to-end | `5c7ecdb` | 17/17 |
| Cloud, `main` after fast-forward merge | `5b36ebc` | `tools/check.py`: 1,211 passed + the 20 MariaDB connector tests passed in a re-run once the local MariaDB service was started (1,231 in all) |

**Test data left in the Windows development database (FY 2570):** Plan Amendment no. 2 adding a
Central Pool toner (5 × 100, bought by Mock Store, OFFICE line +500), and one confirmed store PPR
from `MOCK-PR-2570-1007` drawing 500.00 on it. Demo data (mock HOSxP), kept as evidence.
