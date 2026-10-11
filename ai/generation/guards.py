"""Guardrails. Every function here is deterministic (no LLM), so it is unit-testable.

Layers (each one is a LangGraph node or runs inside one, and writes a TraceStep):
  G1 input_guard       before any LLM call: length, prompt injection, rule-breaking, fabrication, out-of-data,
                       PII masking. Regex on Unicode-normalised text; defense in depth, not the security boundary.
  G2 intent_guard      after the LLM parses intent: every solver-affecting field must be grounded in the user's
                       words (plant, %, order, date, forced move, weights, on-time) or come from the explicit API
                       scenario; entities must exist, forced moves must be eligible, required slots present.
                       Refusal/clarification text is fixed code text; the LLM's reason stays in the trace.
  G3 plan gate         ai/tools/validator.validate_plan (in graph.checker): an invalid plan is never shown
  G4 output_guard      every number, id, date and plant the LLM writes must match a FACTS value of the same
                       unit and sign; plant rankings, moves, capacity and lateness claims must be backed by FACTS.
                       Fails closed (bounded retry, then template fallback).
  G5 approval          graph._assemble forces requires_approval=True / approval_status="pending"
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from shared.models import AnswerType, Dataset, Intent, ParsedIntent, Scenario

MAX_REQUEST_CHARS = 1000
REFUSE_MESSAGE = ("I can't do that. Plans must respect capacity, eligibility and delivery rules, only the solver "
                  "creates allocations, and I only use the loaded data.")
CLARIFY_MESSAGE = ("I can allocate orders, show the cost/carbon trade-off, re-plan after a capacity loss, explain an "
                   "order, or write a brief. Which do you need?")

# ---------------------------------------------------------------- normalisation (G1, G2, G4)
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_PUNCT = {ord("‘"): "'", ord("’"): "'", ord("“"): '"', ord("”"): '"', ord("⁄"): "/"}


def normalise(text: str, keep_lines: bool = False) -> str:
    """NFKC (½ -> 1/2, full-width -> ASCII), one kind of dash and quote, collapsed whitespace."""
    t = unicodedata.normalize("NFKC", text or "").translate(_DASHES).translate(_PUNCT)
    if keep_lines:
        return "\n".join(re.sub(r"[ \t\f\v]+", " ", line).strip() for line in t.splitlines()).strip()
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- G1 patterns (matched on normalised text)
INJECTION_PATTERNS = [
    r"\b(ignore|forget|disregard|override|bypass)\b.{0,40}\b(instructions?|rules|prompts?|safeguards?|guardrails?"
    r"|guidelines|policies|restrictions)\b",
    r"\b(reveal|show|print|repeat|leak|display)\b.{0,30}\b(hidden|system|developer|secret|internal)\s+"
    r"(prompt|instructions|rules|message)",
    r"\bsystem prompt\b", r"\byou(?: are|'re) (now|no longer)\b", r"\bdeveloper mode\b", r"\bjailbreak\b",
    r"\bunrestricted\b", r"\bpretend (to be|you are|you're)\b",
]
RULE_BREAK_PATTERNS = [
    r"\bignor\w* (the |all |any )?(capacity|eligibility|due dates?|delivery|constraints?|limits?)",
    r"\b(exceed|overload|overbook\w*|break|bypass|skip|violat\w*)\b.{0,30}\b(capacity|eligib\w*|rules?|constraints?"
    r"|due dates?|deadlines?|delivery)",
    r"\boverbook\w*",
    r"\b(capacity|eligibility|constraints?|due dates?|rules)\b.{0,30}\b(unlimited|infinite|did not exist|didn't exist"
    r"|do not exist|don't exist|does not exist|doesn't exist|do not apply|don't apply|does not apply|doesn't apply)",
    r"\b(unlimited|infinite) capacity\b",
    r"\bregardless of (the |its |their )?(capacity|eligibility|due dates?|delivery|constraints?|rules)",
    r"\bdespite\b.{0,50}\b(ineligible|not eligible|unable to (make|produce|manufacture|build)"
    r"|(cannot|can't|can not) (make|produce|manufacture|build))",
    r"\b(although|though|even if|even though)\s+(it|they|the plant|plant \w+)\s+(cannot|can't|can not|is unable"
    r"|isn't able|is not able)",
    r"\b(write|make|create|edit|change|produce|do|build|generate)\s+(the |an |my |a |this )?(allocation|plan|schedule)"
    r"\s+(yourself|manually|by hand|directly|without)",
    r"\bwithout (using |calling |running )?(the |any )?(solver|optimi[sz]er|optimi[sz]ation|validator)\b",
    r"\bmanually (allocate|assign|plan|schedule|place|move)\b|\bby hand\b",
]
FABRICATION_PATTERNS = [
    r"\b(fictional|fictitious|fake|dummy|made[- ]up|imaginary|placeholder|bogus)\b",
    r"\bhallucinat\w*",
    r"\b(make up|invent|fabricate)\s+(a |an |any |the |some |all )?(numbers?|figures?|values?|costs?|data|savings?"
    r"|prices?)\b",
    r"\b(estimate|guess|assume|fill in)\s+(any |the |all )?missing\b",
    r"\bguess\b.{0,20}\b(numbers?|figures?|costs?|values?)\b",
    r"\bassume\b.{0,30}\b(costs?|prices?|savings?|carbon|emissions?|total)\b.{0,20}\d",
]
OUT_OF_DATA_PATTERNS = [
    r"next (year|quarter|month)'?s?\b.{0,25}\b(prices?|costs?|tariffs?|demand)",
    r"(future|forecast(ed)?) (energy )?(price|demand|tariff)",
    r"\b(stock|share) price\b", r"\bcompetitors?\b",
]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d\s-]{8,}\d)(?!\w)")


@dataclass
class GuardVerdict:
    ok: bool
    answer_type: AnswerType | None = None   # set when ok=False (refused / clarification_needed)
    reason: str = ""
    check: str = ""                          # short text for the trace
    cleaned: str = ""                        # normalised request after PII masking (what the LLMs see)
    notes: list[str] = field(default_factory=list)


def _match_any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, flags=re.IGNORECASE):
            return p
    return None


def input_guard(request: str) -> GuardVerdict:
    """G1: deterministic checks before the LLM sees anything."""
    text = normalise(request)
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
    if p := _match_any(FABRICATION_PATTERNS, text):
        return GuardVerdict(False, AnswerType.REFUSED,
                            "I only report numbers that the solver computes from the loaded data. I won't invent, "
                            "estimate or assume figures.", f"fabrication:{p}")
    if p := _match_any(OUT_OF_DATA_PATTERNS, text):
        return GuardVerdict(False, AnswerType.REFUSED,
                            "Not in the data: the data pack has no forecasts or external market data, so I won't guess.",
                            f"out_of_data:{p}")
    cleaned = PHONE_RE.sub("[PHONE]", EMAIL_RE.sub("[EMAIL]", text))
    notes = ["PII masked before LLM call"] if cleaned != text else []
    return GuardVerdict(True, check="pass", cleaned=cleaned, notes=notes)


# ---------------------------------------------------------------- G2
# the LLM may set these only when the user's words ask for them
ON_TIME_RE = re.compile(r"\bon[- ]time\b|\bno late\b", re.IGNORECASE)
WEIGHTS_RE = re.compile(r"\bweight\w*|\bcheapest\b|\bcost[- ]only\b|\bgreenest\b", re.IGNORECASE)
PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s?cent\b|pct\b)", re.IGNORECASE)
ORDER_REF_RE = re.compile(r"\b(?:order\s*#?\s*|o-?)(\d+)\b", re.IGNORECASE)
PLANT_REF_RE = re.compile(r"\bplants?\s+([a-z0-9][\w-]{0,9})\b", re.IGNORECASE)
ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def _digits(raw: str) -> str:
    return re.sub(r"\D", "", raw).lstrip("0") or "0"


def _safe(raw: Any) -> str:
    """Entity names echoed back to the user: letters, digits and dashes only."""
    return re.sub(r"[^\w-]", "", str(raw))[:20]


def normalise_order_id(raw: str, ds: Dataset) -> str | None:
    """'77', 'O-77', 'o-077' -> the exact id in orders.csv, or None."""
    ids = ds.orders["order_id"].astype(str)
    if raw in set(ids):
        return raw
    if not re.sub(r"\D", "", raw):
        return None
    matches = [i for i in ids if _digits(i) == _digits(raw)]
    return matches[0] if len(matches) == 1 else None


def _ground(intent: ParsedIntent, request: str, notes: list[str]) -> None:
    """Drop every solver-affecting field the user did not state (REVIEW-001 #2). Mutates intent."""
    pcts = {float(p) for p in PCT_RE.findall(request)}
    orders = {_digits(d) for d in ORDER_REF_RE.findall(request)}
    plants_said = {p.upper() for p in PLANT_REF_RE.findall(request)}
    dates = set(ISO_DATE_RE.findall(request))

    if intent.carbon_cut_pct is not None and intent.carbon_cut_pct not in pcts:
        notes.append(f"dropped ungrounded carbon_cut_pct={intent.carbon_cut_pct:g} (no such % in the request)")
        intent.carbon_cut_pct = None
    kept = []
    for c in intent.capacity_changes:
        if c.plant.upper() not in plants_said:
            notes.append(f"dropped capacity change for plant {_safe(c.plant)} (plant not named in the request)")
            continue
        if c.capacity_loss_pct not in pcts:
            notes.append(f"dropped ungrounded capacity_loss_pct={c.capacity_loss_pct:g} (no such % in the request)")
            continue
        for attr in ("from_date", "to_date"):
            d = getattr(c, attr)
            if d is not None and d.isoformat() not in dates:
                notes.append(f"dropped ungrounded {attr}={d.isoformat()} for plant {_safe(c.plant)}")
                setattr(c, attr, None)
        kept.append(c)
    intent.capacity_changes = kept
    if intent.order_id is not None and _digits(intent.order_id) not in orders:
        notes.append(f"dropped ungrounded order_id={_safe(intent.order_id)} (order not named in the request)")
        intent.order_id = None
    for oid, plant in list(intent.force_assignments.items()):
        if _digits(oid) not in orders or plant.upper() not in plants_said:
            notes.append(f"dropped ungrounded forced move {_safe(oid)}->{_safe(plant)}")
            intent.force_assignments.pop(oid)
    # objective flags must be asked for, or a plain "allocate" silently ignores the user's weights
    if intent.require_all_on_time and not ON_TIME_RE.search(request):
        notes.append("dropped ungrounded require_all_on_time (request does not ask for on-time delivery)")
        intent.require_all_on_time = False
    if intent.weights_override is not None and not WEIGHTS_RE.search(request):
        w = intent.weights_override
        notes.append(f"dropped ungrounded weights_override cost={w.cost:g} carbon={w.carbon:g} "
                     "(request names no weights)")
        intent.weights_override = None


def intent_guard(intent: ParsedIntent, request: str, ds: Dataset,
                 scenario: Scenario | None = None) -> tuple[GuardVerdict, ParsedIntent]:
    """G2: the LLM may only extract, never invent; the explicit API scenario is trusted and wins.
    Returns (verdict, sanitised intent)."""
    if intent.intent == Intent.REFUSE:
        return GuardVerdict(False, AnswerType.REFUSED, REFUSE_MESSAGE, "router:refuse"), intent
    request = normalise(request)
    notes: list[str] = []
    _ground(intent, request, notes)
    if scenario is not None:
        if scenario.capacity_changes:
            intent.capacity_changes = [c.model_copy() for c in scenario.capacity_changes]
            if intent.intent in (Intent.ALLOCATE, Intent.CLARIFY):
                intent.intent = Intent.REPLAN
        if scenario.carbon_cut_pct is not None:
            intent.carbon_cut_pct = scenario.carbon_cut_pct
    if intent.intent == Intent.CLARIFY:
        return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED, CLARIFY_MESSAGE, "router:clarify",
                            notes=notes), intent

    plants = {p.upper(): p for p in ds.plants["plant"].astype(str)}
    for c in intent.capacity_changes:
        if c.plant.upper() not in plants:
            return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                                f"Plant '{_safe(c.plant)}' is not in the data. Known plants: "
                                f"{', '.join(sorted(plants.values()))}.", "unknown_plant", notes=notes), intent
        c.plant = plants[c.plant.upper()]

    if intent.order_id is not None:
        oid = normalise_order_id(intent.order_id, ds)
        if oid is None:
            return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                                f"Order '{_safe(intent.order_id)}' is not in the data.", "unknown_order",
                                notes=notes), intent
        intent.order_id = oid

    # forced assignments must respect eligibility (we price choices, we refuse rule-breaking)
    for raw_oid, raw_plant in list(intent.force_assignments.items()):
        oid, plant = normalise_order_id(raw_oid, ds), plants.get(raw_plant.upper())
        if oid is None or plant is None:
            return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                                f"Cannot find order '{_safe(raw_oid)}' or plant '{_safe(raw_plant)}'.",
                                "unknown_entity", notes=notes), intent
        product = ds.orders.loc[ds.orders["order_id"] == oid, "product"].iloc[0]
        makers = set(ds.plants.loc[ds.plants["product"] == product, "plant"].astype(str))
        if plant not in makers:
            return GuardVerdict(False, AnswerType.REFUSED,
                                f"Plant {plant} cannot make product {product} (order {oid}). "
                                f"Eligible plants: {', '.join(sorted(makers))}.", "eligibility_refusal",
                                notes=notes), intent
        intent.force_assignments.pop(raw_oid)
        intent.force_assignments[oid] = plant

    # required slots
    if intent.intent == Intent.REPLAN and not intent.capacity_changes:
        return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                            "Which plant loses capacity, and by what percentage?", "missing_slot:capacity",
                            notes=notes), intent
    if intent.intent == Intent.EXPLAIN and not intent.order_id:
        return GuardVerdict(False, AnswerType.CLARIFICATION_NEEDED,
                            "Which order should I explain? Please give the order id.", "missing_slot:order_id",
                            notes=notes), intent
    if intent.intent == Intent.TRADEOFF and intent.carbon_cut_pct is None:
        notes.append("no carbon cut stated; showing the full trade-off table")

    return GuardVerdict(True, check="pass" + (f" ({len(notes)} fixes)" if notes else ""), notes=notes), intent


