# AI 大脑与 MLOps 工作流权威指南 (HOW_TO_USE_AI_BRAIN_CN.md) - v4.0

本文档是本交易系统“AI 大脑”与 MLOps 工作流的权威指南。它将遵循从“研究”到“生产”的完整生命周期，详细阐述各组件的内部机制、协作方式和最佳实践。

## 1. 从研究到生产：全生命周期概览

本系统的核心思想是构建一个自动化的“研究-优化-部署-监控”的闭环。整个生命周期可以概括为以下几个阶段：

1.  **研究与发现 (`pi_autopilot`)**: 由“首席科学家”LLM 从宏观目标出发，提出全新的特征工程和模型训练方案。
2.  **优化与验证 (`alpha_loop` + `wf_hpo`)**: 使用 `Optuna` 等工具，对候选方案进行严格的步进式交叉验证 (Walk-Forward HPO)，在历史数据上找到表现最优的参数组合 (best trial)。
3.  **离线特征工程 (`nightly_oof_flow`)**: 将验证过的模型，通过自动化的夜间流水线生成样本外 (OOF) 预测，作为稳定的 `ml_signal` 特征存入 ClickHouse 数据库。
4.  **参数化与部署 (`control_event_consumer`)**: HPO 或 OOF 流程成功后，事件驱动的消费者会自动进行非回归测试，并将最优参数（如模型权重、策略参数）安全地更新到生产配置文件中。
5.  **上线与 A/B 测试 (`ab_deployer`, `ab_weight_adjuster`)**: 新模型作为“挑战者”(Challenger) 与现有的“冠军”(Champion) 模型并行上线，系统会根据它们的实际表现动态调整权重。
6.  **实盘运行与多层风控 (`EnhancedModelSignalStrategy`)**: 在实盘中，策略本身集成了自适应引擎、市场过滤器、RL 调仓和 Guardian 守护者等多层风控，确保交易的安全与稳定。

---

## 2. 核心组件深度解析

### 2.1. `EnhancedModelSignalStrategy`: 多层风控的“超级策略”

这是系统中最核心的交易执行策略，其内部决策逻辑是一个精密的多层防护体系。

#### 决策优先级：硬否决优先，数值缩放叠加

1.  **硬否决层 (Hard Gates)**: 如果触发，交易将被直接拒绝。
    *   **市场过滤器硬门**: VIX 指数过高、市场流动性不足、或处于禁止交易的时段。
    *   **Guardian 停牌**: 如果 Guardian 模块处于 `halt` 状态（可能由自适应引擎触发），所有新交易都会被阻止。

2.  **数值缩放层 (Scaling Factors)**: 如果未被硬否决，目标仓位会经过一系列乘法因子的顺序缩放。
    *   **基础仓位计算**: 根据 `VolTarget` (波动率目标) 或 `ConfidenceScaled` (置信度加权) 等方法计算一个初始仓位。
    *   **自适应引擎缩放 (`sizer_scale`)**: 根据宏观市场状态（如 VIX 状态）对仓位进行初步放大或缩小。
    *   **市场过滤器软缩放 (`mf_size_scale`)**: 当模型置信度偏低但未触发硬门槛时，进行小幅缩量。
    *   **RL 调仓缩放**: RL 模型根据更精细的实时状态，对仓位进行进一步的动态调整。
    *   **Guardian 全局缩放**: Guardian 的 `reduce` 策略会根据账户级别的风控状态（如整体回撤）对仓位进行最终的全局缩放。

3.  **最终裁决**:
    *   **Guardian 清仓 (`flat`)**: Guardian 的“清仓”指令拥有**最高优先级**，可以无视任何其他模块，在任意时刻将仓位清零并阻止所有新交易。

### 2.2. 离线 OOF 特征：生产环境的推荐路径

在生产环境中，推荐使用离线生成的 OOF (Out-of-Fold) 预测作为交易信号，因为它最稳定、可复现且易于审计。

#### OOF 生成管道 (`pipelines/lstm_oof_to_clickhouse.py`)

这是一个具体的 OOF 生成脚本，其内部流程如下：

1.  **拉取数据**: 从源数据表（如 `trading.features_1m`）拉取所需的数据。
2.  **时间序列切分**: 使用 `TimeSeriesSplit` 将数据切分为多个训练/验证折（Folds），并支持 `embargo`（禁区）设置，严格防止数据泄漏。
3.  **循环训练与预测**: 在每个 Fold 内，训练模型并对 OOF 部分进行预测。
4.  **聚合与置信度计算**: 聚合所有 Folds 的 OOF 预测，并计算置信度。
5.  **入库**: 将带有 `run_id` 和 `model_ver` 的 OOF 预测结果 `upsert` 到目标 ClickHouse 表中（如 `trading.oof_lstm_h5`）。

**运行示例**:
```bash
# 为 TSLA 在指定时间范围内，使用 60 分钟序列长度、4 折交叉验证、10 分钟禁区、训练 2 个 epoch 来生成 OOF 特征
python pipelines/lstm_oof_to_clickhouse.py \
    --symbol TSLA \
    --start '2025-11-05 09:30:00' \
    --end '2025-11-06 16:00:00' \
    --src-table trading.features_1m \
    --oof-table trading.oof_lstm_h5 \
    --seq-len 60 \
    --folds 4 \
    --embargo 10 \
    --epochs 2
```

### 2.3. `llm_trader`: 灵活的 LLM 直驱交易框架

`llm_trader` 是一个与 HPO 路径互补的、独立的实时交易框架。

-   **职责**: 它不负责 HPO 或复杂的模型训练，而是直接基于 LLM 的实时推理进行交易。它内置了数据聚合、时间管理、记忆系统和组合管理等功能，适合执行短线的、事件驱动的或战术级的交易。
-   **协作模式**:
    -   **补充线**: 在 HPO 主策略不活跃的时段（如盘前、盘后）执行特定任务。
    -   **实验线**: 作为一个低权重的“实验组”，与 HPO 主策略（作为“对照组”）并行运行，用于快速验证新的交易思想。
-   **参考文档**: `llm_trader/LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md`

### 2.4. A/B 部署与动态权重调整

这是连接研究与生产的关键环节，确保了新模型能够被安全、自动地部署和评估。

1.  **生成 A/B 方案 (`deploy/ab_deployer.py`)**:
    当一个 HPO 流程成功后，`ab_deployer.py` 脚本会介入。它会比较新出炉的“挑战者”和现有的“冠军”的性能指标，并根据预设规则（如 Sharpe、MDD 的比例）生成一个初始的 A/B 权重方案，例如 `{champion: 0.7, challenger: 0.3}`，并更新 `champion_challenger.json` 注册表。

2.  **动态权重调整 (`ab_weight_adjuster.py`)**:
    系统上线后，`ab_weight_adjuster.py` 脚本会**定期运行**。它会持续监控“冠军”和“挑战者”在**近期实盘（或高质量模拟）**中的表现，并根据滚动结果，以“微步”的方式（如每次调整不超过 5%）动态微调 A/B 权重。如果“挑战者”表现持续不佳或触发风控，权重会被降级，甚至完全回退到 100% 的“冠军”。

3.  **下游消费**:
    A/B 权重主要通过两种方式被消费：
    *   **配置读取**: `llm_trader` 或其他策略可以直接读取 ClickHouse 中的 `llm.ai_brain_plans` 表或本地配置文件，根据权重来限制自身的风险敞口。
    *   **系统路由**: 更上层的交易网关或订单分配服务，可以根据 A/B 权重，按比例分配资金或订单流到不同的策略实例上。

---

