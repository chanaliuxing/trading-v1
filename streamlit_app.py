# -*- coding: utf-8 -*-
"""
streamlit_app.py
----------------
Chain-of-Alpha Research Dashboard

Visualizes alpha_loop.py output artifacts including:
- Generation summaries and OOS metrics
- IS/OOS leaderboards
- Parameter origin tracking (LLM/ML/BOTH/MIXED)
- Equity curves and drawdowns
- Contribution charts
- Parameter heatmaps
- Champion parameters by fold

Usage:
  streamlit run streamlit_app.py -- --out_dir ./alpha_out

Dependencies:
  pip install streamlit pandas matplotlib
"""

import os
import json
import argparse
from pathlib import Path
import pandas as pd
import streamlit as st
import matplotlib.pyplot as plt


def load_json(p):
    """Load JSON file."""
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def list_generations(out_dir):
    """List all gen_* directories in output folder."""
    gens = []
    for p in sorted(Path(out_dir).glob("gen_*")):
        if p.is_dir():
            gens.append(p.name)
    return gens


def eq_plot(curves, title):
    """Plot normalized equity curves."""
    if not curves:
        st.info("No equity curves available.")
        return
    fig, ax = plt.subplots(figsize=(10, 6))
    for payload in curves:
        curve = payload.get("equity_curve", [])
        if not curve:
            continue
        xs = [ts for ts, _ in curve]
        eq = [float(e) for _, e in curve]
        base = eq[0] if eq and eq[0] != 0 else 1e-9
        norm = [e / base for e in eq]
        ax.plot(xs, norm, label=f"Fold {payload.get('fold_id')}")
    ax.set_xlabel("Timestamp (ms)")
    ax.set_ylabel("Normalized Equity")
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3)
    st.pyplot(fig)


