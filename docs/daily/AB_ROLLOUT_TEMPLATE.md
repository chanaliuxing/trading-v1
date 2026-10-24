# A/B Rollout 模板（Champion–Challenger，可回滚）

> 目标：以 70/30 等小权重推进 Challenger；门禁与回滚条件清晰，自动化与人工相结合。

---

## 1) 基本信息
- 日期：YYYY-MM-DD（ET）
- 对象：run_system | llm_trader
- 会话：PRE | RTH | POST | OVN
- Champion 版本：____（策略/Prompt/参数，commit/文件）
- Challenger 版本：____（策略/Prompt/参数，commit/文件）

---

## 2) 权重与路由
- 默认权重：Champion 70% / Challenger 30%（可 ≤30% 起步）
- 路由模式（如执行分流）：
  - shadow | canary | prod
  - canary_ratio：____（例 0.3）
  - 关联能力：`broker/router.py` 支持 shadow/canary/prod（无需改动引擎即可约定执行）

---

## 3) 事前门禁（必须通过）
- 健康门：`cd docs/daily && ./system_health_check.py` → `logs/system_health.json` 为 `healthy`
- 数据门：近10分钟 Tick 存在；昨日日线 Bar≥390
- 执行门：`python temp/check_ibkr_status.py` 正常；关键进程 OK

不通过则仅排障/回补，禁止推进。

---

## 4) 评估窗口与指标
- 窗口：____（如第一小时/全天/3个交易日）
- 指标（成本后）：E、Sharpe、Sortino、MDD、PF、HitRate、滑点 p95、延迟 p95、拒单率
- LLM 专属：推理一致性分

查询示例：
```
clickhouse-client --query "SELECT provider,countIf(executed=1) trades,round(sum(pnl),2) pnl FROM llm_trader.decisions WHERE toDate(ts)=today() GROUP BY provider ORDER BY pnl DESC FORMAT Pretty"
```

---

## 5) 回滚条件（即时执行）
- 账户级：日内止损预算/滚动 MDD 触发
- 执行质量：滑点/延迟超阈或拒单异常
- 健康门转 `error`

动作：
- 立刻回滚至 Champion（或 Shadow），停止新仓并清理旧仓；记录原因与时间。

---

## 6) 晋升条件（Challenger → Champion）
- E>0 且 Sharpe/Sortino 提升，MDD 下降或不劣
- 执行质量达标（滑点/延迟/拒单率处于健康分位）
- LLM：一致性分≥阈值

动作：
- 提升权重（例 30%→50%→70%→100%），每次提升前均重走门禁；留存对比截图与指标表。

---

## 7) 执行步骤
1) 事前门禁 & 记录：`logs/system_health.json` 快照（附 `jq` 片段）
2) 设置权重/路由（如使用 canary）：canary_ratio=____
3) 启动/调整命令（示例）：
```
# 盘前（Challenger 小权重）
cd llm_trader && python run_single_bot.py --provider deepseek_nvidia --symbols TSLA --interval 600 --cycles 33 --close-at "09:35" --forbid-new-positions-before 9 --wait-for-premarket
```
4) 盘中监控（每 30–60 秒）：指标/日志/CH 查询
5) 告警触发则回滚（执行“回滚条件”动作）
6) 窗口结束：对比 Scorecard → 决策“提升/保持/回滚”

---

## 8) 留痕清单
- 健康门报告快照（`logs/system_health.json`）
- 权重/路由设置与时间点
- 运行命令与参数（Action Plan）
- 指标截图/表格（Champion vs Challenger）
- 回滚/晋升原因与时间（可复核）