总体定位

  - AI 大脑 = PrincipalInvestigator（PI），负责“研究员/规划器”角色：拉取元数据→让 LLM 产出“特征配方+训练计划”→生成安全 SQL 抽取
    一份可训练的数据帧→给 HPO 一份可控的搜索网格。
  - 训练/回测的执行者是 WalkForwardHPO + 回测引擎；PI 不直接训练模型，而是规定“用什么数据、目标、候选模型与参数”，再交给 WF/HPO
    做严谨评估。

  核心流程

  - 元数据与提示词构造
      - 读取 ClickHouse 表字段目录，构造 LLM 提示（目标、表/时间窗、特征目录、预测窗口）。
      - 代码：pi/pi_orchestrator.py:31, pi/pi_orchestrator.py:48
  - LLM 产出方案
      - 返回 JSON：feature_recipe（选表/基础特征/派生特征/清洗）+ training_plan（symbol、task、target、horizon、
        model_candidates、decision）。
      - 代码：pi/pi_orchestrator.py:62
  - 安全 SQL 生成与取数
      - 用 feature_recipe + target 生成 SELECT（白名单/标识符校验），批量取出 dataset_df。
      - 代码：pi/pi_orchestrator.py:67
  - HPO 网格构建与护栏
      - 从 training_plan.model_candidates 填充：
          - model_type（如 xgb/lstm/transformer…）
          - feature_subset（base_features + derived aliases）
          - task、decision（量化或阈值）
          - model_params（每个候选的超参）
      - 经 grid_guard 收敛参数范围+数量（防爆炸）。
      - 代码：pi/pi_orchestrator.py:80, pi/pi_orchestrator.py:92
  - 交给 WalkForwardHPO
      - alpha_loop “pi_autopilot” 分支把 dataset_df + hpo_grid、model_task/target 写入 hpo_cfg，指定策略
        （ModelSignalStrategy），执行走步验证（IS/OOS/embargo/PIT）。
      - 代码：alpha_loop.py:1320

  “凭什么选择模型”

  - 候选来源
      - PI/LLM 在 training_plan 里给出 model_candidates（如 XGBoost、LSTM、Transformer 族），以及对应 model_params 初值。
  - 选择依据
      - 实际选择不是“凭直觉”，而是 WF/HPO 客观打分：
          - 对每个 trial（可能包含不同模型、特征子集、决策规则、超参），在各折上训练/预测→回测→聚合 IS/OOS 指标。
          - 目标函数（Sharpe/Sortino/Calmar 或自定义公式/IC）+ 约束（min_trades/max_mdd）明确了“好”的定义。
  - 最终赢家
      - 使用 Optuna（推荐）或网格搜索在同一时间窗/同一折分上比较，最优 trial 即“被选中”的模型+参数+特征。
      - 代码：wf_hpo.py:606（模型分发），wf_hpo.py:620（fit/predict/注入信号），alpha_loop.py:720（Optuna目标函数调用 WF）

  “为什么选择某个模型/股票/回测窗口”

  - 模型选择（策略性理由）
      - XGBoost：数据表征稳定、特征工程充足、计算预算有限时的强基线；易审计可复现。
      - LSTM：短中期时序结构明显、lookback 32–64 可捕捉动量/均值回归型信号。
      - Transformer/ETSformer/iTransformer：要素交互/多因子/非平稳时序更强，计算更重；适合中等规模特征集的深度挖掘。
      - 最终以 OOS 目标胜负论英雄，而非先验。
  - 股票选择
      - 配置给定（Enhanced 策略可多符号，PI 当前一次只处理 1 个 symbol）。机构常选：
          - 高流动性/稳定微结构（更可靠的成交/点差/滑点估计）
          - 能代表 Desk 的核心票，并在小规模上先验证可复制性
      - 代码：alpha_loop.py:1351（PI 取 symbol），hpo 配置的 symbols（Enhanced）
  - 回测窗口
      - 由 fold_generator 控制（start/end/is_days/oos_days/step_days/embargo），符合 WF 的再现性与防泄漏。
      - 机构实践：覆盖最近 1–3 个月的滚动窗口，OOS 分层覆盖多个市场状态；Embargo 避免标签泄漏。
      - 代码：wf_hpo.py:106（make_folds）

  WF/HPO 如何执行机器学习

  - 单折数据切分与特征构建
      - 从 adapter 取数或用 PI 给的 dataset_df，确保 ts 时区一致、PIT 校验（feature_cutoff_ts）。
      - 代码：wf_hpo.py:520（加载/验证），backtest_ml_engine.py:389（PIT）
  - 模型训练与预测（in-loop）
      - 参数中存在 model_type 才会启用 in-loop 训练（否则走“消费 OOF”的策略路径）。
      - 已支持：xgb / lstm / transformer / etsformer / itransformer。
      - 自动补标签：若缺少 model_target_col，用未来收益（close）回填（支持回归/二分类）。
      - 代码：wf_hpo.py:566（开关），wf_hpo.py:574（目标列兜底），wf_hpo.py:606（模型映射）
  - OOS 预测对齐
      - 序列模型根据 lookback 先拼接 IS 尾部到 OOS 开头，避免错位。
      - 代码：wf_hpo.py:704
  - 信号注入与回测
      - ml_pred/ml_signal/ml_conf 注入数据帧，交策略（Enhanced）走回测。
      - 代码：wf_hpo.py:632, wf_hpo.py:716；策略：adapters/enhanced_model_adapter.py:1

  护栏与可运维性

  - 防爆炸
      - grid_guard 收敛 hpo.grid；Optuna 代替全组合；MedianPruner 早停；workers 强制=1；BLAS 线程限速。
      - 代码：alpha_loop.py:617–621（线程限速），alpha_loop.py:655–665（Pruner）
  - 防泄漏
      - PIT 校验、embargo 天数。
      - 代码：wf_hpo.py:383（embargo），backtest_ml_engine.py:389（PIT）
  - 资源与稳定性
      - 内存日志/Crash Hook；LSTM/Transformer 的序列数据集改为“按需切片”，避免一次性物化窗口导致 OOM。
      - 代码：alpha_loop.py:49（faulthandler），models/lstm_model.py:1（内存友好数据集）

  推荐组织级实践

  - 生产默认：provider=optuna
      - 在 hpo.optuna_space 同时放入 xgb/lstm/transformer 候选，让“模型选择”由 OOS 目标说话；保存 study 到 DB 以便审计与断点
        续跑。
  - 加速（有 LLM 预算）：provider=coop_optuna
      - LLM/ML 先缩窄邻域，再由 Optuna 细化；有来源标注（LLM/ML/BASE/BOTH）。
  - 自举新资产：provider=pi_autopilot
      - 产出候选方案（特征/目标/模型/初网格），然后转回 optuna 做 WF 严格验证（通过指标门槛后上线）。
  - 验收门槛（示例）
      - WF OOS：Sharpe≥Desk 阈值、PSR≥0.7、PBO 不显著、OOS 交易数≥最小值、MDD≤风险上限。
      - 从 CH 拉字段目录 → 让 LLM 产出 feature_recipe/training_plan → 生成 SQL → 取出 dataset_df → 构造 hpo_grid → 交给
        WalkForwardHPO。
  - 若要让“模型竞争”
      - 在 training_plan.model_candidates（由 LLM 返回）中同时列出 xgb/lstm/transformer 及其 model_params；或在 optuna_space 里
        给多个 model_type 候选（非 PI 路径）。

  排障要点

  - 卡在“加载数据”前：优先确认 CH 端口与客户端；增强适配器已支持 8123/9000 自动回退。
  - OOM：确认使用“按需切片”的序列数据集（已改）；先用小 epochs/batch_size/lookback；禁用 Kafka（DISABLE_EVENT_BUS=1）。
  - 没有模型训练：检查 HPO 参数是否包含 model_type；否则就是“消费 OOF”的 Enhanced 路径（不 in-loop 训练）。策略优先级（Enhanced）

  - 信号合成
      - 由多模型集成器先合成信号与置信度（weighted_avg/vote/max_conf）。
      - 代码: strategies/enhanced_model_signal_strategy.py:598
  - 自适应引擎覆盖
      - 返回 overrides：如 conf_floor、sizer_scale、exec_mode、halt_for_sec 等。
      - 代码: strategies/enhanced_model_signal_strategy.py:318, 960
  - 市场过滤器（硬门+软缩放）
      - should_trade → (allow, reason, mf_size_scale)
      - conf 低于 conf_floor/soft_threshold 时不否决，但下发 mf_size_scale<1（软缩量）。
      - 点差/成交量/VIX/交易时段为硬门；失败直接拒单。
      - 代码: strategies/enhanced_model_signal_strategy.py:440, 468, 477, 537
  - Guardian（强优先级：停牌/降档/清仓）
      - 预交易时若处于 halt 窗口，直接拒单；可被 adaptive 的 halt_for_sec 触发。
      - 后续会在提交前再检查 trip：可 reduce/halt/flat（flat=清仓并拒绝新单）。
      - 代码: strategies/enhanced_model_signal_strategy.py:1121, 1561, 1607
  - 仓位计算（顺序叠加）
      - Base sizing（Fixed/ConfidenceScaled/VolTarget/Kelly）→ target_qty
          - 代码: strategies/enhanced_model_signal_strategy.py:670, 1146
      - Adaptive 大小缩放：sizer_scale（默认 clamp 至 [0,3]）
          - 代码: strategies/enhanced_model_signal_strategy.py:1166
      - 市场过滤器软缩放：mf_size_scale（置信度偏低）
          - 代码: strategies/enhanced_model_signal_strategy.py:1172
      - RL 缩放（带“最大头寸/最大步进”安全约束）
          - 代码: strategies/enhanced_model_signal_strategy.py:1420, 1464
      - Guardian 全局缩放（reduce 策略）
          - 代码: strategies/enhanced_model_signal_strategy.py:1184
      - 可选微结构确认（SFS）与 Beta 对冲（低置信度时）
          - 代码: strategies/enhanced_model_signal_strategy.py:1201, 1223
  - 提交流程
      - 再次进行 Guardian trip 检查（可能调整/阻断）→ 决策单封装/提交（MKT/LMT、可携带 OCO/执行Hint）。
      - 代码: strategies/enhanced_model_signal_strategy.py:1255, 1293
  - 总结优先级
      - 硬否决层（先后）：市场过滤器硬门/守护器 halt > 其它模块
      - 数值缩放层：Adaptive sizer_scale → MF 软缩 → RL 缩放 → Guardian 缩放
      - 任何时刻 Guardian trip 可降档/停牌或清仓；其“阻断/清仓”优先级最高

  离线 OOF 特征（生产推荐路径）

  - 目的
      - 定期生成 Out-of-Fold 预测，作为 ml_signal/ml_conf 写回 ClickHouse，策略上线消费（可复现、稳定、审计友好）。
  - LSTM OOF 管道
      - 入口: pipelines/lstm_oof_to_clickhouse.py:1
      - 流程:
          - 拉取时段数据（ts/symbol/label+序列列），严格时间顺序；pipelines/lstm_oof_to_clickhouse.py:104
          - TimeSeriesSplit 做折分；每折训练→验证（OOF）；支持 embargo（时间禁区避免泄漏）；pipelines/
            lstm_oof_to_clickhouse.py:168, 253
          - 聚合各折 OOF 预测并估 conf（幅度归一/百分位）；pipelines/lstm_oof_to_clickhouse.py:214
          - upsert 到 ClickHouse 指定表（带 run_id/model_ver）；pipelines/lstm_oof_to_clickhouse.py:191
      - 运行示例:
          - python pipelines/lstm_oof_to_clickhouse.py --symbol TSLA --start '2025-11-05 09:30:00' --end '2025-11-06 16:00:00'
            --src-table trading.features_1m --oof-table trading.oof_lstm_h5 --seq-len 60 --folds 4 --embargo 10 --epochs 2
  - 在线消费映射
      - Enhanced 适配器会将存在的 OOF 列映射为策略消费的 ml_signal/ml_conf；
      - 代码:
          - adapters/enhanced_model_adapter.py:1（取数 LEFT JOIN features_1m）
          - backtest_ml_engine.py:412（自动映射 oof 列至策略通用列；也可从 lstm_oof_、xgb_oof_ 映射）
  - In-loop 训练（可选）
      - WF 内支持 xgb/lstm/transformer/etsformer/iTransformer 训练+预测→注入 ml_signal/ml_conf → 回测；通过 optuna_space 搜索模
        型/特征/超参/决策。
      - 代码: wf_hpo.py:606, 620, 704

  llm_trader 的角色

  - 职责
      - 另一条“实时 LLM 驱动”的交易线，适合短线/盘前/事件驱动；不负责 HPO/WF，它直接基于 LLM 提示与上下文做交易决策，内置数据聚
        合、时间管理、记忆系统与组合管理。
      - 文档: llm_trader/LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md:1
  - 运行模式
      - run_single_bot.py 启动单 provider 实盘/模拟；支持多 provider 竞速（run_llm_race.py）与早盘/盘前脚本。
      - 典型运行:
          - python llm_trader/run_single_bot.py --provider openai --symbols TSLA AAPL --cycles 30 --interval 30 --enable-v2
  - 与 Enhanced/HPO 的协作
      - Enhanced + HPO 路径：以 WF+OOF+Optuna 为主，审计/复现最佳；适合产出稳定参数与策略。
      - llm_trader：作为补充线（盘前/事件/战术级），也可按 AB 权重作为小权重试验线；两者通过上层路由或资金分配协同。

  A/B 部署与下游消费

  - 产出
      - HPO 成功后，会生成/更新 champion 与 challenger 的注册（包含策略/参数/权重建议）。
      - 简化工具:
          - scripts/ai_trader/deploy/ab_deployer.py:1（基于 Sharpe/MDD 比例门槛给出 70/30 等方案）
          - scripts/ai_trader/ab_weight_adjuster.py:1（滚动收集近 n 天 champion/challenger 的表现，按规则与 TAMS 限制微调权重）
  - 消费
      - 典型有两种：
          - llm_trader 线：在 llm.ai_brain_plans（ClickHouse 表）或本地配置中记录 ab_champion/ab_challenger，bot 按权重投递或在
            PortfolioManager 层按权重限制敞口。
          - 系统路由：交易网关/订单分配服务读取 AB 权重，将订单或资金分配到 champion 与 challenger 策略实例（资本或订单比例）。
      - AB 权重调整遵循“微步”与回滚规则（默认每次最多 5%）；异常（如 MDD 超阈）时降档或回退至 100% champion。
  从研究到生产的全链路

  - 研究/HPO
      - provider=optuna 路径验证多模型/特征/决策→WF OOS 达标；产出最佳 trial + 产物/指标/可复现实验记录（Optuna study）。
  - 生产参数化
      - 将最佳 trial 写入策略配置（Enhanced extras）与系统配置（如风险/OCO/限价策略等）。
      - 生产策略消费 OOF（建议）或走 in-loop（备选）。
  - 上线与 A/B
      - 初期小权重作为 Challenger 与现 Champion 并行；观察 OOS/实盘指标（Sharpe/PSR/DSR/PBO/交易数/回撤）。
      - 达标后提升权重或轮换为新的 Champion；不达标回滚。
  - 运行与风控
      - 市场过滤器、Adaptive、RL、Guardian、SFS/对冲、OCO 保护形成“多层防护”；所有操作有事件与仪表盘可审计。



