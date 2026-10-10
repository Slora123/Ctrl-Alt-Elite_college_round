# CrisisGrid backend: implemented and pending features

Feature-level checklist. For the phase view see [STATUS.md](STATUS.md); for the reasoning behind each deviation see [decisions.md](decisions.md).

**Legend**
- ✅ **Implemented**: built and covered by passing tests.
- 🟡 **Partial**: built, but a stated part is missing or was never run against the real thing.
- ⬜ **Pending**: not built.

"Tested" always means: Windows, Python 3.13, SQLite, scripted stand-in for the LLM. Nothing has run against Postgres, Docker, or a real model.

---

## 1. Implemented

### 1.1 Trust rules (the platform's core promises)

| Feature | Detail |
|---|---|
| ✅ Only clean runs are evidence | A run must be a fresh process, fixed seed, top to bottom. Interactive runs are rejected by the model itself, the verifier and attribution. |
| ✅ Models never decide a verdict | Only the Claim verifier, on worker-produced results, can output reproduced / partial / not reproduced. A model that smuggles a verdict into its output is ignored (tested). |
| ✅ Executors never see claim targets | Firewall in the Harness guard: rejects any deps or job payload that carries a claim, `reported_value`, `target`, `baseline_value`. Checked for the Run diagnoser and at job enqueue. |
| ✅ No hallucinated facts | Every `stated` value needs a quote that exists in the paper. Fabricated quote: item dropped. No quote: downgraded to `inferred`. |
| ✅ Nothing silently guessed | Missing info becomes an explicit gap, a `not_executed` verdict, an `inconclusive` verdict, or a gate. |
| ✅ Untrusted text is data | Papers, tracebacks and cell source reach models only inside quoted blocks. A forged end-marker inside the text is neutralised. |
| ✅ Secret scrubbing | Worker scrubs, then the API scrubs again. A secret that exists only at runtime was shown not to reach any table, event, gate, run state or report. Notebooks with unmistakable credentials are refused (422) without echoing them. |

### 1.2 Core logic (`core/`, no I/O)

| Feature | Detail |
|---|---|
| ✅ Shared models | Claim, Gap, GraphNode, ExperimentGraph, RunResult, Verdict, EnvSpec, plus the Paper Environment models from the design. UUIDv7 ids, strictly increasing. |
| ✅ Experiment graph service | canonicalize, node hashing (ignores comments and whitespace in bound code), validate (cycles, dangling inputs, duplicates, claim-must-be-sink, stated-needs-source), create_version, diff (with invalidated downstream set), downstream. |
| ✅ Claim verifier | Tolerance and seed-variance logic. Caps at `partial` for scaled-down runs and approximate splits. Caveat for fewer than 3 seeds. `infra_failed` runs excluded. Metric-mismatch gives `inconclusive`. Metric names match ignoring case and punctuation. |
| ✅ Harness guard | Attempt cap per stage, token and time budgets (injectable clock), loop detection on `sha256(action + normalized error)`, one validation retry then escalate. |
| ✅ Audit rules R01–R16 | Seed, hyperparameters, split, metric ambiguity, compute, code availability, data availability, dependency pinning, variance, preprocessing, pretrained weights, best-seed, test-set selection, baseline lineage, code version, external dependency. Only rules (never agents) can emit `blocking`. Gap merge caps agent gaps at `major` and drops duplicates. |
| ✅ Route selector | Case 1 (data missing/private → Tier 1, data gate), case 3 (no repo → Tier 3, gap gate), case 2 (repo + data → Tier 2). Deterministic. |
| ✅ Job rules | Legal status transitions, worker/job capability matching with reasons, retry only on infrastructure failure (bounded), heartbeat staleness (handles naive/aware datetimes). |
| ✅ Action hook | Policy table safe / experiment_changing / external / blocked. **Classifies code edits from the diff itself**, never from the model's claim. Package names must be plain requirements. Host allowlist. File mounts denied. Budget and loop checks first. |
| ✅ Change attribution | Direct single-edit attribution, one-at-a-time contributions with explicit residual, 2-standard-error noise floor that can answer "no attributable effect", partial results under a run budget, confound flags (environment, structural, architecture, mixed concerns), run planning. |
| ✅ Report checks | Number post-check (numbers in a narrative must exist in the evidence, at the precision stated), deviation list for agent edits, reconstruction fidelity kept separate from reproducibility. |
| ✅ Secret scrubber | Known-secret redaction plus credential shapes (HF, GitHub, OpenAI/Anthropic-style, AWS, Slack, bearer tokens, private keys, `password=...`). |

