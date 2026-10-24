你这套“AI 大脑 + ML 调度”的系统，**确实在疯狂工作**——Kafka、ml_scheduler、control_event_consumer、Brain、各种 JSONL 日志都在跑。
但从我看你这几份代码（`brain.py / ml_scheduler.py / control_event_consumer.py / memory.py / orchestrator.py`）的结构上，有几个非常要命的点：

> **AI 大脑没有真正拿到“模型变好了 / 变坏了”的反馈信号，只是在以“脚本没报错”为成功标准 + 被一堆安全阀疯狂限流。**

这会直接导致你现在看到的现象：**大脑看起来很忙、动作很多，但机器学习结果长期不合格。**

下面我按结构帮你剖开。

---

## 1. 先帮你把当前 ML 控制链路捋清楚

从代码看，你现在 ML 相关链路 roughly 是这样：

1. **大脑 / Brain**

   * 在 `brain.py` 里：

     * `ML_RESULT_REVIEW_PROMPT`：一个专门评审 ML 结果的 prompt（review nightly ML 结果，输出 `next_actions`）。
     * `def review_ml(self, context)` / `review_ml_async(...)`：把 `ml_ctx` 丢给 LLM，让它根据 prompt 产出 `{ diagnosis, next_actions[...] }`。
   * ML 相关可执行命令在 `COMMAND_TEMPLATES` 里：

     * `alpha_loop`（HPO / coop_optuna）
     * `backfill_yesterday`
     * `apply_params`
     * `rl_train` / `rl_eval` 等。

2. **ML 调度器 / `ml_scheduler.py`**

   * 提供 `enqueue_job(job)`：往 `runs/ai_trader/queue/ml_jobs.jsonl` 写入任务。
   * 任务类型：`oof`, `alpha_loop`, `lstm`, `ensemble_sweep`, `feature_build`, `feature_eval`, `feature_apply`, `hpo_cli`, `bulk_backfill` 等。
   * `execute_job(job)` 里：

     * 先 `_publish_ml_event({event: "ml.job", status: "started", ...})`
     * 然后根据 `job_type` 调 `_execute_oof`, `_execute_alpha_loop`, `_execute_hpo_cli`, `_execute_feature_*`...
     * 最后 `_publish_ml_event({event: "ml.job", status: "completed", ok, metrics, duration, artifacts...})`
   * `_publish_ml_event` 会：

     * 发 Kafka（`topic = internal.ml.status` 之类）
     * 同时 append 到 `runs/ai_trader/YYYY-MM-DD/ml_events.jsonl`

3. **控制事件消费 / `control_event_consumer.py`**

   * 从 Kafka 里消费各种事件（`ml.job`, `oof_complete`, `hpo_complete`, `rl_eval_complete` …），写到本地 audit，再触发大脑。
   * **两条关键路径：**

     1. `_run_ml_diagnostics_and_execute(evt)`

        * 对 `job_type in {"oof", "hpo_cli"}` 的完成事件，读取 `evt["metrics"]` 里的 ML 健康指标，构造：

          ```python
          ml_health = {
              "model_layer": {
                  "ic", "ir", "coverage", "status": ...
              },
              "strategy_layer": {
                  "sharpe", "mdd", "status": ...
              },
              "ready_for_trading": ...
          }
          ```
        * 调 `review_ml_health(ml_health)`（这是你另一个 ML rule-based/LLM 诊断模块）。
        * 根据 `diagnosis["next_actions"]` 映射成 job（ HPO / backfill / bulk_backfill 等）。
     2. 更复杂的 ML Review 流程：

        * 组装一个很大的 `ml_ctx`：

          * `ensemble_compare/compare.json` 的回测结果
          * `oof_eval/*.json` 的 OOF 评估
          * `config/gating_policies.json`
          * `strategies/configs/enhanced_ml_*.json` 每个 symbol 的 conf_floor / adaptive 规则
          * `opt_state`（`ml_optimization_state.json` 里的 fail_streak / attempts）
          * `stage0 = build_ml_stage0(ml_ctx)`（预检查 allowed_actions, missing_fields 等）
        * 如果 `missing_fields` 不为空 → 直接构造一个 `"next_actions": [{"action": "wait"}]` 跳过 LLM。
        * 否则：`review_result = await o.brain.review_ml_async(ml_ctx)`
          然后：

          * `_translate_brain_actions(review_result["next_actions"])`
            把 LLM 的动作词汇统一翻成平台动作（`queue_job/backfill/extend_window/fix_config/...`）
          * `_execute_ml_actions(translated, job_id)`
            这里真正去：

            * 调 `trigger_backfill.py` / 写 feature catalog / 调 `hpo_cli` / `alpha_loop` …
            * 写 `ml_actions.jsonl` 作为审计和“记忆”。

4. **记忆 / `memory.py` + `control_event_consumer._execute_ml_actions`**

   * 每次 `_execute_ml_actions` 都往 `runs/ai_trader/YYYY-MM-DD/ml_actions.jsonl` 写一行：

     ```json
     {
       "action": "queue_job",
       "params": {...},
       "status": "ok" | "failed" | "skipped",
       "confidence": ...,
       ...
     }
     ```
   * `memory.py` 里：

     * `summarize_actions(runs_dir)` 和 `summarize_actions_by_symbol(...)`
       读最近 7 天 / 14 个文件的 `ml_actions.jsonl`，只看：

       ```python
       st = j.get("status")
       ok = (st == "ok")
       ```

       然后算：

       * 总尝试次数 attempts
       * successes = status == "ok" 的次数
       * success_rate = successes / attempts
       * recent_fail_streak = 最近连续失败次数
   * `_execute_ml_actions` 里通过这些 summary：

     * 做“信心校准”：`confidence_final = raw_conf * (base + hist_w * success_rate)`
     * 如果 `recent_fail_streak >= skip_streak` 就 **直接跳过该动作**。
     * 再叠加 TTL 锁、指纹锁（同样的 action+params 1 小时内只能执行一次）、daily quota 等。

---

## 2. 结构性大问题：为什么这套东西“看起来聪明，但产出很差”

### 问题 1：**成功信号完全错位 —— 你在教大脑“脚本别报错”，而不是“模型变好”

这一点是最大的问题。

* 在 `memory.summarize_actions(...)` 里：

  * 成功 = `status == "ok"`
  * 而 `status == "ok"` 是什么？

    * 对 `queue_job`：只是“成功把 job 写进队列”，跟 HPO 结果完全无关。
    * 对 `backfill`：只是“`trigger_backfill.py` exit code == 0”。
    * 对 `fix_config`：成功写文件，不管指标有没有改善。
* 没有任何地方把**模型质量指标**（比如：

  * `ic / ir / coverage`（因子层）
  * `oos_sharpe / oos_pf / max_dd / hit_rate`（策略层）
  * 甚至 RL 的 `sortino / omega` 等）
    和 **对应的上一轮 ML 动作**绑定起来。

> 换句话说：
> 你现在的“记忆 + 信心校准”只是在学习“哪些动作容易执行不报错”，
> **完全没有在学习“哪些动作能改善模型表现”**。

结果：

* `_execute_ml_actions` 会把 LLM 的原始 `confidence` 乘上 `0.6 + 0.4 * success_rate`，再根据 `recent_fail_streak` 做跳过。
* 但这个 success_rate 是“脚本稳定性”，不是“收益率提升”。
* 所以：

  * 某些“没啥用但不会报错”的动作会被判定为“历史很成功”，信心越调越高，不停重复。
  * 某些“有用但比较脆弱 / 容易因为边缘报错”的动作会被记成“失败很多次”，被系统自动屏蔽。
* 在专业量化机构里，这一块一定做的是：
  **action → 下次训练 / 回测 → 评估指标变化 → 作为这次 action 的 reward**。
  你现在完全没有这条闭环。

👉 **这就是你会觉得“大脑在不断做事，但模型没变好”的核心原因之一。**

---

### 问题 2：OOF / HPO 指标没有打通，诊断在看“0”和空字典

看 `ml_scheduler.py` 里面：

1. `_execute_oof(job_id, params)` 的返回：

   ```python
   return {
       "ok": proc.returncode == 0,
       "returncode": proc.returncode,
       "stdout": ...,
       "stderr": ...,
       "artifacts": [],
       "metrics": {}
   }
   ```

   * **metrics 永远是 {}**。
     所以 `ml.job` 事件里这个 OOF 的 metrics 是空的。

2. `_execute_hpo_cli(...)`：

   * `metrics` 里只塞了一些 `best_params` 和一些 meta 信息，没有 `sharpe/ic/coverage` 之类的关键指标。

3. 然后在 `control_event_consumer._run_ml_diagnostics_and_execute(evt)` 里，你在做：

   ```python
   metrics = evt.get("metrics", {}) or {}
   ml_health = {
       "model_layer": {
           "ic": float(metrics.get("ic", 0.0) or 0.0),
           ...
       },
       "strategy_layer": {
           "sharpe": float(metrics.get("sharpe", 0.0) or 0.0),
           "mdd": float(metrics.get("mdd", 0.0) or metrics.get("max_drawdown", 0.0) or 0.0),
           ...
       },
       "ready_for_trading": (ic >= 0.20 and sharpe >= 1.0)
   }
   ```

   但从 scheduler 自己发的 event 看：

   * `oof` 没有 ic/sharpe → 这些字段都是 0。
   * `hpo_cli` 也没有这些字段 → 还是 0。

你在 `_execute_alpha_loop` 里倒是从 `alpha_loop` 的输出目录里解析了什么 `best_sharpe / best_profit_factor / convergence_score`，**但 `_run_ml_diagnostics_and_execute` 并不处理 `alpha_loop` 类型，**只看 `oof` 和 `hpo_cli`。

