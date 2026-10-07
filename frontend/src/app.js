'use strict';
/**
 * Express user interface (D-24): server-rendered Thai pages over the Python API.
 *
 * Rules kept here:
 *  - no business rule and no permission decision: every action is an API call and the
 *    API's answer (or refusal) is shown as it is; buttons are only hints (spec §22.9);
 *  - the API token lives in a signed, HTTP-only, SameSite=Strict cookie; page scripts
 *    never see it; every form carries a CSRF token derived from it;
 *  - roles are re-read from the API on every request (a revoked role takes effect at once).
 */

const crypto = require('node:crypto');
const path = require('node:path');
const express = require('express');
const cookieParser = require('cookie-parser');
const { Api, ApiError } = require('./api');
const labels = require('./labels');
const { multipart } = require('./multipart');

const TOKEN_COOKIE = 'ppr_token';
const PRE_COOKIE = 'ppr_pre';
const FLASH_COOKIE = 'ppr_flash';
const MASTER_KINDS = ['DEPARTMENT', 'BUDGET_CATEGORY', 'FUND_SOURCE', 'ITEM'];
// Role hints for showing links and buttons only; the API decides (spec §22.9).
const QUEUE_ROLES = ['PROCUREMENT', 'HEAD_OF_PROCUREMENT', 'PLAN_OFFICER', 'ADMIN', 'CFO', 'EXECUTIVE'];
const APPOINTMENT_READ_ROLES = ['ADMIN', 'PLAN_OFFICER', 'PROCUREMENT', 'HEAD_OF_PROCUREMENT'];
const SYNC_RUN_ROLES = ['PLAN_OFFICER', 'ADMIN'];
const SYNC_READ_ROLES = ['PLAN_OFFICER', 'ADMIN', 'PROCUREMENT', 'HEAD_OF_PROCUREMENT'];
// States a PPR may be unlocked from (state machine UNLOCK sources); the API decides.
const UNLOCKABLE = ['CONFIRMED_LOCKED', 'PROCUREMENT_VERIFIED', 'PR_CHANGED_REVIEW_REQUIRED'];
// States the PR synchronization looks at (D-29); DRAFT never, CANCELLED only by re-check.
const SYNC_STATES = ['CONFIRMED_LOCKED', 'PROCUREMENT_VERIFIED', 'PR_CHANGED_REVIEW_REQUIRED', 'UNLOCKED_FOR_REVISION'];
const SEARCH_KEYS = [
  'pr_no', 'ppr_number', 'requester', 'department_id', 'item', 'budget_category_id',
  'fund_source_id', 'fiscal_year', 'state', 'created_from', 'created_to',
];
const QUEUE_BUCKETS = ['waiting', 'verified', 'changed', 'unlocked', 'cancelled', 'inactive'];
// Wave 8A hints (D-33, D-34); the API decides.
const PLAN_MANAGE_ROLES = ['PLAN_OFFICER'];
const PLAN_TYPES = ['DEPARTMENT', 'CENTRAL', 'ASSIGNED'];
const ALERT_SETTING_CODES = ['PPR_INACTIVE_DAYS', 'PLAN_LOW_PERCENT'];
const DEMAND_ROWS = 6;
// Wave 7A (D-37): Plan Amendment form. The API judges every value; the form only collects.
const PLAN_AMEND_ROLES = ['PLAN_OFFICER'];
const AMEND_NEW_ITEM_ROWS = 3;
const AMEND_NEW_BUDGET_ROWS = 2;
const AMEND_MAX_ROWS = 300;
const AMEND_TYPES = ['DEPARTMENT', 'CENTRAL', 'ASSIGNED'];
const AMEND_NEW_DEMAND_ROWS = 4; // demand rows per new ASSIGNED item (D-38 A-6)
const AMEND_PATH = /^\/plans\/\d{4}\/amendments(\/preview)?$/;
// Wave 12A-1 (D-42): plan import upload
const IMPORT_UPLOAD_PATH = /^\/plans\/\d{4}\/import\/preview$/;
const IMPORT_MAX_BYTES = 10 * 1024 * 1024;
const IMPORT_PENDING_TTL_MS = 30 * 60_000;
const IMPORT_PENDING_MAX = 20;
// Wave 8B (D-35): report filters forwarded to the API (it validates and scopes them).
const REPORT_KEYS = {
  fiscal_year: /^[0-9]{4}$/,
  department_id: /^.{1,100}$/,
  fund_source_id: /^.{1,100}$/,
  budget_category_id: /^.{1,100}$/,
  item: /^.{1,100}$/,
  state: /^[A-Z_]{3,40}$/,
  date_from: /^\d{4}-\d{2}-\d{2}$/,
  date_to: /^\d{4}-\d{2}-\d{2}$/,
  action: /^[A-Za-z_]{1,60}$/,
  entity_type: /^[A-Za-z_]{1,60}$/,
};
function reportParams(query) {
  const out = new URLSearchParams();
  for (const [key, re] of Object.entries(REPORT_KEYS)) {
    const raw = query[key];
    const value = String(Array.isArray(raw) ? raw[0] : raw || '').trim();
    if (!value) continue;
    if (!re.test(value)) { out.set('_dropped', '1'); continue; }
    // Audit codes are stored as ACTION_CODE / entity_type; accept either case.
    out.set(key, key === 'action' ? value.toUpperCase() : key === 'entity_type' ? value.toLowerCase() : value);
  }
  return out;
}
// D-43: a plan-row choice travels as "pr_item_id:plan_item_id" (a select value or a hidden
// input); anything else is dropped here and the API re-checks every choice.
function parseChoices(raw) {
  const out = {};
  for (const v of [].concat(raw || []).slice(0, 500)) {
    const m = /^(.{1,100}):([0-9]{1,12})$/.exec(String(v));
    if (m) out[m[1]] = m[2];
  }
  return out;
}

function choiceSearch(prNo, chosen) {
  const q = new URLSearchParams({ pr_no: prNo });
  for (const [line, plan] of Object.entries(chosen)) q.append('choice', `${line}:${plan}`);
  return q.toString();
}

const reportCodeOk = (code) => /^[A-Z_]{3,40}$/.test(String(code || ''));
const LOOKUP_MAX = 50;

function searchParams(query) {
  const out = new URLSearchParams();
  for (const key of SEARCH_KEYS) {
    const raw = query[key];
    const value = String(Array.isArray(raw) ? raw[0] : raw || '').trim();
    if (!value) continue;
    if (key === 'fiscal_year' && !/^[0-9]{4}$/.test(value)) continue;
    if ((key === 'created_from' || key === 'created_to') && !/^\d{4}-\d{2}-\d{2}$/.test(value)) continue;
    if (key === 'state' && !/^[A-Z_]{3,40}$/.test(value)) continue;
    out.set(key, value.slice(0, 100));
  }
  return out;
}
const MASTER_TTL_MS = 60_000;
const FLASH_TTL_MS = 60_000;
const FLASH_MAX = 2000;

function hmac(secret, text) {
  return crypto.createHmac('sha256', secret).update(text).digest('hex');
}

