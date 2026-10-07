# ruff: noqa: E501 - SQL test fixture text
"""A small MADE-UP database used to test the SQL HOSxP adapter.

Table and column names are deliberately fake (``mockhx_*``) - they do NOT describe
HOSxP. The adapter never knows them either: they only appear in the test query file
below, exactly as a hospital's own query file would name its real tables.
Portable across PostgreSQL and MariaDB/MySQL.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import Engine, text

TABLES = (
    "mockhx_move",
    "mockhx_user",
    "mockhx_counter",
    "mockhx_pr_line",
    "mockhx_pr",
    "mockhx_cat",
    "mockhx_fund",
    "mockhx_item",
    "mockhx_dept",
)

DDL = [
    "CREATE TABLE mockhx_dept (dep_code VARCHAR(30) PRIMARY KEY, dep_name VARCHAR(100),"
    " dep_abbr VARCHAR(10), is_active INT)",
    "CREATE TABLE mockhx_item (item_code VARCHAR(30) PRIMARY KEY, item_name VARCHAR(100),"
    " item_abbr VARCHAR(10), unit_name VARCHAR(20), is_active INT)",
    "CREATE TABLE mockhx_fund (fund_code VARCHAR(30) PRIMARY KEY, fund_name VARCHAR(100),"
    " fund_abbr VARCHAR(10), is_active INT)",
    "CREATE TABLE mockhx_cat (cat_code VARCHAR(30) PRIMARY KEY, cat_name VARCHAR(100),"
    " cat_abbr VARCHAR(10), parent_code VARCHAR(30), is_active INT)",
    "CREATE TABLE mockhx_pr (pr_key INT PRIMARY KEY, pr_number VARCHAR(20), pr_day DATE,"
    " budget_year INT, dep_code VARCHAR(30), fund_code VARCHAR(30), cat_code VARCHAR(30),"
    " total DECIMAL(18,2), state_code VARCHAR(5), requester VARCHAR(100))",
    "CREATE TABLE mockhx_pr_line (line_key INT PRIMARY KEY, pr_key INT, item_code VARCHAR(30),"
    " qty DECIMAL(18,4), price DECIMAL(18,2), amount DECIMAL(18,2))",
    # D-39: made-up store movements; 'OUT' issue, 'BACK' return, 'ADJ' an adjustment that the
    # test query file deliberately leaves unmapped.
    "CREATE TABLE mockhx_move (move_key INT PRIMARY KEY, move_day DATE, dep_code VARCHAR(30),"
    " item_code VARCHAR(30), qty DECIMAL(18,4), move_type VARCHAR(5))",
    "CREATE TABLE mockhx_user (user_key VARCHAR(30) PRIMARY KEY, login_name VARCHAR(50),"
    " full_name VARCHAR(100), dep_code VARCHAR(30), pwd_digest VARCHAR(64), is_active INT)",
]

ROWS = [
    "INSERT INTO mockhx_dept VALUES ('MOCK-DEP-ENT','Mock ENT','ENT',1),"
    " ('MOCK-DEP-OR','Mock OR','OR',1), ('MOCK-DEP-OLD','Mock closed','OLD',0)",
    "INSERT INTO mockhx_item VALUES ('MOCK-ITEM-TONER','Mock toner','I1','box',1),"
    " ('MOCK-ITEM-PAPER','Mock paper','I2','ream',1), ('MOCK-ITEM-GLOVE','Mock gloves','I3','pack',1)",
    "INSERT INTO mockhx_fund VALUES ('MOCK-FUND-1','Mock fund 1','F1',1)",
    "INSERT INTO mockhx_cat VALUES ('MOCK-CAT-MAT','Materials','C1',NULL,1),"
    " ('MOCK-CAT-OFFICE','Office supplies','C2','MOCK-CAT-MAT',1)",
    # PR 1: ordinary. PR 2: header total differs. PR 3: cancelled (native code 'X9').
    "INSERT INTO mockhx_pr VALUES"
    " (1,'MOCK-SQL-0001','2026-10-15',2570,'MOCK-DEP-ENT','MOCK-FUND-1','MOCK-CAT-OFFICE',"
    "500.00,'A1','Mock requester'),"
    " (2,'MOCK-SQL-0002','2026-10-15',2570,'MOCK-DEP-ENT','MOCK-FUND-1','MOCK-CAT-OFFICE',"
    "107.00,'A1',NULL),"
    " (3,'MOCK-SQL-0003','2026-10-15',2570,'MOCK-DEP-ENT','MOCK-FUND-1','MOCK-CAT-OFFICE',"
    "100.00,'X9',NULL)",
    "INSERT INTO mockhx_pr_line VALUES (11,1,'MOCK-ITEM-TONER',4,100.00,400.00),"
    " (12,1,'MOCK-ITEM-PAPER',2,50.00,100.00), (21,2,'MOCK-ITEM-TONER',1,100.00,100.00),"
    " (31,3,'MOCK-ITEM-TONER',1,100.00,100.00)",
    "INSERT INTO mockhx_move VALUES (1,'2026-10-15','MOCK-DEP-ENT','MOCK-ITEM-TONER',3,'OUT'),"
    " (2,'2026-10-16','MOCK-DEP-OR','MOCK-ITEM-TONER',2,'OUT'),"
    " (3,'2026-10-17','MOCK-DEP-ENT','MOCK-ITEM-TONER',1,'BACK'),"
    " (4,'2026-10-18','MOCK-DEP-ENT','MOCK-ITEM-TONER',5,'ADJ'),"
    " (5,'2025-09-30','MOCK-DEP-ENT','MOCK-ITEM-TONER',9,'OUT')",
    # Users (D-25): the made-up column holds an MD5 hex digest. Passwords:
    # mock_ent = "Ent-Pass-1" (stored upper-case), mock_off = "Off-Pass-2" (inactive),
    # mock_thai = "รหัสไทย9" (UTF-8 bytes hashed).
    "INSERT INTO mockhx_user VALUES"
    " ('MOCK-U-1','mock_ent','Mock ENT user','MOCK-DEP-ENT',"
    "'" + hashlib.md5(b"Ent-Pass-1", usedforsecurity=False).hexdigest().upper() + "',1),"
    " ('MOCK-U-2','mock_off','Mock inactive user','MOCK-DEP-ENT',"
    "'" + hashlib.md5(b"Off-Pass-2", usedforsecurity=False).hexdigest() + "',0),"
    " ('MOCK-U-3','mock_thai','Mock Thai password','MOCK-DEP-OR',"
    "'" + hashlib.md5("รหัสไทย9".encode(), usedforsecurity=False).hexdigest() + "',1)",
]

QUERIES_TOML = """
[status_map]
"A1" = "ACTIVE"
"X9" = "CANCELLED"

