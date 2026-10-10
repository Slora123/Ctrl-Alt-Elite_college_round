import express from 'express';
import multer from 'multer';
import Database from 'better-sqlite3';
import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';
import crypto from 'crypto';
import { createRequire } from 'module';
const require = createRequire(import.meta.url);
const pdfParse = require('pdf-parse');
import * as ai from './ai_mock.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const BASE = __dirname;
const UP = path.join(BASE, 'uploads');
if (!fs.existsSync(UP)) {
  fs.mkdirSync(UP, { recursive: true });
}

const app = express();
app.use(express.json());

const upload = multer({ dest: UP, limits: { fileSize: 20 * 1024 * 1024 } });

function db() {
  const c = new Database(path.join(BASE, 'crisisgrid.db'));
  c.exec(`
    create table if not exists papers(id integer primary key,filename text,stored text,title text,size integer,pages integer,text text,analysis text,created text);
    create table if not exists items(id integer primary key,kind text,paper_id integer,data text,created text);
    create table if not exists history(id integer primary key,ts text,type text,detail text,paper_id integer);
    create table if not exists kv(k text primary key,v text);
  `);
  return c;
}

const now = () => {
  const pad = n => n.toString().padStart(2, '0');
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
};

function log(c, type, detail, pid = null) {
  c.prepare("insert into history(ts,type,detail,paper_id) values(?,?,?,?)").run(now(), type, detail, pid);
}

const SEC = ["abstract", "introduction", "related work", "method", "methodology", "dataset", "experiments", "results", "evaluation", "conclusion", "references"];
const PAR = {
  "learning rate": /learning[\s-]*rate[^0-9a-z]{0,12}([0-9.]+(?:e-?\d+)?)/,
  "batch size": /batch[\s-]*size[^0-9]{0,12}(\d+)/,
  "epochs": /(\d+)\s+epochs/,
  "random seed": /seed[^0-9]{0,12}(\d+)/,
  "optimizer": /\b(adamw|adam|sgd|rmsprop)\b/
};
const KW = ["accuracy", "f1", "precision", "recall", "auc", "transformer", "cnn", "resnet", "bert", "lstm", "dropout", "augmentation", "baseline"];

function analyze(text) {
  const low = (text || "").toLowerCase();
  const params = {};
  for (const [k, r] of Object.entries(PAR)) {
    const m = low.match(r);
    params[k] = m ? m[1] : null;
  }
  const sections = SEC.filter(s => new RegExp("^\\s*(\\d+\\.?\\s*)?" + s + "\\b", "m").test(low));
  const keywords = {};
  for (const k of KW) {
    const c = (low.match(new RegExp("\\b" + k + "\\b", "g")) || []).length;
    if (c > 0) keywords[k] = c;
  }
  return {
    method: "regex heuristics on extracted text (not AI)",
    sections,
    parameters: params,
    keywords
  };
}

app.get('/api/health', (req, res) => {
  res.json({ ok: true, ai_provider: ai.PROVIDER });
});

app.post('/api/papers', upload.single('file'), async (req, res) => {
  if (!req.file || !req.file.originalname.toLowerCase().endsWith('.pdf')) {
    return res.status(415).json({ detail: "Only .pdf files are accepted" });
  }
  
  const data = fs.readFileSync(req.file.path);
  if (!data.slice(0, 4).equals(Buffer.from("%PDF"))) {
    fs.unlinkSync(req.file.path);
    return res.status(415).json({ detail: "File is not a valid PDF" });
  }
  
  let text = "", pages = 0;
  let ext_ok = false;
  try {
    const parsed = await pdfParse(data);
    text = parsed.text;
    pages = parsed.numpages;
    ext_ok = true;
  } catch (e) {
    console.warn("PDF parsing error", e);
  }
  
  const stored = crypto.randomUUID().replace(/-/g, '') + '.pdf';
  fs.renameSync(req.file.path, path.join(UP, stored));
  
  const lines = text.split('\n').map(l => l.trim()).filter(l => l.length >= 10);
  const title = (lines[0] || req.file.originalname).substring(0, 140);
  
  const c = db();
  const info = c.prepare("insert into papers(filename,stored,title,size,pages,text,analysis,created) values(?,?,?,?,?,?,?,?)")
    .run(req.file.originalname, stored, title, data.length, pages, text, JSON.stringify(analyze(text)), now());
  
  const id = info.lastInsertRowid;
  log(c, "Paper uploaded", req.file.originalname, id);
  c.close();
  
  res.json({ id, title, pages, extracted: text.trim().length > 0, note: ext_ok ? null : "pdf-parse failed; no text extracted" });
});

