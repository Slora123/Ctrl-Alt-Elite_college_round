// Express-level behaviour of the auth middleware and ownership checks, exercised against a real server instance.
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const SECRET = 'middleware-test-secret-0123456789abcdef';
const PORT = 18000 + Math.floor(Math.random() * 1000);  // never collide with a running dev system on 8000
const b64u = (b) => Buffer.from(b).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const token = (id, role = 'researcher', exp = Date.now() / 1000 + 600) => {
  const h = b64u(JSON.stringify({ alg: 'HS256', typ: 'JWT' })), p = b64u(JSON.stringify({ iss: 'crisisgrid', sub: id, name: id, email: `${id}@x.io`, role, exp }));
  return `${h}.${p}.${b64u(crypto.createHmac('sha256', SECRET).update(`${h}.${p}`).digest())}`;
};
const H = (t) => ({ authorization: `Bearer ${t}` });

let server, dir;
async function up() {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cg-node-'));
  fs.cpSync(here, dir, { recursive: true, filter: (s) => !s.includes('node_modules') && !s.endsWith('.db') && !s.includes('uploads') });
  fs.symlinkSync(path.join(here, '..', 'node_modules'), path.join(dir, 'node_modules'));
  server = spawn(process.execPath, ['server.js'], { cwd: dir, env: { ...process.env, PORT: String(PORT), CG_AUTH_SECRET: SECRET, CG_SEED: '0', CG_AUTH_ENABLED: '1' }, stdio: 'ignore' });
  for (let i = 0; i < 60; i++) { try { if ((await fetch(`http://127.0.0.1:${PORT}/api/health`)).ok) return; } catch { /* starting */ } await new Promise((r) => setTimeout(r, 250)); }
  throw new Error('node backend did not start (port clash?)');
}
const down = () => { server?.kill(); fs.rmSync(dir, { recursive: true, force: true }); };
const api = (p, o = {}) => fetch(`http://127.0.0.1:${PORT}/api${p}`, o);

function pdf(text) { // a tiny valid one-page PDF
  const body = `BT /F1 12 Tf 20 100 Td (${text}) Tj ET`;
  const objs = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 200] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
    `<< /Length ${body.length} >>\nstream\n${body}\nendstream`, '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'];
  let out = '%PDF-1.4\n'; const off = [];
  objs.forEach((o, i) => { off.push(out.length); out += `${i + 1} 0 obj\n${o}\nendobj\n`; });
  const x = out.length; out += `xref\n0 ${objs.length + 1}\n0000000000 65535 f \n` + off.map((o) => `${String(o).padStart(10, '0')} 00000 n \n`).join('');
  return Buffer.from(out + `trailer\n<< /Size ${objs.length + 1} /Root 1 0 R >>\nstartxref\n${x}\n%%EOF`);
}
async function upload(tok, name = 'p.pdf') {
  const fd = new FormData(); fd.append('file', new Blob([pdf('Learning rate 0.1 batch size 128')], { type: 'application/pdf' }), name);
  return api('/papers', { method: 'POST', body: fd, headers: H(tok) });
}

test.before(up);
test.after(down);

test('health is public; everything else requires a valid token', async () => {
  assert.equal((await api('/health')).status, 200);
  for (const p of ['/papers', '/overview', '/history', '/me', '/items/claims']) assert.equal((await api(p)).status, 401, p);
  assert.equal((await api('/papers', { headers: H('junk') })).status, 401);
  assert.equal((await api('/papers', { headers: H(token('u1', 'researcher', Date.now() / 1000 - 5)) })).status, 401);
  assert.equal((await api('/me', { headers: H(token('u1')) })).status, 200);
});

test('viewers are read-only', async () => {
  const v = token('viewer1', 'viewer');
  assert.equal((await api('/papers', { headers: H(v) })).status, 200);
  assert.equal((await upload(v)).status, 403);
});

test('users only see and touch their own papers; admins see all', async () => {
  const a = token('alice'), b = token('bob'), admin = token('root', 'admin');
  const up = await upload(a);
  assert.equal(up.status, 200);
  const { id } = await up.json();
  assert.deepEqual((await (await api('/papers', { headers: H(a) })).json()).map((p) => p.id), [id]);
  assert.deepEqual(await (await api('/papers', { headers: H(b) })).json(), []);
  assert.equal((await api(`/papers/${id}`, { headers: H(a) })).status, 200);
  assert.equal((await api(`/papers/${id}`, { headers: H(b) })).status, 404);          // :pid guard
  assert.equal((await api(`/papers/${id}/report`, { headers: H(b) })).status, 404);
  assert.equal((await api(`/graph/${id}`, { headers: H(b) })).status, 404);
  assert.equal((await api(`/papers/${id}`, { method: 'DELETE', headers: H(b) })).status, 404);
  assert.equal((await api(`/papers/${id}`, { headers: H(admin) })).status, 200);
  // items hang off papers: bob cannot create or list them
  const mk = (tok) => api('/items/claims', { method: 'POST', headers: { ...H(tok), 'content-type': 'application/json' }, body: JSON.stringify({ paper_id: id, statement: 'acc', reported: 0.9 }) });
  assert.equal((await mk(b)).status, 404);
  const made = await mk(a);
  assert.equal(made.status, 200);
  const iid = (await made.json()).id;
  assert.equal((await api(`/items/claims/${iid}`, { method: 'DELETE', headers: H(b) })).status, 404);   // :iid guard
  assert.deepEqual(await (await api('/items/claims', { headers: H(b) })).json(), []);
  assert.equal((await (await api('/items/claims', { headers: H(a) })).json()).length, 1);
  const ov = await (await api('/overview', { headers: H(b) })).json();
  assert.deepEqual(ov.papers, []);
  const hist = await (await api('/history', { headers: H(b) })).json();
  assert.ok(hist.every((h) => h.paper_id == null || h.paper_id !== id));
});

test('only admins may reseed', async () => {
  assert.equal((await api('/seed', { method: 'POST', headers: H(token('alice')) })).status, 403);
});
