# CrisisGrid: project status

*Last updated at the end of the integration + test phase. This file describes the whole system; the older
[`STATUS.md`](STATUS.md) / [`FEATURES.md`](FEATURES.md) cover the pipeline backend as it was when they were written (their
notes about `paper_env` are out of date, see "Corrections" below). How the parts connect: [`INTEGRATION.md`](INTEGRATION.md).*

**What it is:** a platform that helps check whether AI/ML papers reproduce. Three services run together:
the **website** (React) with its Node API, the **CrisisGrid API** (FastAPI: accounts, pipeline, workers), and the
**Paper Environment** (`paper_env`, mounted inside the API: a Colab-style notebook with an isolated Python environment,
dataset store and kernels per paper).

```
python scripts/run_all.py      # http://localhost:5173  (first account becomes the administrator)
```

Legend: ✅ built **and** covered by passing tests · 🟡 built, but with a stated gap or never run against the real thing · ⬜ not built

---

## 1. Test results at this commit

| Suite | Result | Covers |
|---|---|---|
| `core` (pytest) | 284 passed | models, graph, verifier, guard, audit rules, attribution |
| `api` (pytest) | 182 passed, 1 skipped | pipeline, gates, workers, **auth, ownership, mounted notebook API, worker executor** |
| `paper_env` (pytest) | 374 tests: the full run passed 373; the one failure (a macOS-fragile assertion in a terminal test) was fixed and that file re-run green | notebook, env manager, datasets, kernels, runs, checkpoints, events, hook, **auth, markdown/ipynb, runtimes/limits, secrets, terminal** |
| `website` Node (`npm test`) | 8 passed | JWT verification (incl. Python interop), auth middleware, per-user ownership |
| Browser E2E: notebook UI (`paper_env/frontend/e2e/smoke.mjs`) | 20/20 | CodeMirror editor, rich/sanitised output, tqdm, `# @param` forms, widgets, `input()`, collapse |
| Browser E2E: whole system (`.../e2e/website.mjs`) | 25/25 | sign-up, auth guard, API tokens, pipeline upload, notebook running **inside** the website, per-user isolation, sign-out/in |

"Tested" means: macOS, Python 3.12, SQLite, one process, scripted stand-in for any LLM. Nothing has run against Postgres,
Docker, a real LLM, a cloud, or CI.

---

## 2. Implemented

### 2.1 Paper Environment (`paper_env/`)
| Area | Status | Notes |
|---|---|---|
| Notebook: cells, immutable version history, `apply_edit` (the single edit door), `revert_to`, stale propagation | ✅ | every change carries an author (human/generator/agent) and a diff |
| **Markdown cells**, table of contents, **find & replace** (versioned) | ✅ | markdown never runs and never goes stale |
| **`.ipynb` import/export**, `!pip` rewrite | ✅ | import never executes; pip lines become comments + a package list; lines with shell operators are reported, not parsed |
| Env manager (uv): create / install / pin / rebuild, lockfile + hash as run identity, structured failures | ✅ | resolves the whole spec on install; failed rebuild keeps the old env |
| Kernel runner: streaming output, per-cell and per-run timeouts, interrupt, restart, output caps, secret scrubbing | ✅ | experiment failure vs **infrastructure** failure (kernel death / OOM / segfault) kept distinct everywhere |
| Run modes: cell / until / all / clean; `stop_on_fail` skipping; `verification_eligible` only for complete clean runs | ✅ | one run per environment at a time |
| Checkpoint & resume (dill / safetensors, RNG + imports restored, honest stale marking, integrity-checked) | ✅ | unpickling happens only inside the kernel |
| Dataset store: content-addressed read-only cache, resumable HTTP/HF/URL/upload fetchers, checksums, LRU, preflight, derived datasets, gated tokens | ✅ | Kaggle and S3 are explicit stubs |
| `datasets` and `userdata` helpers injected into kernels; `report_metric` for pipeline jobs | ✅ | |
| Events: typed, persisted, subscribable (in-process + WebSocket with replay); ephemeral widget traffic | ✅ | |
| Pre-execute hook (allow / deny / needs-approval) on cell run, install, pin, rebuild, fetch, **terminal**; fails closed | ✅ | blocked actions are resumable |
| Compute backends behind one interface; **runtime picker** (CPU / local GPU / TPU / AWS listed with availability) | 🟡 | only the local backend exists; TPU/AWS are listed as unavailable with the reason |
| **Session limits**: idle timeout, max length, cost cap, auto-disconnect | ✅ | cost cap is real but local runtimes cost 0 |
| **Secrets manager**: per-user, Fernet-encrypted, never returned, redacted everywhere, `userdata.get` only for secrets you grant | ✅ | |
| **Terminal** (xterm.js, PTY) | 🟡 | off by default, researcher/admin only, goes through the hook, package changes recorded as *unmanaged* lockfile drift. **Not sandboxed.** |
| Files workspace (upload/download/delete, path-confined, size-capped) | ✅ | |
| **Notebook web UI** (Vite + TypeScript): CodeMirror 6 with kernel completion/docs, JupyterLab rendermime (sanitised), **ipywidgets**, tqdm bars, `input()`, `# @param` forms, collapse | ✅ | verified in a real browser |
| FastAPI layer (~60 endpoints) with **roles and per-user ownership** on every route and WebSocket | ✅ | |