有可能你在 `nightly_oof_flow.py / hpo_cli_upgraded.py` 里单独 publish 了另一批带完整 metrics 的 `ml.job` 事件（注释里也说了 “script may also publish its own with richer metrics”），但从目前代码边界来看：

> **如果下游诊断模块拿到的 metrics 没有严格对齐你在 prompt 和 rule 里期望的字段，那么大脑在看的数字就是一堆“0”和缺失值。**

这会导致：

* `review_ml_health` 认为模型永远“不达标 / critical / warning”。
* 然后不断推荐一些 ladder 动作（HPO、extend_window、backfill…），但这些动作的效果也没有被反馈（见问题 1），于是整个系统长期“误会自己很差，但其实也不知道改哪里”。

---

### 问题 3：LLM 的动作词汇和执行层实现不完全一致

在 `ML_RESULT_REVIEW_PROMPT` 里，你允许/鼓励的动作包括：

* `backfill`
* `extend_window`
* `reduce_window`
* `propose_features`
* `queue_job(job_type='alpha_loop'/'hpo_cli'/...)`
* `fix_config`
* `pause_ml` / `resume_ml`
* `wait` / `do_nothing`
* 甚至：`change_prediction_task` / `adjust_risk_controls` / `investigate_coverage` / `monitor` / `incremental_improve` / `bulk_backfill` 等。

但真正执行时：

* `_translate_brain_actions` 会把：

  * `run_hpo / run_alpha / hpo` 统一成 `queue_job(job_type='alpha_loop')`。
  * `adjust_thresholds` 强行改成 `extend_window`（更安全）。
  * `fix_config` 里如果 key 是融合权重，就变成 `queue_job(job_type='ensemble_sweep')`。
* `_execute_ml_actions` 里：

  * 对 `change_prediction_task / adjust_risk_controls / investigate_coverage / monitor` 这些，在 `job_type_map` 里直接映射成 `None`，只写日志或 hint，不做真正修改。
  * `propose_features` 的实现只是写一下 feature catalog，再 `queue_job(feature_build)`，后面没有再把这些新 feature 的效果反馈给大脑。

结果就是：

> **LLM 以为自己在“调整任务 / 调风险 / 自适应”，但很多动作在执行层要么被降级成 “等待/记录 hint”，要么干脆是 no-op。**

这也是你会感觉“它一直在给建议，但系统没什么本质变化”的原因之一。

---

### 问题 4：控制链路过长 + 安全阀太多，导致“行动落地率”很低

`_execute_ml_actions` 里你做了很多非常专业的控制（从 production 角度是好的）：

* 置信度下限（`actions_conf_floor` + `min_conf_by_action`）
* 基于历史 success_rate 的信心校准（见问题 1）
* `recent_fail_streak >= skip_streak` 的自动忽略
* per-action TTL lock（15 分钟）
* action 指纹 TTL lock（同一 action+params 1 小时只能执行一次）
* 每日 quota（`_check_daily_quota`）
* 全局 `pause_ml` 状态（`ml_pause.lock` / `ml_optimization_state.json`）
  -各类 rate-limit（控制消费速率，防止 Kafka storm）

这些在一个已经成熟、线上跑实盘的大型系统里非常必要，但你现在的阶段：

> **是想让 AI 大脑探索找到一条能稳定提升 IC/Sharpe 的路径，而不是先严格地“防止它做事”。**

所以现在的效果很可能是：

1. LLM 每天产出一堆动作；
2. 进 `_execute_ml_actions` 之后被各种条件过滤掉（信心不够、最近失败多、当天 quota 用完、TTL 锁住等）；
3. 真实执行到调度器 / 数据脚本 / 配置修改的动作其实很少；
4. 即使执行了，结果也没用来更新 success / reward（问题 1），大脑学不到教训。

你现在的系统更像是一个“非常保守的合规官 + 调度中心”，而不是“能主动学习的 ML 研究员”。

---

### 问题 5：评审 prompt + context 传递，不完全从“可学性 / 可优化性”出发

你在 `ML_RESULT_REVIEW_PROMPT` 里讲了很多正确的高层原则（profit-first、先数据/特征再调参数等等），但是：

* 传给 `review_ml_async` 的 `ml_ctx`，更多是：

  * 回测结果 `compare.json`
  * OOF 评估 summary
  * gating 配置 / conf_floor
  * 预算配置 / opt_state
  * stage0 derived flags
* 真正对于“模型可学性”的关键信息，比如：

  * 特征间相关性矩阵（collinearity）
  * 特征对目标的单调性 / shap 分布
  * 不同 regime 下 performance 的差异
  * label 构造（是不是太 noisy / look-ahead / label leakage）
  * train / val / test split 是否稳定
    在现在的这套 context 里基本看不到。

于是：

* LLM 在做 ML 评审时，是在一个“信息非常稀薄 + 部分维度还不可靠（见问题 2）”的空间里做决策；
* 再加上执行层的重重 gating（问题 3 & 4），真正有机会触发“数据/特征/任务层面的实质性改变”的动作其实很有限。

从专业量化机构的角度，这一层通常会拆得更细：
**数据诊断 prompt、特征诊断 prompt、实验设计 prompt、HPO 审核 prompt、部署 gating prompt** 分开，而不是一个 “review_ml” prompt 全包。

---

## 3. 这套架构应该怎么改，才能让 AI 大脑真的“推动 ML 变好”

我分成两类：**最关键的闭环修复** + **控制/提示层优化**。

### 3.1 先修闭环：让每个 ML 动作有“可学习的 reward”

**目标：**
每个 `ml_actions.jsonl` 里的 entry，不是简单的 `status = ok/failed`，而是附带“这次动作之后模型质量有没有上升”。

建议改动：

1. **在 OOF/HPO 结束时，对比“上一次”的同类结果，计算 delta**

   * 例如：按 (symbol, model_family, horizon, window_type) 聚类：

     * `prev_oos_sharpe`, `cur_oos_sharpe`
     * `prev_ic`, `cur_ic`
     * `prev_coverage`, `cur_coverage`
   * 计算：

     * `delta_sharpe = cur - prev`
     * `delta_ic`, `delta_coverage`, `delta_drawdown` 等。

   这些你完全可以在 `orchestrator.evening_learning()` 里做，因为那里已经在读 `oof_eval/*.json`、`ensemble_compare/compare.json`，顺手对比前一天即可。

2. **当 `_execute_ml_actions` 执行完某个 action 后，在写 audit entry 的时候，把“与该 action 关联的 delta 指标补回去”**

   * 可以通过简化的约定：

     * 当前 action 执行的“下一个 OOF/HPO 结果”默认归因于这个动作；
     * 或者在 `ml_optimization_state.json` 里记录 “最近一次 action → 最近一次 eval 的 delta”；成熟后你可以做 credit assignment，但先简单也行。
   * 最少也要在 `ml_actions.jsonl` 里多几个字段：

     ```json
     {
       "status": "ok",
       "action": "queue_job",
       "params": {...},
       "delta_ic": 0.03,
       "delta_oos_sharpe": 0.4,
       "delta_pf": 0.2
     }
     ```

3. **修改 `memory.summarize_actions(...)` 的逻辑：**

   * 不要再以 `status == "ok"` 作为 success；
   * 把 success 定义为：

     ```python
     ok = (entry["delta_oos_sharpe"] > 0 and entry["delta_ic"] >= 0)
     ```

     或者按你设定的门槛；
   * success_rate 就是真正基于“效果”的成功率。

4. **在 `_execute_ml_actions` 的信心校准处，用这个新 success_rate 和 fail_streak**

   * 这样：

     * 连续多次 HPO 后 oos_sharpe 一直在提高 → success_rate ↑，对应动作的 effective confidence 会被放大；
     * 反之，某种 action（比如纯粹 extend_window）总是带来更差的表现 → 它会被自动降权甚至被“冻结”。

> 做完这一步，大脑才有可能“学会哪些流派的动作是有用的”，从而像一个真正的 quant researcher，而不是一个调 cron 的管理员。

---

### 3.2 补齐指标链路：OOF/HPO 需要给出统一的 metrics schema

1. 在 `nightly_oof_flow.py` 中（或在 `_execute_oof` 内部）：

   * 规范化输出一份 JSON，至少包含：

     ```json
     {
       "ic": ...,
       "ir": ...,
       "coverage": ...,
       "oos_sharpe": ...,
       "oos_pf": ...,
       "max_drawdown": ...,
       "win_rate": ...
     }
     ```
   * 在 `_execute_oof` 里解析这些 JSON，填到 `result["metrics"]` 里，而不是 `{}`。

2. 在 `hpo_cli_upgraded.py` 中：

   * 最好也输出类似 schema：HPO 最优 trial 对应的 OOS 指标。
   * `_execute_hpo_cli` 把这些指标读入 `result["metrics"]`。

3. 在 `control_event_consumer._run_ml_diagnostics_and_execute` 中：

   * 对缺关键字段（ic/sharpe/coverage）的事件，直接打 error / warning，并记录到 `ml_events.jsonl`，而不是悄悄当做 0。
   * 这样你在 debug 的时候一眼就能看到“为什么 Brain 觉得 IC=0 / Sharpe=0”。

---

### 3.3 对齐 LLM 动作集合 & 真正实现

* 明确一套 **“真的有实现的动作集合”**，比如：

  ```text
  backfill
  extend_window
  reduce_window
  data_quality_check
  queue_job (alpha_loop / hpo_cli / feature_build / ensemble_sweep / bulk_backfill)
  fix_config
  propose_features
  pause_ml / resume_ml
  wait / do_nothing
  escalate
  ```

