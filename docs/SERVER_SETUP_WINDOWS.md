# Windows Server setup — development / test

This sets up a **development and test** server. It is **not** a production deployment:
there is no real HOSxP connection, and production needs extra hardening (ADR-004: separate
database roles, backup/restore drill, TLS). Do not load real patient or HOSxP data here.

## 1. Choose a working model

| Model | How programmers work | PostgreSQL access |
|---|---|---|
| **A. Work on the server** (simplest) | Each programmer logs in to the server (Remote Desktop) with their **own Windows account** and keeps their own clone of the repository | Local only (`localhost`) |
| **B. Work on own PCs** | Each programmer has Python + Git on their PC; the server only hosts PostgreSQL | Remote, restricted to the programmers' IP addresses |

Every programmer gets **their own** role and databases (`--name`, step 5), because test runs
recreate the test schema and would collide on a shared test database.

## 2. Install software (IT / administrator, once)

1. **PostgreSQL 16** — https://www.postgresql.org/download/windows/ → *Download the installer* (EDB) → PostgreSQL 16.x, Windows x86-64.
   - Components: **PostgreSQL Server**, **Command Line Tools** (pgAdmin optional, Stack Builder not needed).
   - Port **5432**. Set the `postgres` superuser password and keep it with IT; programmers do not need it after step 5.
2. **Python 3.12** — https://www.python.org/downloads/windows/ → Python 3.12.x, Windows installer (64-bit). Tick **"Add python.exe to PATH"**.
3. **Git for Windows** — https://git-scm.com/download/win (or GitHub Desktop).
4. **Node.js 20 LTS or newer** (Wave 5A user interface, D-24) — https://nodejs.org → *LTS*, Windows Installer (.msi), 64-bit. Default options.

Verify in PowerShell:

```powershell
py -3.12 --version
git --version
node --version
& "C:\Program Files\PostgreSQL\16\bin\psql.exe" --version
```

## 3. PostgreSQL network access

- **Model A:** keep PostgreSQL reachable only from the server itself. In `postgresql.conf` (in the data directory, default `C:\Program Files\PostgreSQL\16\data`) set `listen_addresses = 'localhost'`, then restart the *postgresql-x64-16* service.
- **Model B:** allow only the programmers' PCs:
  - `postgresql.conf`: `listen_addresses = '*'` (or the server's LAN address)
  - `pg_hba.conf`: one line per programmer, e.g. `host  ppr_somchai_dev,ppr_somchai_test  ppr_somchai  10.0.0.25/32  scram-sha-256`
  - Windows Firewall: allow inbound TCP 5432 **only** from those IP addresses.
  - Restart the *postgresql-x64-16* service.

Check the current setting before changing it; installers differ.

## 4. Get the code (each programmer)

```powershell
git clone https://github.com/<organization>/<repository>.git C:\work\Plan_Claude
cd C:\work\Plan_Claude
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e "backend[dev]"
```

If Git reports *"detected dubious ownership"*, run once:
`git config --global --add safe.directory C:/work/Plan_Claude`

## 5. Create the programmer's own databases

Run **on the server** by IT together with the programmer (needs the `postgres` password once;
it is not stored). The superuser never logs in from a programmer's PC.

```powershell
# Model A (programmer works on the server, in their own clone):
.venv\Scripts\python tools\dev\setup_local_db.py --name somchai

# Model B (programmer works on their PC): run on the server in a clone kept by IT,
# using the server's LAN address so the written .env points at the server:
.venv\Scripts\python tools\dev\setup_local_db.py --name somchai --host <server-lan-address>
# then hand the resulting .env to the programmer securely (it holds their DB password)
# and delete it from IT's clone.
```

This creates role `ppr_somchai` and databases `ppr_somchai_dev` / `ppr_somchai_test`,
revokes other users' access to them, and writes `.env` (git-ignored) in that clone.
Without `--name` it creates `ppr_app` / `ppr_dev` / `ppr_test` (single-user setup).
The report (no secrets) is in `_db_setup_result.txt`.

## 6. Verify

```powershell
.venv\Scripts\python tools\check.py
```

Expected: **9/9 gates passed** (ruff lint, ruff format, mypy, import-linter, phase_guard,
database, traceability, frontend, pytest). Then prepare the development database:

```powershell
.venv\Scripts\python -m ppr.cli migrate
```

## 7. Try the system (demonstration mode, D-26)

Double-click `run_demo.bat` in the repository folder (or run the commands in ADR-009). It
migrates the development database, starts the API in demonstration mode and the user
interface, and opens http://127.0.0.1:3000. Demo users: `demo_req_ent`, `demo_req_or`,
`demo_plan`, `demo_proc`, `demo_head` (appointed Head of Procurement), `demo_head_old`
(last year's head), `demo_finance`, `demo_exec`, `demo_admin`; password `demo1234`.
The data is mock HOSxP data only. Close the two server windows to stop.

## 8. Rules for the server

- `.env` stays on the machine; never commit or share it.
- One role and one pair of databases per programmer; never run tests against another person's databases or against anything not named `*_test` (the tests refuse to).
- No real HOSxP connection and no real patient data on this server during Phase 1 development.
