-- =============================================================================
-- โครงสร้างข้อมูลสำหรับเชื่อมระบบ PPR กับ HOSxP (Interface views) - PostgreSQL
-- =============================================================================
-- ผู้ทำ: ฝ่าย IT / DBA ของโรงพยาบาล (ทำครั้งเดียว บนฐานข้อมูล HOSxP หรือ replica)
--
-- หลักการ
--   * สร้าง schema ใหม่ชื่อ ppr_interface แล้วสร้าง view 7 ตัวตามแบบด้านล่าง
--   * ชื่อ view และชื่อคอลัมน์ (หลัง AS) ต้องตรงตามนี้ทุกตัว ห้ามเปลี่ยน
--   * ส่วนที่อยู่ใน < > คือสิ่งที่ IT ต้องแทนด้วยตาราง/คอลัมน์จริงของ HOSxP
--     (ระบบ PPR ไม่รู้และไม่เดาชื่อตาราง HOSxP - IT เป็นผู้รู้)
--   * ระบบ PPR อ่าน view เหล่านี้อย่างเดียว ไม่เขียน ไม่แก้ HOSxP
--   * บัญชี ppr_readonly ได้สิทธิ์ SELECT เฉพาะ view เหล่านี้ ไม่เห็นตารางผู้ป่วย
--
-- กติกาสำคัญ
--   1. รหัส (id) ทุกตัวแปลงเป็นข้อความด้วย CAST(... AS text)
--   2. ตัวเลขจำนวน/ราคา/มูลค่า ใช้ CAST(... AS numeric) แบบไม่ระบุทศนิยม
--      ห้ามใช้ numeric(18,2) เพราะจะปัดเศษโดยไม่รู้ตัว (ระบบตรวจทศนิยมเอง, D-23)
--   3. active ต้องเป็นจริง/เท็จ (boolean) เช่น (<คอลัมน์สถานะ> = 'Y')
--   4. ปีงบประมาณเป็น พ.ศ. 4 หลัก (เช่น 2570) ถ้า HOSxP เก็บเป็น ค.ศ. ให้ + 543
--   5. สถานะ PR ส่งค่าดิบตามที่ HOSxP เก็บ ห้ามแปลงเอง
--   6. รหัสที่อ้างถึงกัน ต้องเป็นรหัสชุดเดียวกัน เช่น pr_header.department_id
--      ต้องตรงกับ department.source_id, pr_item.item_id ตรงกับ item.source_id
--
-- วิธีตรวจหลังสร้างเสร็จ (บนเครื่องที่ติดตั้งระบบ PPR):
--   .venv\Scripts\python -m ppr.cli hosxp-check --pr <เลขที่ PR จริง>
--   โดยใช้ไฟล์ query สำเร็จรูป docs/hosxp_interface/hosxp_queries.views.toml
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS ppr_interface;

-- -----------------------------------------------------------------------------
-- 1) ppr_interface.pr_header - หัวใบขอซื้อ (PR): 1 แถวต่อ 1 เลขที่ PR
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ppr_interface.pr_header AS
SELECT
    CAST(<รหัสภายในของ PR (primary key)> AS text)          AS pr_id,
    CAST(<เลขที่ PR ที่แสดงบนหน้าจอ HOSxP> AS text)         AS pr_no,
    CAST(<วันที่ PR> AS date)                              AS pr_date,
    CAST(<ปีงบประมาณ พ.ศ. ของ PR> AS integer)              AS fiscal_year,
    CAST(<รหัสหน่วยงานที่ขอซื้อ> AS text)                   AS department_id,
    CAST(<รหัสประเภทงบ / แหล่งเงิน> AS text)                AS fund_source_id,
    CAST(<รหัสชื่องบ / หมวดงบ> AS text)                     AS budget_category_id,
    CAST(<รหัสผู้ขอซื้อ> AS text)                           AS requester_id,
    CAST(<ชื่อผู้ขอซื้อ> AS text)                            AS requester_name,
    CAST(<ยอดรวมของ PR ตามหน้าจอ> AS numeric)             AS total_amount,
    CAST(<สถานะ PR ค่าดิบ> AS text)                        AS native_status,
    CAST(<วันเวลาที่แก้ไข PR ล่าสุด หรือ NULL> AS timestamp) AS last_modified_at