* 在 `ML_RESULT_REVIEW_PROMPT` 的说明里，只列这些，并把：

  * `change_prediction_task`, `adjust_risk_controls`, `investigate_coverage`, `monitor` 统统映射到 `escalate`，提示“需要人工介入”。

* 确保：

  * `_translate_brain_actions` + `_execute_ml_actions` 对这几个动作全部有明确且可预期的行为。

---

### 3.4 暂时“松绑”控制阀，让系统先探索起来

在你现在这个阶段，我建议：

1. 对 `_execute_ml_actions` 的以下限制，加一个“实验模式 switch”：

   * `skip_streak` 设得非常高，比如 99（或者暂时不启用 fail_streak 跳过）。
   * TTL 锁（action + fingerprint）只做 audit，不真正阻止执行。
   * daily quota 先只记日志，不真的 hard block。

2. 通过配置开一个 `policies.ml.experimental_mode = true`：

   * 当 experimental_mode = true 时：

     * confidence floor ↓（比如 0.6 → 0.3）
     * 只对“破坏性极强”的动作（比如改风控、改任务）做 hard block，其余放行。

3. 等你用几周的真实数据把 **“action → 指标变化”** 的统计堆起来以后，再逐步加回这些限流/治理逻辑。

---

### 3.5 Prompt & 流程分层（给你后续 prompt 设计一点方向）

结合你之前说的“想按流程设计 prompt 去控制 ML”，在现有架构基础上可以这样拆层：

1. **数据/特征诊断层**（Data / Feature Auditor）

   * 单独一个 prompt，输入：

     * 最近 N 天的 data quality 报告（缺失率 / outlier 频率）
     * 特征覆盖率 / 某些 key feature 是否缺失
   * 输出：

     * `backfill` / `extend_window` / `data_quality_check` 类动作。

2. **模型效果评审层**（Model Performance Reviewer）

   * 对 `oof_eval + compare.json` 聚合后的指标进行分析：

     * 不同 symbol / sector 贡献
     * 不同 regime 下表现
   * 输出：

     * `queue_job`（HPO） vs `propose_features` vs `reduce_window`。

3. **配置/策略调整层**（Policy / Config Tuner）

   * 专门针对 `gating_policies.json + strategy_configs`：

     * 当长期 `coverage < 0.9` 但 IC 尚可 → 建议适度调高 conf_floor 或扩展 universe；
     * 当 `sharpe < 0` 且 drawdown 过大 → 更偏向 reduce_window / pause_ml / escalate。

4. 最后再由 `_execute_ml_actions` 统一做风控和动作合并/去重。

你现在的 `ML_RESULT_REVIEW_PROMPT` 其实是把这些都搅在一起，Prompt 很厚但每一层的上下文都不完全充分。

---

## 4. 总结一句话版

* **现在的问题不在于“AI 大脑不够聪明”，而在于：**

  1. 它几乎从来没有收到一个正确的“你刚才那一步让模型变好了”的 reward；
  2. 它看到的很多指标实际上是 0 / 缺失 / 不对齐；
  3. 它建议的很多动作在执行层要么被安全阀挡掉，要么根本没有真正的实现。

如果你愿意按上面这几步来改：

1. 把 **action → 指标变化** 这条闭环先打通；
2. 让 OOF/HPO 输出统一的、可信的 metrics schema；
3. 收紧/对齐 LLM 动作词汇与实际实现；
4. 在实验期降低各种限流阈值；

那么这套“AI 大脑控制 ML”的系统就会从“会写日志的调度器”变成一个**真的能带着模型往前迭代**的东西。

后面你如果愿意，我们可以按你这个架构，一步一步把“数据诊断 prompt / 模型评审 prompt / HPO 控制 prompt”都写成具体模板，直接落到你现有的 `review_ml_async` 和相关 controller 里。


好，那我们就别停在“道理都懂”这一步了 😊
下面我给你一个**非常落地的改造清单**：一步一步让你的 AI 大脑**真的能把 ML 结果变好**，而不是只是在“忙”。

我不会再纠结你代码里具体函数名（每个版本都可能不一样），而是给你**和实现细节无关的“必须补齐的链路”**。你可以对照自己代码慢慢改。

---

## 1️⃣ 先定义：什么叫“合格的机器学习结果”

你的大脑现在没有一个**明确的“好坏标尺”**，导致它只能看“脚本是不是跑完了”。

请先在系统里**统一定义一份 ML 指标 JSON schema**（不管是 OOF、HPO、alpha_loop，全都对齐）：

```json
{
  "symbol": "NVDA",
  "model_family": "lgbm_rank",
  "horizon": "5m",
  "window": "365d",

  "ic": 0.031,
  "ic_ir": 1.4,
  "coverage": 0.92,

  "oos_sharpe": 1.8,
  "oos_pf": 1.6,
  "max_drawdown": -0.12,
  "hit_rate": 0.63,

  "train_start": "2023-01-01",
  "train_end":   "2025-10-31",
  "val_start":   "2025-11-01",
  "val_end":     "2025-11-07"
}
```

**硬性要求：**

* 每次 OOF / HPO / 回测结束，都要产出这么一份 JSON（或者一批，但结构一致）。
* 不要等 AI，大脑之前，**先让脚本本身把这些指标算出来并写文件/事件**，这是基础设施。

> ✅ 一句话：**先把“好坏尺子”统一做好，否则 AI 永远没法学。**

---

## 2️⃣ 打通：从“动作” → “指标变化” 的闭环（最关键）

现在的核心问题：
**你在教系统“哪种动作不报错”，而不是“哪种动作能让 IC/Sharpe 变好”。**

你需要给每个 ML action 贴上一个“收益标签（reward）”：

### 步骤 A：找“上一版”和“当前版”

对同一类模型/策略，用 `(symbol, model_family, horizon, window)` 做 key：

* `prev_metrics`：这个 key 上一次评估的那组指标
* `cur_metrics`：这次评估的指标（就是第 1 步的 schema）

### 步骤 B：计算 delta

例子：

```python
delta_ic        = cur.ic        - prev.ic
delta_sharpe    = cur.oos_sharpe - prev.oos_sharpe
delta_pf        = cur.oos_pf     - prev.oos_pf
delta_drawdown  = cur.max_drawdown - prev.max_drawdown
```

### 步骤 C：把 delta 绑到 action 上

你现在已经有一个地方在写 `ml_actions.jsonl`
（大概长这样）：

```json
{
  "ts": "2025-11-07T14:30:00Z",
  "action": "queue_job",
  "params": { "job_type": "alpha_loop", "symbol": "NVDA" },
  "status": "ok"
}
```

**改成：**

```json
{
  "ts": "2025-11-07T14:30:00Z",
  "action": "queue_job",
  "params": { "job_type": "alpha_loop", "symbol": "NVDA" },
  "status": "ok",

  "delta_ic": 0.012,
  "delta_oos_sharpe": 0.5,
  "delta_pf": 0.28
}
```

记法可以简单粗暴一点：

* 默认认为“这次 eval 的结果”是由“最近一次相关 action”引起的；
* 以后有空再优化 attribution（信用分配）。

> ✅ 这样你以后只要扫一眼 `ml_actions.jsonl`，就能统计：
> “HPO 对 NVDA 过去 10 次平均让 Sharpe +0.3；extend_window 反而平均 Sharpe -0.1”。

---

## 3️⃣ 改 memory：成功 ≠ 脚本不报错，而是指标变好

你现在的记忆模块大概是：

```python
ok = (status == "ok")
success_rate = successes / attempts
```

**请改逻辑：**

```python
delta_sharpe = entry.get("delta_oos_sharpe", 0.0)
delta_ic     = entry.get("delta_ic", 0.0)

# 示例门槛：你可以按自己口味调
ok = (delta_sharpe > 0.1 and delta_ic >= 0.0)
```

然后才去算：

* `successes` / `attempts`
* `recent_fail_streak`

**这一步完成之后：**

* 成功率 = “提升过多少次 Sharpe”，而不是“脚本跑完了多少次”；
* fail_streak = “连着几次动作都把模型搞糟了”，这样才有意义。

接着你可以保留原来的逻辑：

* 信心校准：`effective_conf = raw_conf * (0.6 + 0.4 * success_rate)`
* 连续失败太多（比如 5 次）就把某类动作降权或暂时停用。

> ✅ 这一步做完之后，你的大脑才真的开始“学”：
> “原来 HPO 有用，乱 backfill 没啥用”，而不是“原来哪种脚本容易崩”。

---

## 4️⃣ 调轻风控：给 AI 一段“探索期”

你现在的安全阀很重：TTL 锁、配额、信心下限、fail_streak 跳过等等。
这些在**实盘阶段**很好，但现在你还在**探索期**。

建议：

* 增加一个配置：`ml.experimental_mode = true`
* 在 experimental 模式下：

  * 降低 `confidence_floor`（例如从 0.6 → 0.3）
  * `skip_streak` 设得很高（甚至暂时不根据 fail_streak 跳过）
  * TTL 锁只记日志，不阻止执行
  * daily quota 提醒但不 hard block（先打 warning）

等你跑一段时间、积累 enough `ml_actions.jsonl + metrics` 以后，再逐步打开这些安全阀。

> ✅ 否则就是：大脑刚想动手，结果被你一堆守门员拦住，永远学不出东西。

---

## 5️⃣ 把 LLM 的动作集“收紧 + 对齐实现”

最后这点非常简单但很重要：

