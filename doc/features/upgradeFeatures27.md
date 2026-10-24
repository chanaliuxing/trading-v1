好，按我们刚才定的顺序，这一轮我帮你把：

> **ExecutionDecision → Order DSL → BrokerAdapter**

这一整条链打通，做到：
**AI 大脑只说一句话（ExecutionDecision），系统就能生成“可以直接丢给 IBKR 层”的标准订单请求。**

我会分 4 步：

1. 定义通用的 **订单 DSL（OrderRequest / OrderBatch）**
2. 写一个 **Decision → Order 的转换器（OrderBuilder）**
3. 定义 **BrokerAdapter 抽象层** + 一个假数据的 `PaperBroker`
4. 给你一个 **端到端示例：Orchestrator + ExecutionAgent + OrderBuilder + BrokerAdapter**

> ⚠️ 数值型参数（如最大仓位、最小订单金额、风险阈值）我只做“占位 + 约束接口”，明确标注“必须用历史数据&回测来校准”，不会当成拍脑袋的生产参数。

---

## 1. 订单 DSL：一个统一的 “OrderRequest 语言”

### `orders/types.py`

```python
# orders/types.py

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Literal, Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(str, Enum):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"
    GTD = "GTD"


@dataclass
class OrderRequest:
    """
    这是所有 BrokerAdapter 都要吃的“中间语言”。
    不管底下是 IBKR / paper engine / mock engine，
    都统一用这个结构来描述一个订单。
    """

    symbol: str
    side: OrderSide
    quantity: int

    order_type: OrderType
    time_in_force: TimeInForce

    # limit 单特有
    limit_price: Optional[float] = None

    # 方便审计 & 回放
    client_order_id: Optional[str] = None
    tags: Dict[str, Any] = field(default_factory=dict)

    # 预留字段：给特定 broker 用，比如 IBKR 的 algo 参数
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderBatch:
    """
    一次“决策”可能对应一组订单（例如：
    - 先平掉部分旧仓
    - 再开新仓
    - 再挂一个保护性止损
    这里先做极简版：主订单列表 + 元数据。
    """

    orders: list[OrderRequest]
    session_id: str
    label_id: str
    symbol: str

    # 决策原文（ExecutionDecision.to_dict()），方便后面回放
    decision_context: Dict[str, Any] = field(default_factory=dict)
```

---

## 2. ExecutionDecision → OrderBatch：OrderBuilder

我们写一个专门的转换器：

* 输入：

  * `ExecutionDecision`（AI 大脑的决策）
  * 当前 `position` / `account_equity` / `price` / `lot_size` 等环境信息
* 输出：

  * `OrderBatch`（一组标准化的 `OrderRequest`）

### `orders/builder.py`

