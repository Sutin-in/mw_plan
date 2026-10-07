'use strict';
/**
 * Thin client for the Python API (D-24). The user interface holds no business rule:
 * it forwards the user's API token and shows what the API answers. The API is the only
 * component that reads the database or HOSxP, and it enforces every permission.
 */

const SYNC_ONE_TIMEOUT_MS = 180_000;
const IMPORT_TIMEOUT_MS = 180_000; // a hospital plan has thousands of rows

class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message || code || `API error ${status}`);
    this.status = status;
    this.code = code || `HTTP_${status}`;
    this.details = Array.isArray(details) ? details : [];
  }
}

function errorFromBody(status, body) {
  const detail = body && typeof body === 'object' ? body.detail : undefined;
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    return new ApiError(status, detail.code, detail.message, detail.details);
  }
  if (Array.isArray(detail)) {
    // FastAPI request validation (422): a list of {loc, msg}
    const msg = detail.map((d) => `${(d.loc || []).slice(1).join('.')}: ${d.msg}`).join('; ');
    return new ApiError(status, 'INVALID_INPUT', msg);
  }
  return new ApiError(status, undefined, typeof detail === 'string' ? detail : undefined);
}

function correctionQuery(c) {
  if (!c || (!c.correction_reason && !c.source_reference)) return '';
  const q = new URLSearchParams();
  q.set('correction_reason', c.correction_reason || '');
  q.set('source_reference', c.source_reference || '');
  return `?${q}`;
}

function choiceList(choices) {
  return Object.entries(choices || {}).map(([line, plan]) => ({
    pr_item_id: line, plan_item_id: Number(plan),
  }));
}

function choiceQuery(choices) {
  const q = new URLSearchParams();
  for (const [line, plan] of Object.entries(choices || {})) q.append('choice', `${line}:${plan}`);
  const text = q.toString();
  return text ? `?${text}` : '';
}

class Api {
  constructor(baseUrl, token, { timeoutMs = 30000 } = {}) {
    this.baseUrl = baseUrl.replace(/\/+$/, '');
    this.token = token || null;
    this.timeoutMs = timeoutMs;
  }

  async request(method, path, body, timeoutMs = this.timeoutMs) {
    const headers = { Accept: 'application/json' };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let res;
    try {
      res = await fetch(this.baseUrl + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
    } catch (err) {
      throw new ApiError(503, 'API_UNREACHABLE', `cannot reach the API (${err.name})`);
    }
    if (res.status === 204) return null;
    const type = res.headers.get('content-type') || '';
    const payload = type.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) throw errorFromBody(res.status, payload);
    return payload;
  }

  get(path) { return this.request('GET', path); }
  post(path, body) { return this.request('POST', path, body === undefined ? {} : body); }
  put(path, body) { return this.request('PUT', path, body); }
  del(path) { return this.request('DELETE', path); }

