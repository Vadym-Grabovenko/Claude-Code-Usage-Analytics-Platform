"""Interactive AI-assistant usage analytics dashboard (Streamlit + Plotly).

Covers Claude Code, ChatGPT / OpenAI, Gemini and DeepSeek through one canonical
event table. Organized around the personas who consume this data:
  • Leadership   → overview KPIs, cost trends, provider share & adoption
  • FinOps       → cost by provider / model / practice / seniority, top spenders
  • Platform/DX  → provider reliability & latency, tool adoption, errors
  • Engineers    → session behavior, adoption by environment

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Make the src package importable whether run from repo root or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analytics import config, queries  # noqa: E402
from analytics.queries import Filters  # noqa: E402

st.set_page_config(page_title="AI Assistant Usage Analytics", page_icon="📊", layout="wide")

DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

# One hue per provider, used by every chart so a provider is the same color everywhere.
# Mid-luminance picks: each clears WCAG 3:1 non-text contrast on both the light (#ffffff)
# and dark (#0e1117) Streamlit backgrounds (3.1–3.8 light, 5.0–6.1 dark).
PROVIDER_COLORS = {
    "claude_code": "#D97757",  # warm orange
    "openai": "#10A37F",       # teal green
    "gemini": "#4285F4",       # blue
    "deepseek": "#9B6BDF",     # violet
}
OTHER_COLOR = "#7D8796"        # any provider without a pinned color (3.6 light, 5.2 dark)
ANOMALY_COLOR = "#cf3a3a"
NO_DATA = "No data for the current filters — widen the date range or clear a filter."


@st.cache_resource
def get_connection():
    return queries.connect(read_only=True)


@st.cache_data(ttl=600)
def get_options():
    return queries.filter_options(get_connection())


def money(x: float) -> str:
    return f"${x:,.2f}"


def num(x) -> float:
    """NULL-safe float for metric cards (query NULLs arrive as None/NaN)."""
    return 0.0 if x is None or pd.isna(x) else float(x)


def rounded(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """Round numeric columns only (date columns would make pandas warn)."""
    return df.round({c: n for c in df.select_dtypes("number").columns})


def provider_label(p) -> str:
    """Display name for a provider id, from the query layer's label map."""
    if p is None or (isinstance(p, float) and pd.isna(p)):
        return "Unknown"
    return get_options()["provider_labels"].get(p, str(p))


def labelled(df: pd.DataFrame) -> pd.DataFrame:
    """Swap provider ids for display names so legends read 'Claude Code', not 'claude_code'."""
    if "provider" not in df.columns:
        return df
    return df.assign(provider=df["provider"].map(provider_label))


def pstyle(df: pd.DataFrame) -> dict:
    """Plotly Express kwargs pinning each provider (in a ``labelled`` frame) to its color."""
    cmap = {provider_label(k): v for k, v in PROVIDER_COLORS.items()}
    order = [provider_label(k) for k in PROVIDER_COLORS]
    for p in df["provider"].dropna().unique():
        if p not in cmap:
            cmap[p] = OTHER_COLOR
            order.append(p)
    return dict(color="provider", color_discrete_map=cmap,
                category_orders={"provider": order})


def single_color(in_view: list[str]) -> dict:
    """With exactly one provider in view, draw single-series charts in that provider's hue."""
    if len(in_view) == 1:
        return dict(color_discrete_sequence=[PROVIDER_COLORS.get(in_view[0], OTHER_COLOR)])
    return {}


