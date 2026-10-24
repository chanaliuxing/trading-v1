好，继续把这条链路往下打通：

> **BrainDecision → Live Orchestrator → 执行 Agent**

上一轮我们已经有：

* `ExperimentRegistry`：存好每个 run 的指标、数据区间、sanity 等
* `StrategySelector`：大脑只在 *allowed_run_ids* 里选策略 + 选执行 prompt（带硬校验）

现在这一轮，我们加上：

1. **策略运行时接口（StrategyRuntime）**：给 run_id → 模型加载 & 推理的统一入口
2. **交易 Session 计划（SessionPlan）**：把 BrainDecision 变成一个可执行的“当日作战计划”
3. **LiveOrchestrator**：串起来——早盘选策略，盘中调用模型 + 执行 Agent

我会给你**完整代码骨架**，但需要你自己在几个 TODO 处接上你现有的 HPO / 模型保存 / 下单逻辑。

---

## 1. 策略运行时接口：StrategyRuntime

这个层就是“给我一个 run_id，我帮你把模型+特征信息准备好，后面你只管调用 `predict()`”。

### `runtime/strategy_runtime.py`

```python
# runtime/strategy_runtime.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Protocol

import pandas as pd

from experiments.types import ExperimentRecord


@dataclass
class ModelHandle:
    """
    一个已加载好的模型句柄 + 需要的元数据。
    这里不假设具体框架（xgboost/lightgbm/torch/sklearn），由你在实现类里决定。
    """
    run_id: str
    model: Any
    feature_columns: List[str]
    label_id: str
    meta: Dict[str, Any]


class StrategyRuntime(Protocol):
    """
    抽象接口：run_id -> ModelHandle；ModelHandle + features -> 预测结果。

    好处：
    - LiveOrchestrator 不关心模型是 XGB 还是 Torch
    - 你可以后面换框架，而 Orchestrator 代码不用变
    """

    def load_model(self, rec: ExperimentRecord) -> ModelHandle:
        """
        根据 ExperimentRecord 加载模型。

        要求：
        - 从 rec.model.artifact_path 中找到模型文件/目录
        - 从 rec.feature_recipe 中恢复特征列顺序
        """
        ...

    def infer(
        self,
        handle: ModelHandle,
        features: pd.DataFrame,
    ) -> pd.Series:
        """
        对一批样本做预测（单步/多步由你的模型决定）。

        返回：
        - pd.Series，index 对齐到 features.index，如预测的是 score/probability。
        """
        ...
```

你可以先写一个“假实现”（mock），对接你现有的 HPO / 模型保存结构时，只要实现这两个方法即可。

例如：

### `runtime/simple_xgb_runtime.py`（示意）

```python
# runtime/simple_xgb_runtime.py

from __future__ import annotations

from pathlib import Path
from typing import List

import joblib
import pandas as pd

from experiments.types import ExperimentRecord
from .strategy_runtime import StrategyRuntime, ModelHandle


class SimpleXGBRuntime(StrategyRuntime):
    """
    示例实现：
    - 假设所有模型都是 xgboost/sklearn 风格的 .pkl
    - feature 列从 rec.feature_recipe["selected_features"] 拿
    """

    def load_model(self, rec: ExperimentRecord) -> ModelHandle:
        path = Path(rec.model.artifact_path)
        if not path.exists():
            raise FileNotFoundError(f"Model artifact not found for run {rec.run_id}: {path}")

        model = joblib.load(path)

        recipe = rec.feature_recipe or {}
        feat_cols: List[str] = recipe.get("selected_features") or recipe.get("base_features") or []
        if not feat_cols:
            raise ValueError(f"No feature columns found in feature_recipe for run {rec.run_id}")

        return ModelHandle(
            run_id=rec.run_id,
            model=model,
            feature_columns=feat_cols,
            label_id=rec.dataset.label_id,
            meta={
                "model_type": rec.model.model_type,
                "params": rec.model.params,
            },
        )

    def infer(
        self,
        handle: ModelHandle,
        features: pd.DataFrame,
    ) -> pd.Series:
        X = features[handle.feature_columns]
        # sklearn/xgboost 风格接口
        if hasattr(handle.model, "predict_proba"):
            # 二分类时，取正类概率
            prob = handle.model.predict_proba(X)[:, 1]
            return pd.Series(prob, index=features.index, name=f"{handle.run_id}_score")
        else:
            y = handle.model.predict(X)
            return pd.Series(y, index=features.index, name=f"{handle.run_id}_score")
```

