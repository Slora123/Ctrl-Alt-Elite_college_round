import {useState,useEffect} from 'react'
import {NavLink,Link,Routes,Route,Navigate,useLocation} from 'react-router-dom'
import {NAV} from './data'
import {useCg} from './cgload'
import * as Pg from './real'
import * as Pg2 from './pages2'
import {useAuth} from './auth'

export default function Dashboard(){
  const auth=useAuth()
  const [pid,setPid]=useState(()=>{try{return localStorage.getItem('cg.pid')||null}catch{return null}})
  const [sidebarOpen,setSidebarOpen]=useState(()=>typeof window !== 'undefined' ? window.innerWidth > 900 : true)
  
  useEffect(()=>{try{pid?localStorage.setItem('cg.pid',pid):localStorage.removeItem('cg.pid')}catch{}},[pid])
  
  // the real backend (FastAPI under /cg). Poll while an analysis is in flight so statuses update by themselves.
  const pl=useCg('/papers',{every:d=>(d||[]).some(p=>['running','waiting_jobs'].includes(p.latest_run?.status))?2000:0})
  const papers=pl.data||[]
  // refetch the overview whenever any paper's run starts, moves or changes status (the query parameter only changes the URL we watch)
  const stamp=papers.map(p=>p.latest_run?`${p.latest_run.run_id}.${p.latest_run.status}.${p.latest_run.node}`:'-').join(',')
  const ov=useCg('/overview?at='+encodeURIComponent(stamp),{every:d=>(d?.runs.running||d?.runs.waiting_jobs)?3000:0})
  useEffect(()=>{
    if(papers.length&&!papers.some(p=>p.id===pid))setPid(papers[0].id)
    if(!papers.length&&pid)setPid(null)
  },[papers])
  
  const ctx={pid,setPid,papers,reloadPapers:pl.reload,papersErr:pl.err,overview:ov.data,overviewErr:ov.err,reloadOverview:ov.reload}
  const loc=useLocation().pathname.slice(1)||'mission'
  const cur=NAV.flatMap(g=>g[1]).find(x=>x[0]===loc)
  const R={mission:Pg.Mission,papers:Pg.Papers,understand:Pg.Understand,artifact:Pg.Artifacts,claims:Pg.Claims,lab:Pg.Lab,graph:Pg.Graph,versions:Pg.Versions,audit:Pg.Audit,reports:Pg.Reports,history:Pg.History,settings:Pg.Settings,pipeline:Pg2.Pipeline,notebook:Pg2.Notebook,account:Pg2.Account}

  return (
    <div id="app" className={sidebarOpen ? 'sidebar-open' : 'sidebar-closed'}>
      {sidebarOpen && <div className="sidebar-backdrop" onClick={()=>setSidebarOpen(false)}/>}
      <aside className={sidebarOpen ? 'open' : 'collapsed'}>
        <div className="brand">
          <Link to="/" className="brand-text" style={{display:'block'}} title="Back to intro">
            <b>CRISISGRID</b>
            <span>RESEARCH INTELLIGENCE</span>
          </Link>
          <button className="close-sidebar-btn" onClick={()=>setSidebarOpen(false)} aria-label="Close sidebar">✕</button>
        </div>
        <div className="nav-list">
          {NAV.map(([g,it])=>(
            <div key={g} className="nav-group">
              <div className="lab">{g}</div>
              {it.map(([k,n])=>(
                <NavLink
                  key={k}
                  to={'/'+k}
                  className={({isActive})=>'nv'+(isActive?' on':'')}
                  onClick={()=>{if(window.innerWidth<=900)setSidebarOpen(false)}}
                >
                  {n}
                </NavLink>
              ))}
            </div>
          ))}
        </div>
        <div className="demo">
          CRISISGRID BACKEND<br/>
          FASTAPI · LIVE DATA<br/>
          {ov.data?(ov.data.llm_configured?'AI ENGINE: LLM CONFIGURED':'AI ENGINE: NOT CONFIGURED'):'AI ENGINE: …'}
        </div>
      </aside>
      <main>
        <div className="bar">
          <button
            className="hamburger-btn"
            onClick={()=>setSidebarOpen(!sidebarOpen)}
            aria-label="Toggle sidebar"
            title={sidebarOpen ? "Collapse sidebar" : "Expand sidebar"}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round">
              <line x1="3" y1="12" x2="21" y2="12"></line>
              <line x1="3" y1="6" x2="21" y2="6"></line>
              <line x1="3" y1="18" x2="21" y2="18"></line>
            </svg>
          </button>
          <span className="bar-title">
            CrisisGrid <span className="bar-slash">/</span> <span className="bar-cur">{cur?.[1]}</span>
          </span>
          <span style={{flex:1}}/>
          {auth.cfg?.auth_enabled
            ? <span className="chip" title={auth.user?.email}><i className="dot" style={{background:'#2f9e44'}}/>{auth.user?.name} · {auth.user?.role}</span>
            : <span className="chip"><i className="dot" style={{background:'#C98B2B'}}/>Local mode (no login)</span>}
          {auth.cfg?.auth_enabled && <button className="btn-sm" onClick={auth.logout}>Sign out</button>}
          <span className="chip">{papers.length} paper{papers.length===1?'':'s'} · {pl.err&&!pl.data?'API offline':'API online'}</span>
          <span className="chip">{ov.data?(ov.data.stages.executed?`${ov.data.stages.executed} paper(s) with clean runs`:'No experiments executed'):'…'}</span>
        </div>
        {pl.err && (
          <div className="err" style={{margin:'0 0 16px 0'}}>
            The CrisisGrid API is not reachable. Start everything with <code>python scripts/run_all.py</code> — retrying automatically…
          </div>
        )}
        <Routes>
          {Object.entries(R).map(([k,C])=><Route key={k} path={'/'+k} element={<C ctx={ctx} k={k}/>}/>)}
          <Route path="*" element={<Navigate to="/mission" replace/>}/>
        </Routes>
      </main>
    </div>
  )
}
