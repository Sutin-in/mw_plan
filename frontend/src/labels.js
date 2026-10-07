'use strict';
/** Thai display labels. Unknown codes fall back to the API's own message. */

const STATES = {
  DRAFT: 'ฉบับร่าง',
  CONFIRMED_LOCKED: 'ยืนยันแล้ว (ล็อก)',
  PROCUREMENT_VERIFIED: 'พัสดุตรวจสอบแล้ว',
  PR_CHANGED_REVIEW_REQUIRED: 'PR มีการเปลี่ยนแปลง ต้องทบทวน',
  UNLOCKED_FOR_REVISION: 'ปลดล็อกเพื่อแก้ไข',
  CANCELLED: 'ยกเลิก',
  CLOSED: 'ปิดแล้ว',
};

const ROLES = {
  REQUESTER: 'ผู้ขอ',
  PLAN_OFFICER: 'เจ้าหน้าที่แผน',
  PROCUREMENT: 'เจ้าหน้าที่พัสดุ',
  HEAD_OF_PROCUREMENT: 'หัวหน้าเจ้าหน้าที่พัสดุ',
  FINANCE: 'การเงิน',
  CFO: 'ผู้บริหารการเงิน',
  EXECUTIVE: 'ผู้บริหาร',
  ADMIN: 'ผู้ดูแลระบบ',
};