> ✅ 你可以继续用 mock 模型；等真正接入 HPO 保存的模型路径时，只改这个 runtime 文件就够了。

---

## 2. 交易 Session 计划：SessionPlan

BrainDecision 还只是“脑子里的想法”；我们要把它转换成一个**当日 Session 的计划**：

* 用哪些 run_id
* 各自权重
* 对应的 ModelHandle
* 使用哪个执行 prompt

### `orchestrator/types.py`

```python
# orchestrator/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List

from brain.types import BrainDecision
from runtime.strategy_runtime import ModelHandle


@dataclass
class SessionStrategyConfig:
    """
    每个策略在本次 Session 中的配置。
    """
    run_id: str
    weight: float
    role: str
    model_handle: ModelHandle


@dataclass
class SessionPlan:
    """
    一次交易 Session 的完整计划：
    - 针对某个 symbol + label
    - 大脑已经选定的策略组合
    - 已经加载好的模型句柄（避免盘中频繁 load）
    """
    session_id: str
    symbol: str
    label_id: str
    mode: str
    execution_prompt_id: str
    strategies: List[SessionStrategyConfig]
    notes: str

    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "symbol": self.symbol,
            "label_id": self.label_id,
            "mode": self.mode,
            "execution_prompt_id": self.execution_prompt_id,
            "notes": self.notes,
            "strategies": [
                {
                    "run_id": s.run_id,
                    "weight": s.weight,
                    "role": s.role,
                    "model_handle": {
                        "run_id": s.model_handle.run_id,
                        "feature_columns": s.model_handle.feature_columns,
                        "label_id": s.model_handle.label_id,
                        "meta": s.model_handle.meta,
                    },
                }
                for s in self.strategies
            ],
        }
```

> 这里 `model_handle` 只导出 meta 信息到 dict；真正的 `model` 对象留在内存里给 Orchestrator 用，不要序列化。

---

## 3. LiveOrchestrator：串起来

### 3.1 核心职责

* **Pre-market（比如 6:27）**：

  1. 调 `StrategySelector` → `BrainDecision`
  2. 根据 `BrainDecision.choices` 从 `ExperimentRegistry` 拿到每个 run 的 `ExperimentRecord`
  3. 用 `StrategyRuntime.load_model()` 加载模型
  4. 生成 `SessionPlan`（可保存为 JSON 用于审计）

* **Intraday（盘中）**：

  * 接收实时特征 DataFrame（比如最新 1 条/几条 bar 的特征）
  * 对 `SessionPlan.strategies` 中每个模型做 `infer`
  * 用 `execution_prompt_id` 作为执行 Agent 的 prompt ID
  * 把模型输出 + 风控 + 市场状态喂给执行 Agent，得到下单指令

（执行 Agent 本身你已经有雏形了，我们这里只做接口）

---

### 3.2 LiveOrchestrator 代码骨架