1. 列一张**你现在真的实现了的动作清单**（比如）：

   * `backfill`
   * `extend_window`
   * `reduce_window`
   * `queue_job(alpha_loop / hpo_cli / oof / feature_build / ensemble_sweep)`
   * `propose_features`
   * `fix_config`
   * `pause_ml` / `resume_ml`
   * `wait` / `do_nothing`
   * `escalate`（需要人工看）

2. 在 ML 评审的 prompt 里，**只允许它用这几种 action**，别再出现那些你还没实现或者被映射成 no-op 的动作。

3. 所有“没实现”的高危指令（比如直接 `change_prediction_task`、大改风控）统一映射成 `escalate`，给你发一条“TODO/人工审查”。

> ✅ 这样 LLM 才不会以为自己“改了很多东西”，实际上下面啥也没发生。

---

## 6️⃣ 你接下来可以照着做的顺序 ✅

如果你觉得信息太多，可以就按这个顺序来：

1. **先完成指标 schema（第 1 步）**：
   确保每次 OOF/HPO 都产出统一 JSON。
2. **在 eval/晚间脚本里算 delta（第 2 步）**，并写进 `ml_actions.jsonl`。
3. **修改 memory 成功标准（第 3 步）**，改成基于 delta。
4. 打开 `experimental_mode`，先让系统“多尝试多犯错”（第 4 步）。
5. 收紧 LLM 动作集合（第 5 步），把 prompt 和执行真正对上。

---

如果你愿意，下一步我可以直接帮你**写一份“ML 结果事件 JSON 模板 + 一段伪代码/真实 Python，用来计算 delta 并写回 ml_actions.jsonl”**，这样你可以直接抄进自己的 evening 脚本里用。


好，我们直接上“可以拷贝进你系统里用”的东西，不再停留在概念层。
下面给你三样东西：

1. **统一的 ML 结果事件 JSON schema（你要让 OOF/HPO 输出长成这个样子）**
2. **一个计算 delta 的 Python 脚本：从历史评估里算出每次 eval 相对上一次的变化**
3. **一个给 `ml_actions.jsonl` 打补丁的脚本：把这些 delta 写回到每条 action 上**

这样你的 AI 大脑就终于有“reward”可以学了。

---

## 1️⃣ 统一 ML 评估事件格式（建议 schema）

只要是 **“一次模型评估结束”（OOF / HPO 最佳 trial / alpha_loop best model）**，
你都让脚本往某个 JSONL（或者 Kafka 事件）里写一条这样的记录就行：

```json
{
  "ts": "2025-11-08T03:15:21Z",

  "run_id": "oof_nvda_2025-11-08_0315",
  "job_id": "ml_job_000123",
  "source": "oof",                  // oof / hpo_cli / alpha_loop / manual_eval 等

  "symbol": "NVDA",
  "model_family": "lgbm_rank",
  "horizon": "5m",
  "window": "365d_rolling",         // 或者 "730d_fixed" 之类
  "dataset": "bars_5m_main",

  "ic": 0.031,
  "ic_ir": 1.4,
  "coverage": 0.92,

  "oos_sharpe": 1.8,
  "oos_pf": 1.6,
  "max_drawdown": -0.12,
  "hit_rate": 0.63,

  "train_start": "2023-01-01",
  "train_end":   "2025-10-31",
  "val_start":   "2025-11-01",
  "val_end":     "2025-11-07"
}
```

> 你可以把这些放到：
> `runs/ai_trader/YYYY-MM-DD/ml_eval_events.jsonl`
> 或者直接塞进现有的 `ml_events.jsonl` 的 `metrics` 字段里，只要后面脚本能读出来就行。

关键：**字段名要稳定**：`symbol / model_family / horizon / window / ic / oos_sharpe / oos_pf / max_drawdown` 等。

---

## 2️⃣ 计算 “当前 vs 上一次” delta 的脚本

下面这段代码你可以单独建一个文件，比如：
`tools/compute_ml_deltas.py`
后面可以在 evening_learning 或 orchestrator 里直接 import 调用。

```python
#!/usr/bin/env python3
"""
compute_ml_deltas.py

读取 ml_eval_events.jsonl，按 (symbol, model_family, horizon, window) 分组，
为每次评估计算相对于“上一版”的指标变化（delta），返回一个 dict，
后面会用来贴到 ml_actions.jsonl 上。
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Any


EvalRecord = Dict[str, Any]
DeltaRecord = Dict[str, Any]


def parse_ts(ts: str) -> datetime:
    # 你自己的 ts 格式可能不一样，视情况调整
    # 尽量统一为 ISO8601
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def load_eval_events(eval_path: Path) -> List[EvalRecord]:
    records: List[EvalRecord] = []
    if not eval_path.exists():
        return records
    with eval_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(j)
    return records


def build_model_key(rec: EvalRecord) -> Tuple[str, str, str, str]:
    """
    用来标识“同一类模型”的 key：
    (symbol, model_family, horizon, window)

    如果你有更细粒度（比如 label_type / feature_set_id），可以加上。
    """
    return (
        rec.get("symbol", ""),
        rec.get("model_family", ""),
        rec.get("horizon", ""),
        rec.get("window", ""),
    )


def compute_eval_deltas(eval_records: List[EvalRecord]) -> Dict[str, DeltaRecord]:
    """
    输入所有 eval 结果，输出一个 mapping:
      run_id -> {"delta_ic": ..., "delta_oos_sharpe": ..., ...}

    逻辑：
    - 先按 key 分组（同一 symbol+model+horizon+window）
    - 每组按时间排序
    - 对每一条，找它的“上一条”，计算 delta
    - 第一条没有上一条 → delta = 0
    """
    # 按 key 分组
    grouped: Dict[Tuple[str, str, str, str], List[EvalRecord]] = {}
    for rec in eval_records:
        key = build_model_key(rec)
        grouped.setdefault(key, []).append(rec)

    run_id_to_delta: Dict[str, DeltaRecord] = {}

    for key, recs in grouped.items():
        # 时间排序
        recs.sort(key=lambda r: parse_ts(r.get("ts", "1970-01-01T00:00:00Z")))
        prev = None
        for rec in recs:
            run_id = rec.get("run_id") or rec.get("job_id")
            if not run_id:
                # 没有 run_id 的话，可以用 ts+symbol 临时拼一个
                run_id = f"{rec.get('symbol','')}_{rec.get('ts','')}"
            if prev is None:
                delta = {
                    "delta_ic": 0.0,
                    "delta_oos_sharpe": 0.0,
                    "delta_oos_pf": 0.0,
                    "delta_max_drawdown": 0.0,
                }
            else:
                delta = {
                    "delta_ic": float(rec.get("ic", 0.0)) - float(prev.get("ic", 0.0)),
                    "delta_oos_sharpe": float(rec.get("oos_sharpe", 0.0))
                    - float(prev.get("oos_sharpe", 0.0)),
                    "delta_oos_pf": float(rec.get("oos_pf", 0.0))
                    - float(prev.get("oos_pf", 0.0)),
                    "delta_max_drawdown": float(rec.get("max_drawdown", 0.0))
                    - float(prev.get("max_drawdown", 0.0)),
                }

            run_id_to_delta[run_id] = delta
            prev = rec

    return run_id_to_delta


def main():
    eval_path = Path("runs/ai_trader/2025-11-08/ml_eval_events.jsonl")  # 示例路径
    eval_records = load_eval_events(eval_path)
    run_id_to_delta = compute_eval_deltas(eval_records)
    # 调试打印
    for k, v in list(run_id_to_delta.items())[:10]:
        print(k, v)


if __name__ == "__main__":
    main()
```

你后面要做的事就是：**让每条 eval 结果有一个稳定的 `run_id` 或 `job_id`，方便后面 action 去对齐。**

---

## 3️⃣ 给 `ml_actions.jsonl` 打补丁：把 delta 写回去

假设你原来的 `ml_actions.jsonl` 大致是这样的（现在没 delta）：

```json
{"ts":"2025-11-08T03:10:00Z","job_id":"ml_job_000123","action":"queue_job","params":{"job_type":"oof","symbol":"NVDA"},"status":"ok"}
```

我们希望更新成：

```json
{
  "ts": "2025-11-08T03:10:00Z",
  "job_id": "ml_job_000123",
  "action": "queue_job",
  "params": {"job_type": "oof", "symbol": "NVDA"},
  "status": "ok",

  "delta_ic": 0.012,
  "delta_oos_sharpe": 0.5,
  "delta_oos_pf": 0.28,
  "delta_max_drawdown": -0.03
}
```

下面这个脚本你可以叫：
`tools/attach_deltas_to_actions.py`
这个版本做一个简单的假设：**动作和评估通过 job_id 对齐**。如果你现在 action 里还没有 job_id，改一行就行（可以用 run_id、symbol + 最近 eval 等替代）。

