"""Production-configuration and backup/restore drill (Wave 10A) on THIS machine's PostgreSQL.

Builds a throw-away copy of a production installation and exercises it the way IT will run it,
with nothing from a real hospital: a made-up stand-in for HOSxP (``mockhx_*`` tables, the same
fixture the adapter tests use - not HOSxP), random passwords that live only in memory, and
databases / roles whose names all start with ``ppr_drill``. It needs a PostgreSQL superuser to
create those roles and databases: ``PPR_ADMIN_DATABASE_URL``, or the password is asked for
(postgres@localhost) and never stored. At the end everything it created is dropped
(``--keep`` leaves it for inspection).

Checks, in order:

 1. role split: ``migrate`` runs as the schema owner and grants the runtime role; the runtime
    account passes the least-privilege check and is refused UPDATE / DELETE / TRUNCATE on
    evidence, DDL and disabling triggers by PostgreSQL itself;
 2. production refusals: the API will not start with the owner account, on a non-local
    address, or in demo mode;
 3. the API runs under the supervisor in production (read-only SQL HOSxP stand-in); a user
    signs in with the HOSxP password check; masters synchronize;
 4. restart: the API process is killed and the supervisor brings it back; the log shows it;
 5. the nightly job runs through the supervisor; ``ops-status`` passes (schema, least-privilege
    runtime account, fresh nightly run);
 6. the user interface refuses production without HTTPS settings, runs with them, and its
    deep health check turns 503 when the API stops;
 7. backup (consistent snapshot + manifest) -> restore into another database -> the copy is
    identical; restoring over the live database is refused.

    .venv\\Scripts\\python tools\\dev\\drill_production.py
"""

from __future__ import annotations

import getpass
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
sys.path.insert(0, str(BACKEND / "src"))
sys.path.insert(0, str(BACKEND / "tests"))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import URL, make_url  # noqa: E402
from sqlalchemy.exc import DBAPIError  # noqa: E402

import mockhx_schema  # noqa: E402
from ppr.infrastructure.db.privileges import runtime_account_issues  # noqa: E402

API_PORT, UI_PORT = 8768, 3768
APP_DB, HX_DB, RESTORE_DB = "ppr_drill", "ppr_drill_hosxp", "ppr_drill_restore"
OWNER, RUNTIME, HX = "ppr_drill_owner", "ppr_drill_runtime", "ppr_drill_hx"
results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> bool:
    results.append((ok, what))
    print(("PASS  " if ok else "FAIL  ") + what, flush=True)
    return ok


def admin_url() -> URL:
    raw = os.environ.get("PPR_ADMIN_DATABASE_URL")
    if raw:
        return make_url(raw)
    pw = getpass.getpass("PostgreSQL superuser 'postgres' password (localhost, not stored): ")
    return URL.create(
        "postgresql+psycopg", username="postgres", password=pw, host="localhost", port=5432
    )


def http(url: str, data: dict[str, object] | None = None, token: str | None = None) -> int:
    req = urllib.request.Request(url, method="POST" if data is not None else "GET")
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            http.last_body = r.read().decode()  # type: ignore[attr-defined]
            return int(r.status)
    except urllib.error.HTTPError as e:
        http.last_body = e.read().decode(errors="replace")  # type: ignore[attr-defined]
        return int(e.code)
    except OSError:
        return 0