```python
# orchestrator/live_orchestrator.py

from __future__ import annotations

import uuid
from typing import Dict, List

import pandas as pd

from experiments.registry import ExperimentRegistry
from experiments.types import ExperimentRecord
from brain.selector import StrategySelector
from brain.types import BrainDecision
from runtime.strategy_runtime import StrategyRuntime, ModelHandle
from .types import SessionPlan, SessionStrategyConfig


class LiveOrchestrator:
    """
    串联：
      ExperimentRegistry → StrategySelector → StrategyRuntime → 执行 Agent

    这里不直接下单，只生成：
      - SessionPlan（早盘）
      - 每一时刻的模型输出 + 执行 Agent 输入 payload（盘中）
    """

    def __init__(
        self,
        *,
        registry: ExperimentRegistry,
        strategy_selector: StrategySelector,
        runtime: StrategyRuntime,
    ) -> None:
        self._registry = registry
        self._selector = strategy_selector
        self._runtime = runtime

    # ------------------------------------------------------------------ #
    # Pre-market: 生成 SessionPlan
    # ------------------------------------------------------------------ #

    def prepare_session(
        self,
        *,
        symbol: str,
        label_id: str,
        goal: str,
        explicit_run_ids: List[str] | None = None,
        session_id: str | None = None,
    ) -> SessionPlan:
        """
        生成当日 SessionPlan：
        - 如果 explicit_run_ids 不为空，表示你手动点名要用哪些 run（绕过大脑选）
        - 否则调用 StrategySelector 让大脑在 registry 中选
        """

        if explicit_run_ids:
            # 手动指定策略的情况：构造一个假的 BrainDecision
            decision = self._build_decision_from_manual_runs(
                symbol=symbol,
                label_id=label_id,
                run_ids=explicit_run_ids,
            )
        else:
            # 正常情况：大脑选策略
            decision: BrainDecision = self._selector.select_strategies(
                symbol=symbol,
                label_id=label_id,
                goal=goal,
            )

        sess_id = session_id or f"{symbol}-{label_id}-{uuid.uuid4().hex[:8]}"

        # 从 registry 加载每个 run 的模型
        strat_cfgs: List[SessionStrategyConfig] = []
        for choice in decision.choices:
            rec: ExperimentRecord = self._registry.get(choice.run_id)
            handle: ModelHandle = self._runtime.load_model(rec)
            cfg = SessionStrategyConfig(
                run_id=choice.run_id,
                weight=choice.weight,
                role=choice.role,
                model_handle=handle,
            )
            strat_cfgs.append(cfg)

        plan = SessionPlan(
            session_id=sess_id,
            symbol=symbol,
            label_id=label_id,
            mode=decision.mode,
            execution_prompt_id=decision.execution_prompt_id,
            strategies=strat_cfgs,
            notes=decision.notes,
        )
        return plan

    def _build_decision_from_manual_runs(
        self,
        *,
        symbol: str,
        label_id: str,
        run_ids: List[str],
    ) -> BrainDecision:
        """
        如果你想绕过大脑，手动挑 run_id：
        - 权重自动均分
        - execution_prompt_id 用 StrategySelectorConfig 的 default
        """
        from brain.types import BrainDecision, StrategyChoice  # 避免循环导入
        from brain.selector import StrategySelectorConfig

        # 这里用 selector 的默认配置
        # 注意：如果你在初始化 StrategySelector 时没有保留 cfg，这里可以改成外部传入。
        cfg = StrategySelectorConfig()

        n = len(run_ids)
        if n == 0:
            raise ValueError("explicit_run_ids is empty")
        w = 1.0 / n

        choices = [StrategyChoice(run_id=r, weight=w, role="primary") for r in run_ids]

        return BrainDecision(
            symbol=symbol,
            label_id=label_id,
            mode="ensemble",
            execution_prompt_id=cfg.default_prompt_id,
            choices=choices,
            notes="Manual selection of runs, equal-weight ensemble.",
        )

    # ------------------------------------------------------------------ #
    # Intraday: 给执行 Agent 的输入构造
    # ------------------------------------------------------------------ #

    def build_agent_input(
        self,
        *,
        plan: SessionPlan,
        features: pd.DataFrame,
        market_state: Dict[str, any],
        risk_state: Dict[str, any],
    ) -> Dict[str, any]:
        """
        在盘中调用：
        - 用每个模型对当前 features 做预测
        - 按权重合成一个 ensemble score（或保留每个模型的输出）
        - 构造给 Execution Agent（LLM 交易决策）的输入 payload

        注意：这里只构造 payload，不直接调 LLM 或下单。
        """

        per_model_scores: Dict[str, pd.Series] = {}
        ensemble = None

        for strat in plan.strategies:
            handle = strat.model_handle
            score = self._runtime.infer(handle, features)
            per_model_scores[handle.run_id] = score

            w = strat.weight
            if ensemble is None:
                ensemble = w * score
            else:
                ensemble = ensemble + w * score

        # 如果 features 只是一条样本，你可以把 score.iloc[-1] 作为当前 bar 的值
        latest_idx = features.index[-1]
        latest_scores = {
            run_id: s.loc[latest_idx]
            for run_id, s in per_model_scores.items()
        }
        latest_ensemble = ensemble.loc[latest_idx] if ensemble is not None else None

        payload = {
            "session": {
                "session_id": plan.session_id,
                "symbol": plan.symbol,
                "label_id": plan.label_id,
                "mode": plan.mode,
                "execution_prompt_id": plan.execution_prompt_id,
                "notes": plan.notes,
            },
            "time_index": str(latest_idx),
            "model_outputs": {
                "per_model": latest_scores,
                "ensemble": latest_ensemble,
            },
            "market_state": market_state,  # 你可以在外面提供：价格、盘口、RVOL、ATR 等（数据驱动）
            "risk_state": risk_state,      # 账户杠杆、已用保证金、当日损益、风控门限等
        }
        return payload
```