def anomaly_chart(df, value_col: str, label: str, pct: bool = False):
    """Daily series with its trailing baseline and anomalous days ringed in red."""
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["event_date"], y=df[value_col], mode="lines+markers",
                             name=label, line=dict(color="#2a78d6", width=2),
                             marker=dict(size=4)))
    fig.add_trace(go.Scatter(x=df["event_date"], y=df["baseline"], mode="lines",
                             name="trailing baseline", line=dict(color="#98a2b3", width=1.5,
                             dash="dot")))
    an = df[df["is_anomaly"]]
    if not an.empty:
        fig.add_trace(go.Scatter(x=an["event_date"], y=an[value_col], mode="markers",
                                 name="anomaly", marker=dict(color=ANOMALY_COLOR, size=13,
                                 symbol="circle-open", line=dict(width=3))))
    fig.update_layout(title=f"{label} — daily, anomalies flagged", hovermode="x unified",
                      legend=dict(orientation="h", y=1.12, x=0), margin=dict(t=60))
    if pct:
        fig.update_yaxes(tickformat=".1%")
    return fig


def anomaly_chart_by_provider(df, value_col: str, label: str, pct: bool = False):
    """One line per provider (scored against its own baseline), anomalies ringed in red."""
    d = labelled(df)
    fig = px.line(d, x="event_date", y=value_col, markers=True, **pstyle(d))
    fig.update_traces(marker=dict(size=4))
    an = d[d["is_anomaly"].astype(bool)]
    if not an.empty:
        fig.add_trace(go.Scatter(x=an["event_date"], y=an[value_col], mode="markers",
                                 name="anomaly", text=an["provider"],
                                 hovertemplate="%{text}: %{y}<extra>anomaly</extra>",
                                 marker=dict(color=ANOMALY_COLOR, size=13,
                                 symbol="circle-open", line=dict(width=3))))
    fig.update_layout(title=f"{label} by provider — daily, anomalies flagged",
                      hovermode="x unified", legend=dict(orientation="h", y=1.12, x=0),
                      margin=dict(t=60), yaxis_title=label)
    if pct:
        fig.update_yaxes(tickformat=".1%")
    return fig


def sidebar_filters() -> Filters:
    opts = get_options()
    st.sidebar.header("Filters")
    providers = st.sidebar.multiselect("Provider", opts["providers"], format_func=provider_label,
                                       help="Leave empty to include every provider.")
    date_min, date_max = opts["date_min"], opts["date_max"]
    if date_min and date_max:
        dr = st.sidebar.date_input(
            "Date range", value=(date_min, date_max),
            min_value=date_min, max_value=date_max,
        )
        start, end = (dr if isinstance(dr, (list, tuple)) and len(dr) == 2 else (date_min, date_max))
    else:
        start = end = None
    practices = st.sidebar.multiselect("Practice", opts["practices"])
    levels = st.sidebar.multiselect("Seniority level", opts["levels"])
    # Model families of the chosen providers, prefixed with the provider so the list reads
    # grouped (multiselect has no option groups).
    by_provider = opts["models_by_provider"]
    owner = {m: p for p in (providers or list(by_provider)) for m in by_provider.get(p, [])}
    families = st.sidebar.multiselect(
        "Model", list(owner), format_func=lambda m: f"{provider_label(owner[m])} · {m}")
    st.sidebar.caption("Filters apply to every chart below.")
    # The options are model *families*, so they go to ``model_families``; ``models`` (raw ids)
    # stays empty.
    return Filters(start_date=start, end_date=end, providers=providers,
                   practices=practices, levels=levels, model_families=families)


# --- Overview (Leadership) ---------------------------------------------------

