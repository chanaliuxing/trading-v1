AI Brain Prompt 改进 TODO（v2025-11-07）
=====================================

目的
 - 将“诊断强、行动弱”的现状，升级为“可解释、可控、可回退”的审查官式决策流。
 - 引入上下文记忆、数据质量检查、窗口非线性认知与防循环机制，减少无效扩窗与重复 queue_job。

背景摘要
 - 现状：能正确识别 AUC≈0.5/IC≈0 的模型质量问题，但动作集中在 extend_window 与 queue_job，缺少记忆、数据质量维度与保守选项。
 - 症状：n_bars≈35k 时仍建议扩窗，导致“扩展→失败→再扩展”的循环；对历史建议效果无记忆；无 wait/do_nothing。

一、Prompt 必需输入（若缺失需先请求/返回 wait）
 - per-symbol 指标：auc, ic, top_decile_ret_bps, mean_conf, conf_floor, sharpe_gate, pf_gate, n_bars, window_days
 - recent_actions（近3次）：[{action, params, before_metrics, after_metrics, success}]
 - data_quality：{missing_rate, zero_price_count, extreme_return_count, timestamp_gap_count, feature_const_rate, label_leak_suspect}
 - job_outcomes：最近作业失败原因/日志标签
 - 约束：严格只引用输入里的数字；不得猜测缺失指标。

二、动作集合与频控
 - 允许动作：extend_window, reduce_window, queue_job(alpha_loop), backfill, data_quality_check, wait, do_nothing, escalate
 - 频率限制：同一动作在近3轮最多1次；除非上次“显著改善”（AUC↑≥0.03 或 IC↑≥0.01）。
 - 反复失败禁令：最近两次相同动作均无改善 → 本轮禁止该动作，转而 data_quality_check 或 wait。

三、决策硬规则（优先级：数据质量 > 大窗口毒化 > 盈利门槛）
 1) 数据质量红旗优先：
    - 若 zero_price_count>0 或 extreme_return_count 异常增多，或 timestamp_gap/feature_const_rate>5%，或 label_leak_suspect=true → 先 data_quality_check 或 backfill，暂缓窗口/超参类操作。
 2) 大窗口毒化：
    - 若 n_bars ≥ 25,000 且 ic < 0.02 → 优先 reduce_window，window_days ∈ {30, 60}；禁止 extend_window。
 3) 小样本不稳：
    - 若 n_bars ≤ 500 且 auc/ic 不稳定 → 可 extend_window（目标 ≥ 2,000 bars）。
 4) 盈利门槛失败（Sharpe≥1, PF>1）且 500 < n_bars < 25,000：
    - 先 queue_job(alpha_loop) 一次，随后 wait 1 周期观察；禁止连续两次 alpha_loop。
 5) 阈值非根因：
    - mean_conf > conf_floor 且 auc≈0.5 或 ic≈0 → 判定“模型无信号”，优先 reduce_window 或 data_quality_check，而非调整阈值。
 6) 保守选项：
    - 证据冲突或信息缺失 → 返回 wait（cycles=1–3）或 do_nothing。
 7) 升级：
    - 连续3轮无改善或关键输入缺失 → escalate 并明确所需证据清单。

四、置信度与记忆策略
 - 置信度 = 规则匹配强度 × 历史成功率校正。
   - 近3次该动作成功率 >50% 且命中硬规则 → 0.75–0.90；反之降至 0.40–0.60。
 - 输出需包含 prev_attempts_summary：简述近1–2次动作与结果，说明不重复失败动作的原因。

五、输出格式（强制 JSON，仅一条）
 - 字段：
   - diagnosis: [短句，必须引用输入数字]
   - next_actions: [{action, confidence, params}]
   - rationale: 简短因果链，仅引用提供数据
   - prev_attempts_summary: 近次动作与结果的1行总结
 - 示例：
   ```json
   {
     "diagnosis": ["TSLA ic=0.005 with n_bars=35030 indicates large-window toxicity"],
     "next_actions": [
       {"action": "reduce_window", "confidence": 0.86, "params": {"symbols": ["TSLA","NVDA"], "window_days": 60}}
     ],
     "rationale": "n_bars≥25k且IC≈0时扩窗历史失败，先缩窗以降低概念漂移",
     "prev_attempts_summary": "extend_window 连续2次未改善（IC≈0）"
   }
   ```

