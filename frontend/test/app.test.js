'use strict';
/**
 * User-interface tests (D-24): the UI forwards to the API and shows its answers; it
 * holds no business rule. A small stub API stands in for the Python API.
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const express = require('express');
const { createApp } = require('../src/app');
const { loadConfig, productionProblems } = require('../src/server');

const SECRET = 'ui-test-cookie-secret-0123456789abcdef';
const TOKEN = 'api-token-SECRET-VALUE';

function stubApi(state) {
  const api = express();
  api.use(express.json());
  const auth = (req, res, next) => {
    if (req.get('authorization') !== `Bearer ${state.token}`) {
      return res.status(401).json({ detail: { code: 'NOT_AUTHENTICATED', message: 'missing token' } });
    }
    return next();
  };
  api.get('/api/health', (req, res) => res.json({ status: 'ok', database: 'ok' }));
  api.get('/api/info', (req, res) => res.json({
    demo: state.demo, current_fiscal_year: 2570, today: '2026-10-15', mock_data_warning: state.mockWarning || null,
  }));
  api.post('/api/auth/login', (req, res) => {
    state.calls.push(['login', req.body.username]);
    if (req.body.password === 'right') return res.json({ token: state.token, token_type: 'Bearer' });
    return res.status(401).json({ detail: { code: 'AUTH_FAILED', message: 'invalid credentials or inactive account' } });
  });
  api.get('/api/me', auth, (req, res) => res.json({
    id: 1, username: 'u', display_name: 'ผู้ใช้ทดสอบ', department_source_id: 'D', roles: state.roles,
  }));
  api.get('/api/masters/:kind', auth, (req, res) => res.json([]));
  api.get('/api/pprs', auth, (req, res) => res.json(state.pprs));
  api.post('/api/pprs/:id/discard', auth, (req, res) => {
    state.calls.push(['discard', req.params.id, req.body]);
    res.status(204).end();
  });
  const check = (prNo) => ({
    pr_no: prNo, found: true, current_fiscal_year: 2570, eligible: true, lines_checked: true,
    required_amount: '10.00',
    header: { pr_no: prNo, pr_date: '2026-10-15', budget_year: 2570, department_id: 'D',
      fund_source_id: 'F', budget_category_id: 'C', requester_name: null, header_total: '10.00', hosxp_status: 'ACTIVE' },
    header_violations: [],
    lines: [],
    options: [],
    ...(state.check || {}),
  });
  api.get('/api/prs/:no/prevalidation', auth, (req, res) => {
    state.calls.push(['check', req.params.no, [].concat(req.query.choice || [])]);
    res.json(check(req.params.no));
  });
  // Wave 12A-2 (D-43)
  api.post('/api/pprs', auth, (req, res) => {
    state.calls.push(['create', req.body]);
    res.status(201).json({ id: 7 });
  });
  api.get('/api/pprs/:id/choices', auth, (req, res) => {
    state.calls.push(['choices', req.params.id, [].concat(req.query.choice || [])]);
    res.json({ stored: state.stored || [], check: check('PR-1') });
  });
  api.put('/api/pprs/:id/choices', auth, (req, res) => {
    state.calls.push(['setChoices', req.params.id, req.body.choices]);
    res.json({ id: Number(req.params.id) });
  });
  api.get('/api/pprs/:id', auth, (req, res) => res.json({
    id: Number(req.params.id), pr_no: 'PR-1', fiscal_year: 2570, department_id: 'D', fund_source_id: 'F',
    pr_budget_category_id: 'C', state: state.pprState || 'DRAFT', ppr_number: state.pprState ? 'PPR-2570-000001' : null, version: state.pprState ? 1 : 0, required_amount: '10.00',
    allocated_amount: '10.00', multi_category: false, adjustment_reference: null, hosxp_pr_status: 'ACTIVE',
    created_at: '2026-10-15T01:00:00Z', updated_at: '2026-10-15T01:00:00Z', items: [],
    allocations: [{ budget_category_id: 'C', amount: '10.00' }],
    created_by_user_id: state.pprCreator === undefined ? 1 : state.pprCreator,
  }));
  api.get('/api/pprs/:id/versions', auth, (req, res) => res.json([]));
  api.get('/api/pprs/:id/timeline', auth, (req, res) => res.json({
    ppr_id: Number(req.params.id), state: state.pprState || 'DRAFT', version: 0,
    last_activity_at: '2026-10-15T01:00:00Z', days_inactive: 3, can_verify: state.canVerify,
    verifications: [],
    events: [{ at: '2026-10-15T01:00:00Z', action: 'PPR_DRAFT_CREATED', actor_user_id: 1,
      actor_name: 'ผู้ขอ <b>x</b>', version: null, state_before: null, state_after: null,
      reason: null, details: {} }],
  }));
  api.get('/api/ppr-search', auth, (req, res) => {
    state.calls.push(['search', new URLSearchParams(req.query).toString()]);
    res.json(state.pprs);
  });
  api.get('/api/procurement/queue', auth, (req, res) => {
    if (!state.roles.includes('PROCUREMENT')) {
      return res.status(403).json({ detail: { code: 'FORBIDDEN', message: 'requires PROCUREMENT_QUEUE_READ' } });
    }
    state.calls.push(['queue', new URLSearchParams(req.query).toString()]);
    return res.json({ bucket: req.query.bucket, inactive_days: 30, can_verify: state.canVerify,
      appointment: null, items: state.pprs,
      counts: { waiting: 2, verified: 1, changed: 0, unlocked: 0, cancelled: 0, inactive: 0 } });
  });
  api.get('/api/appointments', auth, (req, res) => res.json([
    { id: 1, user_id: 5, user_name: 'หัวหน้า ก', user_has_role: true, fiscal_year: 2570,
      effective_from: '2026-10-01', effective_to: '2027-09-30', status: 'ACTIVE',
      effective_today: true, last_verification_on: '2026-10-15', created_by_user_id: 1,
      created_at: '2026-10-01T01:00:00Z', revoked_at: null, revoke_reason: null },
    { id: 2, user_id: 6, user_name: 'หัวหน้า ข', user_has_role: true, fiscal_year: 2570,
      effective_from: '2026-10-01', effective_to: '2027-09-30', status: 'ACTIVE',
      effective_today: false, last_verification_on: null, created_by_user_id: 1,
      created_at: '2026-10-01T01:00:00Z', revoked_at: null, revoke_reason: null },
  ]));
  api.get('/api/appointments/candidates', auth, (req, res) => res.json([]));
  api.post('/api/pprs/:id/verify', auth, (req, res) => {
    state.calls.push(['verify', req.params.id, req.body.version]);
    res.json({ id: Number(req.params.id), ppr_number: 'PPR-2570-000001', version: req.body.version });
  });
  api.get('/api/pprs/:id/eligible-categories', auth, (req, res) => res.json(['C']));
  // Wave 7B-2 (D-38 A-2)
  api.get('/api/pprs/:id/coverage', auth, (req, res) => res.json(state.coverage || []));
  api.put('/api/pprs/:id/coverage', auth, (req, res) => {
    state.calls.push(['coverage', req.params.id, req.body.coverage]);
    res.json(state.coverage || []);
  });
  api.post('/api/pprs/:id/confirm', auth, (req, res) => {
    state.calls.push(['confirm', req.params.id]);
    res.status(409).json({ detail: state.confirmError });
  });
  api.get('/api/pprs/:id/print', auth, (req, res) => res.type('html').send('<p>PRINT</p>'));
  // Wave 6
  const syncRoles = ['PLAN_OFFICER', 'ADMIN'];
  const forbidden = (res, perm) => res.status(403).json({ detail: { code: 'FORBIDDEN', message: `requires ${perm}` } });
  const obs = (over = {}) => ({
    id: 1, sync_run_id: 9, ppr_id: 7, ppr_number: 'PPR-2570-000001', pr_no: 'PR-1', compared_version: 1,
    observed_at: '2026-10-15T01:00:00Z', recorded_at: '2026-10-15T01:00:01Z', outcome: 'INVALID_EVIDENCE',
    evidence_class: 'CONTROL', hosxp_status: 'UNKNOWN', native_status: null, changes: null,
    evidence_issues: [{ code: 'PR_TOTAL_MISMATCH', class: 'CONTROL', message: 'total <b>', field: 'total_amount', line: null }],
    live_pr: null, details: null, state_before: 'CONFIRMED_LOCKED', state_after: 'PR_CHANGED_REVIEW_REQUIRED',
    released: false, anomaly_code: null, error_code: null, error_message: null, ...over,
  });
  const run = (over = {}) => ({
    id: 9, mode: 'MANUAL', scope: 'PPRS', status: 'PARTIAL', started_at: '2026-10-15T01:00:00Z',
    finished_at: '2026-10-15T01:01:00Z', requested_by_user_id: 1, ppr_id: null, retry_of_sync_run_id: null,
    checked: 3, changed: 1, cancelled: 0, non_critical: 0, not_attempted: 0, error_details: null, ...over,
  });
  api.get('/api/pprs/:id/observations', auth, (req, res) => res.json(state.observations || []));
  api.get('/api/pr-sync/runs', auth, (req, res) => (
    state.roles.some((r) => [...syncRoles, 'PROCUREMENT', 'HEAD_OF_PROCUREMENT'].includes(r))
      ? res.json([run(), run({ id: 8, status: 'SUCCEEDED', mode: 'NIGHTLY' })]) : forbidden(res, 'SYNC_READ')));
  api.get('/api/pr-sync/runs/:id', auth, (req, res) => res.json({ run: run({ id: Number(req.params.id) }), observations: [obs()] }));
  api.get('/api/pr-sync/governance', auth, (req, res) => (
    state.roles.some((r) => syncRoles.includes(r)) ? res.json([obs({ outcome: 'NOT_FOUND', evidence_class: null, evidence_issues: null })])
      : forbidden(res, 'SYNC_RUN')));
  api.post('/api/pr-sync', auth, (req, res) => {
    state.calls.push(['pr-sync', req.body.retry_of || null, req.body.background]);
    return res.json({ run: run({ id: 10, status: 'RUNNING', retry_of_sync_run_id: req.body.retry_of || null }), observations: [] });
  });
  api.post('/api/pprs/:id/sync', auth, (req, res) => {
    state.calls.push(['sync', req.params.id]);
    res.json({ run: run({ id: 11, scope: 'PPR', status: 'SUCCEEDED' }), observations: [obs()] });
  });
  api.post('/api/pprs/:id/recheck', auth, (req, res) => {
    state.calls.push(['recheck', req.params.id]);
    res.json({ run: run({ id: 12, scope: 'PPR_RECHECK', status: 'SUCCEEDED' }),
      observations: [obs({ outcome: 'ANOMALY', evidence_class: null, anomaly_code: 'CANCELLED_PR_REACTIVATED_EXTERNALLY' })] });
  });

  // Wave 8A (D-32 ... D-34)
  const orgWide = () => state.roles.some((r) => r !== 'REQUESTER');
  const alertList = () => ({
    alerts: [
      { type: 'PLAN_LOW', subject: 'plan_item', subject_id: 3, fiscal_year: 2570, department_id: 'D',
        since: null, ppr_number: null, pr_no: null, state: null, days_inactive: null, item_id: 'I<1>',
        item_code: 'C1', item_name: 'ถุงมือ <script>', unit: 'กล่อง', remaining_qty: '1', remaining_amount: '100.00',
        remaining_qty_percent: '10.00', remaining_amount_percent: '10.00', sync_scope: null, sync_status: null },
      { type: 'PPR_INACTIVE', subject: 'ppr', subject_id: 7, fiscal_year: 2570, department_id: 'D',
        since: '2026-09-01T01:00:00Z', ppr_number: 'PPR-2570-000001', pr_no: 'PR-1', state: 'CONFIRMED_LOCKED',
        days_inactive: 44, item_id: null, item_code: null, item_name: null, unit: null, remaining_qty: null,
        remaining_amount: null, remaining_qty_percent: null, remaining_amount_percent: null, sync_scope: null, sync_status: null },
      { type: 'SYNC_FAILURE', subject: 'sync_run', subject_id: 9, fiscal_year: null, department_id: null,
        since: '2026-10-15T01:00:00Z', ppr_number: null, pr_no: null, state: null, days_inactive: null, item_id: null,
        item_code: null, item_name: null, unit: null, remaining_qty: null, remaining_amount: null,
        remaining_qty_percent: null, remaining_amount_percent: null, sync_scope: 'PPRS', sync_status: 'PARTIAL',
        sync_failed: 3, sync_pending: 1 },
    ],
    counts: { PPR_INACTIVE: 1, PR_CHANGED: 0, PLAN_LOW: 1, PLAN_EXHAUSTED: 0, SYNC_FAILURE: 0 },
    inactive_days: 30, low_percent: 20, truncated: false,
  });
  const totals = (over = {}) => ({ planned_amount: '1000.00', used_amount: '900.00', remaining_amount: '100.00',
    items: 1, low: 1, exhausted: 0, ...over });
  const balance = { plan_item_id: 3, fiscal_year: 2570, plan_year_state: 'ACTIVE', plan_type: 'DEPARTMENT',
    item_id: 'I1', item_code: 'C1', item_name: 'ถุงมือ', unit: 'กล่อง', owner_department_id: 'D',
    purchasing_department_id: null, fund_source_id: 'F', budget_category_id: 'C', planned_qty: '10',
    planned_amount: '1000.00', used_qty: '9', used_amount: '900.00', remaining_qty: '1', remaining_amount: '100.00',
    alert: 'PLAN_LOW', remaining_qty_percent: '10.00', remaining_amount_percent: '10.00' };
  api.get('/api/dashboard', auth, (req, res) => {
    state.calls.push(['dashboard', req.query.fiscal_year || null]);
    res.json({
      fiscal_year: Number(req.query.fiscal_year || 2570), plan_year_state: 'ACTIVE',
      department_scope: orgWide() ? null : 'D', totals: totals(),
      by_department: [{ department_id: 'D', ...totals() }],
      by_category: [{ fund_source_id: 'F', budget_category_id: 'C', ...totals() }],
      ppr_counts: { DRAFT: 0, CONFIRMED_LOCKED: 1 }, near_exhaustion: [alertList().alerts[0]],
      alerts: alertList(), recent: [],
    });
  });
  api.get('/api/alerts', auth, (req, res) => res.json(alertList()));
  api.get('/api/alert-settings', auth, (req, res) => res.json([
    { code: 'PPR_INACTIVE_DAYS', value: 30, min: 1, max: 365, updated_at: null, updated_by_user_id: null },
    { code: 'PLAN_LOW_PERCENT', value: 20, min: 1, max: 99, updated_at: null, updated_by_user_id: null },
  ]));
  api.put('/api/alert-settings/:code', auth, (req, res) => {
    state.calls.push(['setting', req.params.code, req.body.value, req.body.reason]);
    if (!state.roles.includes('ADMIN')) return forbidden(res, 'ALERT_SETTING_MANAGE');
    return res.json({ code: req.params.code, value: req.body.value, min: 1, max: 99, updated_at: null, updated_by_user_id: 1 });
  });
  api.get('/api/plan-years', auth, (req, res) => res.json(state.years || [
    { fiscal_year: 2570, state: state.yearState || 'ACTIVE', starts_on: '2026-10-01', ends_on: '2027-09-30' },
  ]));
  api.post('/api/plan-years', auth, (req, res) => {
    state.calls.push(['create-year', req.body.fiscal_year]);
    res.status(201).json({ fiscal_year: req.body.fiscal_year, state: 'DRAFT', starts_on: '2027-10-01', ends_on: '2028-09-30' });
  });
  api.post('/api/plan-years/:fy/transition', auth, (req, res) => {
    state.calls.push(['transition', req.params.fy, req.body.target, req.body.reason]);
    res.status(409).json({ detail: { code: 'PLAN_NOT_VALID', message: 'no',
      details: [{ code: 'ENVELOPE_MISMATCH', message: 'category C: difference 5', subject: 'C' }] } });
  });
  api.get('/api/plan-years/:fy/budgets', auth, (req, res) => res.json([
    { id: 1, fiscal_year: 2570, fund_source_id: 'F', budget_category_id: 'C', approved_amount: '1000.00' },
  ]));
  api.post('/api/plan-years/:fy/budgets', auth, (req, res) => {
    state.calls.push(['budget', req.body]);
    res.status(201).json({ id: 2 });
  });
  api.delete('/api/plan-budgets/:id', auth, (req, res) => {
    state.calls.push(['del-budget', req.params.id, new URLSearchParams(req.query).toString()]);
    res.status(204).end();
  });
  api.get('/api/plan-years/:fy/balances', auth, (req, res) => res.json([balance]));
  api.get('/api/plan-years/:fy/validation', auth, (req, res) => {
    state.calls.push(['validation', req.params.fy]);
    res.json({ fiscal_year: 2570, can_activate: false,
      envelope: [{ fund_source_id: 'F', budget_category_id: 'C', approved_amount: '1000.00', planned_total: '995.00', difference: '-5.00', balanced: false }],
      errors: [{ code: 'ENVELOPE_MISMATCH', message: 'm', subject: 'C' }], warnings: [] });
  });
  api.get('/api/plan-items/:id', auth, (req, res) => res.json({ ...balance, id: 3, plan_budget_id: 1,
    estimated_unit_price: '100', q1: null, q2: null, q3: null, q4: null, note: null, demands: [], warnings: [] }));
  api.post('/api/plan-years/:fy/items', auth, (req, res) => {
    state.calls.push(['item', req.body]);
    res.status(422).json({ detail: { code: 'UNKNOWN_MASTER', message: "item 'X' is not a synchronized HOSxP record" } });
  });
  // Wave 12A-1 (D-42): plan import
  const rawBody = express.raw({ type: 'application/octet-stream', limit: '11mb' });
  const importRec = { id: 5, fiscal_year: 2570, file_name: 'แผน.xlsx', file_sha256: 'a'.repeat(64), rows_imported: 2,
    rows_skipped: 1, total_amount: '300.00', state: 'IMPORTED', imported_by_user_id: 1, imported_at: '2026-10-15T01:00:00Z',
    removed_by_user_id: null, removed_at: null, removed_reason: null };
  api.get('/api/plan-years/:fy/imports', auth, (req, res) => res.json([importRec]));
  api.get('/api/plan-years/:fy/import-template', auth, (req, res) => {
    res.set('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    res.send(Buffer.from('TEMPLATE'));
  });
  api.post('/api/plan-years/:fy/imports/preview', auth, rawBody, (req, res) => {
    state.calls.push(['import-preview', req.query.file_name, req.body.toString()]);
    res.json({ fiscal_year: 2570, file_name: req.query.file_name, file_sha256: 'b'.repeat(64),
      can_import: !state.importErrors, rows_to_import: 2, pre_bound: 0, total_amount: '300.00',
      errors_total: state.importErrors ? 1 : 0, warnings_total: 0, skipped_total: 1,
      errors: state.importErrors ? [{ code: 'UNKNOWN_CODE', message: "หน่วยงาน 'X<b>' ไม่มี", sheet: 'หัวข้อ', row: 2, column: 'รหัสหน่วยงานเจ้าของ' }] : [],
      warnings: [], skipped: [{ code: 'ZERO_ROW', message: 'ข้าม', sheet: 'รายการ', row: 4, column: null }],
      budgets: [], duplicate_of: null });
  });
  api.post('/api/plan-years/:fy/imports', auth, rawBody, (req, res) => {
    state.calls.push(['import', req.query.file_name, req.query.expected_sha256, req.body.toString()]);
    res.status(201).json(importRec);
  });
  api.post('/api/plan-imports/:id/remove', auth, (req, res) => {
    state.calls.push(['import-remove', req.params.id, req.body.reason]);
    res.json({ ...importRec, state: 'REMOVED' });
  });
  // Wave 7A (D-37)
  const planItem = { ...balance, id: 3, plan_budget_id: 1, estimated_unit_price: '100.00', q1: null, q2: null,
    q3: null, q4: null, note: null, demands: [], warnings: [], planned_qty: '10.0000', planned_amount: '1000.00' };
  // Wave 7B-2: an Assigned Purchase item with department demand (D-38)
  const assignedItem = { ...planItem, id: 4, plan_type: 'ASSIGNED', item_code: 'C4', item_name: 'สำลี <x>',
    owner_department_id: 'S', purchasing_department_id: 'S',
    demands: [{ department_id: 'D', demand_qty: '6.0000', state: 'INCLUDED_IN_CENTRAL_PURCHASE', covered: '6.0000' },
      { department_id: 'W', demand_qty: '4.0000', state: 'PLANNED', covered: '1.5000' }] };
  api.get('/api/plan-years/:fy/items', auth, (req, res) => res.json(
    state.withAssigned ? [planItem, assignedItem] : [planItem]));
  const amendmentRec = { id: 7, fiscal_year: 2570, amendment_no: 2, approval_document_no: 'สส <1>', approval_date: '2026-10-14',
    reason: 'โอน', created_by_user_id: 1, created_at: '2026-10-15T01:00:00Z',
    changes: [{ target: 'ITEM', target_id: 3, change_kind: 'UPDATE',
      before: { plan_type: 'DEPARTMENT', item_id: 'I1', owner_department_id: 'D', purchasing_department_id: null,
        fund_source_id: 'F', budget_category_id: 'C', planned_qty: '10.0000', estimated_unit_price: '100.00', planned_amount: '1000.00' },
      after: { plan_type: 'DEPARTMENT', item_id: 'I1', owner_department_id: 'D2', purchasing_department_id: null,
        fund_source_id: 'F', budget_category_id: 'C', planned_qty: '12.0000', estimated_unit_price: '100.00', planned_amount: '1200.00' } }] };
  api.get('/api/plan-years/:fy/amendments', auth, (req, res) => res.json([amendmentRec]));
  api.get('/api/plan-amendments/:id', auth, (req, res) => res.json(amendmentRec));
  api.post('/api/plan-years/:fy/amendments/preview', auth, (req, res) => {
    state.calls.push(['amend-preview', req.body]);
    if (!state.roles.includes('PLAN_OFFICER')) return forbidden(res, 'PLAN_AMEND');
    const ok = state.previewOk !== false;
    return res.json({ ok,
      errors: ok ? [] : [{ code: 'BELOW_USED_QTY', message: 'quantity 8 is below the 9 already used', subject: '3' }],
      warnings: [], budgets: [], new_items: [], demands: state.previewDemands || [],
      items: [{ plan_item_id: 3, item_code: 'C1', item_name: 'ถุงมือ', unit: 'กล่อง', used_qty: '9', used_amount: '900.00',
        before: amendmentRec.changes[0].before, after: amendmentRec.changes[0].after, qty_delta: '2', amount_delta: '200.00' }] });
  });
  api.post('/api/plan-years/:fy/amendments', auth, (req, res) => {
    state.calls.push(['amend', req.body]);
    return res.status(201).json(amendmentRec);
  });
  api.get('/api/sync/runs', auth, (req, res) => (state.roles.some((r) => syncRoles.includes(r))
    ? res.json([]) : forbidden(res, 'SYNC_RUN')));

  // Wave 8B (D-35)
  const reportList = () => [
    { code: 'PLAN_REMAINING', title: 'แผนคงเหลือรายรายการ', kind: 'plan', filters: ['fiscal_year', 'item'], available: true },
    { code: 'PPR_REGISTER', title: 'ทะเบียน PPR', kind: 'ppr', filters: ['fiscal_year', 'state', 'date_from', 'date_to', 'action'], states: ['CONFIRMED_LOCKED', 'CANCELLED'], available: true },
    { code: 'AUDIT', title: 'ประวัติการตรวจสอบ (Audit)', kind: 'audit', filters: ['date_from'], available: state.roles.includes('ADMIN') },
  ];
  api.get('/api/reports', auth, (req, res) => res.json(reportList()));
  api.get('/api/reports/:code', auth, (req, res) => {
    state.calls.push(['report', req.params.code, new URLSearchParams(req.query).toString()]);
    if (req.query.date_from === '2026-12-01') {
      return res.status(422).json({ detail: { code: 'INVALID_DATE_RANGE', message: 'bad range' } });
    }
    return res.json({ code: req.params.code, title: 'แผนคงเหลือรายรายการ', kind: 'plan', filters: ['fiscal_year'],
      columns: [{ key: 'item_name', label: 'รายการ', type: 'TEXT' }, { key: 'remaining_amount', label: 'คงเหลือ (บาท)', type: 'MONEY' },
        { key: 'issued_qty', label: 'เบิกจากคลัง (HOSxP)', type: 'QTY' }],
      rows: [{ item_name: '<b>ถุงมือ</b>', remaining_amount: '1234.5', issued_qty: 'ยังไม่มีข้อมูล' }], truncated: false,
      applied: [['ปีงบประมาณ', '2570']], notes: [], generated_at: '2026-10-15T01:00:00Z' });
  });
  api.get('/api/reports/:code/xlsx', auth, (req, res) => {
    state.calls.push(['xlsx', req.params.code, new URLSearchParams(req.query).toString()]);
    res.set('Content-Disposition', 'attachment; filename="PLAN_REMAINING_20261015_0800.xlsx"');
    res.type('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet').send(Buffer.from('PK-FILE'));
  });
  return api;
}

async function listen(app) {
  return new Promise((resolve) => {
    const server = app.listen(0, '127.0.0.1', () => resolve(server));
  });
}

async function setup(over = {}) {
  const state = {
    token: TOKEN, demo: false, roles: ['REQUESTER'], pprs: [], calls: [],
    confirmError: {
      code: 'PR_CHANGED_SINCE_DRAFT', message: 'the PR changed',
      details: [{ scope: 'line', subject: 'L1', field: 'qty', before: '4', after: '5', critical: true }],
    },
    ...over,
  };
  const apiServer = await listen(stubApi(state));
  const apiBase = `http://127.0.0.1:${apiServer.address().port}`;
  const uiServer = await listen(createApp({ apiBase, cookieSecret: SECRET }));
  const base = `http://127.0.0.1:${uiServer.address().port}`;
  const jar = new Map();
  async function req(path, { method = 'GET', form } = {}) {
    const headers = { cookie: [...jar].map(([k, v]) => `${k}=${v}`).join('; ') };
    let body;
    if (form) {
      headers['content-type'] = 'application/x-www-form-urlencoded';
      // An array is a repeated field, as a browser sends it.
      body = new URLSearchParams(Object.entries(form).flatMap(([k, v]) => [].concat(v).map((x) => [k, x]))).toString();
    }
    const res = await fetch(base + path, { method, headers, body, redirect: 'manual' });
    for (const c of res.headers.getSetCookie()) {
      const [pair] = c.split(';');
      const i = pair.indexOf('=');
      const v = pair.slice(i + 1);
      if (v === '' || /Expires=Thu, 01 Jan 1970/.test(c)) jar.delete(pair.slice(0, i));
      else jar.set(pair.slice(0, i), v);
    }
    return { res, text: await res.text(), setCookies: res.headers.getSetCookie() };
  }
  const csrfOf = (html) => (html.match(/name="_csrf" value="([^"]+)"/) || [])[1];
  async function login(password = 'right') {
    const page = await req('/login');
    return req('/login', { method: 'POST', form: { _csrf: csrfOf(page.text), username: 'u', password } });
  }
  const close = () => { uiServer.close(); apiServer.close(); };
  return { state, req, login, csrfOf, jar, close, base };
}

test('D-24: login stores the API token in an HTTP-only SameSite=Strict cookie, never in the page', async () => {
  const t = await setup();
  try {
    const r = await t.login();
    assert.equal(r.res.status, 303);
    assert.equal(r.res.headers.get('location'), '/dashboard');
    const cookie = r.setCookies.find((c) => c.startsWith('ppr_token='));
    assert.ok(cookie && /HttpOnly/i.test(cookie) && /SameSite=Strict/i.test(cookie));
    const page = await t.req('/pprs');
    assert.equal(page.res.status, 200);
    assert.ok(!page.text.includes(TOKEN));
    assert.ok(page.text.includes('ผู้ใช้ทดสอบ'));
  } finally { t.close(); }
});

test('D-24: a wrong password shows the Thai message from the API code', async () => {
  const t = await setup();
  try {
    const r = await t.login('wrong');
    assert.equal(r.res.status, 401);
    assert.ok(r.text.includes('ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง'));
    assert.ok(!t.jar.has('ppr_token'));
  } finally { t.close(); }
});

test('D-24: pages need a login; an expired API session returns to the login page', async () => {
  const t = await setup();
  try {
    const anon = await t.req('/pprs');
    assert.equal(anon.res.status, 303);
    assert.equal(anon.res.headers.get('location'), '/login');
    await t.login();
    t.state.token = 'rotated'; // the API no longer accepts the old token
    const r = await t.req('/pprs');
    assert.equal(r.res.headers.get('location'), '/login');
    assert.ok(!t.jar.has('ppr_token'));
  } finally { t.close(); }
});

test('D-24: a form without the CSRF token is refused before any API call', async () => {
  const t = await setup();
  try {
    await t.login();
    const r = await t.req('/pprs/7/confirm', { method: 'POST', form: { _csrf: 'forged' } });
    assert.equal(r.res.status, 303);
    assert.ok(!t.state.calls.some(([name]) => name === 'confirm'));
  } finally { t.close(); }
});

test('D-24, D-21: the API refusal is shown with its details, not decided by the UI', async () => {
  const t = await setup();
  try {
    await t.login();
    const page = await t.req('/pprs/new?pr_no=X');
    const r = await t.req('/pprs/7/confirm', { method: 'POST', form: { _csrf: t.csrfOf(page.text) } });
    assert.equal(r.res.headers.get('location'), '/pprs/7');
    assert.deepEqual(t.state.calls.at(-1), ['confirm', '7']);
    const detail = await t.req('/pprs/7');
    assert.ok(detail.text.includes('PR_CHANGED_SINCE_DRAFT'));
    assert.ok(detail.text.includes('(D-21)'));
    assert.ok(detail.text.includes('L1'));
    const again = await t.req('/pprs/7');
    assert.ok(!again.text.includes('PR_CHANGED_SINCE_DRAFT'), 'a message is shown once');
  } finally { t.close(); }
});

test('D-24: a long list of problems is never lost to the cookie size limit', async () => {
  const details = Array.from({ length: 40 }, (_, i) => ({
    code: 'QTY_EXCEEDED', message: `line ${i}: requested quantity exceeds the plan remaining`, subject: `PR-1-${i}`,
  }));
  const t = await setup({ confirmError: { code: 'PR_NOT_ELIGIBLE', message: 'no', details } });
  try {
    await t.login();
    const page = await t.req('/pprs/7');
    const r = await t.req('/pprs/7/confirm', { method: 'POST', form: { _csrf: t.csrfOf(page.text) } });
    assert.ok(r.setCookies.every((c) => c.length < 400));
    const detail = await t.req('/pprs/7');
    assert.ok(detail.text.includes('PR-1-39'));
    assert.ok(detail.text.includes('จำนวนเกินกว่าที่แผนเหลืออยู่'));
  } finally { t.close(); }
});

test('D-24: logging in again works while an old session cookie is still present', async () => {
  const t = await setup();
  try {
    await t.login();
    t.state.token = 'second-token';
    const r = await t.login();
    assert.equal(r.res.headers.get('location'), '/dashboard');
  } finally { t.close(); }
});

test('D-24: unexpected errors do not show internal details', async () => {
  const t = await setup();
  try {
    await t.login();
    t.state.pprs = [{ broken: true }]; // makes the list template throw
    const r = await t.req('/pprs');
    assert.equal(r.res.status, 500);
    assert.ok(r.text.includes('เกิดข้อผิดพลาดที่ไม่คาดคิด'));
    assert.ok(!/TypeError|\.ejs|at .+:\d+/.test(r.text));
  } finally { t.close(); }
});

test('D-24: values from HOSxP are escaped (no script injection)', async () => {
  const t = await setup();
  try {
    await t.login();
    const r = await t.req('/pprs/new?pr_no=' + encodeURIComponent('<script>alert(1)</script>'));
    assert.ok(!r.text.includes('<script>alert(1)</script>'));
    assert.ok(r.text.includes('&lt;script&gt;'));
    assert.match(r.res.headers.get('content-security-policy'), /script-src 'none'/);
  } finally { t.close(); }
});

test('D-26: the demo banner shows only in demonstration mode', async () => {
  const t = await setup({ demo: true });
  const u = await setup({ demo: false });
  try {
    assert.ok((await t.req('/login')).text.includes('โหมดทดลอง'));
    assert.ok(!(await u.req('/login')).text.includes('โหมดทดลอง'));
  } finally { t.close(); u.close(); }
});

test('D-24: the print page is the API page, with its own narrow policy', async () => {
  const t = await setup();
  try {
    await t.login();
    const r = await t.req('/pprs/3/print?version=1');
    assert.equal(r.text, '<p>PRINT</p>');
    assert.match(r.res.headers.get('content-security-policy'), /default-src 'none'/);
  } finally { t.close(); }
});

test('D-24: configuration derives the cookie key from the session secret and refuses short keys', () => {
  const cfg = loadConfig({ PPR_SESSION_SECRET: 'x'.repeat(40) }, '/nonexistent/.env');
  assert.equal(cfg.cookieSecret.length, 64);
  assert.equal(cfg.apiBase, 'http://127.0.0.1:8000');
  assert.throws(() => createApp({ apiBase: 'http://x', cookieSecret: 'short' }), /at least 32/);
});

test('Wave 5B: the verify button appears only when the API says this user may verify', async () => {
  const t1 = await setup({ roles: ['HEAD_OF_PROCUREMENT', 'PROCUREMENT'], canVerify: false, pprState: 'CONFIRMED_LOCKED' });
  const t2 = await setup({ roles: ['HEAD_OF_PROCUREMENT', 'PROCUREMENT'], canVerify: true, pprState: 'CONFIRMED_LOCKED' });
  try {
    await t1.login();
    await t2.login();
    assert.ok(!(await t1.req('/pprs/7')).text.includes('ตรวจสอบและยืนยันแล้ว</button>'));
    const page = await t2.req('/pprs/7');
    assert.ok(page.text.includes('ตรวจสอบและยืนยันแล้ว</button>'));
    const r = await t2.req('/pprs/7/verify', { method: 'POST', form: { _csrf: t2.csrfOf(page.text), version: '1' } });
    assert.equal(r.res.headers.get('location'), '/pprs/7');
    assert.deepEqual(t2.state.calls.at(-1), ['verify', '7', 1]);
  } finally { t1.close(); t2.close(); }
});

test('Wave 5B: the timeline shows system events, escaped', async () => {
  const t = await setup();
  try {
    await t.login();
    const page = await t.req('/pprs/7');
    assert.ok(page.text.includes('สร้างฉบับร่าง'));
    assert.ok(page.text.includes('ผู้ขอ &lt;b&gt;x&lt;/b&gt;'));
    assert.ok(page.text.includes('3 วันที่แล้ว'));
  } finally { t.close(); }
});

test('Wave 5B: search forwards only known filters; the API applies the scope', async () => {
  const t = await setup();
  try {
    await t.login();
    const r = await t.req('/search?pr_no=PR-1&fiscal_year=2569&state=CONFIRMED_LOCKED&evil=1&created_from=bad');
    assert.equal(r.res.status, 200);
    const [, qs] = t.state.calls.find(([n]) => n === 'search');
    assert.equal(qs, 'pr_no=PR-1&fiscal_year=2569&state=CONFIRMED_LOCKED');
  } finally { t.close(); }
});

test('Wave 5B: menu links follow roles; the queue refusal comes from the API', async () => {
  const req = await setup({ roles: ['REQUESTER'] });
  const proc = await setup({ roles: ['PROCUREMENT'] });
  try {
    await req.login();
    await proc.login();
    assert.ok(!(await req.req('/pprs')).text.includes('href="/procurement"'));
    assert.equal((await req.req('/procurement')).res.status, 403);
    const q = await proc.req('/procurement?bucket=verified&item=glove&bucket2=x');
    assert.equal(q.res.status, 200);
    assert.ok(q.text.includes('href="/procurement"'));
    const [, qs] = proc.state.calls.find(([n]) => n === 'queue');
    assert.equal(qs, 'item=glove&bucket=verified');
  } finally { req.close(); proc.close(); }
});

test('D-27: revoke is offered only for an appointment never used to verify', async () => {
  const t = await setup({ roles: ['ADMIN'] });
  try {
    await t.login();
    const page = await t.req('/appointments');
    assert.equal(page.res.status, 200);
    assert.ok(!page.text.includes('action="/appointments/1/revoke"'));
    assert.ok(page.text.includes('เพิกถอนไม่ได้'));
    assert.ok(page.text.includes('action="/appointments/2/revoke"'));
    assert.ok(page.text.includes('action="/appointments/1/end"'));
  } finally { t.close(); }
});

test('Wave 6: the sync page follows roles; only Planning/Admin start a sync or see governance', async () => {
  const proc = await setup({ roles: ['PROCUREMENT'] });
  const officer = await setup({ roles: ['PLAN_OFFICER'] });
  const req = await setup({ roles: ['REQUESTER'] });
  try {
    await proc.login();
    await officer.login();
    await req.login();
    assert.ok(!(await req.req('/pprs')).text.includes('href="/pr-sync"'));
    assert.equal((await req.req('/pr-sync')).res.status, 403);
    const p = await proc.req('/pr-sync');
    assert.equal(p.res.status, 200);
    assert.ok(p.text.includes('href="/pr-sync"'));
    assert.ok(!p.text.includes('ซิงค์ PPR ทั้งหมดตอนนี้'));
    assert.ok(!p.text.includes('รายการที่ต้องตรวจสอบ'));
    const o = await officer.req('/pr-sync');
    assert.ok(o.text.includes('ซิงค์ PPR ทั้งหมดตอนนี้'));
    assert.ok(o.text.includes('รายการที่ต้องตรวจสอบ'));
    assert.ok(o.text.includes('ไม่พบ PR ใน HOSxP (ไม่ใช่การยกเลิก)'));
    assert.ok(o.text.includes('ทำซ้ำรายการที่ไม่สำเร็จ'));
    const r = await officer.req('/pr-sync', { method: 'POST', form: { _csrf: officer.csrfOf(o.text), retry_of: '9' } });
    assert.equal(r.res.headers.get('location'), '/pr-sync/runs/10');
    assert.deepEqual(officer.state.calls.at(-1), ['pr-sync', 9, true]);
    const detail = await officer.req('/pr-sync/runs/10');
    assert.ok(detail.text.includes('PR_TOTAL_MISMATCH'));
    assert.ok(!detail.text.includes('http-equiv="refresh"'));  // stub run is PARTIAL
  } finally { proc.close(); officer.close(); req.close(); }
});

test('Wave 6: PPR detail offers sync for open PPRs and re-check for cancelled ones only', async () => {
  const open = await setup({ roles: ['PLAN_OFFICER'], pprState: 'PR_CHANGED_REVIEW_REQUIRED', observations: [{
    id: 1, sync_run_id: 9, ppr_id: 7, ppr_number: 'P', pr_no: 'PR-1', compared_version: 1,
    observed_at: '2026-10-15T01:00:00Z', recorded_at: '2026-10-15T01:00:01Z', outcome: 'CRITICAL_CHANGE',
    evidence_class: null, hosxp_status: 'UNKNOWN', native_status: null,
    changes: [{ scope: 'line', subject: 'L<1>', field: 'qty', before: '1', after: '2', critical: true }],
    evidence_issues: null, live_pr: null, details: null, state_before: 'CONFIRMED_LOCKED',
    state_after: 'PR_CHANGED_REVIEW_REQUIRED', released: false, anomaly_code: null, error_code: null, error_message: null,
  }] });
  const cancelled = await setup({ roles: ['ADMIN'], pprState: 'CANCELLED' });
  const draft = await setup({ roles: ['PLAN_OFFICER'] });
  const proc = await setup({ roles: ['PROCUREMENT'], pprState: 'CONFIRMED_LOCKED' });
  try {
    for (const t of [open, cancelled, draft, proc]) await t.login();
    const page = await open.req('/pprs/7');
    assert.ok(page.text.includes('ซิงค์ PPR นี้กับ HOSxP ตอนนี้'));
    assert.ok(page.text.includes('ปลดล็อกเพื่อแก้ไข'));  // review exit
    assert.ok(page.text.includes('L&lt;1&gt;'));  // escaped
    const r = await open.req('/pprs/7/sync', { method: 'POST', form: { _csrf: open.csrfOf(page.text) } });
    assert.equal(r.res.headers.get('location'), '/pprs/7');
    assert.deepEqual(open.state.calls.at(-1), ['sync', '7']);
    const c = await cancelled.req('/pprs/7');
    assert.ok(!c.text.includes('ซิงค์ PPR นี้กับ HOSxP ตอนนี้'));
    assert.ok(c.text.includes('ตรวจซ้ำกับ HOSxP'));
    await cancelled.req('/pprs/7/recheck', { method: 'POST', form: { _csrf: cancelled.csrfOf(c.text) } });
    assert.deepEqual(cancelled.state.calls.at(-1), ['recheck', '7']);
    assert.ok(!(await draft.req('/pprs/7')).text.includes('สถานะที่สังเกตได้จาก HOSxP'));
    const p = await proc.req('/pprs/7');
    assert.ok(p.text.includes('สถานะที่สังเกตได้จาก HOSxP'));
    assert.ok(!p.text.includes('ซิงค์ PPR นี้กับ HOSxP ตอนนี้'));
  } finally { open.close(); cancelled.close(); draft.close(); proc.close(); }
});

test('D-32: the dashboard is scoped by the API; a requester also sees their department plan', async () => {
  const req = await setup({ roles: ['REQUESTER'] });
  const exec = await setup({ roles: ['EXECUTIVE'] });
  try {
    await req.login();
    await exec.login();
    assert.equal((await req.req('/')).res.headers.get('location'), '/dashboard');
    const r = await req.req('/dashboard?fiscal_year=2570&x=<b>');
    assert.equal(r.res.status, 200);
    assert.ok(r.text.includes('แผนของหน่วยงาน') && r.text.includes('ถุงมือ'));
    assert.ok(!r.text.includes('ตามหน่วยงาน'));
    assert.ok(r.text.includes('<meter'));
    assert.deepEqual(req.state.calls.at(-1), ['dashboard', '2570']);
    const e = await exec.req('/dashboard?fiscal_year=abc');
    assert.deepEqual(exec.state.calls.at(-1), ['dashboard', null]); // invalid year not forwarded
    assert.ok(e.text.includes('ตามหน่วยงาน') && e.text.includes('รายการที่ใกล้หมด'));
    assert.ok(!e.text.includes('แผนของหน่วยงาน'));
    assert.ok(e.text.includes('ถุงมือ &lt;script&gt;'));  // escaped
    assert.ok(!e.text.includes('<form method="post"'.concat(' action="/plans')));
  } finally { req.close(); exec.close(); }
});

test('D-33: alerts page shows derived alerts; only admin gets the threshold form; the API decides', async () => {
  const admin = await setup({ roles: ['ADMIN'] });
  const officer = await setup({ roles: ['PLAN_OFFICER'] });
  try {
    await admin.login();
    await officer.login();
    const a = await admin.req('/alerts');
    assert.ok(a.text.includes('แผนใกล้หมด') && a.text.includes('PPR-2570-000001'));
    assert.ok(a.text.includes('ยังค้าง <strong>1</strong> จาก 3 PPR'));  // D-36
    assert.ok(a.text.includes('action="/alert-settings/PLAN_LOW_PERCENT"'));
    const r = await admin.req('/alert-settings/PLAN_LOW_PERCENT', { method: 'POST',
      form: { _csrf: admin.csrfOf(a.text), value: '10', reason: 'มติ' } });
    assert.equal(r.res.headers.get('location'), '/alerts');
    assert.deepEqual(admin.state.calls.at(-1), ['setting', 'PLAN_LOW_PERCENT', 10, 'มติ']);
    const o = await officer.req('/alerts');
    assert.ok(!o.text.includes('action="/alert-settings/'));
    await officer.req('/alert-settings/PLAN_LOW_PERCENT', { method: 'POST',
      form: { _csrf: officer.csrfOf(o.text), value: '5', reason: 'x' } });
    const after = await officer.req('/alerts');
    assert.ok(after.text.includes('ไม่มีสิทธิ์ทำรายการนี้'));  // the API refused
    const bad = await admin.req('/alert-settings/NOPE', { method: 'POST', form: { _csrf: admin.csrfOf(a.text), value: '1', reason: 'x' } });
    assert.equal(bad.res.headers.get('location'), '/alerts');
    assert.notEqual(admin.state.calls.at(-1)[1], 'NOPE');
  } finally { admin.close(); officer.close(); }
});

test('D-34: plan screens show the API validation and refusals; edits only for the Plan Officer', async () => {
  const officer = await setup({ roles: ['PLAN_OFFICER'], yearState: 'APPROVED' });
  const cfo = await setup({ roles: ['CFO'], yearState: 'APPROVED' });
  try {
    await officer.login();
    await cfo.login();
    const page = await officer.req('/plans/2570');
    assert.equal(page.res.status, 200);
    assert.ok(page.text.includes('ยังเปิดใช้ไม่ได้') && page.text.includes('ENVELOPE_MISMATCH'));
    assert.ok(page.text.includes('name="correction_reason"'));  // APPROVED: D-15 fields
    assert.ok(page.text.includes('เปิดใช้แผน'));
    const t = await officer.req('/plans/2570/transition', { method: 'POST',
      form: { _csrf: officer.csrfOf(page.text), target: 'ACTIVE', reason: 'ok' } });
    assert.equal(t.res.headers.get('location'), '/plans/2570');
    assert.deepEqual(officer.state.calls.at(-1), ['transition', '2570', 'ACTIVE', 'ok']);
    const shown = await officer.req('/plans/2570');
    assert.ok(shown.text.includes('แผนยังไม่ผ่านการตรวจ เปิดใช้ไม่ได้'));
    await officer.req('/plans/2570/budgets/1/delete', { method: 'POST',
      form: { _csrf: officer.csrfOf(page.text), correction_reason: 'พิมพ์ผิด', source_reference: 'หนังสือ 1/2570' } });
    const del = officer.state.calls.at(-1);
    assert.equal(del[0], 'del-budget');
    assert.ok(del[2].includes('correction_reason=') && del[2].includes('source_reference='));
    const c = await cfo.req('/plans/2570');
    assert.ok(!c.text.includes('name="correction_reason"') && !c.text.includes('เพิ่มวงเงิน'));
    assert.ok(!cfo.state.calls.some((x) => x[0] === 'validation'));
    assert.equal((await cfo.req('/plans/abc')).res.status, 404);
  } finally { officer.close(); cfo.close(); }
});

test('D-34: a refused plan item keeps the entered values and shows the API reason', async () => {
  const officer = await setup({ roles: ['PLAN_OFFICER'], yearState: 'DRAFT' });
  try {
    await officer.login();
    const form = await officer.req('/plans/2570/items/new');
    assert.equal(form.res.status, 200);
    const r = await officer.req('/plans/2570/items', { method: 'POST', form: {
      _csrf: officer.csrfOf(form.text), item_id: 'X<1>', plan_budget_id: '1', plan_type: 'ASSIGNED',
      owner_department_id: 'D', planned_qty: '10', estimated_unit_price: '5', planned_amount: '50',
      demand_department: 'D', demand_qty: '10',
    } });
    assert.equal(r.res.status, 422);
    assert.ok(r.text.includes('ไม่พบรหัสนี้ในข้อมูลหลัก'));
    assert.ok(r.text.includes('value="X&lt;1&gt;"'));
    const body = officer.state.calls.at(-1)[1];
    assert.deepEqual(body.demands, [{ department_id: 'D', demand_qty: '10' }]);
    assert.equal(body.plan_type, 'ASSIGNED');
    assert.equal(body.purchasing_department_id, null);
    assert.ok(!('correction_reason' in body));
  } finally { officer.close(); }
});

test('D-35: reports forward known filters, show the API columns escaped, and download the API file', async () => {
  const t = await setup({ roles: ['CFO'] });
  try {
    await t.login();
    const list = await t.req('/reports');
    assert.ok(list.text.includes('ทะเบียน PPR') && !list.text.includes('ประวัติการตรวจสอบ (Audit)'));
    const page = await t.req('/reports/PLAN_REMAINING?fiscal_year=2570&item=%3Cx%3E&evil=1&state=bad!');
    assert.equal(page.res.status, 200);
    assert.deepEqual(t.state.calls.at(-1), ['report', 'PLAN_REMAINING', 'fiscal_year=2570&item=%3Cx%3E']);
    assert.ok(page.text.includes('&lt;b&gt;ถุงมือ&lt;/b&gt;'));
    assert.ok(page.text.includes('1,234.50'));
    // D-39: a text value in a number column ("no data yet") is shown as text, never NaN.
    assert.ok(page.text.includes('ยังไม่มีข้อมูล') && !page.text.includes('NaN'));
    const file = await t.req('/reports/PLAN_REMAINING/xlsx?fiscal_year=2570');
    assert.equal(file.res.status, 200);
    assert.equal(file.res.headers.get('content-disposition'), 'attachment; filename="PLAN_REMAINING_20261015_0800.xlsx"');
    assert.equal(file.text, 'PK-FILE');
    const bad = await t.req('/reports/PPR_REGISTER?go=1&date_from=2026-12-01');
    assert.equal(bad.res.status, 422);
    assert.ok(bad.text.includes('วันที่สิ้นสุดอยู่ก่อนวันที่เริ่ม'));
    assert.equal((await t.req('/reports/bad-code')).res.status, 404);
    const reg = await t.req('/reports/PPR_REGISTER?go=1&action=ppr_confirmed&date_to=31/12/2026');
    assert.deepEqual(t.state.calls.at(-1), ['report', 'PPR_REGISTER', 'action=PPR_CONFIRMED']);
    assert.ok(reg.text.includes('ตัวกรองบางช่องรูปแบบไม่ถูกต้อง'));
    assert.ok(reg.text.includes('value="CANCELLED"') && !reg.text.includes('value="DRAFT"'));

    // A PPR report waits for "show" before calling the API.
    const before = t.state.calls.length;
    await t.req('/reports/PPR_REGISTER');
    assert.equal(t.state.calls.length, before);
  } finally { t.close(); }
});

test('D-26: the demo banner shows on every Wave 8 page too', async () => {
  const t = await setup({ roles: ['PLAN_OFFICER'], demo: true });
  try {
    await t.login();
    for (const path of ['/dashboard', '/alerts', '/plans', '/plans/2570', '/reports', '/reports/PLAN_REMAINING']) {
      const page = await t.req(path);
      assert.equal(page.res.status, 200, path);
      assert.ok(page.text.includes('class="demo-banner"'), path);
    }
  } finally { t.close(); }
});

test('D-37: the amendment form sends only changed values, previews, then records', async () => {
  const officer = await setup({ roles: ['PLAN_OFFICER'] });
  try {
    await officer.login();
    const year = await officer.req('/plans/2570');
    assert.ok(year.text.includes('href="/plans/2570/amendments/new"'));
    const form = await officer.req('/plans/2570/amendments/new');
    assert.equal(form.res.status, 200);
    assert.ok(form.text.includes('name="q_3"') && !form.text.includes('name="i_3"'));  // used item: numbers only
    const fields = {
      _csrf: officer.csrfOf(form.text), approval_document_no: 'สส 1', approval_date: '2026-10-14', reason: 'โอน',
      base_amendment_no: '2', b_1: '1000.00', ob_1: '1000.00', q_3: '12', p_3: '100', a_3: '1200.00',
      oq_3: '10.0000', op_3: '100.00', oa_3: '1000.00',
      n_type: 'DEPARTMENT', n_item: '', n_owner: '', n_buyer: '', n_fund: '', n_cat: '', n_qty: '', n_price: '', n_amount: '', n_note: '',
    };
    const pv = await officer.req('/plans/2570/amendments/preview', { method: 'POST', form: fields });
    assert.equal(pv.res.status, 200);
    const sent = officer.state.calls.at(-1)[1];
    assert.deepEqual(sent.budgets, []);  // unchanged budget line is not sent
    assert.deepEqual(sent.items, [{ plan_item_id: 3, planned_qty: '12', planned_amount: '1200.00' }]);
    assert.equal(sent.base_amendment_no, 2);  // the latest amendment the form was built on
    assert.deepEqual(sent.new_items, []);
    assert.ok(pv.text.includes('บันทึกได้') && pv.text.includes('formaction="/plans/2570/amendments"'));
    assert.ok(pv.text.includes('value="12"'));  // entered values are kept
    const saved = await officer.req('/plans/2570/amendments', { method: 'POST', form: fields });
    assert.equal(saved.res.status, 303);
    assert.equal(saved.res.headers.get('location'), '/plan-amendments/7');
    assert.equal(officer.state.calls.at(-1)[0], 'amend');
    const detail = await officer.req('/plan-amendments/7');
    assert.ok(detail.text.includes('สส &lt;1&gt;') && detail.text.includes('หน่วยงานเจ้าของแผน'));
  } finally { officer.close(); }
});

test('D-37: a refused amendment shows the API problems and no save button', async () => {
  const officer = await setup({ roles: ['PLAN_OFFICER'], previewOk: false });
  try {
    await officer.login();
    const form = await officer.req('/plans/2570/amendments/new');
    const pv = await officer.req('/plans/2570/amendments/preview', { method: 'POST', form: {
      _csrf: officer.csrfOf(form.text), approval_document_no: 'x', approval_date: '2026-10-14', reason: 'r', q_3: '8',
    } });
    assert.ok(pv.text.includes('จำนวนต่ำกว่าที่ใช้ไปแล้ว') && pv.text.includes('ยังบันทึกไม่ได้'));
    assert.ok(!pv.text.includes('formaction="/plans/2570/amendments"'));
  } finally { officer.close(); }
});

test('D-37: only the Plan Officer is offered amendments; everyone may read the history', async () => {
  const cfo = await setup({ roles: ['CFO'] });
  try {
    await cfo.login();
    const year = await cfo.req('/plans/2570');
    assert.ok(year.text.includes('href="/plans/2570/amendments"') && !year.text.includes('/amendments/new'));
    const list = await cfo.req('/plans/2570/amendments');
    assert.ok(list.text.includes('สส &lt;1&gt;') && !list.text.includes('+ ปรับแผน'));
    const form = await cfo.req('/plans/2570/amendments/new');
    const pv = await cfo.req('/plans/2570/amendments/preview', { method: 'POST', form: {
      _csrf: cfo.csrfOf(form.text), approval_document_no: 'x', approval_date: '2026-10-14', reason: 'r', q_3: '12',
    } });
    assert.equal(pv.res.status, 403);  // the API refuses; the UI only shows it
  } finally { cfo.close(); }
});

test('D-37: untouched fields are compared with the form, not the plan as it is now', async () => {
  const officer = await setup({ roles: ['PLAN_OFFICER'] });
  try {
    await officer.login();
    const form = await officer.req('/plans/2570/amendments/new');
    assert.ok(form.text.includes('name="base_amendment_no" value="2"'));
    assert.ok(form.text.includes('name="oq_3" value="10.0000"'));
    // the form was opened when item 3 had 8 (another officer has since amended it to 10):
    // the user changed only the budget line, so item 3 must not be sent back as 8
    await officer.req('/plans/2570/amendments/preview', { method: 'POST', form: {
      _csrf: officer.csrfOf(form.text), approval_document_no: 'x', approval_date: '2026-10-14', reason: 'r',
      base_amendment_no: '1', b_1: '1100.00', ob_1: '1000.00', q_3: '8', oq_3: '8', a_3: '800', oa_3: '800',
    } });
    const sent = officer.state.calls.at(-1)[1];
    assert.deepEqual(sent.items, []);
    assert.equal(sent.base_amendment_no, 1);  // the API refuses a stale form (PLAN_CHANGED_SINCE_FORM)
    assert.deepEqual(sent.budgets, [{ fund_source_id: 'F', budget_category_id: 'C', approved_amount: '1100.00' }]);
  } finally { officer.close(); }
});

test('D-38 A-2: the purchaser states whom an Assigned Purchase PPR buys for', async () => {
  const coverage = [{ plan_item_id: 4, item_code: 'C4', item_name: 'สำลี <x>', unit: 'ห่อ', drawn_qty: '10.0000',
    lines: [
      { department_id: 'D', demand_qty: '6.0000', covered_by_others: '0', state: 'PLANNED', working: '6.0000', confirmed: '0' },
      { department_id: 'W', demand_qty: '4.0000', covered_by_others: '0', state: 'PLANNED', working: '0', confirmed: '0' },
      { department_id: 'X', demand_qty: '0', covered_by_others: '0', state: 'CANCELLED', working: '0', confirmed: '0' },
    ] }];
  const req = await setup({ roles: ['REQUESTER'], coverage });
  try {
    await req.login();
    const page = await req.req('/pprs/7');
    assert.equal(page.res.status, 200);
    assert.ok(page.text.includes('ซื้อแทนหน่วยงาน') && page.text.includes('สำลี &lt;x&gt;'));
    assert.ok(page.text.includes('รอซื้อ') && page.text.includes('ยกเลิกความต้องการ'));
    assert.equal((page.text.match(/name="cov_qty"/g) || []).length, 2);  // a cancelled demand gets no input
    const r = await req.req('/pprs/7/coverage', { method: 'POST', form: {
      _csrf: req.csrfOf(page.text), cov_item: ['4', '4'], cov_dept: ['D', 'W'], cov_qty: ['6', ''],
    } });
    assert.equal(r.res.status, 303);
    assert.deepEqual(req.state.calls.at(-1), ['coverage', '7', [
      { plan_item_id: 4, department_id: 'D', qty: '6' },
      { plan_item_id: 4, department_id: 'W', qty: '0' },
    ]]);
  } finally { req.close(); }
});

test('D-38 A-6: demand is amended through the amendment form and shown in the plan', async () => {
  const officer = await setup({ roles: ['PLAN_OFFICER'], withAssigned: true,
    previewDemands: [{ target: '4', department_id: 'W', before: '4.0000', after: '0' }] });
  try {
    await officer.login();
    const year = await officer.req('/plans/2570');
    assert.ok(year.text.includes('ความต้องการของหน่วยงานในแผนซื้อแทน') && year.text.includes('อยู่ในใบที่ยืนยันแล้ว'));
    assert.ok(year.text.includes('PPR ครอบคลุมแล้ว') && year.text.includes('1.5'));  // A-2 / A-6 floor
    const form = await officer.req('/plans/2570/amendments/new');
    assert.ok(form.text.includes('name="dq_4"') && form.text.includes('name="odq_4" value="6.0000"'));
    const pv = await officer.req('/plans/2570/amendments/preview', { method: 'POST', form: {
      _csrf: officer.csrfOf(form.text), approval_document_no: 'x', approval_date: '2026-10-14', reason: 'r',
      q_4: '10.0000', oq_4: '10.0000',
      dd_4: ['D', 'W', 'Z'], dq_4: ['6', '0', '2'], odq_4: ['6.0000', '4.0000', ''],
      n_type: 'ASSIGNED', n_item: 'I9', n_owner: 'S', n_buyer: 'S', n_fund: 'F', n_cat: 'C',
      n_qty: '3', n_price: '1', n_amount: '3', n_note: '', n_dd_0: ['D', ''], n_dq_0: ['3', ''],
    } });
    assert.equal(pv.res.status, 200);
    const sent = officer.state.calls.at(-1)[1];
    assert.deepEqual(sent.demands, [
      { plan_item_id: 4, department_id: 'W', demand_qty: '0' },
      { plan_item_id: 4, department_id: 'Z', demand_qty: '2' },
    ]);
    assert.deepEqual(sent.new_items[0].demands, [{ department_id: 'D', demand_qty: '3' }]);
    assert.ok(pv.text.includes('ความต้องการรายหน่วยงาน (แผนซื้อแทน)') && pv.text.includes('ยกเลิกความต้องการ'));
  } finally { officer.close(); }
});

test('Wave 10A: production refuses to run without HTTPS cookies, a named proxy and a local API', () => {
  const base = { PPR_SESSION_SECRET: 'x'.repeat(40), PPR_PROFILE: 'production' };
  const bad = loadConfig(base, '/nonexistent/.env');
  assert.equal(productionProblems(bad).length, 2);  // secure cookies, trust proxy
  const remote = loadConfig({ ...base, PPR_UI_SECURE_COOKIES: '1', PPR_UI_TRUST_PROXY: 'loopback',
    PPR_API_URL: 'http://10.0.0.5:8000' }, '/nonexistent/.env');
  assert.deepEqual(productionProblems(remote).map((p) => p.split(' ')[0]), ['PPR_API_URL']);
  const good = loadConfig({ ...base, PPR_UI_SECURE_COOKIES: '1', PPR_UI_TRUST_PROXY: 'loopback',
    PPR_API_URL: 'http://127.0.0.1:8000' }, '/nonexistent/.env');
  assert.deepEqual(productionProblems(good), []);
  const exposed = loadConfig({ ...base, PPR_UI_SECURE_COOKIES: '1', PPR_UI_TRUST_PROXY: 'loopback',
    PPR_UI_HOST: '0.0.0.0' }, '/nonexistent/.env');
  assert.deepEqual(productionProblems(exposed).map((p) => p.split(' ')[0]), ['PPR_UI_HOST']);
  const remoteProxy = loadConfig({ ...base, PPR_UI_SECURE_COOKIES: '1', PPR_UI_TRUST_PROXY: '10.0.0.9',
    PPR_UI_HOST: '10.0.0.20' }, '/nonexistent/.env');
  assert.deepEqual(productionProblems(remoteProxy), []);  // IT's firewall admits only the proxy
  assert.equal(good.trustProxy, 'loopback');
  assert.deepEqual(productionProblems(loadConfig({ PPR_SESSION_SECRET: 'x'.repeat(40) }, '/x')), []);
});

test('D-40: a production database holding MOCK/DEMO data is announced on every page', async () => {
  const warning = 'คำเตือน: ฐานข้อมูลระบบจริงมีข้อมูลทดลอง (MOCK/DEMO) ปนอยู่ <x>';
  const t = await setup({ roles: ['CFO'], mockWarning: warning });
  try {
    await t.login();
    for (const path of ['/dashboard', '/plans', '/reports']) {
      const page = await t.req(path);
      assert.ok(page.text.includes('class="demo-banner mock-warning" role="alert"'), path);
      assert.ok(page.text.includes('ข้อมูลทดลอง (MOCK/DEMO) ปนอยู่ &lt;x&gt;'), path); // escaped
    }
  } finally { await t.close(); }
  const clean = await setup({ roles: ['CFO'] });
  try {
    await clean.login();
    assert.ok(!(await clean.req('/dashboard')).text.includes('mock-warning'));
  } finally { await clean.close(); }
});

test('Wave 10A: /healthz answers alone; ?deep=1 reports whether the API answers', async () => {
  const up = await setup({});
  try {
    const live = await up.req('/healthz');
    assert.equal(live.res.status, 200);
    const deep = await up.req('/healthz?deep=1');
    assert.equal(deep.res.status, 200);
    assert.deepEqual(JSON.parse(deep.text), { status: 'ok', api: 'ok' });
  } finally { up.close(); }
  const app = createApp({ apiBase: 'http://127.0.0.1:9', cookieSecret: SECRET });
  const server = await listen(app);
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    assert.equal((await fetch(`${base}/healthz`)).status, 200);
    assert.equal((await fetch(`${base}/healthz?deep=1`)).status, 503);
  } finally { server.close(); }
});

test('Wave 10A: behind a proxy only the proxy forwarded headers are trusted', () => {
  const app = createApp({ apiBase: 'http://127.0.0.1:9', cookieSecret: SECRET, trustProxy: 'loopback' });
  const trusted = app.get('trust proxy fn');
  assert.equal(trusted('127.0.0.1', 0), true);
  assert.equal(trusted('10.1.2.3', 0), false);
  const direct = createApp({ apiBase: 'http://127.0.0.1:9', cookieSecret: SECRET });
  assert.equal(direct.get('trust proxy fn')('127.0.0.1', 0), false);
});

test('Wave 10A: the access log records the client address, forwarded only by the trusted proxy', async () => {
  const lines = [];
  const original = console.log;
  console.log = (line) => lines.push(String(line));
  try {
    for (const trustProxy of ['loopback', null]) {
      const app = createApp({ apiBase: 'http://127.0.0.1:9', cookieSecret: SECRET, trustProxy, accessLog: true });
      const server = await listen(app);
      try {
        await fetch(`http://127.0.0.1:${server.address().port}/healthz?secret=1`, {
          headers: { 'X-Forwarded-For': '10.1.2.3' },
        });
      } finally { server.close(); }
    }
  } finally { console.log = original; }
  const log = lines.filter((l) => l.startsWith('GET /healthz'));
  assert.equal(log.length, 2);
  assert.match(log[0], /^GET \/healthz 200 \d+ms 10\.1\.2\.3$/);  // behind the proxy
  assert.doesNotMatch(log[1], /10\.1\.2\.3/);  // a client cannot forge its address
  assert.ok(!log.join(' ').includes('secret'));  // no query string
});

test('D-41: only the creator gets the PPR actions; Planning discards an abandoned draft with a reason', async () => {
  const other = await setup({ roles: ['REQUESTER'], pprCreator: 2 });
  try {
    await other.login();
    const page = await other.req('/pprs/7');
    assert.equal(page.res.status, 200);
    assert.ok(!page.text.includes('ยืนยันและล็อก PPR') && !page.text.includes('action="/pprs/7/discard"'));
    assert.ok(page.text.includes('เฉพาะผู้สร้างเท่านั้น'));
  } finally { other.close(); }
  const own = await setup({ roles: ['REQUESTER'] });
  try {
    await own.login();
    const page = await own.req('/pprs/7');
    assert.ok(page.text.includes('ยืนยันและล็อก PPR') && page.text.includes('ยกเลิกฉบับร่าง'));
    const r = await own.req('/pprs/7/discard', { method: 'POST', form: { _csrf: own.csrfOf(page.text) } });
    assert.equal(r.res.status, 303);
    assert.deepEqual(own.state.calls.at(-1), ['discard', '7', {}]);
  } finally { own.close(); }
  const officer = await setup({ roles: ['PLAN_OFFICER'], pprCreator: 2 });
  try {
    await officer.login();
    const page = await officer.req('/pprs/7');
    assert.ok(page.text.includes('เหตุผลในการยกเลิกฉบับร่างของผู้อื่น'));
    assert.ok(!page.text.includes('ยืนยันและล็อก PPR'));
    const r = await officer.req('/pprs/7/discard', { method: 'POST', form: {
      _csrf: officer.csrfOf(page.text), reason: ' ผู้ขอลาออก ' } });
    assert.equal(r.res.status, 303);
    assert.deepEqual(officer.state.calls.at(-1), ['discard', '7', { reason: 'ผู้ขอลาออก' }]);
  } finally { officer.close(); }
});

test('D-42: a plan file is checked, then imported once from the kept copy; nothing is chosen twice', async () => {
  const t = await setup({ roles: ['PLAN_OFFICER'], yearState: 'DRAFT' });
  try {
    await t.login();
    const page = await t.req('/plans/2570/import');
    assert.equal(page.res.status, 200);
    assert.ok(page.text.includes('ดาวน์โหลดแม่แบบนำเข้า') && page.text.includes('enctype="multipart/form-data"'));
    const tpl = await t.req('/plans/2570/import-template');
    assert.equal(tpl.res.status, 200);
    assert.equal(tpl.text, 'TEMPLATE');
    const upload = async (csrf, bytes = 'XLSX-BYTES', name = 'แผน 2570.xlsx') => {
      const fd = new FormData();
      fd.set('_csrf', csrf);
      fd.set('file', new Blob([bytes]), name);
      const headers = { cookie: [...t.jar].map(([k, v]) => `${k}=${v}`).join('; ') };
      const res = await fetch(`${t.base}/plans/2570/import/preview`, { method: 'POST', body: fd, headers, redirect: 'manual' });
      return { res, text: await res.text() };
    };
    // no CSRF token: refused before the API is called
    const forged = await upload('nope');
    assert.equal(forged.res.status, 303);
    assert.ok(!t.state.calls.some((c) => c[0] === 'import-preview'));
    const pv = await upload(t.csrfOf(page.text));
    assert.equal(pv.res.status, 200);
    assert.deepEqual(t.state.calls.at(-1), ['import-preview', 'แผน 2570.xlsx', 'XLSX-BYTES']);
    assert.ok(pv.text.includes('นำเข้าได้') && pv.text.includes('ยืนยันนำเข้า'));
    const pending = (pv.text.match(/name="pending" value="([0-9a-f]{32})"/) || [])[1];
    assert.ok(pending);
    const ok = await t.req('/plans/2570/import/confirm', { method: 'POST', form: { _csrf: t.csrfOf(pv.text), pending, confirm: 'on' } });
    assert.equal(ok.res.status, 303);
    assert.equal(ok.res.headers.get('location'), '/plans/2570');
    assert.deepEqual(t.state.calls.at(-1), ['import', 'แผน 2570.xlsx', 'b'.repeat(64), 'XLSX-BYTES']);
    // the kept copy is used once
    const again = await t.req('/plans/2570/import/confirm', { method: 'POST', form: { _csrf: t.csrfOf(pv.text), pending, confirm: 'on' } });
    assert.equal(again.res.status, 303);
    assert.equal(t.state.calls.filter((c) => c[0] === 'import').length, 1);
    // removal sends the reason
    const rm = await t.req('/plans/2570/imports/5/remove', { method: 'POST', form: { _csrf: t.csrfOf(page.text), reason: 'ผิดไฟล์' } });
    assert.equal(rm.res.status, 303);
    assert.deepEqual(t.state.calls.at(-1), ['import-remove', '5', 'ผิดไฟล์']);
  } finally { t.close(); }
});

test('D-42: errors of a file are listed (escaped) and it cannot be imported', async () => {
  const t = await setup({ roles: ['PLAN_OFFICER'], yearState: 'DRAFT', importErrors: true });
  try {
    await t.login();
    const page = await t.req('/plans/2570/import');
    const fd = new FormData();
    fd.set('_csrf', t.csrfOf(page.text));
    fd.set('file', new Blob(['X']), 'bad.xlsx');
    const headers = { cookie: [...t.jar].map(([k, v]) => `${k}=${v}`).join('; ') };
    const res = await fetch(`${t.base}/plans/2570/import/preview`, { method: 'POST', body: fd, headers, redirect: 'manual' });
    const text = await res.text();
    assert.equal(res.status, 200);
    assert.ok(text.includes('นำเข้าไม่ได้') && text.includes('UNKNOWN_CODE'));
    assert.ok(text.includes('X&lt;b&gt;') && !text.includes('X<b>'));
    assert.ok(!text.includes('name="pending"'));
  } finally { t.close(); }
});

test('D-42: an upload above 10 MB is refused without reaching the API', async () => {
  const t = await setup({ roles: ['PLAN_OFFICER'], yearState: 'DRAFT' });
  try {
    await t.login();
    const page = await t.req('/plans/2570/import');
    const fd = new FormData();
    fd.set('_csrf', t.csrfOf(page.text));
    fd.set('file', new Blob([Buffer.alloc(11 * 1024 * 1024)]), 'big.xlsx');
    const headers = { cookie: [...t.jar].map(([k, v]) => `${k}=${v}`).join('; ') };
    const res = await fetch(`${t.base}/plans/2570/import/preview`, { method: 'POST', body: fd, headers, redirect: 'manual' });
    assert.equal(res.status, 303);
    assert.equal(res.headers.get('location'), '/plans/2570/import');
    for (const c of res.headers.getSetCookie()) { const [pair] = c.split(';'); const i = pair.indexOf('='); t.jar.set(pair.slice(0, i), pair.slice(i + 1)); }
    const back = await t.req('/plans/2570/import');
    assert.ok(back.text.includes('ไฟล์ใหญ่เกิน 10 MB'));
    assert.ok(!t.state.calls.some((c) => c[0] === 'import-preview'));
  } finally { t.close(); }
});

test('D-42: a checked file is confirmed only by the session that checked it', async () => {
  const t = await setup({ roles: ['PLAN_OFFICER'], yearState: 'DRAFT' });
  try {
    await t.login();
    const page = await t.req('/plans/2570/import');
    const fd = new FormData();
    fd.set('_csrf', t.csrfOf(page.text));
    fd.set('file', new Blob(['MINE']), 'mine.xlsx');
    const headers = { cookie: [...t.jar].map(([k, v]) => `${k}=${v}`).join('; ') };
    const pv = await (await fetch(`${t.base}/plans/2570/import/preview`, { method: 'POST', body: fd, headers })).text();
    const pending = (pv.match(/name="pending" value="([0-9a-f]{32})"/) || [])[1];
    assert.ok(pending);
    // another browser session (a new sign-in gets another API token)
    t.state.token = 'another-token';
    t.jar.clear();
    await t.login();
    const other = await t.req('/plans/2570/import');
    const r = await t.req('/plans/2570/import/confirm', { method: 'POST', form: { _csrf: t.csrfOf(other.text), pending, confirm: 'on' } });
    assert.equal(r.res.status, 303);
    assert.equal(r.res.headers.get('location'), '/plans/2570/import');
    assert.ok(!t.state.calls.some((c) => c[0] === 'import'));
  } finally { t.close(); }
});

// ------------------------------------------------------------------ Wave 12A-2 (D-43)
const LINE = {
  pr_item_id: 'PR-1-1', item_id: 'I-TONER', item_code: 'T1', item_name: 'หมึก', qty: '2', unit: 'box',
  unit_price: '5', amount: '10.00', matched_plan_item_id: null, plan_remaining_qty: null,
  plan_remaining_amount: null, passed: false, offered: [11, 12],
  violations: [{ code: 'PLAN_ROW_NOT_CHOSEN', message: 'choose', subject: 'PR-1-1' }],
};
const OPTIONS = [
  { id: 11, item_name: 'หมึก <b>ดำ</b>', unit: 'box', item_id: 'I-TONER', item_code: 'T1', plan_type: 'DEPARTMENT',
    budget_category_id: 'C', owner_department_id: 'D', remaining_qty: '10', remaining_amount: '100' },
  { id: 12, item_name: 'หมึกทั่วไป', unit: 'box', item_id: null, item_code: null, plan_type: 'CENTRAL',
    budget_category_id: 'C', owner_department_id: 'S', remaining_qty: '5', remaining_amount: '50' },
];

test('D-43: the requester chooses a plan row per line, checks it, and the draft carries the checked rows', async () => {
  const t = await setup({ check: { eligible: false, lines: [LINE], options: OPTIONS } });
  try {
    await t.login();
    const page = await t.req('/pprs/new?pr_no=PR-1');
    assert.equal(page.res.status, 200);
    assert.ok(page.text.includes('<select name="choice"'));
    assert.ok(page.text.includes('value="PR-1-1:11"') && page.text.includes('value="PR-1-1:12"'));
    assert.ok(page.text.includes('หมึก &lt;b&gt;ดำ&lt;/b&gt;'), 'plan row names are escaped');
    assert.ok(page.text.includes('ยังไม่ได้เลือกรายการแผนที่ใช้'));
    assert.ok(!page.text.includes('action="/pprs"'), 'no draft before every line passes');
    // the chosen rows go to the API check; a malformed value is dropped by the UI
    t.state.check = { eligible: true, lines: [{ ...LINE, matched_plan_item_id: 12, passed: true, violations: [] }], options: OPTIONS };
    const checked = await t.req('/pprs/new?pr_no=PR-1&choice=PR-1-1:12&choice=bogus');
    assert.deepEqual(t.state.calls.at(-1), ['check', 'PR-1', ['PR-1-1:12']]);
    assert.ok(checked.text.includes('value="PR-1-1:12" selected'));
    assert.ok(checked.text.includes('<input type="hidden" name="choice" value="PR-1-1:12">'));
    const r = await t.req('/pprs', { method: 'POST', form: { _csrf: t.csrfOf(checked.text), pr_no: 'PR-1', choice: ['PR-1-1:12', 'x:y'] } });
    assert.equal(r.res.headers.get('location'), '/pprs/7');
    assert.deepEqual(t.state.calls.find((c) => c[0] === 'create'), [
      'create', { pr_no: 'PR-1', choices: [{ pr_item_id: 'PR-1-1', plan_item_id: 12 }] },
    ]);
  } finally { t.close(); }
});

test('D-43: the creator changes the plan rows of a draft; others only see them', async () => {
  const t = await setup({
    check: { eligible: true, lines: [{ ...LINE, matched_plan_item_id: 11, passed: true, violations: [] }], options: OPTIONS },
    stored: [{ pr_item_id: 'PR-1-1', plan_item_id: 11 }],
  });
  try {
    await t.login();
    const page = await t.req('/pprs/7');
    assert.ok(page.text.includes('action="/pprs/7/choices"'));
    assert.ok(page.text.includes('value="PR-1-1:11" selected'));
    const r = await t.req('/pprs/7/choices', { method: 'POST', form: { _csrf: t.csrfOf(page.text), choice: 'PR-1-1:12' } });
    assert.equal(r.res.headers.get('location'), '/pprs/7');
    assert.deepEqual(t.state.calls.find((c) => c[0] === 'setChoices'), [
      'setChoices', '7', [{ pr_item_id: 'PR-1-1', plan_item_id: 12 }],
    ]);
  } finally { t.close(); }
  const other = await setup({
    pprCreator: 99,
    check: { eligible: true, lines: [{ ...LINE, matched_plan_item_id: 11, passed: true, violations: [] }], options: OPTIONS },
    stored: [{ pr_item_id: 'PR-1-1', plan_item_id: 11 }],
  });
  try {
    await other.login();
    const page = await other.req('/pprs/7');
    assert.ok(!page.text.includes('<select name="choice"'), 'not the creator: no choice form');
    assert.ok(page.text.includes('หมึก &lt;b&gt;ดำ&lt;/b&gt; (box)'));
  } finally { other.close(); }
});
