"""Interactive Claude Code usage analytics dashboard (Streamlit + Plotly).

Organized around the personas who consume this data:
  • Leadership   → overview KPIs, cost trends, spend by org dimension
  • FinOps       → cost by model / practice / seniority, top spenders
  • Platform/DX  → tool adoption, accept-vs-reject, reliability & errors
  • Everyone     → session behavior, adoption by environment

Run:  streamlit run dashboard/app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

# Make the src package importable whether run from repo root or elsewhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analytics import config, queries  # noqa: E402
from analytics.queries import Filters  # noqa: E402

st.set_page_config(page_title="Claude Code Analytics", page_icon="📊", layout="wide")

DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


@st.cache_resource
def get_connection():
    return queries.connect(read_only=True)


@st.cache_data(ttl=600)
def get_options():
    return queries.filter_options(get_connection())


def money(x: float) -> str:
    return f"${x:,.2f}"


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
                                 name="anomaly", marker=dict(color="#cf3a3a", size=13,
                                 symbol="circle-open", line=dict(width=3))))
    fig.update_layout(title=f"{label} — daily, anomalies flagged", hovermode="x unified",
                      legend=dict(orientation="h", y=1.12, x=0), margin=dict(t=60))
    if pct:
        fig.update_yaxes(tickformat=".1%")
    return fig


def sidebar_filters() -> Filters:
    opts = get_options()
    st.sidebar.header("Filters")
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
    models = st.sidebar.multiselect("Model", opts["models"])
    st.sidebar.caption("Filters apply to every chart below.")
    return Filters(start_date=start, end_date=end,
                   practices=practices, levels=levels, models=models)


def main() -> None:
    st.title("📊 Claude Code Usage Analytics")

    try:
        con = get_connection()
    except FileNotFoundError as exc:
        st.error(str(exc))
        st.info("Build the database first: `make data && make etl` (or `docker compose up`).")
        st.stop()

    f = sidebar_filters()

    # --- KPI row (Leadership) ---
    k = queries.kpis(con, f)
    c = st.columns(6)
    c[0].metric("Total spend", money(k["total_cost"]))
    c[1].metric("API requests", f"{int(k['api_requests']):,}")
    c[2].metric("Sessions", f"{int(k['sessions']):,}")
    c[3].metric("Active users", f"{int(k['active_users']):,}")
    c[4].metric("Tokens", f"{int(k['total_tokens']):,}")
    err_rate = (k["errors"] / k["api_requests"] * 100) if k["api_requests"] else 0
    c[5].metric("API error rate", f"{err_rate:.2f}%")

    tabs = st.tabs(["📈 Overview", "💰 Cost", "🛠 Tools", "⚠️ Reliability",
                    "🧭 Sessions & Adoption", "🚨 Anomalies"])

    # --- Overview ---
    with tabs[0]:
        daily = queries.daily_activity(con, f)
        if daily.empty:
            st.info("No data for the current filters.")
        else:
            st.plotly_chart(
                px.area(daily, x="event_date", y="cost_usd", title="Daily spend (USD)"),
                use_container_width=True,
            )
            cols = st.columns(2)
            cols[0].plotly_chart(
                px.line(daily, x="event_date", y="active_users", title="Daily active users",
                        markers=True), use_container_width=True)
            cols[1].plotly_chart(
                px.bar(daily, x="event_date", y="api_requests", title="Daily API requests"),
                use_container_width=True)

            heat = queries.activity_heatmap(con, f)
            if not heat.empty:
                heat["day"] = heat["event_dow"].map(lambda d: DOW[int(d)])
                pivot = heat.pivot_table(index="day", columns="event_hour",
                                         values="events", fill_value=0).reindex(DOW)
                st.plotly_chart(
                    px.imshow(pivot, aspect="auto", title="Activity by day & hour (UTC)",
                              labels=dict(color="events")), use_container_width=True)

    # --- Cost ---
    with tabs[1]:
        by_model = queries.cost_by_model(con, f)
        cols = st.columns(2)
        if not by_model.empty:
            cols[0].plotly_chart(
                px.pie(by_model, names="model", values="total_cost",
                       title="Spend by model", hole=0.45), use_container_width=True)
            cols[1].plotly_chart(
                px.bar(by_model, x="model", y="avg_cost",
                       title="Avg cost per request by model"), use_container_width=True)
        cols = st.columns(2)
        by_practice = queries.cost_by_practice(con, f)
        if not by_practice.empty:
            cols[0].plotly_chart(
                px.bar(by_practice, x="practice", y="cost_per_user",
                       title="Cost per user by practice"), use_container_width=True)
        by_level = queries.cost_by_level(con, f)
        if not by_level.empty:
            cols[1].plotly_chart(
                px.bar(by_level, x="level", y="cost_per_user",
                       title="Cost per user by seniority"), use_container_width=True)
        st.subheader("Top users by spend")
        st.dataframe(queries.top_users(con, f), use_container_width=True, hide_index=True)

    # --- Tools ---
    with tabs[2]:
        tools = queries.tool_usage(con, f)
        if tools.empty:
            st.info("No tool events for the current filters.")
        else:
            cols = st.columns(2)
            cols[0].plotly_chart(
                px.bar(tools.head(15), x="runs", y="tool_name", orientation="h",
                       title="Most-used tools").update_yaxes(categoryorder="total ascending"),
                use_container_width=True)
            tools_r = tools[tools["success_rate"].notna()].copy()
            cols[1].plotly_chart(
                px.bar(tools_r, x="success_rate", y="tool_name", orientation="h",
                       title="Tool success rate").update_yaxes(categoryorder="total ascending"),
                use_container_width=True)
            tools["reject_rate"] = tools["rejections"] / tools["decisions"].replace(0, 1)
            st.plotly_chart(
                px.bar(tools.sort_values("reject_rate", ascending=False),
                       x="tool_name", y="reject_rate",
                       title="Tool rejection rate (user rejected the proposed call)"),
                use_container_width=True)
            src = queries.decision_sources(con, f)
            if not src.empty:
                st.plotly_chart(
                    px.pie(src, names="decision_source", values="decisions",
                           title="How tool calls were authorized", hole=0.45),
                    use_container_width=True)

    # --- Reliability ---
    with tabs[3]:
        rate = queries.error_rate_over_time(con, f)
        if not rate.empty:
            st.plotly_chart(
                px.line(rate, x="event_date", y="error_rate",
                        title="API error rate over time", markers=True),
                use_container_width=True)
        errs = queries.error_breakdown(con, f)
        if errs.empty:
            st.success("No API errors in the current selection. 🎉")
        else:
            cols = st.columns([1, 2])
            by_code = errs.groupby("status_code", as_index=False)["occurrences"].sum()
            cols[0].plotly_chart(
                px.pie(by_code, names="status_code", values="occurrences",
                       title="Errors by status code", hole=0.45), use_container_width=True)
            cols[1].dataframe(errs, use_container_width=True, hide_index=True)

    # --- Sessions & Adoption ---
    with tabs[4]:
        sessions = queries.session_stats(con, f)
        if not sessions.empty:
            cols = st.columns(3)
            cols[0].metric("Median session (s)", f"{sessions['duration_s'].median():.0f}")
            cols[1].metric("Median turns / session", f"{sessions['turns'].median():.0f}")
            cols[2].metric("Median tool calls / session", f"{sessions['tool_calls'].median():.0f}")
            cols = st.columns(2)
            cols[0].plotly_chart(
                px.histogram(sessions[sessions["duration_s"] > 0], x="duration_s", nbins=50,
                             title="Session duration distribution (s)"),
                use_container_width=True)
            cols[1].plotly_chart(
                px.histogram(sessions, x="cost_usd", nbins=50,
                             title="Session cost distribution (USD)"),
                use_container_width=True)
        st.subheader("Adoption by environment")
        dim = st.selectbox("Break down by",
                           ["terminal_type", "cli_version", "os_type", "location"])
        adoption = queries.adoption_by_dimension(con, f, dim)
        st.plotly_chart(
            px.bar(adoption.head(20), x=dim, y="users",
                   title=f"Active users by {dim}"), use_container_width=True)

    # --- Anomalies ---
    with tabs[5]:
        st.markdown(
            "Causal rolling **z-score** detector — each day is scored against the mean ± "
            "standard deviation of the *previous* N days, so a spike is judged against the "
            "behaviour that preceded it. A day is flagged when **|z| ≥ threshold**. "
            "Useful for catching cost blow-ups (FinOps) and reliability regressions "
            "(Platform/DX) without hand-set limits.")
        cc = st.columns(2)
        window = cc[0].slider("Baseline window (days)", 3, 21, 7)
        zt = cc[1].slider("Anomaly threshold (|z|)", 1.5, 4.0, 3.0, 0.5)

        cost_df = queries.cost_anomalies(con, f, window=window, z_thresh=zt)
        err_df = queries.error_rate_anomalies(con, f, window=window, z_thresh=zt)
        if cost_df.empty:
            st.info("No data for the current filters.")
        else:
            n_cost = int(cost_df["is_anomaly"].sum())
            n_err = int(err_df["is_anomaly"].sum()) if not err_df.empty else 0
            m = st.columns(2)
            m[0].metric("Cost-spike days", n_cost)
            m[1].metric("Error-rate-spike days", n_err)

            st.plotly_chart(anomaly_chart(cost_df, "cost_usd", "Daily spend (USD)"),
                            use_container_width=True)
            flagged = cost_df[cost_df["is_anomaly"]]
            if not flagged.empty:
                show = flagged[["event_date", "cost_usd", "baseline", "z_score"]].copy()
                show.columns = ["Date", "Cost (USD)", "Baseline", "z-score"]
                st.dataframe(show.round(2), use_container_width=True, hide_index=True)
            else:
                st.success("No cost anomalies at the current threshold. 🎉")

            st.plotly_chart(anomaly_chart(err_df, "error_rate", "API error rate", pct=True),
                            use_container_width=True)
            flagged_e = err_df[err_df["is_anomaly"]]
            if not flagged_e.empty:
                show = flagged_e[["event_date", "error_rate", "errors", "requests",
                                  "baseline", "z_score"]].copy()
                show["error_rate"] = (show["error_rate"] * 100).round(2)
                show["baseline"] = (show["baseline"] * 100).round(2)
                show["z_score"] = show["z_score"].round(2)
                show.columns = ["Date", "Error rate %", "Errors", "Requests",
                                "Baseline %", "z-score"]
                st.dataframe(show, use_container_width=True, hide_index=True)
            else:
                st.success("No error-rate anomalies at the current threshold. 🎉")
            st.caption("Baseline uses only prior days (causal), so the first few days of "
                       "the range carry no score until enough history accrues.")

    st.caption(f"Database: `{config.DUCKDB_PATH.name}` · built by the ETL from telemetry_logs.jsonl")


if __name__ == "__main__":
    main()
