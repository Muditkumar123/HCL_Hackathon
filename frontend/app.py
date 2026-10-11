"""Streamlit UI. Talks to the backend only over HTTP (httpx); every number on screen comes from the API.

    API_URL=http://localhost:8000 streamlit run frontend/app.py
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, so `streamlit run frontend/app.py` works

from shared.models import AnalyzeResponse, AnswerType, TraceRecord  # noqa: E402

log = logging.getLogger("frontend")

API_URL = os.getenv("API_URL", "http://localhost:8000").rstrip("/")
TIMEOUT = httpx.Timeout(120.0, connect=5.0)   # the real LLM can take a while; the solver is capped at 10 s
TABLES = ["orders", "plants", "transport_duties", "capacity_events"]
MAX_HISTORY = 20
DEFAULT_COST_PCT = 70       # shared.models.Weights defaults (0.7 / 0.3, lateness 1.0)
DEFAULT_LATENESS = 1.0

# One example per capability. These are requests only; the answers always come from the API.
EXAMPLES = [
    ("Allocate", "Allocate this week's orders"),
    ("Cheapest on time", "Give me the cheapest plan that is on time"),
    ("Carbon trade-off", "What does a 10% carbon cut cost?"),
    ("Re-plan", "Plant B loses 30% capacity. Re-plan."),
    ("Explain an order", "Why was order 77 built in Plant C?"),
    ("Leadership brief", "Write the brief for the leadership team."),
]
BADGE_COLORS = {
    AnswerType.ALLOCATION: "green", AnswerType.SCENARIO: "blue", AnswerType.BRIEF: "violet",
    AnswerType.INFEASIBLE: "orange", AnswerType.CLARIFICATION_NEEDED: "gray", AnswerType.REFUSED: "red",
}
LATE_ROW_STYLE = "background-color: rgba(220, 38, 38, 0.22)"


# ============================================================ API (httpx only)
def api(method: str, path: str, **kwargs: Any) -> tuple[Any | None, str | None]:
    """Returns (json body, error message). Never raises, so one failed call cannot crash the page."""
    try:
        r = httpx.request(method, f"{API_URL}{path}", timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as exc:
        log.warning("%s %s failed: %s", method, path, exc)
        return None, f"API not reachable at {API_URL} ({type(exc).__name__})."
    try:
        body = r.json()
    except ValueError:
        body = None
    if r.status_code >= 400:
        detail = body.get("detail", "") if isinstance(body, dict) else r.text[:300]
        errors = body.get("errors", []) if isinstance(body, dict) else []
        log.warning("%s %s -> %s: %s", method, path, r.status_code, detail)
        return body, f"API error {r.status_code}: {detail or 'see errors below'}" + (
            "\n\n" + "\n".join(f"- {e}" for e in errors[:20]) if errors else "")
    return body, None


@st.cache_data(ttl=30, show_spinner=False)
def get_health() -> tuple[dict | None, str | None]:
    return api("GET", "/health")


@st.cache_data(ttl=30, show_spinner=False)
def get_validation() -> tuple[list | None, str | None]:
    return api("GET", "/validation")


def build_payload(request: str, cost_pct: int, lateness: float, capacity_change: dict | None = None) -> dict:
    """AnalyzeRequest body. Weights are sent only when they differ from the defaults: explicit API weights beat the
    request text (D-005), so untouched sliders must not override "cheapest" or "greenest" in the request."""
    payload: dict[str, Any] = {"request": request}
    if cost_pct != DEFAULT_COST_PCT or lateness != DEFAULT_LATENESS:
        payload["weights"] = {"cost": cost_pct / 100, "carbon": 1 - cost_pct / 100, "lateness": lateness}
    if capacity_change:
        payload["scenario"] = {"capacity_changes": [capacity_change]}
    return payload


# ============================================================ pure view helpers (testable without Streamlit)
def tradeoff_figure(resp: AnalyzeResponse) -> go.Figure | None:
    """x = carbon t, y = cost, label = late orders; the chosen plan is highlighted."""
    if not resp.tradeoff:
        return None
    pts = sorted(resp.tradeoff, key=lambda p: p.carbon_cut_pct)
    fig = go.Figure()
    for feasible, name, symbol in ((True, "Feasible", "circle"), (False, "Not feasible", "x-open")):
        sel = [p for p in pts if p.feasible == feasible]
        if not sel:
            continue
        fig.add_trace(go.Scatter(
            x=[p.carbon_t for p in sel], y=[p.cost for p in sel], name=name,
            mode="lines+markers+text" if feasible else "markers+text", marker={"size": 11, "symbol": symbol},
            text=[f"{p.late_orders} late" for p in sel], textposition="top center",
            customdata=[[f"{p.carbon_cut_pct:g}%", "n/a" if p.implied_carbon_price_per_t is None
                         else f"{p.implied_carbon_price_per_t:,.0f}"] for p in sel],
            hovertemplate="Carbon cut %{customdata[0]}<br>Carbon %{x:,.1f} t<br>Cost %{y:,.0f}"
                          "<br>Implied carbon price per t: %{customdata[1]}<extra></extra>",
        ))
    if resp.kpis:
        fig.add_trace(go.Scatter(
            x=[resp.kpis.total_carbon_t], y=[resp.kpis.total_cost], name="Chosen plan", mode="markers",
            marker={"size": 20, "symbol": "star", "color": "#dc2626", "line": {"width": 1, "color": "white"}},
            hovertemplate="Chosen plan<br>Carbon %{x:,.1f} t<br>Cost %{y:,.0f}<extra></extra>",
        ))
    fig.update_layout(xaxis_title="Carbon (t)", yaxis_title="Total cost", height=380,
                      margin={"l": 10, "r": 10, "t": 30, "b": 10}, legend={"orientation": "h", "y": -0.2})
    return fig


def allocation_frame(resp: AnalyzeResponse) -> pd.DataFrame:
    return pd.DataFrame([{
        "order": a.order_id, "plant": a.plant or "unassigned", "week": a.production_week, "cost": a.cost,
        "carbon kg": a.carbon_kg, "arrival": a.arrival, "due": a.due, "on time": a.on_time, "days late": a.days_late,
        "changed": a.changed,
    } for a in resp.allocation])


def style_late_rows(df: pd.DataFrame) -> Any:
    return df.style.apply(lambda row: [LATE_ROW_STYLE if not row["on time"] else ""] * len(row), axis=1).format(
        {"cost": "{:,.0f}", "carbon kg": "{:,.1f}"})


def changes_frame(resp: AnalyzeResponse) -> pd.DataFrame:
    return pd.DataFrame([{"order": c.order_id, "from": c.from_plant or "unassigned", "to": c.to_plant or "unassigned",
                          "cost change": c.cost_delta, "carbon change kg": c.carbon_delta_kg, "reason": c.reason}
                         for c in resp.changes])


def counterfactual_frame(resp: AnalyzeResponse) -> pd.DataFrame:
    return pd.DataFrame([{
        "alternative plant": c.alternative_plant, "possible": c.feasible, "blocked by": c.blocked_by or "",
        "order cost change": c.order_cost_delta, "plan cost change": c.plan_cost_delta,
        "plan carbon change kg": c.plan_carbon_delta_kg, "new late orders": ", ".join(c.new_late_orders),
        "orders displaced": len(c.displaced_orders),
    } for c in resp.counterfactuals])


def trace_frame(trace: TraceRecord) -> pd.DataFrame:
    return pd.DataFrame([{"agent": s.agent, "tool": s.tool, "check": s.check or "", "ms": s.duration_ms}
                         for s in trace.steps])


def _md(text: str) -> str:
    return text.replace("$", r"\$")   # Streamlit renders $...$ as LaTeX


# ============================================================ sidebar
def sidebar() -> dict[str, Any]:
    with st.sidebar:
        st.header("System")
        health, err = get_health()
        if err or health is None:
            st.markdown("🔴 **API unreachable**")
            st.caption(err)
        else:
            light = "🟢" if health.get("status") == "ok" else "🟠"
            st.markdown(f"{light} **API {health.get('status')}**")
            st.caption(f"Data: {health.get('data_store')} · LLM: {health.get('llm')} · "
                       f"Validation: {'passed' if health.get('validation_passed') else 'not passed'}")
            if health.get("tables"):
                st.caption(" · ".join(f"{t} {n}" for t, n in health["tables"].items()))
        if st.button("Refresh status", width="stretch"):
            get_health.clear()
            get_validation.clear()
            st.rerun()

        st.header("Objective weights")
        cost_pct = st.slider("Cost weight (%)", 0, 100, DEFAULT_COST_PCT, step=5,
                             help="Carbon weight is the rest. Default 70/30: cost first, carbon second.")
        st.caption(f"Carbon weight: {100 - cost_pct}%")
        lateness = st.number_input("Lateness weight", 0.0, 10.0, DEFAULT_LATENESS, step=0.5,
                                   help="Penalty per late order-day. 1.0 makes on-time delivery dominate.")
        if cost_pct == DEFAULT_COST_PCT and lateness == DEFAULT_LATENESS:
            st.caption("Defaults are not sent, so words like \"cheapest\" or \"greenest\" in your request apply.")
        else:
            st.caption("These weights are sent and take priority over wording in your request.")

        st.header("Capacity loss")
        with st.form("capacity_form"):
            known = sorted(st.session_state.get("plants", []))
            plant = st.selectbox("Plant", known) if known else st.text_input("Plant", max_chars=10)
            loss = st.slider("Capacity lost (%)", 1, 100, 30)
            dated = st.checkbox("Only between dates")
            d_from = st.date_input("From", value=date.today(), disabled=not dated)
            d_to = st.date_input("To", value=date.today(), disabled=not dated)
            if st.form_submit_button("Re-plan with this loss", width="stretch"):
                if not plant:
                    st.warning("Enter a plant.")
                elif dated and d_to < d_from:
                    st.warning("'To' must be on or after 'From'.")
                else:
                    change = {"plant": plant, "capacity_loss_pct": loss}
                    text = f"Plant {plant} loses {loss}% capacity"
                    if dated:
                        change |= {"from_date": d_from.isoformat(), "to_date": d_to.isoformat()}
                        text += f" from {d_from.isoformat()} to {d_to.isoformat()}"
                    st.session_state.pending = (text + ". Re-plan.", change)

        st.header("Load data")
        with st.form("ingest_form", clear_on_submit=True):
            table = st.selectbox("Table", TABLES)
            mode = st.radio("Mode", ["replace", "append"], horizontal=True)
            upload = st.file_uploader("CSV file", type=["csv"])
            if st.form_submit_button("Upload", width="stretch") and upload is not None:
                ingest(upload, table, mode)

        st.header("Data validation")
        rows, verr = get_validation()
        if verr:
            st.caption(verr)
        elif rows:
            df = pd.DataFrame(rows)
            if "passed" in df:
                df["passed"] = df["passed"].map({True: "✅", False: "❌"})
            st.dataframe(df, hide_index=True, width="stretch")
    return {"cost_pct": cost_pct, "lateness": lateness}


def ingest(upload: Any, table: str, mode: str) -> None:
    body, err = api("POST", "/ingest", files={"file": (upload.name, upload.getvalue(), "text/csv")},
                    data={"meta": json.dumps({"table": table, "mode": mode})})
    if body and isinstance(body, dict) and "rows_loaded" in body:
        icon = {"ok": "✅", "partial": "⚠️"}.get(body.get("status"), "❌")
        st.toast(f"{icon} {table}: {body['rows_loaded']} rows loaded, {body.get('rows_rejected', 0)} rejected",
                 icon=None)
        for e in body.get("errors", [])[:5]:
            st.caption(e)
    elif err:
        st.error(err)
    get_health.clear()
    get_validation.clear()


# ============================================================ main panel
def ask(request: str, settings: dict, capacity_change: dict | None = None) -> None:
    payload = build_payload(request, settings["cost_pct"], settings["lateness"], capacity_change)
    with st.spinner("Planning..."):
        body, err = api("POST", "/analyze", json=payload)
    if err or body is None:
        st.session_state.history.append({"request": request, "error": err or "Empty response from the API."})
    else:
        resp = AnalyzeResponse.model_validate(body)
        if resp.kpis and resp.kpis.plant_utilisation:
            st.session_state.plants = list(resp.kpis.plant_utilisation)
        st.session_state.history.append({"request": request, "response": body})
    st.session_state.history = st.session_state.history[-MAX_HISTORY:]


def render_header(resp: AnalyzeResponse) -> None:
    cols = st.columns([2, 2, 3])
    with cols[0]:
        st.badge(resp.answer_type.value.replace("_", " "), color=BADGE_COLORS.get(resp.answer_type, "gray"))
    with cols[1]:
        if resp.faithfulness_ok is not None:
            st.badge("numbers checked" if resp.faithfulness_ok else "number check failed",
                     color="green" if resp.faithfulness_ok else "red")
    cols[2].caption(f"run `{resp.run_id}` · approval: {resp.approval_status}")


def render_kpis(resp: AnalyzeResponse) -> None:
    k = resp.kpis
    if k is None:
        return
    util = k.plant_utilisation
    avg = sum(util.values()) / len(util) if util else 0.0
    c = st.columns(4)
    c[0].metric("Total cost", f"{k.total_cost:,.0f}")
    c[1].metric("Carbon", f"{k.total_carbon_t:,.1f} t")
    c[2].metric("Late orders", k.late_orders, help=f"Unassigned: {k.unassigned_orders}")
    c[3].metric("Avg utilisation", f"{avg:.0%}", help=" · ".join(f"{p}: {u:.0%}" for p, u in util.items()))


def render_validator(resp: AnalyzeResponse) -> None:
    v = resp.validator
    if v is None:
        return
    marks = (f"capacity {'✅' if v.capacity_ok else '❌'} · eligibility {'✅' if v.eligibility_ok else '❌'} · "
             f"delivery {'✅' if v.delivery_ok else '⚠️ late orders'}")
    if v.capacity_ok and v.eligibility_ok:
        st.success(f"Validator passed: {marks}")
    else:
        st.error(f"Validator FAILED, this is not a valid plan: {marks}")
        for x in v.violations[:10]:
            st.caption(f"• {x}")


def render_trace(run_id: str) -> None:
    with st.expander("Show trace"):
        traces = st.session_state.traces
        if run_id not in traces and st.button("Load trace", key=f"trace_{run_id}"):
            body, err = api("GET", f"/trace/{run_id}")
            traces[run_id] = err or TraceRecord.model_validate(body)
        t = traces.get(run_id)
        if isinstance(t, str):
            st.error(t)
        elif t is not None:
            st.caption(f"{len(t.steps)} steps · {t.llm_calls} LLM calls · {t.tokens} tokens · {t.latency_ms} ms")
            st.dataframe(trace_frame(t), hide_index=True, width="stretch")
            st.json(t.model_dump(mode="json"), expanded=False)


def render_response(resp: AnalyzeResponse) -> None:
    render_header(resp)
    if resp.brief is None:
        st.markdown(_md(resp.summary))
    render_kpis(resp)
    if resp.brief:
        with st.container(border=True):
            st.subheader("Leadership brief")
            st.markdown(_md(resp.brief))
            st.warning("This is a proposal. It needs management approval before anything changes "
                       f"(status: {resp.approval_status}).")
    fig = tradeoff_figure(resp)
    if fig is not None:
        st.subheader("Cost and carbon trade-off")
        st.plotly_chart(fig, width="stretch", key=f"tradeoff_{resp.run_id}")
    if resp.changes:
        st.subheader(f"What moved ({len(resp.changes)} orders)")
        st.dataframe(changes_frame(resp), hide_index=True, width="stretch")
    if resp.counterfactuals:
        st.subheader("Alternatives tested (whole plan re-solved)")
        st.dataframe(counterfactual_frame(resp), hide_index=True, width="stretch")
    if resp.allocation:
        st.subheader("Allocation")
        st.dataframe(style_late_rows(allocation_frame(resp)), hide_index=True, width="stretch", height=320)
    render_validator(resp)
    for r in resp.recommendations:
        st.info(f"**{r.action}** ({r.confidence} confidence). {r.rationale} {r.estimated_impact}."
                + (" Requires approval." if r.requires_approval else ""))
    with st.expander(f"Assumptions ({len(resp.assumptions)})"):
        for a in resp.assumptions:
            st.markdown(f"- {_md(a)}")
    with st.expander(f"Limitations ({len(resp.limitations)})", expanded=resp.answer_type == AnswerType.INFEASIBLE):
        for x in resp.limitations:
            st.markdown(f"- {_md(x)}")
    render_trace(resp.run_id)


def render_history() -> None:
    history = st.session_state.history
    for i, item in enumerate(history):
        with st.chat_message("user"):
            st.markdown(_md(item["request"]))
        with st.chat_message("assistant"):
            if "error" in item:
                st.error(item["error"])
                continue
            resp = AnalyzeResponse.model_validate(item["response"])
            if i == len(history) - 1:
                render_response(resp)
            else:  # older answers stay compact; the latest one gets the full view
                render_header(resp)
                st.markdown(_md(resp.summary))


def main() -> None:
    st.set_page_config(page_title="Order Allocation", page_icon="🏭", layout="wide")
    for key, default in (("history", []), ("traces", {}), ("plants", []), ("pending", None)):
        st.session_state.setdefault(key, default)

    settings = sidebar()
    st.title("Multi-plant order allocation")
    st.caption("Plans come from the solver and pass the validator. The assistant only reads your request and "
               "explains the result. Every plan needs management approval.")

    cols = st.columns(3)
    for i, (label, text) in enumerate(EXAMPLES):
        if cols[i % 3].button(label, help=text, width="stretch", key=f"example_{i}"):
            st.session_state.pending = (text, None)
    if typed := st.chat_input("Ask about allocation, trade-offs, re-planning, an order, or a brief"):
        st.session_state.pending = (typed, None)

    if st.session_state.pending:
        text, change = st.session_state.pending
        st.session_state.pending = None
        ask(text, settings, change)
    render_history()


if __name__ == "__main__":
    main()
