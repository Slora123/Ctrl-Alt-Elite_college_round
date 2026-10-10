// One command to run everything:  npm run dev
// Starts the FastAPI backend (creating backend/.venv and installing deps on first run),
// waits until it answers, then starts Vite. Use `npm run dev:web` for the frontend only.
import {spawn,spawnSync} from 'node:child_process'
import {existsSync,writeFileSync} from 'node:fs'
import {join,dirname} from 'node:path'
import {fileURLToPath} from 'node:url'
import http from 'node:http'

const root=join(dirname(fileURLToPath(import.meta.url)),'..'),be=join(root,'backend'),win=process.platform==='win32'
const venv=join(be,'.venv'),py=join(venv,win?'Scripts':'bin',win?'python.exe':'python'),stamp=join(venv,'.cg-deps-ok')
const kids=[]
const die=(m,c=1)=>{console.error('\n[crisisgrid] '+m);kids.forEach(k=>k.kill());process.exit(c)}
const run=(cmd,args,opt={})=>spawnSync(cmd,args,{stdio:'inherit',cwd:be,...opt})
const health=()=>new Promise(r=>{const q=http.get('http://127.0.0.1:8000/api/health',x=>{x.resume();r(x.statusCode===200)});q.on('error',()=>r(false));q.setTimeout(1500,()=>{q.destroy();r(false)})})

async function api(){
  if(await health()){console.log('[crisisgrid] API already running on :8000');return}
  if(!existsSync(py)){
    const sys=(win?['py','python','python3']:['python3','python']).find(c=>spawnSync(c,['--version']).status===0)
    if(!sys)die('Python 3.10+ not found. Install it from python.org, then run again.')
    console.log('[crisisgrid] creating backend/.venv ...')
    if(run(sys,['-m','venv','.venv']).status!==0)die('Could not create the virtualenv.')
  }
  if(!existsSync(stamp)){
    console.log('[crisisgrid] installing backend dependencies (first run only) ...')
    if(run(py,['-m','pip','install','-q','fastapi','uvicorn','python-multipart']).status!==0)die('pip install failed. Check your internet connection.')
    // PDF text extraction is optional: the API still works without it.
    if(run(py,['-m','pip','install','-q','pymupdf']).status!==0)console.warn('[crisisgrid] pymupdf failed to install: uploads work but no text is extracted.')
    writeFileSync(stamp,'ok')
  }
  const p=spawn(py,['-m','uvicorn','main:app','--host','127.0.0.1','--port','8000'],{cwd:be,stdio:'inherit'});kids.push(p)
  p.on('exit',c=>{if(c)die('Backend crashed (see error above).',c)})
  for(let i=0;i<60;i++){if(await health()){console.log('[crisisgrid] API ready  http://127.0.0.1:8000/docs');return}await new Promise(r=>setTimeout(r,500))}
  die('Backend did not become ready within 30s.')
}

await api()
if(process.argv.includes('--api-only'))process.on('SIGINT',()=>die('stopped',0))
else{
  const v=spawn(process.execPath,[join(root,'node_modules','vite','bin','vite.js')],{cwd:root,stdio:'inherit'});kids.push(v)
  v.on('exit',c=>die('web server stopped',c||0))
}
process.on('SIGINT',()=>die('stopped',0));process.on('SIGTERM',()=>die('stopped',0))