def tab_overview(con, f: Filters, in_view: list[str]) -> None:
    st.caption("**Leadership** — is adoption and spend growing, and on which assistants? "
               "Informs budget and rollout decisions.")
    daily = queries.daily_activity(con, f)
    if daily.empty:
        st.warning(NO_DATA)
        return
    if len(in_view) > 1:
        by_day = labelled(queries.provider_share_over_time(con, f, grain="day"))
        ps = pstyle(by_day)
        st.plotly_chart(
            px.area(by_day, x="period", y="total_cost", title="Daily spend (USD) by provider",
                    labels={"period": "date"}, **ps),
            width="stretch")
        cols = st.columns(2)
        cols[0].plotly_chart(
            px.line(by_day, x="period", y="users", title="Daily active users by provider",
                    labels={"period": "date"}, markers=True, **ps), width="stretch")
        cols[1].plotly_chart(
            px.bar(by_day, x="period", y="requests", title="Daily API requests by provider",
                   labels={"period": "date"}, **ps), width="stretch")
    else:
        sc = single_color(in_view)
        st.plotly_chart(
            px.area(daily, x="event_date", y="cost_usd", title="Daily spend (USD)", **sc),
            width="stretch",
        )
        cols = st.columns(2)
        cols[0].plotly_chart(
            px.line(daily, x="event_date", y="active_users", title="Daily active users",
                    markers=True, **sc), width="stretch")
        cols[1].plotly_chart(
            px.bar(daily, x="event_date", y="api_requests", title="Daily API requests", **sc),
            width="stretch")

    heat = queries.activity_heatmap(con, f)
    if not heat.empty:
        heat["day"] = heat["event_dow"].map(lambda d: DOW[int(d)])
        pivot = heat.pivot_table(index="day", columns="event_hour",
                                 values="events", fill_value=0).reindex(DOW)
        st.plotly_chart(
            px.imshow(pivot, aspect="auto", title="Activity by day & hour (UTC)",
                      labels=dict(color="events")), width="stretch")


# --- Providers (Leadership / FinOps / Platform-DX) ---------------------------