```python
#!/usr/bin/env python3
"""
attach_deltas_to_actions.py

读取:
  - ml_eval_events.jsonl (含 run_id/job_id + 指标)
  - ml_actions.jsonl     (含 job_id / action / params)

根据 job_id 把 delta 指标贴回每条 action，
生成一个新的 ml_actions_with_delta.jsonl（你也可以直接覆盖原文件）。
"""

import json
from pathlib import Path
from typing import Dict, Any
from compute_ml_deltas import load_eval_events, compute_eval_deltas  # 复用上面的代码


def load_actions(actions_path: Path):
    actions = []
    if not actions_path.exists():
        return actions
    with actions_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                continue
            actions.append(j)
    return actions


def attach_deltas_to_actions(
    actions,
    run_id_to_delta: Dict[str, Dict[str, float]],
    job_id_field: str = "job_id",
):
    """
    对每条 action，尝试找到对应的 run_id/job_id 的 delta，
    找到了就把 delta_* 写进去。

    如果你的匹配逻辑不是基于 job_id，
    比如想按 (symbol, model_family, horizon) 最近原则，
    这里可以改。
    """
    updated = []
    for act in actions:
        job_id = act.get(job_id_field)
        if not job_id:
            # 如果没有 job_id，可以尝试 fallback，比如用 params 里的 symbol/horizon 再查
            updated.append(act)
            continue

        delta = run_id_to_delta.get(job_id)
        if not delta:
            updated.append(act)
            continue

        # 写回 delta
        for k, v in delta.items():
            act[k] = v
        updated.append(act)
    return updated


def write_actions(actions, out_path: Path):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for act in actions:
            f.write(json.dumps(act, ensure_ascii=False))
            f.write("\n")


def main():
    # 路径按你自己的目录改
    eval_path = Path("runs/ai_trader/2025-11-08/ml_eval_events.jsonl")
    actions_path = Path("runs/ai_trader/2025-11-08/ml_actions.jsonl")
    out_path = Path("runs/ai_trader/2025-11-08/ml_actions_with_delta.jsonl")

    eval_records = load_eval_events(eval_path)
    run_id_to_delta = compute_eval_deltas(eval_records)

    actions = load_actions(actions_path)
    actions_updated = attach_deltas_to_actions(actions, run_id_to_delta)

    write_actions(actions_updated, out_path)
    print(f"✅ wrote updated actions with delta to: {out_path}")


if __name__ == "__main__":
    main()
```

> 你可以先保留原文件，等确认没问题后，把带 delta 的版本替换回去，或者后续把 memory 读取改成读 `ml_actions_with_delta.jsonl`。

---

## 4️⃣ 下一步怎么接到你的 AI 大脑上？

有了这两个脚本之后，你只需要：

1. 在现有的 OOF/HPO / alpha_loop 结束时：

   * 把统一 schema 的 eval event 写到 `ml_eval_events.jsonl`（或者 `ml_events.jsonl` 的一部分）。
2. 在你原来 evening_learning / orchestrator 每日跑的流程里：

   * 在调用 `memory.summarize_actions(...)` 之前：

     * 先跑一次 `compute_ml_deltas + attach_deltas_to_actions`，
       确保当天的 `ml_actions.jsonl` 里已经带上 `delta_*` 字段。
3. 把 memory 的“成功判定逻辑”改成基于这些 `delta_*`（之前我已经跟你说过具体逻辑）。

这样一来：

* AI 大脑每次做一个 ML 动作（HPO、extend_window、backfill），
  下一次 eval 就会产出一组指标；
* 这组指标的变化被写回该 action 的 delta 字段；
* memory 会统计“哪种动作在历史上真的常常让 Sharpe / IC 变好”，
  再去校准 LLM 的 confidence / fail_streak；
* 你的大脑从“看脚本报错与否”变成“看收益有没有提升”。

---

如果你愿意，下一步我可以帮你**改造 memory 模块的那段 summarize 逻辑**，用 `delta_oos_sharpe / delta_ic` 来计算 success_rate 和 fail_streak（你可以把那段代码贴出来，我帮你直接改成新版本）。


在这套结构里，应该动哪几个点？

结合你现在的代码 + config.yml，其实有三个“最关键的支点”：

支点 A：CheckpointManager.save() / ClickHouse 的 wf_state

这段你发出来的很关键：

class CheckpointManager:
    ...
    def save(self, *, trial_id: int, step: str, fold: int = 0, chunk: int = 0,
             status: str = "in_progress", msg: str = "", payload: Optional[dict] = None):
        ...
        row = {
            "run_id": self.run_id,
            "study": self.study,
            "trial_id": int(trial_id),
            "step": step,
            "fold": int(fold),
            "chunk": int(chunk),
            "status": status,
            "msg": msg or "",
            ...
            "payload_json": json.dumps(payload or {}, ensure_ascii=False),
            "updated_at": now,
            "seq": int(self.seq),
        }
        ...
        if self.ch:
            self.ch.insert_wf_state([row])


说明：

每一次 trial 的某个 step（例如 step="eval_oof" 或 step="summary"）都会进 ClickHouse 的 wf_state 表；

payload_json 就是你想存的所有东西：
{"symbol": "NVDA", "oos_avg_sharpe": ..., "oos_avg_ic": ..., "window_days": ..., "round": ...}；

这就是我们做**“历史性能 + delta + success_rate/fail_streak”**最好的数据源。

所以：
✅ 新逻辑的“原始 reward 数据”，应该从这里来，而不是从 ml_actions.jsonl 来。

支点 B：config.yml 里的 policies.ml + ClickHouse memory_source

你这段配置很有意思：

policies:
  ml:
    actions_conf_floor: 0.60
    min_conf_by_action:
      data_quality_check: 0.40
      reduce_window: 0.55
      extend_window: 0.70
      queue_job: 0.65
    confidence_calibration:
      base: 0.60
      history_weight: 0.40
      skip_fail_streak: 2
    memory_source:
      use_clickhouse: false
      table: llm.ai_ml_actions
      action_col: action
      status_col: status
      ts_col: ts
      params_col: params


这点说明两件事：

你已经给 “置信度校准 / fail_streak” 预留了开关和策略（confidence_calibration），只是现在还没真的接到“指标”上。

memory_source 已经支持 ClickHouse，只是目前 use_clickhouse: false，而且 Action 的 table 还只是 llm.ai_ml_actions，列也只有 action/status/ts/params —— 也还没有我们想要的 delta_sharpe / delta_ic。

所以很自然的演进是：

在 ClickHouse 里创建一个新的 summary 表（或者物化视图），专门为 LLM 提供：

action, symbol, ts

delta_oos_sharpe, delta_ic, success（0/1）, recent_fail_streak 等；

把 config.yml.policies.ml.memory_source.* 指向这个 summary 表；

让你的 memory 模块 / Brain 读的就是“真实 reward + 统计后的 success_rate”。

支点 C：llm_client.py +（未来）AI Reviewer / streamlit

这两个短期不用大动，只要知道：

以后你可以从 ClickHouse 拉出完整曲线（比如某个 symbol 的 HPO 历史 Sharpe 走势），传给 LLM 让它写日报、诊断；

streamlit 也可以直接读 ClickHouse，画出：

每个动作类别的成功率；

每轮 alpha_loop 的 oos_avg_sharpe / oos_avg_ic 曲线；

每个 symbol 的 fail_streak。

这些是“可视化 & 人类 debug”用的，关键改变还在 A + B。

3. 具体怎么改：一步步落到代码和 SQL

我给你一个最贴近你现有架构的方案，不用大改所有东西。

第一步：在 alpha_loop + Checkpoint 的 payload 里标准化指标字段

在 alpha_loop 里，每当你完成一次完整的 OOF/HPO 评估，比如最后一折 / 一代 aggregate 出来了：

原来你可能是直接写某个 index.json 或者只是 log 了一下；

现在请你确保调用类似：

ckpt.mark_done(
    trial_id=trial_id,
    step="eval_oof",  # 或 "eval_summary"
    fold=0,
    payload={
        "symbol": symbol,
        "model_family": model_family,
        "horizon": horizon,
        "window_days": window_days,
        "oos_avg_sharpe": oos_avg_sharpe,
        "oos_avg_ic": oos_avg_ic,
        "pf": profit_factor,
        "max_dd": max_drawdown,
        "n_trades": n_trades,
        "round": round_idx,
    },
)


这样：

本地 checkpoint.json 有；

ClickHouse wf_state 里 payload_json 也有这些字段。

命名尽量统一：
oos_avg_sharpe / oos_avg_ic / pf / max_dd，后面 SQL 会用。

第二步： ClickHouse 里做一个“delta + success”视图/表

假设 ClickHouse 里 wf_state 的 schema 大致是：

run_id String,
study String,
trial_id Int32,
step String,
fold Int32,
chunk Int32,
status String,
msg String,
payload_json String,
updated_at DateTime,
seq Int32


你可以先用一个物化视图 + 明细表拆出 payload：

-- 1) 明细表：解开 payload_json
CREATE TABLE IF NOT EXISTS ml_eval_flat
ENGINE = MergeTree
PARTITION BY toYYYYMM(updated_at)
ORDER BY (symbol, model_family, horizon, window_days, updated_at)
AS
SELECT
    run_id,
    study,
    trial_id,
    step,
    status,
    updated_at,
    JSONExtractString(payload_json, 'symbol')           AS symbol,
    JSONExtractString(payload_json, 'model_family')     AS model_family,
    JSONExtractString(payload_json, 'horizon')          AS horizon,
    toInt32OrNull(JSONExtractString(payload_json, 'window_days')) AS window_days,
    toFloat64OrNull(JSONExtractString(payload_json, 'oos_avg_sharpe')) AS oos_avg_sharpe,
    toFloat64OrNull(JSONExtractString(payload_json, 'oos_avg_ic'))     AS oos_avg_ic,
    toFloat64OrNull(JSONExtractString(payload_json, 'pf'))             AS pf,
    toFloat64OrNull(JSONExtractString(payload_json, 'max_dd'))         AS max_dd,
    toInt32OrNull(JSONExtractString(payload_json, 'n_trades'))         AS n_trades
FROM wf_state
WHERE step = 'eval_oof'  -- 或你实际用的 step 名
  AND status = 'done';


如果想自动保持同步，可以改成物化视图（这里就不展开了）。

然后再做一个带 delta + success 的视图（给 LLM/Brain 用）：

