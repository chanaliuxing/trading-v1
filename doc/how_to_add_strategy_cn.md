# 如何在不改核心代码的前提下添加新策略

目标：基于现有 StrategyEngine（services/strategy/engine.py）的动态加载能力，只通过新增策略文件与修改 `config.yml` 配置来完成新策略的接入。

---

## 总览 (标准流程)
1. 在 `strategies/` 目录下创建策略代码文件，其中的类继承 `libs.strategy.base.StrategyAlgorithm`。
2. 在 `config.yml` 的 `strategies` 列表中添加一个新条目，指明策略的 `module` 和 `class`，并设置 `enabled: true`。
3. 在策略的 `initialize()` 方法中调用 `await self.ctx.subscribe_features([...], bar_sec=60)` 来订阅所需的数据源。
4. 在 `on_features()` 方法中实现策略逻辑，并使用 `await self.ctx.submit_intent(...)` 提交交易意图。
5. (可选) 如果需要，可以使用 `@register_strategy("unique-name")` 装饰器为策略注册一个全局别名，但这并非当前推荐的必需步骤。

重要说明：
- 当前默认路径是“纯配置驱动（无装饰器）”。仓库中有多种策略使用该方式，请以本文流程为准，不以任一具体策略作为标准模板。
- 请使用 `await self.ctx.subscribe_features(...)` 完成订阅，不要直接调用 `self.ctx.engine.subscribe_features(...)`（内部接口）。

---

## 最小可用示例
文件：`strategies/my_revert_v1.py`

```python
from typing import Dict, Any
from libs.strategy import (
    StrategyAlgorithm,
    DecisionEnvelope,
    OrderStyle,
    Freshness,
    make_decision_id,
)

# 注意：不再需要 register_strategy 装饰器
class MyRevertV1(StrategyAlgorithm):
    async def initialize(self) -> None:
        # 参数通过 config.yml 中的 params.extras 传入
        self.symbols = self.params.extras.get("symbols", ["AAPL"])  # 必要：你要订阅谁
        # 订阅 1m 特征（触发 on_features 回调）
        await self.ctx.subscribe_features(self.symbols, bar_sec=60)

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        # 一个极简均值回归示例：zscore_20 < -2 买入 100 股
        z = float(features.get("zscore_20", 0) or 0)
        px = float(features.get("close", 0) or 0)
        if z < -2.0 and px > 0:
            env = DecisionEnvelope(
                decision_id=make_decision_id(),
                strategy_id=self.ctx.strategy_id,
                symbol=symbol,
                ts_bar_end_us=self.ctx.now_us(),
                target_qty=100.0,
                order_style=OrderStyle(kind="MKT"),
                freshness=Freshness(ttl_ms=self.params.ttl_ms, dlb_ms=500),
                extras={"ref_px": px, "reason": "zscore_lt_-2"},
            )
            await self.ctx.submit_intent(env)
```

要点：
- 使用 `libs.strategy` 下的基类与数据结构。
- 必须在 `initialize()` 中调用 `await self.ctx.subscribe_features(...)`，否则引擎不会向策略推送数据。不要直接操作 `ctx.engine.*`。

---

## 配置接入（config.yml）

**标准方式：**
直接在 `strategies` 列表中声明策略的模块路径和类名。这是最直接且推荐的方式。

```yaml
strategies:
  - id: "my_revert_aapl_001"
    enabled: true
    module: "strategies.my_revert_v1"  # 指向你的 .py 文件
    class: "MyRevertV1"               # 指向你的类名
    params:
      extras:
        symbols: ["AAPL"]
        # 这里放你策略自定义参数（任意嵌套）
```

**可选方式（使用装饰器和注册表）：**
如果策略代码中使用了 `@register_strategy("my_revert_v1")` 装饰器，你可以选择在 `strategy_registry` 中创建一个别名映射。这种方式更明确，但增加了额外的步骤。注意：当前加载器仍要求提供 `module` 与 `class` 字段，且并不会读取 `name` 字段；如需自定义注册名，请使用 `registry_name`。

```yaml
strategy_registry:
  my_revert_v1: strategies.my_revert_v1.MyRevertV1

strategies:
  - id: "my_revert_aapl_001"
    enabled: true
    module: "strategies.my_revert_v1"
    class: "MyRevertV1"
    # 可选：自定义注册名（否则使用 id）
    registry_name: "my_revert_v1"
    params:
      extras:
        symbols: ["AAPL"]
```

