// node --test backend/
import test from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import { verifyJwt } from './auth.js';

const SECRET = 'unit-test-secret-0123456789abcdef';
const b64u = (b) => Buffer.from(b).toString('base64').replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
function sign(claims, secret = SECRET, header = { alg: 'HS256', typ: 'JWT' }) {
  const h = b64u(JSON.stringify(header)), p = b64u(JSON.stringify(claims));
  return `${h}.${p}.${b64u(crypto.createHmac('sha256', secret).update(`${h}.${p}`).digest())}`;
}
const good = () => ({ iss: 'crisisgrid', sub: 'u1', name: 'Ada', email: 'ada@example.com', role: 'admin', exp: Date.now() / 1000 + 60 });

test('accepts a valid token and returns the user', () => {
  assert.deepEqual(verifyJwt(sign(good()), SECRET), { id: 'u1', name: 'Ada', email: 'ada@example.com', role: 'admin' });
});

test('rejects wrong key, expiry, wrong issuer', () => {
  assert.equal(verifyJwt(sign(good(), 'another-secret-0123456789'), SECRET), null);
  assert.equal(verifyJwt(sign({ ...good(), exp: Date.now() / 1000 - 1 }), SECRET), null);
  assert.equal(verifyJwt(sign({ ...good(), iss: 'someone-else' }), SECRET), null);
  assert.equal(verifyJwt(sign({ ...good(), exp: undefined }), SECRET), null);
});

test('rejects algorithm confusion and tampering', () => {
  const t = sign(good());
  const [h, p, s] = t.split('.');
  const none = b64u(JSON.stringify({ alg: 'none', typ: 'JWT' }));
  assert.equal(verifyJwt(`${none}.${p}.`, SECRET), null);
  assert.equal(verifyJwt(`${none}.${p}.${s}`, SECRET), null);
  assert.equal(verifyJwt(sign(good(), SECRET, { alg: 'HS512', typ: 'JWT' }), SECRET), null);
  const forged = b64u(JSON.stringify({ ...good(), role: 'admin', sub: 'attacker' }));
  assert.equal(verifyJwt(`${h}.${forged}.${s}`, SECRET), null);
  for (const junk of ['', 'a.b', 'a.b.c.d', 'not-a-token', '...', 'e30.e30.e30']) assert.equal(verifyJwt(junk, SECRET), null, junk);
});

test('interoperates with the Python implementation (same HS256 format)', async () => {
  // a token produced by crisisgrid_api.auth.security.sign_jwt for the same secret and claims shape
  const { execFileSync } = await import('node:child_process');
  const py = process.env.CG_PYTHON || `${process.cwd()}/../.venv/bin/python`;
  let tok;
  try {
    tok = execFileSync(py, ['-c', `
import time
from crisisgrid_api.auth.security import sign_jwt
print(sign_jwt({"iss":"crisisgrid","sub":"py-user","name":"Py","email":"py@example.com","role":"researcher","exp":time.time()+60}, "${SECRET}"))`], { encoding: 'utf8' }).trim();
  } catch { return; } // the Python environment is optional for this unit test
  assert.deepEqual(verifyJwt(tok, SECRET), { id: 'py-user', name: 'Py', email: 'py@example.com', role: 'researcher' });
});
