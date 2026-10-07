"""Wave 10A: backup and restore end to end (spec §38.3) and the runtime role split (§39).

The backup/restore test dumps the test database holding real activity (plan, confirmed and
verified PPRs, Assigned Purchase coverage, demand states, audit), damages the live copy, restores
the dump over it and checks the copy is exact - every table's rows, the sequences, the evidence
triggers - and that the system goes on working (the next PPR number follows the last one).

The role-split test needs a database account allowed to create roles (CI's is); elsewhere it is
skipped and tools/dev/drill_production.py covers it with a superuser.
"""

from __future__ import annotations

import secrets
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError, ProgrammingError
from test_assigned_api import ENT, WARD, Assigned

from api_harness import Harness
from ppr import ops_backup
from ppr.config.settings import SettingsError
from ppr.infrastructure.db.privileges import grant_runtime, runtime_account_issues


@pytest.mark.spec("S-38.3", "S-36", "S-38.2")
def test_a_backup_restores_to_an_exact_working_copy(
    db: Engine, db_url: str, tmp_path: Path
) -> None:
    a = Assigned(Harness(db))
    d = a.draft("PR-BK1", "30")
    assert a.confirm(d["id"]).status_code == 200
    ppr = a.hx.client.get(f"/api/pprs/{d['id']}", headers=a.store).json()
    assert a.verify(ppr).status_code == 200

    res = ops_backup.backup(db_url, tmp_path)
    assert res.dump.stat().st_size > 0 and res.manifest_file.is_file()
    m = res.manifest
    assert m["tables"]["plan_ledger"]["rows"] > 0 and m["tables"]["audit_log"]["rows"] > 0
    secret = make_url(db_url).password
    assert not secret or secret not in res.manifest_file.read_text(encoding="utf-8")

    # Damage the live database, as a failure would: the manifest tells the difference.
    with db.begin() as c:
        c.execute(text("DELETE FROM ppr_coverage"))
        c.execute(text("UPDATE plan_item_demand SET state = 'PLANNED'"))
    assert ops_backup.verify(res.manifest_file, db_url)

    ops_backup.restore(res.dump, db_url, source_url=None, replace=True)
    assert ops_backup.verify(res.manifest_file, db_url) == []
    assert a.states() == {ENT: "FULFILLED", WARD: "FULFILLED"}
    # The restored system works on: ENT's own PPR continues the numbering.
    a.hx.put_pr("PR-BK2", ("MOCK-ITEM-TONER", "1", "100"), dept=ENT)
    r = a.hx.create_ppr("PR-BK2", a.ent)
    assert r.status_code == 201, r.text
    r = a.hx.client.post(f"/api/pprs/{r.json()['id']}/confirm", headers=a.ent)
    assert r.status_code == 200, r.text
    assert r.json()["ppr_number"].endswith("000002")


@pytest.mark.spec("S-38.3")
def test_restore_refuses_a_damaged_dump_or_a_non_empty_database(
    db: Engine, db_url: str, tmp_path: Path
) -> None:
    Assigned(Harness(db))
    res = ops_backup.backup(db_url, tmp_path)
    with pytest.raises(SettingsError, match="not empty"):
        ops_backup.restore(res.dump, db_url, source_url=None, replace=False)
    with pytest.raises(SettingsError, match="runs on"):
        ops_backup.restore(res.dump, db_url, source_url=db_url, replace=True)
    data = bytearray(res.dump.read_bytes())
    data[len(data) // 2] ^= 0xFF
    res.dump.write_bytes(bytes(data))
    with pytest.raises(SettingsError, match="does not match its manifest"):
        ops_backup.restore(res.dump, db_url, source_url=None, replace=True)


def _can_create_roles(db: Engine) -> bool:
    with db.connect() as c:
        return bool(
            c.execute(
                text("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
            ).scalar_one()
        )


@pytest.mark.spec("S-39", "S-36")
def test_the_runtime_role_cannot_change_evidence_or_the_schema(db: Engine, db_url: str) -> None:
    if not _can_create_roles(db):
        pytest.skip("the test database account may not create roles (see drill_production.py)")
    role, pw = "ppr_rt_" + secrets.token_hex(4), secrets.token_hex(16)
    with db.begin() as c:
        c.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{pw}'"))
        grant_runtime(c, role)
    url = make_url(db_url).set(username=role, password=pw)
    rt = create_engine(url)
    try:
        with rt.connect() as c:
            assert runtime_account_issues(c) == []
        # Membership of the schema owner counts as the owner's rights (SET ROLE).
        with db.begin() as c:
            owner = c.execute(text("SELECT current_user")).scalar_one()
            c.execute(text(f'GRANT "{owner}" TO {role}'))
        with rt.connect() as c:
            assert any("member of" in i for i in runtime_account_issues(c))
        with db.begin() as c:
            c.execute(text(f'REVOKE "{owner}" FROM {role}'))
        with rt.begin() as c:
            c.execute(text("SELECT count(*) FROM audit_log"))  # may read
        for stmt in (
            "UPDATE audit_log SET action = 'X'",
            "DELETE FROM plan_ledger",
            "TRUNCATE ppr_version",
            "CREATE TABLE intruder (x int)",
            "ALTER TABLE audit_log DISABLE TRIGGER ALL",
        ):
            with pytest.raises((ProgrammingError, DBAPIError)) as exc, rt.begin() as c:
                c.execute(text(stmt))
            assert "permission denied" in str(exc.value) or "must be owner" in str(exc.value)
    finally:
        rt.dispose()
        with db.begin() as c:
            c.execute(text(f"DROP OWNED BY {role}"))
            c.execute(text(f"DROP ROLE {role}"))
