# Plugging in the real AI/ML

All AI behaviour lives in `ai_mock.py`. `main.py` only calls these functions, so the
AI/ML team can replace the bodies without touching the API or the frontend:

| Function | Used by endpoint | Returns |
|---|---|---|
| `understand(title, text, analysis)` | `POST /api/ai/papers/{id}/understand` | summary, key_points, model_family, missing, confidence |
| `extract_claims(text, title)` | `POST /api/ai/papers/{id}/extract-claims` | `[(statement, reported_metric 0..1)]` |
| `reproduce_claim(statement, reported, salt)` | `POST /api/ai/claims/{id}/reproduce` | reproduced, runtime_s, note |
| `run_experiment(config)` | `POST /api/ai/items/versions/{id}/run` | metric, curve[], runtime_s |
| `audit(text, analysis)` | `POST /api/ai/papers/{id}/audit` | list of finding dicts |

Items created by the mock are tagged `"source": "mock-ai"` and show a MOCK AI tag in the UI.
When real models replace them, set `"source"` to something else (or drop it) and the tag disappears.
