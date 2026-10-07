"""``SqlHosxpGateway``: the real ``HosxpGateway``, reading HOSxP through read-only SQL.

The code is generic: it runs the site's configured queries (``query_config``) and maps
their columns onto the normalized contracts. It knows no HOSxP table or column names.

Guarantees:
* Read-only three times over: the query text is checked when loaded, each query runs in
  a READ ONLY transaction, and the database account must itself be read-only.
* Every call reads live data; nothing is cached (spec §25.1).
* Connection problems raise ``HosxpUnavailableError``; a query that fails or returns the
  wrong columns raises ``HosxpConfigurationError`` (a subclass), so callers refuse to act
  in both cases (spec §26) instead of using partial or guessed data.
* Error messages carry column names and error types, never row values.

Supported databases: MySQL / MariaDB (driver ``pymysql``) and PostgreSQL (``psycopg``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from ppr.integration.hosxp.contracts import (
    BudgetCategory,
    BudgetSnapshot,
    Department,
    FundSource,
    Item,
    PrHeader,
    PrItem,
    StockMovement,
    StockMovementSource,
)
from ppr.integration.hosxp.errors import HosxpConfigurationError, HosxpUnavailableError
from ppr.integration.hosxp.real.query_config import QueryConfig

M = TypeVar("M", bound=BaseModel)
SUPPORTED_DIALECTS = ("mysql", "mariadb", "postgresql")
DEFAULT_TIMEOUT_SECONDS = 20


class SqlHosxpGateway:
    def __init__(
        self,
        engine: Engine,
        config: QueryConfig,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if engine.dialect.name not in SUPPORTED_DIALECTS:
            raise HosxpConfigurationError(
                f"unsupported HOSxP database {engine.dialect.name!r}; "
                f"supported: {', '.join(SUPPORTED_DIALECTS)}"
            )
        self.engine = engine
        self.config = config
        self.timeout_seconds = timeout_seconds

    # ------------------------------------------------------------------ plumbing
    def _begin_read_only(self, conn: Connection) -> None:
        d = self.engine.dialect
        if d.name == "postgresql":
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {self.timeout_seconds * 1000}")
        else:  # MySQL / MariaDB: session setting applies to the next transaction
            conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
            if getattr(d, "is_mariadb", False):
                conn.exec_driver_sql(f"SET SESSION max_statement_time = {self.timeout_seconds}")
            else:
                conn.exec_driver_sql(
                    f"SET SESSION max_execution_time = {self.timeout_seconds * 1000}"
                )
            conn.commit()  # end the implicit transaction; the next one is READ ONLY

    def _rows(self, operation: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        q = self.config.query(operation)
        try:
            with self.engine.connect() as conn:
                self._begin_read_only(conn)
                result = conn.execute(text(q.sql), dict(params))
                columns = list(result.keys())
                rows = [dict(zip(columns, r, strict=True)) for r in result]
                conn.rollback()  # nothing to keep: the transaction was read-only anyway
        except (OperationalError, InterfaceError) as exc:
            if isinstance(exc, OperationalError) and _is_query_error(exc):
                raise HosxpConfigurationError(
                    f"HOSxP query '{operation}' failed: {type(exc.orig).__name__}"
                ) from None
            raise HosxpUnavailableError("HOSxP database is unavailable") from None
        except DBAPIError as exc:
            raise HosxpConfigurationError(
                f"HOSxP query '{operation}' failed: {type(exc.orig).__name__}"
            ) from None
        self._check_columns(operation, columns)
        return rows

    def rows(self, operation: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Run one configured query read-only (used by the SQL authentication adapter)."""
        return self._rows(operation, params)

    def build(self, operation: str, model: type[M], row: Mapping[str, Any]) -> M:
        return self._build(operation, model, row)

    def _check_columns(self, operation: str, columns: Sequence[str]) -> None:
        op = self.config.query(operation).operation
        got = set(columns)
        if len(got) != len(columns):
            raise HosxpConfigurationError(f"{operation}: duplicate column names")
        if missing := op.required_columns - got:
            raise HosxpConfigurationError(f"{operation}: missing column(s) {sorted(missing)}")
        if unknown := got - op.allowed_columns:
            raise HosxpConfigurationError(
                f"{operation}: unexpected column(s) {sorted(unknown)}; "
                f"allowed: {sorted(op.allowed_columns)}"
            )

    @staticmethod
    def _build(operation: str, model: type[M], row: Mapping[str, Any]) -> M:
        try:
            return model.model_validate(dict(row))
        except ValidationError as exc:
            fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
            raise HosxpConfigurationError(
                f"{operation}: value(s) in column(s) {fields} do not fit the contract"
            ) from None

    def _list(self, operation: str, model: type[M], **params: Any) -> tuple[M, ...]:
        return tuple(self._build(operation, model, r) for r in self._rows(operation, params))

    # ------------------------------------------------------------------ HosxpGateway
    def get_pr(self, pr_no: str) -> PrHeader | None:
        rows = self._rows("get_pr", {"pr_no": pr_no})
        if not rows:
            return None
        if len(rows) > 1:
            raise HosxpConfigurationError(
                f"get_pr returned {len(rows)} rows for one PR number; it must return one"
            )
        row = dict(rows[0])
        native = row.pop("native_status", None)
        native_text = None if native is None else str(native)
        row["native_status"] = native_text
        row["status"] = self.config.status_mapping.normalize(native_text)
        return self._build("get_pr", PrHeader, row)

    def get_pr_items(self, pr_no: str) -> Sequence[PrItem]:
        return self._list("get_pr_items", PrItem, pr_no=pr_no)

    def get_departments(self) -> Sequence[Department]:
        return self._list("get_departments", Department)

    def get_items(self) -> Sequence[Item]:
        return self._list("get_items", Item)

    def get_fund_sources(self) -> Sequence[FundSource]:
        return self._list("get_fund_sources", FundSource)

    def get_budget_categories(self) -> Sequence[BudgetCategory]:
        return self._list("get_budget_categories", BudgetCategory)

    def get_budget_snapshot(
        self, fund_source_id: str, budget_category_id: str
    ) -> BudgetSnapshot | None:
        found = self._list(
            "get_budget_snapshot",
            BudgetSnapshot,
            fund_source_id=fund_source_id,
            budget_category_id=budget_category_id,
        )
        return found[0] if found else None

    def stock_movement_source(self) -> StockMovementSource:
        return self.config.stock

    def get_stock_movements(self, date_from: date, date_to: date) -> Sequence[StockMovement]:
        """D-39: only with a verified query (``QueryConfig.query`` refuses otherwise)."""
        if not self.config.stock.usable:
            raise HosxpConfigurationError(f"stock movements: {self.config.stock.problem}")
        return self._list(
            "get_stock_movements", StockMovement, date_from=date_from, date_to=date_to
        )


def _is_query_error(exc: OperationalError) -> bool:
    """MySQL reports some query errors (e.g. timeouts, read-only violations) as
    OperationalError; a lost/failed connection is 'unavailable', anything else is a
    query/configuration problem."""
    orig = exc.orig
    code = orig.args[0] if orig is not None and orig.args else None
    connection_codes = {2002, 2003, 2006, 2013, 2055, 1040, 1045, 1049}
    if isinstance(code, int):
        return code not in connection_codes
    return False