```python
# orders/builder.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from execution.types import ExecutionDecision
from orders.types import OrderBatch, OrderRequest, OrderSide, OrderType, TimeInForce


@dataclass
class PositionSnapshot:
    """
    盘中该 symbol 的当前持仓视图。
    注意：这些字段的来源应该是你的 portfolio service / broker API，
    这里只是消费端定义，不直接耦合 IBKR。
    """
    symbol: str
    quantity: int          # 正数=多头，负数=空头
    avg_price: float
    unrealized_pnl: float


@dataclass
class OrderBuilderConfig:
    """
    这些数值必须后续通过历史数据 + backtest + 风险偏好 来校准。
    这里只是提供一个“骨架”字段定义。
    """
    # 每单最小名义金额（避免发一堆几百块的小单）
    min_notional_per_order: float

    # 当前 symbol 最大允许名义敞口占总权益的比例（例如 0.3 = 30%）
    max_gross_exposure_frac_per_symbol: float

    # 每次调整的最小数量单位（例如 1 股，或者 5 股）
    min_lot_size: int


class OrderBuilder:
    """
    把 ExecutionDecision 转成 OrderBatch 的纯逻辑层。
    不负责真正“下单”（那是 BrokerAdapter 的事情）。
    """

    def __init__(self, cfg: OrderBuilderConfig) -> None:
        self._cfg = cfg

    # ------------------------------------------------------------------ #
    # 对外主入口
    # ------------------------------------------------------------------ #

    def build_orders(
        self,
        *,
        decision: ExecutionDecision,
        position: PositionSnapshot,
        last_price: float,
        account_equity: float,
    ) -> OrderBatch:
        """
        1. 根据 decision.action / side / size_frac，算出目标仓位
        2. 从当前持仓 → 目标持仓，推导“需要买/卖多少”
        3. 算出订单数量，snap 到 min_lot_size
        4. 小于 min_notional_per_order 的直接不下（等下一次信号）
        """

        symbol = decision.symbol or position.symbol
        # 1) 计算当前该 symbol 的名义敞口（绝对值）
        current_notional = abs(position.quantity) * last_price

        # 2) 计算该 symbol 的最大允许名义敞口
        max_notional_for_symbol = (
            self._cfg.max_gross_exposure_frac_per_symbol * account_equity
        )

        # 3) 基于 decision.size_frac 算目标名义敞口
        target_notional = decision.size_frac * max_notional_for_symbol

        # 对不同 action 做解释：
        # - HOLD: 目标 = 当前，不动
        # - FLATTEN: 目标 = 0
        # - REDUCE: 目标 = 当前的一部分（这里简单按 size_frac * 当前）
        #   （若要更精细，可以扩展 ExecutionDecision 决定用“绝对目标”还是“比例减仓”）
        # - OPEN / CLOSE: 为简化，这里按目标名义来做
        if decision.action == "HOLD":
            target_notional = current_notional
        elif decision.action == "FLATTEN":
            target_notional = 0.0
        elif decision.action == "REDUCE":
            target_notional = current_notional * decision.size_frac
        else:
            # OPEN 或 CLOSE 就用上面通过 size_frac 算出来的 target_notional
            pass

        # 4) 由目标名义 → 目标数量（带方向）
        if decision.side == "FLAT" or target_notional == 0.0:
            target_qty_signed = 0
        else:
            signed_sign = 1 if decision.side == "LONG" else -1
            target_qty_float = target_notional / max(last_price, 1e-8)
            target_qty_signed = int(round(target_qty_float)) * signed_sign

        # 5) 计算需要调整的 delta_qty
        delta_qty = target_qty_signed - position.quantity

        # 如果 delta_qty = 0，就不下单
        if delta_qty == 0:
            return OrderBatch(
                orders=[],
                session_id=decision.session_id,
                label_id=decision.label_id,
                symbol=symbol,
                decision_context=decision.__dict__.copy(),
            )

        # 6) snap 到最小交易单位
        if abs(delta_qty) < self._cfg.min_lot_size:
            # 不足一个 lot，就暂时不动，避免碎片订单
            return OrderBatch(
                orders=[],
                session_id=decision.session_id,
                label_id=decision.label_id,
                symbol=symbol,
                decision_context={
                    **decision.__dict__,
                    "note": f"delta_qty {delta_qty} < min_lot_size {self._cfg.min_lot_size}, skip order.",
                },
            )

        # 对 delta_qty 做 lot 对齐
        if delta_qty > 0:
            delta_qty = (delta_qty // self._cfg.min_lot_size) * self._cfg.min_lot_size
        else:
            delta_qty = -(
                (abs(delta_qty) // self._cfg.min_lot_size) * self._cfg.min_lot_size
            )

        # 7) 名义金额过滤：太小不下
        order_notional = abs(delta_qty) * last_price
        if order_notional < self._cfg.min_notional_per_order:
            return OrderBatch(
                orders=[],
                session_id=decision.session_id,
                label_id=decision.label_id,
                symbol=symbol,
                decision_context={
                    **decision.__dict__,
                    "note": (
                        f"order_notional {order_notional:.2f} < "
                        f"min_notional_per_order {self._cfg.min_notional_per_order:.2f}, skip order."
                    ),
                },
            )

        # 8) 构造 OrderRequest
        side = OrderSide.BUY if delta_qty > 0 else OrderSide.SELL

        if decision.order_type == "LIMIT" and decision.limit_offset_bps is not None:
            # limit 价格: 在 last_price 上加/减一定 bp
            offset = decision.limit_offset_bps / 10_000.0
            if side == OrderSide.BUY:
                limit_price = last_price * (1 - offset)
            else:
                limit_price = last_price * (1 + offset)
            order_type = OrderType.LIMIT
        else:
            limit_price = None
            order_type = OrderType.MARKET

        tif = TimeInForce(decision.time_in_force.upper())

        order = OrderRequest(
            symbol=symbol,
            side=side,
            quantity=abs(delta_qty),
            order_type=order_type,
            time_in_force=tif,
            limit_price=limit_price,
            client_order_id=f"{decision.session_id}-{decision.label_id}",
            tags={
                "session_id": decision.session_id,
                "label_id": decision.label_id,
                "decision_confidence": decision.confidence,
                "decision_action": decision.action,
                "decision_side": decision.side,
            },
        )

        return OrderBatch(
            orders=[order],
            session_id=decision.session_id,
            label_id=decision.label_id,
            symbol=symbol,
            decision_context=decision.__dict__.copy(),
        )
```

> ✅ 现在：AI 大脑只需要决定 `side/size_frac/order_type/limit_offset_bps`，
> 你这里就能自动算出**实际下多少股**，并且保证：
>
> * 不超过单 symbol 的最大风险敞口
> * 小于最小交易单位 & 最小名义金额的订单自动忽略

---

## 3. BrokerAdapter：抽象层 + 一个简单 PaperBroker

### 3.1 抽象基类

