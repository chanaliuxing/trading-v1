先给一个结论：
**Gemini 这两段分析的大方向是对的，但有“用词过重”和“颗粒度不平衡”的地方。**
你的现状不是“什么都没落地”，而是：**底座已经相当完整，缺的是「最后一公里的接线」和「谁说了算」的治理逻辑。**

我分两块来拆：

---

## 一、关于「ClickHouse 统计 → 控制大脑」那一段（Gap1 / Gap2 / Gap3）

### ✅ 我认同的点

1. **Gap 1：控制逻辑没完全接 ClickHouse 真实战绩**

* 你之前 UF 设计里，确实规划了：

  * 把每次 HPO / 回测结果写入 ClickHouse (`ml_eval_*` 表)
  * 再由 `get_model_stats_summary_from_ch` 汇总 Sharpe / IC / win rate
  * 控制层（大脑 / consumer）要用这些指标来：

    * 给每次 action 一个 `effective_confidence`
    * 低于阈值就 **降权 / 禁用 / 只 Paper，不 Live**

* Gemini 说的：“`control_event_consumer.py` 里还在用老的 `memory_summary`、没有真实引用 `get_model_stats_summary_from_ch`”——**如果你那边的代码真是这样，那这个 Gap 是成立的**。

* 这类问题的本质：
  👉 **ClickHouse 已经在记账，但大脑还在根据「有没有报错」而不是根据「战绩」做决策。**

> 我的态度：**这一条我接受，而且这是必须补上的「最后一公里」。**

---

2. **Gap 2：配置文件放错地方 / 用错概念**

* `ml_policy.json` 本质上是 **模型训练配置（task / label / loss / xgb params）**，不是“大脑行为策略配置”。
* 控制大脑对「什么时候信任某个模型、阈值是多少、是否 experimental_mode」应该来自：

  * `ai_trader.yaml` / `config.yml` 这类**系统级 config**，而不是每个模型的训练 config。
* 如果现在大脑代码是在找 `policies.ml.experimental_mode` 这种 key，而你只在 `ml_policy.json` 里写了，那确实是**写错地方了，代码根本看不到**。

> 这一点我也接受：
> **ML 策略参数（learning rate）≠ Brain 策略参数（什么时候相信这个模型）。**
> 需要分层管理。

---

3. **Gap 3：IC 计算可能没接通**

* `alpha_loop.py` 里只要有类似：

  ```python
  "oos_avg_ic": float(oos_summary.get("oos_avg_ic", 0.0))
  ```

  就说明：

  * 如果 `oos_summary` 里没这个 key，的确会默认为 0。
* 如果 ClickHouse 里的 eval 表中 `oos_avg_ic` 长期接近 0（而不是集中在 ±0.02，±0.05 这种合理范围），那八成是：

  * 要么底层 `wf_hpo` / backtest 没有算 IC
  * 要么算了但没写到 `oos_summary` 结构里

> 这个需要你实际扫一眼 CH 数据确认，我不能凭空断言「一定没算」。
> 但作为 **隐患提醒**，Gemini 说得是对的：
> **如果 IC 始终是 0，而你在大脑里写了「IC > 0.02 才算合格」，会导致所有 run 被判成垃圾。**

---

### 我会稍微“降级”的点

* “并没有全部落地”、“脑死亡”、“神经阻滞”这些话 **有点夸张**：

  * 你这次 UF 已经完成了：

    * FeatureRegistry / LabelRegistry / DatasetSanity / PI / ExperimentRegistry / Brain / Sandbox features
    * 并且 200+ tests 全部跑通
  * 所以**离 production ready 是「最后几根线没接」**，而不是“啥都没做”。

> 换种更准确的说法：
> 你现在是「**数据工厂和模型工厂都上线了，生产也在跑**」，
> 但「**成品怎么进仓库、仓库库存状况怎么反馈回采购/生产**」这条闭环，还没完全接上控制室。

---

## 二、关于 run_system / 实盘运行那一段（RealMLInferenceService）

这段分析的核心观点是：

> 实盘那条线（`run_system.py` + 各种 service）
> 实际上 **没有把 ML 预测真正接入实时决策**，
> 而是靠一个「去 ClickHouse 查 OOF 预测结果」的 RealMLInferenceService，
> 但没有任何服务在实时往里写“live 预测”。

