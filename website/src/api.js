import {useState,useEffect} from 'react'
export async function api(path,o={}){const r=await fetch('/api'+path,o.body instanceof FormData?o:{headers:{'Content-Type':'application/json'},...o,body:o.body===undefined?undefined:JSON.stringify(o.body)})
 if(!r.ok){let m=r.statusText;try{const j=await r.json();m=typeof j.detail==='string'?j.detail:JSON.stringify(j.detail)}catch{}const e=new Error(m);e.status=r.status;throw e}
 return o.text?r.text():r.json()}
export function useLoad(path){const [s,setS]=useState({data:null,err:null,loading:!!path}),[n,setN]=useState(0)
 useEffect(()=>{if(!path)return;let ok=true,tm;setS(x=>({...x,loading:true}));api(path).then(d=>ok&&setS({data:d,err:null,loading:false})).catch(e=>{if(!ok)return;setS({data:null,err:e.status>=500||e.status==null?'API unreachable ('+e.message+')':e.message,loading:false});if(e.status==null||e.status>=500)tm=setTimeout(()=>setN(v=>v+1),3000)});return()=>{ok=false;clearTimeout(tm)}},[path,n]);return {...s,reload:()=>setN(v=>v+1)}}