# ---------------------------------------------------------------- G4 tokens
THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
APPROVAL_SENTENCE = "This plan is a proposal and needs management approval."
REL_TOL, ABS_TOL = 0.003, 0.051            # display rounding (1,842,317 -> 1.84M) but not a new figure

_ID_EXEMPT = {"CO2", "TCO2", "KGCO2"}
ID_RE = re.compile(r"\b(?:(?P<pfx>[A-Za-z]{1,3})-?(?P<d1>\d+)|order\s*#?\s*(?P<d2>\d+))\b", re.IGNORECASE)
PLANT_RE = re.compile(r"\b(?i:plants?)\s+(?P<p>[A-Z][A-Z0-9-]{0,3}|[a-z]|\d{1,3})\b")
NUM_RE = re.compile(r"""
    (?:(?<![\w.])(?P<sign>[-+]))?
    (?<![\w.])(?P<num>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)
    (?P<exp>[eE][-+]?\d+)?
    (?:/(?P<den>\d+))?
    (?:\s*(?P<scale>thousand|lakhs?|lacs?|millions?|mn|crores?|billions?|bn|trillions?|[kKmM])(?![A-Za-z]))?
    (?:\s*(?P<unit>%|percent\b|per\s?cent\b|pct\b|tonnes?\b|tons?\b|t\b|kgs?\b|kilograms?\b))?
""", re.VERBOSE)
UNIT_RE = re.compile(r"^\s*(?P<unit>%|percent\b|per\s?cent\b|pct\b|tonnes?\b|tons?\b|t\b|kgs?\b|kilograms?\b)",
                     re.IGNORECASE)