// Violation / error codes returned by the API (spec D-03 ... D-23).
const CODES = {
  PR_NOT_FOUND: 'ไม่พบ PR นี้ใน HOSxP',
  PR_HAS_NO_ITEMS: 'PR ไม่มีรายการ',
  FISCAL_YEAR_UNKNOWN: 'PR ไม่ระบุปีงบประมาณ',
  FISCAL_YEAR_NOT_ACTIVE: 'แผนของปีงบประมาณนี้ยังไม่เปิดใช้งาน',
  PLAN_YEAR_NOT_FOUND: 'ยังไม่มีแผนของปีงบประมาณปัจจุบัน',
  PLAN_YEAR_EXPIRED: 'แผนหมดอายุแล้ว: PR เป็นของปีงบประมาณที่ผ่านมา (D-18)',
  FISCAL_YEAR_NOT_CURRENT: 'PR เป็นของปีงบประมาณถัดไป (D-18)',
  PR_MISSING_DEPARTMENT: 'PR ไม่ระบุหน่วยงาน',
  PR_CANCELLED_IN_HOSXP: 'PR ถูกยกเลิกใน HOSxP',
  PR_TOTAL_UNAVAILABLE: 'PR ไม่มียอดรวม',
  PR_TOTAL_MISMATCH: 'ยอดรวม PR ไม่เท่ากับผลรวมของรายการ (D-17)',
  PR_VALUE_PRECISION: 'ทศนิยมเกินที่ระบบรองรับ (D-23) ระบบไม่ปัดเศษให้',
  PR_ALREADY_BOUND: 'PR นี้เคยมี PPR แล้ว ใช้ซ้ำไม่ได้ (D-03)',
  DRAFT_ALREADY_EXISTS: 'PR นี้มีฉบับร่างที่กำลังทำอยู่แล้ว (D-05)',
  UNKNOWN_FUND_SOURCE: 'ไม่รู้จักแหล่งเงินของ PR',
  DUPLICATE_PR_ITEM: 'รายการใน PR ซ้ำกัน',
  MANUAL_PR_ITEM: 'รายการที่ไม่ได้มาจาก HOSxP ไม่อนุญาต',
  NOT_IN_PLAN: 'ไม่มีรายการแผนของหน่วยงานและแหล่งเงินนี้ที่นับด้วยหน่วยเดียวกับรายการ PR',
  AMBIGUOUS_PLAN_MATCH: 'รายการตรงกับแผนมากกว่าหนึ่งรายการ',
  // Wave 12A-2 (D-43): the requester chooses the plan row of every PR line
  PLAN_ROW_NOT_CHOSEN: 'ยังไม่ได้เลือกรายการแผนที่ใช้ (D-43)',
  PLAN_ROW_NOT_ALLOWED: 'รายการแผนที่เลือกใช้กับรายการ PR นี้ไม่ได้ (หน่วยงาน แหล่งเงิน หรือหน่วยนับไม่ตรง หรือไม่อยู่ในแผนที่เปิดใช้) (D-43)',
  PLAN_ROW_CHANGED: 'รายการแผนที่เลือกถูกเปลี่ยนชื่อหรือหน่วยนับหลังจากเลือก: เลือกใหม่อีกครั้งก่อนยืนยัน (D-43)',
  CHOICE_NOT_A_PR_LINE: 'PR ไม่มีรายการนี้แล้ว: เปิดหน้าใหม่แล้วเลือกอีกครั้ง',
  DUPLICATE_CHOICE: 'เลือกรายการแผนให้รายการ PR เดียวกันซ้ำ',
  BAD_CHOICE: 'ข้อมูลรายการแผนที่เลือกไม่ถูกต้อง',
  NAME_AND_UNIT_REQUIRED: 'รายการแผนที่ไม่มีรหัสสินค้า HOSxP ต้องมีชื่อรายการและหน่วยนับ (D-43)',
  NAME_CHANGE_AFTER_USE: 'มี PPR ใช้รายการแผนนี้แล้ว: ชื่อและหน่วยนับเปลี่ยนไม่ได้ (D-43)',
  NAME_FROM_HOSXP: 'รายการนี้มีรหัสสินค้า HOSxP: ชื่อและหน่วยนับมาจาก HOSxP',
  ASSIGNED_WITHOUT_ITEM: 'แผนซื้อแทนต้องมีรหัสสินค้า HOSxP (D-38 A-1)',
  DEMAND_IN_CENTRAL_PURCHASE: 'หน่วยงานนี้มีความต้องการรายการนี้อยู่ในแผนซื้อแทน ให้หน่วยงานผู้ซื้อเป็นผู้ซื้อ (D-38)',
  COVERAGE_REQUIRED: 'ต้องระบุว่าซื้อให้หน่วยงานใดเท่าไร (แผนซื้อแทน, D-38)',
  COVERAGE_SUM_MISMATCH: 'ผลรวมที่ซื้อให้แต่ละหน่วยงานต้องเท่ากับจำนวนที่ซื้อ',
  COVERAGE_UNKNOWN_DEPARTMENT: 'หน่วยงานนี้ไม่มีความต้องการในรายการนี้',
  COVERAGE_NON_POSITIVE: 'จำนวนที่ซื้อให้หน่วยงานต้องมากกว่าศูนย์',
  COVERAGE_EXCEEDS_NEED: 'จำนวนที่ซื้อให้เกินความต้องการที่เหลือของหน่วยงาน',
  COVERAGE_NOT_IN_PPR: 'PPR นี้ไม่ได้ซื้อรายการแผนนี้',
  COVERAGE_INVALID: 'ยอดที่ซื้อให้แต่ละหน่วยงานไม่ถูกต้อง',
  COVERAGE_DEMAND_CLAIMED_ELSEWHERE: 'ความต้องการของหน่วยงานนี้อยู่ในแผนซื้อแทนอีกรายการด้วย ความต้องการหนึ่งซื้อได้ทางเดียว ต้องปรับแผนก่อน (D-38)',
  DUPLICATE_DEMAND_CLAIM: 'หน่วยงานเดียวมีความต้องการรายการ/แหล่งเงินเดียวกันในแผนซื้อแทนมากกว่าหนึ่งรายการไม่ได้ (ความต้องการหนึ่งซื้อได้ทางเดียว, D-38)',
  DUPLICATE_ROW: 'ระบุหน่วยงานซ้ำในรายการเดียวกัน',
  INVALID_REQUESTED_QTY: 'จำนวนที่ขอไม่ถูกต้อง',
  INVALID_REQUESTED_AMOUNT: 'จำนวนเงินที่ขอไม่ถูกต้อง',
  QTY_EXCEEDED: 'จำนวนเกินกว่าที่แผนเหลืออยู่',
  AMOUNT_EXCEEDED: 'จำนวนเงินเกินกว่าที่แผนเหลืออยู่',
  QTY_EXHAUSTED: 'แผนของรายการนี้ใช้หมดแล้ว (จำนวน)',
  AMOUNT_EXHAUSTED: 'แผนของรายการนี้ใช้หมดแล้ว (เงิน)',
  ALLOCATION_EMPTY: 'ต้องระบุหมวดงบอย่างน้อยหนึ่งหมวด',
  ALLOCATION_DUPLICATE_CATEGORY: 'ระบุหมวดงบซ้ำ',
  ALLOCATION_NON_POSITIVE: 'ยอดของหมวดงบต้องมากกว่าศูนย์',
  ALLOCATION_UNKNOWN_CATEGORY: 'หมวดงบนี้ใช้กับ PPR นี้ไม่ได้',
  ALLOCATION_CROSS_FUND: 'หมวดงบต้องอยู่ในแหล่งเงินเดียวกับ PR (D-19)',
  ALLOCATION_SUM_MISMATCH: 'ผลรวมของหมวดงบต้องเท่ากับยอดที่ขอ',
  ADJUSTMENT_REFERENCE_REQUIRED: 'แบ่งหลายหมวดงบต้องระบุเลขอ้างอิงการปรับ (D-19)',
  ALLOCATION_INVALID: 'การแบ่งหมวดงบไม่ถูกต้อง',
  PR_CHANGED_SINCE_DRAFT: 'PR ใน HOSxP เปลี่ยนไปจากตอนทำฉบับร่าง ต้องยกเลิกร่างแล้วทำใหม่ (D-21)',
  PR_IDENTITY_CHANGED: 'ปีงบ/หน่วยงาน/แหล่งเงินของ PR เปลี่ยน ยืนยันซ้ำไม่ได้ ต้องให้คณะกรรมการพิจารณา (D-22)',
  PR_NOT_ELIGIBLE: 'PR นี้ยังไม่ผ่านการตรวจสอบกับแผน',
  PPR_LOCKED: 'PPR ถูกล็อกแล้ว แก้ไขไม่ได้',
  NOT_A_DRAFT: 'ยกเลิกได้เฉพาะฉบับร่าง',
  PPR_NOT_FOUND: 'ไม่พบ PPR นี้ หรือไม่มีสิทธิ์ดู',
  PLAN_CHANGED_DURING_CONFIRM: 'แผนเปลี่ยนระหว่างยืนยัน กรุณาลองอีกครั้ง',
  PLAN_OVERSUBSCRIBED: 'ยอดคงเหลือของแผนไม่พอ',
  INVALID_STATE: 'สถานะปัจจุบันทำรายการนี้ไม่ได้',
  ROLE_NOT_ALLOWED: 'บทบาทของคุณทำรายการนี้ไม่ได้',
  REQUESTER_ONLY: 'ต้องมีบทบาทผู้ขอ',
  NOT_THE_CREATOR: 'เฉพาะผู้ขอที่สร้าง PPR ใบนี้เท่านั้นที่ทำรายการนี้ได้ (D-41)',
  REASON_REQUIRED: 'ต้องระบุเหตุผล',
  HOSXP_UNAVAILABLE: 'ติดต่อ HOSxP ไม่ได้ในขณะนี้ ไม่มีการบันทึกข้อมูลใด ๆ',
  API_UNREACHABLE: 'ติดต่อระบบหลังบ้าน (API) ไม่ได้',
  AUTH_FAILED: 'ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง หรือบัญชีไม่ใช้งาน',
  NOT_AUTHENTICATED: 'กรุณาเข้าสู่ระบบ',
  FORBIDDEN: 'ไม่มีสิทธิ์ทำรายการนี้',
  NOT_CONFIRMED: 'ฉบับร่างยังไม่มีเอกสารสำหรับพิมพ์',
  CSRF_FAILED: 'แบบฟอร์มหมดอายุ กรุณาลองใหม่',
  NOT_APPOINTED: 'คุณไม่ใช่หัวหน้าเจ้าหน้าที่พัสดุที่ได้รับแต่งตั้งและมีผลในวันนี้ (D-28)',
  PPR_VERSION_CHANGED: 'PPR มีฉบับใหม่แล้ว กรุณาดูฉบับปัจจุบันก่อนตรวจสอบ',
  ALREADY_VERIFIED: 'ฉบับนี้ตรวจสอบแล้ว',
  APPOINTMENT_OVERLAP: 'ช่วงวันที่ซ้อนกับการแต่งตั้งที่ใช้งานอยู่ ให้กำหนดวันสิ้นสุดของคนเดิมก่อน (D-27)',
  APPOINTMENT_OUTSIDE_FISCAL_YEAR: 'วันที่ต้องอยู่ในปีงบประมาณที่แต่งตั้ง (D-27)',
  APPOINTMENT_INVALID_RANGE: 'วันสิ้นสุดต้องไม่ก่อนวันเริ่ม',
  APPOINTMENT_END_NOT_EARLIER: 'วันสิ้นสุดใหม่ต้องก่อนวันสิ้นสุดเดิม',
  APPOINTMENT_END_TOO_EARLY: 'วันสิ้นสุดใหม่เร็วเกินไป (ก่อนวันเริ่ม ก่อนเมื่อวาน หรือก่อนวันที่เคยใช้ตรวจสอบ)',
  APPOINTMENT_NOT_ACTIVE: 'การแต่งตั้งนี้ถูกเพิกถอนแล้ว',
  APPOINTMENT_IN_USE: 'การแต่งตั้งนี้เคยใช้ตรวจสอบ PPR แล้ว เพิกถอนไม่ได้ ให้กำหนดวันสิ้นสุด หรือเสนอคณะกรรมการ (D-27)',
  APPOINTMENT_OVERLAPS_EVIDENCE: 'ช่วงวันที่นี้มีการตรวจสอบ PPR ภายใต้การแต่งตั้งอื่นแล้ว (D-27)',
  APPOINTMENT_NOT_FOUND: 'ไม่พบการแต่งตั้งนี้',
  USER_NOT_FOUND: 'ไม่พบผู้ใช้',
  USER_NOT_ELIGIBLE: 'แต่งตั้งได้เฉพาะผู้ใช้ที่ยังใช้งานและมีบทบาทหัวหน้าเจ้าหน้าที่พัสดุ',
  UNKNOWN_BUCKET: 'ไม่รู้จักกลุ่มงานนี้',
  // Wave 6 (D-29 ... D-31)
  SYNC_ALREADY_RUNNING: 'มีการซิงค์ PPR ทั้งหมดกำลังทำงานอยู่ กรุณารอให้เสร็จก่อน',
  PPR_NOT_SYNCABLE: 'PPR สถานะนี้ไม่ต้องซิงค์ (ฉบับร่างไม่ซิงค์ · PPR ที่ยกเลิกแล้วใช้ "ตรวจซ้ำ")',
  NOT_CANCELLED: 'ตรวจซ้ำได้เฉพาะ PPR ที่ยกเลิกแล้ว',
  SYNC_RUN_NOT_FOUND: 'ไม่พบรอบการซิงค์นี้',
  RUN_STILL_RUNNING: 'รอบการซิงค์นี้ยังทำงานไม่เสร็จ',
  RUN_NOT_RETRYABLE: 'รอบการตรวจซ้ำทำซ้ำอัตโนมัติไม่ได้ ให้กดตรวจซ้ำที่ PPR',
  REQUESTER_REQUIRED: 'การซิงค์ด้วยมือต้องระบุผู้สั่ง',
  HOSXP_QUERY_ERROR: 'คำสั่งอ่าน PR ของ HOSxP ผิดพลาด/ได้ข้อมูลที่ใช้ไม่ได้ (ตรวจไฟล์คำสั่งของโรงพยาบาล)',
  HOSXP_ITEMS_QUERY_ERROR: 'อ่านรายการของ PR จาก HOSxP ไม่ได้ (ตรวจไฟล์คำสั่งของโรงพยาบาล)',
  PR_NO_MISMATCH: 'HOSxP ส่ง PR เลขอื่นกลับมา ผูกกับ PPR นี้ไม่ได้',
  PR_ID_MISMATCH: 'รหัสภายในของ PR ใน HOSxP ไม่ตรงกับตอนยืนยัน ผูกกับ PPR นี้ไม่ได้',
  PR_STATUS_UNREADABLE: 'สถานะ PR ที่ได้มาผิดรูปแบบ ใช้ไม่ได้',
  ALREADY_RELEASED: 'แผนของ PPR นี้คืนไปแล้ว ไม่คืนซ้ำ',
  RELEASE_REFUSED: 'ระบบปฏิเสธการคืนแผน (ตรวจบัญชีแผน)',
  // Wave 8A: plan screens (D-11 ... D-16, D-34) and alert settings (D-33)
  PLAN_YEAR_EXISTS: 'มีปีงบประมาณนี้แล้ว',
  INVALID_FISCAL_YEAR: 'ปีงบประมาณไม่ถูกต้อง (พ.ศ. 4 หลัก)',
  INVALID_TRANSITION: 'เปลี่ยนสถานะแผนแบบนี้ไม่ได้',
  PLAN_NOT_VALID: 'แผนยังไม่ผ่านการตรวจ เปิดใช้ไม่ได้ (D-12)',
  PLAN_YEAR_LOCKED: 'แผนเปิดใช้แล้ว แก้ไขได้เฉพาะผ่านการปรับแผน (D-37)',
  CORRECTION_DETAILS_REQUIRED: 'แผนอนุมัติแล้ว: แก้ได้เฉพาะการแก้ข้อมูลให้ตรงเอกสารที่อนุมัติ ต้องระบุเหตุผลและเลขที่เอกสารอ้างอิง (D-15)',
  UNKNOWN_MASTER: 'ไม่พบรหัสนี้ในข้อมูลหลักที่ซิงค์จาก HOSxP',
  INACTIVE_MASTER: 'รหัสนี้ปิดใช้งานแล้วใน HOSxP',
  BUDGET_EXISTS: 'แหล่งเงิน/หมวดงบนี้มีวงเงินแล้ว',
  // Wave 12A-1 (D-42): plan import from Excel
  FILE_TOO_LARGE: 'ไฟล์ใหญ่เกิน 10 MB',
  EMPTY_FILE: 'ยังไม่ได้เลือกไฟล์',
  BAD_UPLOAD: 'อ่านไฟล์ที่ส่งมาไม่ได้ ลองเลือกไฟล์ใหม่',
  NOT_XLSX: 'ไม่ใช่ไฟล์ Excel (.xlsx)',
  MISSING_SHEET: 'ไฟล์ไม่มีแผ่นตามแม่แบบนำเข้า',
  MISSING_COLUMNS: 'ไฟล์ไม่มีคอลัมน์ตามแม่แบบนำเข้า',
  DUPLICATE_COLUMNS: 'ไฟล์มีคอลัมน์ชื่อซ้ำ',
  TOO_MANY_ROWS: 'ไฟล์มีแถวมากเกินไป',
  IMPORT_HAS_ERRORS: 'ไฟล์มีข้อผิดพลาด ไม่ได้นำเข้าเลย (ทั้งหมดหรือไม่มีเลย)',
  DUPLICATE_FILE: 'ไฟล์นี้นำเข้าแล้วในปีงบนี้: ยกเลิกการนำเข้าเดิมก่อนถ้าจะนำเข้าซ้ำ',
  FILE_CHANGED: 'ไฟล์ไม่ตรงกับที่ตรวจ: ตรวจไฟล์ใหม่',
  IMPORT_EXPIRED: 'ผลตรวจหมดเวลาหรือไม่ใช่ของคุณ: ตรวจไฟล์ใหม่อีกครั้ง',
  PLAN_YEAR_NOT_DRAFT: 'นำเข้าหรือยกเลิกการนำเข้าได้เฉพาะแผนสถานะร่าง (D-42)',
  IMPORT_ALREADY_REMOVED: 'การนำเข้านี้ถูกยกเลิกแล้ว',
  IMPORT_NOT_FOUND: 'ไม่พบการนำเข้านี้',
  ITEM_REQUIRED: 'ต้องมีรหัสสินค้า HOSxP: แผนซื้อแทน หรือรายการที่มีรหัสอยู่แล้ว (D-43)',
  UNIT_MISMATCH: 'หน่วยนับของสินค้าใน HOSxP ไม่ตรงกับรายการแผน (D-42)',
  UNBOUND_ROW: 'การปรับแผนไม่เพิ่มหรือเอารหัสสินค้า HOSxP ออกจากรายการเดิม: เพิ่มรายการใหม่แทน (D-43)',
  ITEM_COLUMN_IGNORED: 'คอลัมน์รหัสสินค้า HOSxP ไม่ใช้แล้ว: ไม่นำเข้ารหัส (D-43)',
  BUDGET_NOT_FOUND: 'ไม่พบวงเงินนี้',
  BUDGET_IN_USE: 'วงเงินนี้มีรายการแผนอยู่ ให้ลบหรือย้ายรายการก่อน',
  BUDGET_NOT_IN_YEAR: 'วงเงินที่เลือกไม่ได้อยู่ในปีงบนี้',
  DEMAND_NOT_ALLOWED: 'ความต้องการรายหน่วยงานใช้กับแผนซื้อแทน (ASSIGNED) เท่านั้น',
  DUPLICATE_DEMAND: 'ระบุหน่วยงานซ้ำในความต้องการ',
  PLAN_ITEM_NOT_FOUND: 'ไม่พบรายการแผน หรือไม่มีสิทธิ์ดู',
  INVALID_INPUT: 'ข้อมูลที่กรอกไม่ถูกต้อง',
  SETTING_OUT_OF_RANGE: 'ค่าที่กำหนดอยู่นอกช่วงที่อนุญาต',
  NO_BUDGETS: 'ปีงบนี้ยังไม่มีวงเงิน',
  NO_ITEMS: 'ปีงบนี้ยังไม่มีรายการแผน',
  ENVELOPE_MISMATCH: 'ผลรวมรายการแผนไม่เท่ากับวงเงินที่อนุมัติของหมวดงบ (D-11)',
  // Wave 7A: Plan Amendment (D-37)
  AMENDMENT_NOT_VALID: 'บันทึกการปรับแผนไม่ได้ ดูรายการที่ต้องแก้',
  PLAN_YEAR_NOT_ACTIVE: 'ปรับแผนได้เฉพาะแผนที่เปิดใช้แล้ว',
  APPROVAL_DOCUMENT_REQUIRED: 'ต้องระบุเลขที่หนังสืออนุมัติ',
  APPROVAL_DOCUMENT_TOO_LONG: 'เลขที่หนังสืออนุมัติยาวเกิน 100 ตัวอักษร',
  APPROVAL_DATE_IN_FUTURE: 'วันที่อนุมัติต้องไม่เกินวันนี้',
  AMENDMENT_NOT_FOUND: 'ไม่พบการปรับแผนนี้',
  EMPTY_AMENDMENT: 'ยังไม่มีค่าใดเปลี่ยนจากแผนปัจจุบัน',
  UNKNOWN_PLAN_ITEM: 'รายการแผนนี้ไม่อยู่ในปีงบนี้',
  DUPLICATE_CHANGE: 'ปรับรายการหรือหมวดงบเดียวกันซ้ำในครั้งเดียว',
  FUND_SOURCE_CHANGED: 'เปลี่ยนแหล่งเงินของรายการแผนไม่ได้ (โอนได้เฉพาะในแหล่งเงินเดียวกัน)',
  PLAN_TYPE_CHANGED: 'เปลี่ยนประเภทแผนไม่ได้',
  DATA_CHANGE_AFTER_USE: 'รายการที่ใช้แล้วแก้ได้เฉพาะตัวเลข ถ้าจะเปลี่ยนข้อมูล ให้ลดเหลือเท่าที่ใช้แล้วเพิ่มรายการใหม่',
  NEGATIVE_VALUE: 'จำนวน ราคา และวงเงินติดลบไม่ได้',
  BELOW_USED_QTY: 'จำนวนต่ำกว่าที่ใช้ไปแล้ว',
  BELOW_USED_AMOUNT: 'วงเงินต่ำกว่าที่ใช้ไปแล้ว',
  ZERO_QTY_WITH_AMOUNT: 'จำนวนเป็นศูนย์ วงเงินต้องเป็นศูนย์ด้วย',
  NEW_ITEM_ZERO_QTY: 'รายการใหม่ต้องมีจำนวนมากกว่าศูนย์',
  DEMAND_NOT_ASSIGNED: 'ความต้องการรายหน่วยงานใช้กับแผนซื้อแทน (ASSIGNED) เท่านั้น',
  BELOW_COVERED_DEMAND: 'ลดความต้องการต่ำกว่ายอดที่ PPR ที่ยังมีผลครอบคลุมไว้ไม่ได้ ต้องแก้ไขหรือยกเลิก PPR ที่เกี่ยวข้องก่อน (D-38)',
  NEGATIVE_BUDGET: 'วงเงินหมวดงบติดลบไม่ได้',
  CENTRAL_WITHOUT_PURCHASER: 'แผนกลางต้องระบุหน่วยงานผู้ซื้อ (คลัง) (D-38)',
  DUPLICATE_MATCH_KEY: 'รายการเดียวกันซ้ำระหว่างแผนหน่วยงานและแผนกลางของหน่วยงาน/แหล่งเงินเดียวกัน PR จะจับคู่ไม่ได้ (D-38)',
  ASSIGNED_PURCHASER_CHANGED: 'แผนซื้อแทนเปลี่ยนหน่วยงานผู้ซื้อไม่ได้ (ยอดที่ซื้อแทนผูกกับ PPR ของผู้ซื้อเดิม) (D-38)',
  PLAN_CHANGED_SINCE_FORM: 'แผนถูกปรับโดยผู้อื่นหลังจากเปิดแบบฟอร์มนี้ กรุณาเปิดแบบฟอร์มปรับแผนใหม่',
  ITEM_WITHOUT_BUDGET: 'รายการแผนไม่มีวงเงินของแหล่งเงิน/หมวดงบนี้',
  DUPLICATE_DEPARTMENT_ITEM: 'รายการเดียวกันซ้ำในแผนหน่วยงาน/แหล่งเงินเดียวกัน (D-16)',
  ASSIGNED_WITHOUT_DEMAND: 'แผนซื้อแทนต้องระบุความต้องการรายหน่วยงาน',
  ASSIGNED_WITHOUT_PURCHASER: 'แผนซื้อแทนต้องระบุหน่วยงานผู้ซื้อ',
  AMOUNT_DIFFERS_FROM_ESTIMATE: 'ยอดเงินต่างจาก จำนวน × ราคาประมาณ (อนุญาต)',
  DEMAND_TOTAL_DIFFERS: 'ผลรวมความต้องการรายหน่วยงานต่างจากจำนวนในแผน',
  QUARTERS_DIFFER: 'ไตรมาส 1–4 รวมไม่เท่าจำนวนหรือยอดเงิน (ติดตามเท่านั้น)',
  // Wave 8B (D-35)
  REPORT_NOT_FOUND: 'ไม่พบรายงานนี้',
  FISCAL_YEAR_REQUIRED: 'รายงานแผนต้องระบุปีงบประมาณ',
  INVALID_DATE_RANGE: 'วันที่สิ้นสุดอยู่ก่อนวันที่เริ่ม',
  STATE_NOT_IN_REPORT: 'สถานะนี้ไม่อยู่ในรายงานนี้',
};