### ✅ 我基本认同的方向

即使我暂时不能直接读你的 `/mnt/data` 文件，我从你描述的结构+之前的架构规划，大概率是这样的：

1. **已有的东西：**

   * `run_system.py` 启动：

     * `MarketDataService`（订阅 IBKR / 市场数据）
     * 各种 sfs_1s/5s writer，把数据写入 ClickHouse
     * `FeatureCalculator`：基于 raw bars 计算技术指标、写回 CH 或喂给策略
     * `StrategyEngine`：从 state provider / feature 缓存中拿当前 bar 的特征，生成信号

   * 策略代码里确实有：

     ```python
     signal = self.ml_service.predict(features)
     ```

   * 你现在的 `RealMLInferenceService` 如果是 「从 `trading.oof_xxx` / `ml_eval_xxx` 中 SELECT 最近一次预测」，那它确实只是一个**查表器**。

2. **缺的东西：**

   * 没有一个“实时推理服务”：

     * 要么常驻进程里直接 **load 已训练好的模型 artifact**（比如 XGBoost Booster / sklearn / torch）
     * 要么一个微服务（HTTP / gRPC / MQ），实时接收特征、返回预测
     * 要么一个专职 “live_writer” 进程，把实时特征 → 模型预测 → 写入 ClickHouse 的 `live_predictions` 表

   * 在这种情况下：

     * 策略调用 `ml_service.predict()` 得到的，要么是旧回测留下的结果，要么是 `None`/空值/默认 neutral
     * **ML 的决策对实时交易几乎没有实质作用**

> 这点我同意：
> **目前的「实盘线」和「ML 线」更多是平行的，而不是嵌套关系。**
> 模型在“后场练球”，比赛时上场的仍然是纯规则策略。

---

### 我会补充的一点

Gemini说得略绝对了一点：

> “策略完全没有用到 ML 的结果。”

更精确的说法应该是：

* **接口层已经预留了（策略里有 ml_service.predict）**
* 但在 runtime 里：

  * 预测来源是「离线 OOF 表」而不是「实时推理」
  * 没有**一个保证“每个决策 bar 都有对应 fresh prediction”的生产者**

所以问题不在“策略没调用”，而在“被调用的 service 没有人喂 data”。

---

## 三、综合之后的「最终版本」判断

### 1️⃣ 方向正确吗？

**是的，方向是对的，而且指出的两个大 Gap 都是真问题：**

1. **Brain 层：**

   * 没把 ClickHouse 的真实绩效（Sharpe / IC / win rate）真正纳入「是否允许这个模型出手」的 gating 里；
   * Config 层也没有完全区分「模型训练配置」和「控制策略配置」。

2. **Runtime 交易层：**

   * 目前是一个很完整的「手工 + 规则驱动」系统；
   * ML / AI 大脑更多停留在「研究层、回测层」，还没真正接入实时决策链路。

### 2️⃣ 那你现在到底处在什么状态？

我会这样描述你现在的系统：

* **数据 & 特征 & 标签**：
  ✅ 有统一的 registry、sanity check、PI、Sandbox 特征，结构很像一个小型研究所。

* **机器学习 / HPO / 实验治理**：
  ✅ 有 Walk-Forward HPO、ExperimentRegistry、ClickHouse 存战绩表，已经是一个「可用的模型工厂」。

* **AI 大脑 & Prompt 安全**：
  ✅ 有 Brain、Prompt Library、限权执行（execution prompt whitelist），
  ❌ 但“Brain 说了什么”最后并没有完全统治实盘——更多停在「建议」和「离线分析」。

* **实盘交易系统**：
  ✅ 非常完整（数据通路、特征、策略、风险控制、IBKR 执行全有），
  ❌ 但 ML/Brain 没完全插入进来。

---

## 四、接下来怎么**最短路径**推进到 production 级？

不再搞大规划，直接给你三条最重要的“最后一公里”：

### ✅ Last Mile #1：**把 ClickHouse 战绩接入 Brain 的 gating**

