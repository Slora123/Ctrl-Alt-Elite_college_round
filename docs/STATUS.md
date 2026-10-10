# CrisisGrid backend: status

What is built, what is only partly built, and what is not built, measured against `implementation_plan.md`.
Detailed reasoning for every deviation is in [decisions.md](decisions.md) (D-01 to D-36).

**How "done" is used here:** *Done* means built and covered by passing tests. It does **not** mean run against real infrastructure.
Test results at the tip of `feat/p10-hardening-demo`: `core` 284 passed, `api` 140 passed, 1 skipped (needs numpy). Lint and type checks clean.
Everything was tested on **SQLite, Windows, Python 3.13, with a scripted stand-in for the LLM**.

## Phase by phase

| Phase | Status | Notes |
|---|---|---|
| P0 Foundations | **Mostly done** | API with `/health`, config, JSON logging, Alembic migrations, event log, repository base, CI workflow. **Not verified:** `docker-compose.yml`, Dockerfile, Postgres/pgvector migration branch, and the CI run on GitHub. The OpenTelemetry piece is a no-op stub. No monorepo dirs for components that do not exist yet. |
| P1 Core models, graph, verifier | **Done** | Models, graph hashing/diff/versioning/validation, claim verifier with all preconditions. An interactive run is rejected (raises). |
| P2 Tier 1 audit | **Done, with caveats** | PDF ingest, quote-verified parser, artifact finder, gap auditor, audit rules R01-R16, end-to-end on a generated PDF. **R01-R11 are my own definitions** (v1 spec not provided). No gold-labelled evals on real papers, no OCR engine (scanned PDFs are flagged `needs_ocr`), no pgvector storage (in-memory index only). |
| P3 Gateway + Harness guard | **Done, with caveats** | Provider failover, backoff, cooling, cache, audit log, typed output with one retry, attempt/token/time budgets, loop detection, repair firewall. **The LiteLLM adapter has never run against a real provider.** |
| P4 Pipeline, gates, SSE | **Done** | Persisted state machine, all five gate kinds, REST API, SSE, restart-mid-wait test. Not built on `pydantic-graph` (own state machine instead). |
| P5 Paper Environment | **Pre-existing, not audited by tests** | `paper_env/` was already in the repo. Audited by reading against the 12-step list: most steps present; **missing** `.ipynb` import/export, `!pip` rewrite, Redis output stream, `KernelSession` record. **Cannot be imported on Windows (`fcntl`)**, so its tests were not run and it was not modified. Not connected to `api/`. |
| P6 Worker, queue, clean runs | **Done, with a serious limit** | DB-backed queue (not Redis), atomic claim, capability matching, heartbeat reaper (now scheduled), infra-only retries, outbound-only workers with tokens, clean runs per seed, verdicts. **No Docker sandbox**: workers are restricted subprocesses with **no network or filesystem limits**. |
| P7 Action hook + repair loop | **Backend done, UI not built** | Diff-based risk classification, Run diagnoser, `action` gates (approve/edit/reject), versioned edits, deviation list. Repair is **off by default**. Package installs during repair and a revert endpoint are not built. |
| P8 Attribution + report | **Partly done** | Attribution, noise floor, residual, confound flags, report number post-check, fidelity summary, checked narrative: all built and tested **as pure functions**. **Not wired into the pipeline** (no `AttributeNode`, no graph persistence, no Version Comparison endpoints). Markdown report only, **no PDF export**. |
| P9 Code rebuilder (Tier 3) | **Not built** | First on the plan's cut list. Needs the Graph builder first. |
| P10 Hardening + demo | **Partly done** | Done: credential refusal, secret-leak sweep across DB/events/gates, injection tests, scheduled reaper, offline demo (`python -m crisisgrid_api.demo`). **Not done:** cost caps, idle timeouts, AWS budget alarms, recorded golden-paper run with primed LLM cache, README-injection tests. |

## The never-cut list from the plan

| Item | Status |
|---|---|
| Claim verifier | Done |
| Clean-run rule (only clean runs are evidence) | Done, enforced in the model, verifier, attribution and execute node |
| Firewall (executors never see claim targets) | Done, tested at job enqueue and for the diagnoser |
| Action hook interface | Done |
| Secret scrubbing | Done for logs, errors, events, gates, run state, DB. Only unmistakable credential shapes are refused inside notebook source |
| Dataset checksums | **Not done in `api/`**. Exists only inside the untested `paper_env/` |
| Tier 1 | Done |

## Not built at all

- Real LLM runs of any agent, and measured extraction quality
- Graph builder, Env builder, Code rebuilder agents
- Graph persistence, `POST /graphs/*`, Version Comparison, Experiment History endpoint
- User authentication and authorization (only workers authenticate)
- Notebook UI, approval inbox UI, Experiment Lab (frontend)
- Docker sandbox, network allowlist enforcement, per-job resource limits
- Dataset store and checksum verification inside `api/`
- Secrets manager (`GET/PUT /secrets`) and BYO provider-key settings endpoint
- Checkpoint-and-release while a gate waits; queue-wait timeout
- Tauri desktop app, Colab worker bootstrap

## Needs a human decision or an environment I did not have

1. An LLM provider and key, to run the agents for real.
2. Docker, to verify `docker compose up --build` and `/health` on Postgres.
3. A Linux machine or CI run, to execute `paper_env`'s tests.
4. The v1 spec for audit rules R01-R11 (to replace my definitions).
5. How `paper_env` should connect: its kernel runner as the worker's executor (gives real isolation) is the obvious next step.

## Git state

Nothing is merged into `main`. Each phase is a branch, stacked on the previous one, all pushed and matching the remote:
`p1-core-models-graph-verifier` → `p0-foundations` → `p3-gateway-harness` → `p2-tier1-audit` → `p4-pipeline-gates-sse` (also holds P6) →
`p8-attribution-report` → `p7-action-hook-repair` → `p10-hardening-demo` (tip, contains everything).
The branch names `p0`..`p10` are all prefixed `feat/`. One PR from the tip into `main` merges the lot.
