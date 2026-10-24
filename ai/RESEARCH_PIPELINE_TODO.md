# Research & Self-Evolution TODO (Current Gaps vs Blueprint v3)

## Status Snapshot
- ✅ 已接入：ResearchAgent/Orchestrator（健康过滤）、mutable_targets_snapshot 健康度、DataReadinessChecker、WindowManager、sync_features_active、调度/预检/诊断窗口一致、active 同步事件化。
- ✅ 配置落地：configs/research_policy.json；ResearchState 基础快照（策略占位、预算估算、immune/mutable 读取）。
- ✅ 列名与窗口：预检统一使用 ts 列，报告含窗口；诊断/调度窗口 override 贯穿。

## P0（立即可做）
1) 调度前数据防护  
   - `_execute_oof`：调用 DataReadinessChecker，若 `status=needs_window_adjust` 写入 `start_override/end_override/window_days`，并在 ml.job 事件中记录最终窗口。  
   - nightly_oof_flow/诊断：统一使用 override 窗口；诊断 metadata/事件写 start/end。  
   - WindowManager 缩窗（低利用率→30d）接入 Brain/调度输出并记录 clamp 事件。
2) Active 同步闭环  
   - health=sync_or_fallback 或 active 滞后/空：事件化调用 `tools/sync_features_active.py`（started/completed/failed），再决定回退 baseline。  
   - 启用 cron：`tools/cron/generate_mutable_targets_snapshot.sh` 定期刷新 health_score，调度每次 _execute_oof 读取最新快照。

## P1（两周内）
3) ResearchPolicy/State 深度集成  
   - Sleep/Immune loop：写 immune_cases、预检/诊断结果到 memory，刷新 ResearchState（用 ClickHouse/metrics 真实策略/预算替换占位）。  
   - policy_patch_candidates 持久化为待审核列表，供人工/自动应用。
4) 自编程闭环强化  
   - run_code_patch_experiment 读取 target_health：健康差先触发 sync/回退再跑 patch；事件补充 health/sync_action。  
   - health_score < 阈值的目标生成同步/回退事件到 ml_events，便于审计。
5) 数据质量 & RTH 日检  
   - 每日覆盖率/重复率/RTH 异常检测 → JSON/事件；训练前读取标记阻断 bad symbol。  
   - 将检查结果合并到 mutable_targets 健康/immune_cases，驱动调度与 ResearchAgent。

## P2（一月内）
6) Prompt/决策优化  
   - Brain prompt：明确“先数据对齐/缩窗，再 HPO/特征”，输出决策理由/健康得分并日志化。  
   - ResearchAgent：强制使用 health_score/recommendation 决策；命中 sync_or_fallback 先修复再排实验。
7) 可观测性与周报  
   - 看板/告警：同步/回退/窗口缩减/训练失败等事件可视化。  
   - 周报：训练成功率、同步成功率、top 失败原因、数据健康（RTH/覆盖率/重复率）汇总。

## 验收标准
- 训练前 100% 经 DataReadiness/Window 检查，utilization=0 空跑消除。  
- active 同步成功率 >95%，不健康自动回退 baseline，并有事件记录。  
- 自编程对低健康目标执行“先修复后 patch”，事件齐全。  
- 数据质量/RTH 异常可阻断训练，假阳率 <10%。  
- ResearchAgent 读取真实策略/预算/immune/mutable 状态，基于 policy 输出实验与修复建议。 