const SYNC_OUTCOMES = {
  UNCHANGED: 'ไม่เปลี่ยน',
  NON_CRITICAL_CHANGE: 'เปลี่ยนเล็กน้อย (ไม่กระทบการควบคุม)',
  CRITICAL_CHANGE: 'เปลี่ยนในจุดสำคัญ → ต้องทบทวน',
  IDENTITY_CHANGE: 'ปีงบ/หน่วยงาน/แหล่งเงินเปลี่ยน → ต้องทบทวน',
  INVALID_EVIDENCE: 'ข้อมูลจาก HOSxP ใช้ไม่ได้',
  CANCELLED: 'PR ถูกยกเลิกใน HOSxP',
  NOT_FOUND: 'ไม่พบ PR ใน HOSxP (ไม่ใช่การยกเลิก)',
  UNAVAILABLE: 'ติดต่อ HOSxP ไม่ได้',
  SKIPPED_STATE_MOVED: 'ข้าม: PPR เปลี่ยนระหว่างอ่าน HOSxP',
  ERROR: 'ผิดพลาดระหว่างบันทึก (ไม่มีอะไรเปลี่ยน)',
  ANOMALY: 'ผิดปกติ: PR ที่ยกเลิกแล้วกลับมาใช้งานใน HOSxP',
  NO_AUTHORITATIVE_STATUS: 'HOSxP ไม่มีสถานะที่ใช้ตัดสินได้',
};