> 现在 Orchestrator 在盘中只做两件事：
>
> 1. 用选好的模型算出最新 bar 的 score（以及 ensemble）
> 2. 把这些数字 + 市场&账户状态打包成一个 JSON，丢给“执行 Agent LLM”来做最终**多模态/多信息**决策

后面你可以有一个 `ExecutionAgent` 专门负责：

* 读取 `execution_prompt_id` → 从 `PromptLibrary` 拿 prompt 文本
* 把 `payload` 序列化成 JSON 放进 user_prompt
* 要求 LLM 只输出 `{"action": "...", "size": ..., "confidence": ...}` 之类的标准 JSON
* 再交给你的 IBKR 执行模块

---

## 4. 一个端到端调用示例

假设你已经：

* 有 `experiments_store` 目录 + `ExperimentRegistry` 里有若干 run
* 有 `prompt_library.json`
* 有一个选策略的大脑（`StrategySelector`）
* 有一个 `StrategyRuntime` 实现（哪怕是 mock）

你可以在一个 Notebook / CLI 里这样跑：

```python
from experiments.registry import ExperimentRegistry
from prompts.prompt_library import PromptLibrary
from llm.llm_client import LLMClient
from brain.selector import StrategySelector, StrategySelectorConfig
from runtime.simple_xgb_runtime import SimpleXGBRuntime
from orchestrator.live_orchestrator import LiveOrchestrator

# 0) 基础组件
registry = ExperimentRegistry("experiments_store")
prompt_lib = PromptLibrary("prompts/prompt_library.json")

llm = LLMClient(
    model="gpt-5.1-thinking",
    base_url="http://localhost:1234/v1",
)

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

runtime = SimpleXGBRuntime()

orchestrator = LiveOrchestrator(
    registry=registry,
    strategy_selector=selector,
    runtime=runtime,
)

# 1) 早盘：选策略 + 载模型
plan = orchestrator.prepare_session(
    symbol="TSLA",
    label_id="ret_10m_log",
    goal="Intraday 10-min direction with strict drawdown control.",
)

print("SessionPlan:", plan.to_dict())

# 2) 盘中：每个新 bar / snapshot 调用一次
import pandas as pd

# 示例特征：你真实的 feature pipeline 输出
latest_features = pd.DataFrame(
    # ...
)

market_state = {
    "last_price": 250.35,
    "rvol20": 2.7,
    "spread_bp": 3.2,
    # ...
}
risk_state = {
    "account_equity": 100_000,
    "day_pnl": -350,
    "max_day_loss": -1000,
    "current_leverage": 1.3,
    # ...
}

agent_input = orchestrator.build_agent_input(
    plan=plan,
    features=latest_features,
    market_state=market_state,
    risk_state=risk_state,
)

print("Agent payload:", agent_input)

# 3) ExecutionAgent（你后面可以写）：
# action = execution_agent.decide(agent_input, execution_prompt_id=plan.execution_prompt_id)
# broker.send_orders(action)
```

---

## 5. 到这里你已经具备了什么？

**现在这条链路是完整的（只差具体实现细节）**：

1. **数据/特征/标签层**

   * PI 提案 → 安全 SQL → DatasetSanity → DatasetManifest
2. **ML/HPO/实验治理层**

   * HPO 训练 → `ExperimentRecord` → `ExperimentRegistry`
3. **AI 大脑 & Prompt 安全层**

   * `BrainContext` / `BrainDecision` + prompt 白名单 + run_id 白名单
4. **执行层（本轮新加）**

   * `StrategyRuntime` 统一模型接口
   * `SessionPlan` 把大脑想法变成“作战计划”
   * `LiveOrchestrator` 把模型输出 + 市场&风险状态打包给执行 Agent

---

如果你接下来继续说“继续”，我会做两件事之一（按顺序）：