def tab_providers(con, f: Filters, summary: pd.DataFrame) -> None:
    st.caption("**Leadership / FinOps / Platform-DX** — which assistant gives the best value "
               "and reliability, and who uses several? Informs vendor consolidation, seat "
               "and model-routing decisions.")
    if summary.empty:
        st.warning(NO_DATA)
        return
    if len(summary) == 1:
        st.info("Only one provider in the current selection — select two or more (or clear "
                "the Provider filter) to compare them.")

    # KPI cards, one column per provider.
    cards = st.columns(len(summary))
    for col, row in zip(cards, summary.itertuples(index=False)):
        col.markdown(f"**{provider_label(row.provider)}**")
        col.metric("Spend", money(num(row.total_cost)),
                   help=f"{money(num(row.cost_per_request))} per request")
        col.metric("Active users", f"{int(num(row.users)):,}")
        col.metric("Cost / user", money(num(row.cost_per_user)))
        col.metric("API error rate", f"{num(row.error_rate) * 100:.2f}%")

    # Share over time — normalized per week so the stack always reads as 100%.
    metric = st.radio("Share of", ["Spend", "Requests", "Active users"], horizontal=True,
                      key="share_metric")
    ycol = {"Spend": "cost_share", "Requests": "requests_share",
            "Active users": "users_share"}[metric]
    share = labelled(queries.provider_share_over_time(con, f, grain="week"))
    if share.empty:
        st.warning(NO_DATA)
    else:
        st.plotly_chart(
            px.area(share, x="period", y=ycol,
                    title=f"Weekly share of {metric.lower()} by provider",
                    labels={"period": "week", ycol: "share"}, **pstyle(share))
            .update_yaxes(tickformat=".0%"),
            width="stretch")
        if metric == "Active users":
            st.caption("A person using two providers in a week counts toward both.")

    # Unit economics & latency.
    s = labelled(summary)
    ps = pstyle(s)
    cols = st.columns(2)
    cols[0].plotly_chart(
        px.bar(s, x="provider", y="cost_per_1k_tokens", title="Cost per 1K tokens (USD)",
               text_auto=".4f", **ps).update_layout(showlegend=False),
        width="stretch")
    lat = s.melt(id_vars="provider", value_vars=["p50_latency_ms", "p95_latency_ms"],
                 var_name="percentile", value_name="latency_ms")
    lat["percentile"] = lat["percentile"].map({"p50_latency_ms": "p50", "p95_latency_ms": "p95"})
    cols[1].plotly_chart(
        px.bar(lat, x="provider", y="latency_ms", pattern_shape="percentile", barmode="group",
               title="Request latency p50 / p95 (ms)", **ps),
        width="stretch")

    # Who prefers which provider.
    st.subheader("Provider preference by cohort")
    cc = st.columns(2)
    dim = cc[0].selectbox("Rows", ["practice", "level", "location", "client"], key="prov_dim",
                          format_func=lambda c: {"practice": "Practice", "level": "Seniority",
                                                 "location": "Location", "client": "Client"}[c])
    measure = cc[1].radio("Cell value", ["Active users", "Spend (USD)"], horizontal=True,
                          key="prov_measure")
    bd = queries.provider_by_dimension(con, f, dim)
    if bd.empty:
        st.warning(NO_DATA)
    else:
        bd = labelled(bd)
        val = "users" if measure == "Active users" else "total_cost"
        # Rows arrive in display order (levels by level_num); keep it through the pivot.
        rows = list(dict.fromkeys(bd[dim]))
        pivot = bd.pivot_table(index=dim, columns="provider", values=val,
                               aggfunc="sum", fill_value=0).reindex(rows)
        st.plotly_chart(
            px.imshow(pivot, aspect="auto", text_auto=True if val == "users" else ".3s",
                      color_continuous_scale="Blues",
                      title=f"{measure} — {dim} × provider", labels=dict(color=measure)),
            width="stretch")

    # Multi-provider users (FinOps: overlapping subscriptions).
    st.subheader("People using several assistants")
    mpu = queries.multi_provider_users(con, f)
    dist, pairs = mpu["distribution"], mpu["pairs"]
    if dist.empty:
        st.warning(NO_DATA)
    else:
        cols = st.columns([3, 2])
        cols[0].plotly_chart(
            px.bar(dist, x="n_providers", y="users", text_auto=True,
                   title="Users by number of providers used",
                   labels={"n_providers": "providers used"}).update_xaxes(dtick=1),
            width="stretch")
        cols[1].markdown("**Overlap pairs** (users active on both)")
        if pairs.empty:
            cols[1].info("No one in the selection uses more than one provider.")
        else:
            show = pairs.assign(provider_a=pairs["provider_a"].map(provider_label),
                                provider_b=pairs["provider_b"].map(provider_label),
                                jaccard=pairs["jaccard"].round(2))
            cols[1].dataframe(show, width="stretch", hide_index=True)
            cols[1].caption("jaccard = shared users ÷ users of either provider.")

    # Model leaderboard (Platform/DX: which models are actually used).
    st.subheader("Model leaderboard")
    lb = queries.model_leaderboard(con, f)
    if lb.empty:
        st.warning(NO_DATA)
    else:
        show = labelled(lb).drop(columns="provider_label").sort_values("total_cost",
                                                                       ascending=False)
        st.dataframe(rounded(show, 4), width="stretch", hide_index=True)


# --- Cost (FinOps) -----------------------------------------------------------

def tab_cost(con, f: Filters, in_view: list[str]) -> None:
    st.caption("**FinOps** — where does the money go, by model, team and seniority? "
               "Informs model-routing defaults and per-team budgets.")
    cols = st.columns(2)
    if len(in_view) > 1:
        lb = labelled(queries.model_leaderboard(con, f))
        if lb.empty:
            st.warning(NO_DATA)
        else:
            ps = pstyle(lb)
            cols[0].plotly_chart(
                px.sunburst(lb, path=["provider", "model_family"], values="total_cost",
                            title="Spend by provider → model", color="provider",
                            color_discrete_map=ps["color_discrete_map"]),
                width="stretch")
            cols[1].plotly_chart(
                px.bar(lb.sort_values("avg_cost", ascending=False), x="model_family",
                       y="avg_cost", title="Avg cost per request by model",
                       labels={"model_family": "model"}, **ps),
                width="stretch")
    else:
        by_model = queries.cost_by_model(con, f)
        if by_model.empty:
            st.warning(NO_DATA)
        else:
            cols[0].plotly_chart(
                px.pie(by_model, names="model", values="total_cost",
                       title="Spend by model", hole=0.45), width="stretch")
            cols[1].plotly_chart(
                px.bar(by_model, x="model", y="avg_cost", title="Avg cost per request by model",
                       **single_color(in_view)), width="stretch")
    sc = single_color(in_view)
    cols = st.columns(2)
    by_practice = queries.cost_by_practice(con, f)
    if not by_practice.empty:
        cols[0].plotly_chart(
            px.bar(by_practice, x="practice", y="cost_per_user",
                   title="Cost per user by practice", **sc), width="stretch")
    by_level = queries.cost_by_level(con, f)
    if not by_level.empty:
        cols[1].plotly_chart(
            px.bar(by_level, x="level", y="cost_per_user",
                   title="Cost per user by seniority", **sc), width="stretch")
    st.subheader("Top users by spend")
    top = queries.top_users(con, f)
    if top.empty:
        st.warning(NO_DATA)
    else:
        st.dataframe(top, width="stretch", hide_index=True)