```python
# broker/base.py

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List

from orders.types import OrderBatch


@dataclass
class OrderExecutionResult:
    """
    执行一个 OrderBatch 后的简单结果描述。
    真正的字段你可以根据 IBKR 的返回结构再扩展。
    """
    success: bool
    order_ids: List[str]
    message: str
    raw: Dict[str, Any]


class BrokerAdapter(ABC):
    """
    Adapter 模式的核心接口：
    - 上游永远只用 OrderBatch / OrderExecutionResult 交互
    - 下游可以是 IBKR / paper engine / mock engine / 回测引擎
    """

    @abstractmethod
    def submit_order_batch(self, batch: OrderBatch) -> OrderExecutionResult:
        """
        同步提交一组订单。
        生产上你可以再扩展为异步+回调/轮询。
        """
        raise NotImplementedError
```

### 3.2 一个最简单的 PaperBroker（纯内存，方便本地联调）

```python
# broker/paper_broker.py

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Dict, List

from broker.base import BrokerAdapter, OrderExecutionResult
from orders.types import OrderBatch, OrderRequest


@dataclass
class FilledOrder:
    order_id: str
    request: OrderRequest
    fill_price: float
    fill_quantity: int


@dataclass
class PaperBrokerState:
    """
    极简版 PaperBroker 状态：
    - 只记录所有成交（不维护持仓/资金），方便后面自己写一个 PortfolioService。
    """
    fills: List[FilledOrder] = field(default_factory=list)


class PaperBroker(BrokerAdapter):
    def __init__(self) -> None:
        self._state = PaperBrokerState()
        self._id_iter = itertools.count(1)

    @property
    def state(self) -> PaperBrokerState:
        return self._state

    def submit_order_batch(self, batch: OrderBatch) -> OrderExecutionResult:
        order_ids: List[str] = []

        for req in batch.orders:
            oid = f"PB-{next(self._id_iter)}"
            order_ids.append(oid)

            # 简化：假设所有订单都立刻按“理想价格”成交：
            # - MARKET: 用 tags 里传过来的 last_price
            # - LIMIT: 用 limit_price
            last_price = batch.decision_context.get("market_state", {}).get("last_price")

            if req.order_type.name == "MARKET":
                fill_price = float(last_price or 0.0)
            else:
                fill_price = float(req.limit_price or last_price or 0.0)

            fill = FilledOrder(
                order_id=oid,
                request=req,
                fill_price=fill_price,
                fill_quantity=req.quantity,
            )
            self._state.fills.append(fill)

        success = len(order_ids) == len(batch.orders)
        message = f"PaperBroker filled {len(order_ids)}/{len(batch.orders)} orders."

        return OrderExecutionResult(
            success=success,
            order_ids=order_ids,
            message=message,
            raw={"fills_count": len(self._state.fills)},
        )
```

> ✅ 有了这个 `PaperBroker`，你可以**在完全不动 IBKR 代码**的情况下，把：
>
> * Orchestrator → ExecutionAgent → OrderBuilder → BrokerAdapter
>   整条链在本地跑通，并且记录所有“虚拟成交”，给实验/回放用。

---

## 4. 串起来：一个完整的“从大脑到下单”的 demo

下面这个例子把前面几块拼在一起（只示意，不要求你现在就真的跑）：

