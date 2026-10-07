# ADR-013 — Reports and Excel export (Wave 8B)

- **Status:** Proposed with Wave 8B (2026-09-30)
- **Spec:** §4.3, §22.1, §36, §37; decisions D-32, D-33, D-35. Gates G-1, G-3, G-4 stay open.

## Design

| Part | Where |
|---|---|
| Report definitions (columns, Thai headings, value types) and row building | `application/report_services.py` — plain data, no SQLAlchemy / FastAPI |
| PPR filters: first-confirmation date range, confirmed-only; first confirmation and last verification times | `PprSearch`, `PprRepository.first_confirmations`, `.last_verifications` |
| Audit rows with filters | `AuditWriter.search` |
| HTTP: `GET /api/reports`, `GET /api/reports/{code}`, `GET /api/reports/{code}.xlsx` | `api/report_routes.py` |
| `.xlsx` writer (openpyxl) | `api/report_xlsx.py` |
| Screens | Express `reports` (list) and `report` (filters, table, download link) |

- One row builder serves the screen and the file, so they cannot disagree.
- Plan reports reuse the Wave 8A item balances (`dashboard_services.item_statuses`) and so the
  ledger formula of §18.3; PPR reports reuse the PPR search with the caller's scope.
- `REPORT_READ` (every role); the audit report additionally needs `AUDIT_READ`.
- Each `.xlsx` download writes one `REPORT_EXPORTED` audit row; viewing on screen writes nothing.

## Consequences

- New runtime dependency `openpyxl` (`pip install -e backend` again on existing machines).
- The UI stays free of business rules: it forwards the filters and shows the API's columns.

## Verification evidence (Wave 8B, 2026-09-30)

| Where | Commit | Result |
|---|---|---|
| Cloud (Linux, PostgreSQL 16.13) | `0c2fa00` | `tools/check.py` 9/9, 1,096 passed |
| Cloud end-to-end (`tools/dev/verify_wave8b.py`, demo mode) | `f4751ba` | 40/40 |
| Windows dev machine (PostgreSQL 16.15, Node 24.16) | `0c2fa00` | `pip install -e backend[dev]` (openpyxl 3.1.5), `migrate` to 0007, `tools/check.py` 9/9, 1,076 passed, 20 skipped (MariaDB connector tests, no local MariaDB) |
| Windows end-to-end | `f4751ba` (only the check script changed after `0c2fa00`) | 40/40 |
| Cloud, `main` after fast-forward merge | `f4751ba` | `tools/check.py` 9/9, 1,096 passed |

**Caveat — PPR requester isolation, Windows live data: verification incomplete.** On the Windows development database
there is no confirmed PPR, so the end-to-end register check compared two empty registers
(0 rows each) and did not exercise isolation with live data there. Requester isolation is
covered by the automated tests that passed on Windows (`test_report_api.py`: a requester of
another department sees an empty register; a requester passing another department's id sees
nothing; plan-report scope by owner / purchaser / demand department) and by the cloud live-data
run, where the requester saw 1 row (own department) of the organisation's 2.

Windows live-data verification of requester isolation stays **open** until a confirmed PPR exists
on a Windows database; it is to be repeated then (`tools/dev/verify_wave8b.py`).