const EVIDENCE_CLASSES = {
  BINDING: 'ผูกกับ PR ไม่ได้แน่นอน — สถานะ PPR ไม่เปลี่ยน',
  STATUS: 'สถานะ PR เชื่อถือไม่ได้ — สถานะ PPR ไม่เปลี่ยน',
  CONTROL: 'ข้อมูล PR ไม่ผ่านกฎควบคุม (D-17/D-23)',
};

const RUN_STATUS = {
  RUNNING: 'กำลังทำงาน',
  SUCCEEDED: 'สำเร็จ',
  PARTIAL: 'สำเร็จบางส่วน',
  FAILED: 'ไม่สำเร็จ',
};

const RUN_SCOPES = {
  MASTERS: 'ข้อมูลหลัก (หน่วยงาน สินค้า แหล่งเงิน หมวดงบ)',
  PPRS: 'PPR ที่ยืนยันแล้วทั้งหมด',
  PPR: 'PPR เดียว',
  PPR_RETRY: 'ทำซ้ำรายการที่ไม่สำเร็จ',
  PPR_RECHECK: 'ตรวจซ้ำ PPR ที่ยกเลิกแล้ว',
};

const RUN_MODES = { MANUAL: 'สั่งด้วยมือ', NIGHTLY: 'ตามตารางเวลา' };

