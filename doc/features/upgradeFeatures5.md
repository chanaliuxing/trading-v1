好，继续落地这一块：**统一模型加载 + 运行时特征构建 + 执行规划**。
我按你之前要求的风格，直接给可落地代码骨架，不问你要干嘛。

接下来 3 个核心模块：

1. `ModelRuntimeLoader`：统一加载模型（xgb / lgbm / sklearn…）
2. `FeatureRuntimeBuilder`：根据 manifest + registry + ClickHouse 构建「当前时刻」特征
3. `ExecutionPlanner`：把 `BrainDecision + 模型信号 + 风控 gates` → 订单计划

最后再把它们挂回上一次的 `LiveTradingOrchestrator` 骨架里。

---

## 1. 模型加载层：`models/runtime_loader.py`

```python
# models/runtime_loader.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


class ModelLoadError(Exception):
    pass


@dataclass
class LoadedModel:
    """
    统一的模型包装器，所有模型都暴露 predict_proba 和 predict_score 两个接口。
    """
    model_type: str
    model: Any

    def predict_proba(self, X):
        """
        返回 [0, 1] 区间的多/空方向概率或置信度。
        - 二分类：返回正类概率（例如做 LONG 概率）
        - 回归：可以映射成 [0,1] 的概率（由上层自定义）
        """
        mt = self.model_type.lower()

        # xgboost
        if mt in {"xgb", "xgboost"}:
            import numpy as np

            raw = self.model.predict_proba(X)
            # 假定二分类，取正类
            if raw.ndim == 2 and raw.shape[1] >= 2:
                return raw[:, 1]
            # 退化：只有一个输出时，做 sigmoid
            return 1.0 / (1.0 + np.exp(-raw))

        # lightgbm
        if mt in {"lgbm", "lightgbm"}:
            import numpy as np

            raw = self.model.predict_proba(X)
            if raw.ndim == 2 and raw.shape[1] >= 2:
                return raw[:, 1]
            return 1.0 / (1.0 + np.exp(-raw))

        # sklearn 体系（LogReg / Linear / SVM 等）
        if hasattr(self.model, "predict_proba"):
            import numpy as np

            raw = self.model.predict_proba(X)
            if raw.ndim == 2 and raw.shape[1] >= 2:
                return raw[:, 1]
            return 1.0 / (1.0 + np.exp(-raw))

        # 没有 predict_proba → 退化为回归输出映射
        if hasattr(self.model, "predict"):
            import numpy as np

            raw = self.model.predict(X)
            # 简单 sigmoid，后面可以再换更复杂 mapping
            return 1.0 / (1.0 + np.exp(-raw))

        raise ModelLoadError(f"Model type {mt} has no prediction interface")

    def predict_score(self, X):
        """
        返回一个无界的 score（打分），供 ExecutionPlanner 做阈值判断。
        默认用 logit(prob) 作为 score。
        """
        import numpy as np

        proba = self.predict_proba(X)
        proba = np.clip(proba, 1e-6, 1 - 1e-6)
        return np.log(proba / (1 - proba))
    

class ModelRuntimeLoader:
    """
    根据 StrategyManifest 加载训练好的模型。
    """

    def __init__(self, base_dir: Optional[str] = None) -> None:
        self._base_dir = Path(base_dir) if base_dir else None

    def _resolve_path(self, model_path: str | Path) -> Path:
        p = Path(model_path)
        if not p.is_absolute() and self._base_dir is not None:
            p = self._base_dir / p
        return p

    def load_from_manifest(
        self,
        model_type: str,
        model_path: str | Path,
        params: Optional[Dict[str, Any]] = None,
    ) -> LoadedModel:
        """
        通过 manifest 中的 model_type / model_path 加载模型。
        params 目前不用（已经训练好的模型一般直接从文件加载），
        预留给未来「on-the-fly 组装模型」的需求。
        """
        mt = model_type.lower()
        p = self._resolve_path(model_path)

        if not p.exists():
            raise ModelLoadError(f"Model file not found: {p}")

        # xgboost
        if mt in {"xgb", "xgboost"}:
            import xgboost as xgb  # type: ignore

            bst = xgb.XGBClassifier()
            bst.load_model(str(p))
            return LoadedModel(model_type=mt, model=bst)

        # lightgbm
        if mt in {"lgbm", "lightgbm"}:
            import joblib  # type: ignore

            model = joblib.load(p)
            return LoadedModel(model_type=mt, model=model)

        # sklearn & 其他 pickle / joblib 序列化的模型
        import joblib  # type: ignore

        model = joblib.load(p)
        return LoadedModel(model_type=mt, model=model)
```