已完成“xgb + lstm + 三种 Transformer 同台竞技”的顺序化、一机一模运行方案，并提供了可直接执行的脚本，确保每次只跑一个
  模型，按顺序跑完并输出所选模型与 OOS 指标和产物路径。

  如何运行

  - 顺序跑 5 个模型（每个 1 trial，互斥、顺序执行）
      - scripts/run_enhanced_model_sweep.sh config/alpha_config_enhanced_debug.json alpha_out/enhanced_sweep
  - 说明
      - 每个模型单独生成临时配置（将 optuna_space.model_type 置为单一 choice，trials=1，workers=1）
      - 针对当次模型会过滤 model_params 选项（避免拿到不匹配的参数集）
      - 逐模型运行 alpha_loop（provider=optuna）
      - 每次运行结束后打印该模型的 OOS 指标与产物路径
  - 输出示例
      - 每个模型都会输出一行 JSON，包含：
          - model_run（本次跑的模型）
          - best_model_type（从 best_params_by_fold.json 读取，理论上等于 model_run）
          - oos 指标：sharpe、total_return、max_drawdown、trades
          - paths：gen_01/oos_summary.json 与 best_params_by_fold.json

  脚本位置

  - scripts/run_enhanced_model_sweep.sh:1

  保证“每次只跑一个模型”

  - 每个模型一个子目录：
      - alpha_out/enhanced_sweep/xgb/gen_01/…
      - alpha_out/enhanced_sweep/lstm/gen_01/…
      - alpha_out/enhanced_sweep/transformer/gen_01/…
      - alpha_out/enhanced_sweep/etsformer/gen_01/…
      - alpha_out/enhanced_sweep/itransformer/gen_01/…

  注意事项

  - 建议先用 debug 配置（小窗口、小 epoch）验证链路；生产配置再放大 trials/窗口。
  - 如需只跑某几个模型，编辑脚本 MODELS 数组顺序或内容。
  - 如需固定 GPU/CPU，可在 model_params 里加 "device":"cpu" 或 "device":"mps"/"cuda"。
  - 本地未部署 Kafka，请保留 DISABLE_EVENT_BUS=1 以避免超时阻塞。

