# ADR-009 — User interface, running servers, HOSxP login and demonstration mode (Wave 5A)

- **Status:** Accepted with Wave 5A (2026-09-29)
- **Spec:** §21, §22.9, §31; decisions D-24 (UI stack), D-25 (HOSxP MD5 password check), D-26 (Wave 5 split, demonstration mode).

## Shape

```text
browser ──HTML forms──▶ Express UI (frontend/, Node.js ≥ 20)
                          │  API token in a signed HttpOnly SameSite=Strict cookie
                          │  CSRF token on every form; CSP: no page scripts
                          ▼
                        Python API (python -m ppr.cli serve)  ──▶ PostgreSQL
                          │  every rule, permission and audit      (plan, PPR, ledger)
                          ▼
                        HOSxP (read-only): mock in demo mode, SQL site queries otherwise
```

- The Express application is presentation only (D-24). It calls the API with the user's
  token, shows the API's answer, and translates error codes to Thai. It has no database
  driver and no HOSxP access; `tests/guards/test_frontend_boundary.py` fails if one appears.
  Buttons follow the user's roles only as hints; the API refuses anything not allowed.
- Roles are re-read from the API on every page (`GET /api/me`), so a revoked role applies at once.
- The printed PPR is the API's own page (D-20), passed through with a narrow policy.
- One-time messages after an action are kept in the UI process's memory (the cookie holds
  only a random id), so long problem lists are never cut by the browser's cookie limit.
- Logging out removes the cookie; the API token itself stays valid until it expires
  (stateless tokens, ADR-004, `PPR_SESSION_TTL_MINUTES`).

## Running

```text
python -m ppr.cli migrate
python -m ppr.cli serve --demo          # API on 127.0.0.1:8000, demonstration mode
node frontend/src/server.js             # UI on 127.0.0.1:3000 (PPR_API_URL, PPR_UI_PORT)
run_demo.bat                            # Windows: all of the above
```

The server refuses to start when the schema is not at the latest migration. The UI cookie
key is `PPR_UI_COOKIE_SECRET`, or derived from `PPR_SESSION_SECRET` when absent.

## HOSxP login (D-25)

`SqlHosxpAuthenticationAdapter` runs two site queries from `hosxp_queries.toml`:
`get_login_user(:username)` → `source_id, username, password_hash[, display_name,
department_id, active]` and `get_user_profile(:source_id)`. It hashes the typed password
(UTF-8) with MD5 and compares with the stored hexadecimal hash in constant time, ignoring
case. More than one row is a configuration error; HOSxP unreachable → 503, never "wrong
password". Neither the password nor the hash is stored, logged or returned. MD5 is HOSxP's
scheme as stated by the product owner, not a choice of this system (this system's own
sessions are HMAC-signed tokens, ADR-004).

This is **provisional compatibility behaviour, not verified HOSxP authentication**: UTF-8
password bytes, hexadecimal MD5, case-insensitive comparison and the site-provided `active`
field are assumptions until checked. Go-live is blocked by the D-25 go-live gate: real-HOSxP
evidence for (1) the query/table/column mapping, (2) the stored hash format (raw 32-character
hexadecimal MD5 or another representation; prefix, suffix, case, whitespace), (3) the password
encoding before hashing, and (4) the exact meaning of an active/enabled/disabled account. If the
evidence differs, the adapter and its tests are changed before go-live.

## Demonstration mode (D-26)

`serve --demo` builds a mock HOSxP whose PRs are dated today in the current fiscal year
(`MOCK-PR-<FY>-1001` … `-1006`, each showing one behaviour: ordinary, second category,
header-total mismatch D-17, expired year D-18, other department D-09, more than the plan
has left), seven demo users (password `demo1234`), and seeds idempotently the users' roles
and an `ACTIVE` plan for the current fiscal year. It refuses `PPR_HOSXP_MODE=sql` and any
`*_test` database; every page shows a demonstration banner. After the fiscal year changes,
restart the server to get the new year's demo PRs.

## Gates

`tools/check.py` gains a ninth gate, **frontend**: Node.js ≥ 20, `npm ci` when needed,
`node --check` on every file, `node --test`. CI installs Node 22.

## Not in 5A

Head of Procurement verification with the annual appointment, search and the tracking
timeline (Wave 5B, D-26). The real HOSxP login needs the site's `[queries.get_login_user]`
and `[queries.get_user_profile]` written by IT.
