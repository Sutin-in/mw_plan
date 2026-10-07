# ติดตั้งใช้งานจริงบน Windows Server — Production deployment guide (Wave 10A)

สำหรับฝ่าย IT ของโรงพยาบาล เอกสารนี้ไม่ผูกกับผลิตภัณฑ์ใดเป็นพิเศษ (IIS, nginx, Apache, HAProxy,
อุปกรณ์ load balancer ฯลฯ) — ระบุเฉพาะ **สิ่งที่ต้องมี** ส่วนการตั้งค่าจริงให้ IT เลือกตามโครงสร้างของโรงพยาบาล

> ห้ามเก็บรหัสผ่าน ใบรับรอง (certificate) หรือ private key ใด ๆ ไว้ใน repository —
> ค่าลับอยู่ในไฟล์ `.env` บนเครื่อง server เท่านั้น (git-ignored) และใน certificate store ของ IT

ขั้นตอนเชื่อม HOSxP (บัญชีอ่านอย่างเดียว, `hosxp_queries.toml`) อยู่ใน `docs/HOSXP_CONNECTION.md`
ลำดับงานส่งมอบทั้งหมด (ตรวจ `hosxp-verify`, `first-start --check`, เปิดครั้งแรก, checklist) อยู่ใน `docs/IT_HANDOFF.md` และ `docs/GO_LIVE_CHECKLIST.md`

## 1. ภาพรวม

```text
ผู้ใช้ (browser) ──HTTPS──> reverse proxy ของโรงพยาบาล ──HTTP (เครื่องเดียวกัน/LAN ที่จำกัด)──> UI :3000
                                                                               │
                                                                    127.0.0.1 │ เท่านั้น
                                                                               ▼
                         HOSxP (อ่านอย่างเดียว) <── API :8000 ──> PostgreSQL 16 (ppr)
```

* **API** (Python) ฟังที่ `127.0.0.1` เท่านั้น — production profile ไม่ยอมเปิดที่ address อื่น
  ผู้ใช้ไม่เข้าถึง API โดยตรง มีเพียง UI บนเครื่องเดียวกันที่เรียก
* **UI** (Node.js) คือสิ่งเดียวที่ reverse proxy ส่งต่อไป
* **HTTPS สิ้นสุดที่ reverse proxy** ระบบไม่ถือ certificate เอง

## 2. บัญชีฐานข้อมูล (แยกเจ้าของ schema กับบัญชีที่ระบบใช้งาน)

| บัญชี | ใช้ทำอะไร | สิทธิ์ |
|---|---|---|
| `ppr_owner` (ชื่อเลือกได้) | `migrate` และ `backup` เท่านั้น | เจ้าของ database และทุกตาราง |
| `ppr_runtime` (ชื่อเลือกได้) | ระบบใช้งานประจำวัน (`PPR_DATABASE_URL`) | อ่าน/เขียนแถว; ตารางหลักฐาน (audit, ledger, ฉบับ PPR, การผูก PR, การตรวจของพัสดุ, ผลซิงค์, ประวัติปรับแผน) ได้แค่ SELECT และ INSERT; ไม่เป็นเจ้าของอะไร สร้างหรือแก้ schema ไม่ได้ ปิด trigger ไม่ได้ |

สร้างครั้งเดียวโดย DBA (psql ในฐานะ superuser — ตั้งรหัสผ่านเองแทน `<...>`):

```sql
CREATE ROLE ppr_owner   LOGIN PASSWORD '<รหัสยาว>';
CREATE ROLE ppr_runtime LOGIN PASSWORD '<รหัสยาวอีกชุด>';
CREATE DATABASE ppr OWNER ppr_owner;
```

แล้วรัน migrate (ใช้บัญชี owner และ grant สิทธิ์ให้ runtime ให้อัตโนมัติ — รันซ้ำทุกครั้งที่อัปเดตระบบ):

```bat
cd C:\ppr\backend
..\.venv\Scripts\python -m ppr.cli migrate
```

ระบบตรวจตอนเริ่มทำงานทุกครั้ง: ถ้า `PPR_DATABASE_URL` เป็นบัญชีที่มีสิทธิ์เกิน (เช่น owner หรือ
superuser) API จะไม่ยอมเริ่ม

## 3. ไฟล์ `.env` บน server (ตัวอย่างครบอยู่ใน `.env.example`)