FROM <ตาราง PR ของ HOSxP>;
-- JOIN ตารางอื่นได้ แต่ 1 เลขที่ PR ต้องได้ 1 แถวเท่านั้น

-- -----------------------------------------------------------------------------
-- 2) ppr_interface.pr_item - รายการสินค้าในใบ PR: 1 แถวต่อ 1 บรรทัดของ PR
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ppr_interface.pr_item AS
SELECT
    CAST(<เลขที่ PR ของบรรทัดนี้ (ค่าเดียวกับ pr_header.pr_no)> AS text) AS pr_no,
    CAST(<รหัสบรรทัดรายการ (ไม่ซ้ำ)> AS text)              AS pr_item_id,
    CAST(<รหัสสินค้า/พัสดุ> AS text)                        AS item_id,
    CAST(<รหัสย่อสินค้า หรือ NULL> AS text)                 AS item_code,
    CAST(<ชื่อสินค้า> AS text)                              AS item_name,
    CAST(<จำนวนที่ขอ> AS numeric)                          AS qty,
    CAST(<หน่วยนับ> AS text)                               AS unit,
    CAST(<ราคาต่อหน่วย> AS numeric)                        AS unit_price,
    CAST(<มูลค่ารวมของบรรทัด> AS numeric)                  AS amount
FROM <ตารางรายการใน PR ของ HOSxP>;

-- -----------------------------------------------------------------------------
-- 3) ppr_interface.department - หน่วยงาน
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ppr_interface.department AS
SELECT
    CAST(<รหัสหน่วยงาน> AS text)       AS source_id,
    CAST(<รหัสย่อหน่วยงาน> AS text)    AS code,
    CAST(<ชื่อหน่วยงาน> AS text)       AS name,
    (<เงื่อนไขว่ายังใช้งานอยู่>)          AS active
FROM <ตารางหน่วยงานของ HOSxP>;

-- -----------------------------------------------------------------------------
-- 4) ppr_interface.item - สินค้า/พัสดุ
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ppr_interface.item AS
SELECT
    CAST(<รหัสสินค้า> AS text)         AS source_id,
    CAST(<รหัสย่อสินค้า> AS text)      AS code,
    CAST(<ชื่อสินค้า> AS text)         AS name,
    CAST(<หน่วยนับ> AS text)          AS unit,
    (<เงื่อนไขว่ายังใช้งานอยู่>)          AS active
FROM <ตารางสินค้า/พัสดุของ HOSxP>;

-- -----------------------------------------------------------------------------
-- 5) ppr_interface.fund_source - ประเภทงบ (แหล่งเงิน)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ppr_interface.fund_source AS
SELECT
    CAST(<รหัสประเภทงบ> AS text)       AS source_id,
    CAST(<รหัสย่อ> AS text)           AS code,
    CAST(<ชื่อประเภทงบ> AS text)       AS name,
    (<เงื่อนไขว่ายังใช้งานอยู่>)          AS active
FROM <ตารางประเภทงบของ HOSxP>;

-- -----------------------------------------------------------------------------
-- 6) ppr_interface.budget_category - ชื่องบ / หมวดงบ (มีหมวดแม่ได้)
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ppr_interface.budget_category AS
SELECT
    CAST(<รหัสหมวดงบ> AS text)          AS source_id,
    CAST(<รหัสย่อ> AS text)            AS code,
    CAST(<ชื่อหมวดงบ> AS text)          AS name,
    CAST(<รหัสหมวดแม่ หรือ NULL> AS text) AS parent_source_id,
    (<เงื่อนไขว่ายังใช้งานอยู่>)           AS active
