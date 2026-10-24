先把这次结果说清楚，再给你一个更“赚钱导向”的优化方向和下一步要跑的命令。

  1. 这次 debug 跑出来的问题是什么？

  从 alpha_enhanced_smoke/gen_01 和日志看：

  - 整体 OOS 结果：
      - oos_summary.json 里：
          - oos_trades: 1（15 个 fold，只有 1 笔 OOS 交易）
          - oos_avg_total_return: 约 -0.9%
          - oos_avg_sharpe: -0.0127（非常接近 0，略负）
  - 每个 fold 的 OOS：leaderboard_oos.csv
      - 多数 fold：trades = 0，收益全 0；
      - 少数 fold（例如 fold 3,4,5,15）：有非零收益，但 trades 仍然是 0 或 1。
  - IS 侧：best_params_by_fold.json
      - 有些 fold 在 IS 上有 1–2 笔交易（甚至有盈利的那个 fold），
        但 OOS 上几乎没有再出现。
  - 日志：~/grafana-stack/logs/trader.log 对应这次 backtest：
      - 大量的
          - order_skipped ... reason=market_filter, reason_detail=low_volume: xxxx
          - 以及 order_size_reduced ... reason=low_conf_soft
      - 几乎没有 order_submitted / order_fill。

  结合 strategies/configs/enhanced_ml_TSLA.json 可以看到：

  "market_filter": {
    "conf_floor": 0.08,
    "conf_soft_threshold": 0.30,
    "conf_soft_sizedown_scale": 0.5,
    "max_spread_bps": 8,
    "min_volume": 100000,
    "check_after_hours": true
  }

  而日志里的 low_volume 值都是 4260、4916、10908… 远小于 100000。

  根因：

  - 虽然我们在 debug 配置 (alpha_config_enhanced_debug.json) 里把
    market_filter.min_volume 配成了 1000、conf_floor 配成 0.0，
    但策略在 initialize() 里会用 enhanced_model_config_loader 去加载
    strategies/configs/enhanced_ml_TSLA.json，然后用文件里的配置覆盖 base extras。
  - 所以 HPO 回测实际用的是：
      - min_volume = 100000、max_spread_bps = 8、conf_floor = 0.08、conf_soft_threshold = 0.30。
  - 结果就是：
      - 很多 bar 模型给了信号，但 volume < 100000 → market_filter 直接 order_skipped；
      - 少数没被 volume 挡住的，conf 低又被软缩小仓位，综合下来极少出现符合条件的交易；
      - 最终 OOS 只有 1 笔成交，整体 Sharpe 接近 0。

  换句话说：
  这次“跑完没什么交易”跟模型本身关系不大，主要是“实盘配置（JSON）把回测挡死了”。

  ———

  2. 这次改动已经做了什么优化？

  在你这次跑之前，我已经做的几件事：

  - 决策逻辑修正（解决“用尺子量体重”问题）：
      - label 已经改成用 label_profile = "intraday_reg_5m"（5 根 1m bar 的 log-return 和），
        且在 wf_hpo._prepare_model_data() 里用这个 profile 生成 y。
      - 对回归任务的 ml_conf：用 |pred| / (3*std(pred_is)) 映射到 0–1，
        再传给策略 → conf 真的是“置信度”，不是“未来涨幅值”。
      - DecisionScheme 支持：
          - quantile（q_long/q_short），保证一定比例的 bar 会被打成多/空；
          - threshold，但门槛已经调低（生产配置里是 0.0001 / 0.0002）。
  - 防“没样本”/LSTM 崩溃：
      - LSTM fit() 发现样本 < lookback 时，标记 _unfittable，predict() 返回全 0；
      - wf_hpo._grid_eval() 外层 try/except 把 num_samples=0 这种 trial 标成 error="no_samples"，不再炸掉 alpha_loop。
  - 主 alpha 配置 (alpha_config_enhanced.json) 的优化：
      - 时间窗已经扩大到 2025-08→2025-11-14；
      - optuna_space.decision 已经改成：
          - 两个 quantile（0.7/0.3, 0.6/0.4）；
          - 两个小阈值的 threshold（±0.0001、±0.0002）；
      - optuna.target 从 "is" 改成 "oos"，
        → HPO 直接用 OOS 指标选最优。

  这次你跑的 debug 配置：

  - 确认了整个链条（ClickHouse→特征→标签→模型→策略→回测）能跑完；
  - 但因为 JSON 的 min_volume=100000 等设置，导致样本窗口太短 + 滤得太严，
    结果 OOS 几乎没交易，这个结果没法直接用来“赚钱”。

  ———

  3. 针对“赚钱”的优化方向

  分三层来说：过滤器 → 决策层 → 数据窗口/标签。

  (1) 过滤器：把“实盘超保守配置”和“研究用配置”分开

  - 实盘 JSON (strategies/configs/enhanced_ml_TSLA.json) 当前非常保守：
      - min_volume=100000、max_spread_bps=8、conf_floor=0.08、conf_soft_threshold=0.30。
  - 对 HPO/研究来说，这样的 volume 门槛太高，会把 90% 以上的 bar 删掉。

  我刚做了一步重要的改动（只影响 alpha_loop / HPO，不动实盘）：

  - 在 config/alpha_config_enhanced_debug.json 的 strategy_base_extras 里加了：

    "config_prefix": "enhanced_ml_debug"
  - ConfigLoader 会用这个前缀找 enhanced_ml_debug_TSLA.json，
    而这个文件并不存在 → 它会打印 “Config file not found”，然后
    只用 base extras（也就是 debug 配里设的 min_volume=1000、conf_floor=0.0 等）。
  - 这样：
      - HPO/debug 回测完全按“研究用的过滤器”来，不再被实盘 JSON 卡死；
      - 实盘跑系统（ai_trader）用的还是原来的 enhanced_ml_TSLA.json，互不干扰。

  如果你想让实盘也多交易一点，可以以后再考虑把 enhanced_ml_TSLA.json 里的：

  "min_volume": 100000  →  比如改到 20000 或 50000
  "conf_soft_threshold": 0.30 → 可以视情况调低
  "conf_floor": 0.08 → 保留或略调低

  但这一步先不用急，我优先让 HPO 跑出“有交易、可分析”的结果。

  (2) 决策层：让模型确实打出足够多的信号

  - 目前 Champion 参数里，你可以看到：
      - 很多 fold 的 decision.mode = "threshold", threshold_long = 0.001（老门槛）；
      - 少数是 quantile (0.8 / 0.2)。
  - 对 1m / 5m 的 log-return 来说，0.1% 的门槛确实偏高，会导致很多预测都夹在中间。

  我已经在配置里做的调整：

  - debug 配（短跑）：
      - optuna_space.decision.choices 只有：
          - quantile (0.7 / 0.3)、quantile (0.8 / 0.2)、threshold (±0.0001)；
      - 这样 Optuna 在 debug 里不会再去采样旧的 0.001 阈值。
  - 主配（长跑）：
      - alpha_config_enhanced.json 的 optuna_space.decision：
          - 两个 quantile（0.7/0.3, 0.6/0.4）；
          - 两个小阈值（±0.0001、±0.0002）。

  后续可以再进一步“进攻一点”：

  - 在主配置里，把 quantile 再加一个稍宽的：
      - 比如 q_long=0.55, q_short=0.45，让更多 bar 有非零信号；
  - 或者增加一个更细的 threshold 组合：
      - ±0.00005，用来捕捉微小 intraday edge；
  - 如果你希望，我可以直接把这些加进 alpha_config_enhanced.json 的 optuna_space.decision。

  (3) 数据窗口 / 标签：保证“样本足够 + 标签合理”

  这一层目前整体是合理的：

  - label_profile = intraday_reg_5m：
      - horizon=5、log_return、sum → 对 1m 高频策略是合适的；
      - 避免 horizon 太长导致 “dropna 把前后吃光” 的问题。
  - 主 HPO (alpha_config_enhanced.json) 已经用了较长窗口（3 个多月），
    不再是之前 20 天那种会触发 num_samples=0 的配置。

  如果你愿意更激进地“喂饱模型”，下一步可以做：

  - 把主配置里的 hpo.start 从 2025-08-01 再往前推，例如 2024-08-01，
    这样每个 fold 的 IS 都有更多历史，模型能更稳定地学到东西。
    代价是：本地 HPO 会更慢、内存压力大，我建议分阶段来做。

  ———

  4. 接下来你可以怎么跑，才能看到“更真实可用”的结果

  现在 debug 配已经改成“只用 HPO 专用过滤器，不吃 JSON 配置”了。

  (1) 重新短跑一次（验证：有足够的交易）

  venv/bin/python alpha_loop.py \
    --config config/alpha_config_enhanced_debug.json \
    --provider optuna \
    --gens 1 \
    --k-top 4 \
    --out_dir alpha_enhanced_smoke_v2

  跑完后，重点看：

  - alpha_enhanced_smoke_v2/gen_01/oos_summary.json：
      - oos_trades 是否明显 > 1；
      - oos_avg_sharpe 是否趋近于 0 上下，至少不是因为“没交易”而无意义。
  - alpha_enhanced_smoke_v2/gen_01/leaderboard_oos.csv：
      - 多数 fold 是否开始有 3–10 笔交易；
      - 哪些决策模式（quantile VS threshold）在 OOS 上表现更好。

  (2) 确认 OK 后，再跑一次长周期 HPO（真正“赚钱导向”的 run）

  venv/bin/python alpha_loop.py \
    --config alpha_config_enhanced.json \
    --provider coop_optuna \
    --gens 1 \
    --k-top 12 \
    --out_dir alpha_enhanced_ml_v1

  - 这个跑完之后：
      - alpha_enhanced_ml_v1/gen_01/oos_summary.json：看 OOS Sharpe / 回撤 / 交易数；
      - alpha_enhanced_ml_v1/gen_01/leaderboard_oos.csv：挑出 1–2 个在 OOS 上既有交易、又赚钱的参数组合；
      - 然后我们可以再一起把这些参数“落到”
          - strategies/configs/enhanced_ml_TSLA.json（实盘配置），
          - 或者一个新的 “TSLA_ml_v1” JSON 里。

  ———

  这次 0 交易的根本原因，是实盘 JSON 里的 market_filter.min_volume=100000 等超保守参数在回测中生效，把 ML 策略的所有机会都挡掉
  了，不是模型完全没用。我已经把 debug/HPO 模式下的配置改成只用研究用过滤器，下一步按上面的短跑 + 长跑命令跑一轮，我们就能看到
  更有交易、可以分析和优化的结果。如果你跑完新的 smoke/长跑结果，我可以再帮你一起挑出“可实盘落地”的那一组参数。