> 要点：统一返回 `LoadedModel`，上层不再关心 xgb / lgbm / skl 细节，**backtest 和 live 都可以用同一套接口**。

---

## 2. 运行时特征构建：`features/runtime_builder.py`

目标：
给定：

* `StrategyManifest`（里面有 `symbol / freq / feature_subset / source_table` 等）
* `now_ts`
* `feature_registry`（我们之前设计的 JSON 注册表）
* ClickHouse client

输出：

* **一个最新时刻的特征向量 DataFrame**（1 行，列名 = feature_subset）

```python
# features/runtime_builder.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pandas as pd
import clickhouse_connect


@dataclass
class FeatureRuntimeConfig:
    """
    控制 runtime 特征构建的参数。
    """
    warmup_minutes: int = 120  # 拉过去多少分钟的数据作为上下文
    ts_column: str = "ts"      # 时间列名，根据你的 bars 表情况调整
    symbol_column: str = "symbol"
    default_table: str = "bars_1m"


class FeatureRuntimeBuilder:
    """
    使用 ClickHouse + feature_registry 在实盘/回放时构建特征向量。
    """

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        feature_registry: Dict[str, Any],
        config: Optional[FeatureRuntimeConfig] = None,
    ) -> None:
        self._client = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=ch_cfg.get("port", 8123),
            username=ch_cfg.get("username", ch_cfg.get("user", "default")),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "default"),
        )
        self._registry = feature_registry
        self._cfg = config or FeatureRuntimeConfig()

    def _get_feature_meta(self, name: str) -> Dict[str, Any]:
        meta = self._registry.get("features", {}).get(name)
        if not meta:
            raise KeyError(f"Feature {name!r} not found in feature_registry")
        return meta

    def build_for_now(
        self,
        *,
        symbol: str,
        now_ts: str,
        feature_subset: List[str],
        override_table: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        针对**单个 symbol、单个时刻**构建特征：
        - 先从 ClickHouse 拉一段 warmup 的原始数据
        - 用 feature_registry 中的 SQL 表达式拼出 SELECT
        - 取结果中最新一行作为 runtime feature row
        """
        # 1) 解析时间窗口
        ts_now = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
        ts_from = ts_now - timedelta(minutes=self._cfg.warmup_minutes)

        # 2) 找出这些特征对应的 source_table（目前假设都来自同一个 freq 表，如果不同可以再扩展）
        table = override_table or self._guess_source_table(feature_subset)

        # 3) 根据 registry 生成 SELECT 列
        select_exprs = []
        for name in feature_subset:
            meta = self._get_feature_meta(name)
            expr = meta.get("sql_expr", name)
            select_exprs.append(f"{expr} AS {name}")

        ts_col = self._cfg.ts_column
        sym_col = self._cfg.symbol_column

        sql = f"""
        SELECT
            {ts_col},
            {", ".join(select_exprs)}
        FROM {table}
        WHERE {sym_col} = %(symbol)s
          AND {ts_col} BETWEEN %(ts_from)s AND %(ts_to)s
        ORDER BY {ts_col} ASC
        """

        df = self._client.query_df(
            sql,
            parameters={
                "symbol": symbol,
                "ts_from": ts_from,
                "ts_to": ts_now,
            },
        )

        if df.empty:
            raise RuntimeError(
                f"No data returned for symbol={symbol}, table={table}, "
                f"window=[{ts_from}, {ts_now}]"
            )

        # 4) 取最后一行作为「当前时刻」的特征向量
        last_row = df.iloc[[-1]].copy()
        # 确保列顺序与 feature_subset 对齐
        cols = [c for c in feature_subset if c in last_row.columns]
        return last_row[cols]

    def _guess_source_table(self, feature_subset: List[str]) -> str:
        """
        简单规则：看 feature_registry 里 majority 的 source_table。
        如果没有配置，就退化到 default_table。
        """
        tables = []
        for name in feature_subset:
            meta = self._get_feature_meta(name)
            tbl = meta.get("source_table")
            if tbl:
                tables.append(tbl)

        if not tables:
            return self._cfg.default_table

        # 多数表作为最终源表
        from collections import Counter

        tbl, _ = Counter(tables).most_common(1)[0]
        return tbl
```