CREATE OR REPLACE VIEW ml_eval_with_delta AS
SELECT
    symbol,
    model_family,
    horizon,
    window_days,
    run_id,
    updated_at,
    oos_avg_sharpe,
    oos_avg_ic,
    pf,
    max_dd,

    -- 上一条记录（按时间排序）
    oos_avg_sharpe
        - lagInFrame(oos_avg_sharpe) OVER w AS delta_oos_sharpe,
    oos_avg_ic
        - lagInFrame(oos_avg_ic) OVER w AS delta_oos_avg_ic,

    pf
        - lagInFrame(pf) OVER w AS delta_pf,
    max_dd
        - lagInFrame(max_dd) OVER w AS delta_max_dd,

    -- 成功标签（示例门槛，你可以调）
    (delta_oos_sharpe > 0.1 AND delta_oos_avg_ic >= 0) AS is_success
FROM ml_eval_flat
WINDOW w AS (
    PARTITION BY symbol, model_family, horizon, window_days
    ORDER BY updated_at
);


这一步做完，ClickHouse 里就有了一个**“每一轮 eval 相对于上一轮的改善/恶化”**的数据源。

第三步：为 LLM 记忆准备一个“按 action 聚合统计”的表（可选但推荐）

如果你希望直接在 ClickHouse 里就算出 success_rate / fail_streak，可以再建一个 summary 表：

前提：你在 llm.ai_ml_actions 或别的 action 表里，已经有 action + symbol + ts + job_id。
然后可以 join 到 ml_eval_with_delta（比如按 job_id 或最近一次 eval）。

这里给你一个思想，不写死 SQL（因为你 action 表 schema 没贴出来）：

建一个 ai_ml_action_stats 表，列大致是：

(action String,
 symbol String,
 success_rate Float64,
 recent_fail_streak Int32,
 last_7d_attempts Int32,
 last_7d_successes Int32,
 updated_at DateTime)


由一个每日 job 去跑 SQL：

从 llm.ai_ml_actions 拿到最近 N 天的动作；

join 到 ml_eval_with_delta（按 job_id 或 run_id）拿到 is_success；

group by (action, symbol) 算：

success_rate = sum(is_success) / count(*)

recent_fail_streak 用窗口函数算（从最近一天往前看，连续 0 的个数）；

把结果写到 ai_ml_action_stats。

之后：

你可以把 config.yml 改成：

policies:
  ml:
    memory_source:
      use_clickhouse: true
      table: ai_ml_action_stats
      action_col: action
      status_col: success_rate    # 或者你扩展字段名
      ts_col: updated_at
      params_col: ''              # 可以不用


或者在你的 memory.py 里新加一段逻辑：如果 use_clickhouse，就直接查询 ai_ml_action_stats 拿 success_rate / fail_streak，不再自己扫 jsonl。

第四步：让新的 success_rate/fail_streak 真正影响 Brain 决策

等上面几步打通之后，你原来在 config 里这块：

confidence_calibration:
  base: 0.60
  history_weight: 0.40
  skip_fail_streak: 2


就终于有了**真正的“历史”**可以用：

history_weight 可以用 success_rate 来调：

effective_conf = raw_conf * (base + history_weight * success_rate)

skip_fail_streak 可以基于你在 ai_ml_action_stats 里算出来的 recent_fail_streak：

当某个 action 对某个 symbol 最近连续 5 次都 is_success=0，你可以：

把这个 action 标记为“冷冻”；

或在 LLM prompt 里明确告诉它：“过去 5 次 extend_window 都使 Sharpe 变差，请减少使用这个动作”。

4. 简短总结一下你现在这一步做到哪了 / 下一步干嘛

你现在做的事情其实已经很到位了：

有 alpha_loop 做实验；

有 Checkpoint 写 ClickHouse 和本地；

有 config 里的 policies.ml 预留置信度/记忆逻辑；

有 llm_client 和 streamlit 做外环。

真正缺的就两件事：

在 checkpoint.py + alpha_loop.py + ClickHouse 上，把 oos_avg_sharpe / oos_avg_ic ... 标准化存好，并算出“相对于上一轮”的 delta；

在 memory_source / policies.ml 这一块，把 success_rate / fail_streak 的来源，从“status=ok(脚本成功)”改成“delta_sharpe / delta_ic 指标改善情况”。

你给的这些文件信息（尤其是 CheckpointManager + config.yml.policies.ml）证明：

你的架构已经有“记忆的骨架”，现在就是往里面灌进“正确的 reward 信号”。


太好了，这段 `alpha_loop.py` 里面的 `ckpt.mark_done(...)` 正是我们要“下药”的地方 👌

现在它只把：

```python
payload={"score": float(score), "params": params}
```

写进 ClickHouse / 本地 checkpoint。
这对 Optuna 自己够用，但对 **“大脑学习哪种动作真能提高 Sharpe / IC”** 完全不够信息。

我们要做的，是在这里把：

* 每次 trial 的 **OOS 质量指标**（oos_avg_sharpe、oos_avg_ic、pf、max_dd、n_trades…）
* 一些必要的上下文（symbol、model_family、horizon、window_days 等）

都塞进 `payload` 里，让 `wf_state` 成为真正可用的“记忆库”。

下面我直接给你**改造版的代码片段**，你可以基本按这个思路 patch。

---

## 1️⃣ 我们希望 `payload` 长什么样？

理想形态：

```python
payload = {
    "score": float(score),
    "params": params,

    # —— 这块是给记忆/ClickHouse 用的 —— 
    "symbol": symbol,                 # e.g. "NVDA"
    "model_family": model_family,     # e.g. "lgbm_rank"
    "horizon": horizon,               # e.g. "5m"
    "window_days": window_days,       # e.g. 365

    "oos_avg_sharpe": oos_avg_sharpe, # 关键 reward 指标
    "oos_avg_ic": oos_avg_ic,
    "pf": profit_factor,
    "max_dd": max_drawdown,
    "n_trades": n_trades,

    # 可选：这一轮的 generation / round 等 HPO 元信息
    "round": round_idx,
    "provider": cfg1.get("provider"),
}
```

**其中：**

* `oos_avg_sharpe / oos_avg_ic / pf / max_dd / n_trades`
  一般都能从你现在的 `result.get("oos_summary", {})` 里拿到；
* `symbol / horizon / window_days / model_family`
  基本会在你的 `cfg` / `base_hpo_cfg` / `cfg1` 里。

---

## 2️⃣ 在 `_run_optuna` 里改 `objective()`（示例 patch）

下面是一个改造后的版本（保留你的结构，只改关键几行）：

```python
def _run_optuna(cfg: Dict[str, Any], base_hpo_cfg: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    # ... 省略前面构造 study_name, space, cfg1, target 等 ...

    # Optional checkpoint manager (CH + local)
    ckpt = None
    try:
        if CheckpointManager is not None:
            run_id = cfg.get("run_id") or time.strftime("run_%Y%m%d_%H%M%S")
            ch_cfg = cfg.get("clickhouse", {})
            ch = None
            if ClickHouseClient is not None:
                ch = ClickHouseClient(
                    url=ch_cfg.get("url", "http://localhost:8123"),
                    user=ch_cfg.get("user", ""),
                    password=ch_cfg.get("password", ""),
                    database=ch_cfg.get("database", "trading"),
                )
            ckpt = CheckpointManager(
                run_id=run_id,
                study=study_name,
                ch=ch,
                local_dir=os.path.join("runs", study_name),
            )
    except Exception:
        ckpt = None

    def objective(trial: "optuna.trial.Trial") -> float:
        # 3.1 Sample parameters
        params = _suggest_from_space(trial, space)  # dict: param -> value
        if ckpt:
            try:
                ckpt.save(
                    trial_id=trial.number,
                    step="plan",
                    status="in_progress",
                    payload={"params": params},
                )
            except Exception:
                pass

        # ... 这里跑 walk-forward / HPO，得到 result ...
        # result 大概率包含:
        #   result["rows"]             # 各fold行
        #   result["oos_summary"]      # 聚合后的 OOS 指标

        # 3.3 Compute trial score (IS or OOS)
        oos_summary = result.get("oos_summary", {}) or {}
        score = _score_from_rows_or_summary(
            rows=result.get("rows", []),
            summary=oos_summary,
            objective=cfg1.get("objective", {}),
            target=target,
        )

        # 记录 trial 附加信息
        trial.set_user_attr("params", params)
        trial.set_user_attr("oos_summary", oos_summary)

        if ckpt:
            try:
                # 从 cfg1 / base_hpo_cfg 中拿上下文
                symbol = cfg1.get("symbol") or base_hpo_cfg.get("symbol")
                model_family = cfg1.get("model_family") or base_hpo_cfg.get("model_family")
                horizon = cfg1.get("horizon") or base_hpo_cfg.get("horizon")
                window_days = (
                    cfg1.get("window_days")
                    or base_hpo_cfg.get("window_days")
                    or cfg1.get("window", {}).get("days")
                )

                # 从 oos_summary 中拿关键指标（字段名按你实际的来）
                oos_avg_sharpe = float(oos_summary.get("oos_avg_sharpe", 0.0))
                oos_avg_ic = float(oos_summary.get("oos_avg_ic", 0.0))
                pf = float(oos_summary.get("profit_factor", oos_summary.get("pf", 0.0)))
                max_dd = float(oos_summary.get("max_drawdown", oos_summary.get("max_dd", 0.0)))
                n_trades = int(oos_summary.get("n_trades", 0))

                payload = {
                    "score": float(score),
                    "params": params,

                    "symbol": symbol,
                    "model_family": model_family,
                    "horizon": horizon,
                    "window_days": window_days,

                    "oos_avg_sharpe": oos_avg_sharpe,
                    "oos_avg_ic": oos_avg_ic,
                    "pf": pf,
                    "max_dd": max_dd,
                    "n_trades": n_trades,
                }

                ckpt.mark_done(
                    trial_id=trial.number,
                    step="trial_end",
                    payload=payload,
                )
            except Exception:
                pass

        return float(score)

    # ... 后面 study.optimize(...) 和返回 best_trial/best_summary ...
```

