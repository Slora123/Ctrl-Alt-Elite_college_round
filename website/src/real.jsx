// Dashboard pages backed by the REAL CrisisGrid API (FastAPI, proxied under /cg): papers, understanding, artifacts, claims,
// audit, reports, history, mission control and settings. Nothing here is mock data. Pages whose backend is not built yet
// (Experiment Lab, Graph, Version Comparison) say so instead of pretending.
import {useState} from 'react'
import {Link, NavLink, useNavigate} from 'react-router-dom'
import {cg, useAuth} from './auth'
import {useCg, errText, download} from './cgload'

// ------------------------------------------------------------------------------------------------ small shared pieces
const Tag = ({c, children, title}) => <span className={'tag ' + c} title={title}>{children}</span>
const Err = ({e}) => (e ? <div className="err" style={{margin: '0 0 16px'}}>{errText(e)}</div> : null)
const Loading = ({what = 'data'}) => <div className="status-loading"><span className="spinner"/> Loading {what}…</div>
const Empty = ({t, action}) => (
  <div className="empty-box"><div className="empty-icon">📂</div><div className="empty-text">{t}</div>{action && <div style={{marginTop: 12}}>{action}</div>}</div>
)
const when = s => (s ? new Date(s).toLocaleString() : '—')
const num = x => (x == null ? '—' : String(+Number(x).toFixed(4)))
const ACTIVE = ['running', 'waiting_jobs']
/** Changes whenever the paper's latest run starts, moves to another step or changes status. Put in a page's request path so the
 *  page refetches by itself (the query parameter is ignored by the API; it only changes the URL the hook watches). */
const runKey = ctx => { const r = ctx.papers.find(x => x.id === ctx.pid)?.latest_run; return r ? `${r.run_id}.${r.status}.${r.node}` : 'none' }
export const isActive = run => !!run && ACTIVE.includes(run.status)

const RUN_TAG = {
  completed: ['ok', 'analysis complete'],
  failed: ['no', 'failed'],
  running: ['na', 'running'],
  waiting_gate: ['wa', 'needs your decision'],
  waiting_jobs: ['wa', 'waiting for a worker'],
}
const RunTag = ({run}) => {
  if (!run) return <Tag c="na">not analysed</Tag>
  const [c, label] = RUN_TAG[run.status] || ['na', run.status]
  return <Tag c={c} title={run.error || ''}>{label}</Tag>
}
const VERDICT_TAG = {reproduced: 'ok', partial: 'wa', not_reproduced: 'no', inconclusive: 'na', not_executed: 'na'}
const VERDICT_TEXT = {
  reproduced: 'Reproduced', partial: 'Partially reproduced', not_reproduced: 'Not reproduced',
  inconclusive: 'Inconclusive', not_executed: 'Not executed',
}
const SEV_TAG = {blocking: 'no', major: 'no', minor: 'wa'}
const PROV_TAG = {stated: 'ok', inferred: 'wa', human: 'na'}

/** Where a run can be answered: gates live in the Pipeline page. */
const GateLink = ({run, paper}) =>
  run && run.status === 'waiting_gate' ? (
    <Link className="btn g" to={'/pipeline?paper=' + (paper || run.paper_id)} style={{whiteSpace: 'nowrap'}}>Answer in Pipeline →</Link>
  ) : null