```ini
PPR_PROFILE=production
PPR_DATABASE_URL=postgresql+psycopg://ppr_runtime:<รหัส>@localhost:5432/ppr
PPR_MIGRATION_DATABASE_URL=postgresql+psycopg://ppr_owner:<รหัส>@localhost:5432/ppr
PPR_RUNTIME_ROLE=ppr_runtime
PPR_SESSION_SECRET=<ค่าสุ่มอย่างน้อย 32 ตัวอักษร>
PPR_HOSXP_MODE=sql
PPR_HOSXP_DATABASE_URL=<บัญชี HOSxP อ่านอย่างเดียว>
PPR_HOSXP_AUTH_DATABASE_URL=<บัญชีอ่านอย่างเดียวของ server login HOSxP ถ้าแยก server (D-41) ไม่ต้องใส่ถ้าเป็นฐานเดียวกัน>
PPR_UI_SECURE_COOKIES=1
PPR_UI_TRUST_PROXY=loopback        ; หรือ IP ของ reverse proxy ถ้าอยู่คนละเครื่อง
PPR_BACKUP_DIR=D:\ppr-backups      ; ที่เก็บไฟล์สำรอง (นอกโฟลเดอร์ระบบ)
```

* ไฟล์ `.env` ให้สิทธิ์อ่านเฉพาะบัญชีที่รันระบบและผู้ดูแล
* production ไม่ยอม: โหมดทดลอง (demo), ฐานข้อมูลชื่อ `*_dev` / `*_test`, UI ที่ไม่ตั้ง secure
  cookies หรือไม่ระบุ proxy, UI ที่ชี้ไป API นอกเครื่อง

## 4. HTTPS reverse proxy — สิ่งที่ต้องมี (ไม่ระบุยี่ห้อ)

1. **ใบรับรอง TLS** ที่ browser ของผู้ใช้เชื่อถือ (CA ของโรงพยาบาลหรือ CA สาธารณะ) เก็บใน certificate
   store ของ IT ไม่เก็บใน repository; ต่ออายุก่อนหมด
2. **TLS 1.2 ขึ้นไป** ปิดโปรโตคอล/cipher ที่ล้าสมัย
3. **HTTP → HTTPS redirect** (301/308) ทุก path และควรตั้ง `Strict-Transport-Security`
4. **ส่งต่อเฉพาะไปที่ UI** (`http://127.0.0.1:3000` หรือที่ IT กำหนด) — ห้ามเปิด path หรือ port
   ที่ไปถึง API (`:8000`) จากภายนอก
5. **Forwarding headers**: `X-Forwarded-Proto: https`, `X-Forwarded-For` (IP ผู้ใช้จริง) และคง `Host`
   เดิม; UI ใช้ IP นี้ใน access log (`logs\ui-*.log`) และเชื่อ header เหล่านี้เฉพาะจาก proxy ที่ระบุใน
   `PPR_UI_TRUST_PROXY` (ผู้ใช้ปลอม header เองไม่ได้)
   * proxy อยู่เครื่องเดียวกัน: `PPR_UI_TRUST_PROXY=loopback` และ UI ต้องฟังที่ `127.0.0.1`
     (ค่าเริ่มต้น) — production ไม่ยอมให้ UI เปิดที่ address อื่นในกรณีนี้
   * proxy อยู่คนละเครื่อง: `PPR_UI_TRUST_PROXY=<IP ของ proxy>`, `PPR_UI_HOST=<IP ของ server>`
     และ firewall ต้องให้เฉพาะ proxy เข้าพอร์ต UI
6. **ขนาด request** อย่างน้อย 2 MB (ฟอร์มปรับแผน) และ **timeout** อย่างน้อย 120 วินาที (การซิงค์ทั้งชุด)
7. **Firewall**: เปิดจากภายนอกเฉพาะพอร์ต HTTPS ของ proxy; พอร์ต UI เปิดให้เฉพาะ proxy;
   พอร์ต API และ PostgreSQL ไม่เปิดออกนอกเครื่อง
8. ไม่ต้องเพิ่ม header ความปลอดภัยของหน้าเว็บ — UI ส่ง `Content-Security-Policy`,
   `X-Frame-Options: DENY`, `X-Content-Type-Options`, `Referrer-Policy`, `Cache-Control: no-store` เอง

ตรวจหลังตั้งค่า: เปิด `https://<ชื่อระบบ>/healthz?deep=1` ได้ `{"status":"ok","api":"ok"}`,
เปิด `http://` แล้วถูกพาไป `https://`, และเรียก `https://<ชื่อระบบ>:8000` จากเครื่องอื่นต้องไม่ได้