## 如何一直跑机器学习不停
  接下来，如果你想让它像 auto_loop 一样一直跑，只要把 --max-cycles 去掉，或者设置成很大：

  ENV_MODE=paper venv/bin/python tools/ml_brain_loop.py \
    --symbols TSLA AAPL \
    --start-job-type oof \
    --sleep-sec 10
› gemini 分析目前有 trade,现在从技术问题转向策略问题.你分析一下是否正确,记住我们的目的是赚钱: Next Steps: From a Working
  System to a Profitable System

    To get to a profitable strategy, you need to improve the "brain" of the system—the ML model. Here are the key
    areas to investigate:

     1. Labeling: Is the data used to train the model properly labeled? If the training data has a huge imbalance (e.g.,
        95% of labels are "hold" or "sell"), the model will learn to be heavily biased. This is a very common problem.
     2. Features: Are the features predictive? You need to analyze which features are most important to the model. It's
        possible the model is relying on a feature that is not very useful, or that you need to engineer new, more
        predictive features.
     3. Model Calibration: The confidence scores are very low. This suggests the model is "uncalibrated." A
        well-calibrated model's confidence score should reflect the actual probability of its prediction being correct.
        You may need to add a calibration step after training.

    The immediate next step should be to analyze the model that was used in this backtest. You need to understand its
    feature importance and, most critically, the distribution of the labels it was trained on.


