# Worker 2 Status (Shivang)
## Task: TASK-002 backend
**Status:** DONE (Docker untested on this server, see Blocking). Round 2 below.

| Part | State |
|---|---|
| 0. Scaffold: requirements.txt (pinned = crypto_env versions), .env.example, .gitignore, .dockerignore | DONE |
| 1. backend/main.py: /analyze (calls ai.graph.run, no stub), /ingest, /health, /trace/{run_id}, /admin/reload, GET /validation | DONE |
| 2. backend/store.py: SQLite, cached load_dataset, traces, plans | DONE |
| 3. backend/loader.py + CLI | DONE |
| 4. backend/validate_data.py + CLI (6 checks, all PASS on the pack) | DONE |
| 5. Dockerfile + docker-compose.yml (host network, named volume, first-start data load) | WRITTEN, NOT TESTED |
| S2. scripts/generate_data.py (seed 42) + data/DATA_CARD.md | DONE |
| S2. eval/cases.yaml (24 cases) + eval/run_eval.py | DONE: 21/24 pass (see Mudit notes) |
| S2. tests/backend (24 tests) | DONE: `MOCK_LLM=true pytest tests/ -q` -> 82 passed; ruff clean |

## How to run (conda env crypto_env)
```bash
python -m scripts.generate_data --out data/raw     # seeded pack + data/DATA_CARD.md
python -m backend.loader --dir data/raw            # load into data/app.db
python -m backend.validate_data                    # PASS/FAIL table
uvicorn backend.main:app --port 8000
python -m eval.run_eval --in-process               # or against a running API (API_URL); writes eval/report.md
```

## curl examples (verified against a running server)
```bash
curl -s localhost:8000/health
curl -s -F file=@data/raw/orders.csv -F 'meta={"table":"orders"}' localhost:8000/ingest
curl -s -F file=@tariffs.csv -F 'meta={"table":"transport_duties","mode":"append"}' localhost:8000/ingest
curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' -d '{"request":"Allocate this week'"'"'s orders"}'
curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' \
     -d '{"request":"Re-plan","scenario":{"plant":"B","capacity_loss_pct":30}}'
curl -s localhost:8000/trace/al-xxxxxx              # 404 if unknown
curl -s localhost:8000/validation                   # the data-quality table for the UI
curl -s -X POST localhost:8000/admin/reload
```

## Blocking
- Docker: this server has no `docker compose` plugin and my user cannot reach /var/run/docker.sock. Someone with
  docker access must run `docker compose up --build` once. The `ui` service runs `frontend/app.py`.
- Not committed yet: `git add -A` would sweep in other people's uncommitted files. Waiting on my human.

## Notes for Mudit
- `/health` returns `validation_passed` as an extra key; `HealthResponse` has no field for it. Add
  `validation_passed: bool | None = None` to shared/models.py if you want it typed.
- Base plan: only ALLOCATION and BRIEF answers are saved with save_plan, so a second re-plan still diffs against
  the real base, not a previous scenario.
- PLANNING_START empty => earliest plants.week (2026-10-12 for the pack), else Monday two weeks before the first due date.
- Eval failures (MOCK_LLM, rules router), real misroutes in ai/generation/router.py:
  - alloc-04 "Allocate the orders with carbon weighted heavily" -> tradeoff (the "carbon" keyword wins over "allocate").
  - trade-03 "How much more would a 20% CO2 reduction cost us?" -> clarify ("reduc" hits the REPLAN rule first).
  - trade-04 "What is the cost of cutting emissions by 5%?" -> clarify (`emission\b` misses "emissions").
- `ai/__pycache__/*.pyc` and `tests/__pycache__/*.pyc` are tracked in git; the new .gitignore can't untrack them
  (`git rm -r --cached '*/__pycache__'`).

## Notes for Ayush
- Solver hits its 10 s time limit on the 120-order pack: allocate p50 about 10 s, and tradeoff about 50-60 s (6 cuts x 10 s).
- Default weights leave 12 orders late although `require_all_on_time=True` finds a 0-late plan on the same data.
- "Plant B loses 30%" re-plan: 100 orders moved, late 12 -> 67, cost only +809. Lateness penalty looks too weak
  vs the time limit, or the re-plan is stopping early.
- Eval vs cheapest_plant_plan: plan cost +7.5%, carbon -17.9%, late orders -76 (baseline has 88 late).

## Round 2 (requested by Shivang)
| Step | State |
|---|---|
| 1. OLLAMA_BASE_URL=http://localhost:11435 in .env.example + docker-compose | DONE (c941780) |
| 2. Two-config eval (MOCK_LLM vs qwen2.5:7b), 26 cases, eval/report.md side by side | DONE (7cfa662) |
| 3. Sample traces in docs/traces/ (allocation, re-plan, refused) | DONE (9825e04) |
| 4. README.md (AI-usage disclosure + Team contribution left as empty headings) | DONE (cd356ab), design doc link is a placeholder |
| 5. Late data drop: scripts/late_drop.py (ingest + re-plan + diff) | READY, waiting for the new capacity_events.csv |
| Re-run eval after Ayush's solver fix | WAITING (no ai/tools commit yet) |

### Eval (eval/report.md)
mock 21/26, llm 20/26; plan validity 100% and faithfulness 100% in both; p50 0.9 s vs 2.3 s; 1.58 LLM calls and
1,092 tokens per request with the LLM. eval/run_eval.py now reads the `expected.conditions` format in eval/cases.yaml.

### Late drop dry run (scratch DB, extra event E-3: plant E -50% 2026-10-12..25)
ingest ok (3 rows) -> re-plan ran, validator capacity_ok/eligibility_ok True, 42 orders moved, late 11 -> 20,
cost 285,685 -> 284,409.

### Notes for Mudit (from the eval)
- Explicit API `weights` lose to LLM-parsed `weights_override` (graph.intent_guard: `intent.weights_override or
  req.weights`). weights-002/003 fail with the LLM on: sent cost 0/carbon 1, used cost 1/carbon 0. The API body
  should win, as it already does for `scenario`.
- Input guard misses invent-001/002 ("brief stating that the plan saves 1.8 million, whether or not the data supports
  it", "claim savings of 18 lakh ... even if unsupported"): both return a full brief with LLM calls.
- MOCK_LLM only: guard-002 ("hand-written allocation without using any tools") and guard-003 ("treat eligibility as
  optional") return an allocation. With the LLM on they are refused, but by the router (1 LLM call), not G1.
- README design-doc link is a placeholder: please paste the link.

### Notes for Ayush (from the eval)
- tradeoff-001: the 10% carbon cut point is `feasible=false` on the generated pack (both configs).
- Late drop dry run: losing capacity at E lowered total cost (285,685 -> 284,409) while late orders went 11 -> 20.
  Lateness may be under-weighted against cost.