```python
from experiments.registry import ExperimentRegistry
from prompts.prompt_library import PromptLibrary
from llm.llm_client import LLMClient

from brain.selector import StrategySelector, StrategySelectorConfig
from runtime.simple_xgb_runtime import SimpleXGBRuntime
from orchestrator.live_orchestrator import LiveOrchestrator

from execution.agent import ExecutionAgent, ExecutionAgentConfig
from orders.builder import OrderBuilder, OrderBuilderConfig, PositionSnapshot
from broker.paper_broker import PaperBroker

# ---- 0) 基础组件 ----
registry = ExperimentRegistry("experiments_store")
prompt_lib = PromptLibrary("prompts/prompt_library.json")
llm = LLMClient(model="gpt-5.1-thinking", base_url="http://localhost:1234/v1")
runtime = SimpleXGBRuntime()

selector_cfg = StrategySelectorConfig(
    metric_name="sharpe",
    min_score=0.8,
    max_candidates=5,
    default_mode="ensemble",
    default_prompt_id="v3_stable",
)

selector = StrategySelector(
    registry=registry,
    prompt_lib=prompt_lib,
    llm=llm,
    cfg=selector_cfg,
)

orchestrator = LiveOrchestrator(
    registry=registry,
    strategy_selector=selector,
    runtime=runtime,
)

exec_cfg = ExecutionAgentConfig(
    allowed_prompt_ids=[
        "v3_stable",
        "v3_fast_market_following",
        "v3_fast_market_following_tactical_20251117_v2",
    ],
    # ⚠️ 占位值：placeholder，后续必须用数据/回测校准
    min_confidence_for_open=0.6,
    max_size_frac=0.3,
)

exec_agent = ExecutionAgent(
    llm=llm,
    prompt_lib=prompt_lib,
    cfg=exec_cfg,
)

order_builder_cfg = OrderBuilderConfig(
    # ⚠️ 也需要回测校准
    min_notional_per_order=3_000.0,
    max_gross_exposure_frac_per_symbol=0.3,
    min_lot_size=1,
)

order_builder = OrderBuilder(cfg=order_builder_cfg)

broker = PaperBroker()

# ---- 1) 早盘：生成 SessionPlan ----
plan = orchestrator.prepare_session(
    symbol="TSLA",
    label_id="ret_10m_log",
    goal="Intraday 10-min direction with strict drawdown control.",
)

# ---- 2) 某个时间点：构造 agent_input ----
import pandas as pd

latest_features = pd.DataFrame(
    # TODO: 用你的特征 pipeline 真正生成特征
)

market_state = {"last_price": 250.35, "rvol20": 2.7, "spread_bp": 3.2}
risk_state = {"account_equity": 100_000, "day_pnl": -350, "max_day_loss": -1_000}

agent_input = orchestrator.build_agent_input(
    plan=plan,
    features=latest_features,
    market_state=market_state,
    risk_state=risk_state,
)

# ---- 3) 执行 Agent 做决策 ----
decision = exec_agent.decide(
    execution_prompt_id=plan.execution_prompt_id,
    agent_input=agent_input,
)

# ---- 4) 当前持仓快照（实际使用时从你的 portfolio service / IBKR 拉）----
position = PositionSnapshot(
    symbol="TSLA",
    quantity=0,
    avg_price=0.0,
    unrealized_pnl=0.0,
)

# ---- 5) 决策 → 订单 ----
batch = order_builder.build_orders(
    decision=decision,
    position=position,
    last_price=market_state["last_price"],
    account_equity=risk_state["account_equity"],
)

# ---- 6) 把订单交给 BrokerAdapter 执行（这里是 PaperBroker）----
if batch.orders:
    result = broker.submit_order_batch(batch)
    print("Order execution result:", result)
else:
    print("No orders generated for this decision.")
```

> 至此：
> 你已经有了一个**完整的、分层清晰的执行通路**：
> **ExperimentRegistry → StrategySelector → LiveOrchestrator → ExecutionAgent → OrderBuilder → BrokerAdapter**

---

## 下一步怎么走？

按照我们一开始约定的路线图，下一步我会回到你最关心的那层：

> **数据 & 特征 & 标签** —— 特别是：
>
> * **Sandbox 特征机制**：让 AI 在安全可控的 SQL 沙箱里“发明新特征”，通过体检 + 回测后晋升为正式特征
> * 把这个机制和 `PrincipalInvestigator`、`feature_registry.json`、`DatasetSanity` 彻底连起来

你刚才说“继续”，所以我就按这个顺序往下走：
**下一轮我们来设计并落地完整的 Sandbox Feature Pipeline**，从 LLM 提案 → 语法+泄露检查 → 临时特征 → 回测晋升逻辑。
好，那这轮我们就老老实实把 **Sandbox 特征管线**落下来，让：

> PI → LLM → 提出新特征 → 静态 + 动态体检 → 通过的记入 registry（标记为 sandbox）→ 参与这次训练 + 写进 ExperimentRegistry

我会按照“能直接落地改代码”的方式来写，不问你要东西，默认你已经有这些模块：

* `PrincipalInvestigator`（PI）
* `DatasetSanity` / `run_dataset_sanity_checks`
* `LLMClient`
* ClickHouse client

---

## 一、FeatureRegistry：统一管理“官方特征 + Sandbox 特征”

先加一个简单的 registry 模块，负责：

* 记录每个特征的：

  * 名字（alias）
  * SQL 表达式（expr）
  * 类型（base/derived/sandbox）
  * 元信息（来源、创建时间等）
* 提供：

  * `get(alias)` 看是否已存在
  * `register_sandbox_features(...)` 注册新 sandbox features
  * `save()` 写回 JSON

### `features/registry.py`

