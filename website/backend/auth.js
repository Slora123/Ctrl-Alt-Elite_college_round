// Verifies the same signed tokens the CrisisGrid API issues (HS256, shared CG_AUTH_SECRET) and enforces roles/ownership.
// No dependencies: JWT verification is a few lines of HMAC. API tokens ("cgt_...") are validated by asking the CrisisGrid
// API (they can be revoked, which a stateless check cannot see); results are cached for 30 s.
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';

export const AUTH_ENABLED = !['0', 'false', 'no'].includes((process.env.CG_AUTH_ENABLED || '1').toLowerCase());
const AUTH_URL = process.env.CG_AUTH_URL || 'http://127.0.0.1:8100';

export function loadSecret(baseDir) {
  if (process.env.CG_AUTH_SECRET) return process.env.CG_AUTH_SECRET;
  // dev convenience: the Python API writes a generated secret next to its data
  for (const f of [process.env.CG_AUTH_SECRET_FILE, path.join(process.env.CG_DATA_DIR || '', '.cg_auth_secret'), path.join(baseDir, '..', '..', '.cg_auth_secret'), path.join(baseDir, '..', '..', 'api', '.cg_auth_secret')]) {
    try { if (f && fs.existsSync(f)) return fs.readFileSync(f, 'utf8').trim(); } catch { /* try next */ }
  }
  return null;
}

const b64u = (s) => Buffer.from(s.replace(/-/g, '+').replace(/_/g, '/'), 'base64');

export function verifyJwt(token, secret) {
  try {
    const [h, b, s] = token.split('.');
    if (!h || !b || !s) return null;
    if (JSON.parse(b64u(h).toString()).alg !== 'HS256') return null;
    const want = crypto.createHmac('sha256', secret).update(`${h}.${b}`).digest();
    const got = b64u(s);
    if (want.length !== got.length || !crypto.timingSafeEqual(want, got)) return null;
    const c = JSON.parse(b64u(b).toString());
    const now = Date.now() / 1000;
    if (c.iss !== 'crisisgrid' || !(c.exp > now)) return null;
    return { id: c.sub, name: c.name, email: c.email, role: c.role };
  } catch { return null; }
}

const cache = new Map();
async function viaApi(token) {
  const hit = cache.get(token);
  if (hit && hit.until > Date.now()) return hit.user;
  try {
    const r = await fetch(`${AUTH_URL}/auth/me`, { headers: { Authorization: `Bearer ${token}` } });
    const user = r.ok ? await r.json() : null;
    cache.set(token, { user, until: Date.now() + 30_000 });
    return user && { id: user.id, name: user.name, email: user.email, role: user.role };
  } catch { return null; }
}

export function authMiddleware(secret) {
  const LOCAL = { id: 'local', name: 'local', email: 'local@localhost', role: 'admin' };
  return async (req, res, next) => {
    if (!AUTH_ENABLED) { req.user = LOCAL; return next(); }
    if (!secret) return res.status(503).json({ detail: 'authentication is enabled but CG_AUTH_SECRET is not configured' });
    const h = req.headers.authorization || '';
    const token = h.toLowerCase().startsWith('bearer ') ? h.slice(7).trim() : null;
    const user = token ? (token.startsWith('cgt_') ? await viaApi(token) : verifyJwt(token, secret)) : null;
    if (!user) return res.status(401).json({ detail: 'missing or invalid credentials' });
    if (req.method !== 'GET' && req.method !== 'HEAD' && user.role === 'viewer') return res.status(403).json({ detail: 'your role is read-only' });
    req.user = user;
    next();
  };
}

export const isAdmin = (u) => u && u.role === 'admin';