1. 补一个 **ExecutionAgent 骨架**：

   * 接 `agent_input` + `execution_prompt_id`
   * 用 PromptLibrary 拼 prompt
   * 强制 LLM 输出 `{"action": ..., "size": ..., "confidence": ..., "reason": ...}` 的 JSON（带硬校验）

2. 回到 **数据&特征层**，把 **sandbox 特征** 机制的 SQL & 校验器写成可用的代码模块，让 PI 可以在安全防泄露前提下“自己发明新特征”。

你不用选，我会按之前的路线图继续往下填坑。
好，我们把**执行 Agent**这块补上，把整条链真正闭环到「给大脑一个 JSON payload → 得到一个标准化的交易决策」。

接下来这一轮我做三件事：

1. 定义 **ExecutionDecision schema + 校验器**（AI 只能在这个“轨道”里说话）
2. 实现 **ExecutionAgent**：用 `prompt_library.json` + `LLMClient`，强制输出合法 JSON 决策
3. 给你一个 **端到端调用示例**：`LiveOrchestrator → ExecutionAgent → (你自己的 broker 层)`

---

## 1. ExecutionDecision：决策协议 & 硬校验

### `execution/types.py`

```python
# execution/types.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional


ActionType = Literal["OPEN", "CLOSE", "HOLD", "REDUCE", "FLATTEN"]
SideType = Literal["LONG", "SHORT", "FLAT"]
OrderType = Literal["MARKET", "LIMIT"]


@dataclass
class ExecutionDecision:
    """
    LLM 最终要给你的唯一“语言”：
    - 做什么 (action/side)
    - 做多大 (size_frac)
    - 用什么方式下单 (order_type/limit_offset_bps)
    - 它自己的信心 (confidence)
    - 为什么这么干 (reason)，方便你事后审计/训练
    """

    action: ActionType
    side: SideType
    size_frac: float  # 0~1 之间，表示占用“本 symbol 上限”的比例
    order_type: OrderType
    limit_offset_bps: Optional[float]  # 对 last_price 的偏移, 单位 bp (1/10000)
    confidence: float  # 0~1
    time_in_force: Literal["DAY", "IOC", "FOK", "GTD"]
    reason: str

    # 一些辅助信息（方便审计，不影响执行逻辑）
    symbol: str
    session_id: str
    label_id: str

    # 预留字段：可以让大脑申请“额外检查/人工确认”等
    flags: Dict[str, Any]


class DecisionValidationError(Exception):
    pass


def validate_decision_payload(raw: Dict[str, Any]) -> ExecutionDecision:
    """
    把 LLM 返回的 dict 转成 ExecutionDecision，并做硬校验。
    一旦不合法，直接抛异常，让上层决定：当作 HOLD 处理 / 重试一次 / 报警。
    """

    def _require(key: str, expected_type: Any):
        if key not in raw:
            raise DecisionValidationError(f"Missing required field: {key}")
        val = raw[key]
        if expected_type is not Any and not isinstance(val, expected_type):
            raise DecisionValidationError(
                f"Field {key} has wrong type: expected {expected_type}, got {type(val)}"
            )
        return val

    action = _require("action", str).upper()
    side = _require("side", str).upper()
    order_type = _require("order_type", str).upper()
    size_frac = float(_require("size_frac", (int, float)))
    confidence = float(_require("confidence", (int, float)))
    time_in_force = _require("time_in_force", str).upper()
    reason = _require("reason", str)
    symbol = _require("symbol", str)
    session_id = _require("session_id", str)
    label_id = _require("label_id", str)
    flags = raw.get("flags") or {}

    limit_offset_bps_raw = raw.get("limit_offset_bps")
    limit_offset_bps = None
    if limit_offset_bps_raw is not None:
        limit_offset_bps = float(limit_offset_bps_raw)

    # 枚举校验
    if action not in ("OPEN", "CLOSE", "HOLD", "REDUCE", "FLATTEN"):
        raise DecisionValidationError(f"Invalid action: {action}")

    if side not in ("LONG", "SHORT", "FLAT"):
        raise DecisionValidationError(f"Invalid side: {side}")

    if order_type not in ("MARKET", "LIMIT"):
        raise DecisionValidationError(f"Invalid order_type: {order_type}")

    if time_in_force not in ("DAY", "IOC", "FOK", "GTD"):
        raise DecisionValidationError(f"Invalid time_in_force: {time_in_force}")

    # 数值范围校验 —— ⚠️ 具体阈值要用数据校准，这里只做基础 0~1 限制
    if not (0.0 <= size_frac <= 1.0):
        raise DecisionValidationError(f"size_frac out of range [0,1]: {size_frac}")

    if not (0.0 <= confidence <= 1.0):
        raise DecisionValidationError(f"confidence out of range [0,1]: {confidence}")

    return ExecutionDecision(
        action=action,           # type: ignore
        side=side,               # type: ignore
        size_frac=size_frac,
        order_type=order_type,   # type: ignore
        limit_offset_bps=limit_offset_bps,
        confidence=confidence,
        time_in_force=time_in_force,  # type: ignore
        reason=reason,
        symbol=symbol,
        session_id=session_id,
        label_id=label_id,
        flags=flags,
    )
```

