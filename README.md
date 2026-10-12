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

Step-by-step run and check list (with MOCK_LLM=true): [docs/DOCKER.md](docs/DOCKER.md).

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

curl -s localhost:8000/trace/al-7f707c              # 404 if the run_id is unknown
```

Sample traces (qwen2.5:7b, frozen data pack):
- [Allocation](docs/traces/allocation_al-7f707c.json): 7 steps, 2 LLM calls; the number guard checks 14 items
  and passes.
- [Plant C unavailable, re-plan](docs/traces/replan_scenario_al-5e103d.json): the guard rejects the first LLM draft
  (2 unsupported items), and the retry passes.
- [Refused prompt injection](docs/traces/refused_al-a9a496.json): blocked by the input guard, with 0 LLM calls.

## Data

Synthetic only, frozen pack (`python -m scripts.generate_data`, seed 42, `data_version` e543801d): 120 orders,
6 products, 10 destinations, 5 plants, 4 production weeks from 2026-10-12, 50 routes and 2 capacity events. Traps are
built in on purpose: a cheapest plant that is late, a single-plant product, a duty that flips the cheapest plant, a
low-carbon plant with low capacity, weekly carbon intensity, and capacity events. See
[data/DATA_CARD.md](data/DATA_CARD.md).

Headline numbers on this pack (default weights 70/30, all computed by the solver and validator):

| | Result |
|---|---|
| Plan | cost 285,583, carbon 19.8 t, 9 late orders, 0 unassigned; validator: capacity and eligibility OK |
| Cheapest-plant baseline | cost 266,355, carbon 24.4 t, 88 late orders, 3 overloaded plant-weeks (not a valid plan) |
| Orders not at their cheapest eligible plant | 58%, so "it was the cheapest" would be the wrong explanation for most orders |
| Carbon trade-off | 5% cut: +4,003 cost and 49 late orders (3,727 per tonne). The deepest feasible cut is 5.4%; 10% and above is not feasible within capacity |

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

Two suites, two configurations, same data (frozen pack) and solver; only the language layer changes. The run used a
clean checkout of eba2dd4 (everyone's latest commits). Full per-case results and every failing condition are in
[eval/report.md](eval/report.md).
- **Team cases** ([eval/cases_team.yaml](eval/cases_team.yaml), 24): written by us. They check the answer type, and
  the carbon-cut cases check the answer either way: a feasible cut must cost more and emit less, and an infeasible
  one must quote the deepest feasible cut.
- **Independent auditor cases** ([eval/cases.yaml](eval/cases.yaml), 26): written by Codex as an independent
  auditor, with machine-checked conditions (format: `.coordination/reviews/EVAL-FORMAT.md`). Conditions fail closed.

| metric | Team: MOCK_LLM | Team: qwen2.5:7b | Auditor: MOCK_LLM | Auditor: qwen2.5:7b |
|---|---|---|---|---|
| Cases passed | 24/24 | 24/24 | 24/26 | 24/26 |
| Answer-type (intent) accuracy | 100% | 100% | 96% | 96% |
| Condition checks passed | 100% | 100% | 97% | 97% |
| Plan validity | 100% | 100% | 100% | 100% |
| Faithfulness of the shown text | 100% | 100% | 100% | 100% |
| Router fallback to rules | n/a | 0% | n/a | 0% |
| Text from template | 100% | 17% | 100% | 19% |
| Number-guard retries / template after 2 fails | 0 / 0 | 12 / 3 | 0 / 0 | 9 / 3 |
| Latency p50 / p95 | 2.3 s / 6.2 s | 4.7 s / 9.0 s | 0.1 s / 4.9 s | 2.1 s / 7.4 s |
| LLM calls / tokens per request | 0 / 0 | 2.04 / 1,506 | 0 / 0 | 1.58 / 1,281 |

What it shows:
- Both configurations show only valid plans, and every number and claim in the shown text matched the solver.
- The guard is busy with the LLM on: qwen2.5:7b's drafts were rejected 21 times, mostly for invented claims such as
  "orders moved from A to B". In 6 answers the retry also failed, and the code-written template was shown.
- The two auditor failures are the same in both configurations. `tradeoff-001` requires a feasible 10% cut, which
  this pack does not have (fix suggested in `.coordination/reviews/EVAL-NOTES.md`). `explain-001` is a real bug:
  the base plan reused for "why" was solved with other weights (see Limitations).
- The auditor suite runs second, and the solver caches results, so its latency is not comparable with the team suite's.

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
  - A saved base plan is reused for "why" and re-plan questions even when it was solved with different weights
    (auditor explain-001). `backend.store.latest_plan(weights)` can now filter by weights; the graph has to pass them.
  - The claim check can reject a true statement (seen in an earlier run: a correct "highest utilisation" claim).
    The template is then shown, so the answer stays correct, only less fluent.
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
| Capacity loss lowers the reported cost | Possible: unassigned (deferred) orders carry no cost, and the 70/30 objective can move work to a cheaper, dirtier plant. Read cost together with unassigned orders and carbon |

## AI-usage disclosure

## Team contribution

**Mudit Kumar (Orchestrator): LangGraph flow and guardrails (owner), plus UI and the shared contract**

The LangGraph orchestration (`ai/graph.py`) and all guardrails (`ai/generation/guards.py`, G1 to G5) are Mudit's work.

- Owned `shared/models.py`, the single data contract for every request, response, tool output and trace, and logged
  each contract change in `DECISIONS.md` (D-001 to D-008).
- Built the LangGraph flow in `ai/graph.py`: a fixed graph (input guard → intent parsing → intent guard → solver tools →
  validator gate → writer → number check → finalize), with a bounded retry, a code-written fallback summary and a
  full trace for every request. Plans are shown only after they pass `validate_plan`.
- Wrote the guardrails in `ai/generation/guards.py`:
  - G1 blocks prompt injection, rule-breaking and requests for invented numbers before any LLM call, and masks PII.
  - G2 drops any solver setting the user did not state (plant, %, order, dates, weights, on-time) and derives the
    on-time and cost-only settings in code.
  - G4 checks every number, order, plant and claim in the LLM's text against the solver's facts, and rejects
    unsupported claims ("on time", "impossible", late vs unassigned).
- Wrote the intent router, prompts and writer (`ai/generation/`), the LLM wrapper `ai/llm.py` (Ollama, mock mode,
  safe fallbacks), and development stubs for the solver tools behind `USE_TOOL_STUBS`.
- Built the Streamlit UI (`frontend/app.py`): chat with example questions, KPI tiles, cost/carbon trade-off chart,
  allocation table with late rows highlighted, what-moved and validator panels, trace viewer, CSV upload and
  capacity-loss form.
- Fixed every finding from the independent reviews REVIEW-001 and REVIEW-002, each with regression tests using the
  reviewer's exact inputs (`tests/test_graph_guards.py`), and coordinated the team through the task files and queues.

## Declaration
