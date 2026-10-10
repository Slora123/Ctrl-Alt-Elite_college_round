import json,os,re,sqlite3,time,uuid,urllib.request
from pathlib import Path
from typing import Optional
from fastapi import FastAPI,UploadFile,File,HTTPException
from fastapi.responses import PlainTextResponse
import ai_mock as ai  # MOCK AI/ML layer; swap its function bodies for real models later
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz=None
BASE=Path(__file__).parent;UP=BASE/"uploads";UP.mkdir(exist_ok=True)
app=FastAPI(title="CrisisGrid API")
@app.get("/api/health")
def health():return {"ok":True,"ai_provider":ai.PROVIDER}
def db():
    c=sqlite3.connect(BASE/"crisisgrid.db");c.row_factory=sqlite3.Row;return c
with db() as c:
    c.executescript("""create table if not exists papers(id integer primary key,filename text,stored text,title text,size integer,pages integer,text text,analysis text,created text);
create table if not exists items(id integer primary key,kind text,paper_id integer,data text,created text);
create table if not exists history(id integer primary key,ts text,type text,detail text,paper_id integer);
create table if not exists kv(k text primary key,v text);""")
now=lambda:time.strftime("%Y-%m-%d %H:%M:%S")
def log(c,t,d,pid=None):c.execute("insert into history(ts,type,detail,paper_id) values(?,?,?,?)",(now(),t,d,pid))
def bad(m,code=422):raise HTTPException(code,m)
SEC=["abstract","introduction","related work","method","methodology","dataset","experiments","results","evaluation","conclusion","references"]
PAR={"learning rate":r"learning[\s-]*rate[^0-9a-z]{0,12}([0-9.]+(?:e-?\d+)?)","batch size":r"batch[\s-]*size[^0-9]{0,12}(\d+)","epochs":r"(\d+)\s+epochs","random seed":r"seed[^0-9]{0,12}(\d+)","optimizer":r"\b(adamw|adam|sgd|rmsprop)\b"}
KW=["accuracy","f1","precision","recall","auc","transformer","cnn","resnet","bert","lstm","dropout","augmentation","baseline"]
def analyze(text):
    low=text.lower()
    p={k:(m.group(1) if (m:=re.search(r,low)) else None) for k,r in PAR.items()}
    return {"method":"regex heuristics on extracted text (not AI)","sections":[s for s in SEC if re.search(r"^\s*(\d+\.?\s*)?"+s+r"\b",low,re.M)],"parameters":p,"keywords":{k:low.count(k) for k in KW if k in low}}
@app.post("/api/papers")
async def upload(file:UploadFile=File(...)):
    if not (file.filename or "").lower().endswith(".pdf"):bad("Only .pdf files are accepted",415)
    data=await file.read()
    if len(data)>20_000_000:bad("File exceeds 20 MB",413)
    if not data.startswith(b"%PDF"):bad("File is not a valid PDF",415)
    text,pages="",0
    if fitz:
        try:
            with fitz.open(stream=data,filetype="pdf") as d:pages=len(d);text="\n".join(p.get_text() for p in d)
        except Exception:bad("PDF could not be parsed")
    stored=uuid.uuid4().hex+".pdf";(UP/stored).write_bytes(data)
    title=next((l.strip() for l in text.splitlines() if len(l.strip())>=10),file.filename)[:140]
    with db() as c:
        i=c.execute("insert into papers(filename,stored,title,size,pages,text,analysis,created) values(?,?,?,?,?,?,?,?)",(file.filename,stored,title,len(data),pages,text,json.dumps(analyze(text)),now())).lastrowid
        log(c,"Paper uploaded",file.filename,i)
    return {"id":i,"title":title,"pages":pages,"extracted":bool(text.strip()),"note":None if fitz else "PyMuPDF not installed; no text extracted"}
def paper_row(r,full=False):
    d={k:r[k] for k in("id","filename","title","size","pages","created")};d["chars"]=len(r["text"] or "")
    if full:d["text"]=(r["text"] or "")[:60000];d["analysis"]=json.loads(r["analysis"])
    return d
@app.get("/api/papers")
def papers():
    with db() as c:return [paper_row(r) for r in c.execute("select * from papers order by id desc")]
@app.get("/api/papers/{pid}")
def paper(pid:int):
    with db() as c:
        r=c.execute("select * from papers where id=?",(pid,)).fetchone()
    return paper_row(r,True) if r else bad("Paper not found",404)
@app.delete("/api/papers/{pid}")
def del_paper(pid:int):
    with db() as c:
        r=c.execute("select stored from papers where id=?",(pid,)).fetchone()
        if not r:bad("Paper not found",404)
        (UP/r["stored"]).unlink(missing_ok=True)
        c.execute("delete from papers where id=?",(pid,));c.execute("delete from items where paper_id=?",(pid,));log(c,"Paper deleted",str(pid))
    return {"ok":True}
