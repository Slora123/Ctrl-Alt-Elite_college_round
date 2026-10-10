// One login for the whole platform: the CrisisGrid API issues the token; the Node backend, the pipeline API and the
// notebook (paper_env, embedded under /cg/paper-env) all verify it. It lives in localStorage under "cg.token", which the
// embedded notebook UI reads too (same origin).
import {createContext,useContext,useEffect,useState,useCallback} from 'react'

const KEY='cg.token'
export const getToken=()=>{try{return localStorage.getItem(KEY)}catch{return null}}
const setTok=t=>{try{t?localStorage.setItem(KEY,t):localStorage.removeItem(KEY)}catch{/* storage blocked */}}
export const authHeaders=()=>{const t=getToken();return t?{Authorization:'Bearer '+t}:{}}

/** fetch against the CrisisGrid FastAPI (proxied under /cg), with the bearer token. */
export async function cg(path,o={}){
  const raw=o.raw
  const r=await fetch('/cg'+path,{...o,headers:{...(raw||o.body===undefined||o.body instanceof FormData?{}:{'Content-Type':'application/json'}),...authHeaders(),...(o.headers||{})},
    body:raw||o.body===undefined||o.body instanceof FormData?o.body:JSON.stringify(o.body)})
  if(r.status===401)window.dispatchEvent(new Event('cg:unauthorized'))
  if(!r.ok){let m=r.statusText;try{const j=await r.json();m=typeof j.detail==='string'?j.detail:JSON.stringify(j.detail)}catch{}const e=new Error(m);e.status=r.status;throw e}
  if(o.text)return r.text()
  return r.status===204?null:r.json()
}

const Ctx=createContext(null)
export const useAuth=()=>useContext(Ctx)

export function AuthProvider({children}){
  const [cfg,setCfg]=useState(null)           // {auth_enabled, registration_open, first_user_becomes_admin}
  const [token,setToken]=useState(getToken())
  const [user,setUser]=useState(null)
  const [ready,setReady]=useState(false)

  const logout=useCallback(()=>{setTok(null);setToken(null);setUser(null)},[])
  useEffect(()=>{
    let ok=true
    fetch('/cg/auth/config').then(r=>r.json()).then(c=>{if(ok)setCfg(c)}).catch(()=>{if(ok)setCfg({auth_enabled:true,registration_open:true,unreachable:true})})
    return()=>{ok=false}
  },[])
  useEffect(()=>{
    if(!cfg)return
    if(!cfg.auth_enabled){setUser({id:'local',name:'local',email:'local@localhost',role:'admin'});setReady(true);return}
    if(!token){setUser(null);setReady(true);return}
    cg('/auth/me').then(u=>{setUser(u);setReady(true)}).catch(()=>{logout();setReady(true)})
  },[cfg,token,logout])
  useEffect(()=>{const h=()=>logout();window.addEventListener('cg:unauthorized',h);return()=>window.removeEventListener('cg:unauthorized',h)},[logout])

  const finish=s=>{setTok(s.token);setToken(s.token);setUser(s.user);return s.user}
  const login=async(email,password)=>finish(await cg('/auth/login',{method:'POST',body:{email,password}}))
  const register=async(email,name,password)=>finish(await cg('/auth/register',{method:'POST',body:{email,name,password}}))
  const authed=cfg&&(!cfg.auth_enabled||!!user)
  return <Ctx.Provider value={{cfg,token,user,ready,authed,login,register,logout,setToken:t=>{setTok(t);setToken(t)}}}>{children}</Ctx.Provider>
}