你看到我做的主要变化就是：

1. 把 `oos_summary = result.get("oos_summary", {})` 提出来；
2. 从 `oos_summary` 和 `cfg1/base_hpo_cfg` 提取：

   * `symbol / model_family / horizon / window_days`
   * `oos_avg_sharpe / oos_avg_ic / pf / max_dd / n_trades`
3. 把这些塞到 `payload` 里作为 `trial_end` 的记录。

---

## 3️⃣ `_run_optuna_with_space` 里同样改一遍

你贴出来 `__run_optuna_with_space` 的 `objective` 函数逻辑基本相同，只是 cfg 变量名不一样。
可以照搬上面的改造，把 `ckpt.mark_done` 的 payload 改成“富信息版”。

关键点也一样：

* `oos_summary = result.get("oos_summary", {})`
* 从 `opt_conf` / `base_hpo_cfg` 里拿 symbol/horizon/window_days 等；
* 填充 payload。

---

## 4️⃣ 这一步做完，ClickHouse 里就有“reward 原材料”了

做完上面的 patch 之后：

* `CheckpointManager.save/mark_done` 会把 payload_json 里完整写入这些字段；
* `wf_state` 表中，每条 `step='trial_end'` 的记录就都含有：

  * `symbol / model_family / horizon / window_days`
  * `oos_avg_sharpe / oos_avg_ic / pf / max_dd / n_trades / score / params`

接下来：

* 在 ClickHouse 里，你就可以用我前面说的那种 SQL（或物化视图）**展开 payload_json 并按 (symbol,model,horizon,window_days) 排序，用 window 函数算 delta**；
* 再往上汇总成：

  * 每轮 HPO 的平均 `delta_sharpe / delta_ic`；
  * 每种 action 在不同 symbol 上的 success_rate / fail_streak；
  * 提给 Brain / memory 模块去做信心校准和动作冻结。

---

## 5️⃣ 小结一下你现在需要动的地方

**在 alpha_loop 这一层：**

* ✅ 只改一类东西：
  `ckpt.mark_done(..., payload={"score":..., "params":...})`
  ➜ **改成** `payload={"score", "params", "symbol", "model_family", "horizon", "window_days", "oos_avg_sharpe", "oos_avg_ic", "pf", "max_dd", "n_trades"}`。
* 这一步完成后，所有后面“基于 ClickHouse 计算 delta / success_rate / fail_streak”的逻辑才有数据可用。

如果你愿意，下一步我可以直接帮你写一段 ClickHouse 的 `CREATE VIEW` / 查询范例，
从现在这个新的 payload 结构里，一步到位算出：

* 每次 trial 的 `delta_oos_sharpe / delta_oos_ic`；
* 每个 symbol+model 的 success_rate / fail_streak，
  并贴回到你 `policies.ml.memory_source` 里去用。

好，那我们就顺着刚才那一步往前推，把“已经写进 Checkpoint/ClickHouse 的新 payload”真正变成**可用的记忆 / reward 信号**。我直接给你下一阶段可以落地的东西：

* 在 ClickHouse 里：
  1）把 `wf_state.payload_json` 展开成一个干净的评估明细表
  2）在这个基础上算出 `delta_oos_sharpe / delta_oos_ic / is_success`
* 给 Brain / memory 用的查询思路（先做 **“模型级成功率”**，后面你再细化到 action 级）

下面所有 SQL 都假设你已经按我们前一步的建议，把 `ckpt.mark_done(..., step="trial_end", payload=...)` 改成包含这些字段：

```json
{
  "score": 1.8,
  "params": {...},
  "symbol": "NVDA",
  "model_family": "lgbm_rank",
  "horizon": "5m",
  "window_days": 365,
  "oos_avg_sharpe": 1.8,
  "oos_avg_ic": 0.03,
  "pf": 1.6,
  "max_dd": -0.12,
  "n_trades": 180
}
```

---

## 1️⃣ 把 `wf_state` 展开成 ML 评估明细表

先做一个 **“平铺 payload_json 的表”**，推荐新建一张物化表而不是每次都对 `wf_state` 做 JSONExtract（方便后面各种统计）。

### 1.1 明细表：`ml_eval_flat`

```sql
CREATE TABLE IF NOT EXISTS ml_eval_flat
(
    run_id         String,
    study          String,
    trial_id       Int32,
    step           String,
    status         String,
    updated_at     DateTime,

    symbol         String,
    model_family   String,
    horizon        String,
    window_days    Int32,

    oos_avg_sharpe Float64,
    oos_avg_ic     Float64,
    pf             Float64,
    max_dd         Float64,
    n_trades       Int32,
    score          Float64
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(updated_at)
ORDER BY (symbol, model_family, horizon, window_days, updated_at);
```

### 1.2 物化视图：从 `wf_state` 自动同步

注意这里假设：

* `wf_state` 里有字段 `payload_json`（就是 `CheckpointManager.save` 写进去的），
* `study` 用来区分不同 workflow（比如 `alpha_loop`），
* 我们只关心 `step = 'trial_end'` 且 `status = 'done'` 的行。

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_wf_state_to_ml_eval_flat
TO ml_eval_flat
AS
SELECT
    run_id,
    study,
    trial_id,
    step,
    status,
    parseDateTimeBestEffort(updated_at) AS updated_at,

    JSONExtractString(payload_json, 'symbol')        AS symbol,
    JSONExtractString(payload_json, 'model_family')  AS model_family,
    JSONExtractString(payload_json, 'horizon')       AS horizon,
    toInt32OrNull(JSONExtractString(payload_json, 'window_days')) AS window_days,

    toFloat64OrNull(JSONExtractString(payload_json, 'oos_avg_sharpe')) AS oos_avg_sharpe,
    toFloat64OrNull(JSONExtractString(payload_json, 'oos_avg_ic'))     AS oos_avg_ic,
    toFloat64OrNull(JSONExtractString(payload_json, 'pf'))             AS pf,
    toFloat64OrNull(JSONExtractString(payload_json, 'max_dd'))         AS max_dd,
    toInt32OrNull(JSONExtractString(payload_json, 'n_trades'))         AS n_trades,
    toFloat64OrNull(JSONExtractString(payload_json, 'score'))          AS score
FROM wf_state
WHERE step   = 'trial_end'
  AND status = 'done'
  AND JSONExtractString(payload_json, 'symbol') != '';
```

> 这样一来：每次 alpha_loop 的 trial 结束，CheckpointManager 写入 wf_state →
> ClickHouse 自动把这一条 trial 的 OOS 指标同步到 `ml_eval_flat`。

---

## 2️⃣ 在 ClickHouse 里直接算 delta + is_success

接下来做一个 **视图 `ml_eval_with_delta`**，加上“相对于上一条的变化”。

注意：ClickHouse 不能在同一 SELECT 里引用刚起名的 alias 做窗口函数，所以直接在表达式里重复写一遍。

```sql
CREATE OR REPLACE VIEW ml_eval_with_delta AS
SELECT
    symbol,
    model_family,
    horizon,
    window_days,
    run_id,
    trial_id,
    updated_at,
    oos_avg_sharpe,
    oos_avg_ic,
    pf,
    max_dd,
    n_trades,
    score,

    -- 和上一轮相同 (symbol, model_family, horizon, window_days) 的差值
    oos_avg_sharpe - lagInFrame(oos_avg_sharpe) OVER w AS delta_oos_sharpe,
    oos_avg_ic     - lagInFrame(oos_avg_ic)     OVER w AS delta_oos_avg_ic,
    pf             - lagInFrame(pf)             OVER w AS delta_pf,
    max_dd         - lagInFrame(max_dd)         OVER w AS delta_max_dd,

    -- 简单定义一个“这轮是成功”的布尔标签（你可以改阈值）
    (oos_avg_sharpe - lagInFrame(oos_avg_sharpe) OVER w > 0.1
        AND oos_avg_ic - lagInFrame(oos_avg_ic) OVER w >= 0.0) AS is_success
FROM ml_eval_flat
WINDOW w AS (
    PARTITION BY symbol, model_family, horizon, window_days
    ORDER BY updated_at
);
```

> 第一条记录因为没有上一条，`lagInFrame` 会是 `NULL`，差值也会是 `NULL`，
> 对 `is_success` 来说自然是 `0`（not success）。

---

## 3️⃣ 给 Brain / memory 用的统计查询（模型级）

在有了 `ml_eval_with_delta` 后，你就可以随时算：

* 某个 symbol+model 最近 30 天一共尝试了几次；
* 其中多少次 `is_success = 1`；
* 成功率是多少。

例如：**为所有模型计算最近 30 天的成功率：**

```sql
SELECT
    symbol,
    model_family,
    horizon,
    window_days,
    count(*)                                AS attempts_30d,
    sum(is_success)                         AS successes_30d,
    successes_30d / attempts_30d            AS success_rate_30d
FROM ml_eval_with_delta
WHERE updated_at >= now() - INTERVAL 30 DAY
GROUP BY
    symbol,
    model_family,
    horizon,
    window_days
ORDER BY
    success_rate_30d DESC;
