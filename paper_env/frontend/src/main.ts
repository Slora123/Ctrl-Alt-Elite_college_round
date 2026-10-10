import './style.css';
import { api, enc, ApiError, type Json } from './api';
import { createEditor, type Editor } from './editor';
import { renderBundle, renderStream, setWidgetManager } from './outputs';
import { LiveWidgetManager } from './widgets';

// ---------------------------------------------------------------- helpers
const $ = <T extends HTMLElement = HTMLElement>(s: string) => document.querySelector(s) as T;
type Kid = Node | string | null | undefined | false | Kid[];
function h<K extends keyof HTMLElementTagNameMap>(tag: K, props: Record<string, any> = {}, ...kids: Kid[]): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k.startsWith('data-')) e.setAttribute(k, v);
    else if (v !== null && v !== undefined) (e as any)[k] = v;
  }
  const add = (c: Kid) => { if (Array.isArray(c)) c.forEach(add); else if (c !== null && c !== undefined && c !== false) e.append(c instanceof Node ? c : document.createTextNode(String(c))); };
  kids.forEach(add);
  return e;
}
/** replaceChildren that tolerates null/false entries (conditional children). */
const put = (el: HTMLElement, ...kids: Kid[]) => { el.replaceChildren(); const add = (c: Kid) => { if (Array.isArray(c)) c.forEach(add); else if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c))); }; kids.forEach(add); };
let toastT: number;
const toast = (m: string, bad = false) => {
  const t = $('#toast'); t.textContent = m; t.style.background = bad ? '#d93025' : '#1f2328'; t.style.display = 'block';
  clearTimeout(toastT); toastT = window.setTimeout(() => (t.style.display = 'none'), 4500);
};
const guard = <A extends any[]>(fn: (...a: A) => Promise<any>) => async (...a: A) => {
  try { return await fn(...a); } catch (e: any) { toast(e.message ?? String(e), true); }
};

// ---------------------------------------------------------------- state
interface OutItem { type: 'stream' | 'display' | 'note'; name?: string; text?: string; data?: Record<string, any>; ow?: boolean }
interface CellOut { items: OutItem[]; error?: Json; meta?: string; failureKind?: string | null }
const state = {
  envs: [] as Json[], envId: localStorage.getItem('envId') || null as string | null, cells: [] as Json[], detail: null as Json,
  runs: [] as Json[], active: null as string | null, tab: 'events', events: [] as Json[], lastSeq: 0, ws: null as WebSocket | null,
  sources: {} as Record<string, string>, dirty: {} as Record<string, string>, outputs: {} as Record<string, CellOut>,
  openHist: {} as Record<string, boolean>, prompts: {} as Record<string, { prompt: string; password: boolean }>,
  session: null as string | null,
};
const ui: Record<string, { code: boolean; out: boolean }> = JSON.parse(localStorage.getItem('ui') || '{}');
const uiOf = (id: string) => (ui[id] ||= { code: false, out: false });
const saveUi = () => localStorage.setItem('ui', JSON.stringify(ui));
const curEnv = () => state.envs.find(e => e.id === state.envId);

const widgets = new LiveWidgetManager(async (msgType, content, buffers) => {
  if (!state.envId) return;
  await api('POST', `/paper-envs/${state.envId}/kernel/comm`, { msg_type: msgType, content, buffers });
});
setWidgetManager(widgets);