### 1.3 Tier 1: static audit from a paper alone (`api/`)

| Feature | Detail |
|---|---|
| ✅ PDF ingest | Text per page, SHA-256 pin of the exact paper version, creation-date metadata, scanned-PDF detection (`needs_ocr`, never an empty analysis), OCR hook. |
| ✅ Chunking and retrieval | Page-aware chunks with overlap; long papers are condensed to the passages that answer the questions we need; hashing embedder and in-memory index (deterministic, offline). |
| ✅ Paper parser agent | Typed `ParsedPaper` output, contradictions kept with both spans, quotes verified against the whole paper afterwards. |
| ✅ Artifact finder | Repo, HF/Zenodo/Kaggle datasets, weights; repo commit pinned; "default branch changed after the paper" check; dataset de-duplication; GitHub adapter (tested on a mock transport only). |
| ✅ Gap auditor agent | Finds gaps the rules did not; merged with rule gaps; can never block alone. |
| ✅ Tier 1 end to end | `run_tier1`: PDF to claims, gap list and artifact inventory. |

### 1.4 Model gateway (`api/gateway`)

| Feature | Detail |
|---|---|
| ✅ Provider chain | Ordered providers, per-agent profiles. |
| ✅ Failure handling | Rate limit cools the provider and fails over with no backoff; timeouts/5xx back off with full jitter; auth/invalid skip the provider; all-failed raises with every reason. |
| ✅ Deterministic-call cache | Only temperature-0 calls; invalid replies are evicted so a retry is never served the same bad answer. |
| ✅ Audit log | Every call stored with prompt, output, provider, model, tokens, errors and cache hits (`llm_calls`). |
| ✅ Typed output | JSON validated against a Pydantic model; one retry with the validation error shown; then escalate. |

### 1.5 Pipeline and human gates

| Feature | Detail |
|---|---|
| ✅ Persisted pipeline | Ingest, Understand, Audit, Route, gates, Execute, Verify, Report, Final gate. State saved after every node. |
| ✅ Survives a restart | A second app/container on the same database resumes a waiting run and makes no model calls (tested). |
| ✅ Gate kinds | `data`, `gap`, `stuck`, `final`, `action`, each with its allowed answers. Invalid answers are refused **before** the gate is resolved. |
| ✅ Stuck handling | Guard failures (budget, loop, invalid output, no usable result) open a `stuck` gate. A human retry of the same failure is blocked after one repeat. Skip stage and abort supported. |
| ✅ Final gate | Accept, request rerun, edit caveats. |
| ✅ Honest Tier-1 reports | A report that says "no experiment was executed" whenever that is true. |

### 1.6 Execution (Tier 2)

| Feature | Detail |
|---|---|
| ✅ Job queue | DB-backed, atomic compare-and-set claim (two workers cannot take the same job), FIFO, capability matching. |
| ✅ Retries | Infrastructure failures retried once; code failures and timeouts are final; cancelled jobs notify the run. |
| ✅ Reaper | Jobs of a silent worker are requeued once then failed; runs automatically in the background (`CG_REAPER_INTERVAL_S`). |
| ✅ Workers | Register, bearer token (hash stored), heartbeat, claim, start, stream logs, complete. Outbound only. Registration closed outside dev unless a shared secret is set. |
| ✅ Subprocess executor | Fresh `python -I` process, scrubbed environment, fixed seed (python/numpy/torch), timeout kill, magics and in-cell installs refused, dying process classified as infrastructure, secrets scrubbed from logs and errors. |
| ✅ Notebook input | `.ipynb` (or `{cells: [...]}`) parsed with limits; cells publish numbers with `report_metric(name, value)`. |
| ✅ Seeds | One clean job per seed (default 0,1,2). |
| ✅ Verdicts | Run results built from job results; substitute data forces `inconclusive`; case-3 results labelled directional; lower-isolation caveat added to reports. |