# --- Tools (Platform/DX, Claude Code only) -----------------------------------

def tab_tools(con, f: Filters, in_view: list[str]) -> None:
    st.caption("**Platform-DX** — which agent tools get used, trusted and succeed? Informs "
               "permission defaults and which tools to harden.")
    if in_view and "claude_code" not in in_view:
        st.info("Tool telemetry is available for Claude Code only — add Claude Code to the "
                "Provider filter to see tool decisions and results.")
        return
    tools = queries.tool_usage(con, f)
    if tools.empty:
        st.warning("No tool events for the current filters."
                   + (" Tool events carry no model — clear the Model filter to see them."
                      if f.model_families else ""))
        return
    if len(in_view) > 1:
        st.caption("Accept/reject decisions and success rates come from Claude Code; ChatGPT "
                   "tool calls are counted as runs only.")
    sc = single_color(["claude_code"])
    cols = st.columns(2)
    cols[0].plotly_chart(
        px.bar(tools.head(15), x="runs", y="tool_name", orientation="h",
               title="Most-used tools", **sc).update_yaxes(categoryorder="total ascending"),
        width="stretch")
    tools_r = tools[tools["success_rate"].notna()].copy()
    cols[1].plotly_chart(
        px.bar(tools_r, x="success_rate", y="tool_name", orientation="h",
               title="Tool success rate", **sc).update_yaxes(categoryorder="total ascending"),
        width="stretch")
    # Only tools that went through a permission decision (Claude Code); ChatGPT tool calls
    # have none, and would otherwise plot as a misleading 0%.
    decided = tools[tools["decisions"] > 0].copy()
    decided["reject_rate"] = decided["rejections"] / decided["decisions"]
    st.plotly_chart(
        px.bar(decided.sort_values("reject_rate", ascending=False),
               x="tool_name", y="reject_rate",
               title="Tool rejection rate (user rejected the proposed call)", **sc),
        width="stretch")
    src = queries.decision_sources(con, f)
    if not src.empty:
        st.plotly_chart(
            px.pie(src, names="decision_source", values="decisions",
                   title="How tool calls were authorized", hole=0.45),
            width="stretch")


# --- Reliability (Platform/DX) -----------------------------------------------

