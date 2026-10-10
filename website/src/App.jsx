import {useEffect} from 'react'
import {Routes,Route,Navigate,useLocation} from 'react-router-dom'
import Landing from './Landing'
import Dashboard from './Dashboard'
import Login from './Login'
import {useAuth} from './auth'

// "/" is the landing page, "/login" signs in, everything else (/mission, /papers, ...) is the dashboard behind the login.
function Guard({children}){
  const {authed,ready}=useAuth()
  const loc=useLocation()
  if(!ready)return <div className="status-loading" style={{margin:40}}><span className="spinner"/> Loading…</div>
  if(!authed)return <Navigate to={'/login?next='+encodeURIComponent(loc.pathname+loc.search)} replace/>
  return children
}

export default function App(){
  const {pathname}=useLocation()
  useEffect(()=>{window.scrollTo(0,0)},[pathname])
  return (
    <Routes>
      <Route path="/" element={<Landing/>}/>
      <Route path="/login" element={<Login/>}/>
      <Route path="/*" element={<Guard><Dashboard/></Guard>}/>
    </Routes>
  )
}