• 整体方向上，Gemini 说“从技术问题转向策略问题（标签、特征、模型校准）”是对的，但结合你现在这套框架和代码细节，有几点需要纠正和
  补充，才能真正朝“赚钱”走。

  1. 关于 Label（标签）

  - 你现在主线已经不是“95% hold / 5% buy”的分类问题了，而是：
      - label_profile = intraday_reg_5m，回归目标是“未来 5 根 1m K 线的 log-return 累积”。
      - 没有显式的“hold 类别”，所以“类别极端不平衡”不是最重要的问题。
  - 真正要看的，是：
      - y 的分布是不是太窄、几乎全在 0 附近（比如绝大多数在 [-0.0002, 0.0002] ），
      - 正负样本是否比较对称（正/负收益数量接近），
      - 有没有明显的 outlier（虽有剪裁/winsorize，但仍可能有长尾）。
  - 所以：“要检查标签分布”这个建议是对的，但“95% hold”这个类比不完全贴合你的当前配置。
    下一步确实可以抽一段训练窗口（比如 TSLA 2025-09 一个多月）直接画 y 的直方图、统计正负比例，看是不是整个目标本身就“信号
    太弱”。

  2. 关于 Features（特征）

  - 你现在的特征结构是有层次的：
      - 基础：vwap, atr_14, ret_1m, ret_5m, rsi_14, volume；
      - 生产 HPO 配置里还有 DSL feature program（feature_programs.json），甚至有 Featuretools/DSL v2 管线。
  - Gemini 说要看“特征是否有预测力、看特征重要性”，这点完全正确，而且在你的框架里是可行的：
      - 对 XGB/LGBM 这类树模型，可以直接输出 feature importance；
      - 更基础的，可以先算每个特征与未来收益（y）的 IC（Spearman 相关），看看有没有长期稳定为正/负的特征。
  - 目前从结果看：
      - oos_avg_ic = 0、ic_coverage = 0，说明现在这一轮 run 的模型几乎没学到可用的 cross-sectional edge（或者样本太少）。
  - 所以：“要检查特征有效性”这个建议是对的，而且比调整一点点阈值更关键——如果特征本身不带信息，怎么调 threshold 都不赚钱。

  3. 关于 Confidence / 校准

  - 现在 pipeline 已经做了简单的“置信度重标定”：
      - 对回归任务：conf = |pred| / (3 * std(pred_is))，再截断在 [0,1]。
      - 这比“直接把回归输出当 conf_floor”安全很多。
  - 日志里你看到的 conf ≈ 0.03~0.06，有两层含义：
      - 模型预测出来的未来 5m 收益本身就很小（绝大多数接近 0）；
      - 经标准差归一后，绝大多数预测都不属于“>3σ”的极端情况。
  - 在当前 backtest 配置下：
      - debug 回测我已经把 conf_floor 设成 0（只用来缩小仓位、不再硬挡），
      - 所以低 confidence 主要影响仓位大小，不再直接让你 “不交易”。
  - 也就是说：
    现在的“conf 很低”，更多是在反映“模型 edge 真不大 / 信噪比很低”，单纯再套一层 Platt / isotonic calibration 也不会突然变
    赚钱。
    真正决定赚钱与否的，还是“标签定义 + 特征 + 模型结构 + 折叠方式”。

  4. 哪些地方 Gemini 说得对，哪些地方要调整重点

  - 正确的部分：
      - 现在系统已经能跑完、有少量交易，瓶颈确实从“工程问题”转移到了“alpha 质量问题”；
      - 需要认真检查：
          - 标签分布（是否几乎全是噪声/太窄）；
          - 特征信息量（IC、重要性）；
          - 模型输出与实际 PnL/方向的一致性（而不是只看 IS Sharpe）。
      - “先分析这次 backtest 用的模型，而不是盲目再调参数”这个思路也是对的。
  - 需要调整的点：
      - 你这轮 debug run 的主要问题，其实还是过滤器和数据窗口：
          - 实盘 JSON 里的 min_volume=100000 把大部分 bar 挡掉 → OOS 几乎没交易；
          - debug 窗口只有 2 天，天生样本不足，不适合作为“赚钱能力”的依据。
      - Confidence 校准部分，你的框架已经做了第一层合理处理，现在更重要的是：
          - 在研究模式下，不要用太保守的 min_volume、max_spread、conf_floor 把所有机会挡死；
          - 先让策略“正常地多交易”，再谈“根据 conf 来做动态 sizing / 过滤”。

  5. 真正“为了赚钱”的下一步建议

  如果我们以“赚钱”为目标，现在更合理的路线是：

  1. 先用“研究专用配置”跑一轮长窗口 HPO：
      - 已经把 debug 配的 config_prefix 改成 enhanced_ml_debug，避免被实盘 JSON 干扰；
      - 建议你接下来跑的是主配置（带更长窗口）而不是 2 天 debug：

        venv/bin/python alpha_loop.py \
          --config alpha_config_enhanced.json \
          --provider coop_optuna \
          --gens 1 \
          --k-top 12 \
          --out_dir alpha_enhanced_ml_v1
      - 前提：如果你愿意，我可以再把主配置里也加上一个 config_prefix，专门用于 HPO/研究，让它也不吃实盘 JSON 的
        min_volume=100000。
  2. 针对“盈利/不盈利”的模型做诊断，而不是对这次两天的 debug：
      - 在 alpha_enhanced_ml_v1/gen_01/ 中挑出 1–2 个：
          - OOS 上 trades 足够多（比如每 fold 至少 5 笔）；
          - oos_avg_sharpe 接近或略高于 0；
      - 针对这些折/模型：
          - 抽出对应的训练数据窗口，做：
              - y 的分布图（均值、标准差、偏度、正负比例）；
              - 每个基础特征的 IC（Spearman 相关）；
              - XGB/LGBM 模型的 feature importance。
      - 如果在这些“相对最好”的组合里，特征 IC 仍然接近 0、标签几乎全是噪声，那才说明策略逻辑本身没 edge，需要从策略思想/特征设
        计上重构。
  3. 把过滤器和 sizing 调成“赚钱优先”的版本再评估：
      - 研究用配置里：
          - 把 min_volume 设在一个更现实的水平（比如 10k–20k），保证足够样本；
          - conf_floor 保持 0，conf_soft_threshold 可以放在 0.2–0.3，用于软缩小仓位；
          - OCO 先用比较宽的止损/止盈，减少过早的噪声止损。
      - 先让模型在一个“不过度谨慎”的环境中表现出来真实的 Sharpe，再逐步加严过滤器控制回撤。

  ———

  一句话总结：

  Gemini 提出“从标签、特征、校准去优化模型”的方向是对的，但在你这套系统里，现在最关键的是：先用研究专用的、不过度保守的过滤配
  置，在足够长的数据窗口上，跑出一套“有交易、有一点 IC/Sharpe”的模型，然后再分析标签分布和特征重要性。
  否则，你现在看到的“不赚钱”，更多是过滤器和样本量问题，而不是模型本身已经用尽。


  怎么知道 AI 大脑“干得对不对”，避免黑箱？

  你现在手里其实已经有一套“显微镜”：

  - 结构化审计文件（每天一套）：
      - runs/ai_trader/<date>/ml_review.json
          - 看它对当前 HPO 结果的诊断、next_actions、以及引用的关键数字（Sharpe、IC、trades）。
      - runs/ai_trader/<date>/ml_actions.jsonl
          - 每条 Brain 动作（backfill / extend_window / queue_job / fix_config / pause_ml ...）的时间线。
      - runs/ai_trader/<date>/hpo_retry_audit.json / hpo_stop_loss_report.json
          - 每轮 HPO 的 OOS 指标、gate 是否通过、Brain 决策前后的变化。
  - 配置变更的真实痕迹：
      - backups/policies/、backups/alpha_config/
          - 任何 Brain 通过 fix_config 改过的 ml_policy.json、oof_policies.json、gating_policies.json、
            alpha_config_enhanced*.json 都会有备份，可 diff。
  - 工具级“效果评估”（不是黑箱）：
      - tools/summarize_ml_actions.py：
          - 汇总 “某类 action → 后续 job 的 IC/AUC/ok_rate”，看哪类动作真的提高了 OOF 质量，哪类动作等于白忙活。
          - 专门做“有 Brain 干预 vs 没 Brain 干预”的成功率、平均 IC/AUC 对比，告诉你 Brain 整体是加分还是减分。
      - tools/nightly_oof_flow.py：
          - 会读取 ml_review.json 里的 next_actions，把 extend_window / fix_config 建议变成下一轮 OOF/HPO 的参数，形成可追踪的
            闭环。
          - cat runs/ai_trader/$(date +%F)/ml_review.json | jq '.review'
            → 看这次 Brain 的诊断和下一步动作。
          - cat runs/ai_trader/$(date +%F)/ml_actions.jsonl
            → 看它今天都做了哪些动作、有没有频繁乱动。
          - python tools/summarize_ml_actions.py --days 7 --runs-dir runs/ai_trader --output runs/ai_trader/$(date +%F)/
            ml_branches_summary.json
            → 汇总近 7 天每种动作的成功率，确认它的“习惯”是不是健康。

  总结监控思路：

  - 定义“对不对”的标准：OOS Sharpe / PF / 赢率 / trades / IC 是否在 gate 之上（gate 已在 gating_policies 和 orchestrator 里
    编码）。
  - Brain 的每一个动作 → 下一个 HPO/OOF job 的结果，都被结构化写入 JSONL，你可以用现有工具看：
      - 某类动作长期来看，是否提高了 OOS 指标；
      - 如果没有，你可以停用某类 autopatch（通过 config.yml 里的 brain.autopatch_* 开关），让它只给建议，不自动改。