def tab_reliability(con, f: Filters, in_view: list[str], summary: pd.DataFrame) -> None:
    st.caption("**Platform-DX** — which provider fails most, and with what errors? Informs "
               "retry/fallback policy and rate-limit negotiations.")
    if len(in_view) > 1:
        rate = labelled(queries.error_rate_over_time(con, f, by_provider=True))
        if rate.empty:
            st.warning(NO_DATA)
        else:
            cols = st.columns([2, 1])
            cols[0].plotly_chart(
                px.line(rate, x="event_date", y="error_rate", markers=True,
                        title="API error rate over time by provider", **pstyle(rate))
                .update_yaxes(tickformat=".1%"),
                width="stretch")
            s = labelled(summary)
            cols[1].plotly_chart(
                px.bar(s, x="provider", y="error_rate", title="Error rate by provider",
                       **pstyle(s)).update_yaxes(tickformat=".1%").update_layout(showlegend=False),
                width="stretch")
    else:
        rate = queries.error_rate_over_time(con, f)
        if rate.empty:
            st.warning(NO_DATA)
        else:
            st.plotly_chart(
                px.line(rate, x="event_date", y="error_rate",
                        title="API error rate over time", markers=True, **single_color(in_view)),
                width="stretch")
    errs = queries.error_breakdown(con, f)
    if errs.empty:
        if not rate.empty:
            st.success("No API errors in the current selection. 🎉")
    else:
        cols = st.columns([1, 2])
        by_code = errs.groupby("status_code", as_index=False)["occurrences"].sum()
        cols[0].plotly_chart(
            px.pie(by_code, names="status_code", values="occurrences",
                   title="Errors by status code", hole=0.45), width="stretch")
        cols[1].dataframe(errs, width="stretch", hide_index=True)


# --- Sessions & Adoption (engineers / Platform-DX) ---------------------------

def tab_sessions(con, f: Filters, in_view: list[str]) -> None:
    st.caption("**Engineers / Platform-DX** — how long and how deep are working sessions, and "
               "in which environments? Informs enablement and supported-platform choices.")
    sc = single_color(in_view)
    # Duration & cost cover every provider in view; with several, one query per provider
    # (session_stats has no provider column) so the histograms show the mix.
    if len(in_view) > 1:
        parts = [queries.session_stats(con, replace(f, providers=[p])).assign(provider=p)
                 for p in in_view]
        sessions = labelled(pd.concat(parts, ignore_index=True))
        hs = dict(**pstyle(sessions), barmode="overlay", opacity=0.6)
    else:
        sessions = queries.session_stats(con, f)
        hs = sc
    if sessions.empty:
        st.warning(NO_DATA)
    else:
        st.metric("Median session (s)", f"{sessions['duration_s'].median():.0f}")
        cols = st.columns(2)
        cols[0].plotly_chart(
            px.histogram(sessions[sessions["duration_s"] > 0], x="duration_s", nbins=50,
                         title="Session duration distribution (s)", **hs),
            width="stretch")
        cols[1].plotly_chart(
            px.histogram(sessions, x="cost_usd", nbins=50,
                         title="Session cost distribution (USD)", **hs),
            width="stretch")
        if in_view != ["claude_code"]:
            st.caption("Providers without a session id are grouped into synthetic sessions "
                       "(same person, 30-minute inactivity gap).")

    # Turns (user_prompt) and tool calls exist only in Claude Code telemetry; other
    # providers' sessions would drag the medians to 0.
    st.markdown("**Claude Code sessions**")
    if in_view and "claude_code" not in in_view:
        st.info("Turns and tool calls per session are available for Claude Code only — add "
                "Claude Code to the Provider filter to see them.")
    elif not sessions.empty:
        # Several providers → keep the Claude Code slice; otherwise the frame is already CC-only.
        cc = (sessions[sessions["provider"] == provider_label("claude_code")]
              if "provider" in sessions.columns else sessions)
        if cc.empty:
            st.warning(NO_DATA)
        else:
            cols = st.columns(3)
            cols[0].metric("Median Claude Code session (s)", f"{cc['duration_s'].median():.0f}")
            cols[1].metric("Median turns / Claude Code session", f"{cc['turns'].median():.0f}")
            cols[2].metric("Median tool calls / Claude Code session",
                           f"{cc['tool_calls'].median():.0f}")
    st.subheader("Adoption by environment")
    dim = st.selectbox("Break down by",
                       ["client", "terminal_type", "cli_version", "os_type", "location"])
    if dim not in ("client", "location"):
        st.caption("Terminal, CLI version and OS are reported by Claude Code only; other "
                   "providers show as 'unknown'.")
    adoption = queries.adoption_by_dimension(con, f, dim)
    if adoption.empty:
        st.warning(NO_DATA)
    else:
        st.plotly_chart(
            px.bar(adoption.head(20), x=dim, y="users",
                   title=f"Active users by {dim}", **sc), width="stretch")


