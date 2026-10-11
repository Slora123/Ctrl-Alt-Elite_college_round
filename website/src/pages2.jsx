// Pages that talk to the CrisisGrid FastAPI (proxied under /cg): account management, the embedded notebook
// environment, and the live pipeline (papers -> audit -> clean runs -> verdicts, with human gates).
import {useState,useEffect,useRef,useCallback} from 'react'
import {useNavigate,useSearchParams} from 'react-router-dom'
import {cg,useAuth,getToken,authHeaders} from './auth'
import {downloadNotebook} from './real'

const Err=({e})=>e?<div className="err" style={{margin:'12px 0'}}>{String(e.message||e)}</div>:null
const Tag=({c,children})=><span className={'tag '+c}>{children}</span>
const when=s=>s?new Date(s).toLocaleString():'—'

// ------------------------------------------------------------------------------------------------- Account
export function Account(){
  const {user,setToken,logout,cfg}=useAuth()
  const [tokens,setTokens]=useState([])
  const [users,setUsers]=useState([])
  const [err,setErr]=useState(null)
  const [fresh,setFresh]=useState(null)
  const [tname,setTname]=useState('')
  const [pw,setPw]=useState({current_password:'',new_password:''})
  const [msg,setMsg]=useState('')
  const load=useCallback(async()=>{
    try{
      if(cfg?.auth_enabled){
        setTokens(await cg('/auth/tokens'))
        if(user?.role==='admin')setUsers(await cg('/auth/users'))
      }
    }catch(e){setErr(e)}
  },[user,cfg])
  useEffect(()=>{load()},[load])
  const run=fn=>async(...a)=>{setErr(null);setMsg('');try{await fn(...a);await load()}catch(e){setErr(e)}}

  if(cfg&&!cfg.auth_enabled)return <><div className="page-header"><h2>Account</h2></div><div className="card-panel">Authentication is switched off on this server (<code>CG_AUTH_ENABLED=0</code>), so everything runs as a local administrator.</div></>
  return (
    <>
      <div className="page-header"><h2>Account</h2><p className="sub">Your profile, password and API tokens. One login covers the dashboard, the pipeline and the notebook.</p></div>
      <Err e={err}/>
      {msg&&<div className="card-panel" style={{marginBottom:16}}>{msg}</div>}
      <div className="card-panel" style={{maxWidth:640,marginBottom:20}}>
        <div className="lab">Profile</div>
        <p><b>{user?.name}</b> · {user?.email}<br/>Role: <Tag c={user?.role==='admin'?'ok':'na'}>{user?.role}</Tag> {user?.role==='viewer'&&' (read-only)'}</p>
        <button className="btn g" onClick={run(async()=>{await cg('/auth/logout-all',{method:'POST'});logout()})}>Sign out of all devices</button>
      </div>

      <div className="card-panel" style={{maxWidth:640,marginBottom:20}}>
        <div className="lab">Change password</div>
        <div className="frm-field"><label className="frm-label">Current password</label><input className="frm-input" type="password" value={pw.current_password} onChange={e=>setPw({...pw,current_password:e.target.value})} autoComplete="current-password"/></div>
        <div className="frm-field"><label className="frm-label">New password (10+ characters)</label><input className="frm-input" type="password" value={pw.new_password} onChange={e=>setPw({...pw,new_password:e.target.value})} autoComplete="new-password"/></div>
        <button className="btn" style={{marginTop:10}} onClick={run(async()=>{const r=await cg('/auth/change-password',{method:'POST',body:pw});setToken(r.token);setPw({current_password:'',new_password:''});setMsg('Password changed. Your other sessions were signed out.')})}>Change password</button>
      </div>

      <div className="card-panel" style={{maxWidth:820,marginBottom:20}}>
        <div className="lab">API tokens</div>
        <p style={{fontSize:13,color:'var(--t2)'}}>For scripts and workers. A token acts with your role. Send it as <code>Authorization: Bearer cgt_…</code>.</p>
        <div style={{display:'flex',gap:8,marginBottom:12}}>
          <input className="frm-input" placeholder="token name, e.g. laptop script" value={tname} onChange={e=>setTname(e.target.value)}/>
          <button className="btn" disabled={!tname||user?.role==='viewer'} onClick={run(async()=>{setFresh(await cg('/auth/tokens',{method:'POST',body:{name:tname}}));setTname('')})}>Create token</button>
        </div>
        {fresh&&<div className="card-panel" style={{background:'var(--bg2,#f5f7fa)',marginBottom:12}}><b>Copy it now, it cannot be shown again:</b><br/><code style={{wordBreak:'break-all'}}>{fresh.token}</code></div>}
        <table><thead><tr><th>Name</th><th>Prefix</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>
          {tokens.filter(t=>!t.revoked).map(t=><tr key={t.id}><td>{t.name}</td><td><code>{t.prefix}…</code></td><td>{when(t.created_at)}</td><td>{when(t.last_used_at)}</td>
            <td><button className="btn-sm-danger" onClick={run(()=>cg('/auth/tokens/'+t.id,{method:'DELETE'}))}>Revoke</button></td></tr>)}
          {!tokens.some(t=>!t.revoked)&&<tr><td colSpan={5} style={{color:'var(--t2)'}}>No active tokens.</td></tr>}
        </tbody></table>
      </div>

      {user?.role==='admin'&&<div className="card-panel" style={{maxWidth:820}}>
        <div className="lab">Users (administrator)</div>
        <table><thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Last sign-in</th><th>Status</th></tr></thead><tbody>
          {users.map(u=><tr key={u.id}><td>{u.name}</td><td>{u.email}</td>
            <td><select className="frm-input" value={u.role} onChange={run(e=>cg('/auth/users/'+u.id,{method:'PATCH',body:{role:e.target.value}}))}>{['viewer','researcher','admin'].map(r=><option key={r}>{r}</option>)}</select></td>
            <td>{when(u.last_login_at)}</td>
            <td><button className={u.disabled?'btn-sm':'btn-sm-danger'} onClick={run(()=>cg('/auth/users/'+u.id,{method:'PATCH',body:{disabled:!u.disabled}}))}>{u.disabled?'Enable':'Disable'}</button></td></tr>)}
        </tbody></table>
      </div>}
    </>
  )
}

