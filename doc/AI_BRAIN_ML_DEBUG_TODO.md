# AI Brain ML Debug TODO（以赚钱为目标，而不是“跑通系统”）

> 目的：让 AI 大脑具备**真正调试机器学习**的能力——围绕“日内多次交易、日收益目标 ≥ 1%、绝不隔夜持仓”持续改进标签、特征、模型空间和执行门槛，而不是只关注脚本是否成功运行。

---

## 0. 当前状态小结（2025-11-13）

- ✅ 完整 ML 管线已上线：`wf_hpo.py`、`hpo_cli_upgraded.py`、ClickHouse 特征表、`label_profiles.yml`、MODEL_POOL、stacking、AutoML 等均已可用。
- ✅ AI 大脑已具备基础 ML 评审能力：`ML_RESULT_REVIEW_PROMPT` + `ControlBrain.review_ml()` + `control_event_consumer._execute_ml_actions()`。
- ✅ 已在 `ML_RESULT_REVIEW_PROMPT` 中加入：
  - **profit-first** 原则（Sharpe/PF/Expectancy gate）；
  - **日内 + 不隔夜 + 日内交易数** 的约束提示；
  - 对 AutoML/HPO（`alpha_loop`/`hpo_cli`）和 MODEL_POOL 的高层描述。
- ⏳ 缺失：**每个 ML 动作的“收益反馈”闭环**、更细粒度的“Alpha/Model/Execution 分工”和针对“日内赚钱”的专门专家 prompt。

---

## 1. 宪法与目标层（统一“赚钱”的判定标准）

- [x] 在 `ML_RESULT_REVIEW_PROMPT` 中加入：
  - 日内目标：`flat-overnight day trading + positive intraday PnL`；
  - 将“交易过少但 Sharpe 好”视为失败模式的说明；
  - 引导优先使用 `hpo_cli + wf_hpo + MODEL_POOL/stacking` 做 AutoML。
- [ ] 抽取一段统一的 **“AI 大脑宪法”** 文本，供所有 System Prompt 复用（Director / Alpha / Model / Execution / Critic）：
  - 账户级：日内多次交易、目标净收益 ≥ 1%/日、绝不隔夜；
  - 评估优先级：`intraday PnL > OOS Sharpe > PF > 回撤`；
  - 禁止：放宽风险门槛到 0、引入隔夜、只为“跑通 pipeline”而牺牲盈利；
  - 输出格式统一为“可机读 JSON + dot-path patch”。
- [ ] 将宪法挂入其他现有 prompt：
  - `prompts/ai_brain_hpo_prompt.md`；
  - Feature Engineering Agent prompt；
  - 未来新增的 Director / Alpha / Model / Execution / Critic prompt。

---

## 2. 多智能体 Prompt 与职责拆分（只负责“说清楚要改什么”）

> 在 prompts/ 下收敛为 4+1 轻量 Agent，每个只负责一个维度的 patch，后续由平台落地到 HPO/回测/配置。

- [x] 新增多智能体 prompt 文件（初版，仅文本）：
  - [ ] `prompts/brain_constitution.md`：统一宪法片段，供其他 prompt 引用。
  - [ ] `prompts/brain_director.md`：研究总导演（Director）
    - 输入：近 7 日日内指标 + 各阶段 delta 摘要。
    - 输出：`focus ∈ {alpha, model, execution}` + `horizon_days` + 成功门槛。
  - [ ] `prompts/alpha_expert.md`：Alpha 专家（Label + Feature）
    - 输入：`label_profiles.yml` 片段 + feature_programs/catalog 摘要 + 近期 IC/Sharpe/持仓时间统计。
    - 输出：`changes` 只包含 label_profile & 特征相关 dot-path patch。
  - [ ] `prompts/model_expert.md`：模型 & 空间专家
    - 输入：HPO 报告（`wf_hpo`/`hpo_cli`）、MODEL_POOL/stacking 配置、指标摘要（含 trades/day）。
    - 输出：HPO 空间 patch（`model_type`/stacking/model_pool 超参范围）。
  - [ ] `prompts/execution_expert.md`：执行 & 门槛专家
    - 输入：日内交易分布、PnL 时间结构、gating/risk 配置摘要。
    - 输出：执行参数 patch（time_stop、禁最后 N 分钟、min_notional、sizing 等），不可触碰 overnight。
  - [ ] `prompts/critic_referee.md`：轻量裁判
    - 输入：某个 Agent 的 JSON 输出 + 宪法 + schema。
    - 输出：`{valid: bool, violations: [...]}`。