const EVENTS = {
  PPR_DRAFT_CREATED: 'สร้างฉบับร่าง',
  PLAN_IMPORTED: 'นำเข้าแผนจาก Excel',
  PLAN_IMPORT_REMOVED: 'ยกเลิกการนำเข้าแผน',
  PPR_ALLOCATION_UPDATED: 'แก้การแบ่งหมวดงบ',
  PPR_DRAFT_DISCARDED: 'ยกเลิกฉบับร่าง',
  PPR_CONFIRMED: 'ยืนยันขอใช้แผนและล็อก',
  PPR_RECONFIRMED: 'ยืนยันอีกครั้ง (ฉบับใหม่)',
  PPR_UNLOCKED: 'ปลดล็อกเพื่อแก้ไข',
  PPR_VERIFIED: 'หัวหน้าพัสดุ: ตรวจสอบและยืนยันแล้ว',
  PPR_COVERAGE_UPDATED: 'แก้ยอดที่ซื้อให้แต่ละหน่วยงาน (แผนซื้อแทน)',
  DEMAND_STATE_CHANGED: 'สถานะความต้องการของหน่วยงานเปลี่ยน (แผนซื้อแทน)',
  PPR_PRINT_VIEWED: 'เปิดหน้าพิมพ์ PPR',
  PPR_PR_CHANGED: 'ระบบ: ย้ายไป "PR มีการเปลี่ยนแปลง ต้องทบทวน"',
  PPR_CANCELLED_FROM_HOSXP: 'ระบบ: ยกเลิก PPR และคืนแผนที่ใช้ไว้',
  HOSXP_NON_CRITICAL_CHANGE: 'พบใน HOSxP: PR เปลี่ยนเล็กน้อย',
  HOSXP_CRITICAL_CHANGE: 'พบใน HOSxP: PR เปลี่ยนในจุดสำคัญ',
  HOSXP_IDENTITY_CHANGE: 'พบใน HOSxP: ปีงบ/หน่วยงาน/แหล่งเงินของ PR เปลี่ยน',
  HOSXP_INVALID_EVIDENCE: 'พบใน HOSxP: ข้อมูล PR ใช้ไม่ได้',
  HOSXP_CANCELLED: 'พบใน HOSxP: PR ถูกยกเลิก',
  HOSXP_NOT_FOUND: 'พบใน HOSxP: ไม่พบ PR',
  HOSXP_ANOMALY: 'พบใน HOSxP: PR ที่ยกเลิกแล้วกลับมาใช้งาน (ผิดปกติ)',
  HOSXP_NO_AUTHORITATIVE_STATUS: 'พบใน HOSxP: ไม่มีสถานะที่ใช้ตัดสินได้',
};

