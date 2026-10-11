# ruff: noqa: F811  (test arguments shadow the fixtures imported from backend_fixtures, which is how pytest finds them)
"""API tests with FastAPI TestClient. Run: MOCK_LLM=true pytest tests/backend -q"""

from __future__ import annotations

import json

from backend_fixtures import CSVS, ORDERS, client, db  # noqa: F401 (pytest fixtures, imported by name)


def _ingest(client, table: str, text: str, **meta):
    return client.post("/ingest", files={"file": (f"{table}.csv", text.encode(), "text/csv")},
                       data={"meta": json.dumps({"table": table, **meta})})


def _load_all(client) -> None:
    for table, text in CSVS.items():
        assert _ingest(client, table, text).status_code == 200


def test_ingest_good_csv(client):
    r = _ingest(client, "plants", CSVS["plants"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["rows_loaded"] == 6 and body["rows_rejected"] == 0
    assert body["data_version"]


def test_ingest_missing_column_is_error(client):
    r = _ingest(client, "orders", "order_id,product,qty\nO-1,P1,5\n")
    assert r.status_code == 422
    assert r.json()["status"] == "error"
    assert "missing required columns: destination, due" in r.json()["errors"][0]


def test_ingest_bad_rows_are_rejected_and_reported(client):
    _ingest(client, "plants", CSVS["plants"])
    bad = ORDERS + "O-9,P1,-5,DE,2026-10-20\nO-10,P1,5,DE,not-a-date\nO-1,P1,5,DE,2026-10-20\n"
    body = _ingest(client, "orders", bad).json()
    assert body["status"] == "partial" and body["rows_loaded"] == 4 and body["rows_rejected"] == 3
    assert any("qty=-5 is negative" in e for e in body["errors"])
    assert any("is not a date" in e for e in body["errors"])
    assert any("duplicate key" in e for e in body["errors"])


def test_ingest_unknown_plant_rejected(client):
    _ingest(client, "plants", CSVS["plants"])
    text = CSVS["transport_duties"] + "Z,DE,1,2,0,0.1\n"
    body = _ingest(client, "transport_duties", text).json()
    assert body["rows_rejected"] == 1 and "unknown plant 'Z'" in body["errors"][0]


def test_ingest_bad_meta(client):
    r = client.post("/ingest", files={"file": ("x.csv", b"a\n1\n")}, data={"meta": '{"table": "nope"}'})
    assert r.status_code == 422 and "unknown table" in r.json()["detail"]
    r = client.post("/ingest", files={"file": ("x.csv", b"a\n1\n")}, data={"meta": "not json"})
    assert r.status_code == 422


def test_ingest_append_mode(client):
    _load_all(client)
    r = _ingest(client, "orders", "order_id,product,qty,destination,due\nO-5,P1,10,DE,2026-10-25\n", mode="append")
    assert r.json()["rows_loaded"] == 1
    assert client.get("/health").json()["tables"]["orders"] == 5


def test_health_empty_then_loaded(client):
    body = client.get("/health").json()
    assert body["data_store"] == "empty" and body["status"] == "degraded" and body["llm"] == "mock"
    _load_all(client)
    body = client.get("/health").json()
    assert body["data_store"] == "ok"
    assert body["tables"] == {"orders": 4, "plants": 6, "transport_duties": 9, "capacity_events": 1}
    assert "validation_passed" in body


def test_trace_unknown_is_404(client):
    r = client.get("/trace/al-000000")
    assert r.status_code == 404 and "al-000000" in r.json()["detail"]


def test_analyze_without_data_is_422(client):
    r = client.post("/analyze", json={"request": "Allocate this week's orders"})
    assert r.status_code == 422 and "No data loaded" in r.json()["detail"]


def test_analyze_invalid_body_is_422(client):
    r = client.post("/analyze", json={"request": ""})
    assert r.status_code == 422 and r.json()["errors"]


def test_analyze_saves_trace_and_base_plan(client, db):
    _load_all(client)
    r = client.post("/analyze", json={"request": "Allocate this week's orders"})
    assert r.status_code == 200
    body = r.json()
    assert body["answer_type"] == "allocation" and len(body["allocation"]) == 4
    assert body["summary"] != "STUB"
    trace = client.get(f"/trace/{body['run_id']}").json()
    assert trace["run_id"] == body["run_id"] and trace["steps"]
    base = db.latest_plan()
    assert base is not None and len(base.assignments) == 4


def test_validation_endpoint(client):
    _load_all(client)
    checks = client.get("/validation").json()
    assert len(checks) == 6 and {"check", "passed", "detail"} <= set(checks[0])