# --- Anomalies (FinOps / Platform-DX) ----------------------------------------

def tab_anomalies(con, f: Filters, in_view: list[str]) -> None:
    st.caption("**FinOps / Platform-DX** — did spend or errors jump unexpectedly? Informs "
               "when to investigate a cost blow-up or a provider incident.")
    st.markdown(
        "Causal rolling **z-score** detector — each day is scored against the mean ± "
        "standard deviation of the *previous* N days, so a spike is judged against the "
        "behaviour that preceded it. A day is flagged when **|z| ≥ threshold**. "
        "Useful for catching cost blow-ups (FinOps) and reliability regressions "
        "(Platform/DX) without hand-set limits.")
    cc = st.columns(3)
    window = cc[0].slider("Baseline window (days)", 3, 21, 7)
    zt = cc[1].slider("Anomaly threshold (|z|)", 1.5, 4.0, 3.0, 0.5)
    per_provider = cc[2].toggle(
        "Per provider", value=False, disabled=len(in_view) < 2,
        help="Score each provider against its own baseline, so one provider's spike "
             "isn't hidden by the others' volume.")
    per_provider = per_provider and len(in_view) > 1
    extra = {"by_provider": True} if per_provider else {}

    cost_df = queries.cost_anomalies(con, f, window=window, z_thresh=zt, **extra)
    err_df = queries.error_rate_anomalies(con, f, window=window, z_thresh=zt, **extra)
    if cost_df.empty:
        st.warning(NO_DATA)
        return
    chart = anomaly_chart_by_provider if per_provider else anomaly_chart
    key = ["provider"] if per_provider else []
    n_cost = int(cost_df["is_anomaly"].sum())
    n_err = int(err_df["is_anomaly"].sum()) if not err_df.empty else 0
    m = st.columns(2)
    m[0].metric("Cost-spike days", n_cost)
    m[1].metric("Error-rate-spike days", n_err)

    st.plotly_chart(chart(cost_df, "cost_usd", "Daily spend (USD)"), width="stretch")
    flagged = labelled(cost_df[cost_df["is_anomaly"].astype(bool)])
    if not flagged.empty:
        show = flagged[["event_date", *key, "cost_usd", "baseline", "z_score"]].copy()
        show.columns = ["Date", *[k.title() for k in key], "Cost (USD)", "Baseline", "z-score"]
        st.dataframe(rounded(show, 2), width="stretch", hide_index=True)
    else:
        st.success("No cost anomalies at the current threshold. 🎉")

    if err_df.empty:
        st.warning(NO_DATA)
        return
    st.plotly_chart(chart(err_df, "error_rate", "API error rate", pct=True),
                    width="stretch")
    flagged_e = labelled(err_df[err_df["is_anomaly"].astype(bool)])
    if not flagged_e.empty:
        show = flagged_e[["event_date", *key, "error_rate", "errors", "requests",
                          "baseline", "z_score"]].copy()
        show["error_rate"] = (show["error_rate"] * 100).round(2)
        show["baseline"] = (show["baseline"] * 100).round(2)
        show["z_score"] = show["z_score"].round(2)
        show.columns = ["Date", *[k.title() for k in key], "Error rate %", "Errors",
                        "Requests", "Baseline %", "z-score"]
        st.dataframe(show, width="stretch", hide_index=True)
    else:
        st.success("No error-rate anomalies at the current threshold. 🎉")
    st.caption("Baseline uses only prior days (causal), so the first few days of "
               "the range carry no score until enough history accrues.")


# --- Data quality (data owners) ----------------------------------------------

