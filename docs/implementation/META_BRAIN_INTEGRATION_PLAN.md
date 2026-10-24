# “Meta 大脑”自主进化系统 — 最终集成落地计划

核心目标：打通学习→合成→验证→上线→应用，形成可观测、可治理、可回滚的自进化闭环，消除手动依赖并内联安全闸。

## 先决条件
- 稳定的回放/影子环境，用于技能回测与安全验证。
- 明确的性能指标（Sharpe、IC、PF、回撤、交易数）与覆盖率门槛（默认 90%）。
- 配置一致：`config.yml` 的 `features.v3_autonomy.*` 与 `configs/research_policy.json` 的治理规则对齐。

## 阶段 1：自动化调度与触发（状态：已落地，待稳定化）
- 核心循环驱动研究：在 `scripts/ai_trader/auto_loop.py` 的 afterhours/overnight 分支调用 `run_research_cycle_once()`，研究/自进化由主循环触发。
- 定时兜底：用 systemd Timer（或 cron 兜底）在低峰（如 00:30）补发一次 `run_research_cycle_once()`，防止漏触发。
- 专用复盘：为 `scripts/ai_trader/memory/sleep_loop.py` 配置每日 EOD+30min 定时器，生成 lessons/skills 到 `runs/ai_trader`。
- 每日锁：sleep_loop/研究触发均使用 `runs/YYYY-MM-DD/locks/*.lock`，确保每日一次。

## 阶段 2：技能流水线（生成→验证→审批）（状态：部分落地）
- Evolution Chamber（新增 `libs/evolution/evolution_chamber.py`）：拉取 `skill_approval_queue.jsonl`，生成/加载代码与测试（模板或 LLM），在隔离环境跑 `pytest`+`ruff`，调用 `governance.py` 检查 forbidden/allowed、配额、覆盖率≥90%，并做小样本回测/影子验证；结果写 `runs/evolution/results/<skill_id>.json`（通过/失败/覆盖率/拒绝原因）。
- SkillApprover 守护（新增 `scripts/ai_trader/evolvers/skill_approver.py`）：监听审批队列，调用 Evolution Chamber，记录审计。
- 自动审批：扩展 `scripts/admin/review_skills.py` 支持 `--auto-approve`，对通过验证的技能执行上线动作。
- SkillRegistry：新增 `config/skill_registry.json`（或 `runs/memory/skill_registry.json`），记录技能 ID/路径/版本/性能基线/`enabled` 状态。

## 阶段 3：交易大脑动态加载与应用（状态：部分落地）
- 加载器：在交易入口（如 `scripts/ai_trader/orchestrator.py`）读取 SkillRegistry，`importlib` 动态加载 `libs/skills/generated/*` 中 `enabled: true` 的技能。
- 决策链注入：在决策/风控/执行（如 `LLMTraderController`、deciders）预留插件位，将动态技能用作特征生成、信号过滤、风险调节。
- 安全开关：技能级 enable/disable，默认 shadow 或小流量 canary；异常或指标恶化自动禁用并写审计。

## 阶段 4：可观测性与安全审计（状态：部分落地）
- 生命周期日志：在生成/验证/审批/上线/禁用节点追加到 `runs/ai_trader/<date>/skill_flow.jsonl`。
- 监控面板：扩展 `scripts/ai_trader/monitoring_dashboard.py`/Grafana，新增卡片：学习（sleep_loop 最近运行、lessons 数）、进化（待审/已审、覆盖率、拒绝原因 TopN）、技能库（激活列表/版本/调用频次）、应用（调用次数/失败率/性能快照）。
- 健康检查：在 `health_snapshot.py` 统计 sleep_loop、`run_research_cycle_once` 成功/失败/耗时；连续失败、治理拒绝或性能衰退触发告警（Slack/邮件）。

## 阶段 5：治理与安全（状态：已落地，需对齐目录/配额）
- 配置一致性：启用 `features.v3_autonomy`，并对齐 `research_policy.json` 的 allowed/forbidden/max_patches_per_day 与生成目录。
- 质量闸：Evolution Chamber 强制覆盖率/配额/禁区目录，拒绝原因可追溯。
- 性能熔断：上线技能持续监测 P&L/Sharpe/交易数，触发阈值自动下线并写治理日志。

## 阶段 6：测试与验收（状态：未落地）
- 单元：Evolution Chamber、SkillRegistry/SkillLibrary、治理 gate（允许/拒绝）、技能加载器（含异常/回滚）。
- 集成：夜间 e2e dry-run：sleep_loop → skill_approver → evolution_chamber → registry → 交易侧加载（shadow）→ 审计日志。
- 验收：纸面/仿真模式跑 2–3 天，确认定时触发稳定、注册表生成、治理拒绝生效、交易日志出现技能调用且可回滚。

## 后续高级功能（状态：未落地）
1) 回测/影子作为激活门槛：审批后默认不启用，先跑回测/影子，超过基线再设 `enabled:true`。  
2) Canary + 最小变更策略：LLM 非模板代码默认 10% 流量 canary，并提供快速回滚钩子。  
3) 性能反馈与淘汰：周期性写回技能表现（P&L/Sharpe/命中率），夜间循环自动降权/淘汰低效技能。  
4) 免疫机制：连续 N 次交易失败或核心风控触发时，冻结近期上线技能并触发数据/特征自愈检查。  
5) 告警接入：核心指标（触发次数、阻断原因、覆盖率分布、活跃技能数）接入 Prometheus/Grafana 告警。
