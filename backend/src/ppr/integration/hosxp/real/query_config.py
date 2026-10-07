"""Site query configuration for the SQL HOSxP adapter.

The adapter's code contains NO HOSxP table or column names (spec §47: they are not
guessed). Each hospital supplies a TOML file with one read-only query per gateway
operation, written after inspecting its own HOSxP schema. Every query must return
columns named exactly after this system's contract fields (``AS pr_no`` ...).

Format::

    [status_map]                 # native HOSxP PR status -> normalized status (§24.5)
    "<native value>" = "CANCELLED"

    [queries.get_pr]
    sql = '''SELECT ... AS pr_id, ... AS pr_no, ... WHERE ... = :pr_no'''

    [stock_movement]             # optional (D-39): central-store issues / returns, report only
    sql = '''SELECT ... AS movement_id, ... WHERE ... BETWEEN :date_from AND :date_to'''
    verified_by = "<who in IT checked the query against HOSxP>"
    verified_on = 2026-10-15      # a TOML date
    reference = "<where the check is recorded>"
    [stock_movement.kind_map]    # native HOSxP movement kind -> ISSUE / RETURN
    "<native kind>" = "ISSUE"

Safety rules enforced when the file is loaded (before any connection is made):
* one statement only, starting with SELECT or WITH;
* no data-changing or session-changing keywords anywhere in the text;
* named parameters must be exactly the operation's parameters.
The database account must additionally be read-only, and every query runs inside a
read-only transaction (see ``sql_gateway``).

``[stock_movement]`` is kept apart from ``[queries]`` (D-39): its query is used only with a
verification record and a kind mapping, and any problem in that section (missing record,
unsafe or wrong query, unknown kind) only makes the stock figures unavailable - it never
stops the rest of the file, which the PR features depend on, from loading.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from ppr.integration.hosxp.contracts import (
    BudgetCategory,
    BudgetSnapshot,
    Department,
    FundSource,
    HosxpUserProfile,
    Item,
    PrHeader,
    PrItem,
    StockMovement,
    StockMovementKind,
    StockMovementSource,
    StockVerification,
)
from ppr.integration.hosxp.errors import HosxpConfigurationError
from ppr.integration.hosxp.status import HosxpPrStatus, PrStatusMapping


class LoginRow(BaseModel):
    """What the site's ``get_login_user`` query returns (D-25). Adapter-internal: the
    password hash never leaves ``sql_auth`` and is never logged or stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    username: str
    password_hash: str
    display_name: str | None = None
    department_id: str | None = None
    active: bool = True


@dataclass(frozen=True)
class Operation:
    """A gateway operation: its query parameters and the columns it may/must return."""

    name: str
    params: frozenset[str]
    required_columns: frozenset[str]
    allowed_columns: frozenset[str]
    needed_now: bool  # False = contract exists but no current feature calls it


def _columns(model: type, *, drop: set[str] = frozenset(), add: set[str] = frozenset()):  # type: ignore[no-untyped-def,assignment]
    fields = model.model_fields  # type: ignore[attr-defined]
    allowed = (set(fields) - set(drop)) | set(add)
    required = {n for n, f in fields.items() if f.is_required()} - set(drop)
    return frozenset(required), frozenset(allowed)


def _op(name: str, params: set[str], model: type, needed_now: bool, **kw: set[str]) -> Operation:
    req, allowed = _columns(model, **kw)
    return Operation(name, frozenset(params), req, allowed, needed_now)


