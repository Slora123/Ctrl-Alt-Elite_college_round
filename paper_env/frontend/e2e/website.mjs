// Browser E2E of the INTEGRATED system through the website: sign-up, auth guard, account/API tokens, pipeline page,
// and the notebook running inside the website with the shared login.  Usage: BASE=http://localhost:5173 node e2e/website.mjs
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import os from 'node:os';
import path from 'node:path';

const BASE = process.env.BASE || 'http://localhost:5173';
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const EMAIL = `e2e-${Date.now()}@example.com`, PW = 'e2e-correct-horse-battery';
const results = [];
const check = (name, ok, detail = '') => { results.push([name, ok]); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  -> ' + detail}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..', '..');
// the pipeline's own PDF parser is strict, so build the fixture with the API test helper
function makePdf(file) {
  execFileSync(process.env.CG_PYTHON || path.join(ROOT, 'api', '.venv', 'bin', 'python'), ['-c',
    "import sys; from pdfgen import make_pdf; open(sys.argv[1],'wb').write(make_pdf([['We train ResNet-20 on CIFAR-10 with a learning rate of 0.1 and a batch size of 128 for 160 epochs using SGD.', 'Our model achieves 91.25% top-1 accuracy on the test set.']]))", file],
    { cwd: path.join(ROOT, 'api', 'tests') });
}

const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 1000 });
const errors = [];
const notFound = [];
page.on('response', (r) => { if (r.status() === 404) notFound.push(r.url()); });
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error' && !/401|403|404|409|503|Failed to load resource/.test(m.text())) errors.push(m.text()); });
const waitFor = async (fn, t = 40000, ...a) => { try { await page.waitForFunction(fn, { timeout: t }, ...a); return true; } catch { return false; } };
const text = () => page.evaluate(() => document.body.innerText);
const mainText = () => page.evaluate(() => (document.querySelector('main')?.innerText ?? 'NO <main>: ' + document.body.innerText.slice(0, 200)).replace(/\n+/g, ' | ').slice(0, 400));
const clickText = (sel, label) => page.evaluate((sel, label) => { const el = [...document.querySelectorAll(sel)].find((e) => e.textContent.trim().includes(label)); if (!el) return false; el.click(); return true; }, sel, label);
const typeInto = async (sel, value) => { await page.click(sel, { clickCount: 3 }); await page.type(sel, value); };

// 1. the dashboard is behind the login
await page.goto(`${BASE}/#/papers`, { waitUntil: 'networkidle2' });
check('unauthenticated visit is redirected to the sign-in page', await waitFor(() => location.hash.startsWith('#/login')), page.url());
check('sign-in page offers account creation for the first user', /Create the administrator account|Create one/.test(await text()), (await text()).slice(0, 200));

// 2. first account becomes the administrator
await clickText('a', 'Create one');
await page.waitForSelector('input[type=email]');
const inputs = await page.$$('input.frm-input');
await inputs[0].type('E2E Admin');
await page.type('input[type=email]', EMAIL);
await page.type('input[type=password]', PW);
await clickText('button', 'Create account');
check('registering signs in and lands on the dashboard', await waitFor(() => !location.hash.startsWith('#/login') && /crisisgrid/i.test(document.body.innerText), 40000), page.url());
check('top bar shows the user and role', await waitFor(() => /e2e admin/i.test(document.body.innerText) && /admin/i.test(document.body.innerText), 15000), await text());
const token = await page.evaluate(() => localStorage.getItem('cg.token'));
check('a session token is stored', !!token && token.split('.').length === 3);

// 3. the old dashboard pages still work with the token (Node backend, mock AI)
await page.goto(`${BASE}/#/papers`, { waitUntil: 'networkidle2' });
check('Research Papers page loads through the Node API with the token', await waitFor(() => /Research Papers|Upload|No papers|papers/i.test(document.body.innerText)), (await text()).slice(0, 200));

// 4. account page: API tokens
await page.goto(`${BASE}/#/account`, { waitUntil: 'networkidle2' });
check('Account page shows the profile', await waitFor((e) => document.body.innerText.toLowerCase().includes(e), 20000, EMAIL), await mainText());
await page.type('input[placeholder^="token name"]', 'e2e token');
await clickText('button', 'Create token');
check('creating an API token shows its secret once', await waitFor(() => /cgt_[A-Za-z0-9_-]{20,}/.test(document.body.innerText)));
const apiToken = (await text()).match(/cgt_[A-Za-z0-9_-]{20,}/)?.[0];
const viaTok = await page.evaluate(async (t) => (await fetch('/cg/paper-env/me', { headers: { Authorization: `Bearer ${t}` } })).status, apiToken);
check('that API token works against the notebook API', viaTok === 200, String(viaTok));
page.once('dialog', (d) => d.accept());
await clickText('button', 'Revoke');
await sleep(800);
const afterRevoke = await page.evaluate(async (t) => (await fetch('/cg/paper-env/me', { headers: { Authorization: `Bearer ${t}` } })).status, apiToken);
check('revoking it takes effect immediately', afterRevoke === 401, String(afterRevoke));

