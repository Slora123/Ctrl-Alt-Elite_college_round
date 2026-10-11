// Browser E2E for the dashboard pages against the REAL backend (no mock engine, no Node API calls).
//
//   1. python api/tests/e2e_app.py --port 8100 --state ./.e2e-state            (real API; scripted stand-in for the LLM)
//   2. CG_API=http://127.0.0.1:8100 npx vite --port 5173
//   3. node e2e/dashboard.mjs                                                   (MODE=nollm against `e2e_app.py --no-llm`)
//
// Env: BASE (default http://localhost:5173), API (default http://127.0.0.1:8100), CHROME (path to Chrome/Edge),
//      PY (the api venv's python, to build the PDF and start a worker), MODE=full|nollm
import puppeteer from 'puppeteer-core';
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BASE = process.env.BASE || 'http://localhost:5173';
const API = process.env.API || 'http://127.0.0.1:8100';
const MODE = process.env.MODE || 'full';
const WIN = process.platform === 'win32';
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const PY = process.env.PY || path.join(ROOT, 'api', '.venv', WIN ? 'Scripts/python.exe' : 'bin/python');
const CHROME = process.env.CHROME || (WIN ? 'C:/Program Files/Google/Chrome/Application/chrome.exe'
  : process.platform === 'darwin' ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' : '/usr/bin/google-chrome');
const EMAIL = `dash-${Date.now()}@example.com`, PW = 'dashboard-correct-horse-battery';
const results = [];
const check = (name, ok, detail = '') => { results.push([name, ok]); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  -> ' + String(detail).slice(0, 400)}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cg-dash-'));
const pdfPath = path.join(tmp, 'resnet-demo-paper.pdf');
execFileSync(PY, ['-c', 'import sys; from crisisgrid_api.demo import PAPER; from crisisgrid_api.demo_pdf import make_pdf; open(sys.argv[1],"wb").write(make_pdf(PAPER))', pdfPath]);
const nbPath = path.join(tmp, 'train.ipynb');
fs.writeFileSync(nbPath, JSON.stringify({ nbformat: 4, nbformat_minor: 5, metadata: {}, cells: [
  { cell_type: 'markdown', metadata: {}, source: ['# train'] },
  { cell_type: 'code', metadata: {}, outputs: [], execution_count: null, source: ['import random\n', 'random.seed(SEED)\n', 'acc = 91.0 + random.random() * 0.5\n', "report_metric('top-1', acc)\n"] },
] }));

let worker = null;
const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1400, height: 1000 });
const errors = [], nodeCalls = [];
page.on('pageerror', (e) => errors.push(String(e)));
page.on('console', (m) => { if (m.type() === 'error' && !/401|403|404|409|503|Failed to load resource/.test(m.text())) errors.push(m.text()); });
page.on('request', (r) => { try { if (new URL(r.url()).pathname.startsWith('/api/')) nodeCalls.push(r.url()); } catch { /* ignore */ } });
const waitFor = async (fn, t = 30000, ...a) => { try { await page.waitForFunction(fn, { timeout: t }, ...a); return true; } catch { return false; } };
const text = () => page.evaluate(() => document.body.innerText);
const full = () => page.evaluate(() => (document.querySelector('main')?.innerText ?? document.body.innerText).replace(/\n+/g, ' | '));
const mainText = async () => (await full()).slice(0, 700);  // for failure messages only
const lc = async () => (await full()).toLowerCase();       // CSS upper-cases tags and labels, so compare lower-cased
const mentions = async (...parts) => { const t = await lc(); return parts.every((x) => t.includes(x.toLowerCase())); };
const clickText = (sel, label) => page.evaluate((sel, label) => { const el = [...document.querySelectorAll(sel)].find((e) => e.textContent.trim().includes(label)); if (!el) return false; el.click(); return true; }, sel, label);
const has = (label, t = 30000) => waitFor((l) => (document.querySelector('main')?.innerText ?? '').toLowerCase().includes(l.toLowerCase()), t, label);
const SHOTS = process.env.SHOTS_DIR;  // optional: save a screenshot at key steps
const snap = async (name) => { if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, name + '.png'), fullPage: true }); } };
const go = async (route) => { await page.goto(`${BASE}/#/${route}`, { waitUntil: 'networkidle2' }); };

