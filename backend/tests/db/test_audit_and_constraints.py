"""Append-only audit log (spec §36) and schema-level constraints (spec §22, §23, §25.4, §30)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import Engine, insert, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from ppr.infrastructure.db.repositories import SqlAuditWriter, unit_of_work
from ppr.infrastructure.db.schema import app_user, plan_year, role_appointment, sync_run
from ppr.ports import SYSTEM_ACTOR, Actor, AuditEntry


def _user(engine: Engine, source: str = "MOCK-U1") -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                insert(app_user)
                .values(hosxp_source_id=source, username=source)
                .returning(app_user.c.id)
            ).scalar_one()
        )


def _audit_one(engine: Engine) -> None:
    with unit_of_work(engine) as uow:
        uow.audit.write(AuditEntry(SYSTEM_ACTOR, "TEST", "thing", "1", after={"x": 1}))


# ------------------------------------------------------------------ audit
@pytest.mark.spec("S-36")
@pytest.mark.parametrize(
    "sql",
    ["UPDATE audit_log SET action = 'TAMPERED'", "DELETE FROM audit_log", "TRUNCATE audit_log"],
)
def test_audit_log_rejects_update_delete_truncate(db: Engine, sql: str) -> None:
    _audit_one(db)
    with pytest.raises(DBAPIError, match="append-only"), db.begin() as conn:
        conn.execute(text(sql))
    with db.connect() as conn:
        assert conn.execute(text("SELECT action FROM audit_log")).scalar_one() == "TEST"


@pytest.mark.spec("S-36")
def test_audit_writer_has_no_mutation_methods() -> None:
    public = {n for n in dir(SqlAuditWriter) if not n.startswith("_")}
    # for_entity (Wave 5B) and search (Wave 8B audit report) are read-only.
    assert public == {"write", "recent", "for_entity", "search"}


@pytest.mark.spec("S-36")
def test_audit_records_who_what_when_before_after_reason(db: Engine) -> None:
    uid = _user(db)
    with unit_of_work(db) as uow:
        uow.audit.write(
            AuditEntry(Actor(uid), "DID", "thing", "7", {"a": 1}, {"a": 2}, reason="why")
        )
        [rec] = uow.audit.recent(10)
    assert (rec.actor_kind, rec.actor_user_id, rec.action, rec.entity_type, rec.entity_id) == (
        "USER",
        uid,
        "DID",
        "thing",
        "7",
    )
    assert (rec.before, rec.after, rec.reason) == ({"a": 1}, {"a": 2}, "why")
    assert rec.occurred_at is not None


def test_audit_actor_kind_must_match_user(db: Engine) -> None:
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_log(actor_kind, actor_user_id, action, entity_type, entity_id)"
                " VALUES ('USER', NULL, 'X', 'y', '1')"
            )
        )


# ------------------------------------------------------------------ plan_year
@pytest.mark.spec("S-7")
def test_plan_year_constraints(db: Engine) -> None:
    ok = dict(fiscal_year=2570, starts_on=date(2026, 10, 1), ends_on=date(2027, 9, 30))
    with db.begin() as conn:
        conn.execute(insert(plan_year).values(**ok))
    bad_cases = [
        ok,  # duplicate fiscal year
        {**ok, "fiscal_year": 2571, "state": "OPEN"},  # unknown state
        {**ok, "fiscal_year": 257},  # not 4 digits
        {**ok, "fiscal_year": 2572, "ends_on": date(2026, 1, 1)},  # ends before start
    ]
    for values in bad_cases:
        with pytest.raises(IntegrityError), db.begin() as conn:
            conn.execute(insert(plan_year).values(**values))


# ------------------------------------------------------------------ roles / appointment
def test_unknown_role_cannot_be_granted(db: Engine) -> None:
    uid = _user(db)
    with pytest.raises(IntegrityError), db.begin() as conn:
        conn.execute(text(f"INSERT INTO user_role(user_id, role_code) VALUES ({uid}, 'ROOT')"))


@pytest.mark.spec("S-23")
def test_only_head_of_procurement_is_appointable_with_valid_range(db: Engine) -> None:
    uid = _user(db)
    base = dict(
        user_id=uid,
        role_code="HEAD_OF_PROCUREMENT",
        fiscal_year=2570,
        effective_from=date(2026, 10, 1),
    )
    with db.begin() as conn:
        conn.execute(insert(role_appointment).values(**base))
    for values in (
        {**base, "role_code": "ADMIN"},
        {**base, "effective_to": date(2026, 9, 30)},
        {**base, "status": "PENDING"},
    ):
        with pytest.raises(IntegrityError), db.begin() as conn:
            conn.execute(insert(role_appointment).values(**values))


# ------------------------------------------------------------------ sync log
@pytest.mark.spec("S-25.4")
def test_sync_run_log_rules(db: Engine) -> None:
    uid = _user(db)
    with db.begin() as conn:
        conn.execute(insert(sync_run).values(mode="NIGHTLY", scope="PR_STATUS"))
        conn.execute(insert(sync_run).values(mode="MANUAL", scope="PR", requested_by_user_id=uid))
    for values in (
        {"mode": "MANUAL", "scope": "PR"},  # manual run must record who requested it
        {"mode": "HOURLY", "scope": "PR"},
        {"mode": "NIGHTLY", "scope": "PR", "records_checked": -1},
        {"mode": "NIGHTLY", "scope": "PR", "status": "DONE"},
    ):
        with pytest.raises(IntegrityError), db.begin() as conn:
            conn.execute(insert(sync_run).values(**values))