## 3. 附录：常用工具与命令

-   **运行 OOF 生成**: (见 2.2 节示例)
-   **运行 `llm_trader`**:
    ```bash
    # 运行一个单独的 llm_trader 机器人
    python llm_trader/run_single_bot.py --provider openai --symbols TSLA --cycles 30 --interval 30
    ```
-   **运行 HPO 优化**:
    ```bash
    # 使用 coop_optuna provider 进行加速搜索
    PYTHONPATH=$PWDDISABLE_EVENT_BUS=1 venv/bin/python alpha_loop.py \
        --config alpha_config_enhanced.json \
        --gens 1 \
        --provider coop_optuna \
        --out_dir alpha_out/coop_test
    ```



    AI 大脑控制面已经在 scripts/ai_trader 下集成好，你可以直接按“控制面”运行整套流程（计划→检查→学习→A/B 等），不必手动调
  用 alpha_loop。

  最简命令（AI 大脑 + 日内赚钱闭环）

  - 一键自运转（建议用 venv；默认 Paper/模拟模式按配置）
      - `PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python scripts/ai_trader/auto_loop.py`
  - 分时段入口（手动触发）
      - 盘前计划与检查：`python scripts/ai_trader/cli.py morning`
      - 盘中执行（第一小时快线）：`python scripts/ai_trader/cli.py intraday --mode fast`
      - 盘中执行（全时段）：`python scripts/ai_trader/cli.py intraday --mode full`
      - 盘后学习（反思→HPO→参数回写→AB + AI 大脑调参）：`python scripts/ai_trader/cli.py learn`

  必备准备（与 AI 大脑联动）

  - 使用仓库自带 venv（避免 NumPy ABI 冲突）
      - python -m venv venv && source venv/bin/activate && pip install -U -r requirements.txt
  - 配置文件
      - 根配置 config.yml（被运行时组件读取）
          - event_bus、kafka、clickhouse、ib、policies.loop_sleep_sec、policies.hpo_deploy、policies.autosettings 等
      - 控制面配置 `scripts/ai_trader/config/ai_trader.yaml`（被 Orchestrator + AI 大脑读取）
          - `brain.enabled: true`：开启 AI 大脑（Director + Alpha/Model/Execution 专家）
          - `brain.autopatch_model_space|features|execution|alpha`：是否自动落地模型空间 / 特征 DSL / 持仓时间 / label_profile 调整
          - `policies.propose_plan`（计划频率/触发器/静默时段）
          - `policies.risk_pretrade`（门禁阈值）
          - `policies.learning`（盘后学习管线：反思→HPO→AI 大脑调参→AB）
          - `policies.tools.llm_trader`（并发/心跳）
  - 环境变量（本地测试建议）
      - DISABLE_EVENT_BUS=1（没有 Kafka 时避免 60s 超时）
      - PYTHONUNBUFFERED=1（无缓冲，便于观察日志）

  它会做什么（现在的 AI 大脑闭环）
  - 盘后学习（`cli.py learn` 或 `auto_loop` 的 afterhours 段）
      - 反思→处方→触发 HPO（alpha_loop / hpo_cli）→产物与指标审查
      - **AI 大脑研究循环**：
        - Director 读取最近 7 天日内指标 + ML 动作记忆（`ml_branches_summary.json`），选择 focus = alpha/model/execution
        - Alpha 专家：建议切换 `label_profile` / 特征标签；Execution 专家：建议收紧/调整持仓时间与 gating；Model 专家：建议收缩/迁移 HPO 搜索空间
        - Critic 审核所有 JSON patch（不允许越权/放宽风控/引入隔夜）
        - 若 `brain.autopatch_*` 为 true，Orchestrator 自动调用安全 patcher：
          - 更新 `alpha_config_enhanced.json`（label_profile + HPO 搜索空间）
          - 更新 `feature_programs.json` + 特征相关 HPO 空间
          - 更新持仓时间搜索空间（`risk.time_stop_min`）
      - 参数回写（受 `policies.autosettings` 控制）→更新 A/B 权重（可用 ab_weight_adjuster）
      - 关键工件打点（可选 MLflow），并生成日/周报
        alpha_config_enhanced.json --gens 1 --provider pi_autopilot --out_dir alpha_out/pi_run
  - 想让 PI 依次只跑一个模型（XGB/LSTM/三种 Transformer），可在配置里用 guard.choices.model_type 限制，并按顺序多次调用上面命令
    （我也能为你编排一个顺序脚本）。

  故障与提示

  - 若卡在 Kafka：加 DISABLE_EVENT_BUS=1
  - 若无输出：加 -X faulthandler 并使用 tee 保存日志；我们已安装 crash hooks，收到 SIGINT/SIGTERM 会打印堆栈
  - 建议先跑 scripts/ai_trader/cli.py morning 验证门禁与基础数据链路，再跑 cli.py learn 看 HPO 全链路

