import {useState,useEffect} from 'react'
import {NavLink,Link,Routes,Route,Navigate,useLocation} from 'react-router-dom'
import {NAV} from './data'
import {useLoad} from './api'
import * as Pg from './pages'

export default function Dashboard(){
  const [tol,setTol]=useState(()=>+localStorage.getItem('cg.tol')||0.01)
  const [pid,setPid]=useState(null)
  const [sidebarOpen,setSidebarOpen]=useState(()=>typeof window !== 'undefined' ? window.innerWidth > 900 : true)
  
  const setT=v=>{setTol(v);try{localStorage.setItem('cg.tol',v)}catch{}}
  
  const pl=useLoad('/papers')
  const papers=pl.data||[]
  useEffect(()=>{
    if(papers.length&&!papers.some(p=>p.id===pid))setPid(papers[0].id)
    if(!papers.length&&pid)setPid(null)
  },[papers])
  
  const ctx={tol,setT,pid,setPid,papers,reloadPapers:pl.reload}
  const loc=useLocation().pathname.slice(1)||'mission'
  const cur=NAV.flatMap(g=>g[1]).find(x=>x[0]===loc)
  const R={mission:Pg.Mission,papers:Pg.Papers,understand:Pg.Understand,artifact:Pg.Artifacts,claims:Pg.Claims,lab:Pg.Lab,graph:Pg.Graph,versions:Pg.Versions,audit:Pg.Audit,reports:Pg.Reports,history:Pg.History,settings:Pg.Settings}

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
          LOCAL ENVIRONMENT<br/>
          FASTAPI + SQLITE<br/>
          AI ENGINE: MOCK (swap later)
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
          <span className="chip"><i className="dot" style={{background:'#C98B2B'}}/>Local mode</span>
          <span className="chip">{papers.length} papers · {pl.err?'API offline':'API online'}</span>
          <span className="chip">No experiments executed</span>
        </div>
        {pl.err && (
          <div className="err" style={{margin:'0 0 16px 0'}}>
            Backend not reachable on port 8000. Start everything with <code>npm run dev</code> — retrying automatically…
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
