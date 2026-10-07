"""Site query file: loading and the read-only safety rules (no database needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mockhx_schema import QUERIES_TOML
from ppr.integration.hosxp.errors import HosxpConfigurationError
from ppr.integration.hosxp.real.query_config import OPERATIONS, check_sql, load_config
from ppr.integration.hosxp.status import HosxpPrStatus

GET_PR = OPERATIONS["get_pr"]


def _load(tmp_path: Path, body: str) -> object:
    f = tmp_path / "q.toml"
    f.write_text(body, encoding="utf-8")
    return load_config(f)


def test_valid_site_file_loads(tmp_path: Path) -> None:
    cfg = _load(tmp_path, QUERIES_TOML)
    assert cfg.missing_needed() == []  # type: ignore[attr-defined]
    m = cfg.status_mapping  # type: ignore[attr-defined]
    assert m.normalize("X9") is HosxpPrStatus.CANCELLED
    assert m.normalize("??") is HosxpPrStatus.UNKNOWN


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE t SET a = 1 WHERE b = :pr_no",
        "DELETE FROM t WHERE b = :pr_no",
        "SELECT 1 WHERE 1 = :pr_no; DELETE FROM t",
        "SELECT a INTO OUTFILE '/tmp/x' FROM t WHERE b = :pr_no",
        "SELECT a FROM t WHERE b = :pr_no FOR UPDATE",
        "WITH x AS (DELETE FROM t RETURNING a) SELECT a FROM x WHERE a = :pr_no",
        "SELECT SLEEP(10) FROM t WHERE b = :pr_no",
        "CALL p(:pr_no)",
        "SELECT a FROM t WHERE b = :pr_no -- SET something",
    ],
)
def test_anything_but_a_single_read_query_is_refused(sql: str) -> None:
    with pytest.raises(HosxpConfigurationError):
        check_sql(GET_PR, sql)


def test_harmless_functions_and_column_names_are_allowed() -> None:
    sql = (
        "SELECT REPLACE(a, '-', '') AS pr_no, update_date, x::text FROM t "
        "WHERE b = :pr_no ORDER BY offset_col;"
    )
    assert check_sql(GET_PR, sql).endswith("offset_col")


@pytest.mark.parametrize(
    "sql", ["SELECT a FROM t", "SELECT a FROM t WHERE b = :pr_no AND c = :other"]
)
def test_parameters_must_match_the_operation_exactly(sql: str) -> None:
    with pytest.raises(HosxpConfigurationError, match="parameters"):
        check_sql(GET_PR, sql)


def test_unknown_operations_statuses_and_missing_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(HosxpConfigurationError, match="unknown operation"):
        _load(tmp_path, "[queries.get_patients]\nsql = 'SELECT 1'\n")
    with pytest.raises(HosxpConfigurationError, match="status_map"):
        _load(tmp_path, '[status_map]\n"A" = "GONE"\n')
    with pytest.raises(HosxpConfigurationError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_example_template_contains_no_queries_yet() -> None:
    root = Path(__file__).resolve().parents[4]
    cfg = load_config(root / "hosxp_queries.example.toml")
    assert set(cfg.missing_needed()) == {n for n, o in OPERATIONS.items() if o.needed_now}


# ------------------------------------------------------------------ [stock_movement] (D-39)
_GOOD_STOCK_SQL = (
    "SELECT a AS movement_id, b AS movement_date, c AS department_id, d AS item_id, "
    "e AS qty, f AS native_kind FROM t WHERE b BETWEEN :date_from AND :date_to"
)
_PR_ONLY = "[queries.get_pr]\nsql = '''SELECT a AS pr_id, b AS pr_no FROM t WHERE b = :pr_no'''\n"


def _stock(body: str) -> str:
    return _PR_ONLY + "\n[stock_movement]\n" + body


_VERIFIED = (
    f"sql = '''{_GOOD_STOCK_SQL}'''\n"
    'verified_by = "MOCK IT"\nverified_on = 2026-10-01\nreference = "MOCK-REF"\n'
)


@pytest.mark.spec("AT-40.12.5", "D-39")
def test_verified_stock_section_is_usable(tmp_path: Path) -> None:
    cfg = _load(tmp_path, _stock(_VERIFIED + '[stock_movement.kind_map]\n"OUT" = "issue"\n'))
    src = cfg.stock  # type: ignore[attr-defined]
    assert src.usable and src.verification.reference == "MOCK-REF"
    assert dict(src.kind_map) == {"OUT": "ISSUE"}
    assert cfg.query("get_stock_movements").sql.startswith("SELECT")  # type: ignore[attr-defined]


@pytest.mark.spec("AT-40.12.5", "D-39")
@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("", "ยังไม่ได้ตั้งค่า"),  # no section at all: as on a new site
        (
            f"sql = '''{_GOOD_STOCK_SQL}'''\n[stock_movement.kind_map]\n\"OUT\" = \"ISSUE\"\n",
            "ยังไม่ได้รับการยืนยัน",
        ),
        (_VERIFIED, "kind_map"),
        (_VERIFIED + '[stock_movement.kind_map]\n"OUT" = "MOVE"\n', "ISSUE or RETURN"),
        (_VERIFIED + '[stock_movement.kind_map]\n"BACK" = "RETURN"\n', "no ISSUE"),
        (
            _VERIFIED.replace("2026-10-01", "2026-10-01T10:00:00")
            + '[stock_movement.kind_map]\n"OUT" = "ISSUE"\n',
            "TOML date",
        ),
        (
            _VERIFIED.replace("SELECT a", "DELETE FROM t; SELECT a")
            + '[stock_movement.kind_map]\n"OUT" = "ISSUE"\n',
            "ไม่ผ่านการตรวจสอบ",
        ),
        (
            _VERIFIED.replace(":date_to", "'2026-12-31'")
            + '[stock_movement.kind_map]\n"OUT" = "ISSUE"\n',
            "parameters",
        ),
        (_VERIFIED + 'extra = 1\n[stock_movement.kind_map]\n"OUT" = "ISSUE"\n', "unknown key"),
    ],
)
def test_a_stock_section_problem_disables_only_the_stock_figures(
    tmp_path: Path, body: str, reason: str
) -> None:
    text = _PR_ONLY if not body else _stock(body)
    cfg = _load(tmp_path, text)  # the file still loads: the PR features are unaffected
    assert cfg.query("get_pr")  # type: ignore[attr-defined]
    src = cfg.stock  # type: ignore[attr-defined]
    assert not src.usable and reason in src.problem
    with pytest.raises(HosxpConfigurationError):
        cfg.query("get_stock_movements")  # type: ignore[attr-defined]


@pytest.mark.spec("D-39")
def test_the_stock_query_is_refused_under_queries(tmp_path: Path) -> None:
    body = _PR_ONLY + f"[queries.get_stock_movements]\nsql = '''{_GOOD_STOCK_SQL}'''\n"
    cfg = _load(tmp_path, body)
    assert not cfg.stock.usable and "[stock_movement]" in cfg.stock.problem  # type: ignore[attr-defined]
    with pytest.raises(HosxpConfigurationError):
        cfg.query("get_stock_movements")  # type: ignore[attr-defined]