### 2.2 Accounts and authentication (`api/crisisgrid_api/auth/`, Node `auth.js`, `paper_env/auth.py`)
| Feature | Status |
|---|---|
| Register / login, scrypt password hashing, password policy, uniform failure messages (no user enumeration) | ✅ |
| Signed session tokens (HS256 JWT, 12 h), verified identically by Python and Node | ✅ |
| Personal **API tokens** (`cgt_…`), hashed at rest, revocable, expiring | ✅ |
| Roles viewer / researcher / admin; admin user management; last admin protected | ✅ |
| Lockout after repeated failures, per-IP rate limit, password change / "sign out everywhere" revoke sessions | ✅ |
| **Ownership**: papers and notebook environments belong to their creator; others get 404/403; admins see all | ✅ |
| Single sign-on across the FastAPI, the Node API and the mounted notebook API | ✅ |

### 2.3 Integration
| Feature | Status |
|---|---|
| `paper_env` mounted in the CrisisGrid API under `/paper-env` (same process, same login) | ✅ |
| **Worker executor** running pipeline clean-run jobs in managed uv environments with real kernels (`--executor paper_env`) | ✅ |
| Website: login/register, guarded dashboard, **Account** (tokens, password, user admin), **Notebook** (embeds the UI), **Pipeline** (real API: upload, live events, gates, claims, audit, report) | ✅ |
| One-command launcher with a shared secret, isolated state dir and configurable ports | ✅ |

### 2.4 The pipeline backend (written by the other half of the team; see `STATUS.md` / `FEATURES.md`)
Models, graph versioning, claim verifier, harness guard, audit rules R01–R16, route selector, Tier-1 static audit, model
gateway, persisted pipeline with gates, job queue and workers, supervised repair loop, attribution and report checks, secret
scrubbing, offline demo: ✅ (with their stated caveats, e.g. rules R01–R11 are author-defined and no real LLM was ever used).

---

## 3. Remaining

### 3.1 Blocks production use
1. **No sandbox.** Notebook kernels, the terminal and the subprocess worker run as the server's OS user with full network and
   filesystem access. Untrusted papers need container isolation (non-root, read-only image, resource limits, egress
   allowlist). *Nothing in this repo provides that yet.*
2. **LLM agents have never run for real.** Prompts, parsing, gate flows and extraction quality are untested against a model.
   Without `CG_LLM_CHAIN` the Pipeline page can ingest and audit-view but cannot start runs.
3. **Deployment path is untested:** `docker-compose.yml`, the Dockerfile, Postgres/pgvector migrations and CI have never run.
   Everything above ran on SQLite in one process.
4. **Auth hardening gaps:** no HTTPS termination or security headers shipped; no password reset / email verification; the
   login rate limiter and the pipeline queue are in-memory (single process); the Node API accepts a revoked *session* token
   until it expires (≤12 h; API tokens are checked live); no audit log of sign-ins/admin actions; tokens live in
   `localStorage` (fine for this app, but an XSS would expose them). Needs a security review.

### 3.2 Paper Environment
- ⬜ AWS and TPU adapters (listed, unavailable). ⬜ BYO-Jupyter adapter.
- 🟡 Widgets: model state is lost on page reload or kernel restart (re-run the cell); only the built-in widget packages load
  (no third-party widget JS). 🟡 Untrusted SVG renders as its text fallback.