可以完全让 AI 大脑自己“短跑→调参→长跑”，你只需要把几条长期跑的进程拉起来，之后就不用再手动敲训练命令了。

  下面分两种模式，你选一个即可。

  ———

  方式一：一次性晚上学习（你手动触发，后续短跑由 Brain+Scheduler 处理）

  步骤：

  1）开一个终端，启动控制事件消费者（执行 Brain 的动作：fix_config / queue_job 等）：

  ENV_MODE=paper venv/bin/python scripts/ai_trader/control_event_consumer.py

  2）再开一个终端，启动 ML 调度器（执行 queue_job：短跑/长跑 HPO、oof 等）：

  ENV_MODE=paper venv/bin/python scripts/ai_trader/ml_scheduler.py

  3）最后，在第三个终端按需触发一次完整的“晚上学习 + AI 大脑复盘 + 后续短跑队列”：

  ENV_MODE=paper venv/bin/python scripts/ai_trader/cli.py learn

  - 这一步会：
      - 跑一轮 orchestrator 的 evening_learning（多轮 alpha_loop HPO，有 60 分钟/round 超时保护）；
      - 写入 gen_summary.json + diagnostics；
      - 调用 AI 大脑 review_ml，它在看到结果后可以发出：
          - queue_job(job_type="alpha_loop"/"hpo_cli", params={n_trials, timeout_hours, ...}) 这样的短跑任务；
          - fix_config（比如切换 feature_program_id、后续我们也可以让它建议 label_profile）。
      - 这些动作会由你在步骤 1、2 启动的进程自动接力执行。

  ———

  方式二：完全自动循环（全天候，推荐放到 tmux/screen 里）

  如果你希望“全天候自动判断什么时候学习/什么时候短跑 HPO”，只要一个命令：

  ENV_MODE=paper venv/bin/python scripts/ai_trader/auto_loop.py

  - 这个脚本会：
      - 确保 control_event_consumer.py 和 ml_scheduler.py 两个进程一直在后台跑；
      - 周而复始地让 AI 大脑根据上一次结果，自己决定是否再发短跑 HPO / 调整配置。

  ———

  你现在可以怎么做

  如果你想先手动感受一轮“AI 大脑自己短跑+调参”的流程，建议先用方式一：

  1. 先启动 control_event_consumer.py 和 ml_scheduler.py 两个后台进程；
  2. 再跑一次：

  ENV_MODE=paper venv/bin/python scripts/ai_trader/cli.py learn

  跑完以后，你可以用：

  venv/bin/python tools/brain_daily_report.py --days 2 --runs-dir runs/ai_trader

  看 AI 大脑这轮都干了什么（有没有发 queue_job、有没有改配置），以及最近两次 HPO 的 Sharpe/IC 变化。

