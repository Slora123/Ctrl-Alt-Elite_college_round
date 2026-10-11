# How the pieces fit together

```
 browser ──► website (React/Vite :5173) ──┬─ /api  ──► website API (Node/Express :8000): legacy, no page calls it any more
                                          └─ /cg   ──► CrisisGrid API (FastAPI :8100)
                                                         ├─ /auth/*        accounts, sessions, API tokens   (the ONE login)
                                                         ├─ /papers /runs /gates /reports …   the pipeline
                                                         ├─ /workers /jobs                    outbound-only workers
                                                         └─ /paper-env/*   Paper Environment (mounted in-process)
                                                              ├─ REST + WebSockets (events, terminal)
                                                              └─ /paper-env/ui/   the notebook UI (embedded by the website)
 worker  ──(outbound HTTP)──► CrisisGrid API ;  executor = subprocess driver  OR  paper_env kernels (--executor paper_env)
```

Start everything: `python scripts/run_all.py` (see the script's header for flags). State lives in `.cg/`.

Python packaging: `core`, `api` and `paper_env` form **one uv workspace** (root `pyproject.toml` + `uv.lock`). `uv sync` anywhere in the repo
creates a single `.venv` with all three installed editable; `crisisgrid-api` depends on `crisisgrid-core` and `paper-env` through
`[tool.uv.sources] … workspace = true`.

## Authentication (single sign-on across all three backends)

* **Accounts** live in the CrisisGrid API (`users`, `api_tokens`; Alembic revision 0005). Passwords are scrypt-hashed;
  repeated failures lock an account for 15 min; sign-in/registration are rate limited per IP; unknown emails cost the same
  time as wrong passwords. The first account becomes the administrator; later registration can be closed
  (`CG_ALLOW_REGISTRATION=0`).
* **Sessions** are HS256 JWTs signed with `CG_AUTH_SECRET` (12 h). Changing the password or "sign out everywhere" bumps the
  user's `token_version`, which invalidates older tokens at the FastAPI/paper_env side. The Node backend verifies signature
  and expiry only (so a revoked *session* token stays valid there until it expires, at most 12 h); **API tokens** (`cgt_…`)
  are checked against the API every 30 s, so revocation is prompt.
* **Roles:** `viewer` (read-only), `researcher` (create/run/edit their own work), `admin` (everything, user management).
* **Ownership:** papers (both backends) and paper environments belong to their creator; other users get 404/403. Admins see
  everything. Anything created while authentication was off (`owner_id` NULL) is visible to all signed-in users.
* **Secret handling:** production must set `CG_AUTH_SECRET` (≥16 chars) and run behind HTTPS. In dev a random secret is
  generated once (`.cg/auth_secret`). `CG_AUTH_ENABLED=0` / `--no-auth` runs everything as a local administrator.
* Workers keep their own tokens (unchanged). The browser keeps the session token in `localStorage["cg.token"]`; the
  embedded notebook UI reads the same key (same origin). WebSockets and file downloads pass `?token=` because browsers cannot set
  headers there; the pipeline's SSE stream accepts it too. **Never log query strings in front of this service.**

## Notebook ⇄ pipeline

* The website's **Pipeline** page drives the real API: upload PDF → audit → route → clean runs → verdicts, with gates.
* A worker started with `--executor paper_env` runs each clean-run job in a managed uv environment and a real Jupyter
  kernel (`worker/paper_env_executor.py`): seeded RNGs, timeouts, kernel death classified as *infrastructure*, lockfile hash
  as environment identity, metrics via `report_metric(name, value)`. It still advertises `isolation="subprocess"`: kernels are
  **not** a sandbox.
* "Open notebook" on a paper creates (or opens) the paper environment `paper-<id>`.

## Known limits of this integration

* **Every dashboard page now talks to the FastAPI** (`/cg`): Papers, Paper Understanding, Artifacts, Claims, Audit, Reports, History,
  Mission Control, Settings, plus Pipeline/Notebook/Account. The mock AI engine (`ai_mock.js`, `/api/ai/*`) was deleted. The Node API
  still exists with its old papers/items/graph routes, but nothing in the website calls it (browser E2E asserts zero `/api` requests);
  it can be retired. Experiment Lab, Graph and Version Comparison read `/papers/{id}/graph*` (graph versions in the `graph_versions` table).
* `paper_env` needs POSIX (`fcntl`, `pty`); on Windows `/paper-env` is simply not mounted (the rest works).
* Domain models are still separate (`core` vs `paper_env.domain`, uuid7 vs uuid4); reconciliation (their D-02/D-08) is open.
* Not production-ready: no sandbox, no HTTPS/CSRF hardening beyond bearer tokens, no email flows (password reset), no
  monitoring; LLM agents, Docker/Postgres and CI have never run. See `STATUS.md` / `FEATURES.md` (their notes about
  `paper_env` predate this work: it now has tests, dataset checksums, a notebook UI, `.ipynb` import/export and the `!pip`
  rewrite).