- [ ] 为上述 prompt 定义统一输出 schema：
  - `version, actor, branch_id, mode(explore/exploit/shift), reasoning_summary, changes{dot.path: value}`。
- [ ] 将这些 prompt 的“允许修改路径白名单”与代码中的 patcher 对齐：
  - Alpha → `label_profiles.yml` + feature_programs/catalog；
  - Model → Alpha/HPO config (`alpha_config_*.json` / HPO JSON)；
  - Execution → `config/gating_policies.json` + 策略 configs；
  - 所有路径必须在 `config_patcher/json_policy_patcher/feature_catalog 工具` 的 whitelist 内。

---

## 3. 上下文与工具层：让 AI 大脑真的“看得到” ML 细节

> 目标：给 Brain 的 `ml_ctx` / 各 Agent 输入补上真正调试 ML 需要的信息，而不是只给 “脚本 ok / not ok”。

- [ ] 在 `control_event_consumer._run_ml_diagnostics_and_execute()` 生成的 `ml_ctx` 中补充：
  - [ ] 当前使用的 `label_profile`（从 HPO config 或策略 config 解析）；
  - [ ] 当前启用的 `model_type` / stacking / MODEL_POOL 配置摘要；
  - [ ] 数据集规模与时间结构：n_bars、window_days、训练/验证/测试时间段（按 symbol）；
  - [ ] 简化的 regime 摘要（如：高波动/低波动、开盘/午盘/尾盘区段表现）；
  - [ ] 特征层摘要：特征数量、主标签（vwap/vol/overnight）tag 分布。
- [ ] 在合适位置增加 **数据/标签健康检查模块**（可选脚本 + 上下文字段）：
  - [ ] Label 健康：零方差/极端偏度、label_leak_suspect、噪声水平估计；
  - [ ] Feature 健康：常数特征比例、极端相关性（max_inter_feature_corr）；
  - [ ] 数据缺失：bars/labels 缺口、极端 return 次数。
  - 输出写入 `ml_ctx["data_quality"]`，供 Alpha/Model 专家参考。
- [ ] 在 `wf_hpo` / HPO 报告侧增加“日内视角”指标（如果尚未有）：
  - 每个 trial 的：日内 PnL 分布、trades/day、日内 MDD，而不仅是全窗 Sharpe。

---

## 4. 奖励与记忆：每个 ML 动作都要有 “好 / 坏” 反馈

> 目标：让 AI 大脑不是“做了很多动作”，而是能从中知道：**哪类动作/分支真的让模型变好、让日内表现变好**。

- [ ] 为 `ml_actions.jsonl` 扩展字段（不改现有字段语义）：
  - `stage`：`alpha | model | execution | data`；
  - `branch_id`：匹配 Agent 输出中的 branch_id；
  - `before_metrics` / `after_metrics`：记录本次动作前后的关键指标快照（可只引用 `ml_last_metrics.json`/回测摘要）；
  - `delta`：`delta_oos_sharpe, delta_ic, delta_coverage, delta_intraday_pnl_pct, delta_trades_per_day`。