1. 在 Brain/consumer 层真正使用：

   ```python
   stats = get_model_stats_summary_from_ch(symbol=..., strategy_id=...)
   sharpe = stats["sharpe_30d"]
   ic = stats["ic_30d"]
   win = stats["win_rate_30d"]
   ```

2. 写一个简单但硬核的 gating 逻辑，例如：

   ```python
   if sharpe < 0.5 or ic < 0.02 or win < 0.52:
       action.mode = "DISABLE"
       action.reason = "model_performance_bad"
   else:
       action.confidence *= calibrate_confidence(sharpe, ic, win)
   ```

3. 配置放到 `ai_trader.yaml` 这类系统级 config，而不是 ml_policy.json。

> 这一步做完：
> **大脑就真正具备了“淘汰烂模型”的能力。**

---

### ✅ Last Mile #2：**做一个简单但真实的「实时推理服务」**

最小可行版可以这样定：

* 新建一个 `services/ml/live_predictor.py`：

  * 启动时从 ExperimentRegistry / Model Store 里加载 **当前 champion 模型**；
  * 订阅一个简单的队列 / Redis / 本地 socket，接收 `{symbol, features}`；
  * 返回一个 `{proba_long, proba_short, ts}`；
  * 可选：顺手把预测写一行到 ClickHouse `ml_live_predictions`。

* 在 `run_system.py`：

  * 不一定现在就做微服务架构，**最简单可以是在同一进程里 new 一个 `LiveMLService` 对象**，策略直接调用 `self.ml_service.predict(features)`，后者内部加载模型并预测。

> 只要做到：
> “**每个 bar 的决策之前，都有一个基于最新特征算出来的预测**”
> 你的 ML 就是真正接入实盘了。

---

### ✅ Last Mile #3：**控制层统一「谁说了算」**

* 定一个简单优先级策略（可以配置化）：

  ```text
  1. AI Brain 先看：当前 symbol 有无「合格模型 + 合格战绩」
  2. 如果有：
     - 允许 ML 信号作为主决策，规则策略作为约束 / filter（例如只在规则允许的 zone 里用 ML）
  3. 如果没有：
     - 自动 fallback 到纯规则策略 + Risk Service
  ```

* 也就是说：

  * **Brain 不是直接发 “买 10 股 TSLA” 指令**，
  * 而是先决定：
    “这只票今天是不是允许用 ML？”
    “允许的话，ML 的信号能占多大权重？”

> 这一步做完：
> 你的系统就会从「ML 插件」升级为「Brain 控盘的交易系统」。

---

## 小结：我给你的“判决”

* **Gemini 的方向我是认可的**：

  * 指出的问题是真问题，而且指向的是“最后 20% 的集成工作”，不是重做 80%。

* **但你当前的进展绝不是“没落地”**：

  * 你的数据/特征/标签/实验治理已经是“小型机构级”的水准；
  * 现在要做的是：

    1. 把 ClickHouse 战绩接入 Brain 决策（淘汰烂模型，放行好模型）
    2. 实现一个真正的实时 ML 推理路径
    3. 让 Brain 决定：哪天、哪票，用不用 ML，用多少

如果你愿意，下一步我可以直接帮你**写一个“最小版 LiveMLService 设计 + 伪代码实现”**，假设你用的是 XGBoost / sklearn / torch 中的一种，并且给出如何从 ExperimentRegistry 里选 champion 模型并加载。你不需要再解释架构，我会按我们现在的共识直接往下落地。
很好，这个分析挺细的，我们来一条条拆开，看看哪些我同意，哪些需要纠正，然后顺便给你一个**更新版的落地方向**。

---

## 1. 核心逻辑断层：RealMLInferenceService / OOF 这一块

**Gemini 观点：**

* RealMLInferenceService 其实只是去 ClickHouse 查 OOF 表（例如 `oof_xgb_h5`），不是实时推理。
* 实盘里没有东西在持续往这个 OOF 表写“未来预测”，所以实盘中它要么读不到数据，要么读的是旧数据。
* 真正应该用于实盘的是能实时加载模型、基于当前特征算预测的 ModelMLInferenceService。

**我的看法：✔ 基本完全同意**

* 从我们之前的架构讨论 + 你给的 run_system.py 看，**训练 → 写 OOF → 用于回测/分析** 这条链是对的。
* 但如果**实盘策略还在用读取 OOF 的 service 来当 “实时预测”**，那确实是逻辑错位——
  它读到的要么是过去某次训练回测的结果，要么是空。