function paperRow(r, full = false) {
  const d = { id: r.id, filename: r.filename, title: r.title, size: r.size, pages: r.pages, created: r.created };
  d.chars = (r.text || "").length;
  if (full) {
    d.text = (r.text || "").substring(0, 60000);
    d.analysis = JSON.parse(r.analysis || "{}");
  }
  return d;
}

app.get('/api/papers', (req, res) => {
  const c = db();
  const rows = c.prepare("select * from papers order by id desc").all();
  c.close();
  res.json(rows.map(r => paperRow(r)));
});

app.get('/api/papers/:pid', (req, res) => {
  const c = db();
  const r = c.prepare("select * from papers where id=?").get(req.params.pid);
  c.close();
  if (!r) return res.status(404).json({ detail: "Paper not found" });
  res.json(paperRow(r, true));
});

app.delete('/api/papers/:pid', (req, res) => {
  const c = db();
  const r = c.prepare("select stored from papers where id=?").get(req.params.pid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Paper not found" }); }
  try { fs.unlinkSync(path.join(UP, r.stored)); } catch (e) {}
  c.prepare("delete from papers where id=?").run(req.params.pid);
  c.prepare("delete from items where paper_id=?").run(req.params.pid);
  log(c, "Paper deleted", req.params.pid.toString());
  c.close();
  res.json({ ok: true });
});

const REQ = {
  artifacts: ["kind", "url"],
  claims: ["statement", "reported"],
  versions: ["name", "config"],
  findings: ["category", "description"]
};

function cleanItem(kind, d) {
  if (!REQ[kind]) throw { status: 404, msg: "Unknown resource" };
  if (typeof d !== 'object' || !d.paper_id) throw { status: 422, msg: "paper_id is required" };
  const miss = REQ[kind].filter(k => d[k] === null || d[k] === undefined || d[k] === "");
  if (miss.length > 0) throw { status: 422, msg: "Missing: " + miss.join(", ") };
  
  for (const k of ["reported", "reproduced", "metric"]) {
    if (k in d) {
      if (d[k] === null || d[k] === "") { d[k] = null; continue; }
      const f = parseFloat(d[k]);
      if (isNaN(f)) throw { status: 422, msg: `${k} must be a number` };
      d[k] = f;
    }
  }
  
  if (kind === "artifacts") {
    if (d.kind === "repository" && !/^https?:\/\/\S+$/.test(d.url)) throw { status: 422, msg: "Repository URL must start with http:// or https://" };
    d.status = d.status || "Not checked";
  }
  if (kind === "findings") {
    d.status = d.status || "Potential risk";
    d.severity = d.severity || "Medium";
  }
  if (kind === "versions") {
    const c = d.config;
    if (typeof c !== 'object') throw { status: 422, msg: "config must be an object" };
    for (const k of ["learning_rate", "batch_size", "epochs"]) {
      if (c[k] !== null && c[k] !== undefined && c[k] !== "") {
        const x = parseFloat(c[k]);
        if (isNaN(x)) throw { status: 422, msg: `${k} must be a number` };
        if (k === "learning_rate") {
          if (x <= 0 || x >= 1) throw { status: 422, msg: "learning_rate must be between 0 and 1" };
          c[k] = x;
        } else {
          if (x <= 0 || !Number.isInteger(x)) throw { status: 422, msg: `${k} must be a positive integer` };
          c[k] = x;
        }
      }
    }
  }
  return d;
}

function buildItem(r) {
  return { id: r.id, paper_id: r.paper_id, created: r.created, ...JSON.parse(r.data || "{}") };
}

function getItems(kind, paper_id = null) {
  if (!REQ[kind]) throw { status: 404, msg: "Unknown resource" };
  const c = db();
  let rows;
  if (paper_id) {
    rows = c.prepare("select * from items where kind=? and paper_id=? order by id").all(kind, paper_id);
  } else {
    rows = c.prepare("select * from items where kind=? order by id").all(kind);
  }
  c.close();
  return rows.map(buildItem);
}

app.get('/api/items/:kind', (req, res) => {
  try {
    const data = getItems(req.params.kind, req.query.paper_id ? parseInt(req.query.paper_id) : null);
    res.json(data);
  } catch (e) {
    res.status(e.status || 500).json({ detail: e.msg || e.message });
  }
});

app.post('/api/items/:kind', (req, res) => {
  try {
    const d = cleanItem(req.params.kind, req.body);
    const pid = parseInt(d.paper_id);
    delete d.paper_id;
    const c = db();
    if (!c.prepare("select 1 from papers where id=?").get(pid)) {
      c.close();
      return res.status(404).json({ detail: "Paper not found" });
    }
    const info = c.prepare("insert into items(kind,paper_id,data,created) values(?,?,?,?)").run(req.params.kind, pid, JSON.stringify(d), now());
    const id = info.lastInsertRowid;
    log(c, req.params.kind.replace(/s$/, '').replace(/^./, l => l.toUpperCase()) + " created", (d.name || d.statement || d.description || d.url || "").substring(0, 80), pid);
    c.close();
    res.json({ id });
  } catch (e) {
    res.status(e.status || 500).json({ detail: e.msg || e.message });
  }
});

app.patch('/api/items/:kind/:iid', (req, res) => {
  const { kind, iid } = req.params;
  const c = db();
  const r = c.prepare("select * from items where id=? and kind=?").get(iid, kind);
  if (!r) { c.close(); return res.status(404).json({ detail: "Not found" }); }
  try {
    let d = { ...JSON.parse(r.data), ...req.body, paper_id: r.paper_id };
    d = cleanItem(kind, d);
    delete d.paper_id;
    c.prepare("update items set data=? where id=?").run(JSON.stringify(d), iid);
    log(c, kind.replace(/s$/, '').replace(/^./, l => l.toUpperCase()) + " updated", "#" + iid + ": " + Object.keys(req.body).join(", "), r.paper_id);
    c.close();
    res.json({ ok: true });
  } catch (e) {
    c.close();
    res.status(e.status || 500).json({ detail: e.msg || e.message });
  }
});

app.delete('/api/items/:kind/:iid', (req, res) => {
  const c = db();
  c.prepare("delete from items where id=? and kind=?").run(req.params.iid, req.params.kind);
  log(c, req.params.kind.replace(/s$/, '').replace(/^./, l => l.toUpperCase()) + " deleted", "#" + req.params.iid);
  c.close();
  res.json({ ok: true });
});

app.post('/api/items/artifacts/:iid/check', async (req, res) => {
  const c = db();
  const r = c.prepare("select * from items where id=? and kind='artifacts'").get(req.params.iid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Not found" }); }
  const d = JSON.parse(r.data);
  if (!/^https?:\/\//.test(d.url)) {
    c.close();
    return res.status(422).json({ detail: "Only http(s) URLs can be checked" });
  }
  try {
    const fetchRes = await fetch(d.url, { method: 'HEAD', signal: AbortSignal.timeout(6000) });
    d.status = `Reachable (HTTP ${fetchRes.status}), checked ${now()}`;
  } catch (e) {
    d.status = `Unreachable: ${e.message.substring(0, 80)}, checked ${now()}`;
  }
  c.prepare("update items set data=? where id=?").run(JSON.stringify(d), req.params.iid);
  log(c, "Artifact URL checked", d.status.substring(0, 80), r.paper_id);
  c.close();
  res.json({ status: d.status });
});

app.post('/api/papers/:pid/autocheck', (req, res) => {
  const pid = parseInt(req.params.pid);
  const c = db();
  const r = c.prepare("select * from papers where id=?").get(pid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Paper not found" }); }
  const a = JSON.parse(r.analysis);
  const existing = c.prepare("select data from items where kind='findings' and paper_id=?").all(pid).map(x => JSON.parse(x.data).description);
  const existingSet = new Set(existing);
  const neu = [];
  
  if (!(r.text || "").trim()) {
    neu.push(["Incomplete evidence", "No text extracted from this PDF (scanned or PyMuPDF missing)"]);
  } else {
    for (const [k, v] of Object.entries(a.parameters)) {
      if (!v) neu.push(["Missing methodology details", `'${k}' not found in extracted text`]);
    }
    if (!a.keywords["baseline"]) {
      neu.push(["Missing baseline models", "No mention of 'baseline' in extracted text"]);
    }
  }
  
  for (const [cat, desc] of neu) {
    if (!existingSet.has(desc)) {
      c.prepare("insert into items(kind,paper_id,data,created) values('findings',?,?,?)").run(pid, JSON.stringify({
        category: cat, description: desc, severity: "Medium", status: "Potential risk", evidence: "Regex check of extracted text", next: "Check the paper manually"
      }), now());
    }
  }
  log(c, "Auto-check run", `${neu.length} gaps found (regex check)`, pid);
  c.close();
  res.json({ checked: neu.length });
});

app.get('/api/overview', (req, res) => {
  try {
    const c = db();
    const out = { papers: c.prepare("select * from papers order by id desc").all().map(r => paperRow(r)) };
    c.close();
    for (const k of Object.keys(REQ)) {
      out[k] = getItems(k);
    }
    res.json(out);
  } catch (e) {
    res.status(500).json({ detail: e.message });
  }
});

app.get('/api/history', (req, res) => {
  const c = db();
  res.json(c.prepare("select * from history order by id desc limit 200").all());
  c.close();
});

app.get('/api/graph/:pid', (req, res) => {
  const c = db();
  const r = c.prepare("select v from kv where k=?").get("graph:" + req.params.pid);
  c.close();
  res.json(r ? JSON.parse(r.v) : null);
});

app.put('/api/graph/:pid', (req, res) => {
  if (!Array.isArray(req.body.nodes)) return res.status(422).json({ detail: "nodes must be a list" });
  const c = db();
  c.prepare("insert or replace into kv values(?,?)").run("graph:" + req.params.pid, JSON.stringify(req.body));
  log(c, "Graph saved", `${req.body.nodes.length} nodes`, parseInt(req.params.pid));
  c.close();
  res.json({ ok: true });
});

app.get('/api/papers/:pid/report', (req, res) => {
  const pid = parseInt(req.params.pid);
  const c = db();
  const r = c.prepare("select * from papers where id=?").get(pid);
  c.close();
  if (!r) return res.status(404).send("Paper not found");
  const p = paperRow(r, true);
  const I = {};
  for (const k of Object.keys(REQ)) I[k] = getItems(k, pid);
  const a = p.analysis;
  
  let L = [
    `# Evidence report: ${p.title}`, "",
    `Generated ${now()} from stored data. File: ${p.filename}, ${p.pages} pages.`, "",
    "## Extraction (regex heuristics, not AI)",
    "Sections found: " + (a.sections.join(", ") || "none"), ""
  ];
  for (const [k, v] of Object.entries(a.parameters)) {
    L.push(`- ${k}: ${v || 'NOT SPECIFIED'}`);
  }
  L.push("", "## Artifacts");
  for (const x of I.artifacts) L.push(`- ${x.kind}: ${x.url} (${x.status})`);
  
  L.push("", "## Claims");
  for (const x of I.claims) {
    const rep = x.reproduced !== null && x.reproduced !== undefined ? x.reproduced : null;
    const diff = rep !== null ? `, abs diff ${(rep - x.reported > 0 ? '+' : '')}${(rep - x.reported).toFixed(4)}` : ", not tested";
    L.push(`- ${x.statement}${x.source === 'mock-ai' ? ' [mock]' : ''}: reported ${x.reported}, reproduced ${rep !== null ? rep : 'None'}${diff}`);
  }
  
  L.push("", "## Experiment versions");
  for (const x of I.versions) {
    L.push(`- ${x.name}: ${JSON.stringify(x.config)}, metric ${x.metric !== undefined ? x.metric : 'None'} (${x.run?.source === 'mock-ai' ? 'mock engine' : 'user-entered'})`);
  }
  
  L.push("", "## Audit findings");
  for (const x of I.findings) {
    L.push(`- [${x.status}] ${x.category}: ${x.description}`);
  }
  
  L.push("", "## Limitations", "- No experiment was executed by CrisisGrid; metrics are user-entered or come from the placeholder mock engine (marked [mock]).", "- Extraction uses regex heuristics and can miss or misread details.", "- Matching metrics alone do not establish full reproducibility.");
  
  const c2 = db();
  log(c2, "Report generated", p.title.substring(0, 60), pid);
  c2.close();
  
  res.setHeader('Content-Type', 'text/plain; charset=utf-8');
  res.send(L.join('\n'));
});

// AI Endpoints
app.get('/api/ai/status', (req, res) => res.json({ provider: ai.PROVIDER, mock: ai.PROVIDER === 'mock' }));

app.post('/api/ai/papers/:pid/understand', (req, res) => {
  const pid = parseInt(req.params.pid);
  const c = db();
  const r = c.prepare("select * from papers where id=?").get(pid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Paper not found" }); }
  const out = ai.understand(r.title, r.text || "", JSON.parse(r.analysis || "{}"));
  out.created = now();
  c.prepare("insert or replace into kv values(?,?)").run("ai:understand:" + pid, JSON.stringify(out));
  log(c, "AI analysis (mock)", r.title.substring(0, 60), pid);
  c.close();
  res.json(out);
});

app.get('/api/ai/papers/:pid/understand', (req, res) => {
  const c = db();
  const r = c.prepare("select v from kv where k=?").get("ai:understand:" + req.params.pid);
  c.close();
  res.json(r ? JSON.parse(r.v) : null);
});

app.post('/api/ai/papers/:pid/extract-claims', (req, res) => {
  const pid = parseInt(req.params.pid);
  const c = db();
  const r = c.prepare("select * from papers where id=?").get(pid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Paper not found" }); }
  
  const existing = c.prepare("select data from items where kind='claims' and paper_id=?").all(pid).map(x => JSON.parse(x.data).statement);
  const existingSet = new Set(existing);
  
  const claims = ai.extract_claims(r.text || "", r.title);
  let n = 0;
  for (const [st, v] of claims) {
    if (existingSet.has(st)) continue;
    c.prepare("insert into items(kind,paper_id,data,created) values('claims',?,?,?)").run(pid, JSON.stringify({ statement: st, reported: v, reproduced: null, source: "mock-ai" }), now());
    n++;
  }
  log(c, "Claims extracted (mock)", `${n} new`, pid);
  c.close();
  res.json({ added: n });
});

app.post('/api/ai/claims/:iid/reproduce', (req, res) => {
  const iid = parseInt(req.params.iid);
  const c = db();
  const r = c.prepare("select * from items where id=? and kind='claims'").get(iid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Not found" }); }
  
  const d = JSON.parse(r.data);
  const out = ai.reproduce_claim(d.statement, d.reported, iid);
  d.reproduced = out.reproduced;
  d.source = "mock-ai";
  c.prepare("update items set data=? where id=?").run(JSON.stringify(d), iid);
  log(c, "Claim reproduced (mock)", d.statement.substring(0, 70), r.paper_id);
  c.close();
  res.json(out);
});

app.post('/api/ai/items/versions/:iid/run', (req, res) => {
  const iid = parseInt(req.params.iid);
  const c = db();
  const r = c.prepare("select * from items where id=? and kind='versions'").get(iid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Not found" }); }
  
  const d = JSON.parse(r.data);
  const out = ai.run_experiment(d.config || {});
  d.metric = out.metric;
  d.run = { curve: out.curve, runtime_s: out.runtime_s, source: "mock-ai", ts: now() };
  c.prepare("update items set data=? where id=?").run(JSON.stringify(d), iid);
  log(c, "Experiment run (mock)", `${d.name}: metric ${out.metric}`, r.paper_id);
  c.close();
  res.json(out);
});

app.post('/api/ai/papers/:pid/audit', (req, res) => {
  const pid = parseInt(req.params.pid);
  const c = db();
  const r = c.prepare("select * from papers where id=?").get(pid);
  if (!r) { c.close(); return res.status(404).json({ detail: "Paper not found" }); }
  
  const existing = c.prepare("select data from items where kind='findings' and paper_id=?").all(pid).map(x => JSON.parse(x.data).description);
  const existingSet = new Set(existing);
  
  const findings = ai.audit(r.text || "", JSON.parse(r.analysis || "{}"));
  let n = 0;
  for (const f of findings) {
    if (existingSet.has(f.description)) continue;
    c.prepare("insert into items(kind,paper_id,data,created) values('findings',?,?,?)").run(pid, JSON.stringify(f), now());
    n++;
  }
  log(c, "AI audit (mock)", `${n} new findings`, pid);
  c.close();
  res.json({ added: n });
});

// Seed DB logic
const DEMO_TEXT = `Shift-Window ResNet Variants on Tiny Images (DEMO PAPER)
Abstract
We propose a shift-window ResNet for tiny image classification and report 87.2% top-1 accuracy on the test split, with a top-5 accuracy of 96.8%.
1. Introduction
Small-image classification remains a useful benchmark for efficient architectures.
3. Method
We train a ResNet with data augmentation using SGD, learning rate 1e-3 and batch size 128. Normalization constants are not given.
4. Experiments
Experiments use a 80/20 train and test partition of the dataset.
5. Results
The model reaches 87.2% accuracy and a 0.951 AUC.
6. Conclusion
References
`;

function seed(c) {
  const title = DEMO_TEXT.split('\n')[0];
  const info = c.prepare("insert into papers(filename,stored,title,size,pages,text,analysis,created) values(?,?,?,?,?,?,?,?)")
    .run("demo-paper.pdf", "", title, DEMO_TEXT.length, 4, DEMO_TEXT, JSON.stringify(analyze(DEMO_TEXT)), now());
  const pid = info.lastInsertRowid;
  
  function add(kind, d) {
    c.prepare("insert into items(kind,paper_id,data,created) values(?,?,?,?)").run(kind, pid, JSON.stringify(d), now());
  }
  
  add("artifacts", { kind: "repository", url: "https://github.com/example/shift-window-resnet", note: "demo link", status: "Not checked" });
  add("claims", { statement: "Top-1 accuracy on test split", reported: 0.872, reproduced: 0.841 });
  add("claims", { statement: "AUC", reported: 0.951, reproduced: 0.948 });
  add("versions", { name: "v1", config: { model: "ShiftResNet", dataset: "TinyImg", learning_rate: 0.001, batch_size: 128, epochs: 30, seed: "" }, metric: 0.841 });
  add("versions", { name: "v2", config: { model: "ShiftResNet", dataset: "TinyImg", learning_rate: 0.0005, batch_size: 128, epochs: 30, seed: "42" }, metric: 0.853 });
  add("findings", { category: "Missing methodology details", description: "Normalization constants are not specified", severity: "Medium", status: "Potential risk", evidence: "Paper section 3 (demo)", next: "Request details from authors" });
  
  log(c, "Demo data loaded", "Demo paper", pid);
  return pid;
}

app.post('/api/seed', (req, res) => {
  const c = db();
  const id = seed(c);
  c.close();
  res.json({ id });
});

const c = db();
if (process.env.CG_SEED !== "0" && !c.prepare("select 1 from papers").get()) {
  seed(c);
}
c.close();

const PORT = 8000;
app.listen(PORT, () => console.log(`[crisisgrid] API ready  http://127.0.0.1:${PORT}/docs`));
