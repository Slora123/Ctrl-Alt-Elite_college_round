# CrisisGrid

**Making AI research more transparent, traceable, and reproducible.**

CrisisGrid is a planned AI-powered research reproducibility platform designed to help researchers analyze AI/ML papers, reconstruct experiments, assess published claims, and understand how experimental changes affect evaluation results.

## Overview

Reproducing AI/ML research can be difficult because papers may contain incomplete documentation, missing configurations, inaccessible datasets, or inconsistent evaluation procedures.

CrisisGrid aims to bridge this gap by connecting research papers, experimental methodologies, configurations, evaluation results, and supporting evidence in a traceable workflow.

## Planned Features

- **Research Understanding:** Analyze research papers and supporting artifacts to identify methodologies and experimental details.
- **Claim Verification:** Help assess whether published findings can be independently reproduced.
- **Experiment Reconstruction:** Convert documented methodologies into structured experiment workflows.
- **Version Comparison:** Compare experiment versions and identify changes associated with different evaluation results.
- **Research Auditing:** Identify methodological inconsistencies, missing information, and reproducibility risks.
- **Evidence-Based Reporting:** Present findings, limitations, comparisons, and supporting evidence.
- **Experiment Traceability:** Track experiment configurations, versions, results, and changes.

*These are planned capabilities. Their availability depends on the implementation.*

## Technology Stack

The specific technologies depend on the implementation. The current website uses the following frontend technologies:

- React
- Vite
- JavaScript
- CSS
- npm

Additional backend services and dependencies may be required for planned platform functionality.

## Project Structure

```text
CrisisGrid/
└── website/
    ├── index.html
    ├── package.json
    ├── package-lock.json
    ├── README.md
    └── src/
        ├── App.jsx
        ├── Landing.jsx
        ├── Dashboard.jsx
        └── landing.css
```

The structure above represents the main website files and may not include every project directory.

## Run the whole system

```bash
python scripts/run_all.py            # website :5173, website API :8000, CrisisGrid API :8100 (incl. notebook environment)
python scripts/run_all.py --help     # --no-auth, --worker, --terminal, --llm-chain/--llm-key
```

Open http://localhost:5173/ and create the first account (it becomes the administrator). Architecture, authentication and
known limits: [docs/INTEGRATION.md](docs/INTEGRATION.md).

## Getting Started

### Prerequisites

- Node.js
- npm

### Installation

1. Navigate to the website directory:

   ```bash
   cd website
   ```

2. Install dependencies:

   ```bash
   npm install
   ```

3. Start the development server:

   ```bash
   npm run dev
   ```

4. Open the local URL displayed in your terminal.

### Production Build

To create a production build:

```bash
npm run build
```

To preview the production build locally:

```bash
npm run preview
```

## Research Workflow

The planned CrisisGrid workflow is:

1. Analyze a research paper and its supporting artifacts.
2. Extract the methodology, dataset, model configuration, and evaluation procedure.
3. Reconstruct the experiment as a structured workflow.
4. Assess published claims against available experimental evidence.
5. Compare experiment versions and investigate differences.
6. Generate transparent reports documenting findings and limitations.

Actual reproducibility depends on the availability of data, code, dependencies, hardware, and sufficient experimental details.

## Responsible Reporting

CrisisGrid aims to distinguish verified findings from assumptions, missing evidence, and illustrative demonstrations. Demonstration data must not be presented as actual experimental verification.

## Project Status

CrisisGrid is under development. Features described in this README represent the project's intended direction and should not be interpreted as completed functionality unless implemented and tested.

## Contributing

Contributions that improve research transparency, experiment traceability, reproducibility, accessibility, and usability are welcome.

## License

A license has not yet been specified. Add a suitable license before distributing the project publicly.


## Backend (Python)

The reproducibility engine lives beside the website. It follows `implementation_plan.md`, `architecture.md` and `system_design.md`.

| Directory | What it is |
|---|---|
| `core/` | Pure logic, no I/O: shared models, experiment graph service, **claim verifier**, harness guard (loop/budget/repair firewall), audit rules R01-R16, route selector, job state machine, **action hook**, change attribution, report checks, secret scrubber |
| `api/` | FastAPI app, model gateway (provider chain, cache, audit log), Tier 1 agents (parser, artifact finder, gap auditor), persisted pipeline with human gates, SSE stream, job queue, outbound-only workers, repair loop, offline demo |
| `paper_env/` | Colab-style notebook + isolated Python env + datasets module (Linux/macOS only; not yet connected to `api/`) |
| `docs/decisions.md` | Every deviation, assumption and known gap, numbered. Read this before relying on anything |

### Try it without any keys or network

```bash
cd api
python -m venv .venv && .venv/bin/pip install -e ../core -e . pytest   # Windows: .venv\Scripts\...
python -m crisisgrid_api.demo     # paper PDF -> audit -> 3 clean runs on a real worker -> verdict -> report
python -m pytest                  # api tests;  cd ../core && python -m pytest  for the core tests
```

### Run it

```bash
docker compose up --build         # Postgres+pgvector, Redis, MinIO, API (see docs/decisions.md D-11: compose is untested)
CG_LLM_CHAIN="groq/qwen/qwen3-32b" CG_LLM_API_KEY=... uvicorn crisisgrid_api.main:app   # without CG_LLM_CHAIN, reading works but runs are refused
python -m crisisgrid_api.worker --api http://localhost:8000 --name my-laptop            # a worker connects OUT to the API
```

### What is and is not real

Real and tested: the verifier, graph hashing, audit rules, quote verification (no hallucinated hyperparameters survive), gates that
survive a restart, the job queue with infrastructure-only retries, clean-run execution, secret scrubbing, the repair loop with
diff-based risk classification, number-checked report narratives.

Not done or not verified (details in `docs/decisions.md`): any real LLM call (the LiteLLM adapter has never run against a provider),
Docker/Postgres (tests use SQLite), Docker sandboxing (workers are restricted subprocesses with **no network/filesystem limits**),
user authentication, the Graph/Env/Code-rebuilder agents, graph persistence and Version Comparison endpoints, PDF export, the
notebook UI, and `paper_env` integration.