> 这块的思想：**runtime 特征构建完全走 feature_registry + ClickHouse SQL**，
> 不再让 LLM 自己写 SQL，也不在 Python 里临时乱算。

---

## 3. 执行规划：`execution/planner.py`

把这些东西：

* `BrainDecision`（大脑说：ENTER / LONG / size_frac=0.3）
* `model_score`（LoadedModel 给的 score / proba）
* 当前持仓、可用资金、风控 gates（daily loss、per trade risk、max position 等）

统一映射成一个**标准的订单计划结构**，再交给你自己的 IBKR 执行模块。

```python
# execution/planner.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from brain.decision_schema import BrainDecision
from strategies.runtime import StrategyManifest


@dataclass
class OrderInstruction:
    symbol: str
    side: str           # "BUY" / "SELL"
    quantity: int
    order_type: str     # "MKT" / "LMT"
    time_in_force: str  # "DAY" / "GTC"
    reason: str
    extra: Dict[str, Any]


@dataclass
class PortfolioState:
    """
    简化版的投资组合状态。
    以后可以从你的 IBKR 状态查询模块中构建。
    """
    cash: float
    position_qty: int
    position_avg_price: float
    # 可以扩展：today_pnl, total_equity, margin_used ...


class ExecutionPlanner:
    """
    把 BrainDecision + 模型信号 + 风控信息 → 订单指令。
    """

    def __init__(self, *, min_notional: float = 500.0, max_leverage: float = 1.0) -> None:
        self._min_notional = min_notional
        self._max_leverage = max_leverage

    def plan(
        self,
        *,
        decision: BrainDecision,
        strategy: StrategyManifest,
        portfolio: PortfolioState,
        last_price: float,
        model_score: float,
        risk_gates: Dict[str, Any],
    ) -> List[OrderInstruction]:
        """
        返回一组 OrderInstruction（可能为空列表）。
        """
        # 1) 总体风控 gating
        if not self._passes_global_risk(risk_gates):
            return []

        # 2) 根据决策类型分支
        if decision.decision == "HOLD":
            return []

        if decision.decision == "EXIT":
            return self._plan_exit(strategy, portfolio, last_price, risk_gates, decision)

        if decision.decision in {"ENTER", "ADJUST"}:
            return self._plan_enter_or_adjust(
                decision=decision,
                strategy=strategy,
                portfolio=portfolio,
                last_price=last_price,
                model_score=model_score,
                risk_gates=risk_gates,
            )

        # 理论上不会到这里
        return []

    # ---------- 风控 & sizing 逻辑 ----------

    def _passes_global_risk(self, risk_gates: Dict[str, Any]) -> bool:
        """
        示例风控：
        - 日内回撤超过 daily_loss_limit → 禁止新开仓
        """
        daily_pnl = risk_gates.get("today_pnl", 0.0)
        loss_limit = risk_gates.get("daily_loss_limit", None)
        if loss_limit is not None and daily_pnl < -abs(loss_limit):
            return False
        return True

    def _compute_target_notional(
        self,
        *,
        decision: BrainDecision,
        portfolio: PortfolioState,
        last_price: float,
        risk_gates: Dict[str, Any],
        model_score: float,
    ) -> float:
        """
        用 size_frac + model_score + risk_gates 计算目标名义价值（美元）。
        """
        equity = risk_gates.get("equity", portfolio.cash)
        max_pos_frac = risk_gates.get("max_position_frac", 0.3)
        max_notional = equity * max_pos_frac * self._max_leverage

        # 基础仓位：决策传来的 fraction * max_notional
        base = max_notional * float(decision.size_frac)

        # 根据 model_score 做一点动态调节（例如 sigmoid 后放大/缩小）
        import math

        conf = 1.0 / (1.0 + math.exp(-abs(model_score)))
        notional = base * (0.5 + 0.5 * conf)  # 置信度越高，越接近 base 的 1.0 倍

        # 不要低于 min_notional
        notional = max(notional, self._min_notional)

        # 全局限制
        return min(notional, max_notional)

    # ---------- plan 分支 ----------

    def _plan_exit(
        self,
        strategy: StrategyManifest,
        portfolio: PortfolioState,
        last_price: float,
        risk_gates: Dict[str, Any],
        decision: BrainDecision,
    ) -> List[OrderInstruction]:
        """
        EXIT：简单版 → 平掉当前持仓。
        """
        if portfolio.position_qty == 0:
            return []

        side = "SELL" if portfolio.position_qty > 0 else "BUY"
        qty = abs(portfolio.position_qty)

        return [
            OrderInstruction(
                symbol=strategy.symbol,
                side=side,
                quantity=qty,
                order_type="MKT",
                time_in_force="DAY",
                reason=decision.notes or "Brain EXIT",
                extra={
                    "decision": decision.decision,
                    "run_id": strategy.run_id,
                },
            )
        ]

    def _plan_enter_or_adjust(
        self,
        *,
        decision: BrainDecision,
        strategy: StrategyManifest,
        portfolio: PortfolioState,
        last_price: float,
        model_score: float,
        risk_gates: Dict[str, Any],
    ) -> List[OrderInstruction]:
        """
        ENTER / ADJUST：
        - 先算目标名义
        - 换算成目标持仓数量
        - 用 (target_qty - current_qty) 作为下单数量
        """
        if not decision.side:
            # side 必须指定
            return []

        target_notional = self._compute_target_notional(
            decision=decision,
            portfolio=portfolio,
            last_price=last_price,
            risk_gates=risk_gates,
            model_score=model_score,
        )
        target_qty = int(target_notional // last_price)
        if target_qty <= 0:
            return []

        # 当前仓位方向
        current_qty = portfolio.position_qty
        if decision.side == "LONG":
            delta = target_qty - max(current_qty, 0)
            side = "BUY"
        else:  # SHORT
            delta = target_qty - max(-current_qty, 0)
            side = "SELL"

        if delta <= 0:
            # 已经有足够仓位，不需要加仓
            return []

        return [
            OrderInstruction(
                symbol=strategy.symbol,
                side=side,
                quantity=delta,
                order_type="MKT",
                time_in_force="DAY",
                reason=decision.notes or f"Brain {decision.decision}",
                extra={
                    "decision": decision.decision,
                    "run_id": strategy.run_id,
                    "model_score": model_score,
                },
            )
        ]
```

