# scripts/ai_trader/monitoring_dashboard.py
import streamlit as st
import pandas as pd
import json
from pathlib import Path
import datetime
try:
    import mlflow  # type: ignore
except Exception:  # pragma: no cover
    mlflow = None
from typing import List, Dict, Any, Optional
from scripts.ai_trader.health_snapshot import collect_health_snapshot

# --- 页面配置 ---
st.set_page_config(
    page_title="AI Trader - Unified Monitoring Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- 核心辅助函数 ---

def get_latest_run_dir() -> Optional[Path]:
    """查找最新的 runs/ai_trader/YYYY-MM-DD 目录"""
    base_dir = Path("runs/ai_trader")
    if not base_dir.exists():
        return None
    
    date_dirs = [d for d in base_dir.iterdir() if d.is_dir() and d.name.startswith('20')]
    if not date_dirs:
        return None
    
    return max(date_dirs)

def load_json_artifact(run_dir: Path, filename: str) -> Optional[Dict[str, Any]]:
    """安全地加载一个JSON文件"""
    file_path = run_dir / filename
    if file_path.exists():
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {"error": f"Failed to load or parse {filename}"}
    return None

def get_live_processes(run_dir: Path) -> pd.DataFrame:
    """扫描procs和hb目录，获取实时进程状态"""
    procs_dir = run_dir / "procs"
    
    if not procs_dir.exists():
        return pd.DataFrame()

    process_data = []
    for proc_file in procs_dir.glob("llm_*.json"):
        try:
            meta = json.loads(proc_file.read_text("utf-8"))
            
            # 检查心跳
            hb_status = "N/A"
            if meta.get("heartbeat_file"):
                hb_path = Path(meta["heartbeat_file"])
                if hb_path.exists():
                    hb_data = json.loads(hb_path.read_text("utf-8"))
                    last_hb_time = datetime.datetime.fromisoformat(hb_data.get("ts").replace("Z", "+00:00"))
                    time_diff_sec = (datetime.datetime.now(datetime.timezone.utc) - last_hb_time).total_seconds()
                    
                    interval = meta.get("interval", 600)
                    if time_diff_sec > interval * 2.5:
                        hb_status = f"🔴 Stale ({time_diff_sec:.0f}s)"
                    else:
                        hb_status = f"🟢 OK ({time_diff_sec:.0f}s ago)"
                else:
                    hb_status = "⚪ File Missing"

            process_data.append({
                "PID": meta.get("pid"),
                "Session": meta.get("session"),
                "Provider": meta.get("provider"),
                "Symbols": ", ".join(meta.get("symbols", [])),
                "Interval": meta.get("interval"),
                "Status": meta.get("status"),
                "Heartbeat": hb_status,
                "Started At (UTC)": meta.get("started_at", "").split('.')[0],
            })
        except Exception:
            continue
            
    return pd.DataFrame(process_data)

def get_mlflow_summary(experiment_name: str = "ai_trader_prod") -> Optional[Dict[str, Any]]:
    """从MLflow获取最新一次 'learn' session 的运行摘要"""
    if mlflow is None:
        return {"error": "mlflow_not_installed"}
    try:
        # 您可能需要设置 tracking_uri
        # mlflow.set_tracking_uri("http://localhost:5000")
        mlflow.set_experiment(experiment_name)
        runs = mlflow.search_runs(
            filter_string="tags.session = 'learn'",
            order_by=["start_time DESC"],
            max_results=1
        )
        if not runs.empty:
            latest_run = runs.iloc[0]
            return {
                "run_name": latest_run.get("tags.mlflow.runName", "N/A"),
                "start_time": pd.to_datetime(latest_run.start_time).strftime('%Y-%m-%d %H:%M:%S'),
                "oos_sharpe": latest_run.get("metrics.oos_sharpe", 0),
                "oos_mdd": latest_run.get("metrics.oos_mdd", 0),
                "pending_count": latest_run.get("metrics.pending_count", 0),
                "run_id": latest_run.run_id
            }
    except Exception as e:
        return {"error": str(e)}
    return None

# --- 仪表盘主渲染函数 ---

def render_dashboard():
    st.title("🤖 AI Trader - 统一运维监控仪表盘")
    
    latest_run_dir = get_latest_run_dir()
    
    if not latest_run_dir:
        st.error("错误：找不到任何运行目录 `runs/ai_trader/`。请先运行一次系统。")
        return

    st.info(f"**当前观测目录**: `{latest_run_dir}`")

    # --- 1. 大脑决策看板 ---
    st.header("🧠 AI 大脑决策看板")
    
    col1, col2 = st.columns(2)
    with col1:
        with st.container(border=True):
            st.subheader("晨间计划")
            morning_plan = load_json_artifact(latest_run_dir, "brain_plan_morning.json")
            st.json(morning_plan if morning_plan else "尚未生成")

    with col2:
        with st.container(border=True):
            st.subheader("关键决策审批")
            hpo_decision = load_json_artifact(latest_run_dir, "hpo_decision.json")
            st.json(hpo_decision if hpo_decision else "尚未生成")

    with st.expander("点击查看大脑决策的参考材料 (新闻)"):
        news_context = load_json_artifact(latest_run_dir, "news_context.json")
        st.json(news_context if news_context else "尚未生成")

    # --- 2. 实时进程状态 ---
    st.header("🚀 实时进程状态")
    process_df = get_live_processes(latest_run_dir)
    if not process_df.empty:
        st.dataframe(process_df, width='stretch')
    else:
        st.info("当前没有正在运行的 `llm_trader` 进程。")

    # --- 3. 核心业绩与学习摘要 ---
    st.header("📈 核心业绩与学习摘要 (MLflow)")
    mlflow_summary = get_mlflow_summary()

    if mlflow_summary:
        if "error" in mlflow_summary:
            st.warning(f"无法连接到 MLflow 或缺少依赖: {mlflow_summary.get('error')}")
        else:
            col1, col2, col3 = st.columns(3)
            col1.metric("最新OOS Sharpe", f"{mlflow_summary.get('oos_sharpe', 0):.3f}")
            col2.metric("最新OOS Max Drawdown", f"{mlflow_summary.get('oos_mdd', 0)*100:.2f}%")
            col3.metric("待处理行动", f"{mlflow_summary.get('pending_count', 0):.0f}")
            st.caption(f"数据来自 MLflow 运行: `{mlflow_summary.get('run_name')}`")
    else:
        st.info("暂未找到 MLflow 运行记录。")

    # --- 4. 今日战报 ---
    st.header("📋 今日战报")
    report_path = latest_run_dir / "daily_report.md"
    if report_path.exists():
        st.markdown(report_path.read_text("utf-8"))
    else:
        st.info("今日战报尚未生成。")

    # --- 5. 对冲执行状态 ---
    st.header("🛡️ 对冲执行状态")
    hedge_audit_path = latest_run_dir / "hedge_audit.jsonl"

    if hedge_audit_path.exists():
        try:
            # Parse hedge audit log
            hedge_records = []
            with open(hedge_audit_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        hedge_records.append(json.loads(line))

            if hedge_records:
                # Calculate statistics
                total_triggers = len(hedge_records)
                successful = sum(1 for r in hedge_records if r.get("result", {}).get("success"))
                success_rate = successful / total_triggers if total_triggers > 0 else 0.0
                total_cost = sum(r.get("result", {}).get("total_cost", 0) for r in hedge_records)

                effectiveness_vals = [
                    r.get("result", {}).get("hedge_effectiveness", 0)
                    for r in hedge_records
                    if r.get("result", {}).get("hedge_effectiveness") is not None
                ]
                avg_effectiveness = sum(effectiveness_vals) / len(effectiveness_vals) if effectiveness_vals else 0.0

                # Display metrics
                col1, col2, col3, col4 = st.columns(4)

                with col1:
                    st.metric(
                        "今日对冲触发次数",
                        total_triggers,
                        help="今日执行对冲的次数"
                    )

                with col2:
                    st.metric(
                        "对冲成功率",
                        f"{success_rate*100:.1f}%",
                        help="对冲执行成功比例"
                    )

                with col3:
                    st.metric(
                        "今日对冲成本",
                        f"${total_cost:,.2f}",
                        help="今日对冲总成本（期权权利金或做空成本）"
                    )

                with col4:
                    st.metric(
                        "平均对冲有效性",
                        f"{avg_effectiveness*100:.1f}%",
                        help="对冲头寸预期对冲效果（基于相关性和delta）"
                    )

                # Show recent hedges
                with st.expander("查看最近对冲历史"):
                    recent_df = pd.DataFrame([
                        {
                            "时间": r.get("ts", "").split("T")[-1][:8],
                            "标的": ", ".join(r.get("intent", {}).get("symbols", [])),
                            "对冲比例": f"{r.get('intent', {}).get('notional_pct', 0)*100:.0f}%",
                            "策略": r.get("intent", {}).get("strategy", ""),
                            "紧急度": r.get("intent", {}).get("urgency", ""),
                            "成功": "✅" if r.get("result", {}).get("success") else "❌",
                            "成本": f"${r.get('result', {}).get('total_cost', 0):.2f}",
                            "有效性": f"{(r.get('result', {}).get('hedge_effectiveness', 0)*100):.1f}%" if r.get('result', {}).get('hedge_effectiveness') else "N/A",
                        }
                        for r in hedge_records[-10:]  # Last 10 records
                    ])
                    st.dataframe(recent_df, width='stretch')

                # Show failures
                failed_hedges = [r for r in hedge_records if not r.get("result", {}).get("success")]
                if failed_hedges:
                    with st.expander(f"⚠️ 失败的对冲 ({len(failed_hedges)})"):
                        failed_df = pd.DataFrame([
                            {
                                "时间": r.get("ts", "").split("T")[-1][:8],
                                "标的": ", ".join(r.get("intent", {}).get("symbols", [])),
                                "错误": r.get("result", {}).get("error", "未知错误")[:50],
                            }
                            for r in failed_hedges
                        ])
                        st.dataframe(failed_df, width='stretch')
            else:
                st.info("今日尚无对冲执行记录。")
        except Exception as exc:
            st.warning(f"无法解析对冲审计日志: {exc}")
    else:
        st.info("尚无对冲执行数据。")

    # --- 6. 健康 / 可观测性 ---
    st.header("🩺 健康 / 可观测性")
    try:
        snapshot = collect_health_snapshot(days=7)
        sleep = snapshot.get("sleep_loop", {})
        vstore = snapshot.get("vector_store", {})
        tools = snapshot.get("tool_calls", {})
        council = snapshot.get("strategy_council", {})
        evolution = snapshot.get("auto_evolution", {})
        skill_flow = snapshot.get("skill_flow", {})
        hedge_health = snapshot.get("hedge_execution", {})
        anomalies = snapshot.get("anomalies", {})
        performance = snapshot.get("performance", {})

        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Sleep Loop 成功率(7d)", f"{(sleep.get('success_rate') or 0)*100:.1f}%", help=str(sleep))
            st.metric("向量库文档数", vstore.get("count", 0))
        with col2:
            st.metric("Tool Call 成功率", f"{(tools.get('success_rate') or 0)*100:.1f}% ({tools.get('total',0)} runs)")
            st.metric("策略委员会采纳率", f"{(council.get('adoption_rate') or 0)*100:.1f}%")
        with col3:
            pnl_delta = evolution.get("pnl_delta")
            pnl_label = f"{pnl_delta:+.4f}" if pnl_delta is not None else "n/a"
            st.metric("Auto-evolution ΔEV", pnl_label)
            st.metric(f"异常/免疫事件({snapshot.get('window_days',7)}d)", anomalies.get("recent", 0))

        st.subheader("📊 收益 / Sharpe vs 基准")
        pcol1, pcol2, pcol3 = st.columns(3)
        sharpe = performance.get("sharpe")
        bench_sharpe = performance.get("bench_sharpe")
        pcol1.metric("策略 Sharpe", f"{sharpe:.2f}" if sharpe is not None else "n/a")
        pcol2.metric("基准 Sharpe", f"{bench_sharpe:.2f}" if bench_sharpe is not None else "n/a")
        excess = performance.get("excess_sharpe")
        excess_label = f"{excess:+.2f}" if excess is not None else "n/a"
        pcol3.metric("超额 Sharpe", excess_label, help=f"status={performance.get('status','n/a')}, days={performance.get('days','?')}")

        st.subheader("🧩 技能流水线")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("已启用技能", skill_flow.get("enabled_skills", 0))
        c2.metric("审批通过(7d)", skill_flow.get("approvals", 0))
        approval_rate = skill_flow.get("approval_rate", 0.0)
        c3.metric("审批通过率(7d)", f"{approval_rate*100:.1f}%")
        cov = skill_flow.get("mean_coverage")
        cov_label = f"{cov:.1f}%" if cov is not None else "n/a"
        c4.metric("平均覆盖率(通过)", cov_label)

        rejection_reasons = skill_flow.get("rejection_reasons", {})
        if rejection_reasons:
            with st.expander("查看主要拒绝原因"):
                st.table(pd.DataFrame(rejection_reasons.items(), columns=['Reason', 'Count']))


        with st.expander("展开查看完整快照 JSON"):
            st.json(snapshot)
    except Exception as exc:
        st.warning(f"无法生成健康快照: {exc}")

# --- 主程序入口 ---
if __name__ == "__main__":
    render_dashboard()