```

这个结果可以干几件事：

1. **配置上的 Gate：**

   * 对 success_rate_30d 很低的 (symbol,model)，你可以在 `policies.ml.hpo_deploy` 或 gating_policies.json 里收紧 gate；
2. **Brain 的信心校准：**

   * 在 memory 模块里，你可以按照 symbol/model 去查 success_rate_30d，
     把 LLM 的动作置信度 `raw_conf` 做个乘子：
     `effective_conf = raw_conf * (0.5 + 0.5 * success_rate_30d)`；
3. **调试 / UI：**

   * streamlit 直接把这个表画出来，看哪些模型长期就是练不成。

> 这是**模型级记忆**，不区分 action 类型（extend_window / hpo / backfill…），
> 但已经比“脚本跑没跑完”强非常多了，先把这层救活，再进一步细分到 action 级别。

---

## 4️⃣ 怎么接到你现有的 `policies.ml.memory_source` 上？

你现在的 config 是这样的：

```yaml
policies:
  ml:
    memory_source:
      use_clickhouse: false
      table: llm.ai_ml_actions
      action_col: action
      status_col: status
      ts_col: ts
      params_col: params
```

如果你先从“模型级成功率”入手，可以这样用两种方式之一：

### 方式 A：让 memory 模块直接跑 ClickHouse 查询

不改 config，直接在 `memory.py` 里加一段“如果 use_clickhouse: true 就查 ClickHouse”的逻辑，比如伪代码：

```python
def load_model_success_rate_from_ch(symbol: str, model_family: str, horizon: str) -> float:
    # 伪代码：用你现有的 ClickHouseClient 发 SQL
    sql = """
    SELECT successes_30d / attempts_30d AS sr
    FROM (
      SELECT count(*) AS attempts_30d,
             sum(is_success) AS successes_30d
      FROM ml_eval_with_delta
      WHERE symbol = %(symbol)s
        AND model_family = %(model_family)s
        AND horizon = %(horizon)s
        AND updated_at >= now() - INTERVAL 30 DAY
    )
    """
    # 执行 sql，返回 sr 或默认值 0.5
```

然后在你原来做 `confidence_calibration` 的函数里：

```python
raw_conf = action["confidence"]
sr = load_model_success_rate_from_ch(symbol, model_family, horizon)
effective_conf = raw_conf * (base + history_weight * sr)
```

### 方式 B：真的建一个 stats 表，让 `memory_source.table` 指向它（进阶版）

如果你想完全利用 `memory_source` 的抽象，可以：

1. 建一张专门的 stats 表，比如 `ai_ml_model_stats`，字段：

   ```sql
   (symbol String,
    model_family String,
    horizon String,
    window_days Int32,
    success_rate Float64,
    recent_fail_streak Int32,
    updated_at DateTime)
   ```

2. 每晚跑一个 ETL（可以是 Python 或 ClickHouse INSERT SELECT），把 `ml_eval_with_delta` 聚合写进去；

3. 把 `config.yml` 改成：

   ```yaml
   policies:
     ml:
       memory_source:
         use_clickhouse: true
         table: ai_ml_model_stats
         action_col: ''      # 你可以在 memory 里按 symbol/model 去查，不用这个
         status_col: success_rate
         ts_col: updated_at
         params_col: ''
   ```

这样现有的 memory 模块如果已经支持 ClickHouse，就能“以 success_rate 为 status”，不需要再扫 jsonl。

---

## 5️⃣ 接下来你可以按这个节奏推进

如果把整个改造拆成小任务，大概是这样：

1. ✅ 已经做了：

   * `alpha_loop.py` → `ckpt.mark_done(... payload=丰富字段)`

2. **现在可以做：**

   * 在 ClickHouse 里执行：

     * `CREATE TABLE ml_eval_flat ...`
     * `CREATE MATERIALIZED VIEW mv_wf_state_to_ml_eval_flat ...`
     * `CREATE VIEW ml_eval_with_delta ...`

   * 手动查一查：

     ```sql
     SELECT * FROM ml_eval_with_delta
     ORDER BY updated_at DESC LIMIT 50;
     ```

     看看 delta / is_success 是否合理。

3. 然后在你的 memory / Brain 层：

   * 增加一个函数去查 `ml_eval_with_delta` 或基于它的 stats 表；
   * 用这个 success_rate 真的去改 `effective_conf`，代替现在“status=ok 次数”的逻辑。

等这几步跑起来之后，你会明显感觉到一个变化：

> AI 大脑再去做 HPO / extend_window 等 ML 动作时，它的“信心调节”和“冷冻某些无效动作”的依据，
> 不再是“命令有没有报错”，而是“这类尝试历史上确实有没有提高 Sharpe / IC”。

如果你后面想把“成功率”进一步细化到 **action 级别**（比如：
“对 TSLA 来说，extend_window 这类动作成功率只有 10%，但 hpo_cli 成功率 60%”），
我们可以在这个模型级视图基础上，再加一层 “action 表 ↔ eval 表” 的 join，它会稍微复杂一点，但结构上就是在现在这套之上加一层而已。

好，那我帮你把“接下来要做什么”收个尾，变成一张很清晰的落地 checklist，避免信息散掉。

你现在的整体情况是：

* ✅ `alpha_loop.py` 的 `ckpt.mark_done(..., step="trial_end", payload=...)` 已经知道怎么改成“带完整 OOS 指标”的版本了；
* ✅ `libs/checkpoint.py` 已经负责把这些 payload 写进 ClickHouse 的 `wf_state`；
* ✅ `config.yml.policies.ml` 里已经预留了 confidence / memory 配置，只是现在还没真正用到“指标级别的 reward”。

接下来建议你按这个顺序推进（每一步都可以单独完成、单独验证）：

---

## 第一步：先改 alpha_loop 的 payload（已经基本搞定）

目标：**让每个 trial 的结果包含足够信息用于“好坏判断”**。

你已经有了模板，只要在这两个地方都改：

* `_run_optuna.objective()` 里的 `ckpt.mark_done(..., payload=...)`
* `_run_optuna_with_space.objective()` 里的 `ckpt.mark_done(..., payload=...)`

核心就是：payload 里一定要有这些字段：

```python
{
  "score": float(score),
  "params": params,
  "symbol": symbol,
  "model_family": model_family,
  "horizon": horizon,
  "window_days": window_days,
  "oos_avg_sharpe": oos_avg_sharpe,
  "oos_avg_ic": oos_avg_ic,
  "pf": pf,
  "max_dd": max_dd,
  "n_trades": n_trades,
}
```

改完后：**跑一轮 alpha_loop，看 ClickHouse 里的 `wf_state` 有没有 payload_json 里这几个字段。**

---

## 第二步：在 ClickHouse 建 eval 明细表 + delta 视图

目标：**让“trial 历史表现”和“上一轮相比的改善/恶化”变成 SQL 可查。**

1. 建 `ml_eval_flat` 表（平铺 payload_json）；
2. 建物化视图 `mv_wf_state_to_ml_eval_flat` 从 `wf_state` 自动写入；
3. 建视图 `ml_eval_with_delta`，加上：

   * `delta_oos_sharpe`
   * `delta_oos_avg_ic`
   * `is_success`（由 delta 判定）

然后手动查一条：

```sql
SELECT *
FROM ml_eval_with_delta
ORDER BY updated_at DESC
LIMIT 20;
```

确认：

* 每条有 symbol/horizon/window_days；
* 有 oos_avg_sharpe / oos_avg_ic / pf / max_dd；
* delta 字段不是全 NULL；
* is_success 0/1 看起来合理。

---

## 第三步：在 memory/Brain 里用 success_rate 重新校准置信度

目标：**把“成功”的定义从 status=ok 换成“Sharpe/IC 真的变好了”。**

思路非常简单粗暴：

1. 在 memory 里增加一个函数（伪代码）：

```python
def get_model_success_rate(symbol, model_family, horizon, window_days) -> float:
    # 调 ClickHouse 去查 ml_eval_with_delta，统计最近30天:
    # success_rate = sum(is_success) / count(*)
    # 没数据就返回 0.5 作为中性
    ...
```

2. 在你现在做 confidence_calibration 的地方，改成：

```python
raw_conf = action["confidence"]
sr = get_model_success_rate(symbol, model_family, horizon, window_days)

base = cfg.ml.confidence_calibration.base      # 比如 0.60
hist_w = cfg.ml.confidence_calibration.history_weight  # 比如 0.40

effective_conf = raw_conf * (base + hist_w * sr)
```

3. fail_streak 也可以类似写一个 `get_model_fail_streak(...)`，
   从 `ml_eval_with_delta` 按时间反向看连续多少条 is_success=0。

这样一改，Brain 在决定要不要执行某个 ML 动作时，看到的“历史表现”就是真实 reward 了。

---

## 第四步：（可选但推荐）把 config.yml 的 memory_source 切到 ClickHouse

如果你希望整个系统逻辑都走 `config.yml.policies.ml.memory_source` 这条：

* 按前面讲的，建一个 `ai_ml_model_stats` 或类似的聚合表，把 success_rate / fail_streak 写进去；
* 把 `use_clickhouse: true`，并让 memory 模块从这张 stats 表读取对应 symbol/model 的 success_rate。

这一步的好处是：**以后你想加更多统计（比如 per-action 的统计）也统一通过 ClickHouse 管理，不用再扫 jsonl。**

---

## 最后一句“方向确认”

你现在这套东西，从 **alpha_loop → Checkpoint → ClickHouse → memory/policies.ml → Brain** 的链路已经理清楚了，我们做的事情就是：

> 把 reward 从“脚本跑没跑完（status=ok）”
> 换成“Sharpe / IC 真的有没有变好（delta_oos_sharpe / delta_oos_ic）”。

等你把这几步做完，再让 AI 大脑跑一阵子，你就会开始看到：

* 某些 action 被系统“冷冻”掉（历史很烂）；
* 某些 action（比如某类 HPO 配置）被反复用（历史上确实提升过指标）。