### 1.7 Supervised repair (opt-in)

| Feature | Detail |
|---|---|
| ✅ Run diagnoser | Categories infra / dependency / data_path / api_change / bug / semantic. Never sees claim targets. Deterministic rules override the model (semantic always goes to a human; infra and dependency never get a code patch; out-of-range or no-op fixes dropped). |
| ✅ Repair loop | Safe edits (imports, comments, path fixes) are applied automatically; anything experiment-changing opens an `action` gate with the diff; same failure twice stops the loop. |
| ✅ Versioned edits | Append-only history with diff, author, risk class and who approved. |
| ✅ Human answers | Approve, edit (human replaces the fix), reject. |
| ✅ Deviations | Agent edits always appear under "Deviations from the paper", even when a human approved them. |

### 1.8 Reporting

| Feature | Detail |
|---|---|
| ✅ Markdown report | Claims, verdicts with caveats, route, gaps, artifacts, deviations, limits. |
| ✅ Optional narrative | Report writer summary whose every number is verified against the evidence; falls back cleanly if it cannot comply (opt-in). |

### 1.9 API surface

`POST /papers` · `GET /papers` · `GET /papers/{id}` · `POST /papers/{id}/runs` (optional notebook, seeds) · `GET /runs/{id}` ·
`GET /runs/{id}/events` (SSE, resumable with `after`) · `GET /gates` · `POST /gates/{id}/respond` · `GET /papers/{id}/claims` ·
`GET /papers/{id}/audit` · `GET /reports/{paper_id}` (md/json) · `POST /workers/register` · `GET /workers` ·
`POST /workers/me/heartbeat` · `POST /workers/me/claim` · `POST /jobs/{id}/start|logs|complete|cancel` · `GET /jobs` · `GET /health`

### 1.10 Tooling

| Feature | Detail |
|---|---|
| ✅ Migrations | Alembic 0001–0004 (events, LLM calls, papers/runs/gates, workers/jobs), up and down tested. |
| ✅ Offline demo | `python -m crisisgrid_api.demo`: paper PDF to audit to 3 clean runs on a real worker to verdict to report, no keys or network. |
| ✅ Worker CLI | `python -m crisisgrid_api.worker --api URL --name NAME`. |
| ✅ Lint, types, tests | ruff, mypy, pytest for `core` and `api`; CI workflow written. |

---

## 2. Partially implemented

| Feature | What exists | What is missing |
|---|---|---|
| 🟡 Docker Compose / Dockerfile | Written | **Never run.** Postgres + pgvector branch of migration 0001 also unrun. |
| 🟡 CI | Workflow for `core`, `api`, and a non-blocking `paper_env` job | **Never seen running on GitHub.** |
| 🟡 LLM access | Gateway, LiteLLM adapter | **Adapter never run against a real provider.** Extraction quality unknown. |
| 🟡 GitHub artifact lookup | Adapter + mock-transport tests | Never run against the live API. |
| 🟡 Paper Environment (`paper_env/`) | Pre-existing module with notebook service, uv env manager, datasets, kernel runner, checkpoints, events | **Not connected to `api/`.** Missing `.ipynb` import/export, `!pip` rewrite, Redis output stream, KernelSession. **Cannot be imported on Windows**, so untested here. |
| 🟡 Attribution + fidelity | Pure functions, fully tested | **Not wired into the pipeline**: no graph persistence, no edited-version endpoint, no one-at-a-time runs, no `AttributeNode`. |
| 🟡 Worker isolation | Restricted subprocess, scrubbed env, timeout | **No Docker, no network or filesystem limits, no CPU/RAM limits.** Reports carry a lower-isolation caveat. |
| 🟡 Repair loop | Full logic, opt-in | Package installs during repair, revert endpoint, checkpoint-and-release while a gate waits. |
| 🟡 Embeddings | Hash embedder, in-memory index | pgvector storage, real embedding provider. |
| 🟡 Audit rules R01–R11 | Implemented | **My own definitions**; the v1 spec was not available. |
| 🟡 Scrubbing in notebook source | Unmistakable credential shapes refused | Loose `password = "..."` style secrets are not rejected in stored source. |
| 🟡 OpenTelemetry | Stub that does nothing | Real tracing/exporter. |