  // --------------------------------------------------------------- endpoints
  info() { return this.get('/api/info'); }
  login(username, password) { return this.post('/api/auth/login', { username, password }); }
  me() { return this.get('/api/me'); }
  masters(kind) { return this.get(`/api/masters/${encodeURIComponent(kind)}`); }
  // D-43: `choices` maps a PR line (pr_item_id) to the plan row (id) the requester chose.
  prevalidate(prNo, choices = {}) {
    return this.get(`/api/prs/${encodeURIComponent(prNo)}/prevalidation${choiceQuery(choices)}`);
  }
  listPprs() { return this.get('/api/pprs?limit=200'); }
  getPpr(id) { return this.get(`/api/pprs/${id}`); }
  createPpr(prNo, choices = {}) {
    return this.post('/api/pprs', { pr_no: prNo, choices: choiceList(choices) });
  }
  choices(id, trial = null) { return this.get(`/api/pprs/${id}/choices${trial ? choiceQuery(trial) : ''}`); }
  setChoices(id, choices) { return this.put(`/api/pprs/${id}/choices`, { choices: choiceList(choices) }); }
  eligibleCategories(id) { return this.get(`/api/pprs/${id}/eligible-categories`); }
  setAllocations(id, allocations, adjustmentReference) {
    return this.put(`/api/pprs/${id}/allocations`, {
      allocations,
      adjustment_reference: adjustmentReference || null,
    });
  }
  // Wave 7B-2 (D-38 A-2): whom an Assigned Purchase PPR buys for
  coverage(id) { return this.get(`/api/pprs/${id}/coverage`); }
  setCoverage(id, coverage) { return this.put(`/api/pprs/${id}/coverage`, { coverage }); }
  discard(id, reason) { return this.post(`/api/pprs/${id}/discard`, reason ? { reason } : {}); }
  confirm(id) { return this.post(`/api/pprs/${id}/confirm`); }
  unlock(id, reason) { return this.post(`/api/pprs/${id}/unlock`, { reason: reason || null }); }
  versions(id) { return this.get(`/api/pprs/${id}/versions`); }
  // Wave 5B
  search(params) { return this.get(`/api/ppr-search?${params}`); }
  timeline(id) { return this.get(`/api/pprs/${id}/timeline`); }
  queue(bucket, params) {
    const q = new URLSearchParams(params);
    q.set('bucket', bucket);
    return this.get(`/api/procurement/queue?${q}`);
  }
  verify(id, version) { return this.post(`/api/pprs/${id}/verify`, { version: Number(version) }); }
  appointments() { return this.get('/api/appointments'); }
  appointmentCandidates() { return this.get('/api/appointments/candidates'); }
  createAppointment(body) { return this.post('/api/appointments', body); }
  endAppointment(id, effectiveTo, reason) {
    return this.post(`/api/appointments/${id}/end`, { effective_to: effectiveTo, reason: reason || null });
  }
  revokeAppointment(id, reason) {
    return this.post(`/api/appointments/${id}/revoke`, { reason: reason || null });
  }
  // Wave 6 (D-29 ... D-31)
  // A full run continues on the server; the page follows the run (background: true).
  prSync(retryOf) {
    const body = { background: true };
    if (retryOf) body.retry_of = Number(retryOf);
    return this.post('/api/pr-sync', body);
  }
  // One PPR waits for HOSxP (up to its own timeouts), so it may take longer than a page.
  syncPpr(id) { return this.request('POST', `/api/pprs/${id}/sync`, {}, SYNC_ONE_TIMEOUT_MS); }
  recheckPpr(id) { return this.request('POST', `/api/pprs/${id}/recheck`, {}, SYNC_ONE_TIMEOUT_MS); }
  prSyncRuns() { return this.get('/api/pr-sync/runs?limit=50'); }
  prSyncRun(id) { return this.get(`/api/pr-sync/runs/${id}`); }
  prSyncGovernance() { return this.get('/api/pr-sync/governance?limit=200'); }
  observations(id) { return this.get(`/api/pprs/${id}/observations?limit=50`); }
  // Wave 8A (D-32 ... D-34)
  dashboard(fiscalYear) {
    const q = fiscalYear ? `?fiscal_year=${encodeURIComponent(fiscalYear)}` : '';
    return this.get(`/api/dashboard${q}`);
  }
  alerts() { return this.get('/api/alerts'); }
  alertSettings() { return this.get('/api/alert-settings'); }
  setAlertSetting(code, value, reason) {
    return this.put(`/api/alert-settings/${encodeURIComponent(code)}`, { value, reason: reason || null });
  }
  planYears() { return this.get('/api/plan-years'); }
  createPlanYear(fiscalYear) { return this.post('/api/plan-years', { fiscal_year: fiscalYear }); }
  transitionPlanYear(fy, target, reason) {
    return this.post(`/api/plan-years/${fy}/transition`, { target, reason: reason || null });
  }
  budgets(fy) { return this.get(`/api/plan-years/${fy}/budgets`); }
  createBudget(fy, body) { return this.post(`/api/plan-years/${fy}/budgets`, body); }
  updateBudget(id, body) { return this.put(`/api/plan-budgets/${id}`, body); }
  deleteBudget(id, correction) { return this.del(`/api/plan-budgets/${id}${correctionQuery(correction)}`); }
  planItems(fy) { return this.get(`/api/plan-years/${fy}/items`); }
  planItem(id) { return this.get(`/api/plan-items/${id}`); }
  createPlanItem(fy, body) { return this.post(`/api/plan-years/${fy}/items`, body); }
  updatePlanItem(id, body) { return this.put(`/api/plan-items/${id}`, body); }
  deletePlanItem(id, correction) { return this.del(`/api/plan-items/${id}${correctionQuery(correction)}`); }
  planBalances(fy) { return this.get(`/api/plan-years/${fy}/balances`); }
  planValidation(fy) { return this.get(`/api/plan-years/${fy}/validation`); }
  // Wave 7A (D-37)
  amendments(fy) { return this.get(`/api/plan-years/${fy}/amendments`); }
  amendment(id) { return this.get(`/api/plan-amendments/${id}`); }
  previewAmendment(fy, body) { return this.post(`/api/plan-years/${fy}/amendments/preview`, body); }
  createAmendment(fy, body) { return this.post(`/api/plan-years/${fy}/amendments`, body); }
  syncMasters() { return this.request('POST', '/api/sync/masters', {}, SYNC_ONE_TIMEOUT_MS); }
  masterSyncRuns() { return this.get('/api/sync/runs?limit=10'); }
  // Wave 8B (D-35)
  reports() { return this.get('/api/reports'); }
  report(code, params) { return this.get(`/api/reports/${encodeURIComponent(code)}?${params}`); }
  async reportFile(code, params) {
    const headers = { Accept: '*/*' };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    let res;
    try {
      res = await fetch(`${this.baseUrl}/api/reports/${encodeURIComponent(code)}/xlsx?${params}`, {
        headers, signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch (err) {
      throw new ApiError(503, 'API_UNREACHABLE', `cannot reach the API (${err.name})`);
    }
    if (!res.ok) {
      const type = res.headers.get('content-type') || '';
      throw errorFromBody(res.status, type.includes('application/json') ? await res.json() : await res.text());
    }
    return {
      body: Buffer.from(await res.arrayBuffer()),
      type: res.headers.get('content-type') || 'application/octet-stream',
      disposition: res.headers.get('content-disposition') || 'attachment',
    };
  }
  // Wave 12A-1 (D-42): plan import from Excel
  async binary(path) {
    const headers = { Accept: '*/*' };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    let res;
    try {
      res = await fetch(this.baseUrl + path, { headers, signal: AbortSignal.timeout(this.timeoutMs) });
    } catch (err) {
      throw new ApiError(503, 'API_UNREACHABLE', `cannot reach the API (${err.name})`);
    }
    if (!res.ok) {
      const type = res.headers.get('content-type') || '';
      throw errorFromBody(res.status, type.includes('application/json') ? await res.json() : await res.text());
    }
    return {
      body: Buffer.from(await res.arrayBuffer()),
      type: res.headers.get('content-type') || 'application/octet-stream',
      disposition: res.headers.get('content-disposition') || 'attachment',
    };
  }
  async upload(path, data) {
    const headers = { Accept: 'application/json', 'Content-Type': 'application/octet-stream' };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    let res;
    try {
      res = await fetch(this.baseUrl + path, {
        method: 'POST', headers, body: data, signal: AbortSignal.timeout(Math.max(this.timeoutMs, IMPORT_TIMEOUT_MS)),
      });
    } catch (err) {
      throw new ApiError(503, 'API_UNREACHABLE', `cannot reach the API (${err.name})`);
    }
    const type = res.headers.get('content-type') || '';
    const payload = type.includes('application/json') ? await res.json() : await res.text();
    if (!res.ok) throw errorFromBody(res.status, payload);
    return payload;
  }
  importTemplate(fy) { return this.binary(`/api/plan-years/${encodeURIComponent(fy)}/import-template`); }
  importPreview(fy, data, fileName) {
    const q = new URLSearchParams({ file_name: fileName });
    return this.upload(`/api/plan-years/${encodeURIComponent(fy)}/imports/preview?${q}`, data);
  }
  importPlan(fy, data, fileName, sha256) {
    const q = new URLSearchParams({ file_name: fileName, expected_sha256: sha256 });
    return this.upload(`/api/plan-years/${encodeURIComponent(fy)}/imports?${q}`, data);
  }
  planImports(fy) { return this.get(`/api/plan-years/${encodeURIComponent(fy)}/imports`); }
  removeImport(id, reason) { return this.post(`/api/plan-imports/${encodeURIComponent(id)}/remove`, { reason }); }
  printHtml(id, version) {
    const q = version ? `?version=${encodeURIComponent(version)}` : '';
    return this.get(`/api/pprs/${id}/print${q}`);
  }
}

module.exports = { Api, ApiError };