[queries.get_pr]
sql = '''
SELECT CONCAT('', p.pr_key) AS pr_id, p.pr_number AS pr_no, p.pr_day AS pr_date,
       p.budget_year AS fiscal_year, p.dep_code AS department_id,
       p.fund_code AS fund_source_id, p.cat_code AS budget_category_id,
       p.requester AS requester_name, p.total AS total_amount, p.state_code AS native_status
FROM mockhx_pr p
WHERE p.pr_number = :pr_no
'''

[queries.get_pr_items]
sql = '''
SELECT CONCAT('', l.line_key) AS pr_item_id, l.item_code AS item_id, i.item_name AS item_name,
       l.qty AS qty, i.unit_name AS unit, l.price AS unit_price, l.amount AS amount
FROM mockhx_pr_line l
JOIN mockhx_pr p ON p.pr_key = l.pr_key
LEFT JOIN mockhx_item i ON i.item_code = l.item_code
WHERE p.pr_number = :pr_no
ORDER BY l.line_key
'''

[queries.get_departments]
sql = "SELECT dep_code AS source_id, dep_abbr AS code, dep_name AS name, is_active AS active FROM mockhx_dept"

[queries.get_items]
sql = '''SELECT item_code AS source_id, item_abbr AS code, item_name AS name, unit_name AS unit,
         is_active AS active FROM mockhx_item'''

[queries.get_fund_sources]
sql = "SELECT fund_code AS source_id, fund_abbr AS code, fund_name AS name, is_active AS active FROM mockhx_fund"

[queries.get_budget_categories]
sql = '''SELECT cat_code AS source_id, cat_abbr AS code, cat_name AS name,
         parent_code AS parent_source_id, is_active AS active FROM mockhx_cat'''

[queries.get_login_user]
sql = '''SELECT user_key AS source_id, login_name AS username, full_name AS display_name,
         dep_code AS department_id, pwd_digest AS password_hash, is_active AS active
         FROM mockhx_user WHERE login_name = :username'''

[stock_movement]
sql = '''
SELECT CONCAT('', m.move_key) AS movement_id, m.move_day AS movement_date,
       m.dep_code AS department_id, m.item_code AS item_id, m.qty AS qty,
       m.move_type AS native_kind
FROM mockhx_move m
WHERE m.move_day BETWEEN :date_from AND :date_to
ORDER BY m.move_key
'''
verified_by = "MOCK IT"
verified_on = 2026-10-01
reference = "MOCK-REF-SQL"

[stock_movement.kind_map]
"OUT" = "ISSUE"
"BACK" = "RETURN"

[queries.get_user_profile]
sql = '''SELECT user_key AS source_id, login_name AS username, full_name AS display_name,
         dep_code AS department_id, is_active AS active
         FROM mockhx_user WHERE user_key = :source_id'''
"""


def create(engine: Engine) -> None:
    drop(engine)
    with engine.begin() as conn:
        for stmt in DDL + ROWS:
            conn.execute(text(stmt))


def drop(engine: Engine) -> None:
    with engine.begin() as conn:
        for t in TABLES:
            conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
