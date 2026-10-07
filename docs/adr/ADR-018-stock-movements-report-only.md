# ADR-018 — Stock issues and returns: HOSxP data for the report only (Wave 10B-1)

- **Status:** Accepted - merged to `main` by fast-forward at `6b825a5` (2026-10-02); baseline tag `phase1-wave10b1-baseline` on the post-merge verified commit (recorded in the tag message and in the next documentation commit)
- **Spec:** D-39 (product owner, 2026-10-02; refines D-38 C-3, supersedes D-01); §10.2, §18.2,
  §18.3, §27, §37, §40.12, §42. G-4 and the HOSxP cancellation native-status mapping stay OPEN.
  No HOSxP table, field, movement kind or native status was added or guessed: every HOSxP
  stand-in here is the made-up `MOCK-*` / `mockhx_*` data.

## Decision

| D-39 | Implementation |
|---|---|
| Stock movements are never Plan Ledger transactions | **Application:** `LedgerEventType` no longer has `CENTRAL_STOCK_ISSUE` / `CENTRAL_STOCK_RETURN`; `LedgerEntry` refuses either name whatever the deltas (`StockMovementNotLedgerError`) and any other unknown event. `LedgerBalance` has no stock fields: remaining = approved + amendments − PPR usage + releases (§18.3). The balance API (`BalanceOut`) lost `stock_issued_qty` / `stock_returned_qty` (always 0, never used). **Database:** migration 0010 removes the two events from `ck_plan_ledger_known_event`, drops their D-01 sign rules and adds `ck_plan_ledger_no_stock_movement` (`event_type NOT IN (...)`). The upgrade changes no row; if a database ever held such an entry the upgrade stops with a message instead of touching the append-only ledger. 0001–0009 unchanged. |
| No stock ledger here | Nothing stores movements: no table, no column, no cache. `application/stock_services.read_stock` reads them when the report runs and returns totals in memory. An import-linter contract forbids `stock_services` from importing the ledger, the ports (repositories / unit of work), the infrastructure or the PPR / plan / sync services; a test checks which modules use the stock read at all. |
| Verified query only | Contract `StockMovement` (movement id, date, receiving/returning department, item, qty > 0, `native_kind` as HOSxP has it, last modified). Gateway: `stock_movement_source()` (configuration only) and `get_stock_movements(date_from, date_to)`. The site file has a separate `[stock_movement]` section: `sql`, `verified_by`, `verified_on` (TOML date), `reference` and `[stock_movement.kind_map]` (native kind → `ISSUE` / `RETURN`, at least one `ISSUE`). The query is used only with all of these; it is never run otherwise (`QueryConfig.query` and the gateway both refuse). Any problem in that section (missing record, unsafe or wrongly parameterised SQL, unknown kind, the query placed under `[queries]`) only disables the stock figures, with the reason — the file still loads and every PR feature is unaffected. `hosxp-check` prints the state on one line (informational, not part of its result). The old optional `get_stock_issues(since)` contract, never used by a feature, is replaced. |
| Fail closed | `read_stock` gives "not available" with a reason (Thai) when there is no verified query, HOSxP cannot be reached, the query fails, a movement id repeats or a row lies outside the asked dates; the report then shows "ยังไม่มีข้อมูล" in the stock cells and a note, and the rest of the report (PPR usage) is unchanged. **Unknown is never coerced to zero** (product owner, 2026-10-02): a movement of an unmapped kind could be an issue or a return, so the issued / returned / net-used figures of that item and department, and the item's total, show "ยังไม่มีข้อมูล" (never the mapped part alone); other departments' figures stay. The note says how many movements were not classified (to a department that sees only its own rows: that some were). An item with no movement in a verified, complete read is a known zero. With no `RETURN` kind mapped (IT has not yet said how HOSxP records a return) returns, net used and bought-not-issued show "ยังไม่มีข้อมูล"; nothing is netted by guess. |
| Report columns | *Central Plan Usage*: row level · item · purchaser · fund source · category · receiving department · planned · bought (PPR) · plan remaining (not bought) · planned / bought / remaining amount · issued · returned · net used (issued − returned) · bought but not issued (bought − net used) · note. Period: the fiscal year from its first day to the server date. A negative "bought but not issued" (more issued than bought this year, e.g. old stock) is shown as it is, with a note. |
| No split by fund | When one HOSxP item is in more than one `CENTRAL` Plan Item of the year (any fund, any purchaser), each plan row shows only its plan figures and one "รวมสินค้า (ทุกรายการแผน)" row carries the item's stock totals with "ไม่สามารถแบ่งยอดตามแหล่งเงินได้จากข้อมูล HOSxP" (plus "และตามหน่วยงานผู้ซื้อ" when the purchasers differ). Nothing is divided, nothing is posted. A fund or category filter still shows the item's stock only as the whole-item total. When some `CENTRAL` lines of the item are outside the caller's view (another purchaser's line, or a purchasing-department filter), the bought figures on that row are partial, so "bought but not issued" shows "ยังไม่มีข้อมูล" with a note instead of comparing part of the purchases with all the issues. |
| No allocation by inference | (product owner, 2026-10-02) HOSxP issues of an item go on its central plan line only when that line is the item's only plan line of the year, of any type. When the item is also in a `DEPARTMENT` or `ASSIGNED` line (bought outside the Central Plan) or in another `CENTRAL` line, the plan lines keep only plan figures and one item row carries the issues, with a note; "bought but not issued" is computed only when every plan line of the item is a `CENTRAL` line in the reader's view, otherwise "ยังไม่มีข้อมูล". |
| Visibility | Purchasing department and organisation-wide roles (the Plan Item scope, C-4): plan rows, item totals and one row per receiving department. Any other department: only rows of its own department for central items it received (issued, returned, net used) — no planned or bought figures, fund source, category or purchaser, never another department; a department, fund-source or category filter that is not its own gives nothing (no probing of plan details). Nothing is shown to them while there is no data. |

