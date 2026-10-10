// End-to-end browser test: drives the real UI in headless Chrome against a running API (default http://127.0.0.1:8000).
// Usage: node e2e/smoke.mjs   (env: BASE, CHROME)
import puppeteer from 'puppeteer-core';

const BASE = process.env.BASE || 'http://127.0.0.1:8765';      // origin
const PREFIX = process.env.PREFIX || '';                        // '' standalone, '/cg/paper-env' behind the website proxy
const TOKEN = process.env.TOKEN || '';                          // bearer token when authentication is on
const CHROME = process.env.CHROME || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const results = [];
const check = (name, ok, detail = '') => { results.push([name, ok]); console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${ok ? '' : '  -> ' + detail}`); };
const api = async (method, path, body) => {
  const r = await fetch(BASE + PREFIX + path, { method, headers: { 'content-type': 'application/json', ...(TOKEN ? { authorization: `Bearer ${TOKEN}` } : {}) }, body: body ? JSON.stringify(body) : undefined });
  const t = await r.text(); const j = t ? JSON.parse(t) : null; if (!r.ok) throw new Error(`${method} ${path}: ${r.status} ${t}`); return j;
};

const alias = 'e2e-' + Date.now();
console.log('building a paper env with ipywidgets + tqdm (needs network the first time)...');
const created = await api('POST', '/paper-envs', { alias, paper_hash: 'e2e', seed: 1, requirements: ['ipywidgets', 'tqdm'], build_env: true });
if (created.env_build.status !== 'success') { console.error('env build failed', created.env_build); process.exit(2); }
const pid = created.paper_env.id;
const cells = {};
const add = async (key, source, extra = {}) => { cells[key] = (await api('POST', `/paper-envs/${pid}/cells`, { source, ...extra })).id; };
await add('basic', "print('hello from the kernel')\n1 + 41");
await add('html', "from IPython.display import HTML, display\ndisplay(HTML('<b id=\"boldmark\">bold</b><img src=x onerror=\"window.__pwn=1\"><script>window.__pwn=1</script>'))");
await add('svg', "from IPython.display import SVG, display\ndisplay(SVG('<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"20\" height=\"20\"><circle id=\"c1\" cx=\"10\" cy=\"10\" r=\"8\"/></svg>'))");
await add('tqdm', "import sys, time\nfrom tqdm import tqdm\nfor i in tqdm(range(5), file=sys.stdout, ascii=True):\n    time.sleep(0.15)");
await add('param', "lr = 0.1  #@param {type:\"number\"}\nlayers = 3  #@param {type:\"slider\", min:1, max:10, step:1}\nname = \"abc\"  #@param [\"abc\", \"def\"]\nprint(lr, layers, name)");
await add('widget', "import ipywidgets as w\nslider = w.IntSlider(value=3, min=0, max=10, description='n')\ndisplay(slider)");
await add('input', "who = input('your name? ')\nprint('hi', who)");

const browser = await puppeteer.launch({ executablePath: CHROME, headless: 'new', args: ['--no-sandbox'] });
const page = await browser.newPage();
await page.setViewport({ width: 1300, height: 1000 });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
page.on('response', r => { if (r.status() === 404) if (!r.url().endsWith('/ui/x')) errors.push('404: ' + r.url()); });
page.on('console', m => { if (m.type() === 'error' && !/404/.test(m.text())) errors.push(m.text()); });
const sleep = ms => new Promise(r => setTimeout(r, ms));
const waitFor = async (fn, what, t = 40000, ...args) => { try { await page.waitForFunction(fn, { timeout: t }, ...args); return true; } catch { return false; } };

if (TOKEN) await page.evaluateOnNewDocument(t => localStorage.setItem('cg.token', t), TOKEN);  // the website's login token, same origin
await page.goto(BASE + PREFIX + '/', { waitUntil: 'networkidle2' });
check('root redirects to the built app (/ui/)', page.url().includes('/ui/'), page.url());
await page.evaluate(id => { localStorage.setItem('envId', id); }, pid);
await page.reload({ waitUntil: 'networkidle2' });
check('7 cell editors rendered with CodeMirror', await waitFor(() => document.querySelectorAll('.cell .cm-editor').length === 7, ''), String(await page.evaluate(() => document.querySelectorAll('.cell').length)));

const cellIdx = { basic: 0, html: 1, svg: 2, tqdm: 3, param: 4, widget: 5, input: 6 };
const clickRun = i => page.evaluate(i => document.querySelectorAll('.cell')[i].querySelector('button.primary').click(), i);
const outText = i => page.evaluate(i => document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? '', i);

// --- basic run + live output + result
await clickRun(cellIdx.basic);
check('stream + execute_result rendered', await waitFor(i => /hello from the kernel/.test(document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? '') && /42/.test(document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? ''), '', 60000, cellIdx.basic), await outText(0));
check('cell badge turns success', await waitFor(i => document.querySelectorAll('.cell')[i].classList.contains('s-success'), '', 30000, cellIdx.basic));

// --- untrusted html is sanitized
await clickRun(cellIdx.html);
await waitFor(i => document.querySelectorAll('.cell')[i].querySelector('.out b'), '', 30000, cellIdx.html);
check('HTML output rendered (<b> kept)', await page.evaluate(() => /bold/.test(document.querySelectorAll('.cell')[1].querySelector('.out b')?.textContent ?? '')), await outText(cellIdx.html));
check('dangerous attributes/tags stripped from HTML', await page.evaluate(() => !document.querySelector('.cell .out img[onerror], .cell .out script')));
await sleep(500);
check('untrusted HTML cannot run script (sanitized)', await page.evaluate(() => window.__pwn === undefined), 'window.__pwn was set');

// --- svg
await clickRun(cellIdx.svg);
await waitFor(i => (document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? '').length > 0 || document.querySelectorAll('.cell')[i].querySelector('.out svg, .out img'), '', 30000, cellIdx.svg);
console.log('      svg output shows:', JSON.stringify(await outText(cellIdx.svg)), 'svg elements:', await page.evaluate(i => document.querySelectorAll('.cell')[i].querySelectorAll('.out svg, .out img').length, cellIdx.svg));
check('SVG output shown (rendered, or safely as text)', await page.evaluate(i => !!document.querySelectorAll('.cell')[i].querySelector('.out svg, .out img') || (document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? '').length > 0, cellIdx.svg));

// --- tqdm progress shows as one overwritten line
await clickRun(cellIdx.tqdm);
await waitFor(i => /100%/.test(document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? ''), '', 40000, cellIdx.tqdm);
const tq = await outText(cellIdx.tqdm);
check('tqdm bar reaches 100%', /100%/.test(tq), tq);
check('tqdm shows a single progress line (carriage returns overwrite)', tq.trim().split('\n').length === 1, JSON.stringify(tq));

// --- @param form controls
const formOk = await page.evaluate(i => { const c = document.querySelectorAll('.cell')[i]; return { num: !!c.querySelector('.form input[type=number]'), range: !!c.querySelector('.form input[type=range]'), sel: !!c.querySelector('.form select') }; }, cellIdx.param);
check('@param renders number, slider and dropdown controls', formOk.num && formOk.range && formOk.sel, JSON.stringify(formOk));
await page.evaluate(i => { const inp = document.querySelectorAll('.cell')[i].querySelector('.form input[type=number]'); inp.value = '0.5'; inp.dispatchEvent(new Event('change', { bubbles: true })); }, cellIdx.param);
await sleep(300);
check('editing the form rewrites the source line', await page.evaluate(i => /lr = 0\.5\s+#@param/.test(document.querySelectorAll('.cell')[i].querySelector('.cm-content').innerText), cellIdx.param));
await clickRun(cellIdx.param);
check('running uses the edited value (saved via apply_edit)', await waitFor(i => /0\.5 3 abc/.test(document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? ''), '', 40000, cellIdx.param), await outText(cellIdx.param));
const vers = await api('GET', `/cells/${cells.param}/versions`);
check('form edit created a new CellVersion', vers.length === 2 && vers[1].author === 'human', JSON.stringify(vers.map(v => v.author)));

// --- ipywidgets: render, then kernel -> frontend sync
await clickRun(cellIdx.widget);
check('ipywidgets slider rendered in the page', await waitFor(i => !!document.querySelectorAll('.cell')[i].querySelector('.widget-hslider, .jupyter-widgets'), '', 40000, cellIdx.widget), await outText(cellIdx.widget));
// change the value from Python and see the widget update (kernel -> comm_msg -> WebSocket -> widget model)
const cellId2 = (await api('POST', `/paper-envs/${pid}/cells`, { source: 'slider.value = 8' })).id;
await sleep(500);
await page.evaluate(() => [...document.querySelectorAll('.cell')].at(-1)?.querySelector('button.primary')?.click());
check('python-side change updates the rendered widget', await waitFor(i => /8/.test(document.querySelectorAll('.cell')[i].querySelector('.widget-readout')?.innerText ?? ''), '', 30000, cellIdx.widget),
  await page.evaluate(i => document.querySelectorAll('.cell')[i].querySelector('.widget-readout')?.innerText ?? 'no readout', cellIdx.widget));

// --- input()
await page.evaluate(() => location.reload()); await page.waitForNetworkIdle();
await waitFor(() => document.querySelectorAll('.cell .cm-editor').length >= 8, '', 20000);
await clickRun(cellIdx.input);
check('input() shows a prompt box', await waitFor(i => getComputedStyle(document.querySelectorAll('.cell')[i].querySelector('.prompt')).display !== 'none', '', 40000, cellIdx.input));
await page.evaluate(i => { const inp = document.querySelectorAll('.cell')[i].querySelector('.prompt input'); inp.value = 'ada'; inp.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' })); }, cellIdx.input);
check('answering the prompt completes the cell', await waitFor(i => /hi ada/.test(document.querySelectorAll('.cell')[i].querySelector('.out')?.innerText ?? ''), '', 40000, cellIdx.input), await outText(cellIdx.input));

// --- collapse
await page.evaluate(() => { [...document.querySelectorAll('.cell')][0].querySelectorAll('.chead button.tog')[0].click(); });
check('collapse hides the code editor', await page.evaluate(() => getComputedStyle([...document.querySelectorAll('.cell')][0].querySelector('.editor-host')).display === 'none'));

check('no uncaught page errors / console errors', errors.length === 0, errors.slice(0, 5).join(' | '));
await page.screenshot({ path: process.env.SHOT || 'e2e/last.png', fullPage: true });
await browser.close();
await api('DELETE', `/paper-envs/${pid}`).catch(() => {});
const failed = results.filter(r => !r[1]).length;
console.log(`\n${results.length - failed}/${results.length} checks passed`);
process.exit(failed ? 1 : 0);
