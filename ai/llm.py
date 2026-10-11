"""LLM access behind one config switch (LLM_PROVIDER / LLM_MODEL). MOCK_LLM=true -> no model at all."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)


def mock_mode() -> bool:
    return os.getenv("MOCK_LLM", "false").lower() == "true"


@lru_cache(maxsize=1)
def get_llm() -> Any | None:
    if mock_mode():
        return None
    from langchain.chat_models import init_chat_model

    provider = os.getenv("LLM_PROVIDER", "ollama")      # ollama | openai | anthropic | google_genai
    model = os.getenv("LLM_MODEL", "qwen2.5:7b-instruct")
    kwargs: dict[str, Any] = {"temperature": 0}
    if provider == "ollama":
        kwargs["base_url"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        if model.startswith("qwen3"):
            kwargs["reasoning"] = False                 # no <think> blocks
    return init_chat_model(model, model_provider=provider, **kwargs)


def _tokens(msg: Any) -> int:
    usage = getattr(msg, "usage_metadata", None) or {}
    return int(usage.get("total_tokens", 0))


def call_structured(system: str, user: str, schema: type[BaseModel]) -> tuple[BaseModel | None, int]:
    """Constrained JSON output (Ollama/vLLM enforce the schema). 1 retry, then None -> caller falls back."""
    llm = get_llm()
    if llm is None:
        return None, 0
    runnable = llm.with_structured_output(schema, method="json_schema", include_raw=True)
    for attempt in range(2):
        try:
            out = runnable.invoke([("system", system), ("user", user)])
            if out.get("parsed") is not None:
                return out["parsed"], _tokens(out.get("raw"))
            log.warning("structured output unparsed (attempt %d): %s", attempt, out.get("parsing_error"))
        except Exception as exc:  # network, timeout, schema error
            log.warning("structured call failed (attempt %d): %s", attempt, exc)
    return None, 0


def call_text(system: str, user: str) -> tuple[str | None, int]:
    llm = get_llm()
    if llm is None:
        return None, 0
    try:
        msg = llm.invoke([("system", system), ("user", user)])
        return str(msg.content), _tokens(msg)
    except Exception as exc:
        log.warning("text call failed: %s", exc)
        return None, 0


def health() -> str:
    if mock_mode():
        return "mock"
    try:
        text, _ = call_text("Reply with OK.", "ping")
        return f"ok:{os.getenv('LLM_PROVIDER', 'ollama')}/{os.getenv('LLM_MODEL', 'qwen2.5:7b-instruct')}" if text else "error: no reply"
    except Exception as exc:
        return f"error: {exc}"
