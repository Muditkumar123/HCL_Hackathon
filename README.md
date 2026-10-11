# Multi-Plant Order Allocation with Cost and Carbon

HCLTech hackathon project. A planner asks in plain English ("Plant B loses 30% capacity. Re-plan.") and gets
back a validated allocation of orders to plants and production weeks, with the cost, carbon and lateness numbers
behind it, a trace of every step, and a plan that waits for management approval.

**The core rule:** every number, plan and ranking comes from code (the cost model, the OR-Tools solver and an
independent validator). The LLM only parses the request and words the answer, and a guard checks every number
it writes against the solver's facts.

Design doc: **[link to be added by Mudit]**. Decision log: [DECISIONS.md](DECISIONS.md).

## Architecture

```
 Streamlit UI (frontend/app.py) ──httpx──► FastAPI (backend/main.py)
                                              │  /analyze /ingest /health /trace/{run_id}
                                              ▼
                     LangGraph flow (ai/graph.py), a fixed DAG, not a ReAct agent
   input_guard (G1) → parse_intent (LLM #1, JSON schema; rules fallback) → intent_guard (G2)
     → allocate | tradeoff | replan | explain | brief      (tools only: ai/tools/)
     → checker (G3: validate_plan gate, builds FACTS)
     → write (LLM #2) → output_guard (G4: number faithfulness, 1 retry, then template)
     → finalize (G5: approval flags, recommendations, trace)
                                              │
        ai/tools/: cost_model · solver (OR-Tools MIP, SCIP) · validator (independent) · scenarios · baseline
                                              │
                          SQLite (backend/store.py): 4 data tables, traces, base plans
```

| Folder | What | Owner |
|---|---|---|
| `shared/models.py` | The contract: every request, response, tool output and trace | Mudit |
| `ai/graph.py`, `ai/generation/`, `ai/llm.py`, `frontend/` | Flow, guards, prompts, LLM access, UI | Mudit |
| `ai/tools/` | Cost model, solver, validator, scenarios, baseline | Ayush |
| `backend/`, `scripts/`, `eval/`, Docker files | API, store, loader, data checks, data generator, eval | Shivang |

## Setup

```bash
conda activate crypto_env            # Python 3.10 for dev (Docker uses 3.11)
pip install -r requirements.txt      # only if the env is new; versions are pinned
cp .env.example .env                 # then edit; .env is never committed
```

`.env` keys: `LLM_PROVIDER=ollama`, `LLM_MODEL=qwen2.5:7b-instruct`, `OLLAMA_BASE_URL=http://localhost:11435`
(Ollama on GPU 1, model kept loaded), `MOCK_LLM=false`, `DB_PATH=data/app.db`, `PLANNING_START=` (empty: derived
from the data), `API_URL=http://localhost:8000`. Set `MOCK_LLM=true` to run without any LLM (rules router +
template text).

## Run

```bash
python -m scripts.generate_data --out data/raw     # seeded synthetic pack + data/DATA_CARD.md
python -m backend.loader --dir data/raw            # load the CSVs into SQLite (bad rows are reported)
python -m backend.validate_data                    # data-quality PASS/FAIL table (6 checks)
uvicorn backend.main:app --port 8000               # API
API_URL=http://localhost:8000 streamlit run frontend/app.py   # UI on :8501
```

Docker (Ollama stays on the host; both services use the host network):

```bash
docker compose up --build            # api on :8000, ui on :8501; first start generates and loads the data
```

Tests and eval:

```bash
MOCK_LLM=true pytest tests/ -q
MOCK_LLM=true  python -m eval.run_eval --in-process --label mock
MOCK_LLM=false python -m eval.run_eval --in-process --label llm
python -m eval.run_eval --report mock llm          # writes eval/report.md
```

Late data drop (for example, a new `capacity_events.csv`): ingest it and re-plan in one step:

```bash
python -m scripts.late_drop data/raw/capacity_events.csv   # against the running API
```

## API

| Endpoint | Does |
|---|---|
| `POST /analyze` | `AnalyzeRequest` (`request`, optional `weights`, optional `scenario`) → `AnalyzeResponse` |
| `POST /ingest` | multipart `file` (CSV) + `meta` JSON `{"table": "...", "mode": "replace" or "append"}` → `IngestResponse` |
| `GET /health` | DB status, row counts, LLM status, `validation_passed` |
| `GET /trace/{run_id}` | Full `TraceRecord` for a run, or 404 |
| `GET /validation`, `POST /admin/reload` | Data-quality table for the UI; reload `data/raw/` |