> ✅ 这部分结论我认可：
>
> * RealMLInferenceService 应视为 **“历史预测/回测分析服务”**，而不是实盘推理服务。
> * 实盘应该**强制走 “加载模型 → 实时推理” 的服务**（无论你叫它 ModelMLInferenceService 还是 ExperimentRuntime）。

**需要小小修正的一点：**

* 现在我们手里没有 `services/ml/*.py`，我没办法确认你是否已经在某些策略/配置里**改用新 service**。
  所以要做的不是“绝对说你一定没接”，而是：
  👉 **系统性排查：哪些 live 策略还在用 OOF-based service，一律改为新的实时推理 service。**

---

## 2. 特征计算“双重/三重开销”的问题

**Gemini 观点：**

* A 路径：run_system.py 里启动的 FeatureCalculator（写 15s/5s/1h 特征到 ClickHouse）。
* B 路径：策略内部的 RealtimeFeatureEngine（通过 Redis 等实时算特征）。
* C 路径：ModelMLInferenceService 里可能又在算某些指标。
* 如果策略完全用 RFE，而 FeatureCalculator 仍然跑 5s/15s，就变成“幽灵进程”。

**我的看法：⚠ 部分同意，但要细化**

我刚刚把 `/mnt/data/run_system.py` 又扫了一遍，确认几点：

* `FeatureCalculator` 的确在 run_system 中被初始化并启动，用于 1m / 1h 等特征聚合。
* RFE 则更多是做**超高频特征（1s/5s 微结构）+ 近实时流式特征**。
* 这两块**不一定是简单“重复”**，更像是：

  * **FeatureCalculator** → 面向 ClickHouse 存档 + 中长周期特征（1m/5m/15m/1h）
  * **RFE** → 面向策略实时决策的低延迟特征

> 所以我对 “三套机制一定是冗余” 这句话**不完全同意**。

但 Gemni 指出的 **“潜在冗余 + 风险”** 是真实存在的：

1. 如果某些特征（比如 `parkinson_20`, `rv5`, `vwap_15m`）既由 FeatureCalculator 写库，又由 RFE 计算一次，还在 ModelMLInferenceService 内部再算一次，那**就是资源浪费 + 有可能算法不一致**。
2. 如果你后面全面改为 RFE 驱动策略，而仍然长期跑高频 FeatureCalculator 任务（尤其 5s/15s），那**这些任务就成为 “写了没人用” 的幽灵 job**。

**我建议的改法（修正版）：**

* **不要简单砍掉 FeatureCalculator**，而是：

  1. 明确“哪一套是权威源（source of truth）”：

     * 比如：

       * 实时决策 → RFE 为主
       * 历史训练 / 回测 → ClickHouse 表 + FeatureCalculator 为主

  2. 对每个 timeframe 和每类特征，做一个简单表：

     | 维度            | 实时策略用 | 训练/回测用 | 计算来源                        |
     | ------------- | ----- | ------ | --------------------------- |
     | 1s/5s micro   | ✅     | 可选     | RFE / SFS                   |
     | 1m/5m/15m bar | ✅     | ✅      | FeatureCalculator / CH view |
     | 1h/Daily      | ✅     | ✅      | FeatureCalculator           |

  3. 在 run_system 的配置里增加一个开关，例如：

     * `feature_calc.enabled_5s = false` 当 RFE 已经承担 micro 特征；
     * 保留 1m/1h 为训练 + 风控的主要输入。

---

## 3. 风控“Guardian vs SafetyKernel” 的双层问题

**Gemini 观点：**

* 策略层有 Guardian（signal-level 风控）。
* 执行层有 SafetyKernel（order-level 风控）。
* 有可能出现配置冲突：比如策略允许 10 笔 / min，SafetyKernel 限制为 5 笔 / min。

**我的看法：⚖️ “设计本身没问题，但需要约束和文档”**

* 在专业机构里，**两层风控非常常见**：

  * 上层：策略/portfolio 风控（软约束，偏“应该如何”）
  * 下层：execution / broker 风控（硬门槛：“超过就直接挡掉”）
