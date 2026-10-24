# 每日 Scorecard（样例，ET：2025-10-28）

> 统一 run_system 与 llm_trader 的评价口径；指标以成本/滑点后净值为准。
> 下面包含可直接运行的 ClickHouse 查询；如环境未就绪，请先执行三道门。

---

## 1) 元信息
- 会话覆盖：PRE（04:00–09:30）/ RTH（09:30–16:00）/ POST（16:00–20:00）/ OVN（20:00–04:00+1）
- 版本：
  - llm_trader：provider=deepseek_nvidia（premarket/overnight），deepseek（first hour），参数见 Action Plan
  - run_system：EnhancedModelSignalStrategy（`config.yml`，shadow_mode 观察）

---

## 2) llm_trader 指标（当日，成本后）

运行：
```
# 全日聚合（执行单）
clickhouse-client --query "
SELECT countIf(executed=1) trades,
       round(sum(pnl),2) total_pnl,
       round(avg(pnl),2) avg_pnl,
       round(sum(pnl>0)*100.0/nullIf(countIf(executed=1),0),2) win_rate
FROM llm_trader.decisions
WHERE toDate(ts)=today() AND executed=1
FORMAT Pretty"

# 按 provider（A/B 或多模型对比）
clickhouse-client --query "
SELECT provider,
       countIf(executed=1) trades,
       round(sum(pnl),2) pnl,
       round(sum(pnl>0)*100.0/nullIf(countIf(executed=1),0),2) win_rate
FROM llm_trader.decisions
WHERE toDate(ts)=today()
GROUP BY provider
ORDER BY pnl DESC
FORMAT Pretty"

# Top/Bottom 决策（质检与一致性抽查）
clickhouse-client --query "
SELECT formatDateTime(ts,'%H:%M:%S','America/New_York') t,symbol,provider,decision,round(pnl,2) pnl,substring(reasoning,1,60) reason
FROM llm_trader.decisions
WHERE toDate(ts)=today() AND executed=1
ORDER BY pnl DESC LIMIT 5 FORMAT Pretty" 

clickhouse-client --query "
SELECT formatDateTime(ts,'%H:%M:%S','America/New_York') t,symbol,provider,decision,round(pnl,2) pnl,substring(reasoning,1,60) reason
FROM llm_trader.decisions
WHERE toDate(ts)=today() AND executed=1
ORDER BY pnl ASC LIMIT 5 FORMAT Pretty"
```

示例输出（样例）：
- 全日：trades=18，total_pnl=$342.50，avg_pnl=$19.03，win_rate=61.11%
- provider：deepseek_nvidia pnl=$280.40（win_rate 62%）；deepseek pnl=$62.10（win_rate 60%）

请在复盘中记录“推理一致性分”（理据与走势/VWAP/RVOL/成本一致性），达标阈值例如≥0.6。

---

## 3) run_system 指标（Alpha Loop/回测/HPO）

数据来源：
- `alpha_out/<run>/gen_*/gen_summary.json`（Sharpe/Sortino/MDD/OOS 汇总）
- `alpha_out/<run>/gen_*/rows.csv`（trial 级明细）

建议核对：
- Stage2（Optuna）OOS：Sharpe ↑、MDD ↓（或不劣）、Trades 足够
- 按会话/波动切片（PRE/RTH/POST/OVN；高/低波）

示例进度检查：
```
tail -f alpha_out/coop_optuna_tsla/alpha_loop.log | grep -E "Gen|Trial|Best"
cat alpha_out/coop_optuna_tsla/gen_03/gen_summary.json | jq '.stage2 | {oos_sharpe, oos_avg_max_drawdown, best_params}'
```

示例结论（样例）：
- Stage2 OOS：Sharpe=1.32，MDD=0.08，Trades=24（优于基线）
- 满足晋升为 Challenger 条件：E>0；Sharpe/Sortino 提升；MDD 不劣

---

## 4) 稳健性切片（必须）
- PRE/RTH/POST/OVN 各自的 win_rate、avg_pnl、total_pnl（llm_trader）
- 高/低波分组（按 RVOL/ATR 分位）后的 Sharpe/MDD（run_system）

查询示例（按小时段近似 PRE/RTH/POST）：
```
clickhouse-client --query "
SELECT toHour(ts) h, countIf(executed=1) trades, round(sum(pnl),2) pnl
FROM llm_trader.decisions WHERE toDate(ts)=today()
GROUP BY h ORDER BY h FORMAT Pretty"
```

---

## 5) 对比与门槛判断
- 基线（Champion） vs 候选（Challenger）：
  - Sharpe ↑ / Sortino ↑ / MDD ↓（或不劣）
  - 执行质量（滑点/延迟/拒单）在健康分位
  - LLM：一致性分 ≥ 阈值

结论（样例）：
- llm_trader（盘前）：保持当前 provider=deepseek_nvidia；Prompt 收紧开盘前新仓规则（保留）
- run_system（TSLA 模型）：提升为 30% Challenger，进入 A/B（见下一节）

---

## 6) A/B 决策与回滚条件
- 今日权重：Champion 70% / Challenger 30%
- 回滚条件（即时）：账户级日内止损/MDD 触发；执行质量异常；健康门转 `error`
- 晋升条件：连续评估窗口内（如 3 个交易日）指标达成且稳定

参考模板：`docs/daily/AB_ROLLOUT_TEMPLATE.md`

---

## 7) 最终结论与行动
- 参数/Prompt 变更：按 `docs/daily/PROMPT_CHANGE_TEMPLATE.md` 留痕
- 下一步：继续 A/B；盘后复盘持续记录一致性与执行质量；必要时扩大/收紧权重