RANGE_GAP_RE = re.compile(r"^\s*(?:-|to)\s*$", re.IGNORECASE)
_SCALES = {"thousand": 1e3, "k": 1e3, "lakh": 1e5, "lac": 1e5, "million": 1e6, "mn": 1e6, "m": 1e6,
           "crore": 1e7, "billion": 1e9, "bn": 1e9, "trillion": 1e12}
_SMALL = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split(), start=2)}
_FRACTIONS = {"half": 0.5, "quarter": 0.25, "dozen": 12}
_WORD_SCALES = {"hundred": 100, **{k: v for k, v in _SCALES.items() if len(k) > 2}}
NUMBER_WORDS = set(_SMALL) | set(_TENS) | set(_FRACTIONS) | set(_WORD_SCALES)
WORD_RE = re.compile(r"[A-Za-z]+")


def _unit(raw: str | None) -> str:
    r = (raw or "").lower()
    if r in ("%", "pct") or r.startswith("per"):
        return "%"
    if r.startswith("t"):
        return "t"
    return "kg" if r.startswith("k") else ""


def _scale(raw: str | None) -> float:
    r = (raw or "").lower()
    return _SCALES.get(r.rstrip("s"), _SCALES.get(r, 1.0)) if r else 1.0


@dataclass(frozen=True)
class Token:
    value: float
    signed: bool       # an explicit + or - was written
    unit: str          # "%" | "t" | "kg" | "" (money and counts)
    raw: str


