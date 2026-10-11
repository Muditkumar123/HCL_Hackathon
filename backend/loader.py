"""CSV loader shared by POST /ingest and the CLI.

    python -m backend.loader --dir data/raw [--mode replace|append]

Checks required columns, coerces types, rejects bad rows (reporting the first 20) and keeps extra columns.
Tariff-style "late drop" files go through the same path (`mode="append"` to add rows, "replace" to swap the table).
"""

from __future__ import annotations

import argparse
import logging
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Literal

import pandas as pd

from backend import store
from shared.models import REQUIRED_COLUMNS, IngestResponse, TableName

log = logging.getLogger(__name__)

MAX_ERRORS = 20
# Load order matters: transport_duties / capacity_events are checked against the plants already loaded.
LOAD_ORDER: tuple[str, ...] = ("plants", "transport_duties", "orders", "capacity_events")

NUMERIC: dict[str, list[str]] = {
    "orders": ["qty"],
    "plants": ["weekly_capacity", "unit_cost", "energy_kwh_per_unit", "carbon_kg_per_kwh"],
    "transport_duties": ["transport_cost_per_unit", "transit_days", "duty_pct", "carbon_kg_per_unit"],
    "capacity_events": ["capacity_loss_pct"],
}
DATES: dict[str, list[str]] = {"orders": ["due"], "capacity_events": ["from_date", "to_date"]}
KEYS: dict[str, list[str]] = {
    "orders": ["order_id"],
    "plants": ["plant", "product", "week"],
    "transport_duties": ["plant", "destination"],
    "capacity_events": ["event_id"],
}


def _read(file: BinaryIO | Path | bytes) -> pd.DataFrame:
    if isinstance(file, bytes):
        file = BytesIO(file)
    df = pd.read_csv(file, dtype=str, keep_default_na=False, skipinitialspace=True)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.apply(lambda s: s.str.strip())


def _row_errors(table: str, df: pd.DataFrame, known_plants: set[str]) -> tuple[pd.DataFrame, list[str]]:
    """Coerce types and return (clean rows, error messages). Row numbers are CSV line numbers."""
    errors: dict[int, str] = {}
    out = df.copy()

    def flag(mask: pd.Series, msg_fn) -> None:
        for i in out.index[mask]:
            errors.setdefault(i, f"line {i + 2}: {msg_fn(i)}")

    for col in REQUIRED_COLUMNS[table]:
        flag(out[col] == "", lambda i, c=col: f"{c} is empty")

    for col in NUMERIC.get(table, []):
        raw = out[col]
        num = pd.to_numeric(raw, errors="coerce")
        flag(num.isna() & (raw != ""), lambda i, c=col, r=raw: f"{c}={r[i]!r} is not a number")
        flag(num < 0, lambda i, c=col, r=raw: f"{c}={r[i]} is negative")
        out[col] = num

    if table == "orders":
        flag(out["qty"] == 0, lambda i: "qty is 0")
    if table == "transport_duties":
        flag(out["transit_days"] % 1 != 0, lambda i: f"transit_days={out['transit_days'][i]} is not a whole number")
    if table == "capacity_events":
        flag(out["capacity_loss_pct"] > 100, lambda i: f"capacity_loss_pct={out['capacity_loss_pct'][i]} > 100")

    for col in DATES.get(table, []):
        raw = out[col]
        parsed = pd.to_datetime(raw, errors="coerce", format="mixed")
        flag(parsed.isna() & (raw != ""), lambda i, c=col, r=raw: f"{c}={r[i]!r} is not a date")
        out[col] = parsed
    if table == "capacity_events":
        flag(out["to_date"] < out["from_date"], lambda i: "to_date is before from_date")

    if known_plants and "plant" in out.columns and table != "plants":
        flag(~out["plant"].isin(known_plants) & (out["plant"] != ""),
             lambda i: f"unknown plant {out['plant'][i]!r} (not in plants table)")

    keys = [k for k in KEYS.get(table, []) if k in out.columns]
    if keys:
        flag(out.duplicated(keys, keep="first"), lambda i: f"duplicate key {tuple(out.loc[i, keys])}")

    bad = sorted(errors)
    clean = out.drop(index=bad)
    if "transit_days" in clean.columns:
        clean["transit_days"] = clean["transit_days"].astype(int)
    for col in DATES.get(table, []):
        clean[col] = clean[col].dt.date
    return clean.reset_index(drop=True), [errors[i] for i in bad]


def load_csv(table: TableName, file: BinaryIO | Path | bytes,
             mode: Literal["replace", "append"] = "replace") -> IngestResponse:
    """Validate a CSV and write the good rows to the store."""
    if table not in REQUIRED_COLUMNS:
        return IngestResponse(table="orders", rows_loaded=0, rows_rejected=0, status="error",
                              errors=[f"unknown table {table!r}; expected one of {list(REQUIRED_COLUMNS)}"])
    try:
        df = _read(file)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeDecodeError) as exc:
        return IngestResponse(table=table, rows_loaded=0, rows_rejected=0, status="error",
                              errors=[f"could not read CSV: {exc}"])

    missing = [c for c in REQUIRED_COLUMNS[table] if c not in df.columns]
    if missing:
        return IngestResponse(table=table, rows_loaded=0, rows_rejected=len(df), status="error",
                              errors=[f"missing required columns: {', '.join(missing)}"])

    known_plants = set(store.read_table("plants")["plant"].astype(str)) if table != "plants" else set()
    clean, errors = _row_errors(table, df, known_plants)
    if clean.empty:
        return IngestResponse(table=table, rows_loaded=0, rows_rejected=len(errors), status="error",
                              errors=(errors or ["file has no data rows"])[:MAX_ERRORS])

    store.write_table(table, clean, mode=mode)
    version = store.load_dataset().data_version
    log.info("ingest table=%s loaded=%d rejected=%d version=%s", table, len(clean), len(errors), version)
    return IngestResponse(table=table, rows_loaded=len(clean), rows_rejected=len(errors),
                          status="partial" if errors else "ok", errors=errors[:MAX_ERRORS], data_version=version)


def load_dir(directory: Path, mode: Literal["replace", "append"] = "replace") -> list[IngestResponse]:
    """Load every CSV in `directory` whose filename (without .csv) is a table name."""
    found = {p.stem.lower(): p for p in Path(directory).glob("*.csv")}
    results = []
    for table in LOAD_ORDER:
        if table in found:
            with found[table].open("rb") as fh:
                results.append(load_csv(table, fh, mode=mode))  # type: ignore[arg-type]
    skipped = sorted(set(found) - set(LOAD_ORDER))
    if skipped:
        log.warning("skipped CSVs that do not match a table name: %s", skipped)
    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="Load the CSV data pack into the SQLite store.")
    ap.add_argument("--dir", default="data/raw", type=Path)
    ap.add_argument("--mode", default="replace", choices=["replace", "append"])
    args = ap.parse_args()
    store.init_db()
    results = load_dir(args.dir, args.mode)
    if not results:
        raise SystemExit(f"no table CSVs found in {args.dir}")
    for r in results:
        print(f"{r.table:<18} {r.status:<8} loaded={r.rows_loaded:<5} rejected={r.rows_rejected}")
        for e in r.errors:
            print(f"    {e}")
    print(f"data_version={store.load_dataset().data_version}")
    if any(r.status == "error" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