def wait_for(url: str, status: int = 200, seconds: int = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if http(url) == status:
            return True
        time.sleep(1)
    return False


def main() -> int:
    keep = "--keep" in sys.argv
    admin = admin_url()
    pw = {r: secrets.token_urlsafe(18) for r in (OWNER, RUNTIME, HX)}

    def db_url(user: str, db: str) -> str:
        return admin.set(
            drivername="postgresql+psycopg", username=user, password=pw[user], database=db
        ).render_as_string(hide_password=False)

    adm = create_engine(admin.set(database="postgres"), isolation_level="AUTOCOMMIT")

    def cleanup() -> None:
        with adm.connect() as c:
            for db in (APP_DB, HX_DB, RESTORE_DB):
                c.execute(text(f"DROP DATABASE IF EXISTS {db} WITH (FORCE)"))
            for role in (RUNTIME, HX, OWNER):
                c.execute(text(f"DROP ROLE IF EXISTS {role}"))

    work = Path(tempfile.mkdtemp(prefix="ppr_drill_"))
    procs: list[subprocess.Popen[bytes]] = []
    try:
        cleanup()
        with adm.connect() as c:
            for role in (OWNER, RUNTIME, HX):
                c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{pw[role]}'"))
            c.execute(text(f"CREATE DATABASE {APP_DB} OWNER {OWNER}"))
            c.execute(text(f"CREATE DATABASE {RESTORE_DB} OWNER {OWNER}"))
            c.execute(text(f"CREATE DATABASE {HX_DB}"))
        hx_admin = create_engine(admin.set(database=HX_DB))
        mockhx_schema.create(hx_admin)
        with hx_admin.begin() as c:
            c.execute(text(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {HX}"))
        hx_admin.dispose()
        queries = work / "hosxp_queries.toml"
        queries.write_text(mockhx_schema.QUERIES_TOML, encoding="utf-8")
        print(f"drill databases {APP_DB}, {HX_DB}, {RESTORE_DB}; work folder {work}")

        env = {
            **os.environ,
            "PYTHONPATH": str(BACKEND / "src"),
            "PPR_PROFILE": "production",
            "PPR_DATABASE_URL": db_url(RUNTIME, APP_DB),
            "PPR_MIGRATION_DATABASE_URL": db_url(OWNER, APP_DB),
            "PPR_RUNTIME_ROLE": RUNTIME,
            "PPR_HOSXP_MODE": "sql",
            "PPR_HOSXP_DATABASE_URL": db_url(HX, HX_DB),
            "PPR_HOSXP_QUERIES": str(queries),
            "PPR_SESSION_SECRET": secrets.token_urlsafe(40),
            "PPR_API_PORT": str(API_PORT),
            "PPR_UI_PORT": str(UI_PORT),
            "PPR_API_URL": f"http://127.0.0.1:{API_PORT}",
            "PPR_LOG_DIR": str(work / "logs"),
            "PPR_RUN_DIR": str(work / "run"),
            "PYTHONIOENCODING": "utf-8",
        }
        env.pop("PPR_UI_COOKIE_SECRET", None)
        py = sys.executable

        def cli(
            *args: str, extra: dict[str, str] | None = None
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [py, "-m", "ppr.cli", *args],
                cwd=BACKEND,
                env={**env, **(extra or {})},
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300,
                check=False,
            )

        # 1. role split ------------------------------------------------------------
        r = cli("migrate")
        check(
            r.returncode == 0 and "granted runtime rights" in r.stdout,
            "migrate as owner, grant runtime role",
        )
        rt = create_engine(env["PPR_DATABASE_URL"])
        with rt.connect() as c:
            issues = runtime_account_issues(c)
        check(issues == [], f"runtime account is least-privilege {issues or ''}")
        denied = 0
        for stmt in (
            "UPDATE audit_log SET action = 'X'",
            "DELETE FROM plan_ledger",
            "TRUNCATE ppr_version",
            "CREATE TABLE intruder (x int)",
            "ALTER TABLE audit_log DISABLE TRIGGER ALL",
        ):
            try:
                with rt.begin() as c:
                    c.execute(text(stmt))
            except DBAPIError as exc:
                denied += "permission denied" in str(exc) or "must be owner" in str(exc)
        rt.dispose()
        check(
            denied == 5,
            f"PostgreSQL refuses the runtime account 5/5 evidence or schema changes ({denied})",
        )

        with adm.connect() as c:  # membership of the owner = the owner's rights
            c.execute(text(f"GRANT {OWNER} TO {RUNTIME}"))
        rt = create_engine(env["PPR_DATABASE_URL"])
        with rt.connect() as c:
            member = runtime_account_issues(c)
        rt.dispose()
        with adm.connect() as c:
            c.execute(text(f"REVOKE {OWNER} FROM {RUNTIME}"))
        check(
            any("member of" in i for i in member),
            "a runtime account that is a member of the owner role is not least-privilege",
        )

        # 2. production refusals ------------------------------------------------------
        def refused(args: list[str], extra: dict[str, str] | None, word: str) -> bool:
            p = subprocess.run(
                [py, "-m", "ppr.cli", "serve", "--port", str(API_PORT + 50), *args],
                cwd=BACKEND,
                env={**env, **(extra or {})},
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            return p.returncode == 2 and word in p.stdout

        check(
            refused([], {"PPR_DATABASE_URL": env["PPR_MIGRATION_DATABASE_URL"]}, "least-privilege"),
            "API refuses to start with the schema owner account",
        )
        check(
            refused(["--host", "0.0.0.0"], None, "this machine only"),
            "API refuses a non-local address",
        )
        check(
            refused(["--demo"], None, "demo mode is not allowed"),
            "API refuses demo mode in production",
        )

        # 3. API under the supervisor -------------------------------------------------
        sup = [py, str(REPO / "tools" / "ops" / "supervise.py")]
        api = subprocess.Popen([*sup, "api"], cwd=REPO, env=env)
        procs.append(api)
        health = f"http://127.0.0.1:{API_PORT}/api/health"
        check(
            wait_for(health, 200, 90), "API up under the supervisor (production profile, SQL HOSxP)"
        )
        status = http(
            f"http://127.0.0.1:{API_PORT}/api/auth/login",
            {"username": "mock_ent", "password": "Ent-Pass-1"},
        )
        token = json.loads(http.last_body).get("token") if status == 200 else None  # type: ignore[attr-defined]
        check(status == 200 and bool(token), "a user signs in with the HOSxP password check")
        r = cli("grant-role", "--username", "mock_ent", "--role", "PLAN_OFFICER")
        check(r.returncode == 0, "bootstrap a Plan Officer (runtime account)")
        status = http(f"http://127.0.0.1:{API_PORT}/api/sync/masters", {}, token)
        check(status == 200, f"masters synchronize from the read-only HOSxP stand-in ({status})")

        # 4. restart after a crash ----------------------------------------------------
        log_text = lambda: "".join(  # noqa: E731
            p.read_text(encoding="utf-8") for p in (work / "logs").glob("api-*.log")
        )
        pids = re.findall(r"started api \(pid (\d+)", log_text())
        if pids:
            os.kill(int(pids[-1]), signal.SIGTERM)
        time.sleep(3)
        back = wait_for(health, 200, 90)
        check(
            back and "start #2" in log_text() and "exited with code" in log_text(),
            "API killed -> restarted by the supervisor (logged)",
        )

        # 5. nightly job and ops-status ------------------------------------------------
        r = cli("ops-status")
        check(
            r.returncode == 1 and "no successful run yet" in r.stdout,
            "ops-status fails before any nightly run",
        )
        nightly = subprocess.run([*sup, "nightly"], cwd=REPO, env=env, timeout=300, check=False)
        check(nightly.returncode == 0, "nightly PR synchronization through the supervisor")
        r = cli("ops-status")
        check(
            r.returncode == 0 and "PASS  runtime account least-privilege" in r.stdout,
            "ops-status passes (schema, least-privilege runtime account, fresh nightly run)",
        )
        print(r.stdout.strip())
        # Wave 10B-2 (D-40): the stand-in HOSxP's masters are MOCK-*: a warning, not a failure.
        check(
            "WARN  MOCK/DEMO data" in r.stdout and "ops-status: PASS (with warnings)" in r.stdout,
            "ops-status warns about the MOCK masters of the stand-in, without failing (D-40)",
        )
        r = cli(
            "first-start",
            "--check",
            extra={
                "PPR_UI_SECURE_COOKIES": "1",
                "PPR_UI_TRUST_PROXY": "loopback",
                "PPR_UI_HOST": "127.0.0.1",
                "PPR_BACKUP_DIR": str(work / "backups"),
            },
        )
        fails = [x for x in r.stdout.splitlines() if x.startswith("FAIL")]
        check(
            r.returncode == 0
            and not fails
            and "WARN  [MOCK/DEMO (D-40)]" in r.stdout
            and "OPEN  [G-4]" in r.stdout
            and "(not PASS)" in r.stdout,
            f"first-start --check: no FAIL on the drilled server, MOCK WARN, OPEN items {fails}",
        )

        # Wave 10B-2: hosxp-verify on the read-only stand-in account, then with a write right.
        r = cli("hosxp-verify", "--pr", "MOCK-SQL-0001")
        check(
            "PASS  [account] no right beyond reading" in r.stdout
            and r.returncode == 0
            and "(not PASS)" in r.stdout,
            "hosxp-verify: the read-only HOSxP account passes; OPEN items stay not PASS",
        )
        hx_admin = create_engine(admin.set(database=HX_DB))
        with hx_admin.begin() as c:
            c.execute(
                text("CREATE VIEW mockhx_dept_v AS SELECT dep_code, dep_name FROM mockhx_dept")
            )
            c.execute(text(f"GRANT SELECT, INSERT ON mockhx_dept_v TO {HX}"))
        r = cli("hosxp-verify")
        check(
            r.returncode == 1 and "FAIL  [account]" in r.stdout and "view(s)" in r.stdout,
            "hosxp-verify: an INSERT right through a view fails the account check",
        )
        with hx_admin.begin() as c:
            c.execute(text("DROP VIEW mockhx_dept_v"))
        hx_admin.dispose()

        # 6. the user interface ----------------------------------------------------------
        node = os.environ.get("PPR_NODE", "node")
        bad = subprocess.run(
            [node, str(REPO / "frontend" / "src" / "server.js")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        check(
            bad.returncode == 2 and "PPR_UI_SECURE_COOKIES" in bad.stderr,
            "user interface refuses production without HTTPS settings",
        )
        ui_env = {**env, "PPR_UI_SECURE_COOKIES": "1", "PPR_UI_TRUST_PROXY": "loopback"}
        ui = subprocess.Popen([*sup, "ui"], cwd=REPO, env=ui_env)
        procs.append(ui)
        deep = f"http://127.0.0.1:{UI_PORT}/healthz?deep=1"
        check(
            wait_for(deep, 200, 60),
            "user interface up (secure cookies, trusted proxy) and reaches the API",
        )
        subprocess.run([*sup, "--stop", "api"], cwd=REPO, env=env, check=False)
        api.wait(60)
        check(wait_for(deep, 503, 30), "deep health check reports the API down (503)")
        check(
            http(f"http://127.0.0.1:{UI_PORT}/healthz") == 200, "the interface itself still answers"
        )
        subprocess.run([*sup, "--stop", "ui"], cwd=REPO, env=env, check=False)
        ui.wait(60)

        # 7. backup / restore ------------------------------------------------------------
        r = cli("backup", "--out", str(work / "backup"))
        check(
            r.returncode == 0 and "manifest" in r.stdout, "backup (consistent snapshot + manifest)"
        )
        print(r.stdout.strip())
        dumps = sorted((work / "backup").glob("*.dump"))
        manifests = sorted((work / "backup").glob("*.manifest.json"))
        if dumps and manifests:
            r = cli("restore", "--dump", str(dumps[-1]), "--target-db", APP_DB, "--replace")
            check(
                r.returncode == 2 and "runs on" in r.stdout,
                "restore over the live database is refused",
            )
            alias = make_url(env["PPR_MIGRATION_DATABASE_URL"]).set(host="127.0.0.1")
            r = cli(
                "restore",
                "--dump",
                str(dumps[-1]),
                "--target-db",
                APP_DB,
                extra={"PPR_MIGRATION_DATABASE_URL": alias.render_as_string(hide_password=False)},
            )
            check(
                r.returncode == 2 and "runs on" in r.stdout,
                "the same database under another host name (127.0.0.1) is recognised and refused",
            )
            r = cli("restore", "--dump", str(dumps[-1]), "--target-db", RESTORE_DB)
            check(r.returncode == 0, "restore into another, empty database")
            r = cli(
                "verify-restore",
                "--manifest",
                str(manifests[-1]),
                "--target-db",
                RESTORE_DB,
            )
            check(
                r.returncode == 0 and "PASS" in r.stdout,
                "restored copy is identical (every table, sequence, trigger)",
            )
            print(r.stdout.strip())
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    p.kill()
        if keep:
            print(f"--keep: drill databases and roles left in place; work folder {work}")
        else:
            cleanup()
            print("drill databases and roles dropped")
        adm.dispose()

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