REQ={"artifacts":["kind","url"],"claims":["statement","reported"],"versions":["name","config"],"findings":["category","description"]}
def num(d,k):
    if d.get(k) in (None,""):d[k]=None;return
    try:d[k]=float(d[k])
    except (TypeError,ValueError):bad(f"{k} must be a number")
def clean(kind,d):
    if kind not in REQ:bad("Unknown resource",404)
    if not isinstance(d,dict) or not d.get("paper_id"):bad("paper_id is required")
    miss=[k for k in REQ[kind] if d.get(k) in (None,"")]
    if miss:bad("Missing: "+", ".join(miss))
    for k in("reported","reproduced","metric"):
        if k in d:num(d,k)
    if kind=="artifacts":
        if d["kind"]=="repository" and not re.match(r"^https?://\S+$",d["url"]):bad("Repository URL must start with http:// or https://")
        d.setdefault("status","Not checked")
    if kind=="findings":d.setdefault("status","Potential risk");d.setdefault("severity","Medium")
    if kind=="versions":
        c=d["config"]
        if not isinstance(c,dict):bad("config must be an object")
        for k in("learning_rate","batch_size","epochs"):
            if c.get(k) not in (None,""):
                try:x=float(c[k])
                except ValueError:bad(f"{k} must be a number")
                if k=="learning_rate":
                    if not 0<x<1:bad("learning_rate must be between 0 and 1")
                    c[k]=x
                elif x<=0 or x!=int(x):bad(f"{k} must be a positive integer")
                else:c[k]=int(x)
    return d
def item(r):return {"id":r["id"],"paper_id":r["paper_id"],"created":r["created"],**json.loads(r["data"])}
@app.get("/api/items/{kind}")
def items(kind:str,paper_id:Optional[int]=None):
    if kind not in REQ:bad("Unknown resource",404)
    with db() as c:
        q="select * from items where kind=?"+(" and paper_id=?" if paper_id else "")+" order by id"
        return [item(r) for r in c.execute(q,(kind,paper_id) if paper_id else (kind,))]
@app.post("/api/items/{kind}")
def add(kind:str,body:dict):
    d=clean(kind,body);pid=int(d.pop("paper_id"))
    with db() as c:
        if not c.execute("select 1 from papers where id=?",(pid,)).fetchone():bad("Paper not found",404)
        i=c.execute("insert into items(kind,paper_id,data,created) values(?,?,?,?)",(kind,pid,json.dumps(d),now())).lastrowid
        log(c,kind[:-1].capitalize()+" created",str(d.get("name") or d.get("statement") or d.get("description") or d.get("url"))[:80],pid)
    return {"id":i}
@app.patch("/api/items/{kind}/{iid}")
def patch(kind:str,iid:int,body:dict):
    with db() as c:
        r=c.execute("select * from items where id=? and kind=?",(iid,kind)).fetchone()
        if not r:bad("Not found",404)
        d={**json.loads(r["data"]),**body,"paper_id":r["paper_id"]};d=clean(kind,d);d.pop("paper_id")
        c.execute("update items set data=? where id=?",(json.dumps(d),iid));log(c,kind[:-1].capitalize()+" updated",f"#{iid}: "+", ".join(body),r["paper_id"])
    return {"ok":True}
@app.delete("/api/items/{kind}/{iid}")
def rm(kind:str,iid:int):
    with db() as c:
        c.execute("delete from items where id=? and kind=?",(iid,kind));log(c,kind[:-1].capitalize()+" deleted",f"#{iid}")
    return {"ok":True}
@app.post("/api/items/artifacts/{iid}/check")
def check(iid:int):
    with db() as c:
        r=c.execute("select * from items where id=? and kind='artifacts'",(iid,)).fetchone()
        if not r:bad("Not found",404)
        d=json.loads(r["data"])
        if not re.match(r"^https?://",d["url"]):bad("Only http(s) URLs can be checked")
        try:
            with urllib.request.urlopen(urllib.request.Request(d["url"],method="HEAD"),timeout=6) as x:d["status"]=f"Reachable (HTTP {x.status}), checked {now()}"
        except Exception as e:d["status"]=f"Unreachable: {str(e)[:80]}, checked {now()}"
        c.execute("update items set data=? where id=?",(json.dumps(d),iid));log(c,"Artifact URL checked",d["status"][:80],r["paper_id"])
    return {"status":d["status"]}