def dd_plot(curves, title):
    """Plot drawdown curves."""
    if not curves:
        st.info("No drawdown curves available.")
        return
    fig, ax = plt.subplots(figsize=(10, 6))
    for payload in curves:
        curve = payload.get("equity_curve", [])
        if not curve:
            continue
        eq = [float(e) for _, e in curve]
        peak, dd = -1e18, []
        for e in eq:
            peak = max(peak, e)
            dd.append(0.0 if peak <= 0 else (peak - e) / peak)
        xs = [ts for ts, _ in curve]
        ax.plot(xs, dd, label=f"Fold {payload.get('fold_id')}")
    ax.set_xlabel("Timestamp (ms)")
    ax.set_ylabel("Drawdown")
    ax.set_title(title)
    ax.legend()
    ax.grid(True, alpha=0.3)
    st.pyplot(fig)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out_dir", required=False, default="./alpha_out")
    args, _ = parser.parse_known_args()

    st.set_page_config(page_title="Chain-of-Alpha Dashboard", layout="wide")

    # Sidebar
    st.sidebar.title("Settings")
    out_dir = st.sidebar.text_input("Output Directory", args.out_dir)

    gens = list_generations(out_dir)
    if not gens:
        st.warning(f"No gen_* directories found in {out_dir}")
        return

    gen = st.sidebar.selectbox("Select Generation", gens, index=len(gens) - 1)
    gdir = Path(out_dir) / gen

    # Load generation summary
    summary_path = gdir / "gen_summary.json"
    if not summary_path.exists():
        st.error(f"gen_summary.json not found in {gdir}")
        return

    summary = load_json(summary_path)
    mode = summary.get("mode", "unknown")

    # Header
    st.title(f"Chain-of-Alpha Dashboard: {gen}")
    st.markdown(f"**Mode**: `{mode}`")

    # Metrics row
    col1, col2, col3 = st.columns(3)

    # Handle two-stage results
    if "stage1" in summary:
        s1 = summary["stage1"].get("oos_summary", {})
        col1.metric("Stage1 OOS Sharpe", f'{s1.get("oos_avg_sharpe", 0):.3f}')
        col2.metric("Stage1 OOS MaxDD", f'{s1.get("oos_avg_max_drawdown", 0):.3f}')
        col3.metric("Stage1 OOS Trades", int(s1.get("oos_trades", 0)))

        if "stage2" in summary:
            st.markdown("---")
            st.subheader("Stage 2 (Optuna Refinement)")
            col4, col5, col6 = st.columns(3)
            s2 = summary["stage2"].get("oos_summary", {})
            col4.metric("Stage2 OOS Sharpe", f'{s2.get("oos_avg_sharpe", 0):.3f}')
            col5.metric("Stage2 OOS MaxDD", f'{s2.get("oos_avg_max_drawdown", 0):.3f}')
            col6.metric("Stage2 OOS Trades", int(s2.get("oos_trades", 0)))

            # Show best params
            best_params = summary["stage2"].get("best_params", {})
            if best_params:
                st.json(best_params)
    else:
        # Single-stage results
        s = summary.get("oos_summary", {})
        col1.metric("OOS Sharpe", f'{s.get("oos_avg_sharpe", 0):.3f}')
        col2.metric("OOS MaxDD", f'{s.get("oos_avg_max_drawdown", 0):.3f}')
        col3.metric("OOS Trades", int(s.get("oos_trades", 0)))

    st.markdown("---")

    # Leaderboards
    st.subheader("Leaderboards")
    c1, c2 = st.columns(2)

    lb_is = gdir / "leaderboard_is.csv"
    lb_oos = gdir / "leaderboard_oos.csv"

    if lb_is.exists():
        c1.write("**IS Leaderboard**")
        df_is = pd.read_csv(lb_is)
        c1.dataframe(df_is, width='stretch')

    if lb_oos.exists():
        c2.write("**OOS Leaderboard**")
        df_oos = pd.read_csv(lb_oos)
        c2.dataframe(df_oos, width='stretch')

    st.markdown("---")

    # Rows detail with origin labels
    st.subheader("Results Detail (with Origin Labels)")
    rows_with_origin = gdir / "rows_with_origin.csv"
    rows_csv = gdir / "rows.csv"

    if rows_with_origin.exists():
        df = pd.read_csv(rows_with_origin)
        st.dataframe(df, width='stretch')

        # Filter by origin
        if "origin_label" in df.columns:
            origins = df["origin_label"].unique().tolist()
            selected_origin = st.selectbox("Filter by Origin", ["All"] + origins)
            if selected_origin != "All":
                df_filtered = df[df["origin_label"] == selected_origin]
                st.dataframe(df_filtered, width='stretch')
    elif rows_csv.exists():
        df = pd.read_csv(rows_csv)
        st.dataframe(df, width='stretch')
    else:
        st.info("No rows.csv or rows_with_origin.csv found")

    st.markdown("---")

    # Equity and Drawdown curves
    st.subheader("OOS Curves")
    eq_png = gdir / "oos_equity.png"
    dd_png = gdir / "oos_drawdown.png"

    c1, c2 = st.columns(2)
    if eq_png.exists():
        c1.image(str(eq_png), caption="OOS Equity Curves", use_column_width=True)
    if dd_png.exists():
        c2.image(str(dd_png), caption="OOS Drawdown Curves", use_column_width=True)

    st.markdown("---")

    # Contribution chart (LLM/ML)
    contrib_png = gdir / "proposer_contrib.png"
    if contrib_png.exists():
        st.subheader("LLM / ML Contribution")
        st.image(str(contrib_png), caption="Proposer Contribution by Phase")

    # Heatmaps
    heatmaps = list(gdir.glob("is_heatmap_*.png"))
    if heatmaps:
        st.subheader("IS Parameter Heatmaps")
        for p in heatmaps:
            st.image(str(p), caption=p.name)

    st.markdown("---")

    # Champion parameters
    best_json = gdir / "best_params_by_fold.json"
    if best_json.exists():
        st.subheader("Champion Parameters (by Fold)")
        best_data = load_json(best_json)
        st.json(best_data)

    # Generation comparison (if multiple generations)
    if len(gens) > 1:
        st.markdown("---")
        st.subheader("Cross-Generation Comparison")

        # Load all generations
        gen_data = []
        for g in gens:
            g_path = Path(out_dir) / g / "gen_summary.json"
            if g_path.exists():
                g_sum = load_json(g_path)
                oos = g_sum.get("oos_summary", {})
                gen_data.append({
                    "Generation": g,
                    "Mode": g_sum.get("mode", "unknown"),
                    "OOS Sharpe": oos.get("oos_avg_sharpe", 0),
                    "OOS Total Return": oos.get("oos_avg_total_return", 0),
                    "OOS Max DD": oos.get("oos_avg_max_drawdown", 0),
                    "OOS Trades": oos.get("oos_trades", 0),
                })

        df_comp = pd.DataFrame(gen_data)
        st.dataframe(df_comp, width='stretch')

        # Plot Sharpe progression
        fig, ax = plt.subplots(figsize=(10, 4))
        ax.plot(df_comp["Generation"], df_comp["OOS Sharpe"], marker="o")
        ax.set_xlabel("Generation")
        ax.set_ylabel("OOS Sharpe")
        ax.set_title("OOS Sharpe Progression")
        ax.grid(True, alpha=0.3)
        st.pyplot(fig)


if __name__ == "__main__":
    main()