# The PR header query returns the NATIVE status; the adapter normalizes it (§24.5).
OPERATIONS: Mapping[str, Operation] = MappingProxyType(
    {
        o.name: o
        for o in (
            _op("get_pr", {"pr_no"}, PrHeader, True, drop={"status"}),
            _op("get_pr_items", {"pr_no"}, PrItem, True),
            _op("get_departments", set(), Department, True),
            _op("get_items", set(), Item, True),
            _op("get_fund_sources", set(), FundSource, True),
            _op("get_budget_categories", set(), BudgetCategory, True),
            _op(
                "get_budget_snapshot",
                {"fund_source_id", "budget_category_id"},
                BudgetSnapshot,
                False,
            ),
            # D-39: optional, report only; configured in [stock_movement], not [queries].
            _op("get_stock_movements", {"date_from", "date_to"}, StockMovement, False),
            # Login (D-25): the user row with its stored MD5 password hash, and the profile.
            _op("get_login_user", {"username"}, LoginRow, True),
            _op("get_user_profile", {"source_id"}, HosxpUserProfile, True),
        )
    }
)

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|UPSERT|DROP|ALTER|CREATE|TRUNCATE|RENAME|GRANT|"
    r"REVOKE|CALL|EXEC|EXECUTE|DO|LOCK|UNLOCK|SET|COPY|LOAD|HANDLER|INTO|OUTFILE|DUMPFILE|"
    r"COMMIT|ROLLBACK|SAVEPOINT|BEGIN|START|PREPARE|DEALLOCATE|VACUUM|ANALYZE|REFRESH|"
    r"LISTEN|NOTIFY|SLEEP|BENCHMARK|PG_SLEEP|FOR\s+UPDATE|FOR\s+SHARE)\b",
    re.IGNORECASE,
)
_PARAM = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


@dataclass(frozen=True)
class Query:
    operation: Operation
    sql: str


@dataclass(frozen=True)
class QueryConfig:
    queries: Mapping[str, Query]
    status_mapping: PrStatusMapping = field(default_factory=PrStatusMapping)
    stock: StockMovementSource = field(default_factory=StockMovementSource)

    def query(self, operation: str) -> Query:
        q = self.queries.get(operation)
        if operation == STOCK_OPERATION and not self.stock.usable:
            q = None  # D-39: never run without its verification record
        if q is None:
            raise HosxpConfigurationError(
                f"HOSxP query '{operation}' is not configured for this site"
            )
        return q

    def missing_needed(self) -> list[str]:
        """Operations the current features need but the site has not configured."""
        return sorted(n for n, o in OPERATIONS.items() if o.needed_now and n not in self.queries)


def check_sql(operation: Operation, sql: str) -> str:
    """Validate one query against the safety rules; return the normalized SQL."""
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    name = operation.name
    if not text:
        raise HosxpConfigurationError(f"{name}: empty query")
    if ";" in text:
        raise HosxpConfigurationError(f"{name}: only one statement is allowed")
    first = text.split(None, 1)[0].upper()
    if first not in ("SELECT", "WITH"):
        raise HosxpConfigurationError(f"{name}: a query must start with SELECT or WITH")
    if m := _FORBIDDEN.search(text):
        raise HosxpConfigurationError(
            f"{name}: keyword {m.group(1).upper()!r} is not allowed in a read-only query"
        )
    used = set(_PARAM.findall(text))
    if used != operation.params:
        raise HosxpConfigurationError(
            f"{name}: parameters must be exactly {sorted(operation.params)}, got {sorted(used)}"
        )
    return text


STOCK_OPERATION = "get_stock_movements"
_STOCK_KEYS = {"sql", "verified_by", "verified_on", "reference", "kind_map"}


def _bad_stock(reason: str) -> StockMovementSource:
    return StockMovementSource(problem="การตั้งค่า query ยอดเบิก/คืนไม่ผ่านการตรวจสอบ: " + reason)