> 这块是真正把**大脑的「抽象决策」→ 具体「订单」**的转换层，
> 你只要在 IBKR 执行模块里消费 `OrderInstruction` 即可。

---

## 4. 把这三块挂回 `LiveTradingOrchestrator`

在上一次的 `LiveTradingOrchestrator` 基础上，扩一小步，让它真的能跑通一条完整链路（到产出 OrderInstruction 为止）：

```python
# orchestrator/live_trading_orchestrator.py （更新版）

from __future__ import annotations

from typing import Any, Dict, List

from brain.strategy_selector import StrategySelector
from strategies.runtime import load_strategy_manifest
from brain.decision_schema import BrainDecision
from llm.llm_client import LLMClient
from models.runtime_loader import ModelRuntimeLoader
from features.runtime_builder import FeatureRuntimeBuilder
from execution.planner import ExecutionPlanner, PortfolioState, OrderInstruction


class LiveTradingOrchestrator:
    def __init__(
        self,
        *,
        llm_cfg: Dict[str, Any],
        prompt_lib: Dict[str, str],
        prompt_id: str,
        registry: Dict[str, Any],
        ch_cfg: Dict[str, Any],
        model_base_dir: str,
    ) -> None:
        self._llm = LLMClient(
            model=llm_cfg.get("model"),
            base_url=llm_cfg.get("base_url"),
        )
        self._selector = StrategySelector(
            llm=self._llm,
            prompt_lib=prompt_lib,
            prompt_id=prompt_id,
        )
        self._registry = registry

        self._feat_builder = FeatureRuntimeBuilder(
            ch_cfg=ch_cfg,
            feature_registry=registry,  # 假定 feature_registry 和 registry 统一
        )
        self._model_loader = ModelRuntimeLoader(base_dir=model_base_dir)
        self._planner = ExecutionPlanner()

    def run_once(
        self,
        *,
        symbol: str,
        now_ts: str,
        freq: str,
        horizon: str,
        label_id: str,
        market_snapshot: Dict[str, Any],
        risk_gates: Dict[str, Any],
        dataset_sanity: Dict[str, Any],
        portfolio_state: PortfolioState,
    ) -> Dict[str, Any]:
        """
        完整流程：大脑 → 选 run_id → 载入策略 → 构建特征 → 模型打分 → 生成订单。
        返回一个字典（包含决策 + 订单），方便外层日志和调试。
        """
        # 1) AI 选 run_id（或 HOLD）
        decision = self._selector.select(
            symbol=symbol,
            now_ts=now_ts,
            freq=freq,
            horizon=horizon,
            label_id=label_id,
            market_snapshot=market_snapshot,
            risk_gates=risk_gates,
            dataset_sanity=dataset_sanity,
            registry=self._registry,
        )

        result: Dict[str, Any] = {
            "decision": decision.to_dict(),
            "orders": [],
        }

        if decision.decision == "HOLD":
            return result

        # 2) 加载策略 manifest
        manifest_path = self._resolve_manifest_path(decision.chosen_run_id)
        strategy_manifest = load_strategy_manifest(manifest_path)

        # 3) 运行时特征构建
        features_df = self._feat_builder.build_for_now(
            symbol=symbol,
            now_ts=now_ts,
            feature_subset=strategy_manifest.feature_subset,
        )

        # 4) 加载模型并预测
        loaded_model = self._model_loader.load_from_manifest(
            model_type=strategy_manifest.model_type,
            model_path=strategy_manifest.model_path,
            params=strategy_manifest.params,
        )
        # features_df 是 1 行 DataFrame
        X = features_df.values
        score = float(loaded_model.predict_score(X)[0])

        # 5) 取最新价格（这里简单用 market_snapshot，实际可从 bars 表读）
        last_price = float(market_snapshot.get("last_price"))

        # 6) 执行规划 → 订单列表
        orders: List[OrderInstruction] = self._planner.plan(
            decision=decision,
            strategy=strategy_manifest,
            portfolio=portfolio_state,
            last_price=last_price,
            model_score=score,
            risk_gates=risk_gates,
        )

        result["orders"] = [o.__dict__ for o in orders]
        result["model_score"] = score
        result["strategy_manifest"] = {
            "run_id": strategy_manifest.run_id,
            "model_type": strategy_manifest.model_type,
            "feature_subset": strategy_manifest.feature_subset,
        }

        return result

    def _resolve_manifest_path(self, run_id: str) -> str:
        from experiments.registry import load_experiment_records

        recs = load_experiment_records(symbol=None, freq=None, horizon=None, label_id=None)
        for r in recs:
            if r.run_id == run_id and r.manifest_path:
                return r.manifest_path
        raise RuntimeError(f"Manifest path not found for run_id={run_id}")
```