用 venv/bin/python -m scripts.ai_trader.auto_loop 跑“AI 大脑”后，它会在盘后学习阶段自动触发 HPO（alpha_loop），按你的
    配置在多个模型家族（XGB/LSTM/Transformer）之间做候选与优化，并结合 A/B 权重与门槛（hpo_deploy）持续迭代。它不是“无限重跑直
    到盈利”，而是受策略门槛、时间窗、预算与风控约束的稳健迭代流程；达到门槛才应用参数并晋升/增权，否则延长窗口、换模型/特征或保
    持现状。

  怎么“自动选择模型并优化”

  - 候选来源
      - PI 自动研究（pi_autopilot）：LLM 产出训练计划（可含多模型候选），并生成 hpo_grid；或
      - 直接 Optuna：在 hpo.optuna_space 里配置 "model_type" 为分类集合（如
        ["xgb","lstm","transformer","etsformer","itransformer"]），我已补好 in‑loop Transformer 支持。
  - 选择机制
      - WalkForwardHPO 对每个 trial（模型类型+特征子集+超参+决策阈值）执行 IS/OOS 走步验证，按目标函数（Sharpe/公式等）选择最
        优；Optuna 做采样与早停，避免全组合爆炸。
  - 应用与迭代
      - 学习阶段（scripts/ai_trader/cli.py learn 或 auto_loop 的 afterhours/overnight）：
          - 触发 HPO → 评估 OOS → 达标（hpo_deploy 门槛）则生成应用计划/更新 A/B 权重；不达标则尝试延长窗口/调整搜索或保持
            基线。
      - 次日继续迭代（有 TTL/静默时段/触发器，不会无休止打满机器）。

  你需要的最少配置

  - alpha_config_enhanced.json
      - hpo.strategy_path 设 Enhanced 策略
      - hpo.optuna_space.model_type 含你想比较的模型集合（已补 xgb+lstm+三 Transformer 示例）
      - hpo.model_task / model_target_col / model_horizon_bars（无标签时我已在 WF 内做了“用未来收益派生”的兜底）
  - config.yml
      - policies.hpo_deploy（上线门槛：Sharpe/MDD/交易数/覆盖率等）
      - policies.autosettings（是否自动应用参数；生产建议 require_approval）
      - event_bus/kafka（本地可用 DISABLE_EVENT_BUS=1 避免超时）
  - 可选（PI 自动研究）
      - 在 alpha_loop 用 provider=pi_autopilot 时，pi.guard.choices.model_type 可限制单次只跑某一模型；你也可以按天顺序跑
        xgb→lstm→transformer。

  如何运行与观察

  - “一键自运转”（建议本地先加 DISABLE_EVENT_BUS=1）
      - `PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python -m scripts.ai_trader.auto_loop`
      - 或手动触发学习：`PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python -m scripts.ai_trader.cli learn`
  - 产物与记录
      - `runs/ai_trader/YYYY-MM-DD/` 下有：
          - `ml_research_plan.json`：Director 给出的本轮研究计划（focus+horizon_days+success_criteria）
          - `ml_model_space_suggestion.json` / `ml_alpha_suggestion.json` / `ml_execution_suggestion.json`：各专家的 JSON patch + Critic 校验 + autopatch 结果
          - `ml_actions.jsonl` / `ml_branches_summary.json`：所有 ML 动作与按阶段聚合的 delta 指标（IC/AUC/coverage）
          - `feature_engineering_suggestion.json`：特征工程 Agent 输出（含 `_autopatch` 实际应用）
          - `brain_calls.jsonl`：所有大脑调用输入/输出 trace（用于 Debug 与审计）
      - `alpha_out/.../gen_*/` 下有 HPO 的 `oos_summary.json`、`rows.csv`、`best_params_by_fold.json`，现在额外带有：
          - `intraday_pnl_mean/intraday_pnl_median`、`trades_per_day`、`intraday_max_drawdown` 等日内视角指标
      - 可选 MLflow：在 ai_trader.yaml 配置后学习阶段会打点最关键指标和工件

  保证“每次只跑一个模型”的方式

  - PI 路径：在 alpha_config 的 pi.guard.choices.model_type 设为 ["xgb"]，跑完换 ["lstm"] … （一天一个或你想要的节奏）
  - Optuna 路径（更常用）：hpo.optuna_space.model_type 放多个候选让 Optuna 直接比较；如果你确实要“完全顺序”，我已经提供脚本
    scripts/run_enhanced_model_sweep.sh 会逐个模型改配置并顺序跑（每次只跑一个）。

  能否“直到能赚钱”

  - 现实是“稳健+可审计”的迭代，而非无限重试：
      - 质量门：hpo_deploy 门槛（Sharpe、MDD、Trades、PF、PSR/DSR/PBO 等）

  建议的落地步骤

  - 先跑 learn 看完整链路是否通：PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python -m scripts.ai_trader.cli learn
  - 若要严格顺序单模型：用我提供的脚本顺序跑一轮，快速比较“哪家族更有戏”
      - scripts/run_enhanced_model_sweep.sh config/alpha_config_enhanced_debug.json alpha_out/enhanced_sweep
  - 观察
      - gen_01/oos_summary.json 的 OOS 指标是否过门槛
      - runs/ai_trader/**/learn_plan.json（或类似）与 A/B 权重是否更新
      - 需要我帮你把现在的配置调成“每天只跑一个模型、按 xgb→lstm→transformer 的节奏”也可以，我来补 orchestrator 的学习策略或
        scheduler 规则。


        要点到为止给你几个安全做法，按需选用：

  - 若还在当前终端前台运行：按 Ctrl-C 结束。
  - 若是后台作业：先 jobs -l 看编号，然后 kill -TERM %<编号>，不行再 kill -KILL %<编号>。
  - 一次结束整棵进程树（推荐，能带走 Optuna 子进程）:
      - 获取进程组并优雅退出：
          - PGID=$(ps -o pgid= -p $(pgrep -f alpha_loop.py | head -n1) | tr -d ' ')
          - [ -n "$PGID" ] && kill -TERM -$PGID
      - 若 3 秒后仍存活，强制结束：
          - sleep 3; [ -n "$PGID" ] && kill -KILL -$PGID
  - 简单按脚本名全局结束：
      - 优雅：pkill -TERM -f alpha_loop.py
      - 强制：pkill -KILL -f alpha_loop.py
  - 只杀该虚拟环境里的 Python（谨慎，可能会影响用同一 venv 的其他进程）：
      - pkill -TERM -f './venv/bin/python'
  - 先查看有哪些相关进程（不会杀）：
      - pgrep -fl alpha_loop.py




