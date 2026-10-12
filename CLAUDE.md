# CLAUDE.md: shared rules for every Claude Code session on this repo

Project: HCLTech hackathon, **Multi-Plant Order Allocation with Cost and Carbon**.
Design doc: ask Mudit for the link. Task files: `.coordination/tasks/`.

## Ownership (write ONLY in your own folders)
| Person | Writes to |
|---|---|
| Mudit (Orchestrator) | `ai/graph.py`, `ai/llm.py`, `ai/generation/`, `frontend/`, `shared/`, `tests/test_graph_guards.py`, `CLAUDE.md`, `DECISIONS.md`, `.coordination/tasks/` |
| Ayush (Worker 1) | `ai/tools/`, `tests/tools/`, `.coordination/status/worker1.md` |
| Shivang (Worker 2) | `backend/`, `scripts/`, `eval/`, `data/DATA_CARD.md`, `Dockerfile`, `docker-compose.yml`, `requirements.txt`, `.env.example`, `tests/backend/`, `.coordination/status/worker2.md` |

Everyone shares one filesystem. **Never edit another person's files.** If you need a change there, write it in your
status file and tell your human.

## The contract
`shared/models.py` is the single source of truth for every request, response, tool output and trace.
Do not change it; only Mudit does. Import from it; never redefine those models locally.

## Non-negotiable rules (from the HCL guide; breaking them can disqualify us)
1. **Every number, plan and ranking comes from code** (the solver, cost model or validator), never from LLM text.
2. **No hard-coded answers** for demo or test questions. Stubs are only for development and must be off for the demo.
3. The LLM only parses requests and writes explanations; the guards in `ai/generation/guards.py` check it.
4. Every plan passes `validate_plan` before it is shown.
5. API keys live in `.env` only, never committed. Synthetic data only.

## Environment
- Conda env `crypto_env` (Python 3.10 for dev; Docker uses 3.11). Do NOT pip install anything without asking the human first. LLM: Ollama `qwen2.5:7b-instruct` at `http://localhost:11434`.
- `MOCK_LLM=true` runs everything without the LLM (tests, fallback).
- Run all tests: `MOCK_LLM=true pytest tests/ -q`

## Code quality
Type hints, `logging` (not print), try/except at API boundaries with clear messages, small pure functions,
a pytest test for every key function, `ruff check` clean. Commit to Git every hour with a clear message.

## When you finish a task
Write `DONE`, what you built, how to run it and anything blocking in your `.coordination/status/` file.

## Commits
Never add "Co-Authored-By: Claude" or "Generated with Claude Code" lines to commit messages. Commit as the human who owns the files.