@app.post("/api/papers/{pid}/autocheck")
def autocheck(pid:int):
    with db() as c:
        r=c.execute("select * from papers where id=?",(pid,)).fetchone()
        if not r:bad("Paper not found",404)
        a=json.loads(r["analysis"]);have={json.loads(x["data"])["description"] for x in c.execute("select data from items where kind='findings' and paper_id=?",(pid,))};new=[]
        if not (r["text"] or "").strip():new.append(("Incomplete evidence","No text extracted from this PDF (scanned or PyMuPDF missing)"))
        else:
            new+=[("Missing methodology details",f"'{k}' not found in extracted text") for k,v in a["parameters"].items() if not v]
            if "baseline" not in a["keywords"]:new.append(("Missing baseline models","No mention of 'baseline' in extracted text"))
        for cat,desc in new:
            if desc not in have:c.execute("insert into items(kind,paper_id,data,created) values('findings',?,?,?)",(pid,json.dumps({"category":cat,"description":desc,"severity":"Medium","status":"Potential risk","evidence":"Regex check of extracted text","next":"Check the paper manually"}),now()))
        log(c,"Auto-check run",f"{len(new)} gaps found (regex check)",pid)
    return {"checked":len(new)}
@app.get("/api/overview")
def overview():
    out={"papers":papers()}
    for k in REQ:out[k]=items(k)
    return out
@app.get("/api/history")
def history():
    with db() as c:return [dict(r) for r in c.execute("select * from history order by id desc limit 200")]
@app.get("/api/graph/{pid}")
def get_graph(pid:int):
    with db() as c:r=c.execute("select v from kv where k=?",(f"graph:{pid}",)).fetchone()
    return json.loads(r["v"]) if r else None
@app.put("/api/graph/{pid}")
def put_graph(pid:int,body:dict):
    if not isinstance(body.get("nodes"),list):bad("nodes must be a list")
    with db() as c:
        c.execute("insert or replace into kv values(?,?)",(f"graph:{pid}",json.dumps(body)));log(c,"Graph saved",f"{len(body['nodes'])} nodes",pid)
    return {"ok":True}
@app.get("/api/papers/{pid}/report",response_class=PlainTextResponse)
def report(pid:int):
    p=paper(pid);I={k:items(k,pid) for k in REQ};a=p["analysis"]
    L=[f"# Evidence report: {p['title']}","",f"Generated {now()} from stored data. File: {p['filename']}, {p['pages']} pages.","","## Extraction (regex heuristics, not AI)","Sections found: "+(", ".join(a["sections"]) or "none"),""]+[f"- {k}: {v or 'NOT SPECIFIED'}" for k,v in a["parameters"].items()]
    L+=["","## Artifacts"]+[f"- {x['kind']}: {x['url']} ({x['status']})" for x in I["artifacts"]]
    L+=["","## Claims"]+[f"- {x['statement']}{' [mock]' if x.get('source')=='mock-ai' else ''}: reported {x['reported']}, reproduced {x.get('reproduced')}"+(f", abs diff {x['reproduced']-x['reported']:+.4f}" if x.get('reproduced') is not None else ", not tested") for x in I["claims"]]
    L+=["","## Experiment versions"]+[f"- {x['name']}: {json.dumps(x['config'])}, metric {x.get('metric')} ({'mock engine' if (x.get('run') or {}).get('source')=='mock-ai' else 'user-entered'})" for x in I["versions"]]
    L+=["","## Audit findings"]+[f"- [{x['status']}] {x['category']}: {x['description']}" for x in I["findings"]]
    L+=["","## Limitations","- No experiment was executed by CrisisGrid; metrics are user-entered or come from the placeholder mock engine (marked [mock]).","- Extraction uses regex heuristics and can miss or misread details.","- Matching metrics alone do not establish full reproducibility."]
    with db() as c:log(c,"Report generated",p["title"][:60],pid)
    return "\n".join(L)

# ---------------- MOCK AI endpoints (backed by ai_mock.py) ----------------
def _paper(c,pid):
    r=c.execute("select * from papers where id=?",(pid,)).fetchone()
    return r if r else bad("Paper not found",404)
def _item(c,iid,kind):
    r=c.execute("select * from items where id=? and kind=?",(iid,kind)).fetchone()
    return r if r else bad("Not found",404)
@app.get("/api/ai/status")
def ai_status():return {"provider":ai.PROVIDER,"mock":ai.PROVIDER=="mock"}
@app.post("/api/ai/papers/{pid}/understand")
def ai_understand(pid:int):
    with db() as c:
        r=_paper(c,pid);out=ai.understand(r["title"],r["text"] or "",json.loads(r["analysis"]));out["created"]=now()
        c.execute("insert or replace into kv values(?,?)",(f"ai:understand:{pid}",json.dumps(out)));log(c,"AI analysis (mock)",r["title"][:60],pid)
    return out