总览

  - 运行入口
      - AI 大脑（控制面，自动学+跑）：venv/bin/python -m scripts.ai_trader.auto_loop
      - 单次学习（仅触发 HPO+评估+应用）：PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python -m scripts.ai_trader.cli learn
      - 单次 HPO（直接跑 alpha_loop）：PYTHONUNBUFFERED=1 DISABLE_EVENT_BUS=1 PYTHONPATH=$PWD venv/bin/python -X faulthandler
        alpha_loop.py --config <cfg>.json --gens 1 --provider optuna --out_dir <out> 2>&1 | tee <out>/run.log; echo EXIT:$?
  - 关键产物位置
      - HPO 输出：alpha_out/<run>/gen_01/oos_summary.json, alpha_out/<run>/gen_01/rows.csv, alpha_out/<run>/gen_01/
        best_params_by_fold.json
      - AI 大脑审计：runs/ai_trader/YYYY-MM-DD/hpo_retry_audit.json, runs/ai_trader/YYYY-MM-DD/learn_plan.json（名称可能略有
        差异）
      - 策略事件：runs/ai_trader/YYYY-MM-DD/adaptive_events.jsonl, guardian_events.jsonl, hedging_events.jsonl

  如何判断“是否卡住/停止”

  - alpha_loop（单次 HPO）
      - 观察 run.log 是否持续出现标记（若无缓冲）：PYTHONUNBUFFERED=1 -X faulthandler ... | tee run.log
      - 关键标记：
          - “Before/After WalkForwardHPO init/runner.run()”（内存打印与构造成功）
          - “Resume No previous results found … / Running X trials”
          - “Trial N START/END”（Optuna 试验开始/结束）
      - 快速 grep：
          - rg -n "Before WalkForwardHPO|HPO-DEBUG|WalkForwardHPO:init|After runner.run\\(\\)" <out>/run.log -S
      - 如长时间无输出，发送信号打印堆栈（不中断）：
          - 找 PID：pgrep -f "alpha_loop.py.*--config <cfg>"
          - 打印堆栈：kill -SIGTERM <PID>（我们已注册 crash hooks，会打印 [CRASH-HOOK] 与 faulthandler 堆栈）
  - AI 大脑（auto_loop / learn）
      - 查看 runs/ai_trader/YYYY-MM-DD/hpo_retry_audit.json 是否有“round 进度 / status / error / provider / mem_stats”
          - jq '.' runs/ai_trader/$(date +%F)/hpo_retry_audit.json
      - 查看 alpha_out/daily/gen_01/oos_summary.json 是否生成
      - 控制面打印会显示：
          - env_overrides_applied（线程/BLAS 限制已应用）
          - hpo_retry_round_start（轮次 + provider + elapsed）
          - alpha_loop_safe_start/alpha_loop_safe_completed（alpha_loop 受保护启动/完成）
          - hpo_retry_round_complete / hpo_retry_timeout / alpha_loop_failed（收敛或失败审计）
  - 资源级检查（确认未“假死”）
      - 查看 CPU/内存是否变化：ps -o pid,pcpu,pmem,etime,command -p <PID>
      - 查看日志是否持续增长：tail -f <out>/run.log
      - 查看是否卡在网络 I/O（CH/LLM/Kafka）：lsof -iTCP -sTCP:ESTABLISHED -p <PID>

  判断“是否停止/完成”

  - alpha_loop
      - 成功完成：shell 返回 EXIT:0；目录下有 gen_01/oos_summary.json、best_params_by_fold.json
      - 异常退出：shell 返回非 0；run.log 尾部会有 [ERROR] 或堆栈
      - 超时：alpha_loop_wrapper 设了 3600s 超时（utils/alpha_loop_wrapper.py），超时会抛出并在 retry_audit 记录
  - AI 大脑（learn）
      - 4 轮重试完成或“时间预算/资源预算耗尽”，会写 hpo_retry_audit.json，并继续执行学习链条（报告/AB 等），流程不崩
      - HPO 通过门槛（config.yml: policies.hpo_deploy）后，会生成应用计划与 A/B 决策记录
      - Kafka 不可用时（本地开发）：会打印 kafka_send_failed 或消费者退出 warning，但 auto_loop 继续执行

  如何查看结果与“最佳模型是谁”

  - HPO 结果（单次或 AI 大脑内部）
      - alpha_out/.../gen_01/oos_summary.json：关键 OOS 指标（Sharpe/Total Return/Trades/MDD/PSR/DSR/PBO）
      - alpha_out/.../gen_01/best_params_by_fold.json：params.model_type 表示此次最佳模型家族
      - 快速提取：
          - jq '.oos_avg_sharpe,.oos_avg_total_return,.oos_trades,.oos_avg_max_drawdown' alpha_out/.../gen_01/
            oos_summary.json
          - jq '.[0].params.model_type' alpha_out/.../gen_01/best_params_by_fold.json
  - AI 大脑审计（多轮）
      - runs/ai_trader/YYYY-MM-DD/hpo_retry_audit.json：每轮 provider、错误/超时与退化等级、内存峰值、最佳 OOS（如有）
      - runs/ai_trader/YYYY-MM-DD/hpo_decision.json（如启用决策与应用）

  卡住/失败常见原因与排查

  - OOM / SIGKILL
      - 特征：Round 0 就被系统杀死，日志只到 “alpha loop killed by SIGKILL”
      - 处理：
          - 我们已默认 Round 0 仅用 optuna（避免 coop_optuna 在 XGB 上内存飙涨）
          - 序列数据集已改为“按需切片”，避免窗口物化
          - 可进一步在 optuna_space.model_params 降低 XGB 的 n_estimators/max_depth，或暂时排除 XGB，仅跑 LSTM/Transformer
          - 观察 env_overrides_applied（线程数已降到 2）
  - ClickHouse 连接错误（UnexpectedPacketFromServerError）
      - 原因：用 clickhouse-driver 连接了 HTTP 8123 端口
      - 处理：
          - 适配器已自动优先 clickhouse-connect（8123），失败再回退 clickhouse-driver（9000），并纠正端口
          - 快检：curl -sS http://localhost:8123/?query=SELECT%201，nc -vz localhost 9000
  - Optuna “dynamic value space”
      - 原因：分类参数 choices 在 study 生命周期内改变
      - 处理：
          - 我已为 space 生成 sha1 签名拼到 study_name，保证每个空间独立，不再复用旧 study（alpha_loop.py 已修复）
          - 若仍报错，检查 hpo.optuna_space 中的 choices 是否会被上游修改
  - 无效 provider
      - 提示：--provider: invalid choice: 'ml-only'（这是无效值）
      - 处理：
          - 我已改为仅 optuna 回退（scripts/ai_trader/orchestrator.py 与 config.yml 统一修正）
  - Kafka 导致“卡住”的假象
      - 本地未启 Kafka 时消费者会打印 warning 并退出，主循环继续
      - 建议本地加 DISABLE_EVENT_BUS=1 避免 publish 超时

  如何“主动触发堆栈/中断”

  - 打印堆栈（不中断，用于诊断）
      - kill -SIGTERM <PID>（我们注册了信号钩子，会打印 [CRASH-HOOK] 和全线程堆栈）
  - 温和中断
      - Ctrl-C（SIGINT）或 kill -INT <PID>（同样打印钩子信息后退出）
  - 清理僵锁（auto_loop 开始会清理昨日的 locks/pids，代码已写）

  自动化监控（脚本建议）

  - 文件活跃性（N 秒未更新）
      - watch -n 10 'ls -lT alpha_out/daily/gen_01/oos_summary.json 2>/dev/null || echo waiting'
  - 进程资源（CPU/MEM）
      - watch -n 5 "ps -o pid,pcpu,pmem,etime,command -p $(pgrep -f 'alpha_loop.py.*--config' | tr '\n' ' ')"
  - 快速诊断工具（仓库已有）
      - scripts/ai_trader/check_tools_health.py（健康与基础依赖）
      - scripts/ai_trader/run_health_check.sh（门禁综合）
      - Grafana dashboard（若启用 Kafka/Prometheus）：ai_boss_dashboard（含 AB Split 与 HPO 状态）
      - 专用 AI 调参 → 日内 PnL/trades/day/drawdown 面板：`grafana/dashboards/active/ai_brain_intraday_pnl.json`

  建议的日常 SOP

  - 本地开发
      - DISABLE_EVENT_BUS=1，先 cli.py learn 确认链路→OK 后交给 auto_loop.py 长跑
      - 仅想比较模型：用我提供的 scripts/run_enhanced_model_sweep.sh 顺序单模型，每次只跑一个
  - 生产/审计
      - study 名称与空间签名已稳定；建议用 SQLite/Postgres 保存 Optuna study，便于 Resume 与审计
      - 门槛（config.yml: policies.hpo_deploy）统一管理；AI 大脑学完后产出 hpo_decision.json 与 AB 决策

  常用命令备忘

  - 学习（一次）：PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python -m scripts.ai_trader.cli learn
  - 自动（长跑）：PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python -m scripts.ai_trader.auto_loop
  - 单次 HPO：PYTHONUNBUFFERED=1 DISABLE_EVENT_BUS=1 PYTHONPATH=$PWD venv/bin/python -X faulthandler alpha_loop.py --config alpha_config_enhanced.json --gens 1 --provider optuna --out_dir alpha_out/manual
  - Stuck 调试（打印堆栈）：kill -SIGTERM $(pgrep -f 'alpha_loop.py.*--config')




  发现问题,检查问题,问题修复举例:
  问题复现与原因判断

  - 日志显示子进程被 SIGKILL 终止，包装器将其“猜测”为 OOM；但你看到的内存指标来自父进程（包装器）而非 alpha_loop 子进程，所以
    会“看起来不高”。
  - macOS/系统或外部看门狗/手工 pkill 都会导致 -9 返回码。若你刚执行过全局 pkill，这条报错也会出现。

  我已做的修复与改动

  - 子进程内存监控与预防性终止
      - 在 utils/alpha_loop_wrapper.py:71 开始的 run_alpha_loop_safe 内，将运行方式从 subprocess.run 改为 Popen +
        communicate，并在新线程中持续汇总“子进程+所有子进程”的 RSS，用 budget 做阈值预警/终止。
      - 关键位置：utils/alpha_loop_wrapper.py:265 _watch_child(...)、utils/alpha_loop_wrapper.py:318 start_new_session=True
        便于整组杀进程、utils/alpha_loop_wrapper.py:276 超限预警/终止日志为 alpha_loop_child_mem_warning/
        alpha_loop_child_mem_exceeded。
  - 修正内存预算来源

  建议的下一步

  - 先确认没有外部杀进程者
      - pgrep -fl 'ml_watchdog|alpha_loop.py' 查看是否有看门狗或其他 alpha_loop 在跑。
      - 若你此前执行过 pkill -KILL -f alpha_loop.py，请先确认不再杀到当前任务。
  - 重试学习流程（已采用配置中的 6GB 预算 + 子进程内存监控）
      - 命令：PYTHONPATH=$PWD DISABLE_EVENT_BUS=1 venv/bin/python -m scripts.ai_trader.cli learn
  - 若仍触发内存保护（会看到新的 child_mem_warning/exceeded 日志）：
      - 将 config.yml 中 policies.hpo_budget.mem_budget_gb 维持 6.0（或更低），并适当降低 trial_limits_by_round、
        symbol_top_k_by_round，以进一步减载。
      - 可暂时把 BLAS 线程再压低：export OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1。

  文件定位

  - 子进程内存监控与 SIGKILL 处理：utils/alpha_loop_wrapper.py:71, utils/alpha_loop_wrapper.py:265, utils/
    alpha_loop_wrapper.py:318
  - 从配置读取内存预算：scripts/ai_trader/orchestrator.py:2476





    新增脚本

  - scripts/ai_trader/tools/ml_watchdog.py
      - 作用
          - 监控进程 RSS（内存）并与阈值比较
          - 监控日志文件增长，判定是否“卡住”
          - 违例时先发信号触发堆栈打印（配合我们已装好的 CRASH-HOOK + faulthandler），可选 KILL + 自动重启
      - 依赖
          - 优先使用 psutil；若缺失则回退到 ps 命令解析 RSS，macOS/Linux 均可用

  使用方法（常用示例）

  - 监控 alpha_loop，日志 6 分钟无增长视为卡住，RSS 超过 10GB 视为超限
      - PYTHONPATH=$PWD venv/bin/python -m scripts.ai_trader.tools.ml_watchdog --match "alpha_loop.py --config alpha_config_enhanced.json" --log-file alpha_out/daily/run.log --stuck-mins 6 --rss-limit-mb 10000
  - 监控并自动重启（提供安全重启命令）
      - PYTHONPATH=$PWD venv/bin/python -m scripts.ai_trader.tools.ml_watchdog --match "alpha_loop.py --config
        alpha_config_enhanced.json" --log-file alpha_out/daily/run.log --stuck-mins 6 --rss-limit-mb 10000 --restart-
        cmd "PYTHONUNBUFFERED=1 DISABLE_EVENT_BUS=1 PYTHONPATH=$PWD venv/bin/python -X faulthandler alpha_loop.py --config
        alpha_config_enhanced.json --gens 1 --provider optuna --out_dir alpha_out/daily"
  - 说明
      - 初次找 PID：按 --pid 或 --match (pgrep -f) 查找
      - 违例处理：先发 SIGTERM（打印堆栈），等待 --cooldown-sec（默认 120s）；若仍在且有 --restart-cmd，则 KILL 并重启
      - 其他参数：--interval-sec（默认 60s），--max-restarts（默认 3），--signal（默认 TERM）