def _numeric_tokens(text: str) -> tuple[list[Token], str]:
    """Digit numbers (signs, ranges, scales, fractions, scientific) and then number words.
    Returns the tokens and the text with them blanked out."""
    matches = list(NUM_RE.finditer(text))
    out: list[Token] = []
    for i, m in enumerate(matches):
        v = float(m["num"].replace(",", "") + (m["exp"] or ""))
        if m["den"]:
            v = v / float(m["den"]) if float(m["den"]) else v
        scale, unit = m["scale"], m["unit"]
        nxt = matches[i + 1] if i + 1 < len(matches) else None
        if nxt is not None and RANGE_GAP_RE.match(text[m.end():nxt.start()]) and not scale:
            scale, unit = scale or nxt["scale"], unit or nxt["unit"]      # "10-20 million", "18-30 lakh", "5-10%"
        v *= _scale(scale)
        if m["sign"] == "-":
            v = -v
        out.append(Token(v, bool(m["sign"]), _unit(unit), m.group(0).strip()))
    blank = NUM_RE.sub(lambda m: " " * len(m.group(0)), text)

    words = list(WORD_RE.finditer(blank))
    i = 0
    while i < len(words):
        if words[i].group(0).lower() not in NUMBER_WORDS:
            i += 1
            continue
        j, run = i, []
        while j < len(words) and (words[j].group(0).lower() in NUMBER_WORDS
                                  or (run and words[j].group(0).lower() in ("and", "a"))):
            run.append(words[j].group(0).lower())
            j += 1
        total = cur = 0.0
        for w in run:
            if w in _SMALL:
                cur += _SMALL[w]
            elif w in _TENS:
                cur += _TENS[w]
            elif w in _FRACTIONS:
                cur = (cur or 1) * _FRACTIONS[w]
            elif w == "hundred":
                cur = (cur or 1) * 100
            elif w in _WORD_SCALES:
                total, cur = total + (cur or 1) * _WORD_SCALES[w], 0.0
        u = UNIT_RE.match(blank[words[j - 1].end():])
        out.append(Token(total + cur, False, _unit(u["unit"] if u else None), " ".join(run)))
        i = j
    return out, blank