六、工程落地任务清单（最小改动优先）
 - [ ] Prompt 文本更新：加入“必需输入/硬规则/动作集合/频控/输出格式/示例”。
 - [ ] 输入聚合层补充字段：n_bars、window_days、recent_actions[3]、data_quality、job_outcomes；缺失时由 Brain 选择 wait。
 - [ ] 动作路由器扩展：支持 reduce_window、data_quality_check、wait/do_nothing、escalate（含参数校验）。
 - [ ] 反复失败防抖：为每个 symbol 与动作记录近3轮 (action, result) 并在决策前检查禁令。
 - [ ] 置信度计算：基于规则命中与历史成功率调整。
 - [ ] 审计与可观测性：将建议与执行写入 runs/brain/{day}/recommendations.jsonl 与 actions.jsonl；Grafana 增加“动作频率/成功率”面板。
 - [ ] 单元测试：规则引擎用例（大窗口毒化、数据红旗、频控、禁令、置信度回退）。
 - [ ] 回滚与灰度：按“先沙箱→影子→小流量→全量”的顺序发布；保留老 prompt 开关。

七、验收标准（Acceptance Criteria）
 - 误扩窗率下降：n_bars≥25k 且 IC<0.02 的场景中，extend_window 动作占比 < 5%。
 - 动作多样性：queue_job 占比从 ~91% 下降至 < 50%，reduce_window/data_quality_check/wait 占比提升。
 - 重复失败动作抑制：连续两次相同失败动作后，第三轮不再重复（抽样 N≥10 验证 100% 生效）。
 - 诊断与输出合规：所有输出均为单一 JSON，对齐字段与示例；数值均可追溯到输入。
 - 决策延迟：平均响应时间不高于现状 +10s。

八、发布与回退计划
 - Phase 0（影子）：仅记录建议，不执行；观察 1–2 天，校验动作分布与禁令是否生效。
 - Phase 1（小流量≤20%）：执行 reduce_window/data_quality_check/wait，禁用 extend_window；观测 1 天。
 - Phase 2（受控全量）：开放 extend_window，但受硬规则与频控限制；观测 2–3 天。
 - 回退：保留旧 prompt 文本与动作白名单开关，一键切回 Level 0。

九、跨任务一致性与防幻觉（适用于 propose_plan / review_ml / decide）
- 关键输入校验（硬性）：列出本任务的 critical_fields；若缺失，输出 status=insufficient_context，不得编造默认值。
- 禁止自造常量：不得输出输入中未出现的时间/价格/阈值等硬数值。
- 相对规格优先：当缺关键基础量（如 entry_price、close_at）时，只输出相对规格（relative_spec），不要输出绝对价格或虚构时间。
- 自检字段（输出内）：
  - checks: {context_ok: bool, missing_fields: [..], fabricated_values_detected: bool}
  - numbers_used: [列出所有使用的数值字面量，便于上游做回溯核对]
- 工程侧护栏（聚合层）：
  - 拒收：若输出包含输入中未出现的数值（numbers_used 子集不在输入中），标记为 fabricated 并丢弃执行，仅记录审计。
  - 统一处理 insufficient_context：将 next_actions 转译为 request_context 或 wait，并提示所缺字段。

十、propose_plan（交易计划制定）的 Prompt 约束与片段
- Critical fields：session_type（RTH/afterhours）、now、close_at（或 session_end）、timezone、min/max interval_sec、resource_constraints（可选）。
- 规则：
  - cycles = floor((close_at - now)/interval_sec)。无 close_at → status=insufficient_context，不得假定 20:00 等默认值。
  - afterhours 允许 interval_sec ∈ [300, 600]；若资源紧张可在 notes 说明选择 600，但不得伪造 close_at。
  - 若 cycles < min_cycles（例如 3）→ 输出 do_nothing 或 wait。
- 输出示例（缺 close_at 时）：
  {"status":"insufficient_context","diagnosis":["Missing close_at prevents cycles calculation"],"next_actions":[{"action":"request_context","confidence":0.9,"params":{"fields":["close_at"]}}],"checks":{"context_ok":false,"missing_fields":["close_at"],"fabricated_values_detected":false},"numbers_used":[300,600]}