---

## 3. Pending (not built)

### Agents and pipeline stages
- ⬜ Graph builder agent (graph and notebook skeleton from a paper)
- ⬜ Env builder agent (date-based dependency resolution, torch/CUDA matching)
- ⬜ Code rebuilder agent, Tier 3 rebuild from the paper, assumptions gate (**Phase 9**, first on the plan's cut list)
- ⬜ Environment, Attribute, Escalate pipeline nodes
- ⬜ Compute guard (cost estimate, `scaled_down` labelling)
- ⬜ Gold-labelled parse evaluations on real papers

### Experiment graph and comparison
- ⬜ Persisting graph versions
- ⬜ `POST /graphs/{id}/versions`, `POST /graphs/compare`, `POST /graphs/{id}/execute`, `GET /papers/{id}/graph`
- ⬜ Version Comparison, Experiment History endpoints

### Execution and infrastructure
- ⬜ Docker sandbox runner (non-root, read-only image, limits, network allowlist, no host mounts, metadata endpoints blocked)
- ⬜ Dataset store inside `api/`: registry, content-addressed cache, resumable fetch, **checksum verification**, upstream-change detection
- ⬜ Env manager inside `api/` (create env, install, pin, rebuild, lockfile)
- ⬜ Redis streams for the queue and replayable kernel output
- ⬜ Interactive kernel sessions through the API (connect, run cell/until/all, interrupt, restart, variables inspector)
- ⬜ Checkpoint and resume, long-wait kernel release
- ⬜ Queue-wait timeout (a run waits indefinitely for a worker today)
- ⬜ Cost caps, idle timeouts, AWS budget alarm, GPU quota
- ⬜ Colab worker bootstrap, desktop (Tauri) bundle

### Security and accounts
- ⬜ **User authentication and authorization** (only workers authenticate; every user-facing endpoint is open)
- ⬜ Secrets manager (`GET/PUT /secrets`, scoped, envelope-encrypted)
- ⬜ BYO LLM provider keys (`PUT /settings/providers`)
- ⬜ Idempotency keys on mutating endpoints

### Reporting and product surface
- ⬜ PDF export of reports; evidence links in reports
- ⬜ Research Papers register filters, Mission Control summary endpoints
- ⬜ **All frontend work**: notebook UI, approval inbox, Experiment Lab, Version Comparison page, settings
- ⬜ `.ipynb` export of a run's notebook

### Demo readiness
- ⬜ A recorded, replayable "golden paper" run with a primed LLM cache (needs a real paper and a real model; the scripted demo is only a stand-in)
- ⬜ README-injection tests (the finder reads no README text, so this is unexercised)

---

## 4. What would unblock the most

1. **An LLM provider and key**: turns every "scripted" claim above into a measured one.
2. **Docker available**: verifies Compose and Postgres, and makes a real sandbox possible.
3. **A Linux run of `paper_env`**: its kernel runner could replace the subprocess executor and give real isolation, checkpoints and datasets in one move.
4. **The v1 spec for R01–R11**, and a decision on user authentication before any shared deployment.