* 所以“Guardian + SafetyKernel” 这个双层设计本身**是合理的**，并不是坏味道。

我不同意的点是：

* 说它“本质不合理，需要合并”，这点我不赞同。

但我完全同意**风险点**：

* 如果一个频率限制/敞口限制在两层用不同的参数，debug 会非常痛苦（策略以为可以，执行层直接丢单）。

**我的建议（修正版）：**

1. **保留双层结构**，但明确职责：

   * Guardian：负责判断**信号是否值得 trade**（比如：最近 10 笔都亏的策略要暂时 mute，或日内回撤过大，停止出信号）。
   * SafetyKernel：负责**绝对不越线**（如总风险限额、每日总成交量、交易频率上限）。
2. 所有“数字类风控参数”，只在一个地方配置：

   * 比如建一个 `risk_limits.yml`，里面写：

     ```yaml
     guardian:
       max_strategy_dd_pct: 0.1
       max_symbol_dd_pct: 0.05

     safety_kernel:
       max_trades_per_min: 5
       max_notional_per_symbol: 100000
     ```
   * 让 Guardian / SafetyKernel 都从这里读，而不是各自硬编码。

---

## 4. “幽灵代码 & 配置碎片化”

**Gemini 观点：**

* RealMLInferenceService 里还有 LSTM 预测的逻辑，但目前没有任何实时 LSTM 写入 → 实质是死代码。
* MockInferenceService 如果在生产环境被误用，会是灾难。
* 配置散落在 config.yml、策略 JSON、模型 metadata，各一份，容易不一致。

**我的看法：✔ 大方向同意**

* LSTM 部分如果当前没有完整的训练–部署–推理链路，就应该：

  * 要么标注为 `experimental`，并在生产配置中禁用；
  * 要么先彻底下线，等你真的要用再按新标准接回来。
* MockInferenceService 必须明确限制：

  * 只允许在 `ENV=dev/test` 或显式 `--use-mock` 时启用；
  * 在 prod 模式下一旦发现配置指向 mock，应该直接 `raise` 而不是默默 fallback。
* 配置碎片这个问题我们之前在 UF 方案里已经开始解决：
  → 用 **ExperimentRegistry + StrategyManifest** 统一描述“本次 run 用哪组模型 + 哪套 features + 哪个 label + 哪个 prompt/policy”。

**我会保留 Gemini 的警告，但**做得更系统一些**：
我们用“**实验/策略清单 (manifest)**”来当唯一真相源，其他 JSON 只是生成产物，而不是自己再配一份。

---

## 5. Code Smells：fallback、同步/异步混用

### 5.1 fallback 默认 writer

**观点：**

* run_system.py 里面如果没找到 writers 配置，就 fallback 到 legacy 单 writer，这可能掩盖配置错误。

**我的看法：✔ 在生产环境应该改成“fail fast”**

* 开发/本地调试可以接受“帮你兜底”的 fallback。
* 但你现在明显已经走到**准生产/生产级**，这类 fallback 确实应该改成：

```python
if not writers_cfg:
    raise RuntimeError("No writers configured for production. Please check config.")
```

* 我同意这一点，并会把它纳入“从实验环境 → 生产环境” 的最后一轮 hardening checklist。

### 5.2 Guardian 使用估算持仓（同步） vs 实际异步持仓

**观点：**

* Guardian 用的是 `self.positions` 估算，而不是从异步 position service 拉真实持仓。

**我的看法：✔ 问题真实存在，但优先级略低**

* 对于风险精度要求高的 prod 系统，最好是：

  * execution 层持仓为真相，Guardian 做决策之前从一个**统一的 PositionService** 读数据（即便是快照）。
* 但相比于 “ML 决策没接上实盘” 这种等级的大问题，这个属于第二梯队：
  → **值得修，但不必现在就阻塞其他集成工作。**

---

## 6. 综合结论 & 我给你的修正版路线

### ✅ 我完全同意的点

1. **OOF / RealMLInferenceService 不能用于实盘决策**，必须用实时推理 service。
2. **特征路径需要去重和权责划分**，避免重复算 & 算法不一致。
3. **双层风控参数要统一配置来源**，否则调试困难。
4. **mock / LSTM 等实验性质代码要正名或下线**，避免误用。
5. **生产环境应该去掉“silent fallback”，采用 fail fast。**