def data_quality_expander(con, f: Filters) -> None:
    with st.expander("🧪 Data quality"):
        st.caption("**Data owners** — can these numbers be trusted? Informs which feeds, "
                   "model catalog entries and employee records need fixing.")
        # Latest ingest audit per provider + live counts of unpriced requests and
        # users missing from the employee directory.
        dq = queries.data_quality(con, f)
        if dq.empty:
            st.warning("No ingest runs recorded — rebuild the database with the ETL.")
        else:
            missing = dq[dq["status"] != "ok"]
            if not missing.empty:
                st.warning("Source file missing for: "
                           + ", ".join(missing["provider_label"]) + ".")
            m = st.columns(4)
            m[0].metric("Rows skipped", f"{int(dq['rows_skipped'].sum()):,}")
            m[1].metric("Duplicates removed", f"{int(dq['duplicates_removed'].sum()):,}")
            m[2].metric("Requests without cost", f"{int(dq['unpriced_requests'].sum()):,}")
            m[3].metric("Users not in directory", f"{int(dq['users_without_employee'].sum()):,}")
            st.dataframe(labelled(dq).drop(columns="provider_label"),
                         width="stretch", hide_index=True)

        # Calendar days in the selected range with no events at all.
        if f.start_date and f.end_date:
            daily = queries.daily_activity(con, f)
            seen = set(pd.to_datetime(daily["event_date"]).dt.date) if not daily.empty else set()
            gaps = [d.date() for d in pd.date_range(f.start_date, f.end_date)
                    if d.date() not in seen]
            if gaps:
                st.warning(f"{len(gaps)} day(s) in the range have no events: "
                           + ", ".join(str(d) for d in gaps[:10])
                           + (" …" if len(gaps) > 10 else ""))
            else:
                st.success("Every day in the selected range has events.")


def main() -> None:
    st.title("📊 AI Assistant Usage Analytics")

    try:
        con = get_connection()
    except FileNotFoundError as exc:
        st.error(str(exc))
        st.info("Build the database first: `make data && make etl` (or `docker compose up`).")
        st.stop()

    f = sidebar_filters()
    # One per-provider rollup drives the Providers tab and provider coloring.
    summary = queries.provider_summary(con, f)
    in_view = summary["provider"].tolist() if not summary.empty else []

    # --- KPI row (Leadership) ---
    k = queries.kpis(con, f)
    c = st.columns(7)
    c[0].metric("Total spend", money(k["total_cost"]))
    c[1].metric("API requests", f"{int(k['api_requests']):,}")
    c[2].metric("Sessions", f"{int(k['sessions']):,}")
    c[3].metric("Active users", f"{int(k['active_users']):,}")
    c[4].metric("Tokens", f"{int(k['total_tokens']):,}")
    err_rate = (k["errors"] / k["api_requests"] * 100) if k["api_requests"] else 0
    c[5].metric("API error rate", f"{err_rate:.2f}%")
    c[6].metric("Providers in use", int(k["providers_in_use"]),
                help=", ".join(provider_label(p) for p in in_view) or None)
    if not in_view:
        st.warning(NO_DATA)

    tabs = st.tabs(["📈 Overview", "🔀 Providers", "💰 Cost", "🛠 Tools", "⚠️ Reliability",
                    "🧭 Sessions & Adoption", "🚨 Anomalies"])
    with tabs[0]:
        tab_overview(con, f, in_view)
    with tabs[1]:
        tab_providers(con, f, summary)
    with tabs[2]:
        tab_cost(con, f, in_view)
    with tabs[3]:
        tab_tools(con, f, in_view)
    with tabs[4]:
        tab_reliability(con, f, in_view, summary)
    with tabs[5]:
        tab_sessions(con, f, in_view)
    with tabs[6]:
        tab_anomalies(con, f, in_view)

    data_quality_expander(con, f)
    st.caption(f"Database: `{config.DUCKDB_PATH.name}` · built by the ETL from the "
               "provider usage logs")


if __name__ == "__main__":
    main()