```python
# features/registry.py

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime


class FeatureKind(str, Enum):
    BASE = "base"
    DERIVED = "derived"


@dataclass
class FeatureSpec:
    """
    Registry 里的一条特征定义。
    对于 sandbox 特征，我们用 is_sandbox=True 来标记。
    """

    name: str
    expr: str
    kind: FeatureKind
    is_sandbox: bool = False

    description: str = ""
    tags: List[str] = field(default_factory=list)

    # 元信息：谁/何时创建，用来做审计和“晋升为正式特征”的依据
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    created_by: str = "manual"  # 或 "pi_llm"
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureRegistry:
    path: Path
    features: Dict[str, FeatureSpec]

    @classmethod
    def load(cls, path: str | Path) -> "FeatureRegistry":
        p = Path(path)
        if not p.exists():
            # 初次使用，给个空壳
            return cls(path=p, features={})

        raw = json.loads(p.read_text(encoding="utf-8"))
        feats: Dict[str, FeatureSpec] = {}

        for name, spec in raw.get("features", {}).items():
            feats[name] = FeatureSpec(
                name=name,
                expr=spec["expr"],
                kind=FeatureKind(spec["kind"]),
                is_sandbox=spec.get("is_sandbox", False),
                description=spec.get("description", ""),
                tags=spec.get("tags", []),
                created_at=spec.get("created_at", datetime.utcnow().isoformat()),
                created_by=spec.get("created_by", "manual"),
                meta=spec.get("meta", {}),
            )

        return cls(path=p, features=feats)

    def save(self) -> None:
        payload = {
            "_comment": "Central feature registry. Do not edit manually unless you know what you're doing.",
            "version": "1.0",
            "features": {},
        }
        for name, spec in self.features.items():
            payload["features"][name] = {
                "expr": spec.expr,
                "kind": spec.kind.value,
                "is_sandbox": spec.is_sandbox,
                "description": spec.description,
                "tags": spec.tags,
                "created_at": spec.created_at,
                "created_by": spec.created_by,
                "meta": spec.meta,
            }

        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ----------------- 查询辅助 ----------------- #

    def get(self, name: str) -> Optional[FeatureSpec]:
        return self.features.get(name)

    def exists(self, name: str) -> bool:
        return name in self.features

    def list_sandbox(self) -> List[FeatureSpec]:
        return [f for f in self.features.values() if f.is_sandbox]

    def list_official(self) -> List[FeatureSpec]:
        return [f for f in self.features.values() if not f.is_sandbox]

    # ----------------- 注册 sandbox 特征 ----------------- #

    def register_sandbox_features(
        self,
        specs: List[FeatureSpec],
        *,
        source: str = "pi_llm",
    ) -> List[FeatureSpec]:
        accepted: List[FeatureSpec] = []

        for spec in specs:
            # 冲突处理：如果 alias 已存在且 expr 不同，先拒绝，防止 silently 覆盖
            existing = self.features.get(spec.name)
            if existing and existing.expr != spec.expr:
                # 可以改成 log.warning(...)，这里简单跳过
                continue

            spec.is_sandbox = True
            spec.created_by = source
            if not spec.created_at:
                spec.created_at = datetime.utcnow().isoformat()

            self.features[spec.name] = spec
            accepted.append(spec)

        if accepted:
            self.save()

        return accepted
```

> 你可以以后再把原来的 `feature_programs.json` 慢慢迁移进来；现在先把这个 registry 用在 Sandbox 流程里就可以。

---

## 二、Sandbox Guard：表达式静态体检（防 SQL 注入 + 防泄露）

这里的目标是一个“快速但保守”的 static guard：

* 禁止：

  * 明显危险的关键字（`insert`, `update`, `delete`, `drop`, `join`, `union`, `;` 等）
  * 明显未来泄露（`lead(`, `future`, `fwd_`, `label`, `target`）
* 限制表达式长度 & 复杂度
* 确保用到的“疑似列名”要么是：

  * 出现在 `feature_catalog` 的列
  * 要么是允许的函数名

### `features/sandbox_guard.py`

```python
# features/sandbox_guard.py

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Set


@dataclass
class SandboxGuardConfig:
    max_expr_length: int = 512
    max_tokens: int = 80

    # 严格禁止的字符串（小写匹配）
    forbidden_substrings: List[str] = field(default_factory=lambda: [
        ";",
        " insert ",
        " update ",
        " delete ",
        " drop ",
        " alter ",
        " create ",
        " into ",
        " join ",
        " union ",
        " format ",
        " http",
        " file://",
        " mysql(",
        " postgresql(",
    ])

    # 明显泄露未来 / 标签的词
    forbidden_leakage_substrings: List[str] = field(default_factory=lambda: [
        " lead(",
        " lead0(",
        "future",
        "fwd_",
        "label",
        "target",
    ])

    # 允许的函数名（可以慢慢补充）
    allowed_functions: Set[str] = field(default_factory=lambda: {
        "abs",
        "log",
        "exp",
        "sqrt",
        "greatest",
        "least",
        "if",
        "multiIf",
        "lagInFrame",   # 注意：lag 是过去，不是未来
        "lag",
        "runningDifference",
        "movingAvg",
        "avg",
        "sum",
        "min",
        "max",
    })


@dataclass
class SandboxGuardResult:
    ok: bool
    errors: List[str]


def static_sandbox_guard(
    *,
    expr: str,
    feature_catalog: List[str],
    cfg: SandboxGuardConfig,
) -> SandboxGuardResult:
    """
    不做完整 SQL 解析，只做保守检查：
    - 长度 / token 数限制
    - 禁用关键字
    - 禁用明显未来/标签泄露
    - 判定“疑似标识符”是否在 {catalog ∪ allowed_functions}
    """

    errors: List[str] = []
    raw = expr.strip()
    low = raw.lower()

    if len(raw) > cfg.max_expr_length:
        errors.append(f"expr too long: {len(raw)} > {cfg.max_expr_length}")

    # 简单分词
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", raw)
    if len(tokens) > cfg.max_tokens:
        errors.append(f"too many tokens: {len(tokens)} > {cfg.max_tokens}")

    # 危险关键字
    padded = f" {low} "  # 方便匹配前后空格
    for bad in cfg.forbidden_substrings:
        if bad in padded:
            errors.append(f"contains forbidden substring: {bad.strip()}")

    for bad in cfg.forbidden_leakage_substrings:
        if bad in padded:
            errors.append(f"potential leakage: {bad.strip()}")

    # 标识符检查：简单把 tokens 分为“疑似列名/函数名”
    catalog_set = set(feature_catalog)
    allowed_funcs = cfg.allowed_functions

    for t in tokens:
        # 完全大写大概率是函数或关键字，不强行要求在 catalog 里
        if t in catalog_set:
            continue
        if t in allowed_funcs:
            continue
        if t.upper() == t:
            # 当成关键字/函数，暂时放行
            continue
        # 简单粗暴：如果既不是列名也不是允许函数，就提示
        #（这会有 false positive，但宁可严格一点）
        if t not in catalog_set and t not in allowed_funcs:
            # 可以选择 warnings 或 errors，这里当错误处理
            errors.append(f"identifier '{t}' not in feature_catalog or allowed_functions")

    return SandboxGuardResult(ok=(len(errors) == 0), errors=errors)
```