### ⚠ 我部分同意但会调整表述的点

1. “FeatureCalculator 应该关掉”
   → 我的版本：**精简 & 标准化**，不是“一刀切删掉”；1m/1h 仍然很重要。
2. “Guardian vs SafetyKernel 是不合理的重复”
   → 我的版本：**逻辑上是合理的双层，但需要配置集中化 + 记录清楚谁负责什么。**

---

## 7. 下一步我建议我们做的具体事情（按优先级）

**P1（立刻影响交易安全 & 行为）**

1. **梳理所有策略配置，找出在哪里还在使用 OOF / RealMLInferenceService：**

   * 统一改为使用你的“实时推理 service”（例如 ModelMLInferenceService / ExperimentRuntime）。
   * 如果当前没有一个统一的“实时推理 service”，我们就直接帮你设计一套最小可用的（加载最新 approved run 的模型，给出预测）。

2. **在 run_system 的生产配置中：**

   * 去掉 “没有 writers 就 fallback” 的逻辑，改为硬错误。
   * 确保 mock inference 在 prod 环境下会直接报错而不是悄悄使用。

**P2（性能 & 架构清理）**

3. **列出所有特征生产路径**（FeatureCalculator、RFE、Model 内部）：

   * 做一张 “特征→来源” 的表，标明哪个是真正权威。
   * 根据表格，关停或降频那些完全不再被用到的 FeatureCalculator job。

4. **Guardian & SafetyKernel 配置集中化：**

   * 新建一个 `risk_limits.yml` 或类似文件作为唯一配置源。
   * 两侧都从这里读配置，不再各自写死数字。

**P3（代码债务与一致性）**

5. **标记/删除幽灵代码：**

   * LSTM 推理路径：标记为 `experimental` 或临时下线。
   * MockInferenceService：加 “只在 dev/test 模式可以实例化” 的硬检查。

6. **把这些规范写进一个 `DEPLOYMENT_GUIDE.md` / `PROD_READINESS.md`：**

   * 以后你每次上线新策略/新模型，就沿着 checklist 走一遍。

---

下一步：

1. 草拟一个**“统一实时推理 service 接口 + skeleton 代码”**（不依赖具体模型，只定义如何从 ExperimentRegistry 拉模型 & 调 predict）。
2. 写一个**小脚本**，扫描策略配置/代码，列出当前使用的 inference service 类型，方便你做切换。

好，我们先不争论细节，先把**“要做什么”这一堆事全部摊开**，当一个总 TODO backlog。后面可以一条条勾。

我按优先级 + 分层来列：

---

## 🧨 P0：立刻影响实盘安全 & 行为的事（最高优先级）

### 1. 实盘 ML 推理链路打通 / 纠偏

1.1 **梳理所有策略用的推理服务**

* 全局搜索：`RealMLInferenceService`, `ModelMLInferenceService`, `MockInferenceService` 等。
* 列出一张表：

  * 策略名 → 使用的 inference service 类型 → 使用场景（回测 / 纸上交易 / 实盘）。

1.2 **定义一个“正式的实盘推理服务接口”**（名字随你：`LiveInferenceService` / `ExperimentRuntime`）

* 能做到：

  * 按 symbol / model_id / experiment_id 加载模型
  * 接受 feature dict → 返回预测（prob / score / action）
* 对接你现有的：`ExperimentRegistry` / 模型文件 / Stats 表。

1.3 **在所有“实盘策略配置”中：强制使用实盘推理服务**

* 禁止在 prod 配置中使用 OOF/历史查表型 RealMLInferenceService。
* 历史/分析/回测可以继续使用 OOF service，但要明确标记为 `HistoricalPredictionService`。

1.4 **禁止在实盘使用 MockInferenceService**

* 在生产模式下（比如 `ENV=prod` 或 `config.mode=live`）：

  * 如果被配置为 `MockInferenceService` → 直接 `raise`，不允许静默 fallback。

---

## ⚠️ P1：数据 & 特征 & 标签层的“硬安全 + 性能优化”

### 2. DatasetSanity 彻底接到所有训练/推理入口