> ✅ 到这一步：**无论 LLM 想干啥，你的系统只认这一个 dataclass**，其他全部拒收。

---

## 2. ExecutionAgent：用 prompt 库 + LLMClient 生成标准决策

### 目标

* 只允许使用 **白名单中的 prompt_id**（来自你发的 `prompt` JSON）
* 所有 prompt 文本从 `PromptLibrary` 读，避免 runtime prompt 注入
* 把 `agent_input`（前面 `LiveOrchestrator.build_agent_input` 生成的 payload）作为 user_content 的 JSON
* 利用 `LLMClient`，要求它返回 Python dict，然后用 `validate_decision_payload` 转为 `ExecutionDecision`
* 再叠加一层**本地风控**（例如：size_frac ≤ config.max_size_frac；低于某 confidence 自动 HOLD）

### `execution/agent.py`

```python
# execution/agent.py

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from llm.llm_client import LLMClient
from prompts.prompt_library import PromptLibrary
from execution.types import ExecutionDecision, DecisionValidationError, validate_decision_payload


@dataclass
class ExecutionAgentConfig:
    """
    执行 Agent 的本地约束：
    - 这里的数值门槛建议用历史数据 + 回测校准，不要写死在代码里
    """
    allowed_prompt_ids: List[str]

    # 以下门槛只是一个骨架，具体数值需要你用数据驱动方式填充
    min_confidence_for_open: float = 0.6
    max_size_frac: float = 0.4
    default_time_in_force: str = "DAY"

    # 如果 LLM 出错，系统 fallback 为 HOLD
    fallback_to_hold_on_error: bool = True


class ExecutionAgent:
    """
    把 LiveOrchestrator 提供的 agent_input + execution_prompt_id
    转换成一个结构化的 ExecutionDecision。
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        prompt_lib: PromptLibrary,
        cfg: ExecutionAgentConfig,
    ) -> None:
        self._llm = llm
        self._prompt_lib = prompt_lib
        self._cfg = cfg

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #

    def decide(
        self,
        *,
        execution_prompt_id: str,
        agent_input: Dict[str, Any],
    ) -> ExecutionDecision:
        """
        核心流程：
        1. 校验 prompt_id 是否在白名单
        2. 拼接 system_prompt + user_prompt(JSON)
        3. 调 LLM，拿到 dict 结果
        4. validate_decision_payload → ExecutionDecision
        5. 套本地 risk clamp（size_frac / confidence 等）
        """

        if execution_prompt_id not in self._cfg.allowed_prompt_ids:
            raise ValueError(f"execution_prompt_id not allowed: {execution_prompt_id}")

        base_prompt = self._prompt_lib.get(execution_prompt_id)

        system_prompt = (
            base_prompt
            + "\n\n"
            "You are the execution decision agent. "
            "You MUST reply with a single JSON object ONLY, no markdown, no extra text. "
            "The JSON MUST match this schema (keys and types):\n"
            "{\n"
            '  "action": "OPEN | CLOSE | HOLD | REDUCE | FLATTEN",\n'
            '  "side": "LONG | SHORT | FLAT",\n'
            '  "size_frac": float (0.0~1.0),\n'
            '  "order_type": "MARKET | LIMIT",\n'
            '  "limit_offset_bps": float or null,\n'
            '  "confidence": float (0.0~1.0),\n'
            '  "time_in_force": "DAY | IOC | FOK | GTD",\n'
            '  "reason": string,\n'
            '  "symbol": string,\n'
            '  "session_id": string,\n'
            '  "label_id": string,\n'
            '  "flags": object (optional extra hints)\n'
            "}\n"
            "If you are unsure, choose HOLD with side='FLAT', size_frac=0.0.\n"
        )

        user_prompt = (
            "Here is the current session + model outputs + market_state + risk_state as JSON:\n"
            f"{json.dumps(agent_input, ensure_ascii=False)}"
        )

        try:
            # 假设 LLMClient.generate 返回的是已经 parse 好的 dict，
            # 如果它返回字符串，你可以先 json.loads 再传给 validate_decision_payload。
            raw = self._llm.generate(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            if isinstance(raw, str):
                raw = json.loads(raw)

            decision = validate_decision_payload(raw)
        except (DecisionValidationError, json.JSONDecodeError, TypeError, KeyError) as e:
            if not self._cfg.fallback_to_hold_on_error:
                raise

            # 构造一个“保守 HOLD 决策”
            sess = agent_input.get("session", {})
            return ExecutionDecision(
                action="HOLD",
                side="FLAT",
                size_frac=0.0,
                order_type="MARKET",
                limit_offset_bps=None,
                confidence=0.0,
                time_in_force=self._cfg.default_time_in_force.upper(),
                reason=f"Fallback HOLD due to error: {e}",
                symbol=sess.get("symbol", ""),
                session_id=sess.get("session_id", ""),
                label_id=sess.get("label_id", ""),
                flags={"error": str(e)},
            )

        # 应用本地 risk clamp：
        decision = self._apply_local_risk_clamp(decision)
        return decision

    # ------------------------------------------------------------------ #
    # 本地风险限制（不依赖 LLM）
    # ------------------------------------------------------------------ #

    def _apply_local_risk_clamp(self, decision: ExecutionDecision) -> ExecutionDecision:
        """
        最后一层保护：
        - 信心低于门槛，不允许 OPEN/加仓，自动退化为 HOLD or REDUCE
        - size_frac 上限截断，避免“张口就是 100% 仓位”
        """

        # clamp size_frac
        if decision.size_frac > self._cfg.max_size_frac:
            decision.size_frac = self._cfg.max_size_frac

        # 信心不足时，不允许主动开新仓
        if decision.confidence < self._cfg.min_confidence_for_open:
            if decision.action in ("OPEN",):
                # 改成 HOLD/FLAT
                decision.action = "HOLD"   # type: ignore
                decision.side = "FLAT"     # type: ignore
                decision.size_frac = 0.0
                decision.reason += (
                    f" | Local clamp: confidence {decision.confidence:.3f} "
                    f"< min_open {self._cfg.min_confidence_for_open:.3f}, forced HOLD."
                )

        # 如果 time_in_force 乱填，这里兜底
        tif = decision.time_in_force.upper()
        if tif not in ("DAY", "IOC", "FOK", "GTD"):
            decision.time_in_force = self._cfg.default_time_in_force.upper()  # type: ignore

        return decision
```