> 这个 static guard 是“保守型”：宁可多拒绝一点，也不让奇怪的东西溜进来。
> 以后你可以根据 ClickHouse 的函数集增加 `allowed_functions`。

---

## 三、SandboxFeatureEngine：从 recipe 中识别“新特征 → 校验 → 点击 ClickHouse 做动态体检 → 注册 registry”

核心思路：

* PI 的 LLM 产出的 `feature_recipe["derived"]` 里，**凡是 alias 在 registry 里不存在**的，都认为是“新特征候选（sandbox）”

* 对每个候选：

  1. 跑 `static_sandbox_guard`
  2. 在 ClickHouse 里 `SELECT {expr} AS {alias} ... LIMIT sample_rows` 拉一小批数据
  3. 用 `run_dataset_sanity_checks` 只对这一列跑检查
  4. 有 fatal 问题（全 NAN、全常数、巨大 outlier 且严重不稳定）就拒绝
  5. 通过的写入 `FeatureRegistry`（`is_sandbox=True`）并返回

* 最后返回：

  * `recipe_clean`：仅保留通过验证的 derived 特征（对于本次训练）
  * `accepted_specs`：被注册进入 registry 的 sandbox 特征

### `features/sandbox.py`

```python
# features/sandbox.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import pandas as pd

from clickhouse_connect.driver.client import Client as CHClient

from data_quality.dataset_sanity import (
    run_dataset_sanity_checks,
    DatasetSanityReport,  # 假设你之前定义了
)

from features.registry import FeatureRegistry, FeatureSpec, FeatureKind
from features.sandbox_guard import SandboxGuardConfig, static_sandbox_guard


@dataclass
class SandboxFeatureEngineConfig:
    # 每个 sandbox feature 在 ClickHouse 侧抽多少行做体检（避免全表扫）
    sample_rows: int = 20_000

    # 允许的最大缺失比例（> 这个比例就视为垃圾特征）
    max_nan_ratio: float = 0.3

    # 是否允许“几乎常数”的特征通过（一般不允许）
    max_unique_ratio_for_constant: float = 0.01  # unique/total < 1% 视为常数

    guard_cfg: SandboxGuardConfig = SandboxGuardConfig()


class SandboxFeatureEngine:
    def __init__(
        self,
        *,
        ch_client: CHClient,
        registry: FeatureRegistry,
        cfg: SandboxFeatureEngineConfig | None = None,
    ) -> None:
        self._ch = ch_client
        self._registry = registry
        self._cfg = cfg or SandboxFeatureEngineConfig()

    # ----------------- 对外主接口 ----------------- #

    def process_recipe(
        self,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        recipe: Dict[str, Any],
        feature_catalog: List[str],
    ) -> Tuple[Dict[str, Any], List[FeatureSpec]]:
        """
        1. 找出 recipe.derived 里“alias 在 registry 中不存在”的新特征候选
        2. 对每个候选做 static guard + ClickHouse 采样体检
        3. 通过的注册为 sandbox feature，并保留在 recipe 里参与本次训练
        4. 失败的从 recipe.derived 中剔除，并附加到 recipe["_sandbox_rejections"]
        """

        derived = recipe.get("derived", []) or []
        new_candidates = []
        kept_derived = []
        rejections: List[Dict[str, Any]] = []

        for d in derived:
            alias = d.get("alias")
            expr = d.get("expr")

            if not alias or not expr:
                # schema 不完整，直接丢弃
                rejections.append(
                    {
                        "alias": alias,
                        "expr": expr,
                        "reason": "missing alias or expr",
                    }
                )
                continue

            # 如果 registry 已存在（不管是不是 sandbox），当成“老特征”使用
            if self._registry.exists(alias):
                kept_derived.append(d)
            else:
                # 新特征候选
                new_candidates.append(d)

        accepted_specs: List[FeatureSpec] = []

        for cand in new_candidates:
            alias = cand["alias"]
            expr = cand["expr"]
            desc = cand.get("description", "")

            # 1) Static guard
            guard_res = static_sandbox_guard(
                expr=expr,
                feature_catalog=feature_catalog,
                cfg=self._cfg.guard_cfg,
            )
            if not guard_res.ok:
                rejections.append(
                    {
                        "alias": alias,
                        "expr": expr,
                        "reason": "static_guard_failed",
                        "details": guard_res.errors,
                    }
                )
                continue

            # 2) ClickHouse 采样体检
            sane = self._check_with_clickhouse(
                table=table,
                symbol=symbol,
                start_ts=start_ts,
                end_ts=end_ts,
                alias=alias,
                expr=expr,
            )
            if not sane.ok:
                rejections.append(
                    {
                        "alias": alias,
                        "expr": expr,
                        "reason": "dataset_sanity_failed",
                        "details": sane.summary,
                    }
                )
                continue

            # 3) 注册为 sandbox 特征
            spec = FeatureSpec(
                name=alias,
                expr=expr,
                kind=FeatureKind.DERIVED,
                is_sandbox=True,
                description=desc,
                tags=["sandbox", "pi_llm"],
                meta={
                    "source": "pi_llm",
                    "notes": cand.get("notes", ""),
                },
            )
            accepted_specs.append(spec)
            kept_derived.append(cand)

        if accepted_specs:
            self._registry.register_sandbox_features(accepted_specs, source="pi_llm")

        recipe_clean = {
            **recipe,
            "derived": kept_derived,
        }
        if rejections:
            recipe_clean["_sandbox_rejections"] = rejections

        return recipe_clean, accepted_specs

    # ----------------- 内部：ClickHouse + DatasetSanity ----------------- #

    @dataclass
    class _CheckResult:
        ok: bool
        summary: Dict[str, Any]

    def _check_with_clickhouse(
        self,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        alias: str,
        expr: str,
    ) -> _CheckResult:
        """
        在 ClickHouse 侧抽样计算一次新特征，然后用 DatasetSanity 做单列体检。
        """

        q = f"""
        SELECT
            {expr} AS {alias}
        FROM {table}
        WHERE symbol = %(symbol)s
          AND ts >= %(start_ts)s
          AND ts <= %(end_ts)s
        LIMIT %(limit)s
        """

        df: pd.DataFrame = self._ch.query_df(
            q,
            parameters={
                "symbol": symbol,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "limit": self._cfg.sample_rows,
            },
        )

        if df.empty:
            return self._CheckResult(
                ok=False,
                summary={"reason": "empty_sample"},
            )

        col = df[alias]

        nan_ratio = float(col.isna().mean())
        unique_ratio = float(col.nunique(dropna=True) / max(len(col), 1))

        if nan_ratio > self._cfg.max_nan_ratio:
            return self._CheckResult(
                ok=False,
                summary={
                    "reason": "too_many_nans",
                    "nan_ratio": nan_ratio,
                },
            )

        if unique_ratio < self._cfg.max_unique_ratio_for_constant:
            return self._CheckResult(
                ok=False,
                summary={
                    "reason": "almost_constant",
                    "unique_ratio": unique_ratio,
                },
            )

        # 调用全局 DatasetSanity，只针对这一列
        sanity_report: DatasetSanityReport = run_dataset_sanity_checks(
            df=df[[alias]],
            dataset_name=f"sandbox_feature_{alias}",
        )

        if sanity_report.has_fatal_issues:
            return self._CheckResult(
                ok=False,
                summary={
                    "reason": "dataset_sanity_fatal",
                    "issues": [i.to_dict() for i in sanity_report.issues],
                },
            )

        return self._CheckResult(
            ok=True,
            summary={
                "nan_ratio": nan_ratio,
                "unique_ratio": unique_ratio,
                "issues": [i.to_dict() for i in sanity_report.issues],
            )
```