说明：
- 引擎在启动时读取 `config.yml`。当前推荐的方式是直接通过 `module` 和 `class` 字段动态加载策略，无需使用装饰器。
- 若配置未提供注册名，加载器会在运行时回退：按 `module`/`class` 导入并以 `registry_name` 或 `id` 临时注册，参见 `run_system.py:760-920` 的装载分支。

---

## 运行与验证
1) 启动系统（默认 shadow 模式记录意图，不真实下单）

```bash
python run_system.py
```

2) 观察日志：
- 看到 `StrategyEngine initialized` 与 `Strategy loaded`，说明策略已装载。
- 当有新 bar 推送时（由 `subscribe_features` 触发），`on_features` 会被调用。
- 产生意图后，可在日志中看到 `Intent submitted` 或在 Postgres 的 `shadow_orders` 表看到记录（shadow 模式）。

3) 常见检查：
- 未触发回调：是否在 `initialize()` 中订阅了正确的 `symbols` 与 `bar_sec`？
- 无价格字段：可从 `features` 取 `close`/`vwap`，或通过 `self.ctx.get_price(symbol)` 获取 L1 快照（需要 PriceCache 已启动）。
- 频率控制：目前引擎以“新 bar 到达”驱动回调；`set_cadence()` 仅用于记录/未来扩展，不会节流回调频率。

---

## 策略上下文可用 API（节选）
- 数据订阅：`await ctx.subscribe_features(symbols=[...], bar_sec=60)`
- 历史特征：`await ctx.history(symbol="AAPL", resolution="1m", limit=200)`
- 账户/持仓：`await ctx.portfolio()`、`await ctx.position("AAPL")`
- 快速行情：`ctx.get_price(symbol)`、`ctx.get_minute_stats(symbol)`（依赖 PriceCache）
- 提交意图：`await ctx.submit_intent(envelope)`
- 时间工具：`ctx.now_ms()`、`ctx.now_us()`

实现参考：
- 引擎与上下文：`services/strategy/engine.py`
- 市场数据门面：`libs/dal/market_data_facade.py`
- 决策管线（影子/审批）：`services/strategy/engine.py:submit_intent`

---

## 与早期设计的差异（Gap 分析）
历史文档：
- 最早系统设计快照：`doc/system/1.md`
- 引擎 v1（IoC 草案）：`doc/system/23.md`

关键差异要点：
- 事件来源与分发
  - 早期：引擎订阅总线主题（如 `md.bar.5s.v2`），统一调度 `on_bar_5s`/`on_timer`。
  - 现状：策略需在 `initialize()` 主动调用 `ctx.subscribe_features(...)`，由引擎通过 `MarketDataFacade` 轮询 ClickHouse 生成事件，并在回调中注入 L1 报价（PriceCache）。

- 回调接口
  - 早期：`on_bar_5s`/`on_timer` 等，带“决策回合/幂等”窗口（同一回合只发一次）。
  - 现状：统一使用 `on_features(symbol, features)`；未内建“单回合抑制”，幂等由决策管线/DB 侧保证（`decision_id` 仍保留）。

- 配置与装载
  - 早期：通过 `StrategySpec(module, symbols, cadence_s, ...)` 直接实例化。
  - 现状：通过 `config.yml` 中的 `module` 和 `class` 路径动态装载。`@register_strategy` 装饰器和 `strategy_registry` 提供了可选的、更明确的映射机制，但不是必需的。

- 数据接入
  - 早期：更偏向 Kafka→内存总线直推。
  - 现状：ClickHouse 作为主特征源，轮询增量；并引入 PriceCache 提供低时延 L1 辅助字段（如 `bid/ask/last`、`spread_pct`）。

- 频率控制（cadence）
  - 早期：引擎内 per-strategy worker 与节拍器。
  - 现状：以“新 bar 出现”为触发；`set_cadence()` 预留但不节流 on_features 频率（需在策略内自行判定/去抖）。

迁移建议：按现状实现新策略，尽量使用 `ctx.subscribe_features` + `ctx.get_price` 完成数据闭环；若需要“单回合仅一次”语义，可在策略内部用时间窗/标志位自行抑制重复发单。

---

## 提交前清单
- [ ] 策略文件位于 `strategies/` 并继承 `libs.strategy.base.StrategyAlgorithm`
- [ ] 至少一次 `ctx.subscribe_features()` 在 `initialize()` 中完成
- [ ] 通过 `config.yml` 启用并带上必要的 `params.extras`
- [ ] 本地以 shadow 模式跑通，看到 `shadow_orders` 记录
- [ ] 日志无高频报错；新增依赖/端口已在 `doc/` 说明