```bash
curl -s localhost:8000/health

curl -s -F file=@data/raw/orders.csv -F 'meta={"table":"orders"}' localhost:8000/ingest

curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' \
     -d '{"request": "Allocate this week'"'"'s orders"}'

curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' \
     -d '{"request": "Re-plan", "scenario": {"plant": "B", "capacity_loss_pct": 30}}'

curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' \
     -d '{"request": "What does a 10% carbon cut cost?", "weights": {"cost": 0.7, "carbon": 0.3}}'

curl -s localhost:8000/trace/al-52d447              # 404 if the run_id is unknown
```

Sample traces (qwen2.5:7b, pack v2):
- [Allocation](docs/traces/allocation_al-52d447.json): the guard rejects the first LLM draft (an unsupported claim)
  and the retry passes.
- [Plant B loses 30%, re-plan](docs/traces/replan_scenario_al-481c5d.json): both LLM drafts fail the number guard,
  so the code-written template is shown.
- [Refused prompt injection](docs/traces/refused_al-c0b937.json): blocked by the input guard, with 0 LLM calls.

## Data

Synthetic only, pack v2 (`python -m scripts.generate_data`, seed 42): 120 orders, 6 products, 10 destinations,
5 plants, 4 production weeks from 2026-10-12, 50 routes and 2 capacity events. Traps are built in on purpose: a
cheapest plant that is late, a single-plant product, a duty that flips the cheapest plant, a low-carbon plant with
low capacity (hydro plant C, 350 units/week), weekly carbon intensity, and capacity events.

Pack v2 gives three plants moderate spare capacity and puts two of them on a green tariff at a 10-12% price
premium, so cutting carbon has a real price. On this pack the solver finds:

| carbon cut | cost vs 0% | late orders |
|---|---|---|
| 0% | 311,029 | 0 |
| 5% | +3,894 | 6 |
| 10% | +6,800 | 13 |
| 14.5% (deepest feasible) | +6,955 | 49 |
| 15% and above | not feasible within capacity | |

Pack v1 ran at 86-96% utilisation, and its deepest feasible cut was about 5%. See [data/DATA_CARD.md](data/DATA_CARD.md).

## LLM decision

**qwen2.5:7b-instruct via Ollama, on a local GPU (GPU 1, port 11435), kept loaded (`keep_alive=-1`).**
- The LLM has two narrow jobs: turn the request into a `ParsedIntent` (constrained to our JSON schema) and turn
  code-computed FACTS into prose. Neither needs a large model, because every number comes from the solver.
- **Why not Qwen3:** our cached Qwen3-8B (Hugging Face) could not be converted by this Ollama version
  (`Qwen3ForCausalLM` architecture unsupported). Qwen3 also defaults to thinking mode, which needs a workaround for
  `<think>` blocks and adds latency. qwen2.5:7b is listed in the guide and follows the schema without that.
- Kept loaded so the first request after a pause (for example, in the demo) does not pay the model load time.
- One switch (`LLM_PROVIDER` / `LLM_MODEL`) changes the model; `MOCK_LLM=true` runs with no model at all.

## Eval results

Two suites, two configurations, same data (pack v2) and solver; only the language layer changes. The run used a
clean checkout of ef68ace. Full per-case results and every failing condition are in [eval/report.md](eval/report.md).
- **Team cases** ([eval/cases_team.yaml](eval/cases_team.yaml), 24): written by us. They check the answer type, and
  the carbon-cut cases check the answer either way: a feasible cut must cost more and emit less, and an infeasible
  one must quote the deepest feasible cut.
- **Independent auditor cases** ([eval/cases.yaml](eval/cases.yaml), 26): written by Codex as an independent
  auditor, with machine-checked conditions (format: `.coordination/reviews/EVAL-FORMAT.md`). Conditions fail closed.