@app.get("/api/ai/papers/{pid}/understand")
def ai_understand_get(pid:int):
    with db() as c:r=c.execute("select v from kv where k=?",(f"ai:understand:{pid}",)).fetchone()
    return json.loads(r["v"]) if r else None
@app.post("/api/ai/papers/{pid}/extract-claims")
def ai_extract(pid:int):
    with db() as c:
        r=_paper(c,pid);have={json.loads(x["data"])["statement"] for x in c.execute("select data from items where kind='claims' and paper_id=?",(pid,))};n=0
        for st,v in ai.extract_claims(r["text"] or "",r["title"]):
            if st in have:continue
            c.execute("insert into items(kind,paper_id,data,created) values('claims',?,?,?)",(pid,json.dumps({"statement":st,"reported":v,"reproduced":None,"source":"mock-ai"}),now()));n+=1
        log(c,"Claims extracted (mock)",f"{n} new",pid)
    return {"added":n}
@app.post("/api/ai/claims/{iid}/reproduce")
def ai_reproduce(iid:int):
    with db() as c:
        r=_item(c,iid,"claims");d=json.loads(r["data"]);out=ai.reproduce_claim(d["statement"],d["reported"],iid)
        d["reproduced"]=out["reproduced"];d["source"]="mock-ai";c.execute("update items set data=? where id=?",(json.dumps(d),iid));log(c,"Claim reproduced (mock)",d["statement"][:70],r["paper_id"])
    return out
@app.post("/api/ai/items/versions/{iid}/run")
def ai_run(iid:int):
    with db() as c:
        r=_item(c,iid,"versions");d=json.loads(r["data"]);out=ai.run_experiment(d.get("config") or {})
        d["metric"]=out["metric"];d["run"]={"curve":out["curve"],"runtime_s":out["runtime_s"],"source":"mock-ai","ts":now()}
        c.execute("update items set data=? where id=?",(json.dumps(d),iid));log(c,"Experiment run (mock)",f"{d['name']}: metric {out['metric']}",r["paper_id"])
    return out
@app.post("/api/ai/papers/{pid}/audit")
def ai_audit(pid:int):
    with db() as c:
        r=_paper(c,pid);have={json.loads(x["data"])["description"] for x in c.execute("select data from items where kind='findings' and paper_id=?",(pid,))};n=0
        for f in ai.audit(r["text"] or "",json.loads(r["analysis"])):
            if f["description"] in have:continue
            c.execute("insert into items(kind,paper_id,data,created) values('findings',?,?,?)",(pid,json.dumps(f),now()));n+=1
        log(c,"AI audit (mock)",f"{n} new findings",pid)
    return {"added":n}

# ---------------- Demo data (so the UI is alive on first run) ----------------
DEMO_TEXT="""Shift-Window ResNet Variants on Tiny Images (DEMO PAPER)
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
"""
def seed(c):
    t=DEMO_TEXT;title=t.splitlines()[0]
    pid=c.execute("insert into papers(filename,stored,title,size,pages,text,analysis,created) values(?,?,?,?,?,?,?,?)",("demo-paper.pdf","",title,len(t),4,t,json.dumps(analyze(t)),now())).lastrowid
    def add(kind,d):c.execute("insert into items(kind,paper_id,data,created) values(?,?,?,?)",(kind,pid,json.dumps(d),now()))
    add("artifacts",{"kind":"repository","url":"https://github.com/example/shift-window-resnet","note":"demo link","status":"Not checked"})
    add("claims",{"statement":"Top-1 accuracy on test split","reported":0.872,"reproduced":0.841})
    add("claims",{"statement":"AUC","reported":0.951,"reproduced":0.948})
    add("versions",{"name":"v1","config":{"model":"ShiftResNet","dataset":"TinyImg","learning_rate":0.001,"batch_size":128,"epochs":30,"seed":""},"metric":0.841})
    add("versions",{"name":"v2","config":{"model":"ShiftResNet","dataset":"TinyImg","learning_rate":0.0005,"batch_size":128,"epochs":30,"seed":"42"},"metric":0.853})
    add("findings",{"category":"Missing methodology details","description":"Normalization constants are not specified","severity":"Medium","status":"Potential risk","evidence":"Paper section 3 (demo)","next":"Request details from authors"})
    log(c,"Demo data loaded","Demo paper",pid);return pid
@app.post("/api/seed")
def seed_demo():
    with db() as c:return {"id":seed(c)}
with db() as _c:
    if os.environ.get("CG_SEED","1")!="0" and not _c.execute("select 1 from papers").fetchone():seed(_c)