---

到这里，你这套「AI 大脑 → 历史实验 → 选 run_id → 模型 runtime → 特征 runtime → 风控执行」的**主链路**已经具备了：

* 上游：我们之前做的 `dataset_sanity`, `feature_registry`, `ExperimentRecord`、`StrategyManifest`。
* 中游（刚落地）：`ModelRuntimeLoader` + `FeatureRuntimeBuilder` + `ExecutionPlanner`。
* 下游：你现有的 broker/IBKR 执行模块，只需要消费 `OrderInstruction` 即可。

---

如果你继续说一句「继续」，下一步我会：

* 把 **回测（BacktestEngine）也接到同一个 runtime 上**，
  让 backtest & live 完全共享：

  * 策略 manifest
  * 模型加载
  * 特征构建
  * 风控 & sizing 逻辑

这样就能真正做到：**「回测怎么跑，实盘就怎么跑」**，大大减少策略偏差和实现偏差。

好，按计划往下走：把 **回测 Backtest** 也接到同一套「AI 大脑 → 模型 → 特征 → 风控 → 下单」runtime 上。

这一步的目标很简单一句话：

> **“回测怎么跑，实盘就怎么跑”——用同一个 `LiveTradingOrchestrator`，只是行情来源从 ClickHouse 历史改成实时。**