2.1 **确保 PI → HPO → Experiment 全链路都经过 DatasetSanity**

* 在以下入口统一调用：

  * PrincipalInvestigator.propose() 拿到 df 后
  * 手工特征训练脚本（alpha_loop / wf_hpo / 任意 train_xxx.py）
* 一旦严重问题（全 NaN / 单值 / label 全 0）：

  * 直接抛错，拒绝训练。

2.2 **下沉部分体检到 ClickHouse 侧**

* 在 CH 写一个统计查询函数，例如：

  * 空值率、唯一值数量、basic stats（mean/std/min/max）、简单相关性（顶级 few 列）。
* DatasetSanity：

  * 先用 CH 统计结果做快速判断
  * 再对 sampled DataFrame 做重检查（尤其复杂的检测）。

2.3 **给不同任务/标签配置阈值**

* 在某个 json/yaml 里配置，例如：

  * `ret_10b_fwd`：正例比例不能 < 0.5%
  * 分类标签不能全部相同
  * 特征空值率不可 > X%。

---

### 3. 特征管线去重 & 权责划分

3.1 **画出特征来源地图**

* 每个 timeframe / 特征族：

  * 由谁产出？（FeatureCalculator / RFE / options service / 其它）
  * 写到哪里？（ClickHouse 表 / Redis / 内存）
  * 谁在用？（训练 / 实盘策略 / 风控）

3.2 **选定“权威源”**

* 比如：

  * 训练：ClickHouse + FeatureCalculator 为主
  * 实盘超高频：RFE/sfs_xx_service 为主
* 标记出完全重复、不再被用的计算路径。

3.3 **关停或降频冗余 FeatureCalculator job**

* 典型操作：

  * 如果 1s/5s 由 RFE/SFS 覆盖 → 关掉 FeatureCalculator 的 5s/15s pipeline
  * 保留 1m/5m/15m/1h for 训练 & 风控 & 回测。

3.4 **保证训练/推理使用同一套特征定义（FeatureRegistry）**

* 所有训练脚本改为从 FeatureRegistry 拉特征列表，不再手写列名。
* 实盘推理服务也只认 FeatureRegistry / StrategyManifest 中的 feature list。

---

### 4. 标签定义 & 无泄露保证

4.1 **确认所有 label 都走 LabelRegistry / LabelEngine**

* 搜索任何直接用 SQL 自己写 target 列的地方，逐步迁移到统一 LabelEngine。

4.2 **确保所有 label 计算严格“向前看未来，不用未来信息”**

* 即：用过去的 close / high / low 去算“未来收益”。
* 检查：所有 shift/lead 都是正向（未来），没有把 label 自己混回特征。

4.3 **为不同 label 配置质量规则**（结合 DatasetSanity）

* 如：

  * threshold label：上/下触发比例在合理区间内（不能 99% 不触发）。
  * triple-barrier 类标签：路径依赖逻辑要通过回测引擎生成，不允许 LLM 拼 SQL。

---

## 🧪 P1：机器学习 / HPO / 实验治理层

### 5. ExperimentRegistry 全链路接通

5.1 **确保所有 HPO / 训练脚本在 run 结束后写 ExperimentRecord**

* 包括：

  * 参数配置
  * 使用的特征集合/label ID
  * 回测 / OOS metrics（Sharpe、IC、胜率、MDD、交易次数）
  * 生成的模型文件路径 / 版本号。

5.2 **为 ExperimentRegistry 加一个“选冠军 run”的函数**

* 按 symbol + 策略标签（如 `"tsla_intraday_v7"`）：

  * 按某套规则排序：先硬门槛过滤（交易数/样本数/IC > X），再 Sharpe/Sortino 排序。
* 返回：`experiment_id` + 模型路径 + 核心 metrics。

5.3 **StrategyManifest 绑定 Experiment**

* 每个 live 策略有一个 manifest：

  * `symbol`, `strategy_id`, `experiment_id`, `model_path`, `feature_set_id`, `label_id`, `execution_prompt_id`
* 实盘推理服务只从 manifest 里加载模型。

5.4 **添加一两个 E2E 测试**

* 流程：PI propose → HPO 跑一次（可用小数据 mock）→ 写 Experiment → 生成 manifest → 模拟 live inference 一次 → 检查输出结构正确。