十一、decide（风险决策）的 Prompt 约束与片段
- Critical fields：symbol 基础指标（atr14、rvol20、ADV_USD）、spread_bps（可缺）、entry_price（如缺则进入相对规格模式）、risk_limits。
- 规则：
  - spread_bps 缺失时：不以缺失为拒绝依据；记录不确定性，并在 next_actions 加入 data_quality_check 或 request_context(spread)（低优先级）。
  - 缺失 entry_price：禁止输出 stop_price/take_profit_price 等绝对价格；改为 relative_spec：
    risk_spec = {"stop_loss":{"type":"relative","basis":"ATR14","mult":2.0},"take_profit":{"type":"relative","basis":"ATR14","mult":3.0}}
    并设置 status="pending_entry"，由执行层在下单时换算绝对价格。
  - 保持合规：若任一硬限制违反（例如 ADV_USD < 最小值）→ reject 并说明约束。
- 输出示例（缺 entry_price 时）：
  {"status":"pending_entry","decision":"allow","risk_spec":{"stop_loss":{"type":"relative","basis":"ATR14","mult":2.0},"take_profit":{"type":"relative","basis":"ATR14","mult":3.0}},"diagnosis":["spread_bps=null; using volatility-based relative stops"],"checks":{"context_ok":true,"missing_fields":["entry_price"],"fabricated_values_detected":false},"numbers_used":[2.0,3.0]}

十二、工程任务（跨任务扩展）
- [ ] 聚合层添加 fabricated 数字拦截：校验 numbers_used ⊆ 输入数值集合；否则标记 fabricated 并拒收执行。
- [ ] propose_plan 接口允许 status=insufficient_context 与 request_context 动作；禁止 pipeline 对缺失 close_at 进行默认填充。
- [ ] decide 执行侧支持 relative_spec→绝对价的下单时换算；当 entry_price 可得时进行回填。
- [ ] docs/daily/PROMPT_CHANGE_TEMPLATE.md 增补“critical_fields/insufficient_context/relative_spec/numbers_used/checks”的模板段。
- [ ] 端到端回归：构造三类场景（缺 close_at / n_bars≥25k 且 ic≈0 / 缺 entry_price），验证输出类型与拒收链路。

十四点五、分阶段审查架构（落地方案）
- Stage 0（确定性预检，代码侧）：
  - 计算 derived_flags：{data_quality_red_flag, large_window_toxicity, gates_pass}
  - 构造 allowed_actions：依据 flags 和频控/禁令（例如 large_window_toxicity→禁用 extend_window，优先 reduce_window）
  - 聚合 allowed_numbers：从输入递归提取数值（四舍五入到3位），作为 LLM 可引用的数字全集
  - 汇总 recent_actions[2]（从 runs/ai_trader/YYYY-MM-DD/ml_actions.jsonl）
  - 若 critical_fields 缺失 → 直接返回 insufficient_context / wait（不触发 LLM）
- Stage 1（LLM 诊断，轻输入）：仅传 flags + 最小数值（auc/ic/n_bars/window_days/mean_conf/conf_floor）+ allowed_actions + allowed_numbers + recent_actions 摘要
- Stage 2（动作选择）：
  - 若 root_cause→动作一一映射 → 代码侧确定参数（例如 reduce_window 选 {30,60}）
  - 否则由 LLM 从 allowed_actions 选择，路由层执行频控、禁令、置信度裁剪

十五、优先级与裁剪（策略层实现）
- 决策优先级：数据质量 > 大窗口毒化 > 盈利门槛 > 其它
- 置信度：使用 confidence_final = f(confidence_raw, 历史成功率, 硬规则匹配度)
- 符号范围：review_ml 单符号调用优先；多符号仅在需要做横截面比较时开启

十六、近期开工事项（本周内）
- [x] 实现 validators：build_ml_stage0(context) 与 validate_llm_review_output(output, allowed_actions, allowed_numbers)
- [x] control_event_consumer：注入 Stage 0（allowed_actions/allowed_numbers/flags/recent_actions），并在执行前做校验
- [x] 支持新动作：reduce_window（写 hpo_hints.json）、data_quality_check（运行 system_health_check.py）、wait/do_nothing（审计通过）、escalate（写入 escalations.jsonl）
- [x] 更新 ML_RESULT_REVIEW_PROMPT：加入 reduce_window/data_quality_check/wait/do_nothing/escalate、numbers_used/prev_attempts_summary 字段与示例
- [x] 新增轻量 memory：从 ml_actions.jsonl 汇总动作近 N 成功率，执行前按 success_rate 校准 confidence，并在连续2次失败时跳过
- [x] propose_plan Stage 0：afterhours/premarket 缺失 close_at 时直接返回 providers=[] + safe_mode（不触发LLM）