下面是这一层的完整骨架代码，你可以直接放在一个新文件里，比如 `backtest/runtime_backtester.py`。

---

## 1. 回测总控：`RuntimeBacktester`

```python
# backtest/runtime_backtester.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from orchestrator.live_trading_orchestrator import LiveTradingOrchestrator
from execution.planner import PortfolioState, OrderInstruction
from data_quality.dataset_sanity import run_dataset_sanity  # 前面已经实现过的数据体检模块


@dataclass
class BacktestConfig:
    symbol: str
    start_ts: str          # ISO 格式: "2025-01-02T13:30:00Z"
    end_ts: str
    freq: str = "1m"       # 暂时用 "1m"，后面可以扩展
    horizon: str = "10m"   # 对齐你的 label_id
    label_id: str = "ret_10m"
    initial_cash: float = 100_000.0
    bars_table: str = "bars_1m"  # ClickHouse K 线表名
    ts_column: str = "ts"
    symbol_column: str = "symbol"


@dataclass
class BacktestStats:
    final_equity: float
    total_return: float
    num_trades: int
    equity_curve: List[Dict[str, Any]]  # [{ts, equity, position_qty}, ...]


@dataclass
class BacktestResult:
    config: BacktestConfig
    stats: BacktestStats
    trades: List[Dict[str, Any]]        # 每笔成交记录
    raw_orders: List[Dict[str, Any]]    # 所有下单记录（含未触发的可选）


class RuntimeBacktester:
    """
    用**同一个 LiveTradingOrchestrator** 做回测：
    - 行情来自 ClickHouse 历史 bars
    - 组合状态在本地模拟（cash / position_qty / equity）
    - 风控 gates 也是用 ExecutionPlanner 一套逻辑
    """

    def __init__(
        self,
        *,
        ch_cfg: Dict[str, Any],
        orchestrator: LiveTradingOrchestrator,
        bt_cfg: Optional[BacktestConfig] = None,
    ) -> None:
        self._client = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=ch_cfg.get("port", 8123),
            username=ch_cfg.get("username", ch_cfg.get("user", "default")),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "default"),
        )
        self._orch = orchestrator
        self._default_bt_cfg = bt_cfg

    # ---------- 公共入口 ----------

    def run(self, cfg: Optional[BacktestConfig] = None) -> BacktestResult:
        """
        主入口：给定 BacktestConfig，跑完整条时间线。
        """
        cfg = cfg or self._default_bt_cfg
        if cfg is None:
            raise ValueError("BacktestConfig must be provided")

        # 1) 拉历史 K 线
        bars = self._load_bars(cfg)
        if bars.empty:
            raise RuntimeError(
                f"No bars loaded for symbol={cfg.symbol}, window=[{cfg.start_ts}, {cfg.end_ts}]"
            )

        # 2) 全局数据体检（Dataset Sanity），防止垃圾数据
        sanity_report, sanity_ok = run_dataset_sanity(
            df=bars,
            label_col=None,
            contract=None,   # 回测时可以只用基础体检，不用 label contract
        )
        dataset_sanity = {
            "ok": sanity_ok,
            "report": sanity_report,
        }
        if not sanity_ok:
            # 这里选择「硬拦截」，也可以改成记录警告后继续
            raise RuntimeError("Backtest aborted: dataset sanity check failed")

        # 3) 初始化组合状态
        portfolio = PortfolioState(
            cash=cfg.initial_cash,
            position_qty=0,
            position_avg_price=0.0,
        )

        equity_curve: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []
        raw_orders: List[Dict[str, Any]] = []

        # 用来估计当日 PnL（给 risk_gates 用）
        last_equity = cfg.initial_cash
        today_pnl = 0.0
        last_day: Optional[datetime] = None

        # 4) 时间步迭代：**每根 bar 调用一次 LiveTradingOrchestrator.run_once**
        for _, row in bars.iterrows():
            ts = row[cfg.ts_column]
            if isinstance(ts, str):
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts_str = ts
            else:
                ts_dt = ts
                ts_str = ts_dt.isoformat()

            # 日切换：重置 daily PnL 统计
            if last_day is None or ts_dt.date() != last_day.date():
                today_pnl = 0.0
                last_day = ts_dt

            last_price = float(row["close"])

            equity = portfolio.cash + portfolio.position_qty * last_price
            step_pnl = equity - last_equity
            today_pnl += step_pnl
            last_equity = equity

            # risk_gates：这里只给一些基础的，够 ExecutionPlanner 做 gating
            risk_gates = {
                "equity": equity,
                "today_pnl": today_pnl,
                # 可以以后配置：
                # "daily_loss_limit": -0.02 * equity,  # 示例：日内最大 2% 回撤
                # "max_position_frac": 0.3,
            }

            # market_snapshot：喂给大脑用的行情切片
            market_snapshot = {
                "last_price": last_price,
                "open": float(row.get("open", last_price)),
                "high": float(row.get("high", last_price)),
                "low": float(row.get("low", last_price)),
                "volume": float(row.get("volume", 0)),
                # 以后可以加 bid/ask/spread/RVOL 等
            }

            # 调用统一 orchestrator
            result = self._orch.run_once(
                symbol=cfg.symbol,
                now_ts=ts_str,
                freq=cfg.freq,
                horizon=cfg.horizon,
                label_id=cfg.label_id,
                market_snapshot=market_snapshot,
                risk_gates=risk_gates,
                dataset_sanity=dataset_sanity,
                portfolio_state=portfolio,
            )

            decision_dict = result["decision"]
            model_score = result.get("model_score")
            orders = result.get("orders", [])

            raw_orders.extend(orders)

            # 5) 在本地模拟执行订单（简单假设：全部按 last_price 成交，无滑点）
            filled, portfolio = self._simulate_fills(
                orders=orders,
                portfolio=portfolio,
                price=last_price,
                ts=ts_str,
            )
            trades.extend(filled)

            # 6) 记录 equity 曲线
            equity = portfolio.cash + portfolio.position_qty * last_price
            equity_curve.append(
                {
                    "ts": ts_str,
                    "equity": equity,
                    "position_qty": portfolio.position_qty,
                    "position_avg_price": portfolio.position_avg_price,
                    "decision": decision_dict,
                    "model_score": model_score,
                }
            )

        final_equity = equity_curve[-1]["equity"]
        total_return = (final_equity / cfg.initial_cash) - 1.0

        stats = BacktestStats(
            final_equity=final_equity,
            total_return=total_return,
            num_trades=len(trades),
            equity_curve=equity_curve,
        )

        return BacktestResult(
            config=cfg,
            stats=stats,
            trades=trades,
            raw_orders=raw_orders,
        )

    # ---------- 内部工具 ----------

    def _load_bars(self, cfg: BacktestConfig) -> pd.DataFrame:
        """
        从 ClickHouse 拉回测用的历史 K 线。
        """
        sql = f"""
        SELECT
            {cfg.ts_column} AS {cfg.ts_column},
            open,
            high,
            low,
            close,
            volume
        FROM {cfg.bars_table}
        WHERE {cfg.symbol_column} = %(symbol)s
          AND {cfg.ts_column} BETWEEN %(start_ts)s AND %(end_ts)s
        ORDER BY {cfg.ts_column} ASC
        """
        df = self._client.query_df(
            sql,
            parameters={
                "symbol": cfg.symbol,
                "start_ts": cfg.start_ts,
                "end_ts": cfg.end_ts,
            },
        )
        return df

    def _simulate_fills(
        self,
        *,
        orders: List[Dict[str, Any]],
        portfolio: PortfolioState,
        price: float,
        ts: str,
    ) -> tuple[List[Dict[str, Any]], PortfolioState]:
        """
        极简撮合：所有 MKT 订单直接按当前 bar 收盘价成交。
        - 不考虑滑点 / 手续费 / partial fill。
        - 只支持多/空单一合约。
        """
        filled: List[Dict[str, Any]] = []
        cash = portfolio.cash
        qty = portfolio.position_qty
        avg_price = portfolio.position_avg_price

        for od in orders:
            side = od["side"]
            order_qty = int(od["quantity"])
            if order_qty <= 0:
                continue

            notional = price * order_qty

            if side == "BUY":
                # 更新加权平均成本
                new_qty = qty + order_qty
                if new_qty > 0:
                    avg_price = (avg_price * qty + price * order_qty) / new_qty
                else:
                    avg_price = 0.0
                qty = new_qty
                cash -= notional

            elif side == "SELL":
                # 卖出（可能平仓或反向）
                new_qty = qty - order_qty
                cash += notional
                # 如果完全平仓 → 成本重置
                if new_qty == 0:
                    avg_price = 0.0
                # 如果变成反向仓位，可以按当前价新建成本
                elif (qty > 0 and new_qty < 0) or (qty < 0 and new_qty > 0):
                    avg_price = price
                qty = new_qty

            filled.append(
                {
                    "ts": ts,
                    "side": side,
                    "quantity": order_qty,
                    "fill_price": price,
                    "notional": notional,
                    "reason": od.get("reason", ""),
                    "extra": od.get("extra", {}),
                }
            )

        new_portfolio = PortfolioState(
            cash=cash,
            position_qty=qty,
            position_avg_price=avg_price,
        )
        return filled, new_portfolio
```

