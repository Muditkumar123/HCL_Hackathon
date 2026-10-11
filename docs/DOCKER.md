# Running with Docker

For anyone with Docker access: this server's user account cannot reach the Docker daemon, so these steps were
checked by reading the files, not by running them. Please report back what you see at step 4.

**Needs:** Linux with Docker Engine 20.10+ and the Compose v2 plugin (`docker compose version` works). Both
services use `network_mode: host`, which works only on Linux; on Docker Desktop (macOS/Windows) the ports are not
published this way.

## 1. Get the code and create `.env`

```bash
git clone https://github.com/Muditkumar123/HCL_Hackathon.git
cd HCL_Hackathon
cp .env.example .env
```

Edit `.env` and set `MOCK_LLM=true`. This runs with no model: the rules router and the code-written template text.
The plan and every number are identical. `docker compose` fails if `.env` is missing, because both services use
`env_file: .env`.

## 2. Build and start

```bash
docker compose up --build
```

- One image, `hcl-allocation:dev` (python:3.11-slim plus `requirements.txt`), is used by both services. The first
  build takes a few minutes (pip install); the second service's build is cached.
- **api** (port 8000): on first start, the entrypoint generates the seeded data pack into `data/raw` (only if it is
  not there) and loads it into `data/app.db`. Both live in the named volume `appdata`. Wait for
  `Uvicorn running on http://0.0.0.0:8000`.
- **ui** (port 8501): Streamlit, which talks to the API at `http://localhost:8000`. It skips the data load.

## 3. Check the API

In a second terminal:

```bash
curl -s localhost:8000/health
```

Expect `"status": "ok"`, `"data_store": "ok"`, `"llm": "mock"`, `"validation_passed": true` and
`"tables": {"orders": 120, "plants": 68, "transport_duties": 50, "capacity_events": 2}`.

```bash
curl -s -X POST localhost:8000/analyze -H 'Content-Type: application/json' -d '{"request": "Allocate this week'"'"'s orders"}'
```

Expect HTTP 200 with `"answer_type": "allocation"`, 120 rows in `allocation`, `kpis` filled in, and
`"validator": {"capacity_ok": true, "eligibility_ok": true, ...}`. Then fetch its trace (use the `run_id` from the
answer):

```bash
curl -s localhost:8000/trace/al-xxxxxx
```

## 4. Check the UI

Open `http://<server>:8501`. The sidebar health light should be green. Click an example question, and you should
see the KPI tiles, the allocation table and the validator badge.

## With the real LLM

Set `MOCK_LLM=false` in `.env`. Ollama must run on the host at `http://localhost:11435` (GPU 1, qwen2.5:7b-instruct).
Because of host networking, the container reaches it on localhost; compose sets `OLLAMA_BASE_URL` to that URL.
`/health` then shows `"llm": "ok:ollama/qwen2.5:7b-instruct"`.

## Reset, logs, stop

```bash
docker compose logs -f api            # API log, with run_id on every analyze line
docker compose down                   # stop and keep the data volume
docker compose down -v                # stop and delete the volume: the next start reloads the pack
```

The data is loaded only when `data/app.db` does not exist yet. To load new CSVs into a running stack, use
`POST /ingest` or `POST /admin/reload`, not a restart.

## Static review notes (what was checked)

- `Dockerfile`: python:3.11-slim. The pinned wheels (ortools 9.15, numpy 2.2, pandas 2.3) exist for 3.11. The
  entrypoint path is absolute (`/app/scripts/docker_entrypoint.sh`). `.dockerignore` keeps `.env`, `data/*.db`,
  `.git` and `.claude` out of the image.
- `scripts/docker_entrypoint.sh`: `set -e`, so a loader error stops the container instead of serving empty data.
  `SKIP_DATA_LOAD=true` on `ui`.
- `frontend/app.py` adds the repo root to `sys.path`, so `streamlit run frontend/app.py` imports `shared` in the
  container.
- `uvicorn` binds `0.0.0.0:8000` on the host network, which exposes the API on every interface of the host.
  There is no authentication, so run it only on a trusted network.