---

## 🧠 P1/P2：AI 大脑 & Prompt 安全层

### 6. Brain 决策约束 & 模型成绩接入

6.1 **Brain 只在“已批准的 Experiment 列表”里选模型/策略**

* ControlBrain 输入中增加：`allowed_run_ids` / `allowed_experiments`。
* Brain 的输出 schema 中必须引用这些 id，而不是随便构造一个模型名。

6.2 **接入 ClickHouse 模型表现统计**

* 实现/确认 `get_model_stats_summary_from_ch`：

  * 指标包含：近 N 天 success rate、平均 IC / Sharpe、fail_streak、max_dd 等。
* 在 ControlBrain / control_event_consumer 中：

  * 校准每个模型的 confidence（弱模型自动压权或禁止）。

6.3 **替换旧的 “脑内记忆/简单计数” 逻辑**

* 所有仍在用“status ok 次数 / fail streak”的地方，迁移到基于 ExperimentStats / ClickHouse 统计。

### 7. Prompt 安全 & 策略风格控制

7.1 **清理所有“自由 prompt 文本”**

* 把策略/Brain调用的 prompt 统一改为使用 prompt_id（从你给的 prompt library 中选）。

7.2 **定义 prompt library 的“变更流程”**

* 如：

  * 新 prompt：只能在 config 里新增，并附 meta（创建时间、来源、base_id、签名）。
  * Brain 在生产模式只允许用 white-list 里的 prompt_id。

7.3 **为 Brain 决策输出做严格 schema 校验**

* 已经有 JSON schema 的继续强化：

  * 加上字段校验：

    * confidence 范围 [0,1]
    * action 枚举（BUY/SELL/HOLD/NOOP）
    * position_size 不超过 risk limit。

---

## 🛡 P2：风控统一化（Guardian & SafetyKernel）

### 8. 风控参数统一配置源

8.1 **创建一个统一的 `risk_limits.yml` / `risk_config.json`**

* 包含：

  * Guardian 相关（策略/组合级）：最大回撤阈值、单股/组合风险限制等。
  * SafetyKernel 相关（执行级）：最小间隔、每分钟最多下单次数、单票最大名义金额等。

8.2 **改造 Guardian / SafetyKernel 读取同一份配置**

* 去掉硬编码 numeric magic numbers。

8.3 **增加日志打点**

* 每一次拒单 / 拦截交易时：

  * 明确写清：由哪一层拒绝（Guardian or SafetyKernel）、触发了哪个规则（rule_id）。

---

## 🧹 P2/P3：技术债清理 & 生产化硬化

### 9. 清理幽灵代码 & 实验代码

9.1 **LSTM 推理路径标记/下线**

* 如果当前没有完整的训练/部署链路：

  * 把相关 code 标注为 experimental 或直接移除出 prod build。

9.2 **Mock 服务的使用范围限制**

* 在生产环境启动时如果检测到 Mock → 直接报错退出。
* 在 test / dev 环境下才允许注册 MockInferenceService。

### 10. Fail Fast / 配置硬校验

10.1 **去掉“悄悄 fallback 到 legacy writer/默认配置”的逻辑**

* 发现关键配置缺失（writers / inference / risk limits）时：

  * 直接抛异常并在日志中给出清晰错误。

10.2 **添加一个 `config_sanity_check.py`**

* 在系统启动前跑：

  * 检查必要配置项是否存在
  * 检查模型文件路径/ClickHouse 表/Redis key 是否匹配。

---

## 📚 P3：文档 & 运维支持

### 11. 建一个简单但实用的 `PROD_READINESS.md` / `DEPLOYMENT_GUIDE.md`

11.1 内容包括：

* “一次新模型上生产”要执行的 checklist：

  * 是否写入 ExperimentRegistry
  * 是否生成 StrategyManifest
  * 是否有对应的 prompt_id
  * 是否已在 test 环境跑过一小段 replay / paper trading
  * 是否已检查 risk_limits 配置。

11.2 日常运行监控要点：

* 哪些表/日志是“模型表现心电图”（比如：ml_eval_with_delta, trade_log, failure_log）。
* 发生大回撤时需要看哪些可视化/统计。

---
