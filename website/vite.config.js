import {defineConfig} from 'vite';import react from '@vitejs/plugin-react'
// /api -> the website's Node backend (mock AI features); /cg -> the CrisisGrid FastAPI, which also serves the notebook
// environment under /cg/paper-env (REST, WebSockets for events + terminal, and the built UI).
const API=process.env.CG_NODE_API||'http://127.0.0.1:8000', CG=process.env.CG_API||'http://127.0.0.1:8100'
export default defineConfig({plugins:[react()],server:{proxy:{
  '/api':API,
  '/cg':{target:CG,changeOrigin:true,ws:true,rewrite:p=>p.replace(/^\/cg/,'')},
}}})