// Wave 8A (D-33, D-34)
const ALERT_TYPES = {
  PPR_INACTIVE: 'PPR ไม่มีความเคลื่อนไหว',
  PR_CHANGED: 'PR เปลี่ยน ต้องทบทวน',
  PLAN_LOW: 'แผนใกล้หมด',
  PLAN_EXHAUSTED: 'แผนหมดแล้ว',
  SYNC_FAILURE: 'ซิงค์กับ HOSxP ไม่สำเร็จ',
};

const ALERT_SETTINGS = {
  PPR_INACTIVE_DAYS: { name: 'PPR ไม่มีความเคลื่อนไหวเกิน', unit: 'วัน' },
  PLAN_LOW_PERCENT: { name: 'แผนใกล้หมดเมื่อเหลือไม่เกิน', unit: '%' },
};

const PLAN_YEAR_STATES = {
  DRAFT: 'ร่างแผน',
  APPROVED: 'อนุมัติแล้ว (ยังไม่เปิดใช้)',
  ACTIVE: 'เปิดใช้งาน',
  CLOSED: 'ปิดปีงบแล้ว',
};

// The one next step of each plan-year state (DRAFT → APPROVED → ACTIVE → CLOSED); the API decides.
const PLAN_YEAR_NEXT = {
  DRAFT: { target: 'APPROVED', text: 'บันทึกว่าแผนได้รับอนุมัติ' },
  APPROVED: { target: 'ACTIVE', text: 'เปิดใช้แผน (ตรวจแผนทั้งปีก่อน, D-12)' },
  ACTIVE: { target: 'CLOSED', text: 'ปิดปีงบ (ย้อนกลับไม่ได้)' },
};