// ------------------------------------------------------------------------------------------------- Notebook
export function Notebook(){
  const [sp,setSp]=useSearchParams()
  const envId=sp.get('env')
  const [envs,setEnvs]=useState(null)
  const [err,setErr]=useState(null)
  const [f,setF]=useState({alias:'',requirements:'',build:true})
  const [busy,setBusy]=useState(false)
  const load=useCallback(async()=>{try{setEnvs(await cg('/paper-env/paper-envs'))}catch(e){setErr(e);setEnvs([])}},[])
  useEffect(()=>{load()},[load])
  useEffect(()=>{ // the embedded notebook asks us to sign in again when its token is rejected
    const h=e=>{if(e.origin===location.origin&&e.data?.type==='cg:unauthorized')window.dispatchEvent(new Event('cg:unauthorized'))}
    window.addEventListener('message',h);return()=>window.removeEventListener('message',h)
  },[])
  const create=async e=>{
    e.preventDefault();setBusy(true);setErr(null)
    try{
      const reqs=f.requirements.split(/[,\n]/).map(s=>s.trim()).filter(Boolean)
      const r=await cg('/paper-env/paper-envs',{method:'POST',body:{alias:f.alias.trim(),paper_hash:'manual',requirements:reqs,build_env:f.build}})
      if(r.env_build&&r.env_build.status!=='success')setErr(new Error('Environment build failed: '+(r.env_build.error||r.env_build.conflict_details||'unknown')))
      await load();setSp({env:r.paper_env.id})
    }catch(x){setErr(x)}
    setBusy(false)
  }
  const cur=envs?.find(x=>x.id===envId)
  return (
    <>
      <div className="page-header"><h2>Notebook</h2><p className="sub">Colab-style notebooks with an isolated Python environment per paper: versioned cells, clean runs, datasets, checkpoints.</p></div>
      <Err e={err}/>
      <div style={{display:'flex',gap:8,alignItems:'center',flexWrap:'wrap',marginBottom:12}}>
        <select className="frm-input" style={{maxWidth:320}} value={envId||''} onChange={e=>setSp(e.target.value?{env:e.target.value}:{})}>
          <option value="">{envs===null?'Loading…':envs.length?'Select a paper environment…':'No environments yet'}</option>
          {(envs||[]).map(x=><option key={x.id} value={x.id}>{x.alias}</option>)}
        </select>
        {cur&&<a className="btn g" href={`/cg/paper-env/ui/?env=${cur.id}`} target="_blank" rel="noreferrer">Open in a new tab ↗</a>}
        {cur&&<button className="btn g" onClick={()=>downloadNotebook(cur.id,cur.alias||'notebook').catch(setErr)}>Download .ipynb</button>}
      </div>
      {!cur&&<form className="frm-card" onSubmit={create}>
        <div className="frm-grid">
          <div className="frm-field"><label className="frm-label">Alias (unique)</label><input className="frm-input" required placeholder="e.g. resnet-2015" value={f.alias} onChange={e=>setF({...f,alias:e.target.value})}/></div>
          <div className="frm-field"><label className="frm-label">Python packages (comma separated, optional)</label><input className="frm-input" placeholder="numpy, pandas, ipywidgets" value={f.requirements} onChange={e=>setF({...f,requirements:e.target.value})}/></div>
        </div>
        <div className="frm-footer"><label style={{fontSize:13,marginRight:16}}><input type="checkbox" checked={f.build} onChange={e=>setF({...f,build:e.target.checked})}/> build the environment now (first time needs network)</label>
          <button className="btn" disabled={busy}>{busy?'Creating… (building can take a minute)':'Create paper environment'}</button></div>
      </form>}
      {cur&&<iframe key={cur.id} title="notebook" src={`/cg/paper-env/ui/?env=${cur.id}`} style={{width:'100%',height:'calc(100vh - 230px)',minHeight:520,border:'1px solid var(--bd)',borderRadius:8,background:'#fff'}}/>}
    </>
  )
}

