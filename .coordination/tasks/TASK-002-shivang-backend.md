# TASK-002 — Shivang — Backend, data store, loader, validation, Docker (`backend/`)

**Owner:** Shivang (Worker 2) **Writes only to:** `backend/`, `scripts/`, `data/DATA_CARD.md`, `Dockerfile`,
`docker-compose.yml`, `requirements.txt`, `.env.example`, `tests/backend/`
**Contract:** `shared/models.py` (do not edit; ask the Orchestrator)
**Priority:** get the API answering with contract-shaped JSON in the first 30 minutes, so Mudit's UI can integrate immediately.

---

## Step 1 (target: about 90 minutes, in this order)

### 0. Scaffold (10 min)
`requirements.txt` pinned for Python 3.11 (fastapi, uvicorn, pydantic 2, pandas, ortools, langgraph,
langchain, langchain-ollama, streamlit, plotly, httpx, pytest, ruff, python-dotenv); `.env.example` with
`LLM_PROVIDER=ollama, LLM_MODEL=qwen2.5:7b-instruct, OLLAMA_BASE_URL=http://localhost:11434, MOCK_LLM=false,
DB_PATH=data/app.db, PLANNING_START=`; `.gitignore` that excludes `.env`, `data/*.db` and `data/raw/`.
Use the conda env `crypto_env` on the server, not `crypto_env`.

### 1. `backend/main.py` endpoints (stub first, then wire up)
| Endpoint | Behaviour |
|---|---|
| `POST /analyze` | body `AnalyzeRequest` → `AnalyzeResponse`. Calls `ai.graph.run(req, ds) -> tuple[AnalyzeResponse, TraceRecord]` (**already written by Mudit; read `ai/graph.py`**), saves the trace, and if the response has a plan, saves it with `save_plan` so later re-plans compare against it. Returns the response. **Until Mudit's graph lands:** return a stub built from the models with `summary="STUB"`. Delete the stub by hour 2, because hard-coded answers are a disqualification condition. |
| `POST /ingest` | multipart `file` (CSV) + form field `meta` (JSON `{"table": "orders"}`) → `IngestResponse` |
| `GET /health` | `HealthResponse`: DB reachable, row count per table, LLM status from `ai.llm.health()` |
| `GET /trace/{run_id}` | `TraceRecord`, or 404 |
| `POST /admin/reload` | (optional) re-runs the loader on `data/raw/` |

Wrap every endpoint in try/except, log with `logging` (run_id on every line), and return 422/500 with a clear message.
Never return a raw traceback.

### 2. `backend/store.py` (SQLite, stdlib `sqlite3` + pandas)
```python
def init_db(path: str) -> None
def write_table(table: TableName, df: pd.DataFrame, mode: Literal["replace", "append"] = "replace") -> None
def load_dataset() -> Dataset            # reads all 4 tables; data_version = sha1 of contents [:8]
def save_trace(trace: TraceRecord) -> None   # traces(run_id PK, json TEXT, created_at)
def get_trace(run_id: str) -> TraceRecord | None
def save_plan(run_id: str, result: SolveResult) -> None   # base plan for re-plan diffs
def latest_plan() -> SolveResult | None
```
Cache `load_dataset()` in memory and invalidate the cache on every `/ingest`.

### 3. `backend/loader.py` (shared by `/ingest` and the CLI)
```python
def load_csv(table: TableName, file: BinaryIO | Path, mode="replace") -> IngestResponse
```
Check `REQUIRED_COLUMNS[table]` (missing column → status "error"); coerce types; reject bad rows
(negative qty or capacity, unparseable dates, unknown plant) and report the first 20 errors.
Keep extra columns. Tariff-style "late drop" files go through the same path.
CLI: `python -m backend.loader --dir data/raw` loads every CSV whose filename matches a table name.

### 4. `backend/validate_data.py` (must be shown in the demo)
```python
def run_checks(ds: Dataset) -> list[dict]   # {check, passed, detail}
```
The six required checks: each order's product is makeable by at least one plant · every (plant, destination) pair has a transport row ·
capacity > 0 (unit stated) · due > planning_start · carbon intensity in a plausible range (unit stated) ·
row counts match `data/DATA_CARD.md`. CLI: `python -m backend.validate_data` prints a PASS/FAIL table.
Add it to `/health` as `validation_passed`.

### 5. Docker
`Dockerfile` (python:3.11-slim) plus `docker-compose.yml` with services `api` (uvicorn :8000) and `ui` (streamlit :8501),
a named volume for `data/`, and `env_file: .env`. **Ollama runs on the server host and probably listens only on 127.0.0.1,**
so set `network_mode: host` on `api` (and on `ui`, with `API_URL=http://localhost:8000`) and keep
`OLLAMA_BASE_URL=http://localhost:11434`. Test `docker compose up` once by hour 3, not at 16:00.

## Step 2
- `scripts/generate_data.py` (seeded): at least 100 orders, at least 8 destinations, at least 3 orders where the cheapest plant is late, a single-plant product,
  a duty trap, a low-carbon low-capacity plant, weekly carbon intensity, and 2 capacity events. Fill in `data/DATA_CARD.md` (Annex D).
- `eval/run_eval.py`: runs the at least 20 cases in `eval/cases.yaml` against `/analyze` and reports validity, late orders,
  cost and carbon vs `cheapest_plant_plan`, faithfulness rate, and p50/p95 latency, plus LLM calls and tokens from traces.
- `tests/backend/test_api.py` using FastAPI `TestClient`: ingest a good CSV, ingest a bad CSV, call health, call trace and expect 404.

## Done when
`curl` works for all four endpoints, the loader CLI loads the data pack, and `validate_data` prints its table.
Write DONE plus the curl examples into `.coordination/status/worker2.md`.