- [ ] 在 OOF/HPO 完成路径中实现一个简易 credit assignment：
  - 简化策略：**“最近一次非 wait/do_nothing 动作”默认归因于“下一次 OOF/HPO 结果”**；
  - 在 `_execute_ml_actions` 或 `control_event_consumer` 中，将该动作的 delta 指标补回到对应 `ml_actions.jsonl` 条目。
- [ ] 新增一个小工具（Python 脚本）汇总最近 N 日 `ml_actions.jsonl` → `ml_branches_summary.json`：
  - 按 `(actor, branch_id, stage)` 聚合：成功/失败次数、平均 delta 指标；
  - 提供给 Director Prompt 用作 `deltas_by_stage` 输入。

---

## 5. Orchestrator 层：让“总导演 + 专家”跑在 evening_learning 里

> 目标：每天晚上由 AI 大脑做一次真正的 “ML 研究循环”，围绕盈利目标调整一小步。

- [ ] 在 `scripts/ai_trader/brain/brain.py` 中为 ControlBrain 增加高层接口（仅包装，不破坏现有 API）：
  - `plan_research(context) -> director_output`；
  - `run_alpha_expert(context, director_output) -> alpha_output`；
  - `run_model_expert(context, director_output) -> model_output`；
  - `run_execution_expert(context, director_output) -> execution_output`；
  - 每个方法内部负责：构造 agent 输入 JSON → 调用对应 prompt → 解析 JSON。
- [ ] 在 `scripts/ai_trader/orchestrator.py.evening_learning()`（或合适的 nightly 流）中串起：
  1. 收集最近日内指标 + `ml_branches_summary.json`；
  2. 调用 Director 决定 focus；
  3. 根据 focus 调用某个专家（alpha/model/execution）；
  4. 将专家输出交给 Critic 校验；
  5. 通过已有 patcher / ml_scheduler 落地到配置或 HPO job；
  6. 记录动作到 `ml_actions.jsonl`。
- [ ] 增加开关（config.yml / ai_trader.yaml）：
  - 允许在“只做审稿、不执行动作”模式下运行（dry-run），便于前期观察大脑行为。

---

## 6. 评估与可视化：判断 “AI 大脑调参是否真的让系统赚钱”

- [ ] 在 ClickHouse / Grafana 中增加一小组 Dashboard（可复用现有 ML/OOS 面板）：
  - 最近 N 日：日内 PnL 走势、trades/day 分布、max_intraday_dd；
  - 各阶段（alpha/model/execution）动作频率与成功率（基于 delta_intraday_pnl_pct / delta_oos_sharpe）。
- [ ] 为 nightly 研究循环增加简单验收日志：
  - 每轮记录：“本轮 focus + 采取的动作 + 成功与否 + 对日内指标的贡献”；
  - 方便你人工 review AI 大脑的“研究决策”是否符合直觉。

---

## 7. 推荐执行顺序（阶段推进）

1. **P0：只改 Prompt 与 TODO 文档（本文件 + 多 Agent prompt）**  
   - 先把宪法、Director/Alpha/Model/Execution/Critic prompt 文本稳定下来；
   - 用真实数据在 ChatGPT/DeepSeek 上手工跑 1–2 轮，确认“建议风格”符合预期。

2. **P1：补全 ml_ctx 信息与简易记忆（data_availability + label_profile + model cfg + data_quality）**  
   - 让现有 `review_ml` 就已经能利用更多上下文做出更聪明决定（即使暂时不引入 Director）。

3. **P2：为 `ml_actions.jsonl` 加上 delta 指标与 summary 聚合脚本**  
   - 建立一个可量化的“ML 动作 → 盈利/IC 变化”的记忆层。

4. **P3：接入 Director + 单一专家（例如只先上 Model Expert）**  
   - 用 dry-run 模式观察几周，确认行为安全且对盈利有帮助。

5. **P4：逐步开放 Alpha/Execution 专家，并完善 Critic 规则**  
   - 到这个阶段，AI 大脑才能称为一个“以赚钱为目标的、自我进化 ML 研究员”。  

