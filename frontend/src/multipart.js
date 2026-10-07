'use strict';
/**
 * Minimal multipart/form-data reader for the plan import upload (Wave 12A-1, D-42).
 *
 * Only what the import form sends is accepted: text fields and at most one file field.
 * The whole request is limited to `limit` bytes (the API refuses files above 10 MB anyway);
 * nothing is written to disk. No external package is needed, so the hospital's offline
 * installation stays as it is.
 */

class MultipartError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function boundaryOf(contentType) {
  const m = /^multipart\/form-data;.*\bboundary=(?:"([^"]{1,70})"|([^\s;]{1,70}))/i.exec(contentType || '');
  return m ? (m[1] || m[2]) : null;
}

function headerValue(headers, name) {
  const line = headers.find((h) => h.toLowerCase().startsWith(`${name.toLowerCase()}:`));
  return line ? line.slice(name.length + 1).trim() : '';
}

function dispositionParam(disposition, key) {
  // name="x"; filename="y.xlsx" (a filename* RFC 5987 form is used as plain UTF-8)
  const star = new RegExp(`\\b${key}\\*=(?:UTF-8'')?([^;]+)`, 'i').exec(disposition);
  if (star) {
    try { return decodeURIComponent(star[1].trim().replace(/^"|"$/g, '')); } catch { /* below */ }
  }
  const m = new RegExp(`\\b${key}="((?:[^"\\\\]|\\\\.)*)"`, 'i').exec(disposition);
  return m ? m[1].replace(/\\(.)/g, '$1') : null;
}

function parse(buffer, boundary) {
  const delimiter = Buffer.from(`--${boundary}`);
  const fields = {};
  let file = null;
  let pos = buffer.indexOf(delimiter);
  if (pos < 0) throw new MultipartError('BAD_UPLOAD', 'the upload is not a form');
  for (;;) {
    pos += delimiter.length;
    if (buffer.slice(pos, pos + 2).toString() === '--') break; // closing delimiter
    if (buffer.slice(pos, pos + 2).toString() !== '\r\n') throw new MultipartError('BAD_UPLOAD', 'malformed form');
    pos += 2;
    const headerEnd = buffer.indexOf('\r\n\r\n', pos);
    if (headerEnd < 0) throw new MultipartError('BAD_UPLOAD', 'malformed form');
    const headers = buffer.slice(pos, headerEnd).toString('utf8').split('\r\n');
    const next = buffer.indexOf(Buffer.concat([Buffer.from('\r\n'), delimiter]), headerEnd + 4);
    if (next < 0) throw new MultipartError('BAD_UPLOAD', 'malformed form');
    const body = buffer.slice(headerEnd + 4, next);
    const disposition = headerValue(headers, 'content-disposition');
    const name = dispositionParam(disposition, 'name');
    const filename = dispositionParam(disposition, 'filename');
    if (name) {
      if (filename !== null) {
        if (file) throw new MultipartError('BAD_UPLOAD', 'one file only');
        file = { field: name, filename: filename.split(/[\\/]/).pop().slice(0, 200), data: body };
      } else if (Object.keys(fields).length < 20) {
        fields[name] = body.toString('utf8').slice(0, 2000);
      }
    }
    pos = next + 2;
  }
  return { fields, file };
}

/** Express middleware: reads the form into req.body (fields) and req.upload (the file). */
function multipart({ limit, drainLimit = limit * 3 }) {
  return (req, res, next) => {
    const boundary = boundaryOf(req.headers['content-type']);
    if (req.method !== 'POST' || !boundary) return next();
    let done = false;
    let over = false;
    const finish = (err) => {
      if (done) return;
      done = true;
      next(err);
    };
    const markTooLarge = () => {
      over = true;
      req.uploadError = new MultipartError('FILE_TOO_LARGE', 'file too large');
      req.body = {};
    };
    // A file above the limit is read and thrown away up to `drainLimit` before the answer,
    // so the browser receives the "file too large" page (a connection closed while it is
    // still sending shows as a network error, notably on Windows). Beyond `drainLimit`
    // (an endless or absurd upload) the connection is closed.
    const cutOff = () => {
      markTooLarge();
      res.on('finish', () => req.destroy());
      req.pause();
      finish();
    };
    const declared = Number.parseInt(req.headers['content-length'] || '0', 10);
    if (declared > drainLimit) return cutOff();
    if (declared > limit) markTooLarge();
    const chunks = [];
    let size = 0;
    req.on('data', (chunk) => {
      if (done) return;
      size += chunk.length;
      if (size > drainLimit) { cutOff(); return; }
      if (size > limit && !over) markTooLarge();
      if (!over) chunks.push(chunk);
    });
    req.on('error', (err) => finish(err));
    req.on('end', () => {
      if (done) return;
      if (over) { finish(); return; }
      try {
        const { fields, file } = parse(Buffer.concat(chunks), boundary);
        req.body = fields;
        req.upload = file;
      } catch (err) {
        req.body = {};
        req.uploadError = err instanceof MultipartError ? err : new MultipartError('BAD_UPLOAD', 'bad upload');
      }
      finish();
    });
    return undefined;
  };
}

module.exports = { multipart, parse, boundaryOf, MultipartError };