// 5. pipeline page: upload a PDF, see it, try to start a run (no LLM configured -> a clear message)
await page.goto(`${BASE}/#/pipeline`, { waitUntil: 'networkidle2' });
const pdfPath = path.join(os.tmpdir(), `e2e-${Date.now()}.pdf`);
makePdf(pdfPath);
const fileInput = await page.$('input[type=file][accept="application/pdf"]');
await fileInput.uploadFile(pdfPath);
check('uploaded PDF appears in the pipeline paper list', await waitFor(() => document.body.innerText.includes('e2e-') && document.body.innerText.includes(' KB')), await mainText());
check('selecting it reveals run controls and the notebook button', await waitFor(() => document.body.innerText.includes('Open notebook') && document.body.innerText.includes('Start run')));
await clickText('button', 'Start run');
check('starting a run without an LLM gives a clear message instead of crashing', await waitFor(() => /no LLM is configured|CG_LLM_CHAIN/.test(document.body.innerText)), await mainText());

// 6. notebook inside the website, with the shared login
await clickText('button', 'Open notebook');
check('Open notebook navigates to the Notebook page', await waitFor(() => location.hash.startsWith('#/notebook?env=')), page.url());
await page.waitForSelector('iframe[title=notebook]');
const frameHandle = await page.$('iframe[title=notebook]');
const frame = await frameHandle.contentFrame();
const fw = (fn, t = 60000, ...a) => frame.waitForFunction(fn, { timeout: t }, ...a).then(() => true, () => false);
check('the embedded notebook UI loads and recognises the user (no sign-in overlay)', await fw(() => document.querySelector('#health')?.textContent.includes('E2E Admin')) && !(await frame.$('#signin')), await frame.evaluate(() => document.body.innerText.slice(0, 200)));
check('the paper environment created for the paper is selected', await fw(() => document.querySelector('#envSel option')?.textContent.startsWith('paper-')));
check('an unbuilt environment offers "Build environment"', await fw(() => [...document.querySelectorAll('button')].some((b) => b.textContent.includes('Build environment'))));
await frame.evaluate(() => [...document.querySelectorAll('button')].find((b) => b.textContent.includes('Build environment')).click());
check('building the environment succeeds', await fw(() => /lock [0-9a-f]{10}/.test(document.body.innerText), 180000), await frame.evaluate(() => document.querySelector('#toast')?.textContent));
await frame.evaluate(() => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === '+ Code').click());
await fw(() => document.querySelectorAll('.cell .cm-content').length === 1);
await frame.click('.cell .cm-content');
const MOD = process.platform === 'darwin' ? 'Meta' : 'Control';  // CodeMirror's Mod-a
await page.keyboard.down(MOD); await page.keyboard.press('A'); await page.keyboard.up(MOD);
await page.keyboard.type('print("answer", 6 * 7)');
await page.keyboard.down('Shift'); await page.keyboard.press('Enter'); await page.keyboard.up('Shift');
check('typing code in the embedded editor and pressing Shift+Enter runs it on the kernel', await fw(() => /answer 42/.test(document.querySelector('.cell .out')?.innerText ?? ''), 90000), await frame.evaluate(() => document.querySelector('.cell')?.innerText));
await frame.evaluate(() => [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === '+ Text').click());
check('a text (markdown) cell can be added', await fw(() => document.querySelectorAll('.cell').length === 2));

// 7. the embedded notebook is isolated per user: a second account cannot see this paper or environment
const second = await page.evaluate(async (base) => {
  const r = await (await fetch('/cg/auth/register', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ email: `other-${Date.now()}@example.com`, name: 'Other', password: 'another-strong-pass-1' }) })).json();
  const h = { Authorization: `Bearer ${r.token}` };
  return { papers: await (await fetch('/cg/papers', { headers: h })).json(), envs: await (await fetch('/cg/paper-env/paper-envs', { headers: h })).json(), nodePapers: await (await fetch('/api/papers', { headers: h })).json() };
}, BASE);
check('a second user sees none of the first user\'s papers or environments', second.papers.length === 0 && second.envs.length === 0 && second.nodePapers.every((p) => /demo/i.test(p.filename)), JSON.stringify(second));  // the Node backend's seeded demo paper has no owner and is shared by design

// 8. sign out and back in
await page.goto(`${BASE}/#/mission`, { waitUntil: 'networkidle2' });
await clickText('button', 'Sign out');
check('signing out returns to the sign-in page and clears the token', await waitFor(() => location.hash.startsWith('#/login') || location.hash === '' || location.hash === '#/') && !(await page.evaluate(() => localStorage.getItem('cg.token'))), page.url());
await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle2' });
await page.waitForSelector('input[type=email]');
await page.type('input[type=email]', EMAIL);
await page.type('input[type=password]', PW);
await clickText('button', 'Sign in');
check('signing back in works', await waitFor(() => !location.hash.startsWith('#/login') && /crisisgrid/i.test(document.body.innerText)), page.url());
await page.goto(`${BASE}/#/login`, { waitUntil: 'networkidle2' });
await page.evaluate(() => localStorage.removeItem('cg.token'));
await page.reload({ waitUntil: 'networkidle2' });
await page.type('input[type=email]', EMAIL);
await page.type('input[type=password]', 'definitely-the-wrong-password');
await clickText('button', 'Sign in');
check('a wrong password shows an error and does not sign in', await waitFor(() => /invalid email or password/i.test(document.body.innerText)), (await text()).slice(0, 200));

if (notFound.length) console.log('INFO  404 responses seen:', [...new Set(notFound)].join(', '));
check('no unexpected console/page errors', errors.length === 0, errors.slice(0, 5).join(' | '));
await page.screenshot({ path: process.env.SHOT || 'e2e/website-last.png', fullPage: true });
await browser.close();
fs.rmSync(pdfPath, { force: true });
const failed = results.filter((r) => !r[1]).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
