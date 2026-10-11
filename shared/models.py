"""Shared data contract for the Multi-Plant Order Allocation system.

Used by: backend/ (Shivang), ai/tools/ (Ayush), ai/generation/ + ai/graph.py + frontend/ (Mudit).

Rules
-----
* Only the Orchestrator edits this file. Every change is logged in DECISIONS.md.
* Field names mirror the HCL API contract (guide Section 6) and data schema (Annex B).
* Every number in a response comes from ai/tools (cost model / solver / validator).
  The LLM only fills `summary` and `brief`, and those are checked by the faithfulness guard.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    """Run ids look like the guide's example: 'al-62c1d8'."""
    return f"al-{uuid.uuid4().hex[:6]}"


# =============================================================================
# 1. Data schema (Annex B) - used by backend/loader.py and ai/tools/*
# =============================================================================

TableName = Literal["orders", "plants", "transport_duties", "capacity_events"]

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "orders": ["order_id", "product", "qty", "destination", "due"],
    "plants": [
        "plant", "product", "weekly_capacity", "unit_cost",
        "energy_kwh_per_unit", "carbon_kg_per_kwh",
    ],
    "transport_duties": [
        "plant", "destination", "transport_cost_per_unit",
        "transit_days", "duty_pct", "carbon_kg_per_unit",
    ],
    "capacity_events": ["event_id", "plant", "from_date", "to_date", "capacity_loss_pct"],
}

# Extra columns are allowed by the guide. We use these if present, otherwise we
# fall back to a stated assumption (logged in `assumptions`).
OPTIONAL_COLUMNS: dict[str, list[str]] = {
    "orders": ["priority", "customer"],
    "plants": ["energy_price_per_kwh", "week"],  # 'week' => carbon intensity varies by week
}


@dataclass
class Dataset:
    """In-memory snapshot of all tables. Built by backend.store.load_dataset().

    ai/tools functions take a Dataset and are pure (no DB access) so they are
    unit-testable with small fixture DataFrames.
    """

    orders: pd.DataFrame
    plants: pd.DataFrame
    transport_duties: pd.DataFrame
    capacity_events: pd.DataFrame  # may be empty
    planning_start: date
    data_version: str  # short hash of table contents; changes after /ingest


# =============================================================================
# 2. Request models
# =============================================================================


class Weights(BaseModel):
    """Objective weights. cost + carbon are re-normalised to sum to 1.

    Default 0.7/0.3: cost is the primary business goal, carbon is a real but
    secondary goal. The trade-off table shows the implied carbon price so
    management can see what the default means in money per tonne.
    `lateness` is a penalty multiplier per late order-day. The default of 1.0 is
    set so lateness dominates, because Annex A treats delivery as a promise.
    """

    model_config = ConfigDict(extra="ignore")

    cost: float = Field(0.7, ge=0)
    carbon: float = Field(0.3, ge=0)
    lateness: float = Field(1.0, ge=0)

    @model_validator(mode="after")
    def _normalise(self) -> "Weights":
        total = self.cost + self.carbon
        if total <= 0:
            raise ValueError("cost and carbon weights cannot both be zero")
        self.cost, self.carbon = round(self.cost / total, 4), round(self.carbon / total, 4)
        return self


class CapacityChange(BaseModel):
    """Same shape as a capacity_events.csv row (event_id optional)."""

    model_config = ConfigDict(extra="ignore")

    plant: str
    capacity_loss_pct: float = Field(..., ge=0, le=100)
    from_date: date | None = None  # None => whole horizon
    to_date: date | None = None
    event_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _fraction_to_pct(cls, data: Any) -> Any:
        # Accept 0.3 as well as 30 (the guide only says "share of capacity lost").
        if isinstance(data, dict):
            v = data.get("capacity_loss_pct")
            if isinstance(v, (int, float)) and 0 < v <= 1:
                data = {**data, "capacity_loss_pct": float(v) * 100}
        return data


class Scenario(BaseModel):
    """Optional what-if passed to /analyze. Lenient on purpose: judges may send
    either {"capacity_changes": [...]} or a single flat capacity-event object."""

    model_config = ConfigDict(extra="allow")

    capacity_changes: list[CapacityChange] = Field(default_factory=list)
    carbon_cut_pct: float | None = Field(None, ge=0, le=90)

    @model_validator(mode="before")
    @classmethod
    def _wrap_flat_event(cls, data: Any) -> Any:
        if isinstance(data, dict) and "plant" in data and "capacity_changes" not in data:
            return {"capacity_changes": [data]}
        return data


class AnalyzeRequest(BaseModel):
    request: str = Field(..., min_length=1, max_length=2000)
    weights: Weights | None = None
    scenario: Scenario | None = None


# =============================================================================
# 3. Internal contract: router (Mudit) -> tools (Ayush)
# =============================================================================


class Intent(str, Enum):
    ALLOCATE = "allocate"          # "Allocate this week's orders" / "cheapest on-time plan"
    TRADEOFF = "tradeoff"          # "What does a 10% carbon cut cost?"
    REPLAN = "replan"              # "Plant B loses 30% capacity. Re-plan."
    EXPLAIN = "explain_order"      # "Why was order 77 built in Plant C?"
    BRIEF = "brief"                # "Write the brief for the leadership team."
    CLARIFY = "clarify"            # missing plant / % / order id
    REFUSE = "refuse"              # break capacity/eligibility, LLM-written plan, out of data


