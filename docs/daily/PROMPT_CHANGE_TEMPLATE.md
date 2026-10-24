# Prompt 变更模板（仅战术/元认知层）

> 原则：只改战术/元认知层；每次变更必须附“触发证据/适用状态/回退条件”，可 A/B 小权重试点。

---

## 1) 变更基本信息
- 日期：YYYY-MM-DD（ET）
- 提交人：____
- 目标会话：PRE | RTH | POST | OVN
- 目标标的/组合：____
- 目标 provider/model：deepseek_nvidia | deepseek | openai | gemini
- 涉及文件：`llm_trader/config/prompts.yaml`（或相关 Prompt 构建器）

---

## 2) 变更内容（战术/元认知）
- 战术规则改动（示例）：
  - 提高 RVOL 门槛至 ____（分位/绝对值）
  - 开盘前禁止新仓：保持 `--forbid-new-positions-before` = ____
  - 高波动期收紧止损：____
- 运行节奏旋钮：
  - `--provider` = ____，`--interval` = ____ 秒，`--cycles` = ____
  - `--start-trading-at`、`--stop-trading-at`、`--close-at`、`--forbid-new-positions-before` = ____

（附 diff 片段/摘要，指向 git 变更）

---

## 3) 触发证据（必须）
- 复盘结果/图表：Top/Bottom 决策样本、理由与走势对比
- 一致性分异常场景：____（为何当前 Prompt 失配）
- 数据与执行约束：spread/成本、VWAP 偏离、滑点/延迟情况

参考导出：`cd docs/daily && ./evening_review.sh`（生成 `llm_trader/results/decisions_YYYYMMDD.csv`）

---

## 4) 适用状态
- 市场状态：高/低波、事件日、盘前跳空、行业异动 等
- 会话与时间窗：____

---

## 5) A/B 执行计划（小权重）
- 初始权重：Challenger ____（建议≤30%）
- 评估窗口：____（如 1–3 个交易日/第一小时等）
- 评估指标：E、Sharpe/Sortino、MDD、PF、HitRate、滑点/延迟、一致性分

---

## 6) 回退条件（必须）
- 账户级日内止损/滚动 MDD 触发
- 执行质量异常（滑点/延迟/拒单）
- 一致性分跌破阈值（< ____）

动作：立即回滚 Champion（停止新仓/清理旧仓），恢复原 Prompt。

---

## 7) 预期影响与监控
- 预期收益-风险变化：____
- 重点监控 SQL/命令：
```
clickhouse-client --query "SELECT formatDateTime(ts,'%H:%M:%S','America/New_York') t,symbol,provider,decision,confidence,pnl FROM llm_trader.decisions WHERE toDate(ts)=today() ORDER BY ts DESC LIMIT 20 FORMAT Pretty"
tail -f llm_trader/logs/llm_trader_$(date +%Y%m%d).log
```

---

## 8) 生效与复核
- 生效窗口：起 ____  至 ____
- 复核时间：盘中/日终/周终 ____
- 结论：保留 | 收紧 | 回滚（附原因）