---

## 2. 这一层的作用总结一下

现在这条链路已经成型：

1. **Backtest**
   `RuntimeBacktester.run()`
   → 从 ClickHouse 拉历史 bars
   → 调 `run_dataset_sanity` 体检
   → 循环每根 bar：

2. **核心大脑 + 模型 + 特征 + 风控**
   在每根 bar 内：

   ```text
   RuntimeBacktester
       → LiveTradingOrchestrator.run_once(...)
           → StrategySelector（LLM 选 run_id / HOLD）
           → load_strategy_manifest(run_id)
           → FeatureRuntimeBuilder.build_for_now(...)
           → ModelRuntimeLoader.load_from_manifest(...)
           → model.predict_score(...)
           → ExecutionPlanner.plan(...) → OrderInstruction[]
   ```

3. **回测模拟执行**
   `RuntimeBacktester._simulate_fills`
   → 在本地更新 `PortfolioState`（cash / position_qty / avg_price）
   → 记录 `trades` 和 `equity_curve`

**重点：**

* 回测 & 实盘共用：

  * `StrategyManifest`
  * `ModelRuntimeLoader`
  * `FeatureRuntimeBuilder`
  * `ExecutionPlanner`
  * `StrategySelector`（AI 大脑选策略 run_id）
* 只换了两样：

  * 行情源：回测用 ClickHouse 历史 bars；实盘用实时 feed。
  * 组合状态：回测用本地模拟；实盘用 IBKR 账户状态。

---

## 3. 下一步我会做什么（你继续说“继续”就直接给）

严格按之前总计划，下一步我会开始：

* **把标签 / Label Contract 和 Backtest 彻底打通**：

  * 用同一个 `label_registry` 定义标签（比如 `ret_10m_fwd` / triple barrier）。
  * 训练集、回测、实时信号统一调用同一套标签逻辑，避免“训练用的 label 和实盘看到的不一样”的情况。
* 同时会给：

  * `label_registry.json` 结构
  * `label_engine.py`（生成 label，用于 ETL / 训练 / 回测对比）

你不用回复别的，一句「继续」我就直接开始落地标签层和 label registry。