## 5. ให้ระบบรันเองอัตโนมัติ (Windows Task Scheduler)

รันครั้งเดียวในฐานะ Administrator (ทดลองดูก่อนด้วย `-PrintOnly`):

```bat
powershell -ExecutionPolicy Bypass -File tools\ops\register_tasks.ps1 -RunAsUser HOSP\svc_ppr -NightlyTime 01:30 -BackupTime 03:00
```

| Task (`\PPR\`) | เมื่อไร | ทำอะไร |
|---|---|---|
| PPR API | ทุกครั้งที่เครื่องเปิด (หน่วง 1 นาที) + เฝ้าระวังทุก 5 นาที | API ภายใต้ supervisor |
| PPR UI | ทุกครั้งที่เครื่องเปิด (หน่วง 1 นาที) + เฝ้าระวังทุก 5 นาที | UI ภายใต้ supervisor |
| PPR Nightly Sync | ทุกวัน ตามเวลาที่ IT เลือก (ไม่ชนงานสำรองหรือ maintenance ของ HOSxP) | ซิงค์ PPR กับ PR ใน HOSxP |
| PPR Backup | ทุกวัน ตามเวลาที่ IT เลือก | สำรองฐานข้อมูล + manifest |

* `-RunAsUser` คือบัญชี Windows สำหรับรันระบบ (IT กำหนด) ต้องอ่านโฟลเดอร์ระบบได้ และเขียน
  `logs\`, `run\` และ `PPR_BACKUP_DIR` ได้ รหัสผ่านถูกถามตอนรันและส่งให้ Task Scheduler เท่านั้น
* **การกู้เมื่อพัง:** supervisor (`tools\ops\supervise.py`) เริ่ม service ใหม่เมื่อ process ตาย
  (รอ 5 วินาที เพิ่มเป็น 60 วินาที) และเมื่อ health check ไม่ตอบ 3 ครั้งติด (ห่างกัน 30 วินาที);
  ตัว supervisor ถูก Task Scheduler เริ่มหลัง reboot ทุกครั้ง และ trigger เฝ้าระวังทุก 5 นาทีจะเริ่มมันใหม่
  ถ้ามันหยุดไป (ถ้ายังรันอยู่ trigger นั้นไม่ทำอะไร); มี supervisor ได้ตัวเดียวต่อ service (OS lock)
* **หยุดเพื่อบำรุงรักษา** (ต้องปิด task ก่อน มิฉะนั้น trigger 5 นาทีจะเริ่มให้ใหม่):
  ```bat
  schtasks /Change /TN "\PPR\PPR API" /DISABLE  &  schtasks /Change /TN "\PPR\PPR UI" /DISABLE
  .venv\Scripts\python tools\ops\supervise.py --stop api  &  .venv\Scripts\python tools\ops\supervise.py --stop ui
  ```
  **เริ่มใหม่:**
  ```bat
  schtasks /Change /TN "\PPR\PPR API" /ENABLE  &  schtasks /Change /TN "\PPR\PPR UI" /ENABLE
  schtasks /Run /TN "\PPR\PPR API"  &  schtasks /Run /TN "\PPR\PPR UI"
  ```

## 6. ตรวจว่าระบบทำงานจริง

```bat
powershell -ExecutionPolicy Bypass -File tools\ops\status.ps1
```

ตรวจ: task ทั้ง 4 มีอยู่และผลรอบล่าสุด, `http://127.0.0.1:8000/api/health` (ฐานข้อมูลตอบ),
`http://127.0.0.1:3000/healthz?deep=1` (UI ตอบและถึง API), และ `ppr.cli ops-status`
(schema ล่าสุด, บัญชี runtime สิทธิ์น้อยที่สุด, ซิงค์กลางคืนสำเร็จภายใน 26 ชั่วโมง)
ระบบ monitoring ของโรงพยาบาลใช้สอง URL นี้และ exit code ของ `ops-status` ได้

**ข้อมูลทดลอง MOCK/DEMO (D-40):** ถ้าฐานข้อมูลระบบจริงมีข้อมูลทดลองปน `ops-status` จะแสดงบรรทัด `WARN`
(ไม่ทำให้ผลเป็น FAIL) และ `status.ps1` จะสรุปว่า `status: PASS with n warning(s)` ตอนเปิด API จะมีบรรทัด
`WARNING` และทุกหน้าจอจะแสดงคำเตือน รายการ MOCK จะไม่ถูกลงบัญชีแผน ทางที่ถูกคือใช้ฐานข้อมูลที่สะอาด

**Log:** `logs\<api|ui|nightly|backup|status>-YYYYMMDD.log` (เวลาเครื่อง, ลบไฟล์ที่เก่ากว่า 90 วัน
วันละครั้ง — `PPR_LOG_KEEP_DAYS`) มีทั้งข้อความของ service (UI: หนึ่งบรรทัดต่อ request — method, path
ไม่มี query string, status, เวลา, IP) และเหตุการณ์ของ supervisor (เริ่ม, ตาย, เริ่มใหม่, health check ไม่ผ่าน)

## 7. สำรองและกู้คืนข้อมูล (spec §38.3)

* `ppr.cli backup --out <โฟลเดอร์>` (task PPR Backup ทำให้ทุกวัน) ได้ 2 ไฟล์:
  `ppr-YYYYMMDD-HHMMSS.dump` (pg_dump แบบ custom) และ `.manifest.json` (จำนวนแถวและ hash ของทุกตาราง,
  sequence, trigger — ไม่มีข้อมูลรายการ) ทั้งสองไฟล์มาจาก snapshot เดียวกัน แม้มีผู้ใช้ทำงานอยู่
* การคัดลอกไฟล์สำรองออกนอกเครื่องและระยะเวลาเก็บ เป็นไปตามนโยบายสำรองข้อมูลของโรงพยาบาล
* **กู้คืน** (ลงฐานข้อมูลว่างที่ DBA สร้างให้บน server เดียวกัน เช่น `ppr_restore` เจ้าของคือ `ppr_owner`;
  ใช้บัญชีของ `PPR_MIGRATION_DATABASE_URL` จึงไม่ต้องพิมพ์รหัสผ่านใน command line):
  ```bat
  ..\.venv\Scripts\python -m ppr.cli restore --dump D:\ppr-backups\ppr-20261001-030000.dump --target-db ppr_restore
  ..\.venv\Scripts\python -m ppr.cli verify-restore --manifest D:\ppr-backups\ppr-20261001-030000.manifest.json --target-db ppr_restore
  ```
  `verify-restore: PASS` = ทุกตาราง (รวม ledger ที่ใช้คำนวณแผนคงเหลือ, audit, ฉบับ PPR), sequence
  (เลข PPR ต่อจากเดิม) และ trigger ป้องกันหลักฐาน ตรงกับตอนสำรองทุกประการ
  ระบบไม่ยอมกู้ทับฐานข้อมูลที่กำลังใช้งาน (ตรวจจากตัว server จริง ไม่ใช่แค่ชื่อในการเชื่อมต่อ) และไม่ยอม
  ทับฐานข้อมูลที่มีข้อมูลอยู่ (ยกเว้นชื่อ `*_test` / `*_drill` / `*_restore` พร้อม `--replace`; ใน
  production ไม่ยอม `--replace` ฐานที่ `.env` ชี้อยู่เด็ดขาด)
* ถ้าจะใช้ฐานที่กู้แทนของเดิม (กรณีเสียหาย): หยุด API/UI (หัวข้อ 5), DBA เปลี่ยนชื่อ
  (`ALTER DATABASE ppr RENAME TO ppr_damaged; ALTER DATABASE ppr_restore RENAME TO ppr;`) เพื่อไม่ให้
  ฐานที่ใช้งานจริงชื่อ `*_restore`, รัน `ppr.cli migrate` (ให้สิทธิ์ runtime), เริ่ม API/UI, ตรวจด้วย `status.ps1`
* **ซ้อมกู้คืน** อย่างน้อยไตรมาสละครั้ง และก่อนเปิดใช้งานจริง (ขั้นข้างบน) — การมีแค่ไฟล์สำรองไม่พอ
* ทดลองทั้งชุด (แยกบัญชี, production refusal, restart, backup/restore) บนเครื่องทดสอบได้ด้วย
  `tools\dev\drill_production.py` (ใช้ฐานข้อมูลชื่อ `ppr_drill*` ชั่วคราว ลบทิ้งเมื่อจบ)

## 8. อัปเดตระบบ

1. หยุด: ปิด task แล้ว `--stop` (หัวข้อ 5)
2. สำรอง: `supervise.py backup`
3. ดึงเวอร์ชันใหม่ (`git fetch` + `git checkout <tag>`), `pip install -e backend`, `npm ci` ใน `frontend`
4. `ppr.cli migrate` (owner + grant runtime)
5. เปิด task และเริ่มอีกครั้ง (หัวข้อ 5) แล้วตรวจด้วย `status.ps1`