自检脚本已经落地，你可以直接用来“每天看一眼大脑干得怎么样”。

  1. 新增脚本

  - 文件：tools/brain_daily_report.py
  - 功能：
      - 看最近几天 runs/ai_trader/<date>/ 下的：
          - ml_events.jsonl（每个 HPO/alpha_loop/OOF job 的结果）
          - ml_actions.jsonl（Brain 的动作日志）
      - 输出一个报告，包含：
          - 昨天（target_date） Brain 做了哪些 fix_config 修改（path/key/value/job_id、时间戳）。
          - 最近两次 Alpha/HPO 相关 job 的 Sharpe/IC：
              - 上一次 job（prev）
              - 最新一次 job（last）
              - 它们之间的 ΔSharpe、ΔIC（最后一次相对上一次是提升还是下降）。

  2. 怎么用

    --days 7 \
    --runs-dir runs/ai_trader \
    --output runs/ai_trader/$(date +%F)/brain_daily_report.json

  脚本会输出类似摘要：

  - Brain Daily Report for 2025-11-14
  - Last job: alpha_loop id=... Sharpe=... IC=...
  - Prev job: ...
  - Delta vs prev: ΔSharpe=... ΔIC=...
  - fix_config actions on 2025-11-14: N change(s)

  同时 JSON 里还有详细结构（jobs.prev/last/delta，fix_config_actions 列表），方便你深挖。

  这样每天你都能快速看到：

  - 昨天大脑动了哪些配置（尤其是 ml_policy、gating_policies、alpha_config_enhanced 等）；
  - 最近的 HPO/alpha_loop 结果是往好的方向走，还是 Sharpe/IC 在变差。




  你现在可以直接跑的 orchestrator / Brain 流程命令

  先保证在仓库根目录、虚拟环境已准备好（venv）。

  - 单次完整“晚上学习 + Brain 评审”一轮：

  ENV_MODE=paper venv/bin/python scripts/ai_trader/cli.py learn

  - 跑完之后，看 Brain 每日自检报告：

  venv/bin/python tools/brain_daily_report.py --days 2 --runs-dir runs/ai_trader

  - 想更系统地评估 Brain 是否真的在帮忙（需要多轮 job 后）：

  venv/bin/python tools/validate_brain_effectiveness.py --days 7 --runs-dir runs/ai_trader

  你可以先连续跑几次 learn（哪怕同一天也可以，用来积累样本），然后用上面两个工具看：

  - OOS Sharpe/IC 是不是在往上走；
  - Brain 做的 fix_config/queue_job 具体是什么；
  - conf_floor 是否没有被悄悄调低。



  ##如果崩溃,crash炸掉vs code,机器学习
  而且现在这套系统就是为“VSCode挂了也能事后排查”设计的，不靠终端输出。

  1. 主日志：看 Orchestrator/learn 炸在哪里

  - 日志文件默认在（除非你改了 AUDIT_LOG_PATH）：
      - ~/grafana-stack/logs/trader.log
        或者你之前提到的：
      - ./logs/trader.log（如果你把 AUDIT_LOG_PATH 指到这里）
  - 这个文件里会记录：
      - ai_trader.orchestrator 的 INFO/WARN/ERROR；
      - Python 异常的 traceback（install_error_hooks() 已经把未捕获异常接到 logger 里）。
  - 查法示例（新终端里）：

    tail -n 200 ~/grafana-stack/logs/trader.log
    # 或者
    tail -n 200 ./logs/trader.log

  2. ML/HPO 级别：看 alpha_loop 本身为什么挂

  每次 cli.py learn 里跑 HPO / alpha_loop 时，都会写到：

  - runs/ai_trader/<日期>/ml_events.jsonl：
      - 每个 job 一行 JSON：status=started/completed/failed，job_type=alpha_loop，error_message。
      - 你之前贴过的 num_samples=0、invalid literal for int() with base 10: 'type' 就是从这里来的。
  - 如果是控制平面收到失败，还会有：
      - runs/ai_trader/<日期>/ml_failed.jsonl（按天记录失败详情）

  排查时可以：

  # 看今天的 ML 事件
  cat runs/ai_trader/$(date +%F)/ml_events.jsonl

  3. 回测/HPO 细节：看具体哪一轮/哪个 trial 出错

  - alpha_out/daily/ 下（或你在 config 里指定的 out_dir）：
      - index.json：整轮 HPO 的 OOS 总结；
      - gen_*/gen_summary.json：每代的结果和 diagnostics（label_stats/feature_ic 等）；
  - Orchestrator 还会写：
      - runs/ai_trader/<日期>/hpo_retry_audit.json：每一轮 retry 的状态、错误（如 alpha_loop_failed、gate_failed_retry 等）。

  4. VSCode 崩溃的影响

  - VSCode 关掉/终端挂掉，只会影响当前前台进程的输出，不会影响：
      - trader.log 的写入；
      - ml_events.jsonl、hpo_retry_audit.json 等文件。
  - 也就是说，只要进程在崩之前有机会 log，事后都能从这些文件里把原因追回来。




  # #停止
  一键停止脚本
  venv/bin/python scripts/ai_trader/stop_brain.py
  ps aux | grep 'alpha_loop.py'
  kill