const PLAN_TYPES = {
  DEPARTMENT: 'แผนหน่วยงาน',
  CENTRAL: 'แผนกลาง',
  ASSIGNED: 'แผนซื้อแทน',
};

const MASTER_KINDS_TEXT = {
  DEPARTMENT: 'หน่วยงาน', ITEM: 'สินค้า/พัสดุ', FUND_SOURCE: 'แหล่งเงิน', BUDGET_CATEGORY: 'หมวดงบ',
};

const DEMO_NOTE = 'โหมดทดลอง (D-26): ข้อมูล HOSxP จำลอง ห้ามใช้กับข้อมูลจริง';

const AMEND_CHANGES = {
  ITEM_UPDATE: 'แก้ไขรายการแผน',
  ITEM_CREATE: 'เพิ่มรายการแผนใหม่',
  BUDGET_UPDATE: 'แก้วงเงินหมวดงบ',
  BUDGET_CREATE: 'เพิ่มหมวดงบใหม่',
  DEMAND_UPDATE: 'แก้ความต้องการของหน่วยงาน',
  DEMAND_CREATE: 'เพิ่มความต้องการของหน่วยงาน',
};
// D-38: states of a department's demand on an Assigned Purchase item (derived, never typed).
const DEMAND_STATES = {
  PLANNED: 'รอซื้อ',
  INCLUDED_IN_CENTRAL_PURCHASE: 'อยู่ในใบที่ยืนยันแล้ว',
  FULFILLED: 'พัสดุตรวจแล้ว',
  CANCELLED: 'ยกเลิกความต้องการ',
};
const AMEND_FIELDS = {
  item_id: 'รายการ',
  item_name: 'ชื่อรายการ',
  unit: 'หน่วยนับ',
  owner_department_id: 'หน่วยงานเจ้าของแผน',
  purchasing_department_id: 'หน่วยงานผู้ซื้อ',
  budget_category_id: 'หมวดงบ',
};
const AMEND_FIELD_KINDS = {
  item_id: 'ITEM',
  owner_department_id: 'DEPARTMENT',
  purchasing_department_id: 'DEPARTMENT',
  budget_category_id: 'BUDGET_CATEGORY',
};

function codeText(code, fallback) {
  return CODES[code] || fallback || code;
}

module.exports = {
  STATES, ROLES, CODES, EVENTS, DEMO_NOTE, codeText, AMEND_CHANGES, DEMAND_STATES, AMEND_FIELDS, AMEND_FIELD_KINDS,
  SYNC_OUTCOMES, EVIDENCE_CLASSES, RUN_STATUS, RUN_SCOPES, RUN_MODES,
  ALERT_TYPES, ALERT_SETTINGS, PLAN_YEAR_STATES, PLAN_YEAR_NEXT, PLAN_TYPES, MASTER_KINDS_TEXT,
};
