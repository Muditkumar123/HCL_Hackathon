"""Guardrails. Every function here is deterministic (no LLM), so it is unit-testable.

Layers (each one is a LangGraph node or runs inside one, and writes a TraceStep):
  G1 input_guard       before any LLM call: length, prompt injection, rule-breaking, out-of-data, PII masking
  G2 intent_guard      after the LLM parses intent: values must be grounded in the request, entities must exist,
                       forced assignments must be eligible, required slots must be present
  G3 plan gate         ai/tools/validator.validate_plan (in graph.checker): an invalid plan is never shown as valid
  G4 output_guard      every number the LLM writes must come from FACTS; bounded retry, then template fallback
  G5 approval          graph.finalize forces requires_approval=True / approval_status="pending"
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from shared.models import AnswerType, CapacityChange, Dataset, Intent, ParsedIntent

MAX_REQUEST_CHARS = 1000

# ---------------------------------------------------------------- G1 patterns
INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"\bsystem prompt\b", r"\byou are now\b", r"\bdeveloper mode\b", r"\bjailbreak\b",
    r"pretend (to be|you are)", r"disregard (your|the) (rules|instructions)",
]
RULE_BREAK_PATTERNS = [
    r"ignore (the |all )?(capacity|eligibility|due dates?|delivery)",
    r"(exceed|overload|break|bypass|skip|violate)\b.{0,30}\b(capacity|eligib\w*|rules?)",
    r"even (though|if) (it|the plant|plant \w) (can'?t|cannot|is unable)",
    r"(write|make|create|edit|change|produce) (the |an |my |a )?(allocation|plan|schedule) (yourself|manually|by hand|without the solver)",
    r"(make up|invent|fabricate|guess) (a |the |some )?(numbers?|figures?|costs?|values?)",
]
OUT_OF_DATA_PATTERNS = [
    r"next (year|quarter|month)'?s?\b.{0,25}\b(prices?|costs?|tariffs?|demand)",
    r"(future|forecast(ed)?) (energy )?(price|demand|tariff)",
    r"\b(stock|share) price\b", r"\bcompetitors?\b",
]
# G2: the LLM may set these flags only when the user's words ask for them
ON_TIME_RE = re.compile(r"\bon[- ]time\b|\bno late\b", re.IGNORECASE)
WEIGHTS_RE = re.compile(r"\bweight\w*|\bcheapest\b|\bcost[- ]only\b|\bgreenest\b", re.IGNORECASE)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s-]{8,}\d)(?!\w)")


@dataclass
class GuardVerdict:
    ok: bool
    answer_type: AnswerType | None = None   # set when ok=False (refused / clarification_needed)
    reason: str = ""
    check: str = ""                          # short text for the trace
    cleaned: str = ""                        # request after PII masking
    notes: list[str] = field(default_factory=list)


def _match_any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            return p
    return None


def input_guard(request: str) -> GuardVerdict:
    """G1: deterministic checks before the LLM sees anything."""
    text = (request or "").strip()
    if not text:
        return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED, "The request is empty.", "empty")
    if len(text) > MAX_REQUEST_CHARS:
        return GuardVerdict(False, AnswerType.REFUSED,
                            f"Request is longer than {MAX_REQUEST_CHARS} characters.", "too_long")
    if p := _match_any(INJECTION_PATTERNS, text):
        return GuardVerdict(False, AnswerType.REFUSED,
                            "The request tries to change the system's instructions. It was not processed.",
                            f"prompt_injection:{p}")
    if p := _match_any(RULE_BREAK_PATTERNS, text):
        return GuardVerdict(False, AnswerType.REFUSED,
                            "Plans must respect capacity, eligibility and delivery rules, and only the solver "
                            "creates allocations. I can show the closest valid plan or the cost of a valid change instead.",
                            f"rule_break:{p}")
    if p := _match_any(OUT_OF_DATA_PATTERNS, text):
        return GuardVerdict(False, AnswerType.REFUSED,
                            "Not in the data: the data pack has no forecasts or external market data, so I won't guess.",
                            f"out_of_data:{p}")
    cleaned = PHONE_RE.sub("[PHONE]", EMAIL_RE.sub("[EMAIL]", text))
    notes = ["PII masked before LLM call"] if cleaned != text else []
    return GuardVerdict(True, check="pass", cleaned=cleaned, notes=notes)


# ---------------------------------------------------------------- G2
def _numbers_in(text: str) -> set[float]:
    return {float(n) for n in re.findall(r"\d+(?:\.\d+)?", text or "")}


def normalise_order_id(raw: str, ds: Dataset) -> str | None:
    """'77', 'O-77', 'o-077' -> the exact id in orders.csv, or None."""
    ids = ds.orders["order_id"].astype(str)
    if raw in set(ids):
        return raw
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    matches = [i for i in ids if re.sub(r"\D", "", i).lstrip("0") == digits.lstrip("0")]
    return matches[0] if len(matches) == 1 else None


def intent_guard(intent: ParsedIntent, request: str, scenario_numbers: set[float],
                 ds: Dataset) -> tuple[GuardVerdict, ParsedIntent]:
    """G2: the LLM may only extract, never invent. Returns (verdict, sanitised intent)."""
    if intent.intent in (Intent.REFUSE, Intent.CLARIFY):
        at = AnswerType.REFUSED if intent.intent == Intent.REFUSE else AnswerType.CLARIFICATION_NEEDED
        return GuardVerdict(False, at, intent.reason or "Please rephrase the request.", f"router:{intent.intent.value}"), intent

    stated = _numbers_in(request) | scenario_numbers
    plants = set(ds.plants["plant"].astype(str))
    notes: list[str] = []

    # numbers must be stated by the user, not invented by the LLM
    if intent.carbon_cut_pct is not None and intent.carbon_cut_pct not in stated:
        notes.append(f"dropped ungrounded carbon_cut_pct={intent.carbon_cut_pct}")
        intent.carbon_cut_pct = None
    kept: list[CapacityChange] = []
    for c in intent.capacity_changes:
        if c.plant not in plants:
            return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                                f"Plant '{c.plant}' is not in the data. Known plants: {', '.join(sorted(plants))}.",
                                "unknown_plant"), intent
        if c.capacity_loss_pct not in stated and c.capacity_loss_pct / 100 not in stated:
            notes.append(f"dropped ungrounded capacity_loss_pct={c.capacity_loss_pct} for {c.plant}")
            continue
        kept.append(c)
    intent.capacity_changes = kept

    # objective flags must be asked for, or a plain "allocate" silently ignores the user's weights
    if intent.require_all_on_time and not ON_TIME_RE.search(request):
        notes.append("dropped ungrounded require_all_on_time (request does not ask for on-time delivery)")
        intent.require_all_on_time = False
    if intent.weights_override is not None and not WEIGHTS_RE.search(request):
        w = intent.weights_override
        notes.append(f"dropped ungrounded weights_override cost={w.cost:g} carbon={w.carbon:g} "
                     "(request names no weights)")
        intent.weights_override = None

    # entities must exist
    if intent.order_id is not None:
        oid = normalise_order_id(intent.order_id, ds)
        if oid is None:
            return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                                f"Order '{intent.order_id}' is not in the data.", "unknown_order"), intent
        intent.order_id = oid

    # forced assignments must respect eligibility (we price choices, we refuse rule-breaking)
    for raw_oid, plant in list(intent.force_assignments.items()):
        oid = normalise_order_id(raw_oid, ds)
        if oid is None or plant not in plants:
            return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                                f"Cannot find order '{raw_oid}' or plant '{plant}'.", "unknown_entity"), intent
        product = ds.orders.loc[ds.orders["order_id"] == oid, "product"].iloc[0]
        makers = set(ds.plants.loc[ds.plants["product"] == product, "plant"].astype(str))
        if plant not in makers:
            return GuardVerdict(False, AnswerType.REFUSED,
                                f"Plant {plant} cannot make product {product} (order {oid}). "
                                f"Eligible plants: {', '.join(sorted(makers))}.", "eligibility_refusal"), intent
        intent.force_assignments.pop(raw_oid)
        intent.force_assignments[oid] = plant

    # required slots
    if intent.intent == Intent.REPLAN and not intent.capacity_changes:
        return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                            "Which plant loses capacity, and by what percentage?", "missing_slot:capacity"), intent
    if intent.intent == Intent.EXPLAIN and not intent.order_id:
        return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                            "Which order should I explain? Please give the order id.", "missing_slot:order_id"), intent
    if intent.intent == Intent.TRADEOFF and intent.carbon_cut_pct is None:
        notes.append("no carbon cut stated; showing the full trade-off table")

    return GuardVerdict(True, check="pass" + (f" ({len(notes)} fixes)" if notes else ""), notes=notes), intent


# ---------------------------------------------------------------- G4
NUM_RE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*([kKmM](?![a-zA-Z])|%)?")
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
APPROVAL_SENTENCE = "This plan is a proposal and needs management approval."


def extract_numbers(text: str) -> list[float]:
    """'1,840,000' -> 1840000 · '1.84M' -> 1840000 · '3k' -> 3000 · '12.5%' -> 12.5"""
    out: list[float] = []
    for num, suffix in NUM_RE.findall(text or ""):
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        if suffix in ("k", "K"):
            v *= 1_000
        elif suffix in ("m", "M"):
            v *= 1_000_000
        out.append(v)
    return out


def allowed_numbers(facts: dict, request: str) -> set[float]:
    """Every number that appears anywhere in FACTS (values and pre-formatted strings) or in the request,
    plus percentage <-> fraction variants."""
    base = set(extract_numbers(json.dumps(facts, default=str))) | set(extract_numbers(request))
    extra = {round(v * 100, 2) for v in base if 0 < v <= 1}           # 0.88 -> 88 (%)
    return base | extra


def _matches(x: float, allowed: set[float], rel_tol: float = 0.003) -> bool:
    return any(abs(x - a) <= max(rel_tol * abs(a), 0.051) for a in allowed)


def check_faithfulness(text: str, facts: dict, request: str) -> tuple[bool, list[float], int]:
    """Returns (ok, unmatched numbers, total numbers checked). The tolerance allows display rounding
    (1,842,317 -> 1.84M) but not a new figure."""
    allowed = allowed_numbers(facts, request)
    found = extract_numbers(text.replace(APPROVAL_SENTENCE, ""))
    bad = [x for x in found if not _matches(x, allowed)]
    return (not bad), bad, len(found)


def clean_llm_text(text: str, max_chars: int = 2500) -> str:
    text = THINK_RE.sub("", text or "").strip()
    text = text[:max_chars]
    if APPROVAL_SENTENCE not in text:
        text = f"{text}\n\n{APPROVAL_SENTENCE}".strip()
    return text


def template_summary(answer_type: AnswerType, facts: dict) -> str:
    """Deterministic fallback when the LLM fails the number check twice (or MOCK_LLM=true)."""
    k = facts.get("kpis", {})
    parts = []
    if k:
        parts.append(f"Plan cost {k.get('total_cost', 'n/a')}, carbon {k.get('total_carbon_t', 'n/a')}, "
                     f"{k.get('late_orders', 'n/a')} late orders.")
    if facts.get("changes_count") is not None:
        parts.append(f"{facts['changes_count']} orders moved; cost change {facts.get('cost_delta', 'n/a')}, "
                     f"carbon change {facts.get('carbon_delta_t', 'n/a')}.")
    if facts.get("tradeoff_row"):
        r = facts["tradeoff_row"]
        parts.append(f"A {r['carbon_cut_pct']} carbon cut costs {r['cost_delta']} more, with {r['late_orders']} late orders.")
    if facts.get("explanation"):
        parts.append(str(facts["explanation"]))
    if not parts:
        parts.append(f"Result type: {answer_type.value}.")
    return " ".join(parts) + " " + APPROVAL_SENTENCE
