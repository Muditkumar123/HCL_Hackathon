"""SQLite data store (stdlib sqlite3 + pandas).

Tables: the four Annex B data tables (dates stored as ISO text), plus `traces` and `plans`.
`load_dataset()` is cached in memory; every write (`write_table`, `init_db`) invalidates the cache.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator, Literal, get_args

import pandas as pd

from shared.models import REQUIRED_COLUMNS, Dataset, SolveResult, TableName, TraceRecord, utcnow

log = logging.getLogger(__name__)

TABLES: tuple[str, ...] = get_args(TableName)
DATE_COLUMNS: dict[str, list[str]] = {
    "orders": ["due"],
    "capacity_events": ["from_date", "to_date"],
}

_lock = threading.Lock()
_cache: Dataset | None = None
_db_path: str | None = None


def db_path() -> str:
    return _db_path or os.getenv("DB_PATH", "data/app.db")


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=10)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _invalidate() -> None:
    global _cache
    _cache = None


def init_db(path: str | None = None) -> None:
    """Create the DB file and the trace/plan tables. Data tables are created by `write_table`."""
    global _db_path
    if path is not None:
        _db_path = path
    Path(db_path()).parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS traces (run_id TEXT PRIMARY KEY, json TEXT NOT NULL, "
                     "created_at TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS plans (run_id TEXT PRIMARY KEY, json TEXT NOT NULL, "
                     "created_at TEXT NOT NULL, data_version TEXT)")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)")}
        if "data_version" not in cols:  # DB created before plans were tied to a data version
            conn.execute("ALTER TABLE plans ADD COLUMN data_version TEXT")
    _invalidate()
    log.info("db ready at %s", db_path())


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return row is not None


def _dates_to_text(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_datetime(out[c], errors="coerce").dt.strftime("%Y-%m-%d")
    return out


def _text_to_dates(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            parsed = pd.to_datetime(df[c], errors="coerce")
            df[c] = [d.date() if pd.notna(d) else None for d in parsed]
    return df


def write_table(table: TableName, df: pd.DataFrame, mode: Literal["replace", "append"] = "replace") -> None:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}; expected one of {TABLES}")
    df = _dates_to_text(df, DATE_COLUMNS.get(table, []))
    with _lock, _connect() as conn:
        if mode == "append" and _table_exists(conn, table):
            existing = pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
            df = pd.concat([existing, df], ignore_index=True)
        df.to_sql(table, conn, if_exists="replace", index=False)
        _invalidate()
    log.info("wrote table=%s rows=%d mode=%s", table, len(df), mode)


def read_table(table: TableName) -> pd.DataFrame:
    with _connect() as conn:
        if not _table_exists(conn, table):
            return pd.DataFrame(columns=REQUIRED_COLUMNS[table])
        df = pd.read_sql_query(f'SELECT * FROM "{table}"', conn)
    return _text_to_dates(df, DATE_COLUMNS.get(table, []))


def table_counts() -> dict[str, int]:
    with _connect() as conn:
        return {t: (conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] if _table_exists(conn, t) else 0)
                for t in TABLES}


def _data_version(frames: dict[str, pd.DataFrame]) -> str:
    h = hashlib.sha1()
    for name in TABLES:
        h.update(name.encode())
        h.update(frames[name].to_csv(index=False).encode())
    return h.hexdigest()[:8]


def planning_start(orders: pd.DataFrame, plants: pd.DataFrame | None = None) -> date:
    """First of: PLANNING_START env (ISO date); the earliest date in plants.week; the Monday two weeks
    before the earliest due date (assumption: production must start before the first order is due)."""
    env = os.getenv("PLANNING_START", "").strip()
    if env:
        return date.fromisoformat(env)
    if plants is not None and "week" in plants.columns and len(plants):
        weeks = pd.to_datetime(plants["week"], errors="coerce", format="mixed")
        if weeks.notna().all():
            first = weeks.min().date()
            return first - timedelta(days=first.weekday())
    dues = [d for d in orders.get("due", []) if d is not None]
    if not dues:
        return date.today() - timedelta(days=date.today().weekday())
    first = min(dues) - timedelta(days=14)
    return first - timedelta(days=first.weekday())


def load_dataset() -> Dataset:
    """Read all four tables into a Dataset. Cached until the next write."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        frames = {t: read_table(t) for t in TABLES}
        _cache = Dataset(
            orders=frames["orders"], plants=frames["plants"], transport_duties=frames["transport_duties"],
            capacity_events=frames["capacity_events"], planning_start=planning_start(frames["orders"], frames["plants"]),
            data_version=_data_version(frames),
        )
        log.info("dataset loaded version=%s orders=%d", _cache.data_version, len(_cache.orders))
        return _cache


def save_trace(trace: TraceRecord) -> None:
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO traces (run_id, json, created_at) VALUES (?, ?, ?)",
                     (trace.run_id, trace.model_dump_json(), trace.created_at.isoformat()))


def get_trace(run_id: str) -> TraceRecord | None:
    with _connect() as conn:
        row = conn.execute("SELECT json FROM traces WHERE run_id=?", (run_id,)).fetchone()
    return TraceRecord.model_validate_json(row[0]) if row else None


def list_traces(limit: int = 1000) -> list[TraceRecord]:
    with _connect() as conn:
        rows = conn.execute("SELECT json FROM traces ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [TraceRecord.model_validate_json(r[0]) for r in rows]


def save_plan(run_id: str, result: SolveResult, data_version: str | None = None) -> None:
    """Store a plan so later re-plans diff against it. `data_version` is the dataset the plan was solved on
    (default: the current one); `latest_plan` only returns plans for the current data."""
    version = data_version or load_dataset().data_version
    with _connect() as conn:
        conn.execute("INSERT OR REPLACE INTO plans (run_id, json, created_at, data_version) VALUES (?, ?, ?, ?)",
                     (run_id, result.model_dump_json(), utcnow().isoformat(), version))


def latest_plan() -> SolveResult | None:
    """Newest saved plan for the CURRENT data, else None (the graph then solves a fresh base).

    A plan solved before an /ingest refers to orders, capacities or costs that may no longer exist, so "why" and
    re-plan answers must never diff against it.
    """
    try:
        version = load_dataset().data_version
        with _connect() as conn:
            row = conn.execute("SELECT json FROM plans WHERE data_version = ? ORDER BY created_at DESC LIMIT 1",
                               (version,)).fetchone()
    except sqlite3.Error as exc:  # no DB yet => no base plan
        log.warning("latest_plan unavailable: %s", exc)
        return None
    return SolveResult.model_validate_json(row[0]) if row else None
