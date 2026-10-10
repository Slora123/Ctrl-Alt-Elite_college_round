import {useState} from 'react'
import {useNavigate,Link,NavLink} from 'react-router-dom'
import {api,useLoad} from './api'

const pct = x => x == null ? '—' : (x * 100).toFixed(1) + '%'
const Tag = ({c,children}) => <span className={'tag ' + c}>{children}</span>
const Mk = ({x}) => x && x.source === 'mock-ai' ? <Tag c="na">MOCK AI</Tag> : null

const Spark = ({v}) => (
  <svg width="84" height="24" viewBox="0 0 84 24" style={{verticalAlign:'middle',overflow:'visible'}}>
    <polyline fill="none" stroke="var(--dv)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" points={v.map((y,i)=>(i/(Math.max(v.length-1,1))*80+2)+','+(22-y*20)).join(' ')}/>
  </svg>
)

const cstat = (r,p,t) => {
  if (p == null) return ['na','Not tested']
  const d = Math.abs(r - p)
  return d <= t ? ['ok','Matches within tolerance'] : d <= t * 3 ? ['wa','Partial match'] : ['no','Does not match']
}

const St = ({loading,err}) => loading ? (
  <div className="status-loading"><span className="spinner"/> Loading data…</div>
) : err ? (
  <div className="err">API Error: {err}. Ensure the backend server is running on port 8000.</div>
) : null

const Empty = ({t, action}) => (
  <div className="empty-box">
    <div className="empty-icon">📂</div>
    <div className="empty-text">{t}</div>
    {action && <div style={{marginTop:12}}>{action}</div>}
  </div>
)

const WORKSPACE_TABS = [
  ['understand', 'Paper Understanding'],
  ['artifact', 'Artifact Verification'],
  ['claims', 'Claim Verification'],
  ['lab', 'Experiment Lab'],
  ['graph', 'Experiment Graph'],
  ['versions', 'Version Comparison'],
  ['audit', 'Research Audit'],
  ['reports', 'Evidence Reports'],
  ['history', 'Experiment History']
]

