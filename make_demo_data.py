"""Build demo CSVs for the /ingest part of the presentation, derived from the frozen pack in data/raw.

Run from the repo root:  python make_demo_data.py
Writes to data/demo/. Every file keeps the original capacity events, so uploading in replace OR append
mode never wipes the frozen data. Restore the frozen pack afterwards with:
    python -m backend.loader --dir data/raw
"""
from __future__ import annotations

import pathlib

import pandas as pd

RAW = pathlib.Path("data/raw")
OUT = pathlib.Path("data/demo")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    orders = pd.read_csv(RAW / "orders.csv")
    plants = pd.read_csv(RAW / "plants.csv")
    ev_path = RAW / "capacity_events.csv"
    events = pd.read_csv(ev_path) if ev_path.exists() else pd.DataFrame(
        columns=["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"])

    names = sorted(plants["plant"].astype(str).unique())
    plant = "B" if "B" in names else names[min(1, len(names) - 1)]
    if len(events):
        start = pd.to_datetime(events["from_date"]).min()
    else:
        start = pd.to_datetime(orders["due"]).min() - pd.Timedelta(days=21)
    end = start + pd.Timedelta(days=13)
    loss_is_fraction = len(events) > 0 and float(pd.to_numeric(events["capacity_loss_pct"]).max()) <= 1
    pct = 0.3 if loss_is_fraction else 30

    def row(eid: str, p: str, f, t, loss) -> dict:
        return {"event_id": eid, "plant": p, "from_date": pd.Timestamp(f).strftime("%Y-%m-%d"),
                "to_date": pd.Timestamp(t).strftime("%Y-%m-%d"), "capacity_loss_pct": loss}

    # 1) valid: original events + one new two-week capacity loss
    good = pd.concat([events, pd.DataFrame([row("DEMO-1", plant, start, end, pct)])], ignore_index=True)
    good.to_csv(OUT / "capacity_events_demo.csv", index=False)

    # 2) bad rows: original events + 3 invalid rows (unknown plant, negative loss, end before start)
    bad_rows = pd.DataFrame([
        row("BAD-1", "Z", start, end, pct),
        row("BAD-2", plant, start, end, -pct),
        row("BAD-3", plant, end, start, pct),
    ])
    pd.concat([events, bad_rows], ignore_index=True).to_csv(OUT / "capacity_events_bad_rows.csv", index=False)

    # 3) bad schema: first 3 orders with the required 'due' column removed (rejected before anything is written)
    orders.head(3).drop(columns=["due"]).to_csv(OUT / "orders_missing_due.csv", index=False)

    print(f"plant used: {plant}, window {start.date()} to {end.date()}, loss {pct}")
    for f in sorted(OUT.glob("*.csv")):
        print("wrote", f)


if __name__ == "__main__":
    main()