> ✅ 现在：
>
> * **Prompt 安全**：只能用 `allowed_prompt_ids` 中的某一个 ID → 去 prompt 库取模板
> * **输出安全**：`validate_decision_payload` + `_apply_local_risk_clamp` 双层过滤
> * 你可以在任何时候增加新的执行 prompt（比如 `v3_fast_market_following_tactical_202511xx`），只要写进 `prompt_library.json` + 加入 `allowed_prompt_ids`，系统就能用。

---

## 3. 串联：LiveOrchestrator + ExecutionAgent

下面是一个你可以直接放到 Notebook 里试的**端到端伪代码**（不用马上真的连上 IBKR，只跑到 ExecutionDecision）：

```python
# 示例 orchestrator + agent 一起跑

from experiments.registry import ExperimentRegistry
from prompts.prompt_library import PromptLibrary
from llm.llm_client import LLMClient

from brain.selector import StrategySelector, StrategySelectorConfig
from runtime.simple_xgb_runtime import SimpleXGBRuntime
from orchestrator.live_orchestrator import LiveOrchestrator
from execution.agent import ExecutionAgent, ExecutionAgentConfig

# 0) 基础组件
registry = ExperimentRegistry("experiments_store")
prompt_lib = PromptLibrary("prompts/prompt_library.json")
llm = LLMClient(
    model="gpt-5.1-thinking",
    base_url="http://localhost:1234/v1",
)

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

runtime = SimpleXGBRuntime()

orchestrator = LiveOrchestrator(
    registry=registry,
    strategy_selector=selector,
    runtime=runtime,
)

# ExecutionAgent 配置：允许哪些执行 prompt
exec_cfg = ExecutionAgentConfig(
    allowed_prompt_ids=[
        "v3_stable",
        "v3_fast_market_following",
        "v3_fast_market_following_tactical_20251117_v2",
        # ... 你想开放给执行层使用的那些 prompt_id
    ],
    # 下面这些数值建议你后面用数据/回测做校准，现在只是骨架默认
    min_confidence_for_open=0.6,
    max_size_frac=0.3,
)

exec_agent = ExecutionAgent(
    llm=llm,
    prompt_lib=prompt_lib,
    cfg=exec_cfg,
)

# 1) 早盘：生成 SessionPlan
plan = orchestrator.prepare_session(
    symbol="TSLA",
    label_id="ret_10m_log",
    goal="Intraday 10-min direction with strict drawdown control.",
)

print("SessionPlan:", plan.to_dict())

# 2) 盘中：某个时间点，拿到最新 features + market_state + risk_state
import pandas as pd

latest_features = pd.DataFrame(
    # TODO: 用你的特征 pipeline 输出这里替换
    # index 应该是时间戳，columns 是模型需要的 feature 列
)

market_state = {
    "last_price": 250.35,
    "rvol20": 2.7,
    "spread_bp": 3.2,
    # ...
}
risk_state = {
    "account_equity": 100_000,
    "day_pnl": -350,
    "max_day_loss": -1000,
    "current_leverage": 1.3,
    # ...
}

agent_input = orchestrator.build_agent_input(
    plan=plan,
    features=latest_features,
    market_state=market_state,
    risk_state=risk_state,
)

# 3) 调用 ExecutionAgent，让 AI 根据所有信息做“最后一跳”决策
decision = exec_agent.decide(
    execution_prompt_id=plan.execution_prompt_id,
    agent_input=agent_input,
)

print("Final execution decision:", decision)
# 下一步就是你自己的 broker_adapter：
# orders = broker_adapter.build_orders_from_decision(decision, account_state, symbol_lot_size, etc)
# broker_adapter.submit_orders(orders)
```

