# paper_env: the Paper Environment module of CrisisGrid

Every research paper gets its own **PaperEnv**: a Colab-style notebook, an isolated Python environment
(uv venv + kernel), and dataset storage. Code generation, the repair agent, guardrail policies, the
approval UI and claim verification are built elsewhere and attach through the [harness seams](#harness-seams).

```
paper_env/                     (project root: pyproject.toml, demo.py, tests/)
  paper_env/                   (the importable package)
    domain/                    plain Pydantic models + enums (no persistence imports)
    repos/                     repository Protocols (interfaces.py), in-memory impl (memory.py), SQLite impl (sqlite/)
    events/                    typed event payloads + persisted, subscribable bus
    hooks.py                   PreExecuteHook, Decision, ActionGate, resume_blocked
    security/                  Scrubber, SecretsProvider (env-var impl)
    env/                       EnvManager (uv), hardware info
    datasets/                  DatasetStore, fetchers, resumable downloader, checksums
    backends/                  ComputeBackend Protocol + LocalBackend
    kernel/                    KernelRunner, output recorder, static input check, in-kernel runtime
    services/                  NotebookService, RunService, CheckpointService, PaperEnvService
    api/                       FastAPI app (thin routes) + WebSocket /events
    container.py               composition root: build_app()
```

Persistence rule: services depend on repository interfaces (`PaperEnvRepo`, `CellRepo`, `RunRepo`,
`DatasetRepo`, `EventStore`, plus `CheckpointRepo`, `BlockedActionRepo`) through a `UnitOfWork`. Only
`repos/sqlite/` imports SQLAlchemy/Alembic (enforced by `tests/test_architecture.py`).

## Quick start

```bash
uv sync
uv run python demo.py                 # full walkthrough (network only needed the first time, for uv's package cache)
UV_OFFLINE=1 uv run python demo.py    # afterwards: fully offline
uv run python -m paper_env.api        # HTTP + WebSocket API on http://127.0.0.1:8000  (docs at /docs)
uv run pytest
```

Config: `Settings` (`paper_env/config.py`) holds every limit (timeouts, output caps, cache size, free-disk
reserve, checkpoint-after-each-cell). API env vars: `PAPER_ENV_DATA_DIR`. Secrets: `HF_TOKEN`,
`PAPER_ENV_URL_TOKEN`, `KAGGLE_KEY` are read by the `EnvVarSecretsProvider` at fetch time only.

## Public Python API

Build everything with `app = build_app(Settings(...), hook=..., secrets=..., backend=...)`, then call
`app.startup()` (kills orphaned kernels, removes half-built envs) and `app.shutdown()`.

| Service | Methods |
|---|---|
| `app.paper_envs` | `create(alias, paper_hash, spec, dataset_ids)`, `get`, `get_by_alias`, `list`, `get_spec`, `set_datasets`, `delete` |
| `app.envs` (EnvManager) | `create_env(id)`, `install_package(id, name, version)`, `pin_version(id, name, version)`, `rebuild_env(id)`, `get_lockfile(id)`, `hardware_info(id)` |
| `app.notebook` | `add_cell`, `list_cells`, `source_of`, `versions`, **`apply_edit(cell_id, new_source, author)`**, **`revert_to(version_id)`**, `update_declarations`, `delete_cell` |
| `app.runs` | `run_cell(id)`, `run_until(id)`, `run_all(env_id, stop_on_fail)`, `run_clean(env_id)`, `interrupt(run_id)`, `continue_from(checkpoint_id)` |
| `app.runner` | `restart_kernel(env_id)`, `interrupt(env_id)`, `shutdown()` |
| `app.datasets` | `register(manifest)`, `fetch(id)`, `resolve(id) -> Path`, `verify(id)`, `status(id)`, `register_derived(...)`, `register_upload(...)`, `check_upstream(id)`, `reset(id)`, `preflight(ids)` |
| `app.checkpoints` | `list(env_id)`, `get(id)`, `resume_from(checkpoint_id)` |
| `app.gate` | `list_pending()`, `resume_blocked(action_id, Decision)` |
| `app.bus` | `subscribe(cb, types, run_id=...)`, `subscribe_queue()`, `history(after_seq, ...)` |

Env operations return an `EnvOpResult` (`status` = `success` / `failed` / `blocked`, `failing_package`,
`conflict_details`, `timed_out`, `log_path`, `lockfile_hash`). Packages are never installed from a cell.

### Statuses and failure kinds

| | `CellStatus` | `RunStatus` | `failure_kind` |
|---|---|---|---|
| cell raised / timed out / produced too much output | `failed` | `failed` | `experiment` |
| kernel died, OOM kill, unresponsive-after-timeout, backend/env problem, dataset preflight failure | `failed` (`Cell.last_failure_kind = infrastructure`) | `infra_failed` | `infrastructure` |
| user interrupt | `failed` (error `Interrupted`) | `cancelled` | none |
| not run because an earlier cell failed (`stop_on_fail`), the kernel was lost or the run ended | `skipped` | n/a | none |
| edited, or an upstream cell re-ran / kernel state lost | `stale` | n/a | n/a |
| paused by the pre-execute hook | `blocked` | `blocked` | none |
| run exceeded its total runtime | n/a | `timed_out` | none |

`CellRun.failure_kind`, `Run.failure_kind`, the `cell_failed` event (experiment) and the `kernel_died` event
(infrastructure) all carry the same distinction. After an infrastructure failure every other executed cell is
marked `stale`: in-memory state is never assumed to have survived. "Downstream" means *later position*
(kernel state flows top to bottom).

`verification_eligible` is true only for `run_clean` (fresh kernel, fixed seed, top to bottom) and only if it
reached a conclusion (not `infra_failed`, `cancelled`, `timed_out`, `blocked`).

Every Run records: notebook version, env lockfile hash, seed, backend, hardware info (host platform, env Python
version, CUDA/driver or `"not_detected"`, which RNG libraries were seeded), the dataset manifests used
(id + overall checksum + status), and `approximate_split`. Two runs of the same version are two rows.

## HTTP API

Route handlers are thin; JSON in/out; OpenAPI at `/docs`.

| Area | Endpoints |
|---|---|
| PaperEnv | `POST/GET /paper-envs`, `GET/DELETE /paper-envs/{id}`, `PUT /paper-envs/{id}/datasets` |
| Env ops | `POST /paper-envs/{id}/env/{create,install,pin,rebuild}`, `GET .../env/lockfile`, `GET .../env/hardware` |
| Notebook | `GET/POST /paper-envs/{id}/cells`, `GET/DELETE /cells/{id}`, `PUT /cells/{id}/source` (apply_edit), `PATCH /cells/{id}/declarations`, `GET /cells/{id}/versions`, `POST /cell-versions/{id}/revert` |
| Runs | `POST /runs` (`mode`: cell/until/all/clean; `wait=true` blocks, otherwise 202 + `run_id`), `GET /runs/{id}`, `GET /runs/{id}/cells`, `GET /paper-envs/{id}/runs`, `POST /runs/{id}/interrupt`, `POST /paper-envs/{id}/kernel/restart` |
| Datasets | `POST /datasets`, `POST /datasets/upload`, `GET /datasets[/{id}[/status]]`, `POST /datasets/{id}/{fetch,verify,check-upstream,reset}` |
| Checkpoints | `GET /paper-envs/{id}/checkpoints`, `GET /checkpoints/{id}`, `POST /checkpoints/{id}/resume` |
| Harness | `GET /blocked`, `POST /blocked/{id}/resolve` |
| Events | `GET /events/history`, `GET /events/schema`, **`WS /events?after_seq=&run_id=&paper_env_id=&types=`** |

One run per PaperEnv at a time (`409` otherwise). The service has **no authentication** and binds to
loopback by default; put it behind your own auth before exposing it.

## Event types

All events are persisted (monotonic `seq`), scrubbed of secrets, validated against a typed payload
(`GET /events/schema`), and fanned out in-process and over the WebSocket (replay from `after_seq`, then live;
at-least-once, ordered per connection: use `seq` to de-duplicate).

| Type | Key payload fields |
|---|---|
| `run_started` / `run_finished` | `run_id`, `mode`, `seed`, `notebook_version`, `lockfile_hash`, `verification_eligible` / `status`, `failure_kind`, `message` |
| `cell_started` | `cell_id`, `cell_version_id`, `cell_run_id`, `position`, `timeout` |
| `cell_output` | `cell_id`, `cell_run_id`, `type` (`stream`/`display`), `name`, `text` / `data` |
| `cell_finished` | `status`, `duration`, `state_hash`, `output_truncated`, `full_output_path`, `derived_datasets` |
| `cell_failed` (experiment) | `exception_type`, `message`, `traceback`, `failing_line`, `failing_lineno`, `cell_version_id` |
| `kernel_died` (infrastructure) | `kind` (`kernel_died`/`oom_or_killed`/`segfault`/`killed_after_timeout`/`backend_error`), `message`, `exit_code`, `state_lost`, `stale_cells` |
| `cell_edited` | `change` (created/edited/reverted/deleted), `cell_version_id`, `author`, `diff`, `stale_cells`, `notebook_version` |
| `env_changed` / `env_install_failed` | `op`, before/after `lockfile_hash` / `failing_package`, `conflict_details`, `timed_out`, `log_path` |
| `action_blocked` / `action_resolved` | `action_id`, `kind`, `decision`, `reason`, `payload` |
| `checkpoint_saved` / `checkpoint_restored` | `checkpoint_id`, `saved`, `unserializable`, `rng_saved` / `restored`, `not_restored`, `stale_cells`, `messages` |
| `dataset_fetch_started`, `dataset_fetch_progress`, `dataset_verified`, `dataset_cached`, `dataset_fetch_failed`, `dataset_upstream_changed`, `dataset_evicted` | `dataset_id` (+ `bytes_done`/`bytes_total`, `code`, `mismatches`, ...) |

`dataset_cached` means the data was fetched but no complete expected checksum existed, so it is recorded
(trust-on-first-use) and **not** reported as verified.

## Harness seams

1. **Event bus**: everything above. `app.bus.subscribe(...)` in-process, `WS /events` remotely.
2. **Pre-execute hook**: pass `hook=` to `build_app`. `PreExecuteHook.check(Action) -> Decision(allow | deny |
   needs_approval, reason)` is called before every **cell execution, package install, pin, env (re)build and
   dataset fetch**. Default `AllowAllHook`. A non-allow decision persists a `BlockedAction`, emits
   `action_blocked`, runs nothing, and leaves the cell/run in `blocked`.
   `app.gate.resume_blocked(action_id, Decision)` (or `POST /blocked/{id}/resolve`) then either executes the
   original action (a paused run continues with its remaining cells) or finalises the denial (cell `skipped`,
   run `cancelled`). A hook that raises **fails closed** (treated as deny). Runtime-owned internal
   executions (kernel bootstrap, state hashing, checkpoint I/O) are not gated.
3. **Single edit door**: `NotebookService.apply_edit(cell_id, new_source, author)` is the only way source
   changes (`Cell` has no source field). It appends a `CellVersion` (author, parent, content hash, unified diff),
   marks the cell and all downstream executed cells `stale`, and emits `cell_edited`. `revert_to` appends a new
   version with the old source; history is never rewritten.
4. **Env operations are separate APIs** from cell execution; all go through the hook and emit events.
5. **Execution is ignorant of claims**: no target metric or paper claim is read or required anywhere.

## Datasets

* Manifest `dataset_id` is conventionally `name@version`. Versions are immutable: re-registering different
  content under an existing id is a conflict.
* Content-addressed cache (`datasets/cas/<overall_checksum>/`), files `0444` / dirs `0555`. Runs get a
  per-env mount of symlinks plus a manifest snapshot; inside the kernel `datasets.resolve("cifar10@v1")`
  returns the mounted path, `datasets.info(id)`, `datasets.list()`, and `datasets.derive(name, version,
  parent=...)` returns a writable per-run directory that the host registers as a derived dataset (linked to
  its parent and producing cell version) after the cell succeeds. The helper never touches the network or credentials.
* Fetchers: HuggingFace (HTTP API, pinned to the commit sha), URL (file or base URL + files), local/upload;
  Kaggle and S3 are explicit stubs. Downloads resume with HTTP Range + `If-Range`, retry with backoff, check free
  disk first, report progress, and take a per-checksum lock (threads and processes) so concurrent fetches share one download.
* Checksum mismatch -> `corrupted`; changed upstream (fingerprint differs) -> `upstream_changed`; an event is
  emitted and **nothing is refreshed automatically**. `reset()` is the explicit operator action.
* LRU eviction never removes a dataset used by an active run, an upload, or derived data.
* Preflight (before any cell runs): unregistered / corrupted / changed / offline-and-`remote_only` datasets fail
  the run with a clear message and no kernel is started. Gated datasets take credentials from the
  `SecretsProvider` at fetch time only (request headers; never stored, logged or exposed).

## Checkpoint and resume

After each cell (opt-in per run via `checkpoint=True` or `Settings.checkpoint_after_each_cell`) the kernel
saves the declared outputs of that cell and all cells above it with `dill` (`safetensors` for torch tensors),
plus Python/NumPy/torch RNG state and import bindings. Whatever cannot be serialized is **listed**, not skipped.
`resume_from(checkpoint_id)` verifies file hashes on the host, starts a **fresh kernel**, restores in-kernel and
reports exactly what came back. Cells with unrestored outputs, cells edited since, and everything after them are
marked `stale`, and the result/event lists the reasons. Pickles are only ever loaded inside the kernel process
(`tests/test_checkpoints.py::test_host_code_never_deserializes_anything` enforces it).

## Security model and limits

* Secrets are scrubbed (registered values + token patterns + `key=value` shapes) from outputs, tracebacks,
  logs, events and DB rows. Streams are line-buffered so a secret split across chunks is still caught; a single
  line longer than 8 KB is flushed with a 256-character hold-back, so a secret straddling such a forced flush
  could slip through. Secret-looking environment variables are not passed to kernels or subprocesses.
* Tracebacks, package output and README text are stored as data only; nothing derived from them is executed
  on the host. Package names/versions are validated so they cannot be parsed as installer options.
* Kernels run as the same OS user, on loopback TCP with HMAC-signed messages. **This is not a sandbox**: a cell
  could `chmod` the dataset cache writable (the read-only protection is permissions plus `verify()`), read host files, or use the network.
  Use an isolating backend for untrusted code.
* Cells that read variables they did not declare as inputs, or try to install packages, get
  `Cell.input_warnings` (best-effort `ast` analysis; never blocks).

## Not implemented yet

* AWS and BYO-Jupyter compute adapters; only `LocalBackend` exists (the `ComputeBackend` Protocol is the seam).
* Kaggle and S3 dataset fetchers (explicit stubs that report `not_implemented`).
* Code generation, repair agent, guardrail policies, approval inbox/UI, claim verification, Colab export/import, frontend.
* Authentication/authorization on the HTTP API.
* A blocked run's remaining plan is held in memory: after a server restart a paused run cannot continue
  (the kernel session is gone as well); it is finalised honestly when resumed.
* `system_deps` are recorded in the EnvSpec but never installed.
* Windows is untested (kernel interrupt, process groups and `fcntl` locks assume POSIX).
* Output events are published per stream chunk; very chatty cells produce many event rows (no coalescing yet).
* Dependency graph analysis: staleness uses cell position, not declared inputs/outputs.