const Pick = ({ctx}) => ctx.papers.length ? (
  <div className="workspace-header">
    <div className="picker-card">
      <div className="picker-left">
        <span className="picker-label">ACTIVE MANUSCRIPT</span>
        <div className="picker-select-wrapper">
          <select className="picker-select" value={ctx.pid||''} onChange={e=>ctx.setPid(+e.target.value)}>
            {ctx.papers.map(p=><option key={p.id} value={p.id}>Paper #{p.id} · {p.title.slice(0,75)}</option>)}
          </select>
        </div>
      </div>
      {ctx.pid && (
        <div className="picker-meta">
          <span className="chip"><i className="dot"/> {ctx.papers.find(p=>p.id===ctx.pid)?.pages || 0} Pages</span>
          <span className="chip">{((ctx.papers.find(p=>p.id===ctx.pid)?.size || 0)/1024).toFixed(0)} KB</span>
        </div>
      )}
    </div>
    <div className="ws-tabs-bar">
      {WORKSPACE_TABS.map(([k, name]) => (
        <NavLink
          key={k}
          to={'/' + k}
          className={({isActive}) => 'ws-tab' + (isActive ? ' active' : '')}
        >
          {name}
        </NavLink>
      ))}
    </div>
  </div>
) : (
  <div className="empty-box" style={{marginBottom:24}}>
    <div className="empty-icon">📄</div>
    <div className="empty-text">No papers uploaded yet. <Link className="lk" to="/papers">Upload a PDF paper</Link> to begin.</div>
  </div>
)

function Form({fields,onSubmit,btn,init={}}){
  const [v,setV]=useState(init)
  const [err,setErr]=useState('')
  const [busy,setB]=useState(false)
  const sub=async e=>{
    e.preventDefault()
    setB(true)
    setErr('')
    try{
      await onSubmit(v)
      setV(init)
    }catch(x){
      setErr(x.message)
    }
    setB(false)
  }
  return (
    <form onSubmit={sub} className="frm-card">
      <div className="frm-grid">
        {fields.map(([k,l,o])=>(
          <div key={k} className="frm-field">
            <label className="frm-label">{l}</label>
            {o ? (
              <select className="frm-input" value={v[k]||o[0]} onChange={e=>setV({...v,[k]:e.target.value})}>
                {o.map(x=><option key={x}>{x}</option>)}
              </select>
            ) : (
              <input className="frm-input" placeholder={`Enter ${l.toLowerCase()}...`} value={v[k]||''} onChange={e=>setV({...v,[k]:e.target.value})}/>
            )}
          </div>
        ))}
      </div>
      <div className="frm-footer">
        <button className="btn" disabled={busy}>{busy?'Saving…':btn}</button>
      </div>
      {err && <div className="err">{err}</div>}
    </form>
  )
}

const post = (k,b) => api('/items/' + k, {method:'POST', body:b})

export function Mission({ctx}){
  const {data:d,err,loading,reload:rl}=useLoad('/overview'),go=useNavigate(),{tol}=ctx
  if(!d)return <St loading={loading} err={err}/>
  const {papers,claims,versions,findings,artifacts}=d
  const tested=claims.filter(c=>c.reproduced!=null)
  const ver=tested.filter(c=>Math.abs(c.reported-c.reproduced)<=tol).length
  const open=findings.filter(f=>f.status!=='Resolved').length
  const c=tested[0]
  const p=c&&papers.find(x=>x.id===c.paper_id)
  const tp=(tol*100).toFixed(1)
  const lo=c&&Math.min(c.reported,c.reproduced)-.03
  const hi=c&&Math.max(c.reported,c.reproduced)+.03
  const x=v=>((v-lo)/(hi-lo)*100)+'%'
  const cs=c&&cstat(c.reported,c.reproduced,tol)
  const PS=[
    ['Paper','papers',papers.length,papers.length+' uploaded'],
    ['Method','understand',papers.some(p=>p.chars),papers.filter(p=>p.chars).length+' with extracted text'],
    ['Dataset','artifacts',artifacts.some(a=>a.kind==='dataset'),artifacts.length+' artifacts saved'],
    ['Model','lab',versions.length,versions.length+' versions saved'],
    ['Evaluation','versions',versions.some(v=>v.metric!=null),versions.filter(v=>v.metric!=null).length+' with results'],
    ['Claim','claims',tested.length,ver+' of '+tested.length+' within ±'+tp+' pts']
  ]
  return (
    <>
      <div className="topA">
        <div>
          <h1>Research, put to the test.</h1>
          <p className="sub" style={{fontSize:15,marginBottom:18}}>Reconstruct experiments. Verify claims. Understand why results differ.</p>
          <div style={{display:'flex',gap:12,alignItems:'center',flexWrap:'wrap',marginBottom:24}}>
            <button className="btn" onClick={()=>go('/papers')}>Analyze a paper →</button>
            <button className="btn g" onClick={()=>go('/lab')}>Open Experiment Lab</button>
          </div>
          <div className="strip">
            {[[papers.length,'Papers uploaded'],[ver+'/'+tested.length,'Claims within tolerance'],[versions.length,'Versions saved'],[open,'Open findings']].map(([v,l])=>(
              <div key={l}>
                <b>{v}</b>
                <span className="lab">{l}</span>
              </div>
            ))}
          </div>
        </div>
        <section className="layer">
          <div className="lab">Latest tested claim · user-entered values</div>
          {c ? (
            <>
              <h2 className="ct">{p?.title}</h2>
              <div style={{fontSize:14,color:'var(--tx)',fontWeight:500,marginBottom:12}}>{c.statement}</div>
              <div className="scale">
                <i className="band" style={{left:x(c.reported-tol),width:(2*tol/(hi-lo)*100)+'%'}}/>
                <b className="tk" style={{left:x(c.reported),background:'var(--dv)'}}/>
                <b className="tk" style={{left:x(c.reproduced),background:'#D98E64'}}/>
              </div>
              <div className="m" style={{fontSize:11,color:'var(--t2)',marginBottom:16}}>reported (violet) · reproduced (peach) · green = ±{tp} pts</div>
              <div className="facts">
                <div><span className="lab">Reported</span><br/><b className="m">{pct(c.reported)}</b></div>
                <div><span className="lab">Reproduced</span><br/><b className="m">{pct(c.reproduced)}</b></div>
                <div><span className="lab">Difference</span><br/><b className="m">{((c.reproduced-c.reported)*100).toFixed(1)} pts</b></div>
                <div style={{display:'flex',alignItems:'center'}}><Tag c={cs[0]}>{cs[1]}</Tag></div>
              </div>
            </>
          ) : <Empty t="No claim with a reproduced metric yet. Add one under Claim Verification."/>}
        </section>
      </div>

      <div className="zone">
        <span className="lab">A · Research Pipeline</span>
        <span>Stage is complete only if data exists for it</span>
      </div>
      <div className="pipeR">
        {PS.map(([k,g,ok,m],i)=>(
          <button key={k} className="ps" onClick={()=>go('/'+g)} style={{borderTopColor:ok?'#22c55e':'var(--bd)'}}>
            <span className="lab">0{i+1}</span>
            <b>{k}</b>
            <Tag c={ok?'ok':'na'}>{ok?'has data':'empty'}</Tag>
            <small>{m}</small>
          </button>
        ))}
      </div>

      <div className="zone">
        <span className="lab">B · Research Register</span>
        <span>{papers.length} papers recorded</span>
      </div>
      <div className="reg-card">
        {papers.length ? (
          <table>
            <thead>
              <tr className="lab">
                <th>Paper Title</th>
                <th>Pages</th>
                <th>Claims</th>
                <th>Open findings</th>
                <th style={{textAlign:'right'}}>Inspect</th>
              </tr>
            </thead>
            <tbody>
              {papers.map(p=>(
                <tr key={p.id} className="rw" onClick={()=>{ctx.setPid(p.id);go('/understand')}}>
                  <td>
                    <b>{p.title}</b>
                    <br/>
                    <span className="m" style={{fontSize:11,color:'var(--t2)'}}>#{p.id} · {p.filename}</span>
                  </td>
                  <td className="m">{p.pages}</td>
                  <td className="m">{claims.filter(c=>c.paper_id===p.id).length}</td>
                  <td className="m">{findings.filter(f=>f.paper_id===p.id&&f.status!=='Resolved').length}</td>
                  <td style={{textAlign:'right'}}><span className="lk">Open →</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <Empty
            t="Nothing here yet. Upload a PDF in Research Papers."
            action={<button className="btn g" onClick={async()=>{await api('/seed',{method:'POST'});ctx.reloadPapers();rl()}}>Load Demo Dataset</button>}
          />
        )}
      </div>
    </>
  )
}

export function Papers({ctx}){
  const [msg,setMsg]=useState(''),[busy,setB]=useState(false),go=useNavigate()
  const up=async f=>{
    if(!f)return;setB(true);setMsg('')
    const fd=new FormData();fd.append('file',f)
    try{
      const r=await api('/papers',{method:'POST',body:fd})
      setMsg(`Uploaded: ${r.title}. ${r.extracted?'Text extracted with PyMuPDF.':r.note||'No text found in this PDF.'}`)
      ctx.reloadPapers();ctx.setPid(r.id)
    }catch(e){setMsg('Error: '+e.message)}
    setB(false)
  }
  const del=async id=>{
    if(confirm('Delete this paper and its saved data?')){
      await api('/papers/'+id,{method:'DELETE'})
      ctx.reloadPapers()
    }
  }
  return (
    <>
      <div className="page-header">
        <h2>Research Papers</h2>
        <p className="sub">Upload manuscripts in PDF format to parse text, inspect experimental methodology, and verify published metrics.</p>
      </div>

      <div className="drop-card" onDragOver={e=>e.preventDefault()} onDrop={e=>{e.preventDefault();up(e.dataTransfer.files[0])}}>
        <div className="drop-icon">📄</div>
        <div className="drop-title">Drop your research PDF paper here</div>
        <p className="drop-sub">Drag & drop a file here, or click to select from your device (up to 20MB).</p>
        <label className="btn" style={{cursor:'pointer',display:'inline-flex',alignItems:'center',gap:8}}>
          Browse PDF File
          <input type="file" accept="application/pdf,.pdf" style={{display:'none'}} onChange={e=>{up(e.target.files[0]);e.target.value=''}}/>
        </label>
        {busy && <div className="drop-status busy">Uploading and parsing paper…</div>}
        {msg && <div className={msg.startsWith('Error')?'err':'drop-status success'}>{msg}</div>}
      </div>

      <div className="section-head">
        <span className="lab">Manuscript Library</span>
        <span className="lab">{ctx.papers.length} papers</span>
      </div>

      <div className="reg-card">
        {ctx.papers.length ? (
          <table>
            <thead>
              <tr className="lab">
                <th>#</th>
                <th>Title (First line of PDF)</th>
                <th>Filename</th>
                <th>Pages</th>
                <th>Size</th>
                <th>Uploaded</th>
                <th style={{textAlign:'right'}}>Action</th>
              </tr>
            </thead>
            <tbody>
              {ctx.papers.map(p=>(
                <tr key={p.id} className="rw" onClick={()=>{ctx.setPid(p.id);go('/understand')}}>
                  <td className="m" style={{color:'var(--dv)',fontWeight:700}}>#{p.id}</td>
                  <td><b>{p.title}</b></td>
                  <td className="m" style={{color:'var(--t2)',fontSize:12}}>{p.filename}</td>
                  <td className="m">{p.pages}</td>
                  <td className="m">{(p.size/1024).toFixed(0)} KB</td>
                  <td className="m" style={{fontSize:12,color:'var(--t2)'}}>{p.created}</td>
                  <td style={{textAlign:'right'}}>
                    <span className="btn-sm-danger" onClick={e=>{e.stopPropagation();del(p.id)}}>Delete</span>
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

export function Understand({ctx}){
  const {data:p,err,loading}=useLoad(ctx.pid?'/papers/'+ctx.pid:null)
  const ai=useLoad(ctx.pid?'/ai/papers/'+ctx.pid+'/understand':null)
  const [bz,setBz]=useState(false)
  const a=p?.analysis
  return (
    <>
      <div className="page-header">
        <h2>Paper Understanding</h2>
        <p className="sub">Automated hyperparameter parsing and structural extraction from the active paper.</p>
      </div>

      <Pick ctx={ctx}/>
      <St loading={loading} err={err}/>

      {p && (
        <>
          <div className="card-panel" style={{marginBottom:24}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',flexWrap:'wrap',gap:12,marginBottom:16}}>
              <div>
                <h3 style={{margin:0,fontSize:18,fontWeight:700}}>AI Understanding Engine</h3>
                <p style={{margin:'4px 0 0',color:'var(--t2)',fontSize:13}}>Generates executive summaries and extracts critical experimental findings.</p>
              </div>
              <button
                className="btn"
                disabled={bz}
                onClick={async()=>{
                  setBz(true)
                  try{
                    await api('/ai/papers/'+ctx.pid+'/understand',{method:'POST'})
                    ai.reload()
                  }catch(e){alert(e.message)}
                  setBz(false)
                }}
              >
                {bz?'Analyzing Manuscript…':ai.data?'Re-run AI Analysis':'Run AI Analysis (Mock)'}
              </button>
            </div>

            {ai.data ? (
              <div className="ai-summary-box">
                <div style={{display:'flex',alignItems:'center',gap:10,marginBottom:12}}>
                  <Tag c="na">MOCK AI</Tag>
                  <span className="chip" style={{background:'#fff'}}>Confidence: {(ai.data.confidence*100).toFixed(0)}%</span>
                </div>
                <p style={{fontSize:14,lineHeight:1.6,margin:'0 0 16px',color:'var(--tx)'}}>{ai.data.summary}</p>
                <div className="lab" style={{marginBottom:8}}>Key Findings</div>
                <ul className="clean-list">
                  {ai.data.key_points.map(k=><li key={k}>{k}</li>)}
                </ul>
              </div>
            ) : (
              <div className="empty-box" style={{padding:'20px'}}>
                <div className="empty-text">Click "Run AI Analysis" to synthesize the paper's key hypotheses and claims.</div>
              </div>
            )}
          </div>

          <div className="section-head">
            <span className="lab">Detected Hyperparameters</span>
            <span className="lab">Regex parsing</span>
          </div>
          <div className="param-grid">
            {Object.entries(a.parameters).map(([k,v])=>(
              <div key={k} className="param-card">
                <span className="param-label">{k}</span>
                <span className="param-value">{v || <Tag c="wa">NOT SPECIFIED</Tag>}</span>
              </div>
            ))}
          </div>

          <div className="two" style={{marginTop:24}}>
            <div className="card-panel">
              <span className="lab" style={{display:'block',marginBottom:8}}>Sections Identified</span>
              <div style={{display:'flex',gap:6,flexWrap:'wrap'}}>
                {a.sections.length ? a.sections.map(s=><span key={s} className="chip">{s}</span>) : <span className="m" style={{color:'var(--t2)'}}>None detected</span>}
              </div>
            </div>
            <div className="card-panel">
              <span className="lab" style={{display:'block',marginBottom:8}}>Keyword Frequencies</span>
              <div style={{display:'flex',gap:6,flexWrap:'wrap'}}>
                {Object.entries(a.keywords).length ? Object.entries(a.keywords).map(([k,v])=><span key={k} className="chip">{k} <b style={{color:'var(--dv)',marginLeft:4}}>×{v}</b></span>) : <span className="m" style={{color:'var(--t2)'}}>None</span>}
              </div>
            </div>
          </div>

          <div className="section-head" style={{marginTop:28}}>
            <span className="lab">Extracted Manuscript Text</span>
            <span className="lab">{p.chars.toLocaleString()} Characters</span>
          </div>
          {p.text.trim() ? (
            <pre className="txt">{p.text}</pre>
          ) : (
            <Empty t="No text could be extracted from this PDF (scanned or PyMuPDF missing)."/>
          )}
        </>
      )}
    </>
  )
}

export function Artifacts({ctx}){
  const {data,err,loading,reload}=useLoad(ctx.pid?'/items/artifacts?paper_id='+ctx.pid:null)
  const [m,setM]=useState('')
  const chk=async id=>{
    setM('')
    try{await api(`/items/artifacts/${id}/check`,{method:'POST'})}catch(e){setM(e.message)}
    reload()
  }
  return (
    <>
      <div className="page-header">
        <h2>Artifact Verification</h2>
        <p className="sub">Register and verify the availability of code repositories, datasets, and pretrained checkpoints.</p>
      </div>

      <Pick ctx={ctx}/>

      {ctx.pid && (
        <>
          <div className="info-banner">
            <span>ℹ️</span> Status remains "Not checked" until verified. The automated check verifies whether external URLs respond to HTTP requests.
          </div>

          <div className="section-head">
            <span className="lab">Register New Research Artifact</span>
          </div>
          <Form
            btn="Register Artifact"
            init={{kind:'repository'}}
            fields={[['kind','Artifact Type',['repository','dataset','dependency']],['url','Artifact URL or Name'],['note','Description or Note']]}
            onSubmit={async v=>{await post('artifacts',{...v,paper_id:ctx.pid});reload()}}
          />

          <St loading={loading} err={err}/>
          {m && <div className="err">{m}</div>}

          <div className="section-head" style={{marginTop:28}}>
            <span className="lab">Verified Artifacts</span>
            <span className="lab">{data?.length || 0} recorded</span>
          </div>

          <div className="reg-card">
            {data?.length ? (
              <table>
                <thead>
                  <tr className="lab">
                    <th>Type</th>
                    <th>Artifact URL / Identifier</th>
                    <th>Description / Notes</th>
                    <th>Availability Status</th>
                    <th style={{textAlign:'right'}}>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map(a=>(
                    <tr key={a.id}>
                      <td><span className="chip" style={{textTransform:'capitalize'}}>{a.kind}</span></td>
                      <td className="m" style={{fontWeight:600}}>{a.url}</td>
                      <td style={{color:'var(--t2)',fontSize:13}}>{a.note || '—'}</td>
                      <td>
                        <Tag c={a.status.startsWith('Reach')?'ok':a.status.startsWith('Unreach')?'no':'na'}>
                          {a.status}
                        </Tag>
                      </td>
                      <td style={{textAlign:'right'}}>
                        <button className="btn-sm" onClick={()=>chk(a.id)}>Check URL</button>
                        <span style={{margin:'0 6px',color:'var(--bd)'}}>|</span>
                        <span className="btn-sm-danger" onClick={async()=>{await api('/items/artifacts/'+a.id,{method:'DELETE'});reload()}}>Delete</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : !loading && <Empty t="No artifacts recorded for this paper yet. Use the form above to register code or data."/>}
          </div>
        </>
      )}
    </>
  )
}

export function Claims({ctx}){
  const {tol,setT}=ctx
  const {data,err,loading,reload}=useLoad(ctx.pid?'/items/claims?paper_id='+ctx.pid:null)
  const [m,setM]=useState('')
  return (
    <>
      <div className="page-header">
        <h2>Claim Verification</h2>
        <p className="sub">Compare reported findings against reproduced empirical results within defined tolerance bounds.</p>
      </div>

      <Pick ctx={ctx}/>

      {ctx.pid && (
        <>
          <div className="card-panel" style={{marginBottom:24}}>
            <div style={{display:'flex',justifyContent:'space-between',alignItems:'center',flexWrap:'wrap',gap:16}}>
              <div>
                <span className="lab" style={{display:'block',marginBottom:4}}>Comparison Tolerance Threshold</span>
                <span style={{fontSize:13,color:'var(--t2)'}}>Tolerance determines whether reproduced results match reported claims.</span>
              </div>
              <div style={{display:'flex',alignItems:'center',gap:14}}>
                <span className="chip" style={{fontSize:13,fontWeight:700,background:'#fff',color:'var(--dv)'}}>
                  ±{(tol*100).toFixed(1)} pts
                </span>
                <input
                  type="range"
                  min="0"
                  max="0.05"
                  step="0.001"
                  value={tol}
                  onChange={e=>setT(+e.target.value)}
                  style={{cursor:'pointer',width:150}}
                />
              </div>
            </div>
          </div>

          <div className="section-head">
            <span className="lab">Record a Scientific Claim</span>
            <button
              className="btn g"
              style={{fontSize:12,padding:'6px 12px'}}
              onClick={async()=>{
                try{
                  const r=await api('/ai/papers/'+ctx.pid+'/extract-claims',{method:'POST'})
                  setM(`${r.added} claim(s) extracted by mock engine.`)
                  reload()
                }catch(e){setM('Error: '+e.message)}
              }}
            >
              ✨ Extract Claims (Mock AI)
            </button>
          </div>

          <Form
            btn="Add Research Claim"
            fields={[['statement','Published Scientific Claim'],['reported','Reported Metric Value (0.00 – 1.00)'],['reproduced','Reproduced Metric Value (Optional)']]}
            onSubmit={async v=>{await post('claims',{...v,paper_id:ctx.pid});reload()}}
          />

          <St loading={loading} err={err}/>
          {m && <div className="drop-status success" style={{marginBottom:16}}>{m}</div>}

          <div className="section-head" style={{marginTop:28}}>
            <span className="lab">Claims & Reproduction Matrix</span>
            <span className="lab">{data?.length || 0} claims</span>
          </div>

          {data?.length ? (
            <>
              <div className="reg-card">
                <table>
                  <thead>
                    <tr className="lab">
                      <th>ID</th>
                      <th>Scientific Claim</th>
                      <th>Reported</th>
                      <th>Reproduced</th>
                      <th>Diff (pts)</th>
                      <th>Status</th>
                      <th style={{textAlign:'right'}}>Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.map(c=>{
                      const s=cstat(c.reported,c.reproduced,tol)
                      const r=c.reproduced
                      return (
                        <tr key={c.id}>
                          <td className="m" style={{color:'var(--dv)',fontWeight:700}}>C{c.id}</td>
                          <td>
                            <div style={{fontWeight:600}}>{c.statement}</div>
                            <Mk x={c}/>
                          </td>
                          <td className="m">{pct(c.reported)}</td>
                          <td className="m">{pct(r)}</td>
                          <td className="m" style={{fontWeight:600}}>
                            {r==null?'—':((r-c.reported)*100).toFixed(2)+' pts'}
                          </td>
                          <td><Tag c={s[0]}>{s[1]}</Tag></td>
                          <td style={{textAlign:'right'}}>
                            <button
                              className="btn-sm"
                              onClick={async()=>{
                                try{
                                  await api('/ai/claims/'+c.id+'/reproduce',{method:'POST'})
                                  reload()
                                }catch(e){setM('Error: '+e.message)}
                              }}
                            >
                              Reproduce (Mock)
                            </button>
                            <span style={{margin:'0 6px',color:'var(--bd)'}}>|</span>
                            <span className="btn-sm-danger" onClick={async()=>{await api('/items/claims/'+c.id,{method:'DELETE'});reload()}}>Delete</span>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              <div className="card-panel" style={{marginTop:20}}>
                <span className="lab" style={{display:'block',marginBottom:12}}>Visual Metrics Comparison</span>
                {data.map(c=>(
                  <div key={c.id} style={{marginBottom:14}}>
                    <div style={{display:'flex',justifyContent:'space-between',fontSize:12,marginBottom:4}}>
                      <span className="m"><b>C{c.id}</b> · {c.statement}</span>
                      <span className="m" style={{color:'var(--t2)'}}>Rep: {pct(c.reported)} | Sim: {pct(c.reproduced)}</span>
                    </div>
                    <div className="bar2"><i style={{width:(c.reported*100)+'%',background:'var(--v)'}}/></div>
                    <div className="bar2"><i style={{width:((c.reproduced||0)*100)+'%',background:'#D98E64'}}/></div>
                  </div>
                ))}
                <p style={{color:'var(--t2)',fontSize:12,margin:'12px 0 0'}}>
                  <span style={{color:'var(--v)',fontWeight:600}}>Violet bar:</span> Reported metric. <span style={{color:'#D98E64',fontWeight:600}}>Peach bar:</span> Reproduced metric.
                </p>
              </div>
            </>
          ) : !loading && <Empty t="No claims recorded yet for this manuscript."/>}
        </>
      )}
    </>
  )
}

export function Lab({ctx}){
  const {data,reload}=useLoad(ctx.pid?'/items/versions?paper_id='+ctx.pid:null)
  const [bz,setBz]=useState(null)
  return (
    <>
      <div className="page-header">
        <h2>Experiment Lab</h2>
        <p className="sub">Iterate on model configurations and execute reproduction runs to capture loss curves and target metrics.</p>
      </div>

      <Pick ctx={ctx}/>

      {ctx.pid && (
        <>
          <div className="info-banner">
            <span>⚗️</span> Save configurations as versions to run simulations. The mock execution simulates training steps and outputs convergence curves.
          </div>

          <div className="section-head">
            <span className="lab">Configure Experiment Version</span>
          </div>

          <Form
            btn="Save Experiment Version"
            init={{name:'v'+((data?.length||0)+1)}}
            fields={[
              ['name','Version Identifier'],
              ['model','Architecture Model'],
              ['dataset','Target Dataset'],
              ['learning_rate','Learning Rate'],
              ['batch_size','Batch Size'],
              ['epochs','Training Epochs'],
              ['seed','Random Seed']
            ]}
            onSubmit={async v=>{
              const {name,...config}=v
              await post('versions',{name,config,paper_id:ctx.pid})
              reload()
            }}
          />

          <div className="section-head" style={{marginTop:28}}>
            <span className="lab">Experiment Execution Registry</span>
            <span className="lab">{data?.length || 0} versions</span>
          </div>

          <div className="reg-card">
            {data?.length ? (
              <table>
                <thead>
                  <tr className="lab">
                    <th>Version</th>
                    <th>Configuration Parameters</th>
                    <th>Timestamp</th>
                    <th>Score</th>
                    <th>Training Curve</th>
                    <th style={{textAlign:'right'}}>Run Experiment</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map(v=>(
                    <tr key={v.id}>
                      <td><b>{v.name}</b></td>
                      <td className="m" style={{fontSize:12,color:'var(--t2)'}}>
                        {Object.entries(v.config).filter(([,x])=>x!=='').map(([k,x])=>k+'='+x).join(' · ')}
                      </td>
                      <td className="m" style={{fontSize:12}}>{v.created}</td>
                      <td className="m" style={{fontWeight:700}}>
                        {pct(v.metric)} <Mk x={v.run}/>
                        {v.run && <div style={{fontSize:10,color:'var(--t2)'}}>{v.run.runtime_s}s simulated</div>}
                      </td>
                      <td>{v.run ? <Spark v={v.run.curve}/> : <span style={{color:'var(--t2)'}}>—</span>}</td>
                      <td style={{textAlign:'right'}}>
                        <button
                          className="btn-sm"
                          disabled={bz===v.id}
                          onClick={async()=>{
                            setBz(v.id)
                            try{
                              await api('/ai/items/versions/'+v.id+'/run',{method:'POST'})
                              reload()
                            }catch(e){alert(e.message)}
                            setBz(null)
                          }}
                        >
                          {bz===v.id ? 'Simulating…' : 'Run (Mock)'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : <Empty t="No versions configured yet. Create a version using the form above."/>}
          </div>
        </>
      )}
    </>
  )
}

const TY=['Paper','Dataset','Preprocessing','Model','Training','Evaluation','Results','Claim']
const COL={Paper:'#F1F5F9',Dataset:'#DCFCE7',Preprocessing:'#FFFFFF',Model:'#DBEAFE',Training:'#FEF3C7',Evaluation:'#DCFCE7',Results:'#F1F5F9',Claim:'#FEF3C7'}

export function Graph({ctx}){
  const {data,loading,err}=useLoad(ctx.pid?'/graph/'+ctx.pid:null)
  const [nodes,setN]=useState(null)
  const [sel,setSel]=useState(0)
  const [m,setM]=useState('')
  const ns=nodes||data?.nodes||TY.map((t,i)=>({id:i+1,type:t,detail:'',status:'not configured'}))
  const nd=ns[sel]||ns[0]
  const X=i=>30+i*150
  const upd=p=>setN(ns.map((n,i)=>i===sel?{...n,...p}:n))
  const save=async()=>{
    try{
      await api('/graph/'+ctx.pid,{method:'PUT',body:{nodes:ns}})
      setM('Graph configuration saved successfully.')
    }catch(e){setM('Error: '+e.message)}
  }
  return (
    <>
      <div className="page-header">
        <h2>Experiment Graph</h2>
        <p className="sub">Inspect pipeline lineage from manuscript claims to data preprocessing, architecture, and evaluation metrics.</p>
      </div>

      <Pick ctx={ctx}/>

      {ctx.pid && (
        <>
          <St loading={loading} err={err}/>

          <div className="two" style={{marginTop:16}}>
            <div className="card-panel" style={{padding:0,overflowX:'auto',backgroundImage:'radial-gradient(var(--bd) 1px, transparent 1px)',backgroundSize:'20px 20px'}}>
              <svg viewBox={`0 0 ${X(ns.length)+40} 150`} width={X(ns.length)+40} height="150" style={{display:'block'}}>
                {ns.slice(1).map((n,i)=>(
                  <path key={n.id} d={`M${X(i)+120} 60 L${X(i+1)} 60`} stroke="var(--v)" strokeWidth="2" strokeDasharray="4 2"/>
                ))}
                {ns.map((n,i)=>(
                  <g key={n.id} style={{cursor:'pointer'}} onClick={()=>setSel(i)} transform={`translate(${X(i)},28)`}>
                    <rect
                      width="120"
                      height="64"
                      rx="10"
                      fill={COL[n.type]||'#fff'}
                      stroke={i===sel?'var(--dv)':'var(--bd)'}
                      strokeWidth={i===sel?2.5:1}
                      filter="drop-shadow(0 2px 4px rgba(0,0,0,0.05))"
                    />
                    <text x="12" y="24" fontWeight="700" fontSize="13" fill="var(--tx)">{n.type}</text>
                    <text x="12" y="44" fontSize="10" fill="var(--t2)">{(n.detail||'NOT SPECIFIED').slice(0,18)}</text>
                  </g>
                ))}
              </svg>
            </div>

            <div className="card-panel">
              {nd && (
                <>
                  <div className="lab" style={{marginBottom:10}}>Selected Node: <b style={{color:'var(--dv)'}}>{nd.type}</b></div>
                  <div className="frm-field" style={{marginBottom:12}}>
                    <label className="frm-label">Component Detail</label>
                    <input className="frm-input" value={nd.detail} placeholder="Specify parameters..." onChange={e=>upd({detail:e.target.value})}/>
                  </div>
                  <div className="frm-field" style={{marginBottom:16}}>
                    <label className="frm-label">Lineage Status</label>
                    <select className="frm-input" value={nd.status} onChange={e=>upd({status:e.target.value})}>
                      {['not configured','configured','needs review'].map(s=><option key={s}>{s}</option>)}
                    </select>
                  </div>
                  <div style={{display:'flex',gap:10,alignItems:'center',marginBottom:16}}>
                    <span
                      className="btn-sm-danger"
                      onClick={()=>{
                        if(ns.length>1&&confirm('Remove this node?')){
                          setN(ns.filter((_,i)=>i!==sel))
                          setSel(0)
                        }
                      }}
                    >
                      Delete Node
                    </span>
                  </div>
                </>
              )}

              <hr style={{borderColor:'var(--bd)',margin:'16px 0'}}/>

              <div className="frm-field" style={{marginBottom:14}}>
                <label className="frm-label">Add Pipeline Node</label>
                <select className="frm-input" onChange={e=>{if(e.target.value){setN([...ns,{id:Date.now(),type:e.target.value,detail:'',status:'not configured'}]);setSel(ns.length);e.target.value=''}}}>
                  <option value="">Choose Component Type…</option>
                  {TY.map(t=><option key={t}>{t}</option>)}
                </select>
              </div>

              <div style={{display:'flex',gap:10}}>
                <button className="btn" onClick={save}>Save Graph</button>
                <button className="btn g" onClick={()=>{setN(TY.map((t,i)=>({id:i+1,type:t,detail:'',status:'not configured'})));setSel(0)}}>Reset</button>
              </div>
              {m && <p className="m" style={{marginTop:10,fontSize:12}}>{m}</p>}
            </div>
          </div>
        </>
      )}
    </>
  )
}

export function Versions({ctx}){
  const {data,err,loading,reload}=useLoad(ctx.pid?'/items/versions?paper_id='+ctx.pid:null)
  const [a,setA]=useState(0),[b,setB]=useState(1)
  const A=data?.[a],B=data?.[b]
  const keys=A&&B?[...new Set([...Object.keys(A.config),...Object.keys(B.config)])]:[]
  const ch=keys.filter(k=>(A.config[k]??'')!==(B.config[k]??''))
  const sel=(v,f)=>(
    <select className="frm-input" style={{width:'auto',display:'inline-block'}} value={v} onChange={e=>f(+e.target.value)}>
      {data.map((x,i)=><option key={x.id} value={i}>{x.name}</option>)}
    </select>
  )
  return (
    <>
      <div className="page-header">
        <h2>Version Comparison</h2>
        <p className="sub">Diff experimental configurations side-by-side to attribute metric variance to specific variables.</p>
      </div>

      <Pick ctx={ctx}/>
      <St loading={loading} err={err}/>

      {data && (
        data.length < 2 ? (
          <Empty t="Save at least two experiment versions in Experiment Lab to enable side-by-side comparison."/>
        ) : (
          <>
            <div className="card-panel" style={{display:'flex',alignItems:'center',gap:16,marginBottom:20}}>
              <span className="lab">Select Versions:</span>
              {sel(a,setA)} <b style={{color:'var(--t2)'}}>vs</b> {sel(b,setB)}
            </div>

            <div className="reg-card">
              <table>
                <thead>
                  <tr className="lab">
                    <th>Configuration Field</th>
                    <th>{A.name}</th>
                    <th>{B.name}</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map(k=>(
                    <tr key={k}>
                      <td><b>{k}</b></td>
                      <td className="m">{A.config[k] || '—'}</td>
                      <td className="m">
                        {B.config[k] || '—'}{' '}
                        {ch.includes(k) && <Tag c="wa">CHANGED</Tag>}
                      </td>
                    </tr>
                  ))}
                  <tr style={{background:'#f8fafc'}}>
                    <td><b>Result Metric (Empirical)</b></td>
                    {[A,B].map(v=>(
                      <td key={v.id}>
                        <input
                          className="f"
                          key={v.id+'-'+v.metric}
                          defaultValue={v.metric??''}
                          placeholder="0.00"
                          onBlur={async e=>{
                            try{
                              await api('/items/versions/'+v.id,{method:'PATCH',body:{metric:e.target.value}})
                              reload()
                            }catch(x){alert(x.message)}
                          }}
                        />
                      </td>
                    ))}
                  </tr>
                </tbody>
              </table>
            </div>

            <div className="info-banner" style={{marginTop:18}}>
              <div>
                <b>{ch.length ? `${ch.length} field(s) changed: ${ch.join(', ')}.` : 'Configurations are identical.'}</b>
                {ch.length > 1 && ' Note: When multiple hyperparameters change simultaneously, variance cannot be isolated.'}
                {A.metric!=null && B.metric!=null && (
                  <div style={{marginTop:4,color:'var(--dv)',fontWeight:700}}>
                    Observed metric difference: {((B.metric-A.metric)*100).toFixed(2)} pts
                  </div>
                )}
              </div>
            </div>
          </>
        )
      )}
    </>
  )
}

export function Audit({ctx}){
  const {data,err,loading,reload}=useLoad(ctx.pid?'/items/findings?paper_id='+ctx.pid:null)
  const [sel,setSel]=useState(null)
  const [m,setM]=useState('')
  const f=data?.find(x=>x.id===sel)
  const ST=['Potential risk','Needs human review','Confirmed','Resolved','Not checked']
  return (
    <>
      <div className="page-header">
        <h2>Research Audit</h2>
        <p className="sub">Identify methodology omissions, potential data leakages, and non-reproducible artifacts.</p>
      </div>

      <Pick ctx={ctx}/>

      {ctx.pid && (
        <>
          <div style={{display:'flex',gap:12,flexWrap:'wrap',marginBottom:20}}>
            <button
              className="btn"
              onClick={async()=>{
                const r=await api(`/papers/${ctx.pid}/autocheck`,{method:'POST'})
                setM(`Regex check found ${r.checked} gap(s); duplicates are skipped.`)
                reload()
              }}
            >
              Run Missing-Parameter Check
            </button>
            <button
              className="btn g"
              onClick={async()=>{
                try{
                  const r=await api(`/ai/papers/${ctx.pid}/audit`,{method:'POST'})
                  setM(`Mock AI audit added ${r.added} finding(s).`)
                  reload()
                }catch(e){setM('Error: '+e.message)}
              }}
            >
              ✨ Run AI Audit (Mock)
            </button>
            {m && <span className="m" style={{display:'flex',alignItems:'center',fontSize:12}}>{m}</span>}
          </div>

          <div className="section-head">
            <span className="lab">Record Audit Finding</span>
          </div>

          <Form
            btn="Add Audit Finding"
            init={{severity:'Medium',status:'Potential risk'}}
            fields={[
              ['category','Finding Category'],
              ['description','Finding Description'],
              ['severity','Severity Level',['Low','Medium','High']],
              ['evidence','Supporting Evidence'],
              ['next','Recommended Next Step']
            ]}
            onSubmit={async v=>{await post('findings',{...v,paper_id:ctx.pid});reload()}}
          />

          <St loading={loading} err={err}/>

          <div className="two" style={{marginTop:24}}>
            <div className="reg-card">
              {data?.length ? (
                <table>
                  <thead>
                    <tr className="lab">
                      <th>ID</th>
                      <th>Category & Description</th>
                      <th>Severity</th>
                      <th>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.map(r=>(
                      <tr key={r.id} className="rw" onClick={()=>setSel(r.id)}>
                        <td className="m" style={{color:'var(--dv)',fontWeight:700}}>F{r.id}</td>
                        <td>
                          <b>{r.category}</b>
                          <div style={{fontSize:12,color:'var(--t2)'}}>{r.description.slice(0,60)}…</div>
                          <Mk x={r}/>
                        </td>
                        <td><Tag c={r.severity==='High'?'no':r.severity==='Medium'?'wa':'ok'}>{r.severity}</Tag></td>
                        <td><Tag c={r.status==='Resolved'?'ok':'na'}>{r.status}</Tag></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : !loading && <Empty t="No audit findings recorded yet."/>}
            </div>

            <div className="card-panel">
              {f ? (
                <>
                  <div style={{display:'flex',justifyContent:'space-between',alignItems:'start',marginBottom:12}}>
                    <h3 style={{margin:0,fontSize:18}}>F{f.id} · {f.category}</h3>
                    <Tag c={f.severity==='High'?'no':f.severity==='Medium'?'wa':'ok'}>{f.severity}</Tag>
                  </div>
                  <p style={{fontSize:14,color:'var(--tx)',lineHeight:1.6}}>{f.description}</p>
                  
                  <span className="lab" style={{display:'block',marginTop:14}}>Supporting Evidence</span>
                  <div style={{fontSize:13,color:'var(--t2)',marginTop:4}}>{f.evidence||'—'}</div>

                  <span className="lab" style={{display:'block',marginTop:14}}>Recommended Next Step</span>
                  <div style={{fontSize:13,color:'var(--t2)',marginTop:4}}>{f.next||'—'}</div>

                  <span className="lab" style={{display:'block',marginTop:16}}>Audit Status</span>
                  <select
                    className="frm-input"
                    style={{marginTop:6}}
                    value={f.status}
                    onChange={async e=>{
                      await api('/items/findings/'+f.id,{method:'PATCH',body:{status:e.target.value}})
                      reload()
                    }}
                  >
                    {ST.map(s=><option key={s}>{s}</option>)}
                  </select>

                  <div style={{marginTop:16}}>
                    <span className="btn-sm-danger" onClick={async()=>{await api('/items/findings/'+f.id,{method:'DELETE'});setSel(null);reload()}}>
                      Delete Finding
                    </span>
                  </div>
                </>
              ) : <Empty t="Select an audit finding on the left to inspect details."/>}
            </div>
          </div>
        </>
      )}
    </>
  )
}

export function Reports({ctx}){
  const [md,setMd]=useState(''),[err,setErr]=useState('')
  const gen=async()=>{
    setErr('')
    try{setMd(await api(`/papers/${ctx.pid}/report`,{text:true}))}catch(e){setErr(e.message)}
  }
  const dl=()=>{
    const a=document.createElement('a')
    a.href=URL.createObjectURL(new Blob([md],{type:'text/markdown'}))
    a.download=`crisisgrid-report-${ctx.pid}.md`
    a.click()
  }
  return (
    <>
      <div className="page-header">
        <h2>Evidence Reports</h2>
        <p className="sub">Export comprehensive markdown audit dossiers documenting methodology gaps, claim reproducibility, and artifacts.</p>
      </div>

      <Pick ctx={ctx}/>

      {ctx.pid && (
        <div style={{display:'flex',gap:12,margin:'16px 0'}}>
          <button className="btn" onClick={gen}>Generate Evidence Dossier</button>
          {md && <button className="btn g" onClick={dl}>Download Markdown (.md)</button>}
        </div>
      )}

      {err && <div className="err">{err}</div>}
      {md && <pre className="txt" style={{maxHeight:'none'}}>{md}</pre>}
    </>
  )
}

export function History(){
  const {data,err,loading}=useLoad('/history')
  return (
    <>
      <div className="page-header">
        <h2>Experiment History</h2>
        <p className="sub">Immutable audit log of all paper uploads, AI reproductions, and parameter updates.</p>
      </div>

      <St loading={loading} err={err}/>

      <div className="reg-card">
        {data?.length ? (
          <table>
            <thead>
              <tr className="lab">
                <th>Timestamp</th>
                <th>Operation Event</th>
                <th>Details</th>
                <th>Paper ID</th>
              </tr>
            </thead>
            <tbody>
              {data.map(h=>(
                <tr key={h.id}>
                  <td className="m" style={{fontSize:12,color:'var(--t2)'}}>{h.ts}</td>
                  <td><b>{h.type}</b></td>
                  <td className="m" style={{fontSize:12}}>{h.detail}</td>
                  <td className="m" style={{color:'var(--dv)',fontWeight:700}}>{h.paper_id ? '#'+h.paper_id : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : !loading && <Empty t="No activity recorded yet. Upload a paper to start."/>}
      </div>
    </>
  )
}

export function Settings({ctx}){
  return (
    <>
      <div className="page-header">
        <h2>System Settings</h2>
        <p className="sub">Local environment configurations and reproduction parameters.</p>
      </div>

      <div className="card-panel" style={{maxWidth:600}}>
        <div className="lab" style={{marginBottom:8}}>Claim Comparison Tolerance</div>
        <p style={{fontSize:13,color:'var(--t2)',marginBottom:16}}>
          Current threshold: <b>±{(ctx.tol*100).toFixed(1)} pts</b>. Claims within this tolerance will be evaluated as matching.
        </p>
        <input
          type="range"
          min="0"
          max="0.05"
          step="0.001"
          value={ctx.tol}
          onChange={e=>ctx.setT(+e.target.value)}
          style={{width:'100%',cursor:'pointer'}}
        />
        <hr style={{borderColor:'var(--bd)',margin:'24px 0'}}/>
        <div style={{fontSize:12,color:'var(--t2)'}}>
          Configuration saved automatically in browser <code>localStorage</code>. Backend database: SQLite (local).
        </div>
      </div>
    </>
  )
}
