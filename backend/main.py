"""FastAPI app: /analyze, /ingest, /health, /trace/{run_id}, /admin/reload (+ /validation for the UI table).

    uvicorn backend.main:app --port 8000

Every number in /analyze comes from ai.graph.run (solver, cost model, validator). This module only
moves data in and out of the store and never builds an answer itself.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

load_dotenv()

from backend import loader, store, validate_data  # noqa: E402
from shared.models import (  # noqa: E402
    AnalyzeRequest, AnalyzeResponse, AnswerType, HealthResponse, IngestResponse, SolveResult, TraceRecord,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backend")

RAW_DIR = Path("data/raw")
# Only fresh full solves become the base plan; scenario results must not overwrite it, or a second
# "Plant B loses 30%" would be diffed against the already-reduced plan.
BASE_PLAN_TYPES = (AnswerType.ALLOCATION, AnswerType.BRIEF)


@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init_db()
    yield


app = FastAPI(title="Multi-Plant Order Allocation API", version="1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _bad_request(_: Request, exc: RequestValidationError) -> JSONResponse:
    msgs = [f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}" for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "Invalid request", "errors": msgs})


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error")
    return JSONResponse(status_code=500, content={"detail": f"Internal error ({type(exc).__name__}). See server log."})


# ------------------------------------------------------------------ helpers
def _plan_from_response(resp: AnalyzeResponse, trace: TraceRecord) -> SolveResult | None:
    """Rebuild the solver result from the response so the next re-plan can diff against it."""
    if resp.answer_type not in BASE_PLAN_TYPES or not resp.allocation or resp.kpis is None:
        return None
    if resp.validator is None or not (resp.validator.capacity_ok and resp.validator.eligibility_ok):
        return None
    solve = next((s.output for s in trace.steps if s.tool.startswith("solve_allocation") and s.output), {})
    status = solve.get("status", "FEASIBLE")
    if status not in ("OPTIMAL", "FEASIBLE"):
        return None
    return SolveResult(status=status, assignments=resp.allocation, kpis=resp.kpis,
                       solve_s=float(solve.get("solve_s", 0.0)), weights_used=resp.weights_used)


def _validation() -> list[dict]:
    ds = store.load_dataset()
    if ds.orders.empty:
        return [{"check": "data loaded", "passed": False, "detail": "orders table is empty; POST /ingest first"}]
    return validate_data.run_checks(ds)


# ------------------------------------------------------------------ endpoints
@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest) -> AnalyzeResponse:
    from ai.graph import run

    ds = store.load_dataset()
    if ds.orders.empty or ds.plants.empty:
        raise HTTPException(422, "No data loaded: ingest orders, plants and transport_duties first (POST /ingest).")
    try:
        resp, trace = run(req, ds)
    except ImportError as exc:
        log.error("analysis tools unavailable: %s", exc)
        raise HTTPException(503, f"Analysis tools are not available yet: {exc}") from exc
    log.info("analyze run_id=%s answer_type=%s latency_ms=%s", resp.run_id, resp.answer_type.value, trace.latency_ms)
    try:
        store.save_trace(trace)
        plan = _plan_from_response(resp, trace)
        if plan is not None:
            store.save_plan(resp.run_id, plan, data_version=ds.data_version)
            log.info("analyze run_id=%s saved as base plan", resp.run_id)
    except Exception:  # the answer is still valid; only persistence failed
        log.exception("analyze run_id=%s could not save trace/plan", resp.run_id)
    return resp


@app.post("/ingest", response_model=IngestResponse)
def ingest(file: UploadFile = File(...), meta: str = Form(...)) -> JSONResponse | IngestResponse:
    try:
        m: dict[str, Any] = json.loads(meta)
        table, mode = m["table"], m.get("mode", "replace")
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(422, 'meta must be JSON like {"table": "orders"} (optional "mode": "append")') from exc
    if table not in store.TABLES:
        raise HTTPException(422, f"unknown table {table!r}; expected one of {list(store.TABLES)}")
    if mode not in ("replace", "append"):
        raise HTTPException(422, 'mode must be "replace" or "append"')
    res = loader.load_csv(table, file.file, mode=mode)
    log.info("ingest file=%s table=%s status=%s loaded=%d rejected=%d", file.filename, table, res.status,
             res.rows_loaded, res.rows_rejected)
    if res.status == "error":
        return JSONResponse(status_code=422, content=res.model_dump(mode="json"))
    return res


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    from ai.llm import health as llm_health

    try:
        tables = store.table_counts()
        data_store = "ok" if tables.get("orders") else "empty"
    except Exception as exc:
        log.exception("health: store failed")
        tables, data_store = {}, f"error: {exc}"
    try:
        llm = llm_health()
    except Exception as exc:
        llm = f"error: {exc}"
    try:
        validation_passed = data_store == "ok" and all(c["passed"] for c in _validation())
    except Exception as exc:
        log.warning("health: validation failed to run: %s", exc)
        validation_passed = False
    ok = data_store == "ok" and not llm.startswith("error") and validation_passed
    return HealthResponse(status="ok" if ok else "degraded", data_store=data_store, llm=llm, tables=tables,
                          validation_passed=validation_passed)


@app.get("/validation")
def validation() -> list[dict]:
    """The data-quality table (same checks as `python -m backend.validate_data`)."""
    return _validation()


@app.get("/trace/{run_id}", response_model=TraceRecord)
def trace(run_id: str) -> TraceRecord:
    rec = store.get_trace(run_id)
    if rec is None:
        raise HTTPException(404, f"No trace for run_id {run_id!r}")
    return rec


@app.post("/admin/reload", response_model=list[IngestResponse])
def reload() -> list[IngestResponse]:
    if not RAW_DIR.is_dir():
        raise HTTPException(404, f"{RAW_DIR} does not exist")
    results = loader.load_dir(RAW_DIR)
    if not results:
        raise HTTPException(404, f"no table CSVs found in {RAW_DIR}")
    return results