// ---------------------------------------------------------------- @param forms (Colab-style)
const PARAM_RE = /^(\s*)([A-Za-z_]\w*)\s*=\s*(.*?)\s*#\s*@param\s*(.*)$/;
interface Param { i: number; indent: string; name: string; raw: string; rawSpec: string; spec: Json }
function parseSpec(txt: string, val: string): Json {
  txt = txt.trim(); let spec: Json = {};
  if (txt.startsWith('[')) { try { return { type: 'select', options: JSON.parse(txt.replace(/'/g, '"')) }; } catch { /* fall through */ } }
  if (txt.startsWith('{')) { try { spec = JSON.parse(txt.replace(/([{,]\s*)([A-Za-z_]\w*)\s*:/g, '$1"$2":').replace(/'/g, '"')); } catch { /* inferred below */ } }
  if (!spec.type) spec.type = /^(True|False)$/.test(val) ? 'boolean' : /^-?\d+$/.test(val) ? 'integer' : /^-?[\d.]+(e-?\d+)?$/.test(val) ? 'number' : 'string';
  return spec;
}
function parseParams(src: string): Param[] {
  return src.split('\n').map((line, i) => {
    const m = PARAM_RE.exec(line); if (!m) return null;
    return { i, indent: m[1], name: m[2], raw: m[3], rawSpec: m[4], spec: parseSpec(m[4], m[3]) };
  }).filter(Boolean) as Param[];
}
const unquote = (v: string) => { try { return JSON.parse(v.replace(/^'(.*)'$/, '"$1"')); } catch { return v; } };
const toPy = (spec: Json, v: any) => spec.type === 'boolean' ? (v ? 'True' : 'False')
  : ['integer', 'number', 'slider'].includes(spec.type) || (spec.type === 'select' && typeof v === 'number') ? String(v) : JSON.stringify(String(v));

// ---------------------------------------------------------------- cell views (persistent DOM: editors keep focus/undo)
class CellView {
  root = h('div', { class: 'cell' });
  head = h('div', { class: 'chead' });
  form = h('div', { class: 'form' });
  host = h('div', { class: 'editor-host' });
  decl = h('div', { class: 'decl' });
  warn = h('div', { class: 'warn' });
  prompt = h('div', { class: 'prompt' });
  out = h('div', { class: 'out' });
  err = h('pre', { class: 'err' });
  info = h('div', { class: 'decl' });
  doc = h('div', { class: 'doc' });
  hist = h('div', { class: 'hist' });
  editor: Editor;
  author = h('select', { title: 'author recorded on the new CellVersion' }, ...['human', 'agent', 'generator'].map(a => h('option', { value: a }, a)));
  cellId: string;
  private outTimer = 0;

  constructor(public cell: Json) {
    this.cellId = cell.id;
    this.root.append(this.head, this.form, this.host, this.decl, this.warn, this.doc, this.out, this.prompt, this.err, this.info, this.hist);
    this.editor = createEditor(this.host, state.dirty[cell.id] ?? state.sources[cell.id] ?? '', {
      onChange: doc => { state.dirty[this.cellId] = doc; this.host.classList.add('dirty'); this.renderForm(); },
      onRun: () => runCell(this.cell), onSave: () => saveCell(this.cell, this.author.value),
      onInspect: (code, pos) => this.inspect(code, pos),
      complete: async (code, pos) => (state.envId && state.session ? api('POST', `/paper-envs/${state.envId}/kernel/complete`, { code, cursor_pos: pos }) : null),
    });
    this.doc.style.display = 'none';
  }

  async inspect(code: string, pos: number) {
    try {
      const r = await api('POST', `/paper-envs/${state.envId}/kernel/inspect`, { code, cursor_pos: pos });
      const text = r.found ? (r.data['text/plain'] ?? '') : 'no documentation found (the kernel must be running: run any cell first)';
      this.doc.style.display = 'block'; this.doc.textContent = text.replace(/\x1b\[[0-9;]*m/g, '');
      this.doc.onclick = () => (this.doc.style.display = 'none');
    } catch (e: any) { toast(e.message, true); }
  }

  renderForm() {
    const params = parseParams(this.editor.get());
    this.form.style.display = params.length ? 'flex' : 'none';
    if (!params.length) { this.form.replaceChildren(); return; }
    const set = (p: Param, v: any) => this.editor.replaceLine(p.i, `${p.indent}${p.name} = ${toPy(p.spec, v)}  #@param ${p.rawSpec}`);
    this.form.replaceChildren(...params.map(p => {
      const sp = p.spec, cur = unquote(p.raw); let input: HTMLElement;
      if (sp.type === 'boolean') input = h('input', { type: 'checkbox', checked: p.raw === 'True', onchange: (e: any) => set(p, e.target.checked) });
      else if (sp.type === 'select') input = h('select', { onchange: (e: any) => set(p, sp.options.find((o: any) => String(o) === e.target.value)) }, ...sp.options.map((o: any) => h('option', { value: o, selected: String(o) === String(cur) }, String(o))));
      else if (sp.type === 'slider') { const lab = h('span', {}, cur); input = h('span', {}, h('input', { type: 'range', min: sp.min ?? 0, max: sp.max ?? 100, step: sp.step ?? 1, value: cur, oninput: (e: any) => (lab.textContent = e.target.value), onchange: (e: any) => set(p, +e.target.value) }), ' ', lab); }
      else if (sp.type === 'integer' || sp.type === 'number') input = h('input', { type: 'number', step: sp.type === 'integer' ? 1 : 'any', value: cur, onchange: (e: any) => set(p, +e.target.value) });
      else input = h('input', { type: 'text', value: cur, onchange: (e: any) => set(p, e.target.value) });
      return h('label', {}, p.name, input);
    }));
  }

  scheduleOutputs() {
    if (this.outTimer) return;
    this.outTimer = requestAnimationFrame(() => { this.outTimer = 0; void this.renderOutputs(); });
  }

  async renderOutputs() {
    const u = uiOf(this.cellId), co = state.outputs[this.cellId];
    this.out.style.display = co && co.items.length && !u.out ? 'block' : 'none';
    this.err.style.display = co?.error && !u.out ? 'block' : 'none';
    this.info.style.display = co?.meta ? 'flex' : 'none';
    this.info.textContent = co?.meta ?? '';
    if (!co || u.out) return;
    // reuse item hosts so live streaming does not rebuild everything
    const hosts = Array.from(this.out.children) as HTMLElement[];
    for (let i = 0; i < co.items.length; i++) {
      const it = co.items[i]; let host = hosts[i];
      if (!host) { host = h('div', { class: 'item' }); this.out.append(host); }
      const sig = it.type + (it.name ?? '') + (it.text ?? '') + (it.data ? JSON.stringify(Object.keys(it.data)) + String((it.data['text/plain'] ?? '')).length : '');
      if (host.dataset.sig === sig) continue;
      host.dataset.sig = sig;
      try {
        if (it.type === 'stream') await renderStream(it.name ?? 'stdout', it.text ?? '', host);
        else if (it.type === 'display') await renderBundle(it.data ?? {}, host);
        else { host.textContent = it.text ?? ''; host.className = 'item note'; }
      } catch (e: any) { host.textContent = `[could not render output: ${e.message}]`; }
    }
    while (this.out.children.length > co.items.length) this.out.lastChild!.remove();
    const e = co.error;
    if (e) {
      const infra = co.failureKind === 'infrastructure';
      this.err.className = 'err' + (infra ? ' infra' : '');
      this.err.textContent = `${infra ? 'INFRASTRUCTURE FAILURE' : 'EXPERIMENT FAILURE'} · ${e.type}: ${e.message}` +
        (e.failing_lineno ? `\nline ${e.failing_lineno}: ${e.failing_line}` : '') + (e.traceback && !infra ? '\n\n' + e.traceback : '');
    }
  }

  update(cell: Json) {
    this.cell = cell; this.cellId = cell.id;
    const u = uiOf(cell.id);
    this.root.className = `cell s-${cell.status}`;
    put(this.head, 
      h('span', { class: 'meta' }, `[${cell.position}]`), h('span', { class: `badge b-${cell.status}` }, cell.status),
      cell.status === 'failed' && cell.last_failure_kind === 'infrastructure' ? h('span', { class: 'badge b-infra', title: 'kernel/infrastructure failure, not a cell exception' }, 'infra') : null,
      h('span', { class: 'sp' }),
      h('button', { class: 'primary', title: 'Shift+Enter', onclick: () => runCell(cell) }, '▶ Run'),
      h('button', { class: 'tog' + (u.code ? ' off' : ''), title: 'show/hide code', onclick: () => { u.code = !u.code; saveUi(); this.update(this.cell); } }, 'code'),
      h('button', { class: 'tog' + (u.out ? ' off' : ''), title: 'show/hide output', onclick: () => { u.out = !u.out; saveUi(); this.update(this.cell); void this.renderOutputs(); } }, 'output'),
      this.author,
      h('button', { title: 'Cmd/Ctrl+S: apply_edit → new CellVersion', onclick: () => saveCell(cell, this.author.value) }, 'Save'),
      h('button', { onclick: () => { state.openHist[cell.id] = !state.openHist[cell.id]; this.update(this.cell); } }, 'History'),
      h('button', { class: 'danger', onclick: () => delCell(cell) }, '✕'));
    if (!(cell.id in state.dirty)) { this.editor.set(state.sources[cell.id] ?? ''); this.host.classList.remove('dirty'); }
    this.host.style.display = u.code ? 'none' : '';
    this.decl.style.display = u.code ? 'none' : 'flex';
    this.decl.replaceChildren('inputs:', h('input', { value: cell.declared_inputs.join(','), onchange: (e: any) => saveDecl(cell, 'declared_inputs', e.target.value) }),
      'outputs:', h('input', { value: cell.declared_outputs.join(','), onchange: (e: any) => saveDecl(cell, 'declared_outputs', e.target.value) }));
    this.warn.style.display = !u.code && cell.input_warnings?.length ? 'block' : 'none';
    this.warn.textContent = '⚠ ' + (cell.input_warnings ?? []).join(' · ');
    this.renderForm();
    const pr = state.prompts[cell.id];
    this.prompt.style.display = pr ? 'flex' : 'none';
    if (pr) {
      const inp = h('input', { type: pr.password ? 'password' : 'text', placeholder: 'type your answer and press Enter' });
      const send = guard(async () => { await api('POST', `/paper-envs/${state.envId}/kernel/input`, { text: inp.value }); delete state.prompts[cell.id]; this.update(this.cell); });
      inp.addEventListener('keydown', (e: any) => { if (e.key === 'Enter') void send(); });
      this.prompt.replaceChildren(h('b', {}, pr.prompt || 'input:'), inp, h('button', { class: 'primary', onclick: () => send() }, 'Send'));
      setTimeout(() => inp.focus());
    }
    this.hist.style.display = state.openHist[cell.id] ? 'block' : 'none';
    if (state.openHist[cell.id]) void this.renderHistory();
    void this.renderOutputs();
  }

  async renderHistory() {
    const vs = (await api<Json[]>('GET', `/cells/${this.cellId}/versions`)).slice().reverse();
    this.hist.replaceChildren(...vs.map((v, i) => h('div', {},
      h('b', {}, v.author), ` · ${new Date(v.created_at).toLocaleTimeString()} · ${v.id.slice(0, 8)} `,
      i === 0 ? h('em', {}, '(current)') : h('button', { onclick: guard(async () => { await api('POST', `/cell-versions/${v.id}/revert`, { author: 'human' }); await reload(); }) }, 'revert to this'),
      h('pre', {}, v.diff || v.source))));
  }
}
const views = new Map<string, CellView>();

function mapOutputs(cr: Json): CellOut {
  const items: OutItem[] = [];
  for (const o of cr.outputs ?? []) {
    if (o.type === 'stream') items.push({ type: 'stream', name: o.name, text: o.text });
    else if (o.type === 'execute_result' || o.type === 'display_data') items.push({ type: 'display', data: o.data });
    else if (o.type === 'note') items.push({ type: 'note', text: o.text });
    else if (o.type === 'truncated') items.push({ type: 'note', text: `… output truncated in the database; full copy: ${cr.full_output_path}` });
  }
  const meta = cr.status && cr.status !== 'pending' ? `last run: ${cr.status}${cr.duration ? ' · ' + cr.duration.toFixed(2) + 's' : ''}${cr.state_hash ? ' · state ' + cr.state_hash.slice(0, 10) : ''}` : undefined;
  return { items, error: cr.error ?? undefined, failureKind: cr.failure_kind, meta };
}

// ---------------------------------------------------------------- loading
async function loadEnvs() {
  state.envs = await api('GET', '/paper-envs');
  if (!state.envs.find(e => e.id === state.envId)) state.envId = state.envs[0]?.id ?? null;
  $<HTMLSelectElement>('#envSel').replaceChildren(...state.envs.map(e => h('option', { value: e.id, selected: e.id === state.envId }, e.alias)));
  localStorage.setItem('envId', state.envId ?? '');
}

async function loadOutputs(runId: string, keepLive = true) {
  const crs = await api<Json[]>('GET', `/runs/${runId}/cells`);
  const running = new Set(state.cells.filter(c => c.status === 'running').map(c => c.id));
  for (const cr of crs) if (!(keepLive && running.has(cr.cell_id) && state.outputs[cr.cell_id])) state.outputs[cr.cell_id] = mapOutputs(cr);
}

async function loadAll(full = false) {
  if (!state.envId) { state.cells = []; paint(); return; }
  const [d, cells, runs] = await Promise.all([api('GET', `/paper-envs/${state.envId}`), api<Json[]>('GET', `/paper-envs/${state.envId}/cells`), api<Json[]>('GET', `/paper-envs/${state.envId}/runs`)]);
  state.detail = d; state.active = d.active_run_id; state.cells = cells; state.runs = runs;
  state.session = runs.length ? runs[runs.length - 1].kernel_session_id : null;  // a kernel exists once something has run
  for (const c of cells) if (full || !(c.id in state.sources)) state.sources[c.id] = (await api('GET', `/cells/${c.id}`)).source;
  const latest = runs[runs.length - 1];
  if (latest) await loadOutputs(latest.id, true);
  paint();
}
const reload = async () => { for (const k of Object.keys(state.dirty)) delete state.dirty[k]; await loadAll(true); };
let softT = 0;
const softRefresh = () => { clearTimeout(softT); softT = window.setTimeout(guard(() => loadAll(false)), 250); };

// ---------------------------------------------------------------- notebook painting
function paint() {
  const nb = $('#notebook'); const e = curEnv();
  if (!e) { nb.replaceChildren(h('div', { class: 'empty' }, 'Create or select a paper environment to begin.')); renderPanel(); return; }
  const lock = state.detail?.env_spec?.lockfile_hash, active = state.active;
  const bar = h('div', { class: 'bar' },
    h('button', { class: 'primary', disabled: !!active, onclick: () => startRun('all') }, '▶▶ Run all'),
    h('button', { disabled: !!active, onclick: () => startRun('clean'), title: 'fresh kernel, fixed seed, verification-eligible' }, '✓ Run clean'),
    h('button', { disabled: !active, onclick: () => interrupt() }, '■ Interrupt'),
    h('button', { disabled: !!active, onclick: () => restartKernel() }, '↻ Restart kernel'),
    h('button', { onclick: () => addCell() }, '+ Code'),
    h('label', { class: 'meta' }, h('input', { type: 'checkbox', id: 'stopOnFail', checked: true }), ' stop on fail'),
    h('span', { style: 'flex:1' }),
    h('span', { class: 'meta' }, lock ? `lock ${lock.slice(0, 10)} · seed ${state.detail.env_spec.seed}` : 'env not built'),
    lock ? null : h('button', { class: 'primary', onclick: () => buildEnv() }, 'Build environment'),
    active ? h('span', { class: 'badge b-running' }, 'run active') : null);
  const alive = new Set(state.cells.map(c => c.id));
  for (const [id, v] of views) if (!alive.has(id)) { v.root.remove(); views.delete(id); }
  const ordered: HTMLElement[] = [];
  for (const c of state.cells) {
    let v = views.get(c.id);
    if (!v) { v = new CellView(c); views.set(c.id, v); }
    v.update(c); ordered.push(v.root);
  }
  const empty = state.cells.length ? null : h('div', { class: 'empty' }, 'No cells yet. Click “+ Code”.');
  const want = [bar, ...ordered, ...(empty ? [empty] : [])];
  want.forEach((el, i) => { if (nb.children[i] !== el) nb.insertBefore(el, nb.children[i] ?? null); });
  while (nb.children.length > want.length) nb.lastElementChild!.remove();
  renderPanel();
}

// ---------------------------------------------------------------- actions
const addCell = guard(async () => { await api('POST', `/paper-envs/${state.envId}/cells`, { source: '# new cell\n', author: 'human' }); await loadAll(true); });
const delCell = guard(async (c: Json) => { if (confirm('Delete this cell?')) { await api('DELETE', `/cells/${c.id}`); delete state.dirty[c.id]; await loadAll(true); } });
const saveCell = guard(async (c: Json, author = 'human') => {
  const src = state.dirty[c.id]; if (src === undefined) return;
  const v = await api('PUT', `/cells/${c.id}/source`, { source: src, author });
  state.sources[c.id] = src; delete state.dirty[c.id];
  toast(`saved as new version ${v.id.slice(0, 8)} (${author}); downstream cells are stale`); await loadAll(false);
});
const saveDecl = guard(async (c: Json, key: string, val: string) => { await api('PATCH', `/cells/${c.id}/declarations`, { [key]: val.split(',').map(s => s.trim()).filter(Boolean) }); await loadAll(false); });
const saveDirty = async () => { for (const c of state.cells) if (state.dirty[c.id] !== undefined) await saveCell(c); };
const runCell = guard(async (c: Json) => { await saveDirty(); delete state.outputs[c.id]; await api('POST', '/runs', { paper_env_id: state.envId, mode: 'cell', cell_id: c.id }); state.active = 'pending'; softRefresh(); });
const startRun = guard(async (mode: string) => {
  await saveDirty();
  await api('POST', '/runs', { paper_env_id: state.envId, mode, stop_on_fail: ($('#stopOnFail') as HTMLInputElement)?.checked ?? true, checkpoint: false });
  state.active = 'pending'; softRefresh();
});
const interrupt = guard(async () => { await api('POST', `/runs/${state.active}/interrupt`); });
const restartKernel = guard(async () => { const r = await api('POST', `/paper-envs/${state.envId}/kernel/restart`); toast(r.note); await widgets.reset(); await loadAll(false); });
const buildEnv = guard(async () => {
  toast('building environment with uv…');
  const r = await api('POST', `/paper-envs/${state.envId}/env/create`);
  toast(r.status === 'success' ? 'environment ready' : `env build ${r.status}: ${r.error || ''}`, r.status !== 'success'); await loadAll(true);
});

$('#envSel').addEventListener('change', (e: any) => { state.envId = e.target.value; localStorage.setItem('envId', state.envId!); resetEnvState(); void loadAll(true); });
$('#newEnvBtn').addEventListener('click', () => { const n = $('#newEnv'); n.style.display = n.style.display === 'none' ? 'block' : 'none'; });
$('#neGo').addEventListener('click', guard(async () => {
  const body = { alias: $<HTMLInputElement>('#neAlias').value.trim(), paper_hash: $<HTMLInputElement>('#neHash').value, seed: +$<HTMLInputElement>('#neSeed').value, build_env: $<HTMLInputElement>('#neBuild').checked };
  if (!body.alias) return toast('alias required', true);
  toast('creating…' + (body.build_env ? ' building environment (first time needs network)' : ''));
  const r = await api('POST', '/paper-envs', body); state.envId = r.paper_env.id; resetEnvState(); $('#newEnv').style.display = 'none';
  if (r.env_build && r.env_build.status !== 'success') toast('env build failed: ' + (r.env_build.error || r.env_build.conflict_details), true); else toast('paper env created');
  await loadEnvs(); await loadAll(true);
}));
function resetEnvState() { state.sources = {}; state.dirty = {}; state.outputs = {}; state.prompts = {}; for (const v of views.values()) v.root.remove(); views.clear(); void widgets.reset(); }

// ---------------------------------------------------------------- side panel
const TABS = ['events', 'runs', 'files', 'datasets', 'env', 'blocked'];
function renderTabs() { $('#tabs').replaceChildren(...TABS.map(t => h('button', { class: state.tab === t ? 'on' : '', onclick: () => { state.tab = t; renderTabs(); renderPanel(); } }, t))); }
function showJson(obj: unknown) { state.tab = '__json'; $('#panel').replaceChildren(h('button', { onclick: () => { state.tab = 'runs'; renderTabs(); renderPanel(); } }, '← back'), h('pre', { class: 'json' }, JSON.stringify(obj, null, 2))); }
function summarize(e: Json) {
  const p = e.payload || {};
  if (e.type === 'cell_output') return (p.text || '').trim().slice(0, 80);
  if (e.type === 'cell_failed') return `${p.exception_type}: ${p.message}`;
  if (e.type === 'kernel_died') return `${p.kind}: ${p.message}`;
  if (e.type === 'run_finished') return `${p.status}${p.message ? ' · ' + p.message : ''}`;
  if (e.type === 'cell_edited') return `${p.change} by ${p.author || '?'}`;
  return Object.entries(p).filter(([k, v]) => typeof v !== 'object' && k !== 'diff').slice(0, 3).map(([k, v]) => `${k}=${String(v).slice(0, 24)}`).join(' ');
}
const badge = (s: string) => h('span', { class: `badge b-${s}` }, s);

async function renderPanel() {
  const p = $('#panel'), t = state.tab;
  if (t === 'events') {
    put(p, h('div', { class: 'meta' }, `live over WebSocket · ${state.events.length} shown`), ...state.events.slice(-150).reverse().map(e => h('div', { class: 'ev' + (/failed|died|blocked/.test(e.type) ? ' bad' : '') }, h('b', {}, `#${e.seq} ${e.type}`), ' ', summarize(e))));
  } else if (t === 'runs') {
    put(p, ...[...state.runs].reverse().map(r => h('div', { class: 'listitem' }, h('b', {}, r.mode), ' ', badge(r.status), r.verification_eligible ? ' ✓verification-eligible' : '', r.failure_kind ? ` (${r.failure_kind})` : '',
      h('br'), h('small', {}, `nb v${r.notebook_version} · seed ${r.seed} · lock ${(r.env_lockfile_hash || '').slice(0, 8)} · ${r.approximate_split ? 'APPROX SPLIT · ' : ''}${r.id.slice(0, 8)}`),
      r.message ? h('div', {}, h('small', {}, r.message)) : null,
      h('div', {}, h('button', { onclick: () => showJson(r) }, 'run record'), ' ', h('button', { onclick: guard(async () => { await loadOutputs(r.id, false); paint(); }) }, 'show outputs')))),
      state.runs.length ? null : h('div', { class: 'meta' }, 'no runs yet'));
  } else if (t === 'files') {
    if (!state.envId) return put(p, 'no env');
    const files = await api<Json[]>('GET', `/paper-envs/${state.envId}/files`);
    const pick = h('input', { type: 'file', multiple: true, onchange: guard(async (e: any) => {
      for (const f of e.target.files) { const r = await fetch(`/paper-envs/${state.envId}/files/${enc(f.name)}`, { method: 'PUT', body: f }); if (!r.ok) throw new ApiError((await r.json()).detail || r.statusText, r.status); }
      toast('uploaded'); void renderPanel(); }) });
    put(p, h('div', { class: 'meta' }, 'Cells run inside this folder: open("data.csv") finds uploads, and files your code writes show up here.'), h('div', { class: 'row' }, pick),
      ...files.map(f => h('div', { class: 'fileitem' }, h('a', { href: `/paper-envs/${state.envId}/files/${enc(f.path)}`, download: f.path.split('/').pop() }, f.path), h('small', {}, `${f.size} B`),
        h('button', { class: 'danger', onclick: guard(async () => { await api('DELETE', `/paper-envs/${state.envId}/files/${enc(f.path)}`); void renderPanel(); }) }, '✕'))),
      files.length ? null : h('div', { class: 'meta' }, 'empty'), h('button', { onclick: () => renderPanel() }, 'refresh'));
  } else if (t === 'datasets') {
    const ds = await api<Json[]>('GET', '/datasets');
    const path = h('input', { placeholder: '/absolute/path/to/file-or-dir' }), name = h('input', { placeholder: 'name' }), ver = h('input', { placeholder: 'v1', value: 'v1' });
    put(p, h('div', { class: 'row' }, name, ver), h('div', { class: 'row' }, path, h('button', { class: 'primary', onclick: guard(async () => { await api('POST', '/datasets/upload', { name: name.value, version: ver.value, path: path.value }); toast('uploaded to cache'); void renderPanel(); }) }, 'Upload')),
      h('div', { class: 'meta' }, 'Declare a dataset on this env so cells can call datasets.resolve("name@v1")'),
      ...ds.map(d => { const declared = curEnv()?.dataset_ids?.includes(d.dataset_id);
        return h('div', { class: 'listitem' }, h('b', {}, d.dataset_id), ' ', badge(d.status === 'verified' ? 'success' : /corrupt|upstream/.test(d.status) ? 'failed' : 'pending'), ` ${d.status}`,
          h('br'), h('small', {}, `${d.source_type} · ${d.size_bytes} B · ${d.files.length} file(s)`), h('div', {},
            h('button', { onclick: guard(async () => { await api('POST', `/datasets/${d.dataset_id}/fetch`); void renderPanel(); }) }, 'fetch'), ' ',
            h('button', { onclick: guard(async () => { const r = await api('POST', `/datasets/${d.dataset_id}/verify`); toast(r.ok ? 'verified' : 'mismatch: ' + r.mismatches.join('; '), !r.ok); void renderPanel(); }) }, 'verify'), ' ',
            h('button', { onclick: guard(async () => { const ids = new Set<string>(curEnv().dataset_ids); declared ? ids.delete(d.dataset_id) : ids.add(d.dataset_id);
              await api('PUT', `/paper-envs/${state.envId}/datasets`, { dataset_ids: [...ids] }); await loadEnvs(); void renderPanel(); }) }, declared ? 'undeclare' : 'declare on env'))); }));
  } else if (t === 'env') {
    const d = state.detail; if (!d) return put(p, 'no env');
    const name = h('input', { placeholder: 'package' }), ver = h('input', { placeholder: 'version (optional)' });
    const op = (path: string, body?: unknown) => guard(async () => { toast('working…'); const r = await api('POST', `/paper-envs/${state.envId}/env/${path}`, body); toast(`${path}: ${r.status}${r.failing_package ? ' · ' + r.failing_package : ''}`, r.status === 'failed'); if (r.conflict_details) showJson(r); await loadAll(false); });
    put(p, h('div', { class: 'listitem' }, h('b', {}, 'EnvSpec'), h('pre', { class: 'json' }, JSON.stringify(d.env_spec, null, 1))), h('div', { class: 'row' }, name, ver),
      h('div', { class: 'row' }, h('button', { onclick: () => op('install', { name: name.value, version: ver.value || null })() }, 'install'), h('button', { onclick: () => op('pin', { name: name.value, version: ver.value })() }, 'pin'),
        h('button', { onclick: () => op('rebuild')() }, 'rebuild'), h('button', { onclick: guard(async () => showJson(await api('GET', `/paper-envs/${state.envId}/env/lockfile`))) }, 'lockfile'),
        h('button', { onclick: guard(async () => showJson(await api('GET', `/paper-envs/${state.envId}/env/hardware`))) }, 'hardware')),
      h('div', { class: 'meta' }, 'Packages are installed only here, never from a cell. Install ipywidgets to use widgets.'));
  } else if (t === 'blocked') {
    const bl = await api<Json[]>('GET', '/blocked');
    put(p, h('div', { class: 'meta' }, 'Actions paused by the pre-execute hook.'), ...bl.map(b => h('div', { class: 'listitem' }, h('b', {}, b.kind), ' ', badge('blocked'), h('div', {}, h('small', {}, `${b.decision}: ${b.reason}`)), h('pre', { class: 'json' }, JSON.stringify(b.payload, null, 1)),
      ['allow', 'deny'].map(dn => h('button', { onclick: guard(async () => { await api('POST', `/blocked/${b.id}/resolve`, { decision: dn, reason: 'from UI' }); void renderPanel(); softRefresh(); }) }, dn)))), bl.length ? null : h('div', { class: 'meta' }, 'nothing pending'));
  }
}

// ---------------------------------------------------------------- live events
function pushOutput(cellId: string, p: Json) {
  const co = (state.outputs[cellId] ||= { items: [] });
  if (p.type === 'stream') {
    const last = co.items[co.items.length - 1];
    if (last && last.type === 'stream' && last.name === p.name && last.ow) { last.text = p.text; last.ow = !!p.overwrite; }
    else co.items.push({ type: 'stream', name: p.name, text: p.text, ow: !!p.overwrite });
  } else co.items.push({ type: 'display', data: p.data });
  views.get(cellId)?.scheduleOutputs();
}
function onEvent(e: Json) {
  const mine = e.paper_env_id === state.envId || e.paper_env_id == null, p = e.payload || {};
  if (e.type === 'comm_message') { if (mine) void widgets.handle(p); return; }
  if (e.seq != null) { state.lastSeq = e.seq; state.events.push(e); if (state.events.length > 400) state.events.shift(); }
  if (mine) {
    if (e.type === 'cell_started') { state.active ||= 'live'; state.outputs[p.cell_id] = { items: [] }; delete state.prompts[p.cell_id]; const c = state.cells.find(c => c.id === p.cell_id); if (c) { c.status = 'running'; views.get(c.id)?.update(c); } }
    else if (e.type === 'cell_output') pushOutput(p.cell_id, p);
    else if (e.type === 'input_requested') { state.prompts[p.cell_id] = { prompt: p.prompt, password: p.password }; const c = state.cells.find(c => c.id === p.cell_id); if (c) views.get(c.id)?.update(c); }
    else if (e.type === 'kernel_died' || (e.type === 'env_changed' && p.op === 'kernel_restart')) void widgets.reset();
    if (/cell_finished|cell_failed|kernel_died|run_finished|cell_edited|env_changed|action_/.test(e.type)) softRefresh();
  }
  if (state.tab === 'events' && e.seq != null) void renderPanel();
}
async function connectWS() {
  state.ws?.close();
  const hist = await api<Json[]>('GET', '/events/history?limit=10000');
  state.lastSeq = hist.length ? hist[hist.length - 1].seq : 0; state.events = hist.slice(-100);
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/events?after_seq=${state.lastSeq}`); state.ws = ws;
  ws.onmessage = m => onEvent(JSON.parse(m.data));
  ws.onclose = () => setTimeout(() => state.ws === ws && void connectWS(), 2000);
}

// ---------------------------------------------------------------- boot
(async () => {
  renderTabs();
  try { const hz = await api('GET', '/health'); $('#health').textContent = `backend: ${hz.backend.backend} · gpu: ${hz.backend.gpu}`; } catch { $('#health').textContent = 'API unreachable'; }
  await guard(loadEnvs)(); await guard(() => loadAll(true))(); void connectWS();
  setInterval(() => { if (state.active) softRefresh(); }, 2500);
})();