@dataclass
class _Entities:
    ids: set[tuple[str | None, str]] = field(default_factory=set)   # (prefix or None for "order 77", digits)
    plants: set[str] = field(default_factory=set)
    dates: set[str] = field(default_factory=set)


def _entities(text: str) -> tuple[_Entities, str]:
    """Ids, plants and dates, which are identifiers rather than numeric claims. Returns them and the text
    with them blanked out, so '77' in 'O-077' is never read as a number."""
    e = _Entities()

    def blank(m: re.Match) -> str:
        return " " * len(m.group(0))

    def take_date(m: re.Match) -> str:
        e.dates.add(m.group(0))
        return blank(m)

    def take_id(m: re.Match) -> str:
        if m["pfx"] and (m["pfx"] + m["d1"]).upper() in _ID_EXEMPT:
            return m.group(0)
        e.ids.add((m["pfx"].upper() if m["pfx"] else None, _digits(m["d1"] or m["d2"])))
        return blank(m)

    def take_plant(m: re.Match) -> str:
        e.plants.add(m["p"].upper())
        return blank(m)

    text = ISO_DATE_RE.sub(take_date, text)
    text = ID_RE.sub(take_id, text)
    text = PLANT_RE.sub(take_plant, text)
    return e, text


# ---------------------------------------------------------------- G4 claims
_RANK = (r"cheap\w*|expensive|costl\w*|pricier|priciest|green\w*|clean\w*|dirt\w*|lowest|highest|lower|higher"
         r"|least|most|best|worst|better|worse|efficien\w*|optimal|ideal|prefer\w*")
