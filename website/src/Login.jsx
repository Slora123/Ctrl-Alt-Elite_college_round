import {useState} from 'react'
import {Navigate,useNavigate,useSearchParams,Link} from 'react-router-dom'
import {useAuth} from './auth'

export default function Login(){
  const {cfg,authed,ready,login,register}=useAuth()
  const [sp]=useSearchParams()
  const nav=useNavigate()
  const next=sp.get('next')&&sp.get('next').startsWith('/')?sp.get('next'):'/mission'
  const [mode,setMode]=useState('in')
  const [f,setF]=useState({email:'',name:'',password:''})
  const [err,setErr]=useState('')
  const [busy,setBusy]=useState(false)
  if(ready&&authed)return <Navigate to={next} replace/>
  const first=cfg?.first_user_becomes_admin
  const canRegister=cfg?.registration_open
  const submit=async e=>{
    e.preventDefault();setErr('');setBusy(true)
    try{
      if(mode==='in')await login(f.email,f.password)
      else await register(f.email,f.name,f.password)
      nav(next,{replace:true})
    }catch(x){setErr(x.message)}
    setBusy(false)
  }
  return (
    <div className="login-wrap">
      <form className="card-panel login-card" onSubmit={submit}>
        <Link to="/" className="brand-text" style={{display:'block',marginBottom:18}}><b>CRISISGRID</b><span>RESEARCH INTELLIGENCE</span></Link>
        <h2 style={{margin:'0 0 4px'}}>{mode==='in'?'Sign in':(first?'Create the administrator account':'Create an account')}</h2>
        <p className="sub" style={{marginTop:0}}>{mode==='in'?'One login for the dashboard, the pipeline and the notebook.':(first?'No accounts exist yet. The first account administers this installation.':'Passwords need at least 10 characters.')}</p>
        {cfg?.unreachable&&<div className="err">Cannot reach the CrisisGrid API. Is it running?</div>}
        {mode==='up'&&<div className="frm-field"><label className="frm-label">Name</label><input className="frm-input" value={f.name} onChange={e=>setF({...f,name:e.target.value})} autoComplete="name"/></div>}
        <div className="frm-field"><label className="frm-label">Email</label><input className="frm-input" type="email" required value={f.email} onChange={e=>setF({...f,email:e.target.value})} autoComplete="username"/></div>
        <div className="frm-field"><label className="frm-label">Password</label><input className="frm-input" type="password" required minLength={mode==='up'?10:1} value={f.password} onChange={e=>setF({...f,password:e.target.value})} autoComplete={mode==='in'?'current-password':'new-password'}/></div>
        {err&&<div className="err" style={{margin:'12px 0'}}>{err}</div>}
        <button className="btn" disabled={busy} style={{width:'100%',marginTop:8}}>{busy?'Please wait…':mode==='in'?'Sign in':'Create account'}</button>
        {(canRegister||mode==='up')&&<p style={{fontSize:13,marginTop:14,textAlign:'center'}}>
          {mode==='in'?<>No account? <a href="#" onClick={e=>{e.preventDefault();setMode('up');setErr('')}}>Create one</a></>:<>Have an account? <a href="#" onClick={e=>{e.preventDefault();setMode('in');setErr('')}}>Sign in</a></>}
        </p>}
        {!canRegister&&mode==='in'&&<p style={{fontSize:12,color:'var(--t2)',textAlign:'center'}}>Registration is closed. Ask an administrator for an account.</p>}
      </form>
    </div>
  )
}