// ------------------------------------------------------------------------------------------------- Pipeline
const STATUS_TAG={completed:'ok',failed:'no',waiting_gate:'wa',waiting_jobs:'wa',running:'na',created:'na'}

export function Pipeline(){
  const nav=useNavigate()
  const {user}=useAuth()
  const [papers,setPapers]=useState([])
  const [sel,setSel]=useState(null)
  const [detail,setDetail]=useState(null)
  const [run,setRun]=useState(null)
  const [events,setEvents]=useState([])
  const [gates,setGates]=useState([])
  const [claims,setClaims]=useState([])
  const [gaps,setGaps]=useState([])
  const [report,setReport]=useState('')
  const [err,setErr]=useState(null)
  const [busy,setBusy]=useState(false)
  const [seeds,setSeeds]=useState('0,1,2')
  const [nb,setNb]=useState(null)
  const [gateJson,setGateJson]=useState({})
  const esRef=useRef(null)
  const writer=user?.role!=='viewer'
  const safe=fn=>async(...a)=>{setErr(null);try{await fn(...a)}catch(e){setErr(e)}}

  const loadPapers=useCallback(safe(async()=>{setPapers(await cg('/papers'))}),[])
  useEffect(()=>{loadPapers()},[loadPapers])

  const refresh=useCallback(async(paperId,runId)=>{
    try{
      const d=await cg('/papers/'+paperId);setDetail(d)
      const rid=runId||d.runs[d.runs.length-1]?.run_id
      if(rid){setRun(await cg('/runs/'+rid));setGates(await cg('/gates?run_id='+rid))}
      else {setRun(null);setGates([])}
      setClaims(await cg('/papers/'+paperId+'/claims').catch(()=>[]))
      setGaps(await cg('/papers/'+paperId+'/audit').catch(()=>[]))
      setReport(await cg('/reports/'+paperId+'?format=md',{text:true}).catch(()=>''))
    }catch(e){setErr(e)}
  },[])

  const follow=useCallback(runId=>{ // live events (EventSource cannot send headers, so the stream accepts ?token=)
    esRef.current?.close()
    const t=getToken()
    const es=new EventSource(`/cg/runs/${runId}/events${t?`?token=${encodeURIComponent(t)}`:''}`)
    esRef.current=es
    let timer
    const onAny=ev=>{
      let data={};try{data=JSON.parse(ev.data)}catch{/* keep-alive */}
      setEvents(x=>[...x.slice(-199),{kind:ev.type,ts:data.ts,module:data.module,data}])
      clearTimeout(timer);timer=setTimeout(()=>sel&&refresh(sel,runId),400)
    }
    ;['run_started','node_started','node_finished','gate_opened','gate_resolved','awaiting_worker','job_enqueued','job_started','job_finished','run_finished','run_failed','error','message']
      .forEach(k=>es.addEventListener(k,onAny))
    es.onmessage=onAny
    es.onerror=()=>{es.close()}
  },[sel,refresh])
  useEffect(()=>()=>esRef.current?.close(),[])

  const [sp]=useSearchParams()
  const wanted=sp.get('paper')
  const choose=safe(async id=>{setSel(id);setEvents([]);await refresh(id);const d=await cg('/papers/'+id);const last=d.runs[d.runs.length-1];if(last&&!['completed','failed'].includes(last.status))follow(last.run_id)})

  useEffect(()=>{if(wanted&&wanted!==sel&&papers.some(x=>x.id===wanted))choose(wanted)},[wanted,papers]) // eslint-disable-line react-hooks/exhaustive-deps

  const upload=safe(async e=>{
    const f=e.target.files[0];if(!f)return
    setBusy(true)
    try{
      const r=await cg('/papers',{method:'POST',raw:await f.arrayBuffer(),headers:{'Content-Type':'application/pdf','X-Filename':f.name}})
      await loadPapers();await choose(r.paper_id)
    }finally{setBusy(false);e.target.value=''}
  })
  const start=safe(async()=>{
    setBusy(true)
    try{
      const body={seeds:seeds.split(',').map(s=>parseInt(s.trim(),10)).filter(n=>!Number.isNaN(n))}
      if(nb)body.notebook=JSON.parse(await nb.text())
      const r=await cg('/papers/'+sel+'/runs',{method:'POST',body})
      setEvents([]);await refresh(sel,r.run_id);follow(r.run_id)
    }finally{setBusy(false)}
  })
  const respond=safe(async(g,action)=>{
    let payload={};try{if(gateJson[g.id])payload=JSON.parse(gateJson[g.id])}catch{throw new Error('payload must be valid JSON')}
    await cg('/gates/'+g.id+'/respond',{method:'POST',body:{action,payload}})
    await refresh(sel,run?.run_id);if(run)follow(run.run_id)
  })
  const openNotebook=safe(async()=>{
    const p=papers.find(x=>x.id===sel);const alias='paper-'+sel.slice(-8)
    const envs=await cg('/paper-env/paper-envs')
    let env=envs.find(e=>e.alias===alias)
    if(!env)env=(await cg('/paper-env/paper-envs',{method:'POST',body:{alias,paper_hash:p.paper_hash}})).paper_env
    nav('/notebook?env='+env.id)
  })

  const p=papers.find(x=>x.id===sel)
  return (
    <>
      <div className="page-header"><h2>Pipeline</h2><p className="sub">The real backend: ingest a PDF, audit it, route it, run clean experiments on workers, and read evidence-based verdicts. Human decisions happen at gates.</p></div>
      <Err e={err}/>
      <div style={{display:'grid',gridTemplateColumns:'minmax(240px,320px) 1fr',gap:20,alignItems:'start'}}>
        <div className="card-panel">
          <div className="lab">Papers</div>
          {writer&&<label className="btn" style={{display:'block',textAlign:'center',cursor:'pointer',marginBottom:12}}>{busy?'Uploading…':'Upload PDF'}<input type="file" accept="application/pdf" hidden onChange={upload}/></label>}
          {papers.map(x=><div key={x.id} onClick={()=>choose(x.id)} style={{padding:'8px 10px',cursor:'pointer',borderRadius:6,background:x.id===sel?'var(--bg2,#eef2f7)':'transparent'}}>
            <div style={{fontSize:13,fontWeight:600}}>{x.filename||'paper'}</div><div style={{fontSize:11,color:'var(--t2)'}}>{x.id.slice(-8)} · {Math.round(x.size/1024)} KB</div></div>)}
          {!papers.length&&<div style={{fontSize:13,color:'var(--t2)'}}>No papers yet.</div>}
        </div>

        <div>
          {!p&&<div className="empty-box"><div className="empty-text">Select or upload a paper.</div></div>}
          {p&&<>
            <div className="card-panel" style={{marginBottom:16}}>
              <div style={{display:'flex',gap:10,alignItems:'center',flexWrap:'wrap'}}>
                <b>{p.filename||'paper'}</b><span className="meta" style={{fontSize:12,color:'var(--t2)'}}>sha {p.paper_hash.slice(0,12)}…</span><span style={{flex:1}}/>
                <button className="btn g" onClick={openNotebook}>Open notebook ▣</button>
              </div>
              {writer&&<div style={{display:'flex',gap:10,alignItems:'center',flexWrap:'wrap',marginTop:12}}>
                <label style={{fontSize:12}}>seeds <input className="frm-input" style={{width:100}} value={seeds} onChange={e=>setSeeds(e.target.value)}/></label>
                <label className="btn g" style={{cursor:'pointer'}}>{nb?nb.name:'Attach .ipynb (optional)'}<input type="file" accept=".ipynb,application/json" hidden onChange={e=>setNb(e.target.files[0]||null)}/></label>
                <button className="btn" disabled={busy} onClick={start}>Start run</button>
              </div>}
              <p style={{fontSize:12,color:'var(--t2)',margin:'10px 0 0'}}>Runs need an LLM (<code>CG_LLM_CHAIN</code>) for the agents and a worker for experiments (<code>python -m crisisgrid_api.worker --api … --executor paper_env</code>). Without a notebook the run ends with an honest static-audit report.</p>
            </div>

            {run&&<div className="card-panel" style={{marginBottom:16}}>
              <div className="lab">Latest run</div>
              <p><Tag c={STATUS_TAG[run.status]||'na'}>{run.status}</Tag> node <b>{run.node}</b> {run.route&&<>· route <b>{run.route.case ?? JSON.stringify(run.route)}</b></>} {run.needs_ocr&&<Tag c="wa">needs OCR</Tag>}</p>
              {run.error&&<div className="err">{run.error}</div>}
              {run.caveats?.length>0&&<ul style={{fontSize:12,color:'var(--t2)'}}>{run.caveats.map((c,i)=><li key={i}>{c}</li>)}</ul>}
            </div>}

            {gates.length>0&&<div className="card-panel" style={{marginBottom:16,borderColor:'#C98B2B'}}>
              <div className="lab">Waiting for you</div>
              {gates.map(g=><div key={g.id} style={{padding:'10px 0',borderTop:'1px solid var(--bd)'}}>
                <b>{g.kind}</b> gate <pre style={{fontSize:11,maxHeight:160,overflow:'auto',background:'var(--bg2,#f5f7fa)',padding:8,borderRadius:6}}>{JSON.stringify(g.context,null,1)}</pre>
                {writer&&<><textarea className="frm-input" rows={2} placeholder='optional JSON payload, e.g. {"hint":"use the CPU dataset"}' value={gateJson[g.id]||''} onChange={e=>setGateJson({...gateJson,[g.id]:e.target.value})} style={{width:'100%',fontFamily:'monospace',fontSize:12}}/>
                  <div style={{display:'flex',gap:8,marginTop:6,flexWrap:'wrap'}}>{g.allowed_actions.map(a=><button key={a} className="btn g" onClick={()=>respond(g,a)}>{a}</button>)}</div></>}
              </div>)}
            </div>}

            {claims.length>0&&<div className="card-panel" style={{marginBottom:16}}>
              <div className="lab">Claims and verdicts</div>
              <table><thead><tr><th>Claim</th><th>Reported</th><th>Verdict</th></tr></thead><tbody>
                {claims.map(c=><tr key={c.claim.id}><td>{c.claim.statement||c.claim.text}</td><td>{c.claim.reported_value ?? '—'}</td>
                  <td><Tag c={c.verdict.status==='reproduced'?'ok':c.verdict.status==='partial'?'wa':c.verdict.status==='not_executed'||c.verdict.status==='inconclusive'?'na':'no'}>{c.verdict.status}</Tag>
                    {(c.verdict.caveats||[]).map((x,i)=><div key={i} style={{fontSize:11,color:'var(--t2)'}}>{x}</div>)}</td></tr>)}
              </tbody></table></div>}

            {gaps.length>0&&<div className="card-panel" style={{marginBottom:16}}>
              <div className="lab">Audit findings ({gaps.length})</div>
              <table><thead><tr><th>Rule</th><th>Severity</th><th>Finding</th></tr></thead><tbody>
                {gaps.map((g,i)=><tr key={i}><td>{g.field||g.rule||'—'}</td><td><Tag c={g.severity==='blocking'||g.severity==='major'?'no':g.severity==='minor'?'wa':'na'}>{g.severity}</Tag></td><td>{g.description||g.summary||g.title}</td></tr>)}
              </tbody></table></div>}

            {events.length>0&&<div className="card-panel" style={{marginBottom:16}}>
              <div className="lab">Live events</div>
              <div style={{maxHeight:200,overflow:'auto',fontFamily:'monospace',fontSize:11}}>{events.slice().reverse().map((e,i)=><div key={i}><b>{e.kind}</b> {e.module||''} {e.data?.node||e.data?.status||e.data?.message||''}</div>)}</div></div>}

            {report&&<div className="card-panel"><div className="lab">Report</div><pre style={{whiteSpace:'pre-wrap',fontSize:12.5}}>{report}</pre></div>}
          </>}
        </div>
      </div>
    </>
  )
}