_ACTION = r"mov\w*|shift\w*|reassign\w*|transfer\w*|relocat\w*|recommend\w*|should|suggest\w*|advis\w*"
_CAPACITY = r"capacity|spare|plenty|unlimited|idle|headroom"
CLAIM_RE = re.compile(rf"\b(?:{_RANK}|{_ACTION}|{_CAPACITY})\b", re.IGNORECASE)
ACTION_RE = re.compile(rf"\b(?:{_ACTION})\b", re.IGNORECASE)
EVERY_RE = re.compile(r"\b(?:every|all|each)\s+(?:the\s+)?orders?\b|\beverything\b", re.IGNORECASE)
NO_LATE_RE = re.compile(
    r"\b(?:no|zero|none of the)\s+(?:\w+\s+){0,2}late\b|\bnothing (?:is|will be) late\b"
    r"|\b(?:all|every|each)\s+(?:\w+\s+){0,2}orders?\s+(?:\w+\s+){0,3}on[- ]time\b", re.IGNORECASE)
SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
_PLANT_KEYS = {"plant", "from", "to", "chosen_plant", "alternative_plant", "from_plant", "to_plant"}


@dataclass
class _Facts:
    tokens: list[Token]
    ents: _Entities
    records: list[tuple[set[str], str]]     # (plants, lower-case text) that a plant claim can cite
    late: int | None                        # late + unassigned orders, None if FACTS has no KPIs


def _record_plants(d: dict) -> set[str]:
    out = {str(v).upper() for k, v in d.items() if k in _PLANT_KEYS and isinstance(v, str)}
    util = d.get("plant_utilisation")
    return out | ({str(p).upper() for p in util} if isinstance(util, dict) else set())


def _read_facts(facts: dict) -> _Facts:
    f = _Facts([], _Entities(), [], None)

    def add_text(s: str, path: list[str]) -> None:
        for sentence in SENTENCE_RE.split(normalise(s, keep_lines=True)):
            ents, rest = _entities(sentence)
            f.ents.ids |= ents.ids
            f.ents.plants |= ents.plants
            f.ents.dates |= ents.dates
            f.tokens.extend(_numeric_tokens(rest)[0])
            f.records.append((ents.plants, (" ".join(path) + " " + sentence).lower()))

    def walk(v: Any, path: list[str]) -> None:
        if isinstance(v, dict):
            if path:
                plants = _record_plants(v)
                f.ents.plants |= plants
                f.records.append((plants, (" ".join(path) + " " + json.dumps(v, default=str)).lower()))
            for k, x in v.items():
                walk(x, path + [str(k)])
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x, path)
        elif isinstance(v, str):
            add_text(v, path)
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            f.tokens.append(Token(float(v), False, "", str(v)))

    walk(facts, [])
    k = facts.get("kpis")
    if isinstance(k, dict) and isinstance(k.get("late_orders"), int):
        f.late = k["late_orders"] + int(k.get("unassigned_orders") or 0)
    return f


def _number_ok(t: Token, allowed: list[Token]) -> bool:
    for a in allowed:
        if a.unit != t.unit:
            continue
        x, y = (t.value, a.value) if t.signed else (abs(t.value), abs(a.value))
        if abs(x - y) <= max(REL_TOL * abs(y), ABS_TOL):
            return True
    return False


def _id_ok(i: tuple[str | None, str], known: set[tuple[str | None, str]]) -> bool:
    return any(d == i[1] and (i[0] is None or p is None or p == i[0]) for p, d in known)


