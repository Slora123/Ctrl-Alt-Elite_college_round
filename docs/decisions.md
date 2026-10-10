# Decisions and deferred items

## Decided

- **D-01 (P1) Location.** The P1 code lives in `core/` (package `crisisgrid_core`), a pure-Python project with no persistence, web or LLM imports (enforced by `core/tests/test_architecture.py`). The Node/Express website backend is kept as is for now; migrating it to the Python API is deferred to P4+.
- **D-03 (P1) Interactive runs are rejected, not filtered.** `verify_claim` raises `IneligibleRunError` if any offered `RunResult` is not a clean, verification-eligible run. `RunResult` itself refuses `verification_eligible=True` with `mode="interactive"`. The pseudo-code in system_design 5.2 filters silently; raising is stricter so caller bugs surface.
- **D-04 (P1) Claim comparison is in the claim's own unit.** The verifier compares `metrics[claim.metric]` to `reported_value` and `tolerance` directly. Percent vs fraction conversion is not guessed; a unit mismatch is the Audit rules' job (R-series) and reaches the verifier as `metric_mismatch` (verdict `inconclusive`).
- **D-05 (P1) Hashing.** Floats with integral values fold to ints (`1.0 == 1`); NaN/inf in a spec is an error. A bound cell's code is hashed via `ast.dump` (comments/whitespace ignored), with a comment/whitespace-stripping fallback for source containing notebook magics.
- **D-06 (P1) Edited-node provenance.** `create_version` sets edited nodes to `human` (author human) or `inferred` (author agent), per the Author-to-Provenance mapping.

## Deferred

- **D-02 Reconcile Paper Environment models.** `core.models` implements system_design 1.1 as written. `paper_env/paper_env/domain/models.py` already has local versions with extra fields (e.g. `Run`, `CellRun.blocked_action_id`, `Checkpoint.cell_versions`) and different shapes. They are not merged yet; decide in P5/P6 which is canonical and whether `paper_env` imports from `core`.
- **D-07 Attribution, Audit rules, Harness guard firewall test.** Not in P1. The firewall test lands with the first agent (P3).
- **D-08 Postgres/Alembic/UUIDv7 in repos.** `uuid7()` exists in `core/ids.py`; `paper_env` still uses `uuid4` hex IDs and SQLite. Moves with P0.
- **D-09 P0 not done.** No Docker Compose, CI, or monorepo `api/`, `orchestration/`, etc. yet.