如何看跑的怎么样: 每天跑完 auto_loop 之后，先看这一条（强烈推荐）

  source venv/bin/activate
  venv/bin/python tools/brain_daily_report.py --days 2 --runs-dir runs/ai_trader

  这个基本是“一键总览”：

  - 显示最近一次 ML job（oof/alpha_loop/hpo_cli）的 Sharpe/IC 和上一轮的对比（ΔSharpe/ΔIC）。
  - 列出昨天大脑做过的 fix_config/queue_job/backfill/reduce_window 等动作。
  - 输出一段 summary 文本 + 一份 JSON（你可以存起来做长期对比）。

  如果这个报告看上去是“IC/Sharpe 持平甚至变差 + 一堆 data_corruption / ic_nan”，就说明大脑现在还在“乱动期”；如果看到 IC/Sharpe
  有提升，而且 fix_config/fix_data 这些动作逐渐收敛，就说明在往正确方向走。

  2. 想看大脑总体表现，用这条

  venv/bin/python tools/validate_brain_effectiveness.py --days 7 --runs-dir runs/ai_trader

  - 看最近 7 天：
      - 大脑发了多少 action；
      - 每类 action（reduce_window/backfill/queue_job 等）的成功率（IC/AUC 有没有好转）；
      - 有没有“全都是 data_corruption / ic_nan”这种情况。

  3. 针对某个具体 job 深挖（按需，不是每天都要）

  从 brain_daily_report 里挑一个你关心的 job_id，比如最近的 OOF，然后：

  venv/bin/python scripts/ai_trader/tools/review_ml_job.py \
    --job-id <job_id> \
    --runs-dir runs/ai_trader

  - 会生成 runs/ai_trader/<date>/ml_review_<job_id>.json：
      - 里面有这次 job 的 summary（AUC/IC/Sharpe/coverage）、diagnosis（大脑判断的问题）、next_actions（下一步打算做什么）。

  ———

  总结一下：

  - 日常一键看：brain_daily_report.py（看方向对不对）。
  - 看大脑最近一周是不是在“瞎折腾”：validate_brain_effectiveness.py。
  - 对某次 OOF/HPO 细看：review_ml_job.py。