---

## 4. 现在三层的状态回顾

到目前为止，我们在这三层已经有的东西（都给了代码骨架）：

1. **数据 & 特征 & 标签层**

   * PI → ClickHouse 安全 SQL → DatasetSanity（带 CH 侧统计 + 采样） → DatasetManifest
   * Label 合约（不允许 LLM 随便写 target SQL，统一由 Python 定义）

2. **机器学习 / HPO / 实验治理层**

   * HPO 训练 → `ExperimentRecord` → `ExperimentRegistry`
   * （你那边已经开始落地 Phase 4A：ExperimentRegistry / summary / BrainDecision schema 等）

3. **AI 大脑 & Prompt 安全 & 执行层**

   * StrategySelector：按照白名单 prompt + run_id 选策略
   * LiveOrchestrator：加载模型，生成 SessionPlan，构造给执行 Agent 的输入
   * ExecutionAgent（本轮新加）：用 prompt_library + LLMClient，输出标准化 ExecutionDecision，并有硬校验 + 本地 risk clamp

---

如果你下一步继续说「继续」，我会做下面两条中的**第一条**：

1. 把 **“Broker Adapter + Order DSL”** 的骨架写出来：

   * ExecutionDecision → 规范化的 `OrderRequest` 列表（SIDE, QTY, TYPE, TIF, LIMIT_PRICE 等）
   * 留出位置给你的 IBKR API / 纸交易接口，不碰你的细节，只做抽象层

2. 再回到 **数据/特征层**，把 **Sandbox 特征机制** 写成完整模块：

   * LLM 提出 SandBox SQL 表达式 → 语法/安全检查 → 注册为临时特征 → 通过 DatasetSanity + 回测后晋升为正式特征

你不用选，我会按这个顺序一个一个把坑填完。