> 注意：
>
> * 这里 `max_nan_ratio` / `max_unique_ratio_for_constant` 是 **配置项**，不是“写死的规则”；你后面可以用历史数据统计来调优这些阈值。
> * 真正的 `DatasetSanityReport` 接口以你之前的实现为准，我这里只是示意用法。

---

## 四、把 Sandbox Engine 挂到 PrincipalInvestigator 上

现在我们要让 PI 在 `propose()` 里，自动把 LLM 给的 `feature_recipe` 经过 Sandbox 引擎处理一次。

### 1. 扩展 `PIResult`

```python
# 在你之前的 PIResult 基础上加一个字段

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import pandas as pd
from features.registry import FeatureSpec  # 新增

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
    # 新增：这次调用中被注册/使用的 sandbox 特征
    sandbox_features: List[FeatureSpec] | None = None
```

### 2. 修改 `PrincipalInvestigator` 初始化

```python
# 在 pi/principal_investigator.py（或者你原来的文件）中：

from features.registry import FeatureRegistry
from features.sandbox import SandboxFeatureEngine, SandboxFeatureEngineConfig

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        *,
        feature_registry: Optional[FeatureRegistry] = None,
        sandbox_cfg: Optional[SandboxFeatureEngineConfig] = None,
    ) -> None:
        self._ch_cfg = ch_cfg
        self._llm = LLMClient(
            model=(llm_cfg or {}).get("model"),
            base_url=(llm_cfg or {}).get("base_url"),
        )
        self._client = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=ch_cfg.get("port", 8123),
            username=ch_cfg.get("username", ch_cfg.get("user", "default")),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "default"),
        )

        self._feature_registry = feature_registry
        self._sandbox_engine: Optional[SandboxFeatureEngine] = None

        if feature_registry is not None:
            self._sandbox_engine = SandboxFeatureEngine(
                ch_client=self._client,
                registry=feature_registry,
                cfg=sandbox_cfg,
            )
```