新增一键查看:
venv/bin/python tools/brain_dashboard.py
 会依次输出三块：
      - 0. 基础文件自检：今天/昨天的 ml_events.jsonl、ml_actions.jsonl 是否存在。
      - 1. Daily Brain Report：昨天 vs 上一次 ML job 的 Sharpe / IC / fix_config 动作。
      - 2. Brain 效果验证：最近 7 天 Brain 动作成功率、基线对比、窗口大小相关性等（就是原来的 validate_brain_effectiveness 全
        输出）。

  可选参数：

  # 调整分析窗口天数（传给 validate_brain_effectiveness）
  venv/bin/python tools/brain_dashboard.py --days 7 --runs-dir runs/ai_trader




  你如果想看某次特征实验的详情，可以直接查：
      - runs/ai_trader/<date>/features_build/status.json（视图是否建好）；
      - runs/ai_trader/<date>/feature_eval/report_*.json（auc_lift/ic_lift/coverage/leakage）；
      - 或配合 brain_dashboard.py 和 review_ml_job.py 做更细的分析。





##
1. 现在的自动链路到底长什么样？

  当 LLM 在 review_ml 里给出 propose_features 行动时，整条链路是：

  1. propose_features → 写入 catalog
      - 在 control_event_consumer 里：
          - _translate_brain_actions 把 propose_features 正规化为：
            {"version": "...", "specs": [{name, expr, dtype, notes}, ...]}
          - _execute_ml_actions 里：

            ok, reason = _write_features_catalog(params)
            if ok:
                _enqueue_ml_job({"job_type": "feature_build", "params": {}}, job_id)
          - _write_features_catalog 会把这些 specs 合并进 config/features_catalog.json 的 "candidate"，更新 version/updated_at。
          - spec 如需跨频 (例如使用 trading.features_5m 或 trading.features_1d_prev)，需要在 JSON 里加 `"required_sources": ["m5","d"]`；feature_build 会自动 LEFT JOIN 这些 resample 表（通过 `tools/feature_resample.py` 从 features_1m 生成）并保证只引用上一窗口的数据，防止未来泄漏。
          - 如需重新生成 `trading.features_5m / features_1d_prev`，可以 queue_job(job_type="feature_resample", params={"sources": ["m5","d"]})，调度器会串行调用 `tools/feature_resample.py` 完成刷新。
          - 当 label_stats.zero_frac→1 且 label_bps 过高时，可先 queue_job(job_type="label_diagnostics", params={"symbols":["TSLA"]}) 观察标签分布, 再 queue_job(job_type="label_recompute", params={"symbols":["TSLA"],"start":"2025-11-20 09:30:00","end":"2025-11-20 16:00:00","horizon_min":5,"label_bps":5}) 让调度器调用 `tools/recompute_labels_1m.py` 重新生成标签再继续 HPO。
          - 需要补历史数据 >5 天时，不要反复调用 backfill(days=5)，直接 queue_job(job_type="bulk_backfill", params={"symbols":["TSLA"],"start_date":"2024-01-01","end_date":"2024-06-30","bar_size":"1m","max_days_per_run":30})；调度器会自动按月/块调用 `docs/daily/trigger_backfill.py`，覆盖整段时间。
          - 想要先在沙箱里验证特征提案，可 queue_job(job_type="feature_experiment", params={"specs":[{...}]})；系统会写入临时 catalog、运行 feature_build + feature_eval，并返回 AUC/IC 提升，不会污染生产 catalog。通过 queue_job(job_type="feature_promote", params={"from_catalog":"runs/.../feature_experiments/<job>/catalog.json","spec_names":[...]}), 可以将通过 gate 的特征从沙箱提升到生产 features_catalog.json（candidate 或 active）。
          - 想要在执行层比较不同 ensemble 模式或 gating 配置，可 queue_job(job_type="strategy_experiment", params={"symbols":["TSLA","AAPL"],"start":"2025-07-01 09:30:00","end":"2025-08-31 16:00:00"}), 调度器会调用 `tools/backtest_ensemble_compare.py` 并在 artifacts 中留下 compare.json 供 Brain 分析。
  2. feature_build job → 建候选特征视图
      - ml_scheduler._execute_feature_build 调用：
        tools/feature_build.py --catalog config/features_catalog.json --out runs/.../features_build
      - 这一步：
          - 在 ClickHouse 上创建 trading.features_1m_candidate_v<version> 视图；
          - 输出 status.json（ok/built/version/view）。
      - feature_build 完成后，在 control_event_consumer._evaluate_and_apply_on_success 里：
          - 自动 enqueue 一个 feature_eval job：

            _enqueue_ml_job({"job_type": "feature_eval", "params": {}}, job_id)
  3. feature_eval job → 小窗口 OOF + 特征质量诊断
      - ml_scheduler._execute_feature_eval 调用：
        tools/feature_eval.py --catalog config/features_catalog.json --out runs/.../feature_eval
      - feature_eval.py 做的事情就是你要的“小窗口 OOF 检验”：
          - 先读 baseline：runs/ai_trader/<day>/oof_eval/oof_eval.json，拿到 base_auc, base_ic, base_n_bars；
          - 对候选视图 trading.features_1m_candidate_v<version> 跑一遍 evaluate_oof_quality.py：

            python tools/evaluate_oof_quality.py \
              --symbols <baseline symbols> \
              --start <baseline start> \
              --end   <baseline end> \
              --features-table trading.features_1m_candidate_v<version> \
              --horizon-min 5 \
              --out runs/.../feature_eval/candidate_v<version>/
          - 计算：
              - auc_lift = cand_auc - base_auc
              - ic_lift = cand_ic - base_ic
              - coverage（candidate n_bars / baseline n_bars）
              - max_inter_feature_corr（候选之间的相关性）
              - leakage（候选对 fwd_ret 的相关性比 ret 高太多时标记）。
          - 写 report_<version>.json，并通过 stdout 吐出简单的 {ok, auc_lift, ic_lift, coverage, leakage}，ml_scheduler 把这些封装成 metrics。
  4. feature_eval 结果 → gate + 进一步动作
      - 在 control_event_consumer._evaluate_and_apply_on_success 里，对于 job_type=="feature_eval"：
          - 从配置 policies.features.success_gates 读取门槛：
              - auc_lift_min、ic_lift_min、coverage_min、leakage_forbidden、max_inter_feature_corr；
          - 根据 feature_eval 的 metrics 判定 pass/fail：
              - 通过 → enqueue feature_apply job（把 candidate 提升为 active）；
              - 不通过 → 记录 features_success_gate_fail，不应用这批特征。

  这条链路已经完全走通，而且是自动挂在 Brain 动作之后，不需要你手动跑命令。