try {
  // ---- sign up (the first account is the administrator)
  await go('login');
  await clickText('a', 'Create one');
  await page.waitForSelector('input[type=email]');
  const inputs = await page.$$('input.frm-input');
  await inputs[0].type('Dash Admin');
  await page.type('input[type=email]', EMAIL);
  await page.type('input[type=password]', PW);
  await clickText('button', 'Create account');
  check('signed in', await waitFor(() => !location.hash.startsWith('#/login') && /Mission Control|Research, put to the test/i.test(document.body.innerText), 40000), await text());

  // ---- Mission Control starts empty and is real
  check('Mission Control shows live backend status', await has('Backend status') && await has('Papers uploaded'), await mainText());
  const llmOn = MODE !== 'nollm';
  check(`Mission Control reports the LLM as ${llmOn ? 'configured' : 'not configured'}`, await mentions(llmOn ? 'LLM agents: configured' : 'LLM agents: not configured'), await mainText());

  // ---- upload on the Papers page goes to the real backend
  await go('papers');
  const fileInput = await page.$('input[type=file][accept="application/pdf,.pdf"]');
  await fileInput.uploadFile(pdfPath);
  if (llmOn) {
    check('upload reports the pages and that analysis started', await has('Analysis started'), await mainText());
    check('the library row shows the model-extracted title and a live status', await waitFor(() => { const t = document.querySelector('main').innerText.toLowerCase(); return t.includes('deep residual learning') && /needs your decision|analysis complete/.test(t); }, 60000), await mainText());
    check('the row shows 1 extracted claim and at least 1 audit finding', await page.evaluate(() => {
      const cells = [...document.querySelectorAll('tbody tr')[0].querySelectorAll('td')].map((c) => c.innerText.trim());
      return cells[6] === '1' && Number(cells[7]) >= 1;  // Claims, Findings columns
    }), await mainText());
  } else {
    check('upload without an LLM says analysis needs one', await has('none is configured'), await mainText());
    await go('understand');
    check('Analyse is disabled without an LLM and explains why', await waitFor(() => { const b = [...document.querySelectorAll('button')].find((x) => /Analyse paper/.test(x.textContent)); return !!b && b.disabled; }) && (await lc()).includes('no llm is configured'), await mainText());
    check('the extracted text is still available', await (async () => { await clickText('button', 'Show text'); return has('page 1'); })(), await mainText());
    check('no unexpected errors', errors.length === 0, errors.join(' | '));
    console.log(`\n${results.filter((r) => r[1]).length}/${results.length} passed`);
    await browser.close();
    process.exit(results.every((r) => r[1]) ? 0 : 1);
  }

  // ---- Paper Understanding: real extraction with quotes, provenance, and the dropped hallucination
  await go('understand');
  check('Understanding shows the claim the model extracted', await has('ResNet-20 reaches 91.25% top-1 accuracy'), await mainText());
  check('the claim carries its verified quote from the PDF', await mentions('Our model achieves 91.25% top-1 accuracy on the test set'));
  check('hyperparameters appear with a "stated" provenance tag', /learning rate[\s\S]*0\.1[\s\S]*stated/i.test(await full()), await mainText());
  check('the invented hyperparameter (momentum) was dropped and the page says so', !/momentum\s+0\.9/i.test(await full()) && await mentions('dropped because the quote'), await mainText());
  check('the route is shown (case 2, Tier 2)', await mentions('case 2, Tier 2'), await mainText());
  check('the pending decision is answerable right here (final gate)', await mentions('Final review', 'Accept'), await mainText());
  await clickText('button', 'Show text');
  await sleep(500);
  await snap('understand');
  check('"Show text" loads the extracted manuscript from the backend', await has('learning rate of 0.1 and a batch size of 128'), await mainText());

  // ---- Artifacts, Audit
  await go('artifact');
  check('Artifacts shows the repo, the pinned commit and the dataset', await has('github.com/acme/resnet-cifar') && await mentions('demo0000', 'CIFAR-10'), await mainText());
  await go('audit');
  await sleep(500);
  await snap('audit');
  check('Audit lists rule findings with ids (e.g. R09) and who found them', await has('R09') && await mentions('rule'), await mainText());
  await clickText('button', 'blocking');
  check('the severity filter works (no blocking findings for this paper)', await has('No findings with this severity'), await mainText());

  // ---- Claim Verification: not executed until a notebook is run on a worker
  await go('claims');
  check('Claims says no experiment was run and shows "Not executed"', await has('No experiment has been run') && await mentions('Not executed'), await mainText());
  check('"Run verification" is disabled until a notebook is attached', await page.evaluate(() => { const b = [...document.querySelectorAll('button')].find((x) => /Run verification/.test(x.textContent)); return !!b && b.disabled; }));
  // a worker (outbound-only) registers itself; dev servers allow open registration
  worker = spawn(PY, ['-m', 'crisisgrid_api.worker', '--api', API, '--name', 'e2e-worker', '--poll', '0.5'], { stdio: 'ignore' });
  await (await page.$('input[type=file][accept=".ipynb,application/json"]')).uploadFile(nbPath);
  check('attaching a notebook enables the button', await waitFor(() => { const b = [...document.querySelectorAll('button')].find((x) => /Run verification/.test(x.textContent)); return !!b && !b.disabled; }));
  await clickText('button', 'Run verification');
  check('the verdict arrives from clean runs on the worker: Reproduced over 3 seeds', await waitFor(() => { const t = document.querySelector('main').innerText.toLowerCase(); return t.includes('reproduced') && !t.includes('not reproduced') && /3 seeds/.test(t); }, 120000), await mainText());
    check('a measured value in the expected range is shown', /(^|[^\d.])91\.[0-5]\d*/.test(await full()), await mainText());

  await snap('claims-verdict');
  // ---- answer the final gate from the dashboard
  check('the final gate is waiting again after the verification run', await waitFor(() => document.querySelector('main').innerText.toLowerCase().includes('final review'), 60000), await mainText());
  await clickText('button', 'Accept');
  check('accepting completes the run', await waitFor(() => /analysis complete/i.test(document.querySelector('main').innerText), 30000), await mainText());

  // ---- Reports + History
  await go('reports');
  await sleep(500);
  await snap('reports');
  check('the Evidence Report renders its sections', await has('Verdicts (clean runs only)') && await mentions('Deviations from the paper', 'Limits of this report'), await mainText());
  check('the report offers Markdown download and copy', await page.evaluate(() => [...document.querySelectorAll('button')].some((b) => b.textContent.includes('Download Markdown')) && [...document.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Copy')));
  await go('history');
  check('History lists real events (route, gates, jobs, verdict)', await has('route selected') && await mentions('verdict ready', 'gate opened'), await mainText());

  // ---- Mission Control now reflects the real results
  await go('mission');
  await sleep(500);
  await snap('mission');
  check('Mission Control counts the reproduced claim and shows the latest claim row', await waitFor(() => { const t = document.querySelector('main').innerText.toLowerCase(); return /1\/1/.test(t) && t.includes('reproduced'); }, 30000), await mainText());

  // ---- pages with no backend yet are honest, not mock
  for (const [route, title] of [['lab', 'Experiment Lab'], ['graph', 'Experiment Graph'], ['versions', 'Version Comparison']]) {
    await go(route);
    check(`${title} says it is not available yet (no fake data)`, await has('not available yet') && !/MOCK|SYNTHETIC|P-10\d/.test(await text()), await mainText());
  }
  await go('settings');
  check('Settings shows the real server status', await has('LLM agents') && await mentions('Workers'), await mainText());

  // ---- delete
  await go('papers');
  page.once('dialog', (d) => d.accept());
  await clickText('.btn-sm-danger', 'Delete');
  check('deleting removes the paper from the library', await has('No papers uploaded yet'), await mainText());
  await go('mission');
  check('Mission Control is empty again', await has('Papers uploaded') && /(^|\| )0 \| papers uploaded/i.test(await full()), await mainText());

  // ---- the whole session never touched the Node (mock) API
  check('no request went to the Node /api (mock) backend', nodeCalls.length === 0, nodeCalls.join(', '));
  check('no unexpected console/page errors', errors.length === 0, errors.slice(0, 5).join(' | '));
} catch (e) {
  check('script ran to completion', false, e.stack || e);
} finally {
  await page.screenshot({ path: process.env.SHOT || path.join(tmp, 'dashboard-last.png'), fullPage: true }).catch(() => {});
  console.log(`screenshot: ${process.env.SHOT || path.join(tmp, 'dashboard-last.png')}`);
  worker?.kill();
  await browser.close();
}
console.log(`\n${results.filter((r) => r[1]).length}/${results.length} passed`);
process.exit(results.every((r) => r[1]) ? 0 : 1);
