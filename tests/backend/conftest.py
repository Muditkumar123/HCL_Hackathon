"""Backend fixtures: a fresh SQLite DB per test and small CSVs (the TASK-001 trap shape)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from backend_csvs import CSVS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root, so plain `pytest tests/backend` works
os.environ["MOCK_LLM"] = "true"
os.environ["PLANNING_START"] = "2026-10-05"


@pytest.fixture
def db(tmp_path: Path):
    from backend import store

    store.init_db(str(tmp_path / "test.db"))
    yield store
    store._db_path = None
    store._invalidate()


@pytest.fixture
def loaded(db):
    from backend import loader

    for table, text in CSVS.items():
        assert loader.load_csv(table, text.encode()).status == "ok"
    return db


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:  # lifespan's init_db() keeps the test DB path set above
        yield c
