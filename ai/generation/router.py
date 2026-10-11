"""Intent parsing: LLM with constrained JSON, plus a rule-based fallback that always works."""

from __future__ import annotations

import re

from ai import llm
from ai.generation.prompts import ROUTER_SYSTEM, ROUTER_USER
from shared.models import CapacityChange, Intent, ParsedIntent, Weights


def rules_parse(request: str) -> ParsedIntent:
    """Keyword fallback. Used when MOCK_LLM=true or the LLM fails twice. Also the baseline config in eval."""
    t = request.lower()
    order = re.search(r"\b(?:order\s*#?\s*|o-?)(\d+)\b", t)
    plant = re.search(r"\bplant\s+([a-z])\b", t)
    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    move = re.search(r"\b(?:put|move|force|assign|build)\b.*?\b(?:order\s*#?\s*|o-?)(\d+)\b.*?\bplant\s+([a-z])\b", t)

    if move:
        return ParsedIntent(intent=Intent.ALLOCATE, force_assignments={move.group(1): move.group(2).upper()},
                            reason="rules: manager override", parsed_by="rules")
    if re.search(r"\b(why|explain|reason)\b", t) and order:
        return ParsedIntent(intent=Intent.EXPLAIN, order_id=order.group(1), reason="rules: why + order", parsed_by="rules")
    # a named plant + a capacity-loss word + a % is a re-plan even if carbon is mentioned
    # ("Plant B loses 30%, what happens to carbon?")
    if plant and pct and re.search(r"\b(?:los[et]s?|lost|down|outage)\b|\bcapacity\s+(?:is\s+|was\s+)?reduc\w*", t):
        return ParsedIntent(intent=Intent.REPLAN, reason="rules: capacity loss", parsed_by="rules",
                            capacity_changes=[CapacityChange(plant=plant.group(1).upper(),
                                                             capacity_loss_pct=float(pct.group(1)))])
    # otherwise carbon before capacity loss: "20% CO2 reduction" is a trade-off, not a plant losing capacity
    if re.search(r"\b(carbon|low[- ]carbon|co2(?: reduction)?|emissions?|greener|trade-?off)\b", t):
        if re.search(r"\ballocat\w*", t) and not pct:   # "allocate with carbon weighted heavily": a preference
            return ParsedIntent(intent=Intent.ALLOCATE, reason="rules: allocation with carbon preference",
                                parsed_by="rules")
        return ParsedIntent(intent=Intent.TRADEOFF, carbon_cut_pct=float(pct.group(1)) if pct else None,
                            reason="rules: carbon trade-off", parsed_by="rules")
    if re.search(r"\b(los[et]s?|down|outage|reduc\w*|re-?plan)\b", t):
        changes = ([CapacityChange(plant=plant.group(1).upper(), capacity_loss_pct=float(pct.group(1)))]
                   if plant and pct else [])
        return ParsedIntent(intent=Intent.REPLAN, capacity_changes=changes, reason="rules: capacity loss", parsed_by="rules")
    if re.search(r"\b(brief|leadership|management|summary report)\b", t):
        return ParsedIntent(intent=Intent.BRIEF, reason="rules: brief", parsed_by="rules")
    if re.search(r"\b(allocat\w*|plan|assign|cheapest|schedule)\b", t):
        on_time = bool(re.search(r"on[- ]time", t)) and "cheapest" in t
        return ParsedIntent(intent=Intent.ALLOCATE, require_all_on_time=on_time,
                            weights_override=Weights(cost=1, carbon=0) if on_time else None,
                            reason="rules: allocation", parsed_by="rules")
    return ParsedIntent(intent=Intent.CLARIFY, reason="I can allocate orders, show the cost/carbon trade-off, "
                        "re-plan after a capacity loss, explain an order, or write a brief. Which do you need?",
                        parsed_by="rules")


def parse_intent(request: str) -> tuple[ParsedIntent, int, bool]:
    """Returns (intent, tokens, used_llm)."""
    parsed, tokens = llm.call_structured(ROUTER_SYSTEM, ROUTER_USER.format(request=request), ParsedIntent)
    if isinstance(parsed, ParsedIntent):
        parsed.parsed_by = "llm"
        return parsed, tokens, True
    fallback = rules_parse(request)
    if llm.mock_mode():
        fallback.parsed_by = "mock"
    return fallback, tokens, not llm.mock_mode()