- Identifiers and unit: the query returns the same item and department ids as the masters and
  quantities in the item's unit (§27, example file); IT confirms both when verifying.
- A department that sees only its own rows is told that some movements were left out, not how
  many hospital-wide.
- The dashboards are unchanged: they show no stock figures (only the report does).
- Demo mode (`serve --demo`): the MOCK gateway holds made-up toner and glove movements dated
  the server date and a stand-in "verified" source (`MOCK (ข้อมูลจำลอง)`, reference `MOCK-DEMO`),
  so the report can be tried. A site starts with no verified source, as D-39 requires.
- Test HOSxP (`tests/mockhx_schema.py`): a made-up `mockhx_move` table and a verified
  `[stock_movement]` section, run on PostgreSQL and MariaDB.
- `tools/dev/verify_wave10b1.py`: end-to-end on a development database (see Evidence).
- The per-wave scripts `verify_wave7a/7b1/7b2/8b.py` check the schema version and the stock
  column as they were in their wave; they are kept unchanged as that wave's evidence.

## §40.12 acceptance criteria

| ID | Criterion (D-39) | Tests |
|---|---|---|
| AT-40.12.1 | Verified issues / returns shown correctly per item and department | `test_stock_report`, `test_stock_services`, `test_hosxp_sql_gateway` (PostgreSQL + MariaDB) |
| AT-40.12.2 | Never change Plan Ledger quantity or amount; refused by application and database | `test_ledger`, `test_plan_core`, `test_migration_0010`, `test_stock_report`, `test_stock_services` |
| AT-40.12.3 / .4 | (unchanged: demand preserved, fulfilled demand not bought again) | as in Wave 10A |
| AT-40.12.5 | Not configured / not verified / failing query → "ยังไม่มีข้อมูล", Plan Ledger untouched | `test_stock_report`, `test_query_config`, `test_hosxp_boundary`, `test_hosxp_sql_gateway`, `test_stock_services` |
| AT-40.12.6 | Repeating report, read and synchronization changes no plan / PPR data | `test_stock_report` |
| AT-40.12.7 | HOSxP stays the source of truth: nothing stored, report reflects HOSxP as read | `test_stock_report`, `test_hosxp_sql_gateway`, `test_stock_services` |