十七、配置开关与参数（新增）
- policies.ml.actions_conf_floor: float (默认 0.6)
- policies.ml.confidence_calibration:
  - base: float 置信度基线权重 (默认 0.6)
  - history_weight: float 历史成功率权重 (默认 0.4)
  - skip_fail_streak: int 连续失败阈值，达到后跳过该动作 (默认 2)


十三、附录 B：propose_plan 可粘贴 Prompt 片段
你是交易计划制定器。严格基于输入制定计划；若缺少 critical_fields（session_type、now、close_at、timezone、interval_sec 限制）则输出 status=insufficient_context，不得编造默认时间或价格。afterhours 仅允许 interval_sec ∈ [300,600]。cycles= floor((close_at-now)/interval_sec)。若 cycles<3，选择 do_nothing 或 wait。输出必须包含 checks 与 numbers_used，且所有数值须来自输入。

十四、附录 C：decide 可粘贴 Prompt 片段
你是风险决策器。若缺少 entry_price，则仅输出 relative_spec（ATR 倍数等）且 status=pending_entry，不得输出绝对价格。spread_bps 缺失不构成拒绝，记录为不确定性并可建议补齐。所有数值来自输入；输出包含 checks 与 numbers_used。

附录 A：可直接粘贴的 Prompt 片段
--------------------------------
你是“模型训练审查官”。先诊断（模型/数据/配置），再选择一项最小可行的动作。严格只引用输入提供的数字。若关键字段缺失，返回 wait。

必须输入（缺失则 wait）：
- per-symbol: auc, ic, top_decile_ret_bps, mean_conf, conf_floor, sharpe_gate, pf_gate, n_bars, window_days
- recent_actions[3]: {action, params, before_metrics, after_metrics, success}
- data_quality: {missing_rate, zero_price_count, extreme_return_count, timestamp_gap_count, feature_const_rate, label_leak_suspect}
- job_outcomes: 最近失败原因/日志标签

动作集合（仅此白名单）：extend_window, reduce_window, queue_job(alpha_loop), backfill, data_quality_check, wait, do_nothing, escalate

决策顺序与硬规则：
1) 若出现数据质量红旗 → 优先 data_quality_check/backfill，暂缓窗口/超参调整。
2) 若 n_bars≥25,000 且 ic<0.02 → 选择 reduce_window（30或60天），禁止 extend_window。
3) 若 n_bars≤500 且 auc/ic 不稳 → 选择 extend_window（目标≥2000 bars）。
4) 若盈利门槛失败且 500<n_bars<25,000 → 选择 queue_job(alpha_loop) 一次，然后 wait 1 周期；禁止连续两次。
5) 若 mean_conf>conf_floor 且 auc≈0.5 或 ic≈0 → 判为“无信号”，优先 reduce_window 或 data_quality_check，不归咎阈值。
6) 若证据冲突或信息缺失 → 选择 wait（cycles=1–3）或 do_nothing。
7) 若连续3轮无改善或关键输入缺失 → 选择 escalate 并列出所需证据。

频控与记忆：
- 同一动作近3轮最多1次；若近两次相同动作均无改善，本轮禁止该动作。
- 置信度基于历史成功率与规则匹配强度：成功率>50%且命中硬规则 → 0.75–0.90，否则 0.40–0.60。

仅输出单个 JSON：
- diagnosis: [短句，引用输入数字]
- next_actions: [{action, confidence, params}]
- rationale: 简短因果链，仅引用输入
- prev_attempts_summary: 最近1–2次动作与结果的1行总结

示例输出：
{"diagnosis":["TSLA ic=0.005 with n_bars=35030 indicates large-window toxicity"],"next_actions":[{"action":"reduce_window","confidence":0.86,"params":{"symbols":["TSLA","NVDA"],"window_days":60}}],"rationale":"n_bars≥25k且IC≈0时扩窗历史失败，先缩窗以降低概念漂移","prev_attempts_summary":"extend_window 连续2次未改善（IC≈0）"}
