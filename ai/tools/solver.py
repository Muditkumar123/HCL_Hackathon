"""Allocation solver: a mixed-integer program solved with OR-Tools (pywraplp, SCIP backend).

MIP formulation
---------------
Sets: orders o; options i = (o, p, w) from cost_model.build_options (eligible plant p, production week w).

Variables
    x[o,p,w] ∈ {0,1}   order o is produced at plant p in week w
    u[o]     ∈ {0,1}   order o is deferred: not produced inside the horizon (capacity makes it unavoidable)

Constraints
    assign once   Σ_{p,w} x[o,p,w] + u[o] = 1                          for every order o
    capacity      Σ qty[o]·x[o,p,w] ≤ effective_capacity[p(,product),w]  for every capacity row
                  (shared plant capacity or per (plant, product), see cost_model.ASSUMPTIONS)
    carbon cap    Σ carbon·x + Σ min_carbon[o]·u[o] ≤ carbon_cap_kg   (optional: trade-off frontier; a deferred
                  order still emits when it is made later, so deferring cannot fake a carbon cut)
    on time       x = 0 for late options of orders that have an on-time option (optional)
    forced        Σ_w x[o,p_forced,w] = 1                              (manager override / counterfactual)

Objective (minimise)
    w.cost · Σ cost·x / C_ref  +  w.carbon · Σ carbon·x / K_ref  +  w.lateness · 0.01 · Σ days_late·x
    + Σ defer[o]·u[o]  [+ 0.0001 · (orders moved from base_plan)]
    C_ref = Σ_o min cost[o], K_ref = Σ_o min carbon[o], so both normalised terms are about 1.0 and the
    weights mean what they say. One late order-day costs as much as 1% of total cost (about 1.4x an average
    order's own cost on the 120-order pack), so the plan never makes an order late to save money on it.
    defer[o] = o's worst in-horizon option + one more week of lateness + 0.05: deferring is always worse than
    producing late, but not so much worse that the solver makes dozens of orders late to avoid one deferral
    (a flat big-M did exactly that). The base-plan term (100 moved orders = one late order-day) only breaks
    near-ties in favour of minimal change when re-planning; it never outweighs lateness.

Search strategy (measured on the 120-order pack: full branch-and-bound does not beat the root node in 20 s)
    1. A greedy warm start (forced orders, then base-plan / hinted placements that still fit, then the rest at their
       lowest-objective option that fits) is passed to SCIP as a hint. Large models run a portfolio of warm starts in
       parallel threads (keep base placements / by due date / largest order first) and keep the best plan: a
       re-plan seeded only from the old plan tends to get stuck next to it.
    2. Models with more than FULL_SEARCH_MAX_OPTIONS options stop after the root node: deterministic, about 2 s,
       status FEASIBLE with the gap to SCIP's proven lower bound in `notes`. Smaller models are solved to optimality.
    3. A non-optimal plan is polished by deterministic local search (relocate one order; or move one order into a
       full slot by ejecting another order to its best free option or deferring it), respecting capacity, forced
       orders and the carbon cap. Only strictly improving moves are applied, so the objective never gets worse.

Never raises: any failure becomes status "ERROR" (or "INFEASIBLE") with a note.
Results are memoised on (table contents, arguments): the graph and the trade-off frontier often ask for the
same base plan within one request.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd
from ortools.linear_solver import pywraplp

from ai.tools.cost_model import (
    build_options, capacity_key, capacity_modes, effective_capacity, plan_kpis, to_assignment, unassigned,
)
from shared.models import CapacityChange, Dataset, OrderAssignment, SolveResult, Weights

log = logging.getLogger(__name__)

UNASSIGNED_PENALTY = 1000.0  # only for orders with no eligible option at all (u is forced to 1 anyway)
DEFER_MARGIN = 0.05
LATE_DAY_WEIGHT = 0.01
MOVE_PENALTY = 0.0001  # 100 moved orders weigh as much as one late order-day
WEEK_TIEBREAK = 1e-7  # prefer earlier production weeks among exact ties (deterministic plans)
CACHE_SIZE = 64
MIP_GAP = 0.005
FULL_SEARCH_MAX_OPTIONS = 600  # above this, stop after the root node (see module docstring)
POLISH_BUDGET_S = 1.0
EPS = 1e-9

_cache: OrderedDict[str, SolveResult] = OrderedDict()
_cache_lock = threading.Lock()


def solve_allocation(ds: Dataset, weights: Weights, changes: Sequence[CapacityChange] = (),
                     carbon_cap_kg: float | None = None, require_all_on_time: bool = False,
                     base_plan: list[OrderAssignment] | None = None,
                     forced: dict[str, str] | None = None, time_limit_s: float = 10.0,
                     horizon_weeks: int | None = None, hint: list[OrderAssignment] | None = None,
                     portfolio: bool = True, move_penalty: float = MOVE_PENALTY,
                     max_unassigned: int | None = None) -> SolveResult:
    """Optimal allocation of every order to (plant, week). See the module docstring for the MIP.

    `hint` is a plan whose placements the warm start keeps where they fit (speed only; not memoised).
    `portfolio=False` runs only the first warm start (callers that already solve many models in parallel).
    `move_penalty` is the objective charge per order moved off its base_plan plant (counterfactuals use more).
    `max_unassigned` caps the number of deferred orders (the trade-off uses it, so INFEASIBLE is a real certificate).
    """
    t0 = time.perf_counter()
    try:
        key = None if hint else _cache_key(ds, weights, changes, carbon_cap_kg, require_all_on_time, base_plan,
                                           forced, time_limit_s, horizon_weeks,
                                           (portfolio, move_penalty, max_unassigned))
        with _cache_lock:
            if key in _cache:
                _cache.move_to_end(key)
                log.debug("solve_allocation cache hit")
                return _cache[key].model_copy(deep=True)
        args = (ds, weights, list(changes), carbon_cap_kg, require_all_on_time, base_plan, forced or {},
                time_limit_s, horizon_weeks, hint, t0, move_penalty, max_unassigned)
        if not portfolio or len(build_options(ds, horizon_weeks)) <= FULL_SEARCH_MAX_OPTIONS:
            res = _solve(*args, warm="prefer" if hint or base_plan else "due")
        else:
            res = _portfolio(args, seeded=bool(hint or base_plan))
        if key is not None and res.status != "ERROR":
            with _cache_lock:
                _cache[key] = res.model_copy(deep=True)
                while len(_cache) > CACHE_SIZE:
                    _cache.popitem(last=False)
        return res
    except Exception as exc:  # never raise to the graph; the note explains what happened
        log.exception("solve_allocation failed")
        return SolveResult(status="ERROR", assignments=[], solve_s=round(time.perf_counter() - t0, 3),
                           weights_used=weights, notes=[f"Solver error: {type(exc).__name__}: {exc}"])


def _portfolio(args: tuple, seeded: bool) -> SolveResult:
    """Solve with several warm starts in parallel threads (SCIP releases the GIL) and keep the lowest objective.

    With a base plan, two more runs ignore it entirely ('unseeded-*'); their objective is re-scored with the move
    penalty, so a re-plan is never worse than a fresh plan on the re-plan objective. Deterministic: each run is,
    and ties go to the first strategy.
    """
    base_plan = args[5]
    strategies = ((["prefer", "due", "size"] + (["unseeded-due", "unseeded-size"] if base_plan else []))
                  if seeded else ["due", "size"])

    def run(st: str) -> SolveResult:
        if not st.startswith("unseeded"):
            return _solve(*args, warm=st)
        res = _solve(*args[:5], None, *args[6:9], None, *args[10:], warm=st.split("-")[1])
        base_plant = {a.order_id: a.plant for a in base_plan or [] if a.plant is not None}
        base_week = {a.order_id: a.production_week for a in base_plan or []}
        moved = 0
        for a in res.assignments:
            a.changed = base_plant.get(a.order_id) != a.plant or base_week.get(a.order_id) != a.production_week
            moved += a.order_id in base_plant and a.plant is not None and a.plant != base_plant[a.order_id]
        if res.objective is not None:
            res.objective = round(res.objective + args[11] * moved, 6)
        return res

    with ThreadPoolExecutor(max_workers=len(strategies)) as pool:
        results = list(pool.map(run, strategies))
    ok = [(r.objective, k, r) for k, r in enumerate(results) if r.status in ("OPTIMAL", "FEASIBLE")]
    if not ok:
        return results[0]
    _, k, best = min(ok, key=lambda t: (t[0], t[1]))
    best.notes.append(f"Best of {len(strategies)} warm starts ({', '.join(strategies)}): '{strategies[k]}'.")
    log.debug("portfolio objectives %s", {s: r.objective for s, r in zip(strategies, results)})
    return best


def _fingerprint(ds: Dataset) -> str:
    """Hash of the table contents (not data_version, which tests and callers may reuse after edits)."""
    h = hashlib.sha1(str(ds.planning_start).encode())
    for df in (ds.orders, ds.plants, ds.transport_duties, ds.capacity_events):
        if df is None:
            continue
        h.update(",".join(map(str, df.columns)).encode())
        h.update(pd.util.hash_pandas_object(df.astype(str), index=False).to_numpy().tobytes())
    return h.hexdigest()


def _cache_key(ds: Dataset, weights: Weights, changes: Sequence[CapacityChange], carbon_cap_kg: float | None,
               require_all_on_time: bool, base_plan: list[OrderAssignment] | None, forced: dict[str, str] | None,
               time_limit_s: float, horizon_weeks: int | None, variant: tuple) -> str:
    parts = [_fingerprint(ds), weights.model_dump_json(), [c.model_dump_json() for c in changes], carbon_cap_kg, variant,
             require_all_on_time, sorted((forced or {}).items()), time_limit_s, horizon_weeks,
             [(a.order_id, a.plant, str(a.production_week)) for a in base_plan] if base_plan else None]
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _coefficients(opts: pd.DataFrame, weights: Weights, base_plan: list[OrderAssignment] | None = None,
                  move_penalty: float = MOVE_PENALTY) -> np.ndarray:
    """Objective coefficient of each option row (see the module docstring)."""
    if opts.empty:
        return np.zeros(0)
    c_ref = float(opts.groupby("order_id")["landed_cost"].min().sum()) or 1.0
    k_ref = float(opts.groupby("order_id")["carbon_kg"].min().sum()) or 1.0
    week_idx = {w: k for k, w in enumerate(sorted(set(opts["week_start"])))}
    coef = (weights.cost * opts["landed_cost"].to_numpy(dtype=float) / c_ref
            + weights.carbon * opts["carbon_kg"].to_numpy(dtype=float) / k_ref
            + weights.lateness * LATE_DAY_WEIGHT * opts["days_late"].to_numpy(dtype=float)
            + WEEK_TIEBREAK * opts["week_start"].map(week_idx).to_numpy(dtype=float))
    if base_plan:
        base_plant = {a.order_id: a.plant for a in base_plan if a.plant is not None}
        moved = [oid in base_plant and plant != base_plant[oid] for oid, plant in zip(opts["order_id"], opts["plant"])]
        coef = coef + move_penalty * np.asarray(moved, dtype=float)
    return coef


def _greedy_start(opts: pd.DataFrame, coef: np.ndarray, key_of: list[tuple], limits: dict[tuple, float],
                  qty: np.ndarray, order_ids: list[str], due: dict, forced: dict[str, str],
                  prefer: dict[str, tuple], order_by: str = "due") -> set[int]:
    """Option rows of a capacity-feasible starting plan (ignores the carbon cap). Order: forced orders, then
    preferred placements that still fit, then the rest by due date (largest first) at their lowest-objective
    option that fits. Orders that fit nowhere stay unassigned."""
    left = dict(limits)
    rows: dict[str, list[int]] = defaultdict(list)
    for i, oid in enumerate(opts["order_id"]):
        rows[oid].append(i)
    plant, week = opts["plant"].to_numpy(), opts["week_start"].to_numpy()
    chosen: set[int] = set()
    done: set[str] = set()

    def take(oid: str, cands: list[int]) -> None:
        for i in cands:
            if left.get(key_of[i], 0.0) >= qty[i] - 1e-9:
                left[key_of[i]] -= qty[i]
                chosen.add(i)
                done.add(oid)
                return

    for oid, p in forced.items():
        cands = sorted((i for i in rows[oid] if plant[i] == p), key=lambda i: (prefer.get(oid) != (p, week[i]), coef[i]))
        take(oid, cands)
    for oid, (p, w) in prefer.items():
        if oid not in done and oid not in forced:
            take(oid, [i for i in rows[oid] if plant[i] == p and week[i] == w])
    size = {o: float(qty[rows[o][0]]) if rows[o] else 0.0 for o in order_ids}
    key = ((lambda o: (-size[o], str(due[o]), o)) if order_by == "size"
           else (lambda o: (str(due[o]), -size[o], o)))
    for oid in sorted((o for o in order_ids if o not in done and o not in forced), key=key):
        take(oid, sorted(rows[oid], key=lambda i: coef[i]))
    return chosen


def _polish(opts: pd.DataFrame, coef: np.ndarray, key_of: list[tuple], limits: dict[tuple, float],
            order_ids: list[str], chosen: dict[str, int], defer: dict[str, float], forced: dict[str, str],
            carbon_cap_kg: float | None, budget_s: float) -> dict[str, int]:
    """Deterministic local search on a feasible plan (see module docstring, step 3). Returns {order: option row}."""
    t_end = time.perf_counter() + budget_s
    qty = opts["qty"].to_numpy(dtype=float)
    carbon = opts["carbon_kg"].to_numpy(dtype=float)
    plant = opts["plant"].to_numpy()
    rid = {k: n for n, k in enumerate(limits)}
    row = np.array([rid[k] for k in key_of], dtype=int)
    cap = np.array([limits[k] for k in limits], dtype=float)
    options: dict[str, list[int]] = defaultdict(list)
    for i, o in enumerate(opts["order_id"]):
        options[o].append(i)
    min_c = {o: float(min(carbon[i] for i in options[o])) if options[o] else 0.0 for o in order_ids}
    for o in options:
        if o in forced:
            options[o] = [i for i in options[o] if plant[i] == forced[o]]
        options[o].sort(key=lambda i: (coef[i], i))

    assign = {o: chosen.get(o, -1) for o in order_ids}
    load = np.zeros(len(cap))
    members: dict[int, set[str]] = defaultdict(set)
    for o, i in assign.items():
        if i >= 0:
            load[row[i]] += qty[i]
            members[row[i]].add(o)
    total_c = sum(carbon[i] if i >= 0 else min_c[o] for o, i in assign.items())

    def val(o: str, i: int) -> float:
        return float(coef[i]) if i >= 0 else defer[o]

    def car(o: str, i: int) -> float:
        return float(carbon[i]) if i >= 0 else min_c[o]

    def carbon_ok(delta: float) -> bool:
        return carbon_cap_kg is None or total_c + delta <= carbon_cap_kg + 1e-6

    def move(o: str, i: int) -> None:
        nonlocal total_c
        cur = assign[o]
        if cur >= 0:
            load[row[cur]] -= qty[cur]
            members[row[cur]].discard(o)
        if i >= 0:
            load[row[i]] += qty[i]
            members[row[i]].add(o)
        total_c += car(o, i) - car(o, cur)
        assign[o] = i

    def best_eject(a: str, j: int, gain_a: float, dc_a: float) -> tuple[float, str, int] | None:
        """Cheapest way to free row[j] for order a by moving one order b out of it."""
        k, cur = row[j], assign[a]
        need = load[k] + qty[j] - cap[k]
        best = None
        for b in sorted(members[k]):
            ib = assign[b]
            if qty[ib] + 1e-9 < need:
                continue
            for jb in options[b]:
                if gain_a + coef[jb] - coef[ib] >= -EPS:
                    break
                rb = row[jb]
                if rb == k:
                    continue
                freed = qty[cur] if cur >= 0 and row[cur] == rb else 0.0
                if load[rb] - freed + qty[jb] <= cap[rb] + 1e-9 and carbon_ok(dc_a + carbon[jb] - carbon[ib]):
                    cand = (gain_a + float(coef[jb] - coef[ib]), b, int(jb))
                    best = cand if best is None or cand < best else best
                    break
            if b not in forced:
                d = gain_a + defer[b] - float(coef[ib])
                if d < -EPS and carbon_ok(dc_a + min_c[b] - carbon[ib]) and (best is None or d < best[0]):
                    best = (d, b, -1)
        return best

    improved, passes = True, 0
    while improved and time.perf_counter() < t_end:
        improved, passes = False, passes + 1
        for a in order_ids:
            cur = assign[a]
            cv = val(a, cur)
            for j in options[a]:
                if coef[j] >= cv - EPS:
                    break
                if j == cur:
                    continue
                dc = car(a, j) - car(a, cur)
                same_row = cur >= 0 and row[cur] == row[j]
                if load[row[j]] - (qty[cur] if same_row else 0.0) + qty[j] <= cap[row[j]] + 1e-9 and carbon_ok(dc):
                    move(a, j)
                    improved = True
                    break
                if same_row:
                    continue
                ej = best_eject(a, j, float(coef[j]) - cv, dc)
                if ej is not None:
                    _, b, jb = ej
                    move(a, -1)
                    move(b, jb)
                    move(a, j)
                    improved = True
                    break
    log.debug("polish passes=%d", passes)
    return {o: i for o, i in assign.items() if i >= 0}


def _defer_penalties(opts: pd.DataFrame, coef: np.ndarray, order_ids: list[str], weights: Weights) -> dict[str, float]:
    """defer[o]: worst in-horizon option + one more week late + DEFER_MARGIN (see module docstring)."""
    worst = pd.Series(coef, index=opts["order_id"]).groupby(level=0).max().to_dict() if len(opts) else {}
    extra = weights.lateness * LATE_DAY_WEIGHT * 7 + DEFER_MARGIN
    return {o: worst[o] + extra if o in worst else UNASSIGNED_PENALTY for o in order_ids}


def plan_objective(ds: Dataset, plan: Sequence[OrderAssignment], weights: Weights,
                   horizon_weeks: int | None = None) -> float:
    """The solver's objective value of any plan under `weights` (lets callers compare plans from different solves)."""
    opts = build_options(ds, horizon_weeks)
    coef = _coefficients(opts, weights)
    defer = _defer_penalties(opts, coef, [a.order_id for a in plan], weights)
    row = {(o, p, w): i for i, (o, p, w) in enumerate(zip(opts["order_id"], opts["plant"], opts["week_start"]))}
    total = 0.0
    for a in plan:
        i = row.get((a.order_id, a.plant, a.production_week))
        total += defer[a.order_id] if i is None else float(coef[i])
    return total