# Alpha Loop 全模型测试计划

**目标**: 验证所有ML模型和参数优化场景在修复后都能正常运行，无内存泄漏，无崩溃

**测试日期**: 2025-11-11
**修复的Bug数量**: 8个（详见下方）

---

## 已修复的Bug清单

1. ✅ **Infinite Loop** in `make_folds()` - float类型转换问题
2. ✅ **Pandas DataFrame Assignment Error** - groupby返回多列
3. ✅ **Missing Database Columns** - SQL查询缺少vwap, ret_1, ret_5, rsi_14
4. ✅ **Memory Leak in BacktestEngine** - 183MB/trial泄漏 (CRITICAL)
5. ✅ **XGBoost Early Stopping Error** - 没有validation set时的配置错误
6. ✅ **XGBoost best_iteration AttributeError** - 访问不存在的属性
7. ✅ **UnboundLocalError #1** - cleanup代码中的变量检查
8. ✅ **UnboundLocalError #2** - 结果序列化时的变量引用

---

## 测试矩阵

### 1. 机器学习模型测试 (Model HPO)

| 测试ID | 模型类型 | 配置文件 | 数据规模 | 预期时长 | 内存限制 | 状态 |
|--------|---------|---------|---------|---------|---------|------|
| ML-1 | XGBoost | test_config_xgb.json | 300行, 2 folds | 2-3分钟 | 600MB | ⏳ 待测 |
| ML-2 | LSTM | test_config_lstm.json | 300行, 2 folds | 3-4分钟 | 800MB | ⏳ 待测 |
| ML-3 | Transformer | test_config_transformer.json | 300行, 2 folds | 3-4分钟 | 800MB | ⏳ 待测 |
| ML-4 | ETSformer | test_config_etsformer.json | 300行, 2 folds | 3-4分钟 | 800MB | ⏳ 待测 |
| ML-5 | ITransformer | test_config_itransformer.json | 300行, 2 folds | 3-4分钟 | 800MB | ⏳ 待测 |

### 2. 策略参数优化测试 (Strategy HPO - No ML)

| 测试ID | 优化目标 | 配置文件 | 数据规模 | 预期时长 | 内存限制 | 状态 |
|--------|---------|---------|---------|---------|---------|------|
| STR-1 | Sizing参数 | test_config_sizing.json | 300行, 2 folds | 1-2分钟 | 500MB | ⏳ 待测 |
| STR-2 | Market Filter | test_config_filter.json | 300行, 2 folds | 1-2分钟 | 500MB | ⏳ 待测 |
| STR-3 | Risk参数 | test_config_risk.json | 300行, 2 folds | 1-2分钟 | 500MB | ⏳ 待测 |

### 3. 混合优化测试 (ML + Strategy)

| 测试ID | 场景 | 配置文件 | 数据规模 | 预期时长 | 内存限制 | 状态 |
|--------|-----|---------|---------|---------|---------|------|
| MIX-1 | XGBoost + Sizing | test_config_xgb_sizing.json | 300行, 2 folds | 3-4分钟 | 700MB | ⏳ 待测 |
| MIX-2 | LSTM + Filter | test_config_lstm_filter.json | 300行, 2 folds | 3-4分钟 | 800MB | ⏳ 待测 |
| MIX-3 | 所有模型混合 | alpha_config_enhanced.json | 840行, 2 folds | 5-10分钟 | 1GB | ✅ 已通过 |

### 4. 压力测试

| 测试ID | 场景 | 配置文件 | 数据规模 | 预期时长 | 内存限制 | 状态 |
|--------|-----|---------|---------|---------|---------|------|
| STRESS-1 | 大数据集 | test_config_large.json | 3480行, 61 folds | 30-60分钟 | 2GB | ⏳ 待测 |
| STRESS-2 | 多轮迭代 | test_config_multi_gen.json | 300行, 5 gens | 10-15分钟 | 800MB | ⏳ 待测 |

---

## 测试配置文件准备

### ML-1: XGBoost 测试配置
```json
{
  "start": "2025-10-06 16:00:00",
  "end": "2025-11-06 16:00:00",
  "symbols": ["TSLA"],
  "fold_generator": {
    "is_days": 7,
    "oos_days": 5,
    "step_days": 0.5,
    "gap_days": 0
  },
  "model_task": "reg",
  "optuna_space": {
    "model_type": {"type": "categorical", "choices": ["xgb"]},
    "feature_subset": {"type": "categorical", "choices": [["vwap", "atr_14", "rsi_14", "volume"]]},
    "model_params": {"type": "categorical", "choices": [
      {"n_estimators": 100, "eta": 0.1, "max_depth": 4, "subsample": 0.8, "colsample_bytree": 0.8}
    ]}
  }
}
```

### ML-2: LSTM 测试配置
```json
{
  "optuna_space": {
    "model_type": {"type": "categorical", "choices": ["lstm"]},
    "model_params": {"type": "categorical", "choices": [
      {"hidden": 64, "num_layers": 1, "dropout": 0.1, "lookback": 16, "epochs": 5, "patience": 2, "batch_size": 128, "lr": 0.001}
    ]}
  }
}
```

