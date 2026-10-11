"""Turns FACTS (computed by tools) into words. One LLM call per attempt; the graph's output_guard checks it."""

from __future__ import annotations

import json

from ai import llm
from ai.generation.prompts import BRIEF_USER, RETRY_SUFFIX, SUMMARY_USER, WRITER_SYSTEM
from shared.models import AnswerType


def write(request: str, answer_type: AnswerType, facts: dict, mode: str,
          bad_numbers: list[str] | None = None) -> tuple[str | None, int]:
    """mode: 'summary' | 'brief'. Returns (text or None if no LLM, tokens)."""
    facts_json = json.dumps(facts, indent=1, default=str)
    if mode == "brief":
        user = BRIEF_USER.format(facts_json=facts_json)
    else:
        user = SUMMARY_USER.format(request=request, answer_type=answer_type.value, facts_json=facts_json)
    if bad_numbers:
        user += RETRY_SUFFIX.format(bad_numbers="; ".join(map(str, bad_numbers)))
    return llm.call_text(WRITER_SYSTEM, user)