def _infeasible(weights: Weights, t0: float, notes: list[str]) -> SolveResult:
    return SolveResult(status="INFEASIBLE", assignments=[], solve_s=round(time.perf_counter() - t0, 3),
                       weights_used=weights, notes=notes)


def _solve(ds: Dataset, weights: Weights, changes: list[CapacityChange], carbon_cap_kg: float | None,
           require_all_on_time: bool, base_plan: list[OrderAssignment] | None, forced: dict[str, str],
           time_limit_s: float, horizon_weeks: int | None, hint: list[OrderAssignment] | None,
           t0: float, move_penalty: float = MOVE_PENALTY, max_unassigned: int | None = None,
           warm: str = "prefer") -> SolveResult:
    orders = ds.orders.assign(order_id=ds.orders["order_id"].astype(str))
    order_ids = list(orders["order_id"])
    due = dict(zip(order_ids, orders["due"]))
    opts = build_options(ds, horizon_weeks)
    notes: list[str] = []

    # ---- forced assignments: check eligibility up front so the note is precise
    for oid, plant in forced.items():
        if oid not in due:
            return _infeasible(weights, t0, [f"Forced order {oid} is not in the orders table."])
        if not ((opts["order_id"] == oid) & (opts["plant"] == plant)).any():
            return _infeasible(weights, t0, [f"{oid} cannot be made at or shipped from plant {plant} (eligibility)."])

    # ---- on-time requirement: drop late options of orders that have an on-time option
    if require_all_on_time and not opts.empty:
        has_on_time = opts.groupby("order_id")["on_time"].transform("any")
        dropped = opts[has_on_time & ~opts["on_time"]]
        opts = opts[~has_on_time | opts["on_time"]]
        no_on_time = sorted(set(opts.loc[~opts["on_time"], "order_id"]))
        if no_on_time:
            notes.append(f"{len(no_on_time)} orders have no on-time option at any plant: {', '.join(no_on_time[:10])}.")
        log.debug("require_all_on_time dropped %d late options", len(dropped))
    opts = opts.reset_index(drop=True)

    no_option = [o for o in order_ids if o not in set(opts["order_id"])]
    if no_option:
        notes.append(f"{len(no_option)} orders have no eligible plant: {', '.join(no_option[:10])}.")

    solver = pywraplp.Solver.CreateSolver("SCIP")
    if solver is None:
        raise RuntimeError("OR-Tools SCIP backend is not available")
    solver.SetTimeLimit(int(time_limit_s * 1000))

    x = [solver.BoolVar(f"x{i}") for i in range(len(opts))]
    u = {o: solver.BoolVar(f"u_{o}") for o in order_ids}
    by_order: dict[str, list[int]] = defaultdict(list)
    for i, oid in enumerate(opts["order_id"]):
        by_order[oid].append(i)

    for o in order_ids:
        solver.Add(sum(x[i] for i in by_order[o]) + u[o] == 1)

    # ---- capacity
    modes = capacity_modes(ds)
    cap = effective_capacity(ds, changes, horizon_weeks)
    cap_rows = {(r.plant, r.product, r.week_start): r for r in cap.itertuples(index=False)}
    key_of = [capacity_key(r.plant, r.product, r.week_start, modes) for r in opts.itertuples(index=False)]
    by_key: dict[tuple, list[int]] = defaultdict(list)
    for i, key in enumerate(key_of):
        by_key[key].append(i)
    qty = opts["qty"].to_numpy(dtype=float)
    limits = {key: float(cap_rows[key].capacity) if key in cap_rows else 0.0 for key in by_key}
    for key, idx in by_key.items():
        solver.Add(sum(qty[i] * x[i] for i in idx) <= limits[key])

    # ---- optional constraints
    carbon = opts["carbon_kg"].to_numpy(dtype=float)
    if carbon_cap_kg is not None:
        min_carbon = opts.groupby("order_id")["carbon_kg"].min().to_dict() if len(opts) else {}
        solver.Add(sum(carbon[i] * x[i] for i in range(len(opts)))
                   + sum(min_carbon.get(o, 0.0) * u[o] for o in order_ids) <= carbon_cap_kg)
    for oid, plant in forced.items():
        solver.Add(sum(x[i] for i in by_order[oid] if opts.at[i, "plant"] == plant) == 1)
    if max_unassigned is not None:
        solver.Add(sum(u.values()) <= max_unassigned)

    # ---- objective
    coef = _coefficients(opts, weights, base_plan, move_penalty)
    base_plant = {a.order_id: a.plant for a in (base_plan or []) if a.plant is not None}
    objective = solver.Objective()
    for i, v in enumerate(x):
        objective.SetCoefficient(v, float(coef[i]))
    for o, pen in _defer_penalties(opts, coef, order_ids, weights).items():
        objective.SetCoefficient(u[o], float(pen))
    objective.SetMinimization()

    # ---- warm start + search
    prefer = ({a.order_id: (a.plant, a.production_week) for a in (hint or base_plan or []) if a.plant is not None}
              if warm == "prefer" else {})
    start = _greedy_start(opts, coef, key_of, limits, qty, order_ids, due, forced, prefer,
                          order_by="size" if warm == "size" else "due")
    started = {opts.at[i, "order_id"] for i in start}
    solver.SetHint(x + list(u.values()),
                   [1.0 if i in start else 0.0 for i in range(len(x))] + [0.0 if o in started else 1.0 for o in u])
    root_only = len(opts) > FULL_SEARCH_MAX_OPTIONS
    if root_only:
        solver.SetSolverSpecificParametersAsString("limits/totalnodes = 1")
    params = pywraplp.MPSolverParameters()
    params.SetDoubleParam(pywraplp.MPSolverParameters.RELATIVE_MIP_GAP, MIP_GAP)
    code = solver.Solve(params)
    if root_only and code not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE, pywraplp.Solver.INFEASIBLE):
        log.info("no plan after the root node; continuing the search under the time limit")
        solver.SetSolverSpecificParametersAsString("limits/totalnodes = -1")
        solver.SetTimeLimit(int(max(1.0, time_limit_s - (time.perf_counter() - t0)) * 1000))
        root_only = False
        code = solver.Solve(params)
    status = {pywraplp.Solver.OPTIMAL: "OPTIMAL", pywraplp.Solver.FEASIBLE: "FEASIBLE",
              pywraplp.Solver.INFEASIBLE: "INFEASIBLE"}.get(code, "ERROR")
    if status in ("INFEASIBLE", "ERROR"):
        why = []
        if carbon_cap_kg is not None:
            why.append(f"carbon cap {carbon_cap_kg:,.0f} kg")
        if forced:
            why.append("forced assignments " + ", ".join(f"{o}->{p}" for o, p in forced.items()))
        notes.append("No plan satisfies all constraints" + (f" ({'; '.join(why)})" if why else "") + "."
                     if status == "INFEASIBLE" else f"Solver returned status code {code}.")
        return SolveResult(status=status, assignments=[], solve_s=round(time.perf_counter() - t0, 3),
                           weights_used=weights, notes=notes)

    # ---- read the solution, polish it if not proven optimal
    defer = _defer_penalties(opts, coef, order_ids, weights)
    chosen = {opts.at[i, "order_id"]: i for i, v in enumerate(x) if v.solution_value() > 0.5}
    value = solver.Objective().Value()
    if status == "FEASIBLE":
        budget = min(POLISH_BUDGET_S, max(0.2, time_limit_s - (time.perf_counter() - t0)))
        chosen = _polish(opts, coef, key_of, limits, order_ids, chosen, defer, forced, carbon_cap_kg, budget)
        polished = sum(float(coef[i]) for i in chosen.values()) + sum(defer[o] for o in order_ids if o not in chosen)
        bound = solver.Objective().BestBound()
        gap = (polished - bound) / abs(polished) if polished else 0.0
        why = ("Large model: search stopped after the root node" if root_only
               else f"Time limit of {time_limit_s:g} s reached")
        notes.append(f"{why} and the plan was polished by local search (objective {value:.4f} -> {polished:.4f}); "
                     f"it is within {gap:.1%} of the solver's proven lower bound.")
        value = polished
    cap_events = {k: r.events for k, r in cap_rows.items()}
    base_week = {a.order_id: a.production_week for a in (base_plan or [])}
    assignments: list[OrderAssignment] = []
    for o in order_ids:
        if o in chosen:
            r = opts.loc[chosen[o]]
            key = capacity_key(r.plant, r.product, r.week_start, modes)
            changed = bool(base_plan) and (base_plant.get(o) != r.plant or base_week.get(o) != r.week_start)
            assignments.append(to_assignment(r, changed=changed, extra_refs=cap_events.get(key, [])))
        else:
            assignments.append(unassigned(o, due[o], changed=bool(base_plan) and base_plant.get(o) is not None))
    left = [a.order_id for a in assignments if a.plant is None]
    if left:
        notes.append(f"{len(left)} orders cannot be produced within capacity: {', '.join(left[:10])}.")

    kpis = plan_kpis(ds, assignments, changes, horizon_weeks)
    elapsed = round(time.perf_counter() - t0, 3)
    log.info("solve_allocation status=%s orders=%d options=%d cost=%.0f carbon_t=%.2f late=%d unassigned=%d %.2fs",
             status, len(order_ids), len(opts), kpis.total_cost, kpis.total_carbon_t, kpis.late_orders,
             kpis.unassigned_orders, elapsed)
    return SolveResult(status=status, assignments=assignments, kpis=kpis, solve_s=elapsed,
                       objective=round(value, 6), weights_used=weights, notes=notes)