### ML-3: Transformer 测试配置
```json
{
  "optuna_space": {
    "model_type": {"type": "categorical", "choices": ["transformer"]},
    "model_params": {"type": "categorical", "choices": [
      {"lookback": 16, "d_model": 64, "nhead": 2, "num_layers": 2, "dim_feedforward": 128, "dropout": 0.1, "epochs": 5, "patience": 2, "batch_size": 128, "lr": 0.001}
    ]}
  }
}
```

### STR-1: Sizing 参数优化
```json
{
  "optuna_space": {},
  "grid": {
    "sizing.mode": {"type": "choice", "values": ["fixed", "confidence_scaled"]},
    "sizing.base_qty": {"type": "int", "low": 50, "high": 150, "step": 50}
  }
}
```

---

## 测试执行脚本

### 单个测试运行命令
```bash
# ML-1: XGBoost测试
export PYTHONPATH=.
venv/bin/python alpha_loop.py \
  --config test_configs/test_config_xgb.json \
  --provider optuna \
  --gens 1 \
  --out_dir ./test_results/ml1_xgb 2>&1 | tee /tmp/test_ml1_xgb.log

# 内存监控 (在另一个终端)
watch -n 3 'ps aux | grep alpha_loop | grep -v grep | awk "{printf \"Memory: %.1f MB | CPU: %s%%\\n\", \$6/1024, \$3}"'
```

### 批量测试脚本
```bash
#!/bin/bash
# 保存为: run_all_tests.sh

TEST_CONFIGS=(
  "test_config_xgb.json:ml1_xgb"
  "test_config_lstm.json:ml2_lstm"
  "test_config_transformer.json:ml3_transformer"
  "test_config_etsformer.json:ml4_etsformer"
  "test_config_itransformer.json:ml5_itransformer"
  "test_config_sizing.json:str1_sizing"
  "test_config_filter.json:str2_filter"
  "test_config_risk.json:str3_risk"
)

export PYTHONPATH=.

for test in "${TEST_CONFIGS[@]}"; do
  CONFIG=$(echo $test | cut -d: -f1)
  NAME=$(echo $test | cut -d: -f2)

  echo "=========================================="
  echo "Running test: $NAME"
  echo "Config: $CONFIG"
  echo "=========================================="

  # 记录开始时间和内存
  START_TIME=$(date +%s)
  START_MEM=$(ps aux | grep python | awk '{sum+=$6} END {printf "%.1f", sum/1024}')

  # 运行测试
  venv/bin/python alpha_loop.py \
    --config test_configs/$CONFIG \
    --provider optuna \
    --gens 1 \
    --out_dir ./test_results/$NAME \
    2>&1 | tee /tmp/test_$NAME.log

  EXIT_CODE=$?

  # 记录结束时间和内存
  END_TIME=$(date +%s)
  END_MEM=$(ps aux | grep python | awk '{sum+=$6} END {printf "%.1f", sum/1024}')
  DURATION=$((END_TIME - START_TIME))

  # 检查错误
  ERROR_COUNT=$(grep -E "(Error|Exception|Traceback)" /tmp/test_$NAME.log | wc -l)

  # 写入结果
  echo "$NAME,$CONFIG,$EXIT_CODE,$DURATION,${START_MEM}MB,${END_MEM}MB,$ERROR_COUNT" >> test_results/summary.csv

  if [ $EXIT_CODE -eq 0 ] && [ $ERROR_COUNT -eq 0 ]; then
    echo "✅ PASS: $NAME"
  else
    echo "❌ FAIL: $NAME (exit=$EXIT_CODE, errors=$ERROR_COUNT)"
  fi

  echo ""
  sleep 5
done

echo "All tests completed! Summary:"
cat test_results/summary.csv
```

---

## 验收标准

### 每个测试必须满足:

1. **✅ 无崩溃**: 进程正常退出 (exit code 0)
2. **✅ 无错误日志**: 日志中无AttributeError, UnboundLocalError, ValueError
3. **✅ 内存稳定**:
   - 单个trial内存增长 < 50MB
   - 整个run内存增长 < 200MB
   - 无持续增长趋势
4. **✅ 功能完整**:
   - 生成completed_trials.jsonl
   - 生成best_params.json
   - 生成alpha_*.json结果文件
5. **✅ 性能合理**:
   - XGBoost: <5秒/trial
   - LSTM/Transformer: <15秒/trial
   - 策略参数: <2秒/trial

### 内存监控检查点:
```bash
# 在测试运行期间执行
for i in {1..20}; do
  echo "=== Check $i at $(date +%H:%M:%S) ==="
  ps aux | grep alpha_loop | grep -v grep | awk '{printf "Memory: %.1f MB | CPU: %s%%\n", $6/1024, $3}'
  sleep 10
done
```

---

## 测试报告模板

### 单个测试结果
```
测试ID: ML-1
模型类型: XGBoost
配置文件: test_config_xgb.json
执行时间: 2025-11-11 23:15:00
运行时长: 156秒

结果:
✅ 无崩溃
✅ 无错误日志
✅ 内存稳定 (420MB → 455MB, +35MB)
✅ 功能完整 (3个结果文件生成)
✅ 性能合理 (平均4.2秒/trial)

内存趋势:
  Start: 420 MB
  Peak:  455 MB
  End:   452 MB
  Leak:  0 MB/trial

生成文件:
- completed_trials.jsonl (88 trials)
- best_params.json
- alpha_xgb_best.json

状态: ✅ PASS
```

---

## 快速验证脚本

```bash
#!/bin/bash
# quick_verify.sh - 快速验证单个模型

MODEL=$1  # xgb, lstm, transformer, etc.
LOG_FILE="/tmp/test_${MODEL}.log"

echo "Testing $MODEL model..."

# 运行测试
export PYTHONPATH=.
venv/bin/python alpha_loop.py \
  --config test_configs/test_config_${MODEL}.json \
  --provider optuna \
  --gens 1 \
  --out_dir ./test_results/${MODEL} \
  2>&1 | tee $LOG_FILE &

PID=$!

# 监控15秒
for i in {1..5}; do
  sleep 3
  MEM=$(ps -o rss= -p $PID 2>/dev/null | awk '{printf "%.1f", $1/1024}')
  echo "[$i] Memory: ${MEM}MB"
done

wait $PID
EXIT_CODE=$?

# 检查结果
echo ""
echo "========== VERIFICATION =========="
echo "Exit Code: $EXIT_CODE"
echo "Errors: $(grep -c 'Error\|Exception\|Traceback' $LOG_FILE)"
echo "Files Generated: $(ls -1 test_results/${MODEL}/*.json 2>/dev/null | wc -l)"

if [ $EXIT_CODE -eq 0 ]; then
  echo "Status: ✅ PASS"
else
  echo "Status: ❌ FAIL"
  echo "Last 20 lines of log:"
  tail -20 $LOG_FILE
fi
```

使用方法:
```bash
chmod +x quick_verify.sh
./quick_verify.sh xgb
./quick_verify.sh lstm
./quick_verify.sh transformer
```
模型类型: xgb, lstm, transformer, etsformer, itransformer, sizing
---

## 注意事项

1. **数据库依赖**: 测试前确保ClickHouse运行中
2. **清理旧数据**: 每次测试前删除`completed_trials.jsonl`避免resume
3. **并行限制**: 同时只运行一个测试，避免资源竞争
4. **日志保留**: 所有测试日志保存至少7天用于分析
5. **失败重试**: 失败的测试至少重试2次确认问题可复现

---

**测试负责人**: Claude AI Assistant
**审核人**: 用户
**预计完成时间**: 所有测试约2-3小时


# MLflow Guide: How to Track Your AI Trader Experiments

This guide explains what MLflow is and how to use its web interface to find and understand the results from the `learn` command.

## 1. What is MLflow?
MLFLOW_GUIDE.md

MLflow is a platform for managing the entire machine learning lifecycle. In this project, we primarily use it for **experiment tracking**.
run:
bash scripts/ai_trader/dashboards/start_mlflow.sh