def _claim_problems(sentence: str, plants: set[str], f: _Facts) -> list[str]:
    bad = []
    if NO_LATE_RE.search(sentence) and f.late != 0:
        bad.append(f"unsupported claim (late orders): '{sentence[:80]}'")
    if ACTION_RE.search(sentence) and EVERY_RE.search(sentence):
        bad.append(f"unsupported claim (plan-wide move): '{sentence[:80]}'")
    stems = {m.group(0).lower()[:4] for m in CLAIM_RE.finditer(sentence)}
    if plants and stems and not any(plants <= rp and all(s in rt for s in stems) for rp, rt in f.records):
        bad.append(f"unsupported claim (plant {', '.join(sorted(plants))}): '{sentence[:80]}'")
    return bad


def extract_numbers(text: str) -> list[float]:
    """'1,840,000' -> 1840000 · '1.84M' -> 1840000 · '3k' -> 3000 · '12.5%' -> 12.5 · ids and dates skipped"""
    return [t.value for t in _numeric_tokens(_entities(normalise(text))[1])[0]]


def check_faithfulness(text: str, facts: dict, request: str = "") -> tuple[bool, list[str], int]:
    """G4. Returns (ok, problems, items checked). Fails closed: every number must match a FACTS value of the
    same unit (and sign, if one is written); ids, dates and plants must exist in FACTS; plant rankings, moves,
    capacity and lateness claims must be backed by FACTS. The request is NOT a source of allowed numbers
    (REVIEW-001 #1); the grounded request parameters are already in FACTS."""
    f = _read_facts(facts)
    bad: list[str] = []
    checked = 0
    for sentence in SENTENCE_RE.split(normalise(text.replace(APPROVAL_SENTENCE, ""), keep_lines=True)):
        if not sentence:
            continue
        ents, rest = _entities(sentence)
        tokens = _numeric_tokens(rest)[0]
        checked += len(tokens) + len(ents.ids) + len(ents.plants) + len(ents.dates)
        bad += [t.raw for t in tokens if not _number_ok(t, f.tokens)]
        bad += [f"unknown id {(p or 'order ') + d}" for p, d in ents.ids if not _id_ok((p, d), f.ents.ids)]
        bad += [f"unknown plant {p}" for p in sorted(ents.plants - f.ents.plants)]
        bad += [f"unknown date {d}" for d in sorted(ents.dates - f.ents.dates)]
        bad += _claim_problems(sentence, ents.plants, f)
    return (not bad), bad, checked


def clean_llm_text(text: str, max_chars: int = 2500) -> str:
    text = THINK_RE.sub("", text or "").strip()
    text = text[:max_chars]
    if APPROVAL_SENTENCE not in text:
        text = f"{text}\n\n{APPROVAL_SENTENCE}".strip()
    return text


def template_summary(answer_type: AnswerType, facts: dict) -> str:
    """Deterministic fallback when the LLM fails the check twice (or MOCK_LLM=true). Only copies FACTS strings."""
    parts = []
    if facts.get("violations") is not None:
        parts.append("The proposed plan failed validation, so it is not shown: "
                     + "; ".join(facts["violations"][:3]) + ".")
    k = facts.get("kpis", {})
    if k:
        parts.append(f"Plan cost {k.get('total_cost', 'n/a')}, carbon {k.get('total_carbon_t', 'n/a')}, "
                     f"{k.get('late_orders', 'n/a')} late orders.")
    if facts.get("changes_count") is not None:
        parts.append(f"{facts['changes_count']} orders moved; cost change {facts.get('cost_delta', 'n/a')}, "
                     f"carbon change {facts.get('carbon_delta_t', 'n/a')}.")
    if r := facts.get("tradeoff_row"):
        if r.get("feasible") is False:
            deepest = facts.get("deepest_feasible_cut")
            parts.append(f"A {r['carbon_cut_pct']} carbon cut is not feasible within capacity and delivery rules"
                         + (f"; the deepest feasible cut is {deepest}" if deepest else "")
                         + ". The current plan is shown.")
        else:
            parts.append(f"A {r['carbon_cut_pct']} carbon cut changes total cost by {r['cost_delta']} to "
                         f"{r['cost']}, with {r['late_orders']} late orders.")
    if facts.get("explanation"):
        parts.append(str(facts["explanation"]))
    if not parts:
        parts.append(f"Result type: {answer_type.value}.")
    return " ".join(parts) + " " + APPROVAL_SENTENCE
