'use strict';
/**
 * Start the user interface:  node src/server.js   (or: npm start)
 *
 * Settings come from the environment, then from the repository's git-ignored .env:
 *   PPR_API_URL            Python API address          (default http://127.0.0.1:8000)
 *   PPR_UI_HOST            address to listen on        (default 127.0.0.1)
 *   PPR_UI_PORT            port to listen on           (default 3000)
 *   PPR_UI_COOKIE_SECRET   cookie signing key (>= 32 characters); if absent it is derived
 *                          from PPR_SESSION_SECRET, so one secret in .env is enough
 *   PPR_UI_SECURE_COOKIES  "1" when the site is served over HTTPS (required in production)
 *   PPR_UI_TRUST_PROXY     the reverse proxy to trust for X-Forwarded-* headers, in Express
 *                          "trust proxy" form: "loopback" (proxy on this server) or its address
 *   PPR_PROFILE            "production" refuses to start without secure cookies (HTTPS)
 */

const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { createApp } = require('./app');

function parseEnvFile(file) {
  const out = {};
  if (!fs.existsSync(file)) return out;
  for (const raw of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith('#') || !line.includes('=')) continue;
    const i = line.indexOf('=');
    let value = line.slice(i + 1).trim();
    if (value.length >= 2 && value[0] === value[value.length - 1] && `"'`.includes(value[0])) {
      value = value.slice(1, -1);
    }
    out[line.slice(0, i).trim()] = value;
  }
  return out;
}

function loadConfig(env = process.env, envFile = path.join(__dirname, '..', '..', '.env')) {
  // Only the keys the user interface needs (the .env also holds database URLs).
  const wanted = (k) => k === 'PPR_API_URL' || k === 'PPR_SESSION_SECRET' || k === 'PPR_PROFILE'
    || k.startsWith('PPR_UI_');
  const pick = (o) => Object.fromEntries(Object.entries(o).filter(([k]) => wanted(k)));
  const merged = { ...pick(parseEnvFile(envFile)), ...pick(env) };
  let cookieSecret = merged.PPR_UI_COOKIE_SECRET;
  if (!cookieSecret && merged.PPR_SESSION_SECRET) {
    cookieSecret = crypto.createHmac('sha256', merged.PPR_SESSION_SECRET).update('ppr-ui-cookie').digest('hex');
  }
  return {
    apiBase: merged.PPR_API_URL || 'http://127.0.0.1:8000',
    host: merged.PPR_UI_HOST || '127.0.0.1',
    port: Number(merged.PPR_UI_PORT || 3000),
    cookieSecret,
    secureCookies: merged.PPR_UI_SECURE_COOKIES === '1',
    trustProxy: merged.PPR_UI_TRUST_PROXY || null,
    production: (merged.PPR_PROFILE || 'development').trim().toLowerCase() === 'production',
  };
}

/** Why this configuration may not run in production (empty = fine). */
function productionProblems(config) {
  if (!config.production) return [];
  const out = [];
  if (!config.secureCookies) {
    out.push('PPR_UI_SECURE_COOKIES=1 is required: the site is served over HTTPS (reverse proxy)');
  }
  if (!config.trustProxy) {
    out.push('PPR_UI_TRUST_PROXY is required: name the reverse proxy ("loopback" or its address)');
  }
  const local = (h) => ['127.0.0.1', 'localhost', '::1', '[::1]'].includes(h);
  if (config.trustProxy === 'loopback' && !local(config.host)) {
    out.push('PPR_UI_HOST must be 127.0.0.1 when the proxy is on this server (loopback): '
      + 'otherwise users could reach the interface without HTTPS');
  }
  if (!/^http:\/\/(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$/.test(config.apiBase)) {
    out.push('PPR_API_URL must be the API on this server (http://127.0.0.1:<port>)');
  }
  return out;
}

if (require.main === module) {
  const config = loadConfig();
  if (!config.cookieSecret) {
    console.error('error: set PPR_SESSION_SECRET (or PPR_UI_COOKIE_SECRET) in .env');
    process.exit(2);
  }
  const problems = productionProblems(config);
  if (problems.length) {
    for (const p of problems) console.error(`error: production: ${p}`);
    process.exit(2);
  }
  const app = createApp({ ...config, accessLog: true });
  app.listen(config.port, config.host, () => {
    console.log(`PPR user interface on http://${config.host}:${config.port}  (API: ${config.apiBase})`);
  });
}

module.exports = { loadConfig, parseEnvFile, productionProblems };