def parse_stock(raw: object) -> tuple[StockMovementSource, Query | None]:
    """The optional ``[stock_movement]`` section (D-39). Never raises: a problem makes the
    stock figures unavailable, with the reason, and nothing else."""
    if raw is None:
        return StockMovementSource(), None
    if not isinstance(raw, dict):
        return _bad_stock("[stock_movement] must be a table"), None
    if unknown := set(raw) - _STOCK_KEYS:
        return _bad_stock(f"unknown key(s) {sorted(unknown)}"), None
    sql = raw.get("sql")
    if not isinstance(sql, str):
        return _bad_stock("sql = '...' is required"), None
    try:
        query = Query(OPERATIONS[STOCK_OPERATION], check_sql(OPERATIONS[STOCK_OPERATION], sql))
    except HosxpConfigurationError as exc:
        return _bad_stock(str(exc)), None
    by, on, ref = raw.get("verified_by"), raw.get("verified_on"), raw.get("reference")
    if not (isinstance(by, str) and by.strip() and isinstance(ref, str) and ref.strip()):
        return StockMovementSource(
            problem="query ยอดเบิก/คืนยังไม่ได้รับการยืนยัน (ไม่มี verified_by / reference)"
        ), None
    if not isinstance(on, date) or hasattr(on, "hour"):
        return _bad_stock("verified_on must be a TOML date (YYYY-MM-DD)"), None
    kinds = raw.get("kind_map")
    if not isinstance(kinds, dict) or not kinds:
        return _bad_stock("[stock_movement.kind_map] must map at least one native kind"), None
    mapping: dict[str, StockMovementKind] = {}
    for native, kind in kinds.items():
        try:
            mapping[str(native)] = StockMovementKind(str(kind).upper())
        except ValueError:
            return _bad_stock(f"kind_map: {kind!r} is not ISSUE or RETURN"), None
    if StockMovementKind.ISSUE not in mapping.values():
        return _bad_stock("kind_map has no ISSUE kind"), None
    source = StockMovementSource(
        problem=None,
        verification=StockVerification(by.strip(), on, ref.strip()),
        kind_map=MappingProxyType(mapping),
    )
    return source, query


def parse_config(data: Mapping[str, object]) -> QueryConfig:
    unknown_top = set(data) - {"queries", "status_map", "stock_movement"}
    if unknown_top:
        raise HosxpConfigurationError(f"unknown section(s): {sorted(unknown_top)}")
    raw_queries = data.get("queries", {})
    if not isinstance(raw_queries, dict):
        raise HosxpConfigurationError("[queries] must be a table")
    queries: dict[str, Query] = {}
    stock, stock_query = parse_stock(data.get("stock_movement"))
    if STOCK_OPERATION in raw_queries:
        stock, stock_query = (
            _bad_stock(f"{STOCK_OPERATION} belongs in [stock_movement], with its verification"),
            None,
        )
    if stock_query is not None:
        queries[STOCK_OPERATION] = stock_query
    for name, body in raw_queries.items():
        if name == STOCK_OPERATION:
            continue
        op = OPERATIONS.get(name)
        if op is None:
            raise HosxpConfigurationError(
                f"unknown operation {name!r}; known: {sorted(OPERATIONS)}"
            )
        if not isinstance(body, dict) or set(body) != {"sql"} or not isinstance(body["sql"], str):
            raise HosxpConfigurationError(f"[queries.{name}] must contain exactly: sql = '...'")
        queries[name] = Query(op, check_sql(op, body["sql"]))

    raw_status = data.get("status_map", {})
    if not isinstance(raw_status, dict):
        raise HosxpConfigurationError("[status_map] must be a table")
    mapping: dict[str, HosxpPrStatus] = {}
    for native, normalized in raw_status.items():
        try:
            mapping[str(native)] = HosxpPrStatus(str(normalized).upper())
        except ValueError:
            raise HosxpConfigurationError(
                f"status_map: {normalized!r} is not one of {[s.value for s in HosxpPrStatus]}"
            ) from None
    return QueryConfig(MappingProxyType(queries), PrStatusMapping(MappingProxyType(mapping)), stock)


def load_config(path: Path) -> QueryConfig:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise HosxpConfigurationError(f"HOSxP query file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise HosxpConfigurationError(f"HOSxP query file is not valid TOML: {exc}") from None
    return parse_config(data)