### 3. 在 `propose()` 里插入 Sandbox 流程

```python
    def propose(
        self,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        goal: str,
        horizon: str,
        guard_cfg: Optional[Dict[str, Any]] = None,
    ) -> PIResult:
        catalog = _fetch_catalog(self._client, table)

        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"特征: {', '.join(catalog)}\n"
            f"预测窗口: {horizon}"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
            "feature_recipe.derived 必须是形如 {alias, expr, description?} 的列表。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        sandbox_specs = []

        # ---------- ★ Sandbox 步骤插入点 ★ ---------- #
        if self._sandbox_engine is not None and recipe.get("derived"):
            recipe, sandbox_specs = self._sandbox_engine.process_recipe(
                table=table,
                symbol=symbol,
                start_ts=start_ts,
                end_ts=end_ts,
                recipe=recipe,
                feature_catalog=catalog,
            )
        # ------------------------------------------- #

        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=tplan.get("target"),
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        grid = {
            "model_type": [],
            "feature_subset": [
                recipe.get("base_features", [])
                + [d.get("alias") for d in recipe.get("derived", [])]
            ],
            "task": [tplan.get("task", "reg")],
            "decision": [
                tplan.get(
                    "decision",
                    {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
                )
            ],
            "model_params": [],
        }
        for cand in tplan.get("model_candidates", []) or []:
            grid["model_type"].append(cand.get("model_type", "xgb"))
            grid["model_params"].append(cand.get("model_params", {}))

        guard = (guard_cfg or {}) | {
            "allow_params": ["model_type", "feature_subset", "task", "decision", "model_params"]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            sandbox_features=sandbox_specs or None,
        )
```

> 至此：
>
> * **新 alias** 的 derived 特征，只有通过：
>
>   * 静态 guard
>   * ClickHouse 采样 + DatasetSanity
>   * Registry 注册
>     才能真的进入训练数据 & HPO grid。
> * 所有通过的 sandbox 特征，都会写入 `FeatureRegistry`，以后可以被回测/实验记录引用。

---

## 五、下一步可以做什么？

按照你之前要求的三层：

1. **数据 & 特征 & 标签这一层**

   * ✅ 已经有：

     * DatasetSanity（体检）
     * Sandbox 特征（静态 + 动态体检 + registry）
   * 🔜 下一步可以：

     * 在 `ExperimentRecord` 里记录本次实验用到的 sandbox 特征列表（用于“晋升机制”：多次证明有用 → 从 sandbox 提拔为 official）

2. **机器学习 / HPO / ExperimentRegistry 这一层**

   * 需要把：

     * `PIResult.sandbox_features` 写进 `ExperimentRecord.meta["sandbox_features"]`
     * 并在模型训练完之后，把 feature importance / metric 写回 `ExperimentRegistry`

3. **AI 大脑 & Prompt 安全这一层**

   * 利用：

     * DatasetSanity 报告 + sandbox 成功/失败信息
     * 在 prompt 里教育 AI：
       “哪些表达式被拒绝 / 为什么被拒绝”，让它下次少踩坑

你刚刚说“继续”，这一轮我先把 **Sandbox Feature Pipeline** 这一条链打通了。
如果你愿意，下一轮我就去对接 **ExperimentRegistry：把 sandbox 特征 + 体检信息 + 实验结果串起来**，让 AI 大脑不仅能“发明”，还能根据历史实验结果学会“哪些特征是好人，哪些是坑”。