// A small, safe markdown renderer for the report (headings, lists, **bold**, `code`, _italic_). Builds elements, never HTML.
function inline(text, key) {
  const parts = []
  const rx = /(\*\*[^*]+\*\*|`[^`]+`|_[^_]+_)/g
  let last = 0, m, i = 0
  while ((m = rx.exec(text))) {
    if (m.index > last) parts.push(text.slice(last, m.index))
    const t = m[0]
    if (t.startsWith('**')) parts.push(<b key={key + '-' + i++}>{t.slice(2, -2)}</b>)
    else if (t.startsWith('`')) parts.push(<code key={key + '-' + i++}>{t.slice(1, -1)}</code>)
    else parts.push(<em key={key + '-' + i++}>{t.slice(1, -1)}</em>)
    last = m.index + t.length
  }
  if (last < text.length) parts.push(text.slice(last))
  return parts
}
export function Markdown({text}) {
  const out = []
  let list = null
  const flush = () => { if (list) { out.push(<ul key={'ul' + out.length} className="clean-list">{list}</ul>); list = null } }
  text.split('\n').forEach((raw, i) => {
    const h = raw.match(/^(#{1,3})\s+(.*)$/)
    const li = raw.match(/^(\s*)[-*]\s+(.*)$/)
    if (h) { flush(); const Tg = 'h' + (h[1].length + 1); out.push(<Tg key={i} style={{margin: '18px 0 8px'}}>{inline(h[2], i)}</Tg>) }
    else if (li) { (list = list || []).push(<li key={i} style={{marginLeft: li[1].length * 8}}>{inline(li[2], i)}</li>) }
    else if (!raw.trim()) flush()
    else { flush(); out.push(<p key={i} style={{margin: '6px 0', lineHeight: 1.55}}>{inline(raw, i)}</p>) }
  })
  flush()
  return <div className="md">{out}</div>
}

const WORKSPACE_TABS = [
  ['understand', 'Paper Understanding'], ['artifact', 'Artifact Verification'], ['claims', 'Claim Verification'],
  ['audit', 'Research Audit'], ['reports', 'Evidence Reports'], ['history', 'Experiment History'],
]

/** Active-paper picker + the workspace tabs (real pages only). */
function Pick({ctx}) {
  const p = ctx.papers.find(x => x.id === ctx.pid)
  if (!ctx.papers.length)
    return (
      <div className="empty-box" style={{marginBottom: 24}}>
        <div className="empty-icon">📄</div>
        <div className="empty-text">No papers uploaded yet. <Link className="lk" to="/papers">Upload a PDF paper</Link> to begin.</div>
      </div>
    )
  return (
    <div className="workspace-header">
      <div className="picker-card">
        <div className="picker-left">
          <span className="picker-label">ACTIVE MANUSCRIPT</span>
          <div className="picker-select-wrapper">
            <select className="picker-select" value={ctx.pid || ''} onChange={e => ctx.setPid(e.target.value)}>
              {ctx.papers.map(x => <option key={x.id} value={x.id}>{(x.title || x.filename || 'paper').slice(0, 80)} · {x.id.slice(-6)}</option>)}
            </select>
          </div>
        </div>
        {p && (
          <div className="picker-meta">
            <span className="chip"><i className="dot"/> {p.pages ?? '—'} page{p.pages === 1 ? '' : 's'}</span>
            <span className="chip">{Math.round(p.size / 1024)} KB</span>
            <RunTag run={p.latest_run}/>
          </div>
        )}
      </div>
      <div className="ws-tabs-bar">
        {WORKSPACE_TABS.map(([k, name]) => (
          <NavLink key={k} to={'/' + k} className={({isActive}) => 'ws-tab' + (isActive ? ' active' : '')}>{name}</NavLink>
        ))}
      </div>
    </div>
  )
}

/** Start (or restart) a run of the real pipeline: parse + audit + route (+ clean runs when a notebook is attached). */
/** Save a paper environment's notebook as an .ipynb file (goes through cg() so the login token is sent). */
export async function downloadNotebook(envId, name = 'notebook') {
  const nb = await cg(`/paper-env/paper-envs/${envId}/export.ipynb`)
  download(`${name}.ipynb`, JSON.stringify(nb, null, 1), 'application/x-ipynb+json')
}

async function startAnalysis(paperId, body = {}) {
  return cg(`/papers/${paperId}/runs`, {method: 'POST', body})
}

// ---- gates: the backend pauses at decisions only a human should make; answer them wherever you are
const GATE_INFO = {
  data: ['The dataset is unavailable', 'The paper’s data could not be found or is private, so nothing can be executed. Choose how to continue.'],
  gap: ['Review the reproducibility gaps', 'There is no usable code repository. You can approve a reconstruction (its results are labelled directional) or reject.'],
  stuck: ['The analysis is stuck', 'A step failed or kept failing. Add a hint and retry, skip the step, or abort the run.'],
  final: ['Final review', 'The analysis is finished. Accept it, ask for a re-run, or add caveats to the report.'],
  action: ['Approve a code change', 'An agent proposes editing a notebook cell to make it run. Nothing is changed until you decide.'],
  notebook: ['Review the generated experiment', 'The system wrote this notebook from the paper’s stated setup (not the authors’ code). Read it, edit it in the Notebook page if you want, then approve to run it on a worker. Results will be labelled directional.'],
}
const ACTION_LABEL = {
  upload: 'Continue (I provide the data)', substitute: 'Use substitute data (not a reproduction)', stop_tier1: 'Stop here (audit only)',
  approve_all: 'Approve', approve: 'Approve', reject: 'Reject', edit: 'Edit', retry_with_hint: 'Retry', skip_stage: 'Skip this step',
  'notebook:approve': 'Approve and run on a worker', 'notebook:reject': 'Reject (no experiment)',
  abort: 'Abort the run', accept: 'Accept', request_rerun: 'Re-run the analysis', edit_caveats: 'Add caveats to the report',
}
// answers that need something typed: [payload field, placeholder, rows]
const NEEDS = {
  retry_with_hint: ['hint', 'Optional hint for the model, e.g. “the dataset is CIFAR-10”', 2],
  edit_caveats: ['caveats', 'One caveat per line', 3],
  'gap:edit': ['note', 'Your note about the assumptions', 2],
  'action:edit': ['source', 'The replacement source for the cell', 8],
}

function GatePanel({run, onAnswered, compact = false}) {
  const {user} = useAuth()
  const writer = user?.role !== 'viewer'
  const open = run?.status === 'waiting_gate'
  const g = useCg(open ? `/gates?run_id=${run.run_id}&at=${run.node}` : null)
  const [text, setText] = useState({})
  const [err, setErr] = useState(null)
  const [busy, setBusy] = useState('')
  if (!open) return null
  if (compact) {  // other tabs only point at the decision; the full panel lives on Paper Understanding so tabs do not all look alike
    const titles = (g.data || []).map(x => (GATE_INFO[x.kind] || [x.kind])[0])
    return (
      <div className="card-panel" style={{marginBottom: 16, borderColor: '#C98B2B', display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap'}}>
        <Tag c="wa">waiting for you</Tag>
        <span style={{fontSize: 13.5, flex: 1}}>{titles.join(' · ') || 'A step needs your decision'}.</span>
        <Link className="lk" to="/understand">Review and answer in Paper Understanding →</Link>
      </div>
    )
  }
  const answer = async (gate, action) => {
    setErr(null); setBusy(gate.id + action)
    const need = NEEDS[`${gate.kind}:${action}`] || NEEDS[action]
    const typed = (text[gate.id + action] ?? '').trim()
    const payload = {}
    if (need) {
      if (need[0] === 'caveats') payload.caveats = typed.split('\n').map(x => x.trim()).filter(Boolean)
      else if (typed || need[0] === 'source') payload[need[0]] = typed
    }
    try {
      await cg(`/gates/${gate.id}/respond`, {method: 'POST', body: {action, payload}})
      setText({}); onAnswered?.()
    } catch (e) { setErr(e) }
    setBusy('')
  }
  return (
    <>
      {(g.data || []).map(gate => {
        const [title, blurb] = GATE_INFO[gate.kind] || [gate.kind, '']
        const c = gate.context || {}
        return (
          <div key={gate.id} className="card-panel" style={{marginBottom: 16, borderColor: '#C98B2B'}}>
            <div style={{display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap'}}><Tag c="wa">waiting for you</Tag><b>{title}</b></div>
            <p style={{fontSize: 13.5, margin: '8px 0', color: 'var(--t2)'}}>{blurb}</p>
            {c.reason && <div className="err" style={{margin: '8px 0'}}>{c.reason}</div>}
            {c.retry_blocked && <p style={{fontSize: 12.5, color: 'var(--t2)'}}>{c.retry_blocked}</p>}
            {c.reasons?.length > 0 && <ul className="clean-list">{c.reasons.map(x => <li key={x}>{x}</li>)}</ul>}
            {c.missing_components?.length > 0 && <p style={{fontSize: 13}}>Missing: {c.missing_components.join(', ')}</p>}
            {c.diagnosis && <p style={{fontSize: 13}}><b>Diagnosis ({c.diagnosis.category}):</b> {c.diagnosis.explanation}</p>}
            {c.diff && <pre className="txt" style={{maxHeight: 220}}>{c.diff}</pre>}
            {c.reason == null && c.risk_class && <p style={{fontSize: 12.5, color: 'var(--t2)'}}>Risk class: {c.risk_class}</p>}
            {gate.kind === 'final' && c.caveats?.length > 0 && <ul className="clean-list">{c.caveats.map(x => <li key={x}>{x}</li>)}</ul>}
            {gate.kind === 'notebook' && <>
              {c.env_id && <p style={{fontSize: 13}}><a className="btn g" href={`#/notebook?env=${c.env_id}`}>Open / edit in the Notebook page ↗</a>{' '}
                <button className="btn g" onClick={() => downloadNotebook(c.env_id, 'generated')}>Download .ipynb</button> <span style={{color: 'var(--t2)'}}>approve runs what is there at that moment</span></p>}
              {c.scaled_down && <p style={{fontSize: 13}}><Tag c="wa">scaled down</Tag> the run is smaller than the paper’s; verdicts are capped at “partial”.</p>}
              {c.assumptions?.length > 0 && <><b style={{fontSize: 13}}>Assumptions</b><ul className="clean-list">{c.assumptions.map(x => <li key={x}>{x}</li>)}</ul></>}
              {c.requirements?.length > 0 && <p style={{fontSize: 13}}>Packages: <code>{c.requirements.join(', ')}</code></p>}
              {(c.cells || []).map((cell, i) => <div key={i} style={{margin: '8px 0'}}>
                <div style={{fontSize: 12, color: 'var(--t2)'}}>cell {i + 1}{cell.purpose ? ` · ${cell.purpose}` : ''}</div>
                <pre className="txt" style={{maxHeight: 260}}>{cell.source}</pre></div>)}
            </>}
            {writer ? (
              <div style={{display: 'flex', flexDirection: 'column', gap: 8}}>
                {gate.allowed_actions.filter(a => !(gate.kind === 'notebook' && a === 'edit')).map(a => {  /* notebooks are edited in the Notebook page */
                  const need = NEEDS[`${gate.kind}:${a}`] || NEEDS[a]
                  const key = gate.id + a
                  return (
                    <div key={a} style={{display: 'flex', gap: 8, alignItems: 'flex-start', flexWrap: 'wrap'}}>
                      {need && <textarea className="frm-input" rows={need[2]} placeholder={need[1]} style={{flex: '1 1 320px', fontFamily: need[0] === 'source' ? 'monospace' : 'inherit', fontSize: 12.5}}
                        value={text[key] ?? (need[0] === 'source' ? c.proposed_source ?? '' : '')} onChange={e => setText({...text, [key]: e.target.value})}/>}
                      <button className={a === 'abort' || a === 'reject' ? 'btn-sm-danger' : 'btn g'} disabled={!!busy} onClick={() => answer(gate, a)}>
                        {busy === key ? 'Sending…' : ACTION_LABEL[`${gate.kind}:${a}`] || ACTION_LABEL[a] || a}</button>
                    </div>)
                })}
              </div>
            ) : <p style={{fontSize: 13}}>Viewers cannot answer gates.</p>}
          </div>)
      })}
      <Err e={g.err || err}/>
    </>
  )
}

/**
 * The control strip every analysis page shares: the real status of the latest run, the button that starts the real pipeline
 * (analysis, or with a notebook a verification run on a worker) and the gates that are waiting for a decision.
 */
function RunBar({ctx, notebook = false, onChange, full = false}) {
  const {user} = useAuth()
  const writer = user?.role !== 'viewer'
  const p = ctx.papers.find(x => x.id === ctx.pid)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [seeds, setSeeds] = useState('0,1,2')
  const [nb, setNb] = useState(null)
  if (!p) return null
  const run = p.latest_run
  const llm = ctx.overview?.llm_configured
  const workers = ctx.overview?.workers?.online ?? 0
  const changed = () => { ctx.reloadPapers(); ctx.reloadOverview(); onChange?.() }
  const start = async () => {
    setBusy(true); setErr(null)
    try {
      const body = {}
      if (notebook) {
        const sd = seeds.split(',').map(x => parseInt(x.trim(), 10)).filter(n => !Number.isNaN(n))
        if (sd.length) body.seeds = sd
        if (nb) { try { body.notebook = JSON.parse(await nb.text()) } catch { throw new Error('That file is not a valid .ipynb (JSON) notebook.') } }
      }
      await startAnalysis(p.id, body)
      setNb(null); changed()
    } catch (e) { setErr(e) }
    setBusy(false)
  }
  const active = isActive(run)
  const label = notebook ? (busy ? 'Starting…' : 'Run verification (clean runs)')
    : busy ? 'Starting…' : active ? 'Analysing…' : run ? 'Re-run analysis' : 'Analyse paper'
  return (
    <>
      <GatePanel run={run} onAnswered={changed} compact={!full}/>
      <div className="card-panel" style={{marginBottom: 20}}>
        <div style={{display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap'}}>
          <RunTag run={run}/>
          <span style={{fontSize: 13, color: 'var(--t2)', flex: 1, minWidth: 180}}>
            {run ? <>step <b>{run.node}</b>{run.route && <> · route case {run.route.case}, tier {run.route.tier}</>}{run.needs_ocr && ' · no extractable text'}</> : 'This paper has not been analysed yet.'}
          </span>
          {writer && notebook && <>
            <label style={{fontSize: 12}}>seeds <input className="frm-input" style={{width: 90}} value={seeds} onChange={e => setSeeds(e.target.value)}/></label>
            <label className="btn g" style={{cursor: 'pointer'}}>{nb ? nb.name : 'Attach notebook (.ipynb)'}<input type="file" accept=".ipynb,application/json" hidden onChange={e => setNb(e.target.files[0] || null)}/></label>
          </>}
          {writer && <button className="btn" disabled={busy || active || !llm || p.latest_run?.needs_ocr || (notebook && !nb)} onClick={start}
            title={!llm ? 'No LLM is configured on this server' : notebook && !nb ? 'Attach a notebook that calls report_metric(name, value)' : ''}>{label}</button>}
        </div>
        {run?.error && run.status !== 'waiting_gate' && <div className="err" style={{marginTop: 10}}>{run.error}</div>}  {/* a waiting gate already shows it */}
        {!llm && ctx.overview && <p style={{fontSize: 12.5, color: 'var(--t2)', margin: '10px 0 0'}}>No LLM is configured on this server, so the analysis cannot start (set <code>CG_LLM_CHAIN</code>).</p>}
        {notebook && llm && workers === 0 && <p style={{fontSize: 12.5, color: 'var(--t2)', margin: '10px 0 0'}}>
          No worker is online, so a verification run would wait. Start one: <code>python -m crisisgrid_api.worker --api http://127.0.0.1:8100 --name local</code></p>}
        {notebook && <p style={{fontSize: 12.5, color: 'var(--t2)', margin: '10px 0 0'}}>
          The notebook runs once per seed in a fresh process; cells publish numbers with <code>report_metric(name, value)</code>. The verifier then compares them with the paper’s claims.</p>}
        <Err e={err}/>
      </div>
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Research Papers
export function Papers({ctx}) {
  const {user} = useAuth()
  const writer = user?.role !== 'viewer'
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState('')
  const go = useNavigate()
  const llm = ctx.overview?.llm_configured

  const up = async f => {
    if (!f) return
    if (!/\.pdf$/i.test(f.name) && f.type !== 'application/pdf') { setMsg({err: 'Only PDF files are supported.'}); return }
    setMsg(null); setBusy('Uploading…')
    try {
      const r = await cg('/papers', {method: 'POST', raw: await f.arrayBuffer(), headers: {'Content-Type': 'application/pdf', 'X-Filename': f.name}})
      ctx.setPid(r.paper_id)
      let note = `Uploaded ${f.name} (${r.pages} page${r.pages === 1 ? '' : 's'}).`
      if (r.needs_ocr) note += ' This PDF has no extractable text (scanned?), so it cannot be analysed without OCR.'
      else if (llm) { setBusy('Starting analysis…'); await startAnalysis(r.paper_id); note += ' Analysis started: the backend is parsing and auditing it now.' }
      else note += ' It is stored, but analysis needs an LLM and none is configured on this server (set CG_LLM_CHAIN).'
      setMsg({ok: note})
      ctx.reloadPapers(); ctx.reloadOverview()
    } catch (e) { setMsg({err: errText(e)}) }
    setBusy('')
  }
  const del = async p => {
    if (!confirm(`Delete "${p.title || p.filename}" and everything the backend stored for it (runs, findings, report)?`)) return
    try { await cg('/papers/' + p.id, {method: 'DELETE'}); ctx.reloadPapers(); ctx.reloadOverview() } catch (e) { setMsg({err: errText(e)}) }
  }
  return (
    <>
      <div className="page-header">
        <h2>Research Papers</h2>
        <p className="sub">Upload a manuscript. The backend ingests it, extracts claims and setup with quote-checked evidence, audits it against 16 rules and routes it.</p>
      </div>

      {writer ? (
        <div className="drop-card" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); up(e.dataTransfer.files[0]) }}>
          <div className="drop-icon">📄</div>
          <div className="drop-title">Drop your research PDF paper here</div>
          <p className="drop-sub">Drag & drop a file here, or click to select from your device (up to 30 MB).</p>
          <label className="btn" style={{cursor: 'pointer', display: 'inline-flex', alignItems: 'center', gap: 8}}>
            Browse PDF File
            <input type="file" accept="application/pdf,.pdf" style={{display: 'none'}} onChange={e => { up(e.target.files[0]); e.target.value = '' }}/>
          </label>
          {busy && <div className="drop-status busy">{busy}</div>}
          {msg?.ok && <div className="drop-status success">{msg.ok}</div>}
          {msg?.err && <div className="err">{msg.err}</div>}
        </div>
      ) : <div className="card-panel" style={{marginBottom: 20}}>Your role is <b>viewer</b>: you can read papers but not upload them.</div>}

      {ctx.overview && !llm && (
        <div className="card-panel" style={{marginBottom: 20, borderColor: '#C98B2B'}}>
          <b>No LLM is configured on this server.</b> Uploads are ingested and stored, but the parser and gap auditor need a model to run. Start the
          backend with <code>--llm-chain</code> / <code>--llm-key</code> (or <code>CG_LLM_CHAIN</code>).
        </div>
      )}

      <div className="section-head">
        <span className="lab">Manuscript Library</span>
        <span className="lab">{ctx.papers.length} papers</span>
      </div>
      <Err e={ctx.papersErr}/>
      <div className="reg-card">
        {ctx.papers.length ? (
          <table>
            <thead>
              <tr className="lab">
                <th>Title</th><th>Filename</th><th>Pages</th><th>Size</th><th>Uploaded</th><th>Analysis</th><th>Claims</th><th>Findings</th>
                <th style={{textAlign: 'right'}}>Action</th>
              </tr>
            </thead>
            <tbody>
              {ctx.papers.map(p => (
                <tr key={p.id} className="rw" onClick={() => { ctx.setPid(p.id); go('/understand') }}>
                  <td><b>{p.title || '—'}</b><div className="m" style={{fontSize: 11, color: 'var(--t2)'}}>{p.id.slice(-8)}</div></td>
                  <td className="m" style={{color: 'var(--t2)', fontSize: 12}}>{p.filename || '—'}</td>
                  <td className="m">{p.pages ?? '—'}</td>
                  <td className="m">{(p.size / 1024).toFixed(0)} KB</td>
                  <td className="m" style={{fontSize: 12, color: 'var(--t2)'}}>{when(p.created_at)}</td>
                  <td><RunTag run={p.latest_run}/></td>
                  <td className="m">{p.latest_run ? p.latest_run.claims : '—'}</td>
                  <td className="m">{p.latest_run ? p.latest_run.gaps : '—'}</td>
                  <td style={{textAlign: 'right', whiteSpace: 'nowrap'}} onClick={e => e.stopPropagation()}>
                    <GateLink run={p.latest_run} paper={p.id}/>{' '}
                    {writer && <span className="btn-sm-danger" onClick={() => del(p)}>Delete</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <Empty t="No papers uploaded yet. Drag & drop a PDF manuscript above."/>}
      </div>
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Paper Understanding
function Quote({src}) {
  if (!src?.quote) return null
  return <div style={{fontSize: 11.5, color: 'var(--t2)', marginTop: 4, fontStyle: 'italic'}}>“{src.quote}”</div>
}

function PaperText({paperId}) {
  const [open, setOpen] = useState(false)
  const t = useCg(open ? `/papers/${paperId}/text` : null)
  return (
    <>
      <div className="section-head" style={{marginTop: 28}}>
        <span className="lab">Extracted Manuscript Text</span>
        <button className="btn-sm" onClick={() => setOpen(o => !o)}>{open ? 'Hide' : 'Show text'}</button>
      </div>
      {open && (t.loading && !t.data ? <Loading what="text"/> : t.err ? <Err e={t.err}/> : t.data && (
        t.data.needs_ocr
          ? <Empty t="No text could be extracted from this PDF (scanned?). OCR is not available on this server."/>
          : <>
              <div className="lab" style={{marginBottom: 6}}>{t.data.chars.toLocaleString()} characters · {t.data.pages.length} pages</div>
              <pre className="txt">{t.data.pages.map((p, i) => `── page ${i + 1} ──\n${p}`).join('\n\n')}</pre>
            </>
      ))}
    </>
  )
}

export function Understand({ctx}) {
  const u = useCg(ctx.pid ? `/papers/${ctx.pid}/understanding?at=${runKey(ctx)}` : null, {every: d => (isActive(d?.run) ? 2000 : 0)})
  const d = u.data
  const parsed = d?.parsed
  const hp = parsed ? Object.entries(parsed.hyperparameters || {}) : []
  return (
    <>
      <div className="page-header">
        <h2>Paper Understanding</h2>
        <p className="sub">What the paper states, extracted by the backend. Every “stated” value carries a quote that was verified to exist in the PDF.</p>
      </div>
      <Pick ctx={ctx}/>
      <RunBar ctx={ctx} full onChange={u.reload}/>
      <Err e={u.err}/>
      {u.loading && !d && ctx.pid && <Loading what="analysis"/>}
      {d && (
        <>
          <div className="card-panel" style={{marginBottom: 24}}>
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 12}}>
              <div>
                <h3 style={{margin: 0, fontSize: 18, fontWeight: 700}}>{parsed?.title || d.paper.filename || 'Untitled paper'}</h3>
                <p style={{margin: '4px 0 0', color: 'var(--t2)', fontSize: 13}}>
                  {d.paper.pages} page{d.paper.pages === 1 ? '' : 's'} · {d.paper.chars.toLocaleString()} characters
                </p>
              </div>
            </div>
            {d.overview ? (
              <div style={{marginTop: 14}}>
                <div className="lab" style={{marginBottom: 4}}>Summary{' '}
                  {d.overview.source === 'curated'
                    ? <Tag c="na" title="Written by the team for this demo paper; not produced by the analysis">curated</Tag>
                    : <Tag c="wa" title="Written by the model from the paper; unlike the claims below it has no verified quotes">model-written, not quote-verified</Tag>}</div>
                <p style={{fontSize: 14, lineHeight: 1.6, margin: 0}}>{d.overview.summary}</p>
                {d.overview.method && <><div className="lab" style={{margin: '12px 0 4px'}}>Method</div><p style={{fontSize: 14, lineHeight: 1.6, margin: 0}}>{d.overview.method}</p></>}
              </div>
            ) : parsed && <p style={{fontSize: 12.5, color: 'var(--t2)', margin: '12px 0 0'}}>No summary for this analysis (it ran before summaries existed). Use “Re-run analysis” to generate one.</p>}
            {d.paper.needs_ocr && <div className="err" style={{marginTop: 12}}>This PDF has no extractable text (scanned?). It cannot be analysed without OCR.</div>}
            {!d.llm_configured && !parsed && <p style={{fontSize: 13, color: 'var(--t2)', marginBottom: 0}}>The paper cannot be parsed without an LLM. The extracted text is shown below.</p>}
            {d.run?.route && <p style={{fontSize: 13, margin: '12px 0 0'}}>Route: <b>case {d.run.route.case}, Tier {d.run.route.tier}</b> — {(d.run.route.reasons || []).join('; ')}</p>}
          </div>

          {parsed ? (
            <>
              <div className="section-head"><span className="lab">Claims ({parsed.claims.length})</span><span className="lab">quote-verified</span></div>
              {parsed.claims.length ? (
                <div className="reg-card"><table>
                  <thead><tr className="lab"><th>Claim</th><th>Metric</th><th>Dataset / split</th><th>Reported</th><th>Provenance</th></tr></thead>
                  <tbody>{parsed.claims.map(c => (
                    <tr key={c.id}>
                      <td><b>{c.text}</b><Quote src={c.source}/></td><td className="m">{c.metric}</td><td className="m">{c.dataset} / {c.split}</td>
                      <td className="m">{num(c.reported_value)}{c.unit === 'percent' ? '%' : ''} <span style={{color: 'var(--t2)'}}>±{num(c.tolerance)}</span></td>
                      <td><Tag c={PROV_TAG[c.provenance] || 'na'}>{c.provenance}</Tag></td>
                    </tr>))}</tbody>
                </table></div>
              ) : <Empty t="No claims could be extracted with a verifiable quote."/>}

              <div className="section-head" style={{marginTop: 28}}><span className="lab">Experimental setup</span><span className="lab">stated vs inferred</span></div>
              {hp.length ? (
                <div className="param-grid">{hp.map(([k, v]) => (
                  <div key={k} className="param-card" title={v.source?.quote ? `“${v.source.quote}”` : 'no quote: inferred'}>
                    <span className="param-label">{k.replaceAll('_', ' ')}</span>
                    <span className="param-value">{String(v.value)} <Tag c={PROV_TAG[v.provenance] || 'na'}>{v.provenance}</Tag></span>
                    <Quote src={v.source}/>
                  </div>))}
                </div>
              ) : <Empty t="No hyperparameters were stated with a verifiable quote."/>}

              <div className="two" style={{marginTop: 24}}>
                <div className="card-panel">
                  <span className="lab" style={{display: 'block', marginBottom: 8}}>Datasets</span>
                  {parsed.datasets.length ? parsed.datasets.map((x, i) => (
                    <div key={i} style={{marginBottom: 8, fontSize: 13}}>
                      <b>{x.name}</b> <Tag c={x.access === 'public' ? 'ok' : x.access === 'private' ? 'no' : 'na'}>{x.access}</Tag>
                      <div style={{color: 'var(--t2)'}}>split: {x.split || 'not defined'} · {x.split_provenance.replace('_', ' ')}</div>
                    </div>)) : <span style={{color: 'var(--t2)'}}>none found</span>}
                </div>
                <div className="card-panel">
                  <span className="lab" style={{display: 'block', marginBottom: 8}}>Metrics, seeds, hardware</span>
                  <div style={{fontSize: 13, lineHeight: 1.7}}>
                    <div>Metrics: {parsed.metrics.length ? parsed.metrics.map(m => m.name + (m.definition ? ` (${m.definition})` : '')).join(', ') : <i>none defined</i>}</div>
                    <div>Seeds: {parsed.seeds ? String(parsed.seeds.value) : <i>not stated</i>}</div>
                    <div>Hardware: {parsed.hardware ? String(parsed.hardware.value) : <i>not stated</i>}</div>
                    <div>Variance reported: {parsed.reports_variance == null ? <i>unknown</i> : parsed.reports_variance ? 'yes' : 'no'}</div>
                  </div>
                </div>
              </div>

              {parsed.contradictions?.length > 0 && (
                <div className="card-panel" style={{marginTop: 20, borderColor: '#C98B2B'}}>
                  <span className="lab" style={{display: 'block', marginBottom: 8}}>Contradictions inside the paper (not resolved silently)</span>
                  {parsed.contradictions.map((c, i) => (
                    <div key={i} style={{fontSize: 13, marginBottom: 8}}><b>{c.field}</b>: {String(c.a.value)} <Quote src={c.a.source}/> vs {String(c.b.value)} <Quote src={c.b.source}/></div>))}
                </div>
              )}
              {parsed.dropped?.length > 0 && (
                <p style={{fontSize: 12.5, color: 'var(--t2)', marginTop: 16}}>
                  {parsed.dropped.length} extracted item(s) were dropped because the quote the model gave could not be found in the paper: {parsed.dropped.join('; ')}.
                </p>
              )}
            </>
          ) : d.run && d.run.status !== 'failed' ? (
            isActive(d.run) ? <Loading what="the model's analysis"/> : <Empty t="The analysis has not produced a parsed paper yet."/>
          ) : null}

          <PaperText paperId={ctx.pid}/>
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Artifact Verification
export function Artifacts({ctx}) {
  const a = useCg(ctx.pid ? `/papers/${ctx.pid}/artifacts?at=${runKey(ctx)}` : null, {every: d => (isActive(d?.run) ? 2000 : 0)})
  const inv = a.data?.inventory
  return (
    <>
      <div className="page-header">
        <h2>Artifact Verification</h2>
        <p className="sub">Code, datasets and weights the paper points to. The repository commit is pinned at the moment of analysis.</p>
      </div>
      <Pick ctx={ctx}/>
      <RunBar ctx={ctx} onChange={a.reload}/>
      <Err e={a.err}/>
      {a.loading && !a.data && ctx.pid && <Loading what="artifacts"/>}
      {a.data && !inv && <Empty t={a.data.run ? 'The analysis has not found artifacts yet.' : 'This paper has not been analysed yet. Analyse it from Paper Understanding.'}/>}
      {inv && (
        <>
          <div className="card-panel" style={{marginBottom: 20}}>
            <span className="lab" style={{display: 'block', marginBottom: 8}}>Code repository</span>
            {inv.repo ? (
              <div style={{fontSize: 13.5, lineHeight: 1.8}}>
                <a className="lk" href={inv.repo.url} target="_blank" rel="noreferrer"><b>{inv.repo.url}</b></a><br/>
                commit <code>{inv.repo.commit ? inv.repo.commit.slice(0, 12) : 'not pinned'}</code> · branch {inv.repo.default_branch || '—'}
                {' '}· last commit {when(inv.repo.default_branch_last_commit)}<br/>
                dependencies pinned: <Tag c={inv.repo.dependencies_pinned ? 'ok' : inv.repo.dependencies_pinned === false ? 'no' : 'na'}>
                  {inv.repo.dependencies_pinned == null ? 'unknown' : inv.repo.dependencies_pinned ? 'yes' : 'no'}</Tag>
                {inv.branch_postdates_paper && <> <Tag c="wa">branch changed after the paper</Tag></>}
                {inv.snapshot_hash && <><br/><span style={{fontSize: 11.5, color: 'var(--t2)'}}>snapshot {inv.snapshot_hash.slice(0, 16)}…</span></>}
              </div>
            ) : <span style={{color: 'var(--t2)'}}>No repository was found in the paper.{inv.repo_candidates?.length ? ` Tried: ${inv.repo_candidates.join(', ')}` : ''}</span>}
          </div>

          <div className="two">
            <div className="card-panel">
              <span className="lab" style={{display: 'block', marginBottom: 8}}>Datasets ({inv.datasets.length})</span>
              {inv.datasets.length ? inv.datasets.map((d, i) => (
                <div key={i} style={{marginBottom: 8, fontSize: 13}}>
                  <b>{d.name}</b> <Tag c={d.access === 'public' ? 'ok' : d.access === 'private' ? 'no' : d.access === 'gated' ? 'wa' : 'na'}>{d.access}</Tag>
                  {d.url && <div><a className="lk" href={d.url} target="_blank" rel="noreferrer" style={{fontSize: 12}}>{d.url}</a></div>}
                </div>)) : <span style={{color: 'var(--t2)'}}>none found</span>}
            </div>
            <div className="card-panel">
              <span className="lab" style={{display: 'block', marginBottom: 8}}>Pretrained weights ({inv.weights_urls.length})</span>
              {inv.weights_urls.length ? inv.weights_urls.map(w => <div key={w} style={{fontSize: 12.5, wordBreak: 'break-all'}}><a className="lk" href={w} target="_blank" rel="noreferrer">{w}</a></div>)
                : <span style={{color: 'var(--t2)'}}>none found</span>}
            </div>
          </div>

          {inv.missing_components.length > 0 && (
            <div className="card-panel" style={{marginTop: 20, borderColor: '#C98B2B'}}>
              <span className="lab" style={{display: 'block', marginBottom: 8}}>Missing</span>
              <div style={{display: 'flex', gap: 6, flexWrap: 'wrap'}}>{inv.missing_components.map(m => <Tag key={m} c="no">{m.replaceAll('_', ' ')}</Tag>)}</div>
            </div>
          )}
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Claim Verification
export function Claims({ctx}) {
  const c = useCg(ctx.pid ? `/papers/${ctx.pid}/claims?at=${runKey(ctx)}` : null, {every: () => (isActive(ctx.papers.find(p => p.id === ctx.pid)?.latest_run) ? 2500 : 0)})
  const rows = c.data || []
  const none = c.err?.status === 404
  const executed = rows.some(r => r.verdict.status !== 'not_executed')
  return (
    <>
      <div className="page-header">
        <h2>Claim Verification</h2>
        <p className="sub">Verdicts come only from the Claim verifier, and only from clean runs (fresh process, fixed seed, top to bottom). A model can never declare a claim reproduced.</p>
      </div>
      <Pick ctx={ctx}/>
      <RunBar ctx={ctx} notebook onChange={c.reload}/>
      {c.err && !none && <Err e={c.err}/>}
      {none && <Empty t="This paper has not been analysed yet. Use “Run verification” above only after the analysis has produced claims."/>}
      {c.data && !rows.length && <Empty t="No claims were extracted from this paper."/>}
      {rows.length > 0 && (
        <>
          {!executed && (
            <div className="card-panel" style={{marginBottom: 20}}>
              <b>No experiment has been run for this paper</b>, so every claim is “not executed”. To get verdicts, start a run with a notebook in{' '}
              <Link className="lk" to={'/pipeline?paper=' + ctx.pid}>Pipeline</Link> (it executes clean runs on a worker and the verifier compares the numbers).
            </div>
          )}
          <div className="reg-card"><table>
            <thead><tr className="lab"><th>Claim</th><th>Metric</th><th>Reported</th><th>Measured</th><th>Δ</th><th>Verdict</th></tr></thead>
            <tbody>{rows.map(({claim: cl, verdict: v}) => (
              <tr key={cl.id}>
                <td><b>{cl.text}</b><div style={{fontSize: 11.5, color: 'var(--t2)'}}>{cl.dataset} / {cl.split} · <Tag c={PROV_TAG[cl.provenance] || 'na'}>{cl.provenance}</Tag></div><Quote src={cl.source}/></td>
                <td className="m">{cl.metric}</td>
                <td className="m">{num(cl.reported_value)}{cl.unit === 'percent' ? '%' : ''}<div style={{fontSize: 11, color: 'var(--t2)'}}>tolerance ±{num(cl.tolerance)}</div></td>
                <td className="m">{v.measured_mean != null ? <>{num(v.measured_mean)}{v.measured_std != null && <span style={{color: 'var(--t2)'}}> ± {num(v.measured_std)}</span>}<div style={{fontSize: 11, color: 'var(--t2)'}}>{v.n_seeds} seed{v.n_seeds === 1 ? '' : 's'}</div></> : '—'}</td>
                <td className="m">{v.delta != null ? (v.delta > 0 ? '+' : '') + num(v.delta) : '—'}</td>
                <td><Tag c={VERDICT_TAG[v.status] || 'na'}>{VERDICT_TEXT[v.status] || v.status}</Tag>
                  {(v.caveats || []).map((x, i) => <div key={i} style={{fontSize: 11.5, color: 'var(--t2)', marginTop: 3}}>{x}</div>)}</td>
              </tr>))}</tbody>
          </table></div>
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Research Audit
export function Audit({ctx}) {
  const a = useCg(ctx.pid ? `/papers/${ctx.pid}/audit?at=${runKey(ctx)}` : null, {every: () => (isActive(ctx.papers.find(p => p.id === ctx.pid)?.latest_run) ? 2500 : 0)})
  const [sev, setSev] = useState('all')
  const gaps = a.data || []
  const none = a.err?.status === 404
  const shown = gaps.filter(g => sev === 'all' || g.severity === sev)
  const count = s => gaps.filter(g => g.severity === s).length
  return (
    <>
      <div className="page-header">
        <h2>Research Audit</h2>
        <p className="sub">Reproducibility gaps found by 16 deterministic rules plus a model reviewer. Only rules can mark a gap as blocking.</p>
      </div>
      <Pick ctx={ctx}/>
      <RunBar ctx={ctx} onChange={a.reload}/>
      {a.err && !none && <Err e={a.err}/>}
      {none && <Empty t="This paper has not been analysed yet. Use “Analyse paper” above."/>}
      {a.data && (
        <>
          <div style={{display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 16, alignItems: 'center'}}>
            {['all', 'blocking', 'major', 'minor'].map(s => (
              <button key={s} className={'btn-sm' + (sev === s ? ' on' : '')} style={sev === s ? {outline: '2px solid var(--dv)'} : {}} onClick={() => setSev(s)}>
                {s === 'all' ? `All (${gaps.length})` : `${s} (${count(s)})`}</button>))}
          </div>
          {shown.length ? (
            <div className="reg-card"><table>
              <thead><tr className="lab"><th>Rule</th><th>Severity</th><th>Finding</th><th>Suggested next step</th><th>Found by</th></tr></thead>
              <tbody>{shown.map(g => {
                const [rule, ...rest] = g.field.split(':')
                const isRule = /^R\d+$/.test(rule)
                return (
                  <tr key={g.id}>
                    <td className="m" style={{fontWeight: 700, color: 'var(--dv)', whiteSpace: 'nowrap'}}>{isRule ? rule : 'AI'}<div style={{fontSize: 11, fontWeight: 400, color: 'var(--t2)'}}>{(isRule ? rest.join(':') : g.field).replaceAll('_', ' ')}</div></td>
                    <td><Tag c={SEV_TAG[g.severity] || 'na'}>{g.severity}</Tag></td>
                    <td style={{fontSize: 13}}>{g.description}</td>
                    <td style={{fontSize: 12.5, color: 'var(--t2)'}}>{g.suggested_resolution || '—'}</td>
                    <td><Tag c="na">{g.detected_by}</Tag></td>
                  </tr>)
              })}</tbody>
            </table></div>
          ) : <Empty t={gaps.length ? 'No findings with this severity.' : 'No reproducibility gaps were found.'}/>}
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Evidence Reports
export function Reports({ctx}) {
  const p = ctx.papers.find(x => x.id === ctx.pid)
  const r = useCg(ctx.pid ? `/reports/${ctx.pid}?format=md&at=${runKey(ctx)}` : null, {text: true, every: () => (isActive(p?.latest_run) ? 2500 : 0)})
  const none = r.err?.status === 404
  const [copied, setCopied] = useState(false)
  return (
    <>
      <div className="page-header">
        <h2>Evidence Reports</h2>
        <p className="sub">The written result of the latest analysis: claims, verdicts with caveats, route, gaps, artifacts, deviations and limits.</p>
      </div>
      <Pick ctx={ctx}/>
      <RunBar ctx={ctx} onChange={r.reload}/>
      {r.err && !none && <Err e={r.err}/>}
      {none && <Empty t="There is no report yet. A report is written when an analysis reaches its final step."/>}
      {r.loading && !r.data && ctx.pid && <Loading what="report"/>}
      {r.data && (
        <>
          <div style={{display: 'flex', gap: 8, marginBottom: 14}}>
            <button className="btn" onClick={() => download(`crisisgrid-report-${(p?.filename || ctx.pid).replace(/\.pdf$/i, '').replace(/[^\w.-]+/g, '_')}.md`, r.data)}>Download Markdown</button>
            <button className="btn g" onClick={async () => { await navigator.clipboard?.writeText(r.data); setCopied(true); setTimeout(() => setCopied(false), 1500) }}>{copied ? 'Copied' : 'Copy'}</button>
          </div>
          <div className="card-panel"><Markdown text={r.data}/></div>
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Experiment History
function describe(e) {
  const p = e.payload || {}
  switch (e.kind) {
    case 'stage_started': case 'stage_done': return `step ${p.node}`
    case 'gate_opened': return `${p.gate_kind} gate opened`
    case 'gate_resolved': return `${p.gate_kind} gate answered: ${p.action}`
    case 'route_selected': return `case ${p.case}, tier ${p.tier}`
    case 'job_queued': return `job queued (seed ${p.seed})`
    case 'job_log': return p.line
    case 'job_finished': return `job ${p.status}`
    case 'verdict_ready': return `${p.status}: reported ${p.reported}, measured ${p.measured_mean ?? 'n/a'}`
    case 'edit_applied': return `cell ${p.cell_index} edited by ${p.author} (${p.risk_class})`
    case 'diagnosis': return `${p.category}: ${p.explanation}`
    case 'run_failed': return p.error
    default: { const s = JSON.stringify(Object.fromEntries(Object.entries(p).filter(([k]) => !['diff', 'context', 'ts', 'module'].includes(k)))); return s === '{}' ? '' : s.slice(0, 160) }
  }
}
const KIND_TAG = {run_failed: 'no', gate_opened: 'wa', verdict_ready: 'ok', job_finished: 'na', run_finished: 'ok'}

export function History({ctx}) {
  const h = useCg(ctx.pid ? `/history?paper_id=${ctx.pid}&at=${runKey(ctx)}` : null, {every: () => (isActive(ctx.papers.find(p => p.id === ctx.pid)?.latest_run) ? 3000 : 0)})
  const [logs, setLogs] = useState(false)
  const ev = (h.data?.events || []).filter(e => logs || e.kind !== 'job_log')
  return (
    <>
      <div className="page-header">
        <h2>Experiment History</h2>
        <p className="sub">Everything the backend did for this paper, in order: steps, gates and your answers, jobs, verdicts and edits.</p>
      </div>
      <Pick ctx={ctx}/>
      <RunBar ctx={ctx} onChange={h.reload}/>
      <Err e={h.err}/>
      {h.loading && !h.data && ctx.pid && <Loading what="history"/>}
      {h.data && (
        <>
          <div style={{display: 'flex', gap: 16, alignItems: 'center', marginBottom: 12}}>
            <label style={{fontSize: 13}}><input type="checkbox" checked={logs} onChange={e => setLogs(e.target.checked)}/> show worker log lines</label>
            <button className="btn-sm" onClick={h.reload}>Refresh</button>
          </div>
          {ev.length ? (
            <div className="reg-card"><table>
              <thead><tr className="lab"><th>Time</th><th>Run</th><th>Event</th><th>Detail</th></tr></thead>
              <tbody>{ev.map(e => (
                <tr key={e.id}>
                  <td className="m" style={{fontSize: 12, color: 'var(--t2)', whiteSpace: 'nowrap'}}>{when(e.ts)}</td>
                  <td className="m" style={{fontSize: 11.5}}>{e.run_id.slice(-6)}</td>
                  <td><Tag c={KIND_TAG[e.kind] || 'na'}>{e.kind.replaceAll('_', ' ')}</Tag></td>
                  <td style={{fontSize: 12.5, wordBreak: 'break-word'}}>{describe(e)}</td>
                </tr>))}</tbody>
            </table></div>
          ) : <Empty t="Nothing has happened for this paper yet."/>}
          {h.data.truncated && <p style={{fontSize: 12, color: 'var(--t2)'}}>Showing the latest {h.data.events.length} events.</p>}
        </>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Mission Control
export function Mission({ctx}) {
  const o = ctx.overview
  const go = useNavigate()
  if (ctx.overviewErr && !o) return <Err e={ctx.overviewErr}/>
  if (!o) return <Loading what="overview"/>
  const cl = o.claims
  const stages = [
    ['Paper', 'papers', o.papers, `${o.papers} uploaded`],
    ['Parsed', 'understand', o.stages.parsed, `${o.stages.parsed} analysed by the model`],
    ['Audited', 'audit', o.stages.audited, `${o.gaps.blocking + o.gaps.major + o.gaps.minor} findings`],
    ['Executed', 'claims', o.stages.executed, `${o.stages.executed} with clean runs`],
    ['Verdicts', 'claims', cl.total - cl.not_executed, `${cl.reproduced} reproduced`],
  ]
  return (
    <>
      <div className="topA">
        <div>
          <h1>Research, put to the test.</h1>
          <p className="sub" style={{fontSize: 15, marginBottom: 18}}>Reconstruct experiments. Verify claims. Understand why results differ.</p>
          <div style={{display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginBottom: 24}}>
            <button className="btn" onClick={() => go('/papers')}>Analyze a paper →</button>
            <button className="btn g" onClick={() => go('/pipeline')}>Open Pipeline</button>
          </div>
          <div className="strip">
            {[[o.papers, 'Papers uploaded'], [`${cl.reproduced}/${cl.total - cl.not_executed}`, 'Claims reproduced / tested'],
              [o.open_gates.length, 'Waiting for you'], [o.gaps.blocking + o.gaps.major, 'Major findings']].map(([v, l]) => (
              <div key={l}><b>{v}</b><span className="lab">{l}</span></div>))}
          </div>
        </div>
        <section className="layer">
          <div className="lab">Backend status</div>
          <div style={{fontSize: 13.5, lineHeight: 2}}>
            LLM agents: <Tag c={o.llm_configured ? 'ok' : 'no'}>{o.llm_configured ? 'configured' : 'not configured'}</Tag><br/>
            Workers: <Tag c={o.workers.online ? 'ok' : 'wa'}>{o.workers.online} online</Tag> <span style={{color: 'var(--t2)'}}>of {o.workers.total}</span><br/>
            Runs: {o.runs.running + o.runs.waiting_jobs} active · {o.runs.completed} done · {o.runs.failed} failed
          </div>
        </section>
      </div>

      <div className="section-head"><span className="lab">Pipeline</span></div>
      <div style={{display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 28}}>
        {stages.map(([name, to, n, sub]) => (
          <Link key={name} to={'/' + to} className="card-panel" style={{flex: '1 1 150px', textDecoration: 'none', color: 'inherit', opacity: n ? 1 : 0.55}}>
            <div className="lab">{name}</div><div style={{fontSize: 12.5, color: 'var(--t2)', marginTop: 4}}>{sub}</div>
          </Link>))}
      </div>

      {o.open_gates.length > 0 && (
        <div className="card-panel" style={{marginBottom: 24, borderColor: '#C98B2B'}}>
          <div className="lab">Waiting for your decision</div>
          {o.open_gates.map(g => (
            <div key={g.id} style={{display: 'flex', alignItems: 'center', gap: 12, padding: '8px 0', borderTop: '1px solid var(--bd)'}}>
              <Tag c="wa">{g.kind} gate</Tag><span style={{flex: 1, fontSize: 13.5}}>{g.paper}</span>
              <Link className="btn g" to={'/pipeline?paper=' + g.paper_id}>Answer →</Link>
            </div>))}
        </div>
      )}

      <div className="section-head"><span className="lab">Latest claims</span><Link className="lk" to="/claims">All claims →</Link></div>
      {o.recent_claims.length ? (
        <div className="reg-card"><table>
          <thead><tr className="lab"><th>Paper</th><th>Claim</th><th>Reported</th><th>Measured</th><th>Verdict</th></tr></thead>
          <tbody>{o.recent_claims.map((c, i) => (
            <tr key={i} className="rw" onClick={() => { ctx.setPid(c.paper_id); go('/claims') }}>
              <td style={{fontSize: 12.5}}>{c.paper}</td><td style={{fontSize: 13}}>{c.text}</td>
              <td className="m">{num(c.reported)}</td><td className="m">{num(c.measured_mean)}</td>
              <td><Tag c={VERDICT_TAG[c.status] || 'na'}>{VERDICT_TEXT[c.status] || c.status}</Tag></td>
            </tr>))}</tbody>
        </table></div>
      ) : <Empty t="No claims yet. Upload a paper to extract them." action={<button className="btn" onClick={() => go('/papers')}>Upload a paper</button>}/>}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ Settings
export function Settings({ctx}) {
  const o = ctx.overview
  const {user, cfg} = useAuth()
  return (
    <>
      <div className="page-header"><h2>System Settings</h2><p className="sub">What this server is configured to do. These are server-side settings; they are not changed from the browser.</p></div>
      {!o ? <Loading what="status"/> : (
        <div className="card-panel" style={{maxWidth: 680}}>
          <table><tbody>
            <tr><td>LLM agents</td><td><Tag c={o.llm_configured ? 'ok' : 'no'}>{o.llm_configured ? 'configured' : 'not configured'}</Tag>
              {!o.llm_configured && <div style={{fontSize: 12, color: 'var(--t2)'}}>start the backend with <code>--llm-chain</code> and <code>--llm-key</code></div>}</td></tr>
            <tr><td>Workers</td><td>{o.workers.online} online of {o.workers.total} registered</td></tr>
            <tr><td>Authentication</td><td>{cfg?.auth_enabled ? 'on' : 'off (single-user local mode)'}</td></tr>
            <tr><td>Signed in as</td><td>{user?.name} · {user?.role}</td></tr>
            <tr><td>Experiment isolation</td><td>Workers run notebooks as restricted subprocesses or managed kernels. <b>This is not a sandbox</b>: do not run untrusted papers on a shared machine.</td></tr>
          </tbody></table>
          <p style={{fontSize: 13, marginBottom: 0}}>Manage your password and API tokens in <Link className="lk" to="/account">Account</Link>.</p>
        </div>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------------------------ experiment graphs
const COLS = ['dataset', 'preprocess', 'model', 'train', 'eval', 'claim']
const TYPE_COLOR = {dataset: '#3b82f6', preprocess: '#8b5cf6', model: '#ec4899', train: '#f59e0b', eval: '#10b981', claim: '#64748b'}
const short = (s, n = 22) => (s && s.length > n ? s.slice(0, n - 1) + '…' : s || '')
const jsonText = v => JSON.stringify(v ?? {}, null, 2)

function useGraph(ctx, version) {
  const run = ctx.papers.find(p => p.id === ctx.pid)?.latest_run
  return useCg(ctx.pid ? `/papers/${ctx.pid}/graph?${version ? 'version=' + version + '&' : ''}at=${runKey(ctx)}` : null,
    {every: d => ((d?.current?.runs || []).some(r => ACTIVE.includes(r.status) || r.status === 'waiting_gate') || isActive(run) ? 3000 : 0)})
}

function NoGraph({g}) {
  if (g.err) return <Err e={g.err}/>
  if (g.loading && !g.data) return <Loading what="graph"/>
  if (g.data && !g.data.current)
    return <Empty t={<>No graph yet. It is built from the paper's extracted setup: start an analysis in <Link className="lk" to="/understand">Paper Understanding</Link> and come back once the paper is understood.</>}/>
  return null
}

function VersionPick({versions, value, onChange, label = 'Version'}) {
  return (
    <label style={{display: 'inline-flex', gap: 8, alignItems: 'center', fontSize: 13}}>
      <span className="lab">{label}</span>
      <select className="picker-select" style={{minWidth: 220}} value={value || ''} onChange={e => onChange(+e.target.value)}>
        {versions.map(v => <option key={v.version} value={v.version}>v{v.version} · {v.created_by}{v.change_note ? ' · ' + short(v.change_note, 40) : ''}</option>)}
      </select>
    </label>
  )
}

/** Layered SVG view: one column per node type, edges from inputs. */
function GraphView({graph, cells, selected, onSelect, highlight = {}}) {
  const W = 170, H = 46, GX = 60, GY = 22
  const cols = COLS.map(t => graph.nodes.filter(n => n.type === t))
  const pos = {}
  cols.forEach((ns, ci) => ns.forEach((n, ri) => { pos[n.id] = {x: 20 + ci * (W + GX), y: 20 + ri * (H + GY)} }))
  const width = 40 + COLS.length * W + (COLS.length - 1) * GX
  const height = 40 + Math.max(1, ...cols.map(c => c.length)) * (H + GY)
  const bound = new Set((cells || []).map(c => c.node_id).filter(Boolean))
  return (
    <div style={{overflowX: 'auto', border: '1px solid var(--line, #e5e7eb)', borderRadius: 10, background: 'var(--bg2, transparent)'}}>
      <svg width={width} height={height} style={{display: 'block'}}>
        {graph.nodes.flatMap(n => n.inputs.filter(i => pos[i]).map(i => {
          const a = pos[i], b = pos[n.id]
          return <path key={i + '>' + n.id} d={`M${a.x + W},${a.y + H / 2} C${a.x + W + GX / 2},${a.y + H / 2} ${b.x - GX / 2},${b.y + H / 2} ${b.x},${b.y + H / 2}`}
            fill="none" stroke="#94a3b8" strokeWidth="1.4"/>
        }))}
        {graph.nodes.map(n => {
          const p = pos[n.id], on = selected === n.id, hl = highlight[n.id]
          const label = n.type === 'claim' ? `${n.spec.metric} = ${n.spec.reported_value}` : n.type === 'dataset' ? n.spec.name : n.id
          return (
            <g key={n.id} transform={`translate(${p.x},${p.y})`} onClick={() => onSelect && onSelect(n.id)} style={{cursor: 'pointer'}}>
              <rect width={W} height={H} rx="8" fill={hl ? (hl === 'modified' ? '#fef3c7' : hl === 'added' ? '#dcfce7' : '#fee2e2') : 'var(--card, #fff)'}
                stroke={on ? '#111827' : TYPE_COLOR[n.type]} strokeWidth={on ? 2.5 : 1.5} strokeDasharray={n.provenance === 'inferred' ? '5 3' : undefined}/>
              <rect width="5" height={H} rx="2" fill={TYPE_COLOR[n.type]}/>
              <text x="13" y="18" fontSize="10.5" fill="#64748b">{n.type}{bound.has(n.id) ? ' · code' : ''}</text>
              <text x="13" y="34" fontSize="12.5" fontWeight="600" fill="currentColor">{short(String(label), 21)}</text>
              <title>{n.id} ({n.provenance})</title>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

function NodeDetail({node, cells}) {
  if (!node) return <div style={{color: 'var(--t2)', fontSize: 13}}>Click a node to see its settings, where they come from in the paper, and the code that implements it.</div>
  const code = (cells || []).filter(c => c.node_id === node.id)
  return (
    <div>
      <div style={{display: 'flex', gap: 8, alignItems: 'center', marginBottom: 8}}>
        <b>{node.id}</b><Tag c="na">{node.type}</Tag><Tag c={PROV_TAG[node.provenance] || 'na'}>{node.provenance}</Tag>
      </div>
      <div className="lab">Settings</div>
      <pre className="code" style={{whiteSpace: 'pre-wrap', fontSize: 12, maxHeight: 220, overflow: 'auto'}}>{jsonText(node.spec)}</pre>
      {node.sources?.length > 0 && <><div className="lab">From the paper</div>{node.sources.map((s, i) => <Quote key={i} src={s}/>)}</>}
      <div className="lab" style={{marginTop: 8}}>Code ({code.length} cell{code.length === 1 ? '' : 's'})</div>
      {code.length ? code.map((c, i) => <pre key={i} className="code" style={{whiteSpace: 'pre-wrap', fontSize: 12, maxHeight: 260, overflow: 'auto'}}>{c.source}</pre>)
        : <div style={{fontSize: 12.5, color: 'var(--t2)'}}>No notebook cell is bound to this node{node.type === 'claim' ? ' (claims are checked by the verifier)' : ''}.</div>}
      <div style={{fontSize: 11, color: 'var(--t2)', marginTop: 6}} className="m">hash {node.content_hash?.slice(0, 16)}</div>
    </div>
  )
}

function RunsTable({runs}) {
  if (!runs?.length) return <div style={{fontSize: 13, color: 'var(--t2)'}}>No runs of this version yet.</div>
  return (
    <div className="reg-card"><table>
      <thead><tr className="lab"><th>Run</th><th>Status</th><th>Clean runs</th><th>Ran on</th><th>Metrics (mean)</th></tr></thead>
      <tbody>{runs.map(r => (
        <tr key={r.run_id}>
          <td className="m">{r.run_id.slice(-8)}</td>
          <td><RunTag run={r}/>{r.status === 'waiting_gate' && <> <Link className="lk" to="/pipeline">answer gate</Link></>}</td>
          <td className="m">{r.n_ok}</td>
          <td className="m">{r.ran_on || '—'}</td>
          <td className="m">{Object.entries(r.metrics).map(([k, v]) => <div key={k}>{k}: {num(v)}</div>)}{!Object.keys(r.metrics).length && '—'}</td>
        </tr>))}</tbody>
    </table></div>
  )
}

export function Graph({ctx}) {
  const [ver, setVer] = useState(null)
  const [sel, setSel] = useState(null)
  const g = useGraph(ctx, ver)
  const cur = g.data?.current
  return (
    <>
      <div className="page-header">
        <h2>Experiment Graph</h2>
        <p className="sub">The paper as a graph of data, preprocessing, model, training, evaluation and claims. Solid border: stated in the paper (with the quote). Dashed: inferred. Built without a model, from the extracted setup; notebook cells are bound to the step they implement.</p>
      </div>
      <Pick ctx={ctx}/>
      <NoGraph g={g}/>
      {cur && (
        <>
          <div style={{display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap', marginBottom: 14}}>
            <VersionPick versions={g.data.versions} value={cur.version} onChange={v => { setVer(v); setSel(null) }}/>
            <span style={{fontSize: 12.5, color: 'var(--t2)'}}>{cur.graph.nodes.length} nodes · {cur.cells.length} cells · by {cur.created_by}{cur.parent_version ? ` from v${cur.parent_version}` : ''}</span>
            <Link className="lk" to={'/lab?v=' + cur.version}>Edit in Experiment Lab →</Link>
          </div>
          <GraphView graph={cur.graph} cells={cur.cells} selected={sel} onSelect={setSel}/>
          <div style={{display: 'flex', gap: 12, flexWrap: 'wrap', margin: '10px 0 18px', fontSize: 12}}>
            {COLS.map(t => <span key={t}><i style={{display: 'inline-block', width: 10, height: 10, background: TYPE_COLOR[t], borderRadius: 2, marginRight: 4}}/>{t}</span>)}
          </div>
          <div className="card-panel" style={{marginBottom: 18}}><NodeDetail node={cur.graph.nodes.find(n => n.id === sel)} cells={cur.cells}/></div>
          {cur.cells.some(c => !c.node_id) && <div style={{fontSize: 12.5, color: 'var(--t2)', marginBottom: 12}}>{cur.cells.filter(c => !c.node_id).length} setup cell(s) (imports, seeding) are not bound to a node.</div>}
          <div className="lab" style={{marginBottom: 6}}>Runs of this version</div>
          <RunsTable runs={cur.runs}/>
        </>
      )}
    </>
  )
}

export function Lab({ctx}) {
  const qv = +(new URLSearchParams(window.location.hash.split('?')[1] || '').get('v')) || null
  const [ver, setVer] = useState(qv)
  const g = useGraph(ctx, ver)
  const cur = g.data?.current
  const [draft, setDraft] = useState(null) // {base, specs: {id: text}, cells: [...], note}
  const [sel, setSel] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  const [msg, setMsg] = useState(null)
  const nav = useNavigate()

  const d = draft && cur && draft.base === cur.version ? draft : cur ? {base: cur.version, specs: {}, cells: cur.cells.map(c => ({...c})), note: ''} : null
  const upd = f => setDraft({...d, ...f(d)})
  const node = cur?.graph.nodes.find(n => n.id === sel)
  const ids = cur ? cur.graph.nodes.filter(n => n.type !== 'claim').map(n => n.id) : []
  const changed = d && (Object.keys(d.specs).length > 0 || JSON.stringify(d.cells) !== JSON.stringify(cur.cells))

  async function save(andRun) {
    setBusy(true); setErr(null); setMsg(null)
    try {
      const edits = {}
      for (const [id, t] of Object.entries(d.specs)) {
        try { edits[id] = JSON.parse(t) } catch { throw new Error(`settings of ${id} are not valid JSON`) }
      }
      const v = await cg(`/papers/${ctx.pid}/graph/versions`, {method: 'POST', body: {base: d.base, edits, cells: d.cells, note: d.note || null}})
      if (andRun) await cg(`/papers/${ctx.pid}/graph/versions/${v.version}/run`, {method: 'POST'})
      setDraft(null); setVer(v.version)
      setMsg(`Saved as v${v.version}${andRun ? ' and started a run (it goes to a worker; answer any gate in Pipeline)' : ''}.`)
      g.reload(); ctx.reload && ctx.reload()
    } catch (e) { setErr(e) } finally { setBusy(false) }
  }
  async function runIt() {
    setBusy(true); setErr(null); setMsg(null)
    try { await cg(`/papers/${ctx.pid}/graph/versions/${cur.version}/run`, {method: 'POST'}); setMsg(`Started a run of v${cur.version}.`); g.reload(); ctx.reload && ctx.reload() }
    catch (e) { setErr(e) } finally { setBusy(false) }
  }

  return (
    <>
      <div className="page-header">
        <h2>Experiment Lab</h2>
        <p className="sub">Change a step of the experiment (its settings or its code), save it as a new version and run it. Every version is kept; compare any two in <Link className="lk" to="/versions">Version Comparison</Link>.</p>
      </div>
      <Pick ctx={ctx}/>
      <NoGraph g={g}/>
      {err && <Err e={err}/>}
      {msg && <div className="card-panel" style={{marginBottom: 14}}>{msg}</div>}
      {cur && d && (
        <>
          <div style={{display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap', marginBottom: 14}}>
            <VersionPick label="Start from" versions={g.data.versions} value={cur.version} onChange={v => { setVer(v); setDraft(null); setSel(null) }}/>
            <button className="btn" disabled={busy || !cur.cells.length} onClick={runIt} title={cur.cells.length ? '' : 'this version has no code yet'}>Run v{cur.version} as is</button>
          </div>
          {!cur.cells.length && <div className="card-panel" style={{marginBottom: 14}}>This version has no notebook yet. Start an analysis with notebook generation on (the default) and approve the generated notebook in <Link className="lk" to="/pipeline">Pipeline</Link>; its cells appear here bound to the graph.</div>}
          <GraphView graph={cur.graph} cells={d.cells} selected={sel} onSelect={setSel}
            highlight={Object.fromEntries(Object.keys(d.specs).map(k => [k, 'modified']))}/>
          <div style={{display: 'grid', gridTemplateColumns: 'minmax(0,1fr) minmax(0,1.4fr)', gap: 16, marginTop: 16}}>
            <div className="card-panel">
              {node ? (
                <>
                  <div style={{marginBottom: 6}}><b>{node.id}</b> <Tag c={PROV_TAG[node.provenance] || 'na'}>{node.provenance}</Tag></div>
                  <div className="lab">Settings (JSON; keys you set are merged in)</div>
                  <textarea className="code" spellCheck={false} style={{width: '100%', minHeight: 200, fontFamily: 'monospace', fontSize: 12}}
                    value={d.specs[node.id] ?? jsonText(node.spec)} onChange={e => upd(x => ({specs: {...x.specs, [node.id]: e.target.value}}))}/>
                  {d.specs[node.id] != null && <button className="btn" style={{marginTop: 6}} onClick={() => upd(x => { const s = {...x.specs}; delete s[node.id]; return {specs: s} })}>Undo settings change</button>}
                  <div style={{fontSize: 12, color: 'var(--t2)', marginTop: 8}}>Settings record what the experiment should do and change the node's hash. To change what actually runs, edit the bound code on the right.</div>
                </>
              ) : <div style={{fontSize: 13, color: 'var(--t2)'}}>Click a node in the graph to edit its settings.</div>}
            </div>
            <div className="card-panel">
              <div className="lab" style={{marginBottom: 6}}>Notebook cells</div>
              {d.cells.map((c, i) => (
                <div key={i} style={{marginBottom: 12, outline: sel && c.node_id === sel ? '2px solid #f59e0b' : 'none', borderRadius: 6}}>
                  <div style={{display: 'flex', gap: 8, alignItems: 'center', marginBottom: 4, fontSize: 12}}>
                    <span className="m">[{i + 1}]</span>
                    <select value={c.node_id || ''} onChange={e => upd(x => ({cells: x.cells.map((y, j) => j === i ? {...y, node_id: e.target.value || null} : y)}))}>
                      <option value="">setup</option>{ids.map(id => <option key={id} value={id}>{id}</option>)}
                    </select>
                    <button className="btn" style={{padding: '2px 8px'}} onClick={() => upd(x => ({cells: x.cells.filter((_, j) => j !== i)}))}>remove</button>
                  </div>
                  <textarea spellCheck={false} style={{width: '100%', minHeight: Math.min(320, 40 + 16 * c.source.split('\n').length), fontFamily: 'monospace', fontSize: 12}}
                    value={c.source} onChange={e => upd(x => ({cells: x.cells.map((y, j) => j === i ? {...y, source: e.target.value} : y)}))}/>
                </div>
              ))}
              <button className="btn" onClick={() => upd(x => ({cells: [...x.cells, {node_id: sel && ids.includes(sel) ? sel : null, source: ''}]}))}>+ Add cell</button>
            </div>
          </div>
          <div className="card-panel" style={{marginTop: 16, display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap'}}>
            <input placeholder="What did you change and why? (shown in comparisons)" style={{flex: 1, minWidth: 260}} value={d.note} onChange={e => upd(() => ({note: e.target.value}))}/>
            <button className="btn" disabled={busy || !changed} onClick={() => save(false)}>Save as new version</button>
            <button className="btn primary" disabled={busy || !changed || !d.cells.length} onClick={() => save(true)}>Save and run</button>
            {changed && <button className="btn" disabled={busy} onClick={() => setDraft(null)}>Discard</button>}
            <button className="btn" onClick={() => nav('/versions')}>Compare versions →</button>
          </div>
          <div className="lab" style={{margin: '18px 0 6px'}}>Runs of v{cur.version}</div>
          <RunsTable runs={cur.runs}/>
        </>
      )}
    </>
  )
}

const ATTR_TAG = {attributed: 'ok', no_attributable_effect: 'na', inconclusive: 'wa'}
export function Versions({ctx}) {
  const g = useGraph(ctx, null)
  const vs = g.data?.versions || []
  const [a, setA] = useState(null)
  const [b, setB] = useState(null)
  const va = a || (vs.length > 1 ? vs[vs.length - 2].version : vs[0]?.version)
  const vb = b || vs[vs.length - 1]?.version
  const cmp = useCg(ctx.pid && va && vb && va !== vb ? `/papers/${ctx.pid}/graph/compare?a=${va}&b=${vb}&at=${runKey(ctx)}` : null,
    {every: d => ([...(d?.runs_a || []), ...(d?.runs_b || [])].some(r => ACTIVE.includes(r.status)) ? 3000 : 0)})
  const c = cmp.data
  const hl = c ? Object.fromEntries([...c.diff.invalidated.map(i => [i, 'invalid']), ...c.diff.modified.map(m => [m.node_id, 'modified']), ...c.diff.added.map(i => [i, 'added'])]) : {}
  const gb = useCg(ctx.pid && vb ? `/papers/${ctx.pid}/graph?version=${vb}` : null)
  return (
    <>
      <div className="page-header">
        <h2>Version Comparison</h2>
        <p className="sub">What changed between two versions of the experiment, which downstream results that invalidates, and whether a metric change can be attributed to the edit or is within seed noise (needs at least 2 clean runs per version).</p>
      </div>
      <Pick ctx={ctx}/>
      <NoGraph g={g}/>
      {vs.length === 1 && <Empty t={<>Only one version exists. Make an edit in <Link className="lk" to="/lab">Experiment Lab</Link> to create a second one.</>}/>}
      {vs.length > 1 && (
        <>
          <div style={{display: 'flex', gap: 18, flexWrap: 'wrap', marginBottom: 16}}>
            <VersionPick label="A (baseline)" versions={vs} value={va} onChange={setA}/>
            <VersionPick label="B (edited)" versions={vs} value={vb} onChange={setB}/>
          </div>
          {va === vb && <Empty t="Pick two different versions."/>}
          <Err e={cmp.err}/>
          {c && (
            <>
              {gb.data?.current && <GraphView graph={gb.data.current.graph} cells={gb.data.current.cells} highlight={hl}/>}
              <div style={{fontSize: 12, color: 'var(--t2)', margin: '8px 0 18px'}}>Yellow: edited · green: added · red: result invalidated by an upstream change{c.diff.env_changed ? ' · the environment changed too' : ''}</div>
              <div className="reg-card" style={{marginBottom: 18}}><table>
                <thead><tr className="lab"><th>Node</th><th>Change</th></tr></thead>
                <tbody>
                  {c.diff.added.map(i => <tr key={'a' + i}><td className="m">{i}</td><td><Tag c="ok">added</Tag></td></tr>)}
                  {c.diff.removed.map(i => <tr key={'r' + i}><td className="m">{i}</td><td><Tag c="no">removed</Tag></td></tr>)}
                  {c.diff.modified.map(m => (
                    <tr key={'m' + m.node_id}><td className="m">{m.node_id}</td><td>
                      {Object.entries(m.spec_changes).map(([k, [o, n]]) => <div key={k} className="m" style={{fontSize: 12.5}}>{k}: {JSON.stringify(o)} → <b>{JSON.stringify(n)}</b></div>)}
                      {!Object.keys(m.spec_changes).length && <span style={{fontSize: 12.5}}>{m.fields.join(', ') || 'code changed'}</span>}
                    </td></tr>))}
                  {!c.diff.added.length && !c.diff.removed.length && !c.diff.modified.length && <tr><td colSpan={2} style={{color: 'var(--t2)'}}>No structural or settings change.</td></tr>}
                </tbody>
              </table></div>
              {c.code_changes.length > 0 && (
                <>
                  <div className="lab" style={{marginBottom: 6}}>Code changes</div>
                  {c.code_changes.map(x => (
                    <div key={x.node_id || 'setup'} className="card-panel" style={{marginBottom: 12}}>
                      <b className="m">{x.node_id || 'setup'}</b>
                      <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 10, marginTop: 6}}>
                        <pre className="code" style={{whiteSpace: 'pre-wrap', fontSize: 11.5, maxHeight: 260, overflow: 'auto', background: '#fee2e222'}}>{x.before || '(none)'}</pre>
                        <pre className="code" style={{whiteSpace: 'pre-wrap', fontSize: 11.5, maxHeight: 260, overflow: 'auto', background: '#dcfce722'}}>{x.after || '(none)'}</pre>
                      </div>
                    </div>))}
                </>
              )}
              <div className="lab" style={{margin: '18px 0 6px'}}>Did the edit change the result?</div>
              {!c.attribution.length && <div className="card-panel">No metrics yet. Run both versions (from <Link className="lk" to="/lab">Experiment Lab</Link>, at least 2 clean runs each) to attribute a change.</div>}
              {c.attribution.map(r => (
                <div key={r.metric} className="card-panel" style={{marginBottom: 12}}>
                  <div style={{display: 'flex', gap: 10, alignItems: 'center'}}><b>{r.metric}</b><Tag c={ATTR_TAG[r.status] || 'na'}>{r.status.replaceAll('_', ' ')}</Tag>
                    {r.delta != null && <span className="m">Δ {r.delta > 0 ? '+' : ''}{num(r.delta)}{r.noise_se != null && <> (noise SE {num(r.noise_se)})</>}</span>}
                    <span style={{fontSize: 12, color: 'var(--t2)'}}>n = {r.n_a} vs {r.n_b}</span></div>
                  {r.contributions?.map(x => <div key={x.node_id} className="m" style={{fontSize: 12.5}}>{x.node_id}: {num(x.delta)}</div>)}
                  {[...(r.flags || []), ...(r.caveats || [])].map((x, i) => <div key={i} style={{fontSize: 12, color: 'var(--t2)', marginTop: 3}}>{x}</div>)}
                </div>))}
              <div style={{display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16, marginTop: 10}}>
                <div><div className="lab" style={{marginBottom: 6}}>Runs of v{va}</div><RunsTable runs={c.runs_a}/></div>
                <div><div className="lab" style={{marginBottom: 6}}>Runs of v{vb}</div><RunsTable runs={c.runs_b}/></div>
              </div>
            </>
          )}
        </>
      )}
    </>
  )
}