AT-40.12.1 / .2 are no longer deferred (`docs/acceptance_deferred.txt` is empty).

## Evidence

| Where | Commit | Result |
|---|---|---|
| Cloud (Linux, PostgreSQL 16.13; MariaDB for the SQL-adapter tests) | branch head | `tools/check.py --acceptance-report` 10/10 gates, 1,355 passed, 1 skipped (after the unknown/ambiguity rule) (role-split test, as in ADR-017); acceptance **80 PASS** (AT-40.12.1-.7 all PASS, nothing deferred) |
| Cloud end-to-end (`tools/dev/verify_wave10b1.py`, `ppr_dev`, demo mode) | branch head | 19/19: schema 0010, named constraint present, the database refuses both events, organisation-wide and store views with per-department rows and the verification note, ENT / OR see only their own row, Excel download, every plan and PPR table unchanged |
| Independent review | `70073b7` | no blockers; fixed: partial purchases against whole-item issues (two purchasers), unit and id requirements documented, Excel content checked for a department, hospital-wide unmapped count hidden from a department, constraint asserted by name, doc nits |
| Windows dev machine (PostgreSQL 16.15, Node 24.16, Python 3.12.9) | `9eb50c3` | migration to 0010 OK on `ppr_dev`; end-to-end `verify_wave10b1.py` **19/19**; check 8/10: 1,329 passed, 24 skipped, **1 failed** - `test_stock_reading_has_no_path_to_the_ledger_or_to_storage` compared file paths with `/` (Windows gives `\`): a test defect, not a product one; fixed (`as_posix()`), acceptance 78 PASS + AT-40.12.2 / .7 FAIL from that test alone |
| Windows dev machine | `3edc5db` (unknown / ambiguity rule) | end-to-end **19/19**; check 8/10: 1,331 passed, 24 skipped, 1 failed - the same path-comparison test as above (fix not yet in this commit); the new unknown / ambiguity tests PASS; acceptance AT-40.12.1, .3-.6 PASS |
| Windows dev machine | `2cb8a1d` (since `3edc5db` only that test file and this ADR changed) | `_verify10b1c.bat`: `test_stock_services.py` **6/6 passed** on Windows - with the `3edc5db` run, every test of the wave has passed on Windows |
| Cloud, `main` after fast-forward merge | `42106c3` | `tools/check.py --acceptance-report` 10/10 gates, 1,355 passed, 1 skipped (role-split test); acceptance 80 PASS; `verify_wave10b1.py` 19/19 (a first run hit a PostgreSQL outage of the cloud container - every database test errored - and was repeated after restarting it) |
| Tag | `42106c3` | `phase1-wave10b1-baseline` pushed from Windows by `_w10b1tag.bat` (2026-10-02) after its checks: working tree clean, the commit is `origin/main`'s head, the tag did not exist |

## Consequences

- IT must supply, for the report to show stock figures: the query (central store only, one row
  per movement, positive quantities, dates in range), the verification record and the kind
  mapping — including how HOSxP records a return. Until then the report shows PPR usage and
  "ยังไม่มีข้อมูล".
- The report reads HOSxP each time it runs (screen and Excel), inside the report's database
  transaction; a slow HOSxP slows that report only (the query has the adapter's statement
  timeout).
- A store's issues of an item cannot be told apart by issuing store unless the site query
  restricts itself to the central store; two purchasers of one HOSxP item share one total
  (marked as not splittable).