function safeEqual(a, b) {
  const x = Buffer.from(String(a || ''));
  const y = Buffer.from(String(b || ''));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

const money = new Intl.NumberFormat('th-TH', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const qtyFmt = new Intl.NumberFormat('th-TH', { maximumFractionDigits: 4 });
const dateFmt = new Intl.DateTimeFormat('th-TH', { dateStyle: 'medium', timeStyle: 'short', timeZone: 'Asia/Bangkok' });

function fmtMoney(v) { return v === null || v === undefined || v === '' ? '-' : money.format(Number(v)); }
function fmtQty(v) { return v === null || v === undefined || v === '' ? '-' : qtyFmt.format(Number(v)); }
function fmtDate(v) { return v ? dateFmt.format(new Date(v)) : '-'; }

function createApp(config) {
  const {
    apiBase, cookieSecret, secureCookies = false, apiTimeoutMs = 30000, trustProxy = null,
    accessLog = false,
  } = config;
  if (!apiBase) throw new Error('apiBase is required');
  if (!cookieSecret || cookieSecret.length < 32) {
    throw new Error('the cookie secret must be at least 32 characters');
  }

  const app = express();
  app.disable('x-powered-by');
  // Behind an HTTPS reverse proxy: trust its X-Forwarded-* headers (only from that proxy).
  if (trustProxy) app.set('trust proxy', trustProxy);

  // One line per request for the operations log: method, path (no query string), status,
  // duration and the client address (the real one behind the trusted proxy). No user data.
  if (accessLog) {
    app.use((req, res, next) => {
      const t0 = process.hrtime.bigint();
      res.on('finish', () => {
        const ms = Number(process.hrtime.bigint() - t0) / 1e6;
        console.log(`${req.method} ${req.path} ${res.statusCode} ${ms.toFixed(0)}ms ${req.ip}`);
      });
      next();
    });
  }

  // Liveness for the supervisor and monitoring: answers without touching the API.
  // ?deep=1 also asks the API (and its database): 503 when they do not answer.
  app.get('/healthz', async (req, res) => {
    res.set('Cache-Control', 'no-store');
    if (req.query.deep !== '1') return res.json({ status: 'ok' });
    try {
      const r = await fetch(`${apiBase}/api/health`, { signal: AbortSignal.timeout(10000) });
      if (r.ok) return res.json({ status: 'ok', api: 'ok' });
    } catch { /* reported below */ }
    return res.status(503).json({ status: 'error', api: 'unavailable' });
  });
  app.set('view engine', 'ejs');
  app.set('views', path.join(__dirname, '..', 'views'));
  app.use(AMEND_PATH, express.urlencoded({ extended: false, limit: '2mb', parameterLimit: 50000 }));
  app.use(express.urlencoded({ extended: false, limit: '64kb' }));
  app.use(cookieParser(cookieSecret));
  // The plan import upload is read only for a signed-in session (an anonymous request is
  // not buffered; it then fails the CSRF check like any other form).
  const readUpload = multipart({ limit: IMPORT_MAX_BYTES + 64 * 1024 });
  app.use(IMPORT_UPLOAD_PATH, (req, res, next) => (
    req.signedCookies[TOKEN_COOKIE] ? readUpload(req, res, next) : next()));
  app.use('/static', express.static(path.join(__dirname, '..', 'public'), { maxAge: '1h' }));

  const cookieOpts = { httpOnly: true, sameSite: 'strict', secure: secureCookies, signed: true, path: '/' };
  const api = (req) => new Api(apiBase, req.signedCookies[TOKEN_COOKIE], { timeoutMs: apiTimeoutMs });

  // --------------------------------------------------------------- security headers
  app.use((req, res, next) => {
    res.set({
      'X-Frame-Options': 'DENY',
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'same-origin',
      'Cache-Control': 'no-store',
      'Content-Security-Policy':
        "default-src 'self'; script-src 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
    });
    next();
  });

  // --------------------------------------------------------------- shared state
  let infoCache = { at: 0, value: null };
  async function info() {
    if (Date.now() - infoCache.at < 30_000 && infoCache.value) return infoCache.value;
    try {
      const value = await new Api(apiBase, null, { timeoutMs: apiTimeoutMs }).info();
      infoCache = { at: Date.now(), value };
      return value;
    } catch {
      return null; // the page still renders; the error shows where data is needed
    }
  }

  const masterCache = new Map();
  async function masters(client) {
    const out = {};
    await Promise.all(MASTER_KINDS.map(async (kind) => {
      const hit = masterCache.get(kind);
      if (hit && Date.now() - hit.at < MASTER_TTL_MS) { out[kind] = hit.map; return; }
      try {
        const rows = await client.masters(kind);
        const map = Object.fromEntries(rows.map((r) => [r.source_id, r]));
        masterCache.set(kind, { at: Date.now(), map });
        out[kind] = map;
      } catch {
        out[kind] = {};
      }
    }));
    return out;
  }

  function loginCsrf(req) {
    const pre = req.signedCookies[PRE_COOKIE];
    return pre ? hmac(cookieSecret, `login:${pre}`) : '';
  }

  function csrfFor(req) {
    const token = req.signedCookies[TOKEN_COOKIE];
    return token ? hmac(cookieSecret, `csrf:${token}`) : loginCsrf(req);
  }

  // Messages survive one redirect. They are kept on the server (the cookie holds only a
  // random id), so a long list of problems is never lost to the browser's cookie limit.
  const flashes = new Map();
  function setFlash(res, flash) {
    const now = Date.now();
    for (const [k, v] of flashes) if (now - v.at > FLASH_TTL_MS || flashes.size > FLASH_MAX) flashes.delete(k);
    const id = crypto.randomBytes(16).toString('hex');
    flashes.set(id, { at: now, flash });
    res.cookie(FLASH_COOKIE, id, { ...cookieOpts, maxAge: FLASH_TTL_MS });
  }
  function takeFlash(req, res) {
    const id = req.signedCookies[FLASH_COOKIE];
    if (!id) return null;
    res.clearCookie(FLASH_COOKIE, { path: '/' });
    const hit = flashes.get(id);
    flashes.delete(id);
    return hit && Date.now() - hit.at <= FLASH_TTL_MS ? hit.flash : null;
  }

  function flashFromError(err) {
    if (err instanceof ApiError) {
      return {
        kind: 'error',
        code: err.code,
        text: labels.codeText(err.code, err.message),
        message: err.message,
        details: err.details.map((d) => ({
          code: d.code || d.field || '',
          text: d.code ? labels.codeText(d.code, d.message) : null,
          message: d.message || (d.field ? `${d.field}: ${d.before} → ${d.after}` : ''),
          subject: d.subject || null,
        })),
      };
    }
    console.error('unexpected error:', err); // details stay in the server log
    return { kind: 'error', code: 'UNEXPECTED', text: 'เกิดข้อผิดพลาดที่ไม่คาดคิด กรุณาแจ้งผู้ดูแลระบบ' };
  }

  // Locals for every page.
  app.use(async (req, res, next) => {
    res.locals.labels = labels;
    res.locals.fmt = { money: fmtMoney, qty: fmtQty, date: fmtDate };
    res.locals.nameOf = (m, kind, id) => {
      if (!id) return '-';
      const row = m && m[kind] && m[kind][id];
      return row ? row.name : id;
    };
    res.locals.info = await info();
    res.locals.me = null;
    res.locals.flash = null;
    res.locals.flash = takeFlash(req, res);
    res.locals.csrf = csrfFor(req);
    next();
  });

  // An upload too large to read has no form fields (no CSRF token): nothing is done with
  // it; the user is told and sent back to the import page.
  app.use(IMPORT_UPLOAD_PATH, (req, res, next) => {
    if (!req.uploadError || req.uploadError.code !== 'FILE_TOO_LARGE') return next();
    setFlash(res, { kind: 'error', code: 'FILE_TOO_LARGE', text: labels.codeText('FILE_TOO_LARGE') });
    // (inside a mounted middleware req.path is '/': the original path is used)
    return res.redirect(303, req.originalUrl.split('?')[0].replace(/\/preview$/, ''));
  });

  // CSRF check for every state-changing request.
  app.use((req, res, next) => {
    if (req.method !== 'POST') return next();
    // Login is checked against the pre-login cookie even if an old token cookie exists.
    const expected = req.path === '/login' ? loginCsrf(req) : csrfFor(req);
    if (!expected || !safeEqual(expected, req.body && req.body._csrf)) {
      setFlash(res, { kind: 'error', code: 'CSRF_FAILED', text: labels.codeText('CSRF_FAILED') });
      return res.redirect(303, req.signedCookies[TOKEN_COOKIE] ? '/pprs' : '/login');
    }
    return next();
  });

  // --------------------------------------------------------------- public pages
  app.get('/login', (req, res) => {
    let pre = req.signedCookies[PRE_COOKIE];
    if (!pre) {
      pre = crypto.randomBytes(16).toString('hex');
      res.cookie(PRE_COOKIE, pre, cookieOpts);
    }
    res.render('login', { csrf: hmac(cookieSecret, `login:${pre}`), username: '' });
  });

  app.post('/login', async (req, res) => {
    const username = String(req.body.username || '').trim();
    const password = String(req.body.password || '');
    try {
      const { token } = await new Api(apiBase, null, { timeoutMs: apiTimeoutMs }).login(username, password);
      res.clearCookie(PRE_COOKIE, { path: '/' });
      res.cookie(TOKEN_COOKIE, token, cookieOpts);
      return res.redirect(303, '/dashboard');
    } catch (err) {
      res.status(err instanceof ApiError && err.status < 500 ? 401 : 503);
      res.locals.flash = flashFromError(err);
      return res.render('login', { csrf: loginCsrf(req), username });
    }
  });

  app.post('/logout', (req, res) => {
    res.clearCookie(TOKEN_COOKIE, { path: '/' });
    res.redirect(303, '/login');
  });

  // --------------------------------------------------------------- authenticated pages
  const authed = express.Router();
  authed.use(async (req, res, next) => {
    if (!req.signedCookies[TOKEN_COOKIE]) return res.redirect(303, '/login');
    try {
      res.locals.me = await api(req).me();
      const roles = res.locals.me.roles;
      res.locals.can = {
        request: roles.includes('REQUESTER'),
        unlock: roles.some((r) => r === 'PLAN_OFFICER' || r === 'ADMIN'),
        queue: roles.some((r) => QUEUE_ROLES.includes(r)),
        verifyRole: roles.includes('HEAD_OF_PROCUREMENT'),
        appointments: roles.some((r) => APPOINTMENT_READ_ROLES.includes(r)),
        manageAppointments: roles.includes('ADMIN'),
        syncRun: roles.some((r) => SYNC_RUN_ROLES.includes(r)),
        syncRead: roles.some((r) => SYNC_READ_ROLES.includes(r)),
        planManage: roles.some((r) => PLAN_MANAGE_ROLES.includes(r)),
        planAmend: roles.some((r) => PLAN_AMEND_ROLES.includes(r)),
        alertSettings: roles.includes('ADMIN'),
      };
      return next();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        res.clearCookie(TOKEN_COOKIE, { path: '/' });
        setFlash(res, { kind: 'info', text: 'หมดเวลาการใช้งาน กรุณาเข้าสู่ระบบอีกครั้ง' });
        return res.redirect(303, '/login');
      }
      return next(err);
    }
  });

  authed.get('/', (req, res) => res.redirect(303, '/dashboard'));

  authed.get('/pprs', async (req, res, next) => {
    try {
      const client = api(req);
      const [pprs, m] = await Promise.all([client.listPprs(), masters(client)]);
      res.render('list', { pprs, m });
    } catch (err) { next(err); }
  });

  authed.get('/pprs/new', async (req, res, next) => {
    const prNo = String(req.query.pr_no || '').trim();
    const chosen = parseChoices(req.query.choice);
    if (!prNo) return res.render('new', { prNo: '', report: null, m: {}, chosen });
    try {
      const client = api(req);
      const [report, m] = await Promise.all([client.prevalidate(prNo, chosen), masters(client)]);
      return res.render('new', { prNo, report, m, chosen });
    } catch (err) {
      if (err instanceof ApiError) {
        res.locals.flash = flashFromError(err);
        return res.status(err.status).render('new', { prNo, report: null, m: {}, chosen });
      }
      return next(err);
    }
  });

  authed.post('/pprs', async (req, res) => {
    const prNo = String(req.body.pr_no || '').trim();
    const chosen = parseChoices(req.body.choice); // D-43: the rows checked on the page
    try {
      const ppr = await api(req).createPpr(prNo, chosen);
      setFlash(res, { kind: 'ok', text: `สร้างฉบับร่างของ PR ${prNo} แล้ว` });
      res.redirect(303, `/pprs/${ppr.id}`);
    } catch (err) {
      setFlash(res, flashFromError(err));
      res.redirect(303, `/pprs/new?${choiceSearch(prNo, chosen)}`);
    }
  });

  const idOk = (id) => /^[0-9]{1,12}$/.test(id);

  authed.get('/pprs/:id', async (req, res, next) => {
    if (!idOk(req.params.id)) return next();
    try {
      const client = api(req);
      const [ppr, versions, m, timeline, observations, coverage] = await Promise.all([
        client.getPpr(req.params.id), client.versions(req.params.id), masters(client),
        client.timeline(req.params.id), client.observations(req.params.id),
        client.coverage(req.params.id),
      ]);
      const editable = ppr.state === 'DRAFT' || ppr.state === 'UNLOCKED_FOR_REVISION';
      const categories = editable ? await client.eligibleCategories(ppr.id) : [];
      // D-43: the rows each line may use, read live; HOSxP down only hides the choice form.
      let choices = null;
      let choicesError = null;
      if (editable) {
        try { choices = await client.choices(ppr.id); } catch (err) {
          if (!(err instanceof ApiError)) throw err;
          choicesError = labels.codeText(err.code, err.message);
        }
      }
      // Whether this user may verify now is decided by the API (D-28) and reported with
      // the timeline; the button is only a hint - the API re-checks everything.
      const canVerify = Boolean(timeline.can_verify);
      // D-41: only the requester who created the PPR edits, confirms or discards it.
      const mine = res.locals.can.request && ppr.created_by_user_id === res.locals.me.id;
      return res.render('detail', {
        ppr, versions, m, editable, categories, timeline, canVerify, observations, coverage, mine,
        choices, choicesError,
        unlockable: UNLOCKABLE.includes(ppr.state),
        syncable: SYNC_STATES.includes(ppr.state),
      });
    } catch (err) { return next(err); }
  });

  function action(name, run, { after } = {}) {
    authed.post(`/pprs/:id/${name}`, async (req, res, next) => {
      if (!idOk(req.params.id)) return next();
      const id = req.params.id;
      try {
        const text = await run(api(req), id, req.body);
        setFlash(res, { kind: 'ok', text });
        return res.redirect(303, after ? after(id) : `/pprs/${id}`);
      } catch (err) {
        setFlash(res, flashFromError(err));
        return res.redirect(303, `/pprs/${id}`);
      }
    });
  }

  action('allocations', async (client, id, body) => {
    const cats = [].concat(body.category || []);
    const amounts = [].concat(body.amount || []);
    const allocations = cats
      .map((c, i) => ({ budget_category_id: String(c).trim(), amount: String(amounts[i] || '').trim() }))
      .filter((a) => a.budget_category_id && a.amount);
    await client.setAllocations(id, allocations, String(body.adjustment_reference || '').trim());
    return 'บันทึกการแบ่งหมวดงบแล้ว';
  });
  action('choices', async (client, id, body) => {
    await client.setChoices(id, parseChoices(body.choice));
    return 'บันทึกรายการแผนที่เลือกแล้ว';
  });
  action('coverage', async (client, id, body) => {
    // One row per (Assigned Purchase item, demand department); blank means 0.
    const items = [].concat(body.cov_item || []);
    const depts = [].concat(body.cov_dept || []);
    const qtys = [].concat(body.cov_qty || []);
    const coverage = items.map((item, i) => ({
      plan_item_id: Number(item),
      department_id: String(depts[i] || ''),
      qty: String(qtys[i] || '').trim() || '0',
    }));
    await client.setCoverage(id, coverage);
    return 'บันทึกยอดที่ซื้อให้แต่ละหน่วยงานแล้ว';
  });
  action('confirm', async (client, id) => {
    const ppr = await client.confirm(id);
    return `ยืนยันแล้ว เลขที่ PPR ${ppr.ppr_number} ฉบับที่ ${ppr.version}`;
  });
  action('discard', async (client, id, body) => {
    // The creator discards their draft; Planning / Admin give a reason (D-41).
    await client.discard(id, String(body.reason || '').trim());
    return 'ยกเลิกฉบับร่างแล้ว PR นี้ทำฉบับร่างใหม่ได้';
  }, { after: () => '/pprs' });
  action('verify', async (client, id, body) => {
    const ppr = await client.verify(id, String(body.version || ''));
    return `บันทึก "ตรวจสอบและยืนยันแล้ว" สำหรับ ${ppr.ppr_number} ฉบับที่ ${ppr.version} แล้ว`;
  });
  action('unlock', async (client, id, body) => {
    await client.unlock(id, String(body.reason || '').trim());
    return 'ปลดล็อกแล้ว ผู้ขอแก้ไขและยืนยันใหม่ได้';
  });
  const ATTENTION = ['INVALID_EVIDENCE', 'NOT_FOUND', 'UNAVAILABLE', 'ERROR', 'ANOMALY', 'SKIPPED_STATE_MOVED'];
  function syncAction(name, call) {
    authed.post(`/pprs/:id/${name}`, async (req, res, next) => {
      if (!idOk(req.params.id)) return next();
      const id = req.params.id;
      try {
        const result = await call(api(req), id);
        const o = result.observations[0];
        setFlash(res, o ? {
          kind: ATTENTION.includes(o.outcome) ? 'error' : 'ok',
          text: `ผลจาก HOSxP: ${labels.SYNC_OUTCOMES[o.outcome] || o.outcome}`,
        } : { kind: 'info', text: `ซิงค์แล้ว (${labels.RUN_STATUS[result.run.status] || result.run.status})` });
      } catch (err) {
        setFlash(res, flashFromError(err));
      }
      return res.redirect(303, `/pprs/${id}`);
    });
  }
  syncAction('sync', (client, id) => client.syncPpr(id));
  syncAction('recheck', (client, id) => client.recheckPpr(id));

  // --------------------------------------------------------------- Wave 6 pages
  authed.get('/pr-sync', async (req, res, next) => {
    try {
      const client = api(req);
      const [runs, governance] = await Promise.all([
        client.prSyncRuns(),
        res.locals.can.syncRun ? client.prSyncGovernance() : Promise.resolve(null),
      ]);
      const lastOk = runs.find((r) => r.status === 'SUCCEEDED' && r.scope === 'PPRS') || null;
      return res.render('pr_sync', { runs, governance, lastOk });
    } catch (err) { return next(err); }
  });

  authed.get('/pr-sync/runs/:id', async (req, res, next) => {
    if (!idOk(req.params.id)) return next();
    try {
      const detail = await api(req).prSyncRun(req.params.id);
      return res.render('pr_sync_run', { detail });
    } catch (err) { return next(err); }
  });

  authed.post('/pr-sync', async (req, res) => {
    const retryOf = String(req.body.retry_of || '');
    try {
      const { run } = await api(req).prSync(idOk(retryOf) ? retryOf : null);
      setFlash(res, { kind: 'info', text: `เริ่มซิงค์รอบที่ ${run.id} แล้ว ระบบทำงานต่อเบื้องหลัง หน้านี้จะแสดงผลเมื่อเสร็จ` });
      return res.redirect(303, `/pr-sync/runs/${run.id}`);
    } catch (err) {
      setFlash(res, flashFromError(err));
      return res.redirect(303, '/pr-sync');
    }
  });

  // --------------------------------------------------------------- Wave 8A pages
  const fyOk = (fy) => /^[0-9]{4}$/.test(String(fy || ''));
  const str = (v) => String(Array.isArray(v) ? v[0] : v || '').trim();
  const optional = (v) => (str(v) === '' ? null : str(v));
  function correction(body) {
    const reason = str(body.correction_reason);
    const ref = str(body.source_reference);
    return reason || ref ? { correction_reason: reason, source_reference: ref } : {};
  }

  authed.get('/dashboard', async (req, res, next) => {
    // Only a plausible Buddhist-Era year is forwarded (the API accepts 2400-2700).
    const asked = str(req.query.fiscal_year);
    const fy = fyOk(asked) && Number(asked) >= 2400 && Number(asked) <= 2700 ? asked : null;
    try {
      const client = api(req);
      const [dash, m, years] = await Promise.all([
        client.dashboard(fy), masters(client), client.planYears(),
      ]);
      // A requester sees the plan of their own department item by item (§31.2).
      let items = null;
      if (dash.department_scope !== null && dash.plan_year_state) {
        items = await client.planBalances(dash.fiscal_year);
      }
      return res.render('dashboard', { dash, m, items, years });
    } catch (err) { return next(err); }
  });

  authed.get('/alerts', async (req, res, next) => {
    try {
      const client = api(req);
      const [list, settings, m] = await Promise.all([client.alerts(), client.alertSettings(), masters(client)]);
      return res.render('alerts', { list, settings, m });
    } catch (err) { return next(err); }
  });

  authed.post('/alert-settings/:code', async (req, res) => {
    const code = ALERT_SETTING_CODES.includes(req.params.code) ? req.params.code : null;
    try {
      if (!code) throw new ApiError(404, 'NOT_FOUND', 'unknown setting');
      const value = Number.parseInt(str(req.body.value), 10);
      await api(req).setAlertSetting(code, Number.isFinite(value) ? value : 0, str(req.body.reason));
      setFlash(res, { kind: 'ok', text: 'บันทึกเกณฑ์การแจ้งเตือนแล้ว' });
    } catch (err) {
      setFlash(res, flashFromError(err));
    }
    return res.redirect(303, '/alerts');
  });

  authed.get('/plans', async (req, res, next) => {
    try {
      const client = api(req);
      const [years, runs] = await Promise.all([
        client.planYears(),
        res.locals.can.syncRun ? client.masterSyncRuns() : Promise.resolve(null),
      ]);
      return res.render('plans', { years, runs });
    } catch (err) { return next(err); }
  });

  function planPost(path, run, { back } = {}) {
    authed.post(path, async (req, res, next) => {
      if (req.params.fy !== undefined && !fyOk(req.params.fy)) return next();
      if (req.params.id !== undefined && !idOk(req.params.id)) return next();
      const to = back ? back(req) : `/plans/${req.params.fy}`;
      try {
        const text = await run(api(req), req.params, req.body);
        setFlash(res, { kind: 'ok', text });
      } catch (err) {
        setFlash(res, flashFromError(err));
      }
      return res.redirect(303, to);
    });
  }

  planPost('/plans', async (client, _p, body) => {
    const fy = Number.parseInt(str(body.fiscal_year), 10);
    await client.createPlanYear(Number.isFinite(fy) ? fy : 0);
    return `สร้างแผนปีงบประมาณ ${fy} แล้ว (ร่างแผน)`;
  }, { back: (req) => (fyOk(str(req.body.fiscal_year)) ? `/plans/${str(req.body.fiscal_year)}` : '/plans') });

  planPost('/plans/sync-masters', async (client) => {
    const r = await client.syncMasters();
    return `ซิงค์ข้อมูลหลักจาก HOSxP แล้ว: ตรวจ ${r.records_checked} รายการ เพิ่ม ${r.records_created} ปรับ ${r.records_updated}`;
  }, { back: (req) => (fyOk(str(req.body.back_fy)) ? `/plans/${str(req.body.back_fy)}` : '/plans') });

  planPost('/plans/:fy/transition', async (client, p, body) => {
    const y = await client.transitionPlanYear(p.fy, str(body.target), str(body.reason));
    return `แผนปีงบ ${p.fy}: ${labels.PLAN_YEAR_STATES[y.state] || y.state}`;
  });

  planPost('/plans/:fy/budgets', async (client, p, body) => {
    await client.createBudget(p.fy, {
      fund_source_id: str(body.fund_source_id),
      budget_category_id: str(body.budget_category_id),
      approved_amount: str(body.approved_amount),
      ...correction(body),
    });
    return 'เพิ่มวงเงินแล้ว';
  });

  planPost('/plans/:fy/budgets/:id', async (client, p, body) => {
    await client.updateBudget(p.id, { approved_amount: str(body.approved_amount), ...correction(body) });
    return 'แก้วงเงินแล้ว';
  });

  planPost('/plans/:fy/budgets/:id/delete', async (client, p, body) => {
    await client.deleteBudget(p.id, correction(body));
    return 'ลบวงเงินแล้ว';
  });

  // --------------------------------------------------------------- Wave 12A-1 import (D-42)
  // A previewed file waits here (server memory, 30 minutes) for the confirm button, so the
  // user does not choose it twice. It is bound to the session that previewed it.
  const pendingImports = new Map();
  function keepPending(req, entry) {
    const now = Date.now();
    for (const [k, v] of pendingImports) if (now - v.at > IMPORT_PENDING_TTL_MS) pendingImports.delete(k);
    const owner = hmac(cookieSecret, `import:${req.signedCookies[TOKEN_COOKIE]}`);
    // one waiting file per session (a new check replaces it), at most IMPORT_PENDING_MAX
    for (const [k, v] of pendingImports) if (v.owner === owner) pendingImports.delete(k);
    while (pendingImports.size >= IMPORT_PENDING_MAX) pendingImports.delete(pendingImports.keys().next().value);
    const id = crypto.randomBytes(16).toString('hex');
    pendingImports.set(id, { ...entry, owner, at: now });
    return id;
  }
  function takePending(req, id, fy) {
    const hit = /^[0-9a-f]{32}$/.test(id) ? pendingImports.get(id) : null;
    if (!hit) return null;
    pendingImports.delete(id);
    const owner = hmac(cookieSecret, `import:${req.signedCookies[TOKEN_COOKIE]}`);
    if (!safeEqual(owner, hit.owner) || hit.fy !== fy || Date.now() - hit.at > IMPORT_PENDING_TTL_MS) return null;
    return hit;
  }

  async function importPage(req, res, preview, pendingId) {
    const { fy } = req.params;
    const client = api(req);
    const years = await client.planYears();
    const year = years.find((y) => String(y.fiscal_year) === fy);
    if (!year) throw new ApiError(404, 'PLAN_YEAR_NOT_FOUND', `plan year ${fy} not found`);
    const [imports, m] = await Promise.all([client.planImports(fy), masters(client)]);
    imports.reverse();
    return res.render('plan_import', { year, imports, m, preview, pendingId });
  }

  authed.get('/plans/:fy/import', async (req, res, next) => {
    if (!fyOk(req.params.fy)) return next();
    try { return await importPage(req, res, null, null); } catch (err) { return next(err); }
  });

  authed.get('/plans/:fy/import-template', async (req, res) => {
    if (!fyOk(req.params.fy)) return res.redirect(303, '/plans');
    try {
      const file = await api(req).importTemplate(req.params.fy);
      res.set('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
      res.set('Content-Disposition', `attachment; filename="plan_import_${req.params.fy}.xlsx"`);
      return res.send(file.body);
    } catch (err) {
      setFlash(res, flashFromError(err));
      return res.redirect(303, `/plans/${req.params.fy}/import`);
    }
  });

  authed.post('/plans/:fy/import/preview', async (req, res, next) => {
    if (!fyOk(req.params.fy)) return next();
    const back = `/plans/${req.params.fy}/import`;
    const file = req.upload;
    if (req.uploadError || !file || !file.data.length) {
      const code = req.uploadError ? req.uploadError.code : 'EMPTY_FILE';
      setFlash(res, { kind: 'error', code, text: labels.codeText(code) });
      return res.redirect(303, back);
    }
    try {
      const name = file.filename || 'plan.xlsx';
      const preview = await api(req).importPreview(req.params.fy, file.data, name);
      const pendingId = preview.can_import
        ? keepPending(req, { fy: req.params.fy, data: file.data, name, sha: preview.file_sha256 })
        : null;
      return await importPage(req, res, preview, pendingId);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return next(err);
      setFlash(res, flashFromError(err));
      return res.redirect(303, back);
    }
  });

  authed.post('/plans/:fy/import/confirm', async (req, res, next) => {
    if (!fyOk(req.params.fy)) return next();
    const hit = takePending(req, str(req.body.pending), req.params.fy);
    if (!hit) {
      setFlash(res, { kind: 'error', code: 'IMPORT_EXPIRED', text: labels.codeText('IMPORT_EXPIRED') });
      return res.redirect(303, `/plans/${req.params.fy}/import`);
    }
    try {
      const r = await api(req).importPlan(req.params.fy, hit.data, hit.name, hit.sha);
      setFlash(res, { kind: 'ok', text: `นำเข้าแผนแล้ว ${r.rows_imported} รายการ (ข้าม ${r.rows_skipped}) จากไฟล์ ${r.file_name}` });
      return res.redirect(303, `/plans/${req.params.fy}`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return next(err);
      setFlash(res, flashFromError(err));
      return res.redirect(303, `/plans/${req.params.fy}/import`);
    }
  });

  planPost('/plans/:fy/imports/:id/remove', async (client, p, body) => {
    const r = await client.removeImport(p.id, str(body.reason));
    return `ยกเลิกการนำเข้า #${r.id} แล้ว (ลบรายการแผนของไฟล์นี้)`;
  }, { back: (req) => `/plans/${req.params.fy}/import` });

  planPost('/plans/:fy/items/:id/delete', async (client, p, body) => {
    await client.deletePlanItem(p.id, correction(body));
    return 'ลบรายการแผนแล้ว';
  });

  authed.get('/plans/:fy', async (req, res, next) => {
    if (!fyOk(req.params.fy)) return next();
    const { fy } = req.params;
    try {
      const client = api(req);
      const years = await client.planYears();
      const year = years.find((y) => String(y.fiscal_year) === fy);
      if (!year) throw new ApiError(404, 'PLAN_YEAR_NOT_FOUND', `plan year ${fy} not found`);
      const editable = year.state === 'DRAFT' || year.state === 'APPROVED';
      const [budgets, balances, m, validation, settings, items] = await Promise.all([
        client.budgets(fy), client.planBalances(fy), masters(client),
        res.locals.can.planManage && editable ? client.planValidation(fy) : Promise.resolve(null),
        client.alertSettings(), client.planItems(fy),
      ]);
      // D-38: each department's demand on the Assigned Purchase items the user can see.
      const assigned = items.filter((i) => i.plan_type === 'ASSIGNED');
      // The bars use the stored low-plan threshold (D-33), never a constant of their own.
      const lowSetting = settings.find((x) => x.code === 'PLAN_LOW_PERCENT');
      const low = lowSetting ? lowSetting.value : null;
      return res.render('plan_year', {
        year, budgets, balances, m, validation, editable, low, assigned,
      });
    } catch (err) { return next(err); }
  });

  function itemBody(body) {
    const depts = [].concat(body.demand_department || []);
    const qtys = [].concat(body.demand_qty || []);
    const demands = depts
      .map((d, i) => ({ department_id: String(d).trim(), demand_qty: String(qtys[i] || '').trim() }))
      .filter((d) => d.department_id && d.demand_qty);
    return {
      plan_budget_id: Number.parseInt(str(body.plan_budget_id), 10) || 0,
      plan_type: PLAN_TYPES.includes(str(body.plan_type)) ? str(body.plan_type) : 'DEPARTMENT',
      item_id: optional(body.item_id), // D-43: optional
      item_name: optional(body.item_name), // D-43: required when there is no HOSxP item
      unit: optional(body.unit),
      owner_department_id: str(body.owner_department_id),
      purchasing_department_id: optional(body.purchasing_department_id),
      planned_qty: str(body.planned_qty),
      estimated_unit_price: str(body.estimated_unit_price),
      planned_amount: str(body.planned_amount),
      q1: optional(body.q1), q2: optional(body.q2), q3: optional(body.q3), q4: optional(body.q4),
      note: optional(body.note),
      demands,
      ...correction(body),
    };
  }

  function lookup(m, text) {
    const q = str(text).toLowerCase();
    if (!q) return null;
    const out = [];
    for (const r of Object.values(m.ITEM || {})) {
      if (`${r.source_id} ${r.code} ${r.name}`.toLowerCase().includes(q)) out.push(r);
      if (out.length >= LOOKUP_MAX) break;
    }
    return out;
  }

  async function renderItemForm(req, res, { fy, item, values, status = 200 }) {
    const client = api(req);
    const [years, budgets, m] = await Promise.all([client.planYears(), client.budgets(fy), masters(client)]);
    const year = years.find((y) => String(y.fiscal_year) === String(fy));
    if (!year) throw new ApiError(404, 'PLAN_YEAR_NOT_FOUND', `plan year ${fy} not found`);
    const v = { ...values };
    if (str(req.query.item_id)) v.item_id = str(req.query.item_id);
    return res.status(status).render('plan_item_form', {
      year, budgets, m, item, v, found: lookup(m, req.query.find), find: str(req.query.find),
      demandRows: DEMAND_ROWS, planTypes: PLAN_TYPES,
    });
  }

  function valuesOf(item) {
    if (!item) return { plan_type: 'DEPARTMENT', demands: [] };
    return { ...item, demands: item.demands || [] };
  }

  authed.get('/plans/:fy/items/new', async (req, res, next) => {
    if (!fyOk(req.params.fy)) return next();
    try {
      return await renderItemForm(req, res, { fy: req.params.fy, item: null, values: valuesOf(null) });
    } catch (err) { return next(err); }
  });

  authed.get('/plans/:fy/items/:id/edit', async (req, res, next) => {
    if (!fyOk(req.params.fy) || !idOk(req.params.id)) return next();
    try {
      const item = await api(req).planItem(req.params.id);
      return await renderItemForm(req, res, { fy: req.params.fy, item, values: valuesOf(item) });
    } catch (err) { return next(err); }
  });

  async function saveItem(req, res, next, call, okText) {
    if (!fyOk(req.params.fy) || (req.params.id !== undefined && !idOk(req.params.id))) return next();
    const body = itemBody(req.body);
    try {
      await call(api(req), body);
      setFlash(res, { kind: 'ok', text: okText });
      return res.redirect(303, `/plans/${req.params.fy}`);
    } catch (err) {
      if (!(err instanceof ApiError) || err.status >= 500) return next(err);
      // Show the form again with what was entered and the API's answer.
      res.locals.flash = flashFromError(err);
      try {
        // the stored item (its name and unit) is shown with the form again
        const item = req.params.id
          ? await api(req).planItem(req.params.id).catch(() => ({ id: Number(req.params.id) }))
          : null;
        return await renderItemForm(req, res, { fy: req.params.fy, item, values: body, status: err.status });
      } catch (err2) { return next(err2); }
    }
  }

  authed.post('/plans/:fy/items', (req, res, next) => saveItem(
    req, res, next, (client, body) => client.createPlanItem(req.params.fy, body), 'เพิ่มรายการแผนแล้ว',
  ));
  authed.post('/plans/:fy/items/:id', (req, res, next) => saveItem(
    req, res, next, (client, body) => client.updatePlanItem(req.params.id, body), 'แก้รายการแผนแล้ว',
  ));

  // --------------------------------------------------------------- Wave 7A: Plan Amendment
  const list = (v) => [].concat(v === undefined ? [] : v).map((x) => String(x).trim());
  function sameValue(a, b) {
    const x = String(a === null || a === undefined ? '' : a).trim();
    const y = String(b === null || b === undefined ? '' : b).trim();
    if (x === y) return true;
    const nx = Number(x);
    const ny = Number(y);
    return x !== '' && y !== '' && Number.isFinite(nx) && Number.isFinite(ny) && nx === ny;
  }

  /** The API body from the form: only rows that differ from the current plan are sent. */
  function amendmentBody(body, items, budgets) {
    const out = {
      approval_document_no: str(body.approval_document_no),
      approval_date: str(body.approval_date),
      reason: str(body.reason),
      budgets: [],
      items: [],
      new_items: [],
      demands: [],
    };
    // Compare with the values the form was built on (hidden o* fields), never with the plan
    // as it is now: a field the user did not touch is never sent. The API also refuses the
    // whole amendment when the plan was amended since the form opened (base_amendment_no).
    const base = str(body.base_amendment_no);
    if (/^[0-9]{1,9}$/.test(base)) out.base_amendment_no = Number(base);
    for (const b of budgets) {
      const v = body[`b_${b.id}`];
      const was = body[`ob_${b.id}`] !== undefined ? body[`ob_${b.id}`] : b.approved_amount;
      if (v !== undefined && str(v) !== '' && !sameValue(v, was)) {
        out.budgets.push({ fund_source_id: b.fund_source_id, budget_category_id: b.budget_category_id, approved_amount: str(v) });
      }
    }
    const [nbFund, nbCat, nbAmount] = [list(body.nb_fund), list(body.nb_cat), list(body.nb_amount)];
    nbAmount.forEach((amount, i) => {
      if (nbFund[i] && nbCat[i] && amount) {
        out.budgets.push({ fund_source_id: nbFund[i], budget_category_id: nbCat[i], approved_amount: amount });
      }
    });
    const fields = [
      ['q', 'planned_qty'], ['p', 'estimated_unit_price'], ['a', 'planned_amount'],
      ['i', 'item_id'], ['o', 'owner_department_id'], ['u', 'purchasing_department_id'], ['c', 'budget_category_id'],
      ['nm', 'item_name'], ['un', 'unit'], // D-43: a row without a HOSxP item, while unused
    ];
    for (const it of items) {
      // D-38 A-6: department demand of an ASSIGNED item (rows in order; the last is a new one).
      if (it.plan_type === 'ASSIGNED' && body[`dd_${it.id}`] !== undefined) {
        const [dd, dq, odq] = [list(body[`dd_${it.id}`]), list(body[`dq_${it.id}`]), list(body[`odq_${it.id}`])];
        dd.forEach((dept, i) => {
          if (dept && dq[i] !== '' && dq[i] !== undefined && !sameValue(dq[i], odq[i])) {
            out.demands.push({ plan_item_id: it.id, department_id: dept, demand_qty: dq[i] });
          }
        });
      }
      if (body[`q_${it.id}`] === undefined) continue; // not on the page: unchanged
      const change = { plan_item_id: it.id };
      for (const [short, key] of fields) {
        const v = body[`${short}_${it.id}`];
        if (v === undefined) continue;
        const original = body[`o${short}_${it.id}`];
        const was = original !== undefined ? original : it[key];
        if (!sameValue(v, was)) change[key] = key === 'purchasing_department_id' ? optional(v) : str(v);
      }
      if (Object.keys(change).length > 1) out.items.push(change);
    }
    const n = {
      type: list(body.n_type), item: list(body.n_item), owner: list(body.n_owner), buyer: list(body.n_buyer),
      fund: list(body.n_fund), cat: list(body.n_cat), qty: list(body.n_qty), price: list(body.n_price),
      amount: list(body.n_amount), note: list(body.n_note),
      name: list(body.n_name), unit: list(body.n_unit),
    };
    n.type.forEach((_, i) => {
      const item = n.item[i] || '';
      if (!item && !n.name[i]) return; // an empty row (D-43: a code or a name makes a row)
      const type = AMEND_TYPES.includes(n.type[i]) ? n.type[i] : 'DEPARTMENT';
      const [dd, dq] = [list(body[`n_dd_${i}`]), list(body[`n_dq_${i}`])];
      const demands = type === 'ASSIGNED'
        ? dd.map((d, k) => ({ department_id: d, demand_qty: dq[k] || '' })).filter((d) => d.department_id && d.demand_qty)
        : [];
      out.new_items.push({
        ref: `new-${i + 1}`,
        plan_type: type,
        demands,
        item_id: item || null,
        item_name: n.name[i] || null,
        unit: n.unit[i] || null,
        owner_department_id: n.owner[i] || '',
        purchasing_department_id: n.buyer[i] || null,
        fund_source_id: n.fund[i] || '',
        budget_category_id: n.cat[i] || '',
        planned_qty: n.qty[i] || '0',
        estimated_unit_price: n.price[i] || '0',
        planned_amount: n.amount[i] || '0',
        note: n.note[i] || null,
      });
    });
    return out;
  }

  function amendFilter(src) {
    const pick = (k) => str(Array.isArray(src[k]) ? src[k][0] : src[k]).slice(0, 100);
    return { fund: pick('f_fund'), cat: pick('f_cat'), dept: pick('f_dept'), find: pick('f_find') };
  }

  async function renderAmendForm(req, res, { values = {}, preview = null, status = 200 }) {
    const { fy } = req.params;
    const client = api(req);
    const [years, budgets, items, balances, m, history] = await Promise.all([
      client.planYears(), client.budgets(fy), client.planItems(fy), client.planBalances(fy), masters(client),
      client.amendments(fy),
    ]);
    const latest = history.reduce((n, a) => Math.max(n, a.amendment_no), 0);
    const year = years.find((y) => String(y.fiscal_year) === fy);
    if (!year) throw new ApiError(404, 'PLAN_YEAR_NOT_FOUND', `plan year ${fy} not found`);
    const f = amendFilter({ ...req.query, ...values });
    const used = Object.fromEntries(balances.map((b) => [b.plan_item_id, b]));
    const text = f.find.toLowerCase();
    const shown = items.filter((it) => (!f.fund || it.fund_source_id === f.fund)
      && (!f.cat || it.budget_category_id === f.cat)
      && (!f.dept || it.owner_department_id === f.dept || it.purchasing_department_id === f.dept)
      && (!text || `${it.item_id || ''} ${it.item_code || ''} ${it.item_name}`.toLowerCase().includes(text)));
    return res.status(status).render('amendment_form', {
      year, budgets, items: shown.slice(0, AMEND_MAX_ROWS), truncated: shown.length > AMEND_MAX_ROWS,
      total: items.length, used, m, v: values, f, preview, latest,
      newItemRows: AMEND_NEW_ITEM_ROWS, newBudgetRows: AMEND_NEW_BUDGET_ROWS, amendTypes: AMEND_TYPES,
      newDemandRows: AMEND_NEW_DEMAND_ROWS,
    });
  }

  authed.get('/plans/:fy/amendments', async (req, res, next) => {
    if (!fyOk(req.params.fy)) return next();
    try {
      const client = api(req);
      const [years, amendments] = await Promise.all([client.planYears(), client.amendments(req.params.fy)]);
      const year = years.find((y) => String(y.fiscal_year) === req.params.fy);
      return res.render('amendments', { year, fy: req.params.fy, amendments });
    } catch (err) { return next(err); }
  });

  authed.get('/plans/:fy/amendments/new', async (req, res, next) => {
    if (!fyOk(req.params.fy)) return next();
    try { return await renderAmendForm(req, res, {}); } catch (err) { return next(err); }
  });

  async function amendPost(req, res, next, create) {
    if (!fyOk(req.params.fy)) return next();
    try {
      const client = api(req);
      const [budgets, items] = await Promise.all([client.budgets(req.params.fy), client.planItems(req.params.fy)]);
      const body = amendmentBody(req.body, items, budgets);
      if (create) {
        const rec = await client.createAmendment(req.params.fy, body);
        setFlash(res, { kind: 'ok', text: `บันทึกการปรับแผนครั้งที่ ${rec.amendment_no} แล้ว` });
        return res.redirect(303, `/plan-amendments/${rec.id}`);
      }
      const preview = await client.previewAmendment(req.params.fy, body);
      return await renderAmendForm(req, res, { values: req.body, preview });
    } catch (err) {
      if (!(err instanceof ApiError) || err.status >= 500) return next(err);
      res.locals.flash = flashFromError(err);
      try {
        return await renderAmendForm(req, res, { values: req.body, status: err.status });
      } catch (err2) { return next(err2); }
    }
  }
  authed.post('/plans/:fy/amendments/preview', (req, res, next) => amendPost(req, res, next, false));
  authed.post('/plans/:fy/amendments', (req, res, next) => amendPost(req, res, next, true));

  authed.get('/plan-amendments/:id', async (req, res, next) => {
    if (!idOk(req.params.id)) return next();
    try {
      const client = api(req);
      const [amendment, m] = await Promise.all([client.amendment(req.params.id), masters(client)]);
      return res.render('amendment', { a: amendment, m });
    } catch (err) { return next(err); }
  });

  // --------------------------------------------------------------- Wave 8B pages
  authed.get('/reports', async (req, res, next) => {
    try {
      return res.render('reports', { reports: await api(req).reports() });
    } catch (err) { return next(err); }
  });

  authed.get('/reports/:code', async (req, res, next) => {
    if (!reportCodeOk(req.params.code)) return next();
    const params = reportParams(req.query);
    const dropped = params.has('_dropped');
    params.delete('_dropped');
    const client = api(req);
    if (dropped) res.locals.flash = { kind: 'info', text: 'ตัวกรองบางช่องรูปแบบไม่ถูกต้อง จึงไม่ได้ใช้' };
    try {
      const [list, m, years] = await Promise.all([client.reports(), masters(client), client.planYears()]);
      // Not "info": that name holds the API status (demo banner) on every page.
      const def = list.find((x) => x.code === req.params.code);
      if (!def) throw new ApiError(404, 'REPORT_NOT_FOUND', 'unknown report');
      let report = null;
      if (req.query.go === '1' || def.kind === 'plan') {
        try {
          report = await client.report(req.params.code, params);
        } catch (err) {
          if (!(err instanceof ApiError) || err.status >= 500 || err.status === 401) throw err;
          res.locals.flash = flashFromError(err);
          res.status(err.status);
        }
      }
      return res.render('report', { def, report, m, years, q: Object.fromEntries(params), qs: params.toString() });
    } catch (err) { return next(err); }
  });

  authed.get('/reports/:code/xlsx', async (req, res, next) => {
    if (!reportCodeOk(req.params.code)) return next();
    const params = reportParams(req.query);
    params.delete('_dropped');
    try {
      const file = await api(req).reportFile(req.params.code, params);
      // Only the API's own file name and type are passed on.
      const name = (/filename="([A-Za-z0-9_.-]{1,80})"/.exec(file.disposition) || [])[1] || 'report.xlsx';
      res.set('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
      res.set('Content-Disposition', `attachment; filename="${name}"`);
      return res.send(file.body);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return next(err);
      setFlash(res, flashFromError(err));
      return res.redirect(303, `/reports/${req.params.code}?${params}`);
    }
  });

  // --------------------------------------------------------------- Wave 5B pages
  authed.get('/search', async (req, res, next) => {
    const params = searchParams(req.query);
    const asked = [...params.keys()].length > 0 || req.query.go === '1';
    try {
      const client = api(req);
      const [m, results] = await Promise.all([
        masters(client), asked ? client.search(params) : Promise.resolve(null),
      ]);
      return res.render('search', { m, results, q: Object.fromEntries(params) });
    } catch (err) { return next(err); }
  });

  authed.get('/procurement', async (req, res, next) => {
    const bucket = QUEUE_BUCKETS.includes(req.query.bucket) ? req.query.bucket : 'waiting';
    const params = searchParams(req.query);
    params.delete('state');
    try {
      const client = api(req);
      const [queue, m] = await Promise.all([client.queue(bucket, params), masters(client)]);
      return res.render('queue', { queue, m, bucket, buckets: QUEUE_BUCKETS, q: Object.fromEntries(params) });
    } catch (err) { return next(err); }
  });

  authed.get('/appointments', async (req, res, next) => {
    try {
      const client = api(req);
      const [appointments, candidates] = await Promise.all([
        client.appointments(),
        res.locals.can.manageAppointments ? client.appointmentCandidates() : Promise.resolve([]),
      ]);
      return res.render('appointments', { appointments, candidates });
    } catch (err) { return next(err); }
  });

  function appointmentAction(path, run, okText) {
    authed.post(path, async (req, res, next) => {
      if (req.params.id !== undefined && !idOk(req.params.id)) return next();
      try {
        await run(api(req), req.params.id, req.body);
        setFlash(res, { kind: 'ok', text: okText });
      } catch (err) {
        setFlash(res, flashFromError(err));
      }
      return res.redirect(303, '/appointments');
    });
  }
  appointmentAction('/appointments', (client, _id, body) => client.createAppointment({
    user_id: Number(body.user_id),
    fiscal_year: Number(body.fiscal_year),
    effective_from: String(body.effective_from || ''),
    effective_to: String(body.effective_to || '') || null,
  }), 'แต่งตั้งแล้ว');
  appointmentAction('/appointments/:id/end', (client, id, body) => client.endAppointment(
    id, String(body.effective_to || ''), String(body.reason || '').trim(),
  ), 'กำหนดวันสิ้นสุดใหม่แล้ว');
  appointmentAction('/appointments/:id/revoke', (client, id, body) => client.revokeAppointment(
    id, String(body.reason || '').trim(),
  ), 'เพิกถอนการแต่งตั้งแล้ว');

  authed.get('/pprs/:id/print', async (req, res, next) => {
    if (!idOk(req.params.id)) return next();
    const version = /^[0-9]{1,6}$/.test(String(req.query.version || '')) ? req.query.version : undefined;
    try {
      const html = await api(req).printHtml(req.params.id, version);
      // The print page is produced (and escaped) by the API; it has one print button.
      res.set('Content-Security-Policy',
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'");
      return res.type('html').send(html);
    } catch (err) {
      setFlash(res, flashFromError(err));
      return res.redirect(303, `/pprs/${req.params.id}`);
    }
  });

  app.use(authed);

  // --------------------------------------------------------------- errors
  app.use((req, res) => res.status(404).render('error', { status: 404, error: null }));
  // eslint-disable-next-line no-unused-vars
  app.use((err, req, res, _next) => {
    const status = err instanceof ApiError ? err.status : 500;
    if (status === 401) {
      res.clearCookie(TOKEN_COOKIE, { path: '/' });
      return res.redirect(303, '/login');
    }
    res.locals.flash = flashFromError(err);
    return res.status(status).render('error', { status, error: err });
  });

  return app;
}

module.exports = { createApp, TOKEN_COOKIE };