FROM <ตารางชื่องบของ HOSxP>;

-- -----------------------------------------------------------------------------
-- 7) ppr_interface.app_user - ผู้ใช้ HOSxP สำหรับ login เข้าระบบ PPR (D-25)
--    password_hash = ค่ารหัสผ่านที่ HOSxP เก็บ (MD5) ตามที่เก็บจริง ห้ามแปลง
-- -----------------------------------------------------------------------------
CREATE OR REPLACE VIEW ppr_interface.app_user AS
SELECT
    CAST(<รหัสผู้ใช้ (ไม่เปลี่ยน)> AS text)      AS source_id,
    CAST(<ชื่อผู้ใช้ที่พิมพ์ตอน login> AS text)   AS username,
    CAST(<ชื่อ-สกุล> AS text)                  AS display_name,
    CAST(<รหัสหน่วยงานของผู้ใช้> AS text)        AS department_id,
    COALESCE(CAST(<ค่ารหัสผ่านที่เก็บ> AS text), '') AS password_hash,
    (<เงื่อนไขว่าบัญชียังใช้งานได้>)               AS active
FROM <ตารางผู้ใช้ของ HOSxP>;

-- -----------------------------------------------------------------------------
-- คำอธิบายภาษาไทยเก็บไว้ในฐานข้อมูล (ดูได้ใน pgAdmin)
-- -----------------------------------------------------------------------------
COMMENT ON SCHEMA ppr_interface IS 'ข้อมูลที่ระบบ PPR อ่านจาก HOSxP (อ่านอย่างเดียว)';
COMMENT ON VIEW ppr_interface.pr_header IS 'หัวใบ PR: 1 แถวต่อ 1 เลขที่ PR';
COMMENT ON COLUMN ppr_interface.pr_header.pr_id IS 'รหัสภายในของ PR ไม่เปลี่ยนตลอดอายุ PR';
COMMENT ON COLUMN ppr_interface.pr_header.pr_no IS 'เลขที่ PR ที่ผู้ใช้เห็นบนหน้าจอ';
COMMENT ON COLUMN ppr_interface.pr_header.fiscal_year IS 'ปีงบประมาณ พ.ศ. 4 หลัก';
COMMENT ON COLUMN ppr_interface.pr_header.total_amount IS 'ยอดรวมของ PR ต้องเท่ากับผลรวม pr_item.amount';
COMMENT ON COLUMN ppr_interface.pr_header.native_status IS 'สถานะ PR ค่าดิบจาก HOSxP ไม่แปลง';
COMMENT ON VIEW ppr_interface.pr_item IS 'รายการใน PR: 1 แถวต่อ 1 บรรทัด';
COMMENT ON COLUMN ppr_interface.pr_item.amount IS 'มูลค่าบรรทัด ทศนิยมไม่เกิน 2 ตำแหน่ง';
COMMENT ON VIEW ppr_interface.department IS 'หน่วยงาน';
COMMENT ON VIEW ppr_interface.item IS 'สินค้า/พัสดุ';
COMMENT ON VIEW ppr_interface.fund_source IS 'ประเภทงบ (แหล่งเงิน)';
COMMENT ON VIEW ppr_interface.budget_category IS 'ชื่องบ/หมวดงบ';
COMMENT ON VIEW ppr_interface.app_user IS 'ผู้ใช้สำหรับ login เข้าระบบ PPR';

-- -----------------------------------------------------------------------------
-- สิทธิ์: ให้บัญชีอ่านอย่างเดียวเห็นเฉพาะ view เหล่านี้
-- (สร้างบัญชีตาม docs/HOSXP_CONNECTION.md ขั้นที่ 1 แต่ไม่ต้อง GRANT ตารางอื่น)
-- -----------------------------------------------------------------------------
GRANT USAGE ON SCHEMA ppr_interface TO ppr_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA ppr_interface TO ppr_readonly;
