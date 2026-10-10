# CrisisGrid

## Pages
`/` is the animated landing page (`src/Landing.jsx`, styles in `src/landing.css`). **Skip intro** and **Explore the vision** both go to the dashboard at `/#/mission`. The sidebar logo links back to the landing page. Run `npm install` after pulling this change: it adds `three` for the 3D desk scene.

## Run (one command)
```
npm install
npm run dev
```
`npm run dev` starts the FastAPI backend (creates `backend/.venv` and installs Python deps on first run), waits until it is healthy, then starts Vite at http://localhost:5173. API docs: http://127.0.0.1:8000/docs

Needs Node 18+ and Python 3.10+. Other scripts: `npm run dev:api` (backend only), `npm run dev:web` (frontend only; the API must already be running).

## The "ECONNREFUSED 127.0.0.1:8000" error
It only means the backend was not running. `npm run dev` fixes that by starting both.

## AI/ML status: MOCK
All AI behaviour (paper analysis, claim extraction, "reproduce", experiment runs, AI audit) comes from `backend/ai_mock.py`, which returns simulated, deterministic results tagged **MOCK AI** in the UI. The AI/ML team replaces the function bodies there; see `backend/AI_INTEGRATION.md`.

## Data
SQLite at `backend/crisisgrid.db`, uploads in `backend/uploads`. A demo paper is created on first run (set `CG_SEED=0` to disable, or delete it in the UI).