class ParsedIntent(BaseModel):
    """Output of ai/generation/router.parse_intent. Contains NO plan numbers,
    only parameters extracted from the user's words."""

    intent: Intent
    order_id: str | None = None
    carbon_cut_pct: float | None = Field(None, ge=0, le=90)
    capacity_changes: list[CapacityChange] = Field(default_factory=list)
    require_all_on_time: bool = False      # "cheapest plan that is on time"
    weights_override: Weights | None = None
    force_assignments: dict[str, str] = Field(default_factory=dict)  # {"O-077": "A"}: manager override
    reason: str = ""                        # why this intent; goes into the trace
    parsed_by: Literal["llm", "rules", "mock"] = "rules"


# =============================================================================
# 4. Tool outputs (Ayush) - also embedded in the API response
# =============================================================================


class OrderAssignment(BaseModel):
    order_id: str
    plant: str | None                       # None => could not be assigned (capacity)
    production_week: date | None = None     # Monday of the production week
    cost: float                             # landed cost: production + energy + transport + duty
    carbon_kg: float                        # plant energy carbon + transport carbon
    arrival: date | None = None
    due: date
    on_time: bool
    days_late: int = 0
    changed: bool = False                   # True if plant/week differs from the base plan
    evidence_refs: list[str] = Field(default_factory=list)  # e.g. "plants.csv:C:P-12"


class KPIs(BaseModel):
    total_cost: float
    total_carbon_t: float
    late_orders: int
    unassigned_orders: int = 0
    plant_utilisation: dict[str, float]                       # 0..1
    late_by_destination: dict[str, int] = Field(default_factory=dict)  # fairness view


class ValidatorResult(BaseModel):
    """Produced by ai/tools/validator.py, an independent re-check of the plan
    (it does not reuse the solver's constraint code)."""

    capacity_ok: bool
    eligibility_ok: bool
    delivery_ok: bool                       # False if any order is late (late orders are flagged, not hidden)
    violations: list[str] = Field(default_factory=list)


class TradeoffPoint(BaseModel):
    carbon_cut_pct: float
    cost: float
    carbon_t: float
    late_orders: int
    feasible: bool = True
    implied_carbon_price_per_t: float | None = None  # delta cost / delta carbon vs the 0% point


class PlanChange(BaseModel):
    order_id: str
    from_plant: str | None
    to_plant: str | None
    cost_delta: float
    carbon_delta_kg: float
    reason: str                             # built by code from constraint facts, not by the LLM


class CounterfactualResult(BaseModel):
    """'What if order X went to plant Y instead?' Answered by re-solving the WHOLE plan with X forced to Y.
    This is the only honest 'why' in a capacity-constrained plan (see DECISIONS.md: counterfactual explanations)."""

    order_id: str
    chosen_plant: str | None
    alternative_plant: str
    feasible: bool
    blocked_by: Literal["eligibility", "capacity", "delivery"] | None = None
    order_cost_delta: float = 0.0          # local: this order's own cost difference
    plan_cost_delta: float = 0.0           # global: whole plan, after re-solving
    plan_carbon_delta_kg: float = 0.0
    new_late_orders: list[str] = Field(default_factory=list)
    displaced_orders: list[PlanChange] = Field(default_factory=list)  # the knock-on effects


class SolveResult(BaseModel):
    status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "ERROR"]
    assignments: list[OrderAssignment]
    kpis: KPIs | None = None
    solve_s: float
    objective: float | None = None
    weights_used: Weights
    notes: list[str] = Field(default_factory=list)


class Recommendation(BaseModel):
    action: str
    rationale: str
    evidence_refs: list[str]
    confidence: Literal["low", "medium", "high"]
    estimated_impact: str
    requires_approval: bool = True


# =============================================================================
# 5. API responses (Shivang serves these; frontend consumes them)
# =============================================================================


class AnswerType(str, Enum):
    ALLOCATION = "allocation"
    SCENARIO = "scenario"
    BRIEF = "brief"
    INFEASIBLE = "infeasible"
    CLARIFICATION_NEEDED = "clarification_needed"
    REFUSED = "refused"


class AnalyzeResponse(BaseModel):
    run_id: str
    answer_type: AnswerType
    summary: str
    allocation: list[OrderAssignment] = Field(default_factory=list)
    kpis: KPIs | None = None
    tradeoff: list[TradeoffPoint] = Field(default_factory=list)
    changes: list[PlanChange] = Field(default_factory=list)
    weights_used: Weights = Field(default_factory=Weights)
    validator: ValidatorResult | None = None
    recommendations: list[Recommendation] = Field(default_factory=list)
    counterfactuals: list[CounterfactualResult] = Field(default_factory=list)
    brief: str | None = None
    faithfulness_ok: bool | None = None     # result of the number guard on summary/brief
    approval_status: Literal["pending", "not_required"] = "pending"
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class IngestResponse(BaseModel):
    table: TableName
    rows_loaded: int
    rows_rejected: int
    status: Literal["ok", "partial", "error"]
    errors: list[str] = Field(default_factory=list)  # first 20 row-level errors
    data_version: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    api: str = "ok"
    data_store: str                         # "ok" | "empty" | "error: ..."
    llm: str                                # "ok:<provider>/<model>" | "mock" | "error: ..."
    tables: dict[str, int] = Field(default_factory=dict)  # row counts
    validation_passed: bool | None = None   # all backend.validate_data checks passed; None = not run


class TraceStep(BaseModel):
    agent: str                              # router | optimiser | checker | scenario | writer | guard
    tool: str                               # e.g. solve_allocation, validate_plan, write_brief
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    check: str | None = None                # e.g. "faithfulness: pass (14/14 numbers matched)"
    duration_ms: float | None = None
    ts: datetime = Field(default_factory=utcnow)


class TraceRecord(BaseModel):
    run_id: str
    request: str
    answer_type: AnswerType | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    llm_calls: int = 0
    tokens: int = 0
    latency_ms: float | None = None
    data_version: str | None = None
    approval_status: str = "pending"
    created_at: datetime = Field(default_factory=utcnow)