| metric | Team: MOCK_LLM | Team: qwen2.5:7b | Auditor: MOCK_LLM | Auditor: qwen2.5:7b |
|---|---|---|---|---|
| Cases passed | 24/24 | 24/24 | 24/26 | 24/26 |
| Answer-type (intent) accuracy | 100% | 100% | 92% | 100% |
| Condition checks passed | 100% | 100% | 97% | 97% |
| Plan validity | 100% | 100% | 100% | 100% |
| Faithfulness of the shown text | 100% | 100% | 100% | 100% |
| Router fallback to rules | n/a | 0% | n/a | 0% |
| Text from template | 100% | 17% | 100% | 25% |
| Number-guard retries / template after 2 fails | 0 / 0 | 10 / 3 | 0 / 0 | 10 / 4 |
| Latency p50 / p95 | 1.2 s / 6.9 s | 3.7 s / 9.3 s | 0.1 s / 4.4 s | 2.3 s / 6.0 s |
| LLM calls / tokens per request | 0 / 0 | 1.96 / 1,390 | 0 / 0 | 1.65 / 1,307 |

What it shows:
- Both configurations show only valid plans, and every number and claim in the shown text matched the solver.
- The guard is busy with the LLM on: qwen2.5:7b's drafts were rejected 20 times, mostly for invented claims such as
  "orders moved from A to B". In 7 answers the retry also failed, and the code-written template was shown.
- The LLM recognises every request type; the rules router misroutes two rule-breaking requests (guard-002/003).
- The auditor suite runs second, and the solver caches results, so its latency is not comparable with the team suite's.

Against the naive cheapest-plant baseline (which ignores capacity and due dates: 269,200, 24.3 t, 85 late orders),
the default plan costs 15.5% more, emits 46.7% less carbon and has 0 late orders.

## Assumptions

- Default objective weights are cost 70% / carbon 30%; lateness is a heavy penalty per late order-day.
- Weeks start on Monday. An order is made at one plant in one week (orders are never split).
  Arrival = week start + 6 days + transit days.
- Landed cost = production (+ energy price if given) with duty on the ex-works value, plus transport.
  Carbon = plant energy × weekly grid intensity + transport carbon.
- Capacity is shared across a plant's products when `weekly_capacity` is the same on all its rows, and per
  product otherwise. A capacity loss applies to every week that overlaps its dates. `0.3` is read as 30%.
- A plant–destination pair with no transport row cannot ship there.
- Planning start: `PLANNING_START` if set, else the first week in `plants.week`, else the Monday two weeks
  before the earliest due date.
- Only fresh allocations and briefs become the base plan that re-plans compare against, so a second scenario
  is never diffed against the first one.
- Data checks treat grid intensity outside 0–1.2 kg CO2e/kWh as implausible.

## Limitations

- Every plan is a proposal: `approval_status` stays `pending` until a manager approves it.
- The solver has a 10 s time limit. Large cases return `FEASIBLE` with the optimality gap in `notes`, not `OPTIMAL`.
- Open eval findings (owners notified):
  - In MOCK_LLM mode the rules router lets "hand-written allocation" and "treat eligibility as optional" through
    (guard-002/003). With the LLM on they are refused, but by the router after one LLM call, not by the input guard.
  - A saved base plan is reused for "why" and re-plan questions even when it was solved with different weights
    (auditor explain-001 with the LLM on). The store can now filter by weights; the graph has to pass them.
  - The claim check sometimes rejects true statements, for example "Plant C is at 95% utilisation, the highest".
    The template is then shown, so the answer is still correct, only less fluent.
- Single transport mode per route; no batch sizes, changeovers or inventory.
- SQLite on one node; no authentication. Data is synthetic and not calibrated to real grids or tariffs.

## Known edge cases

| Case | Behaviour |
|---|---|
| Missing plant, % or order id in the request | `clarification_needed`, no plan |
| Prompt injection, "ignore capacity", "write the plan yourself", out-of-data questions | `refused` by the guards, usually before any LLM call |
| Capacity loss removes the only plant for a product | `infeasible`; the orders are listed as unassigned, not hidden |
| Orders that cannot meet their due date | Kept in the plan, flagged late and listed in `limitations` |
| LLM down or `MOCK_LLM=true` | Rules router and template text; the plan is identical |
| LLM text with a number not in FACTS | One retry with the bad numbers named, then a code-written template |
| CSV with a missing column | `/ingest` returns 422, status `error`, nothing loaded |
| CSV with bad rows (negative qty, bad date, unknown plant, duplicate id) | Good rows load (`partial`); the first 20 errors are listed |
| Late data drop | `/ingest` (replace or append) changes `data_version` and clears the cache; the next `/analyze` uses it, and base plans saved under older data are not reused |
| Capacity loss lowers cost | Possible: the plan minimises cost and carbon together (70/30), so losing a cleaner plant can move work to a cheaper, dirtier one. Carbon then rises |

## AI-usage disclosure

## Team contribution