- ⬜ Markdown math (LaTeX), cell reordering by drag, undo of cell deletion, multi-user collaborative editing.
- 🟡 Staleness is by cell position, not by declared inputs/outputs (conservative but coarse).
- 🟡 Variables derived from granted secrets can end up in checkpoint files.
- ⬜ A `KernelSession` DB record and Redis output streams (sessions are in memory and lost on restart; a paused run cannot resume
  after a server restart).
- ⬜ Windows support (`fcntl`, `pty`, process groups; `/paper-env` is simply not mounted there).
- 🟡 Chatty cells produce one event row per output chunk (no coalescing).
- ⬜ Reconciling `paper_env.domain` with `core.models` (uuid4 vs uuid7, separate DBs): their decision D-02/D-08.

### 3.3 Pipeline / product (from `FEATURES.md`, unchanged)
Graph builder, Env builder and Code rebuilder agents; graph persistence and Version Comparison endpoints; wiring attribution
into the pipeline; PDF report export; cost caps / AWS budgets; Colab bootstrap and desktop app; golden-paper recorded demo.

### 3.4 Website
- 🟡 The original dashboard pages (Understand, Claims, Lab, Graph, Versions, Audit, Reports, History) still run on the Node
  **mock-AI** API (shared with the new login). Only **Pipeline** and **Notebook** use the real backends. Migrating them to the
  FastAPI is not done.
- ⬜ Browser E2E is manual (`node e2e/*.mjs` against a running system); not in CI. ⬜ No favicon; bundle is one large chunk.

---

## 4. Suggested next steps (in order)
1. **Container sandbox** for kernels / workers (the single biggest risk), then re-run the pipeline end to end through it.
2. **Wire a real LLM** (`--llm-chain`) and evaluate extraction on 3 hand-labelled papers; fix prompts/gates from what breaks.
3. **Bring up Docker Compose + Postgres** and fix whatever the first real run shows; turn the test suites and the two
   browser E2E scripts into CI jobs.
4. **Security pass** on auth: HTTPS + headers, password reset, audit log, shared revocation for the Node API (or move it onto
   the FastAPI), review token storage.
5. **Migrate the remaining dashboard pages** off the mock Node API onto the pipeline, then retire the Node backend.
6. Reconcile the two domain models and persist kernel sessions; then AWS GPU adapter + cost accounting.

---

## 5. Corrections to older notes
`STATUS.md` / `FEATURES.md` / `decisions.md` (D-27) say `paper_env` was unaudited, untestable, had no dataset checksums in
`api/`, no notebook UI, no `.ipynb` support and no `!pip` handling. Today: it has its own passing test suite, a dataset store with
checksum verification, a full notebook UI, `.ipynb` import/export with the `!pip` rewrite, and is integrated as the worker's
optional executor. Still true: it is POSIX-only, not connected to Redis, and not a sandbox.

## 6. Bugs found while testing this phase (all fixed)
- **Security:** `POST /runs` took `paper_env_id` / `cell_id` in the request body, which the path-based ownership check did not
  see, so another user could start a run on your environment. Now checked explicitly (and tested).
- Granted secrets were deleted before the new kernel read them (a shutdown/start ordering bug).
- Auth: SQLite returns naive datetimes, which crashed account lockout and API-token expiry comparisons (500).
- `!pip install numpy; echo hi` parsed `echo` and `hi` as packages; lines with shell operators are now reported whole.
- A runaway-output cell could flood the host (scrubbing ran before the size cap); a live progress line could reveal half a
  secret; the pipeline upload sent an empty body; a hidden markdown element shadowed the real output area.
- Existing tests that were order-dependent or pinned the previous migration revision were corrected.

## 7. How to run the tests
The Python side is **one uv workspace** (root `pyproject.toml`, one `uv.lock`, one `.venv` shared by `core`, `api` and `paper_env`).
```bash
uv sync                                                      # from the repo root or from any of core/ api/ paper_env/
cd core && uv run pytest
cd api && uv run pytest
cd paper_env && uv run pytest                                # ~10 min (real kernels, uv installs)
cd website && npm install && npm test
# browser E2E (needs a running system; use an isolated one so test accounts do not pollute yours):
python scripts/run_all.py --state-dir /tmp/cg-e2e --api-port 18100 --node-port 18001 --web-port 15173
cd paper_env/frontend && npm install && BASE=http://localhost:15173 node e2e/website.mjs
```
