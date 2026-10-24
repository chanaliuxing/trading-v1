好，我们现在把 **BrainDecision（大脑决策层）** 真正落地，这样：

> PI → HPO → ExperimentRegistry →（你现在的位置）→ AI 大脑从历史实验里“挑策略”。

这一轮我做三件事：

1. 定义 `BrainDecision` / `SelectedRun` 数据结构 + 校验逻辑
2. 定义一份 JSON Schema（给 LLM 用的“输出模版”）
3. 给你一个 orchestrator 示例：如何从 LLM 的 JSON 输出变成「已校验的决策」

---

## 1. `brain/decision_types.py`：决策结构 + 校验

> 新建文件：`brain/decision_types.py`
> 或者如果 Claude 已经建了同名文件，就把下面的内容合并进去。

```python
# brain/decision_types.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set


# 允许的角色 + fallback 动作
ALLOWED_ROLES: Set[str] = {"primary", "hedge", "filter"}
ALLOWED_FALLBACK_ACTIONS: Set[str] = {"hold", "disable_trading"}


@dataclass
class SelectedRun:
    """
    单个已训练策略的选择结果。

    - run_id: 必须来自 allowed_run_ids
    - weight: 0~1 之间，多个策略的权重总和 <= 1（由 validate 控制）
    - role: primary / hedge / filter
    - notes: 给人类看的备注，可选
    """
    run_id: str
    weight: float
    role: str = "primary"
    notes: Optional[str] = None

    def validate_basic(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id:
            raise ValueError(f"SelectedRun.run_id must be non-empty str, got {self.run_id!r}")

        if not isinstance(self.weight, (int, float)):
            raise ValueError(f"SelectedRun.weight must be a number, got {type(self.weight)}")

        if not (0.0 <= float(self.weight) <= 1.0):
            raise ValueError(
                f"SelectedRun.weight must be in [0,1], got {self.weight}"
            )

        if self.role not in ALLOWED_ROLES:
            raise ValueError(
                f"SelectedRun.role must be one of {sorted(ALLOWED_ROLES)}, got {self.role!r}"
            )


@dataclass
class BrainDecision:
    """
    AI 大脑对“用哪些历史 run 来交易”的结构化决策。

    schema 设计原则：
    - 尽量小而硬：只负责“选 run + 分配权重 + fallback 动作”；
    - 不在这里直接下单、不在这里写价格或方向，由下一层策略/风控处理；
    - 必须可以在 Python 里严格校验。
    """

    # 本次决策的逻辑分组ID（比如 "tsla_1m_ret10m_v1"），你可以用来做审计
    strategy_set_id: str

    # 选中的 run 列表（0~N个），但在 validate 时会要求：
    # - 至少 1 个
    # - run_id 必须在 allowed_run_ids 中
    # - 权重规则符合约束
    selected: List[SelectedRun] = field(default_factory=list)

    # 当没有任何 run 可以用时的 fallback：
    # - "hold": 不开新仓/不加仓
    # - "disable_trading": 当前组合直接禁止交易（更激进）
    fallback_action: str = "hold"

    # 给人类看的理由（可以是多行字符串）
    rationale: Optional[str] = None

    # schema 版本，方便未来升级
    version: int = 1

    # ================
    # 校验逻辑
    # ================

    def validate(
        self,
        *,
        allowed_run_ids: Sequence[str],
        max_strategies: int = 5,
        require_non_empty: bool = True,
        max_weight_sum: float = 1.0,
    ) -> None:
        """
        对决策做一轮强校验。

        - 所有 run_id 必须属于 allowed_run_ids
        - 不允许超过 max_strategies 个策略
        - 权重必须在 [0,1]，总和 <= max_weight_sum（容忍少量浮点误差）
        - fallback_action 必须在白名单里
        """

        if not isinstance(self.strategy_set_id, str) or not self.strategy_set_id:
            raise ValueError("BrainDecision.strategy_set_id must be non-empty str")

        if self.fallback_action not in ALLOWED_FALLBACK_ACTIONS:
            raise ValueError(
                f"BrainDecision.fallback_action must be one of "
                f"{sorted(ALLOWED_FALLBACK_ACTIONS)}, got {self.fallback_action!r}"
            )

        if not isinstance(allowed_run_ids, (list, tuple, set)):
            raise ValueError("allowed_run_ids must be a sequence of run_id strings")

        allow_set: Set[str] = set(allowed_run_ids)

        if require_non_empty and not self.selected:
            raise ValueError("BrainDecision.selected must not be empty")

        if len(self.selected) > max_strategies:
            raise ValueError(
                f"Too many selected strategies: {len(self.selected)} > {max_strategies}"
            )

        # 校验每个 selected
        seen_run_ids: Set[str] = set()
        weight_sum: float = 0.0

        for s in self.selected:
            s.validate_basic()

            if s.run_id not in allow_set:
                raise ValueError(
                    f"SelectedRun.run_id={s.run_id!r} is not in allowed_run_ids"
                )

            if s.run_id in seen_run_ids:
                raise ValueError(f"Duplicated run_id in BrainDecision: {s.run_id!r}")
            seen_run_ids.add(s.run_id)

            weight_sum += float(s.weight)

        # 允许一点浮点误差
        eps = 1e-6
        if weight_sum - max_weight_sum > eps:
            raise ValueError(
                f"Sum of SelectedRun.weight={weight_sum:.6f} exceeds "
                f"max_weight_sum={max_weight_sum}"
            )


# ================
# JSON 解析 / 构造
# ================

def parse_selected_run(obj: Dict[str, Any]) -> SelectedRun:
    """
    从 LLM 返回的 dict 构建 SelectedRun。
    如果字段缺失或类型错误，抛 ValueError。
    """
    if not isinstance(obj, dict):
        raise ValueError(f"SelectedRun must be a JSON object, got {type(obj)}")

    try:
        run_id = obj["run_id"]
        weight = obj["weight"]
    except KeyError as e:
        raise ValueError(f"SelectedRun missing required field: {e}") from e

    role = obj.get("role", "primary")
    notes = obj.get("notes")

    return SelectedRun(
        run_id=str(run_id),
        weight=float(weight),
        role=str(role),
        notes=str(notes) if notes is not None else None,
    )


def parse_brain_decision(obj: Dict[str, Any]) -> BrainDecision:
    """
    从一个原始 JSON dict 构建 BrainDecision。
    不在这里做 allowed_run_ids 校验，只做结构和基本类型校验。
    """
    if not isinstance(obj, dict):
        raise ValueError(f"BrainDecision must be a JSON object, got {type(obj)}")

    try:
        strategy_set_id = obj["strategy_set_id"]
        selected_raw = obj.get("selected", [])
    except KeyError as e:
        raise ValueError(f"BrainDecision missing required field: {e}") from e

    if not isinstance(selected_raw, list):
        raise ValueError("BrainDecision.selected must be a JSON array")

    selected = [parse_selected_run(item) for item in selected_raw]

    fallback_action = obj.get("fallback_action", "hold")
    rationale = obj.get("rationale")
    version = int(obj.get("version", 1))

    return BrainDecision(
        strategy_set_id=str(strategy_set_id),
        selected=selected,
        fallback_action=str(fallback_action),
        rationale=str(rationale) if rationale is not None else None,
        version=version,
    )
```

到这里为止，你已经有：

* 一套 **强 schema**（dataclass）
* 一套 **JSON → Python 的 parser**
* 一个 `validate()` 钩子，可以用 `allowed_run_ids` 把 LLM “关进笼子里”。

---

## 2. 给 LLM 的 JSON Schema（示例）

为了让 LLM 更听话，你可以在调用时一起传入 schema 描述，比如：

> 新建：`brain/decision_schema.py`

```python
# brain/decision_schema.py

from __future__ import annotations

from typing import Dict, Any

BRAIN_DECISION_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["strategy_set_id", "selected"],
    "properties": {
        "strategy_set_id": {
            "type": "string",
            "description": "Logical id for this portfolio/label/timeframe decision.",
        },
        "selected": {
            "type": "array",
            "description": "List of selected historical strategy runs.",
            "items": {
                "type": "object",
                "required": ["run_id", "weight"],
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "One of the allowed_run_ids provided in context.",
                    },
                    "weight": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                        "description": "Relative weight of this strategy in the ensemble.",
                    },
                    "role": {
                        "type": "string",
                        "enum": ["primary", "hedge", "filter"],
                        "default": "primary",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional human-readable notes.",
                    },
                },
                "additionalProperties": False,
            },
        },
        "fallback_action": {
            "type": "string",
            "enum": ["hold", "disable_trading"],
            "default": "hold",
        },
        "rationale": {
            "type": "string",
            "description": "Short explanation of why these strategies were selected.",
        },
        "version": {
            "type": "integer",
            "default": 1,
        },
    },
    "additionalProperties": False,
}


# 给 prompt 用的简单示例，直接贴到 system_prompt 或 user_prompt 里
BRAIN_DECISION_JSON_EXAMPLE: Dict[str, Any] = {
    "strategy_set_id": "tsla_1m_ret10m_v1",
    "selected": [
        {
            "run_id": "tsla_1m_ret10m_run_20251115_01",
            "weight": 0.7,
            "role": "primary",
            "notes": "Best Sharpe, stable performance.",
        },
        {
            "run_id": "tsla_1m_ret10m_run_20251116_hedge",
            "weight": 0.3,
            "role": "hedge",
            "notes": "Works better in high volatility regime.",
        },
    ],
    "fallback_action": "hold",
    "rationale": "Use the top Sharpe strategy as primary and a high-volatility hedge.",
    "version": 1,
}
```

你在调用 LLM 时，可以把 schema/示例压成一句话加到 prompt 里：

```text
You must respond with a single JSON object matching this schema:

{ ... BRAIN_DECISION_JSON_SCHEMA ... }

Here is an example (do NOT copy run_ids, these are just examples):

{ ... BRAIN_DECISION_JSON_EXAMPLE ... }
```

或者如果你用的是支持 `response_format={"type": "json_schema"}` 的模型，可以直接把上面的 `BRAIN_DECISION_JSON_SCHEMA` 传进去（这一块具体写法跟你用的 SDK 有关，就不展开了）。

---

## 3. Orchestrator 端集成示例（把所有东西串一下）

当你已经有：

* `build_brain_context()`（我们上一轮写的）
* `BrainDecision` + `parse_brain_decision()`
* `ExperimentRegistry` + `PromptLibrary`

你在 orchestrator 里，大概这样用：

```python
# somewhere in your orchestrator

from brain.prompt_library import PromptLibrary
from brain.context_builder import build_brain_context
from brain.decision_types import parse_brain_decision
from experiments.registry import ExperimentRegistry
from llm.llm_client import LLMClient  # 你现有的封装

prompt_lib = PromptLibrary.from_file("config/prompt_library.json")
exp_registry = ExperimentRegistry.from_path("experiments/records.jsonl")
llm = LLMClient(model="gpt-4.1-mini")  # 举例

def brain_select_strategies(
    symbol: str,
    timeframe: str,
    label_id: str,
    brain_mode_id: str,
    sanity_report: dict | None,
) -> "BrainDecision":
    # 1. 构造大脑上下文
    ctx = build_brain_context(
        symbol=symbol,
        timeframe=timeframe,
        label_id=label_id,
        brain_mode_id=brain_mode_id,
        prompt_library=prompt_lib,
        experiment_registry=exp_registry,
        sanity_report=sanity_report,
        max_history_runs=20,
    )

    # 2. 调 LLM（伪代码：你的 LLMClient 可能不同）
    raw_json = llm.generate_json(
        system_prompt=ctx["system_prompt"],
        user_prompt=ctx["user_prompt"],
        # 可选：把 schema 传进去，视具体 SDK 而定
        # schema=BRAIN_DECISION_JSON_SCHEMA,
    )

    # 3. 结构化解析
    decision = parse_brain_decision(raw_json)

    # 4. 强校验（把 allowed_run_ids 关进笼子里）
    decision.validate(
        allowed_run_ids=ctx["allowed_run_ids"],
        max_strategies=5,
        max_weight_sum=1.0,
    )

    return decision
```

> 从这一刻起：
> **LLM 再也不能乱编 run_id、乱给权重**——所有东西都要通过 `decision.validate()`，否则这次策略选择直接 fail，不会落到交易层。

---

## 下一步可以做什么？

你刚刚问的是“三层都继续落地”，这一轮我们主要推进的是：

3. **AI 大脑 & Prompt 安全层**

现在这层已经具备：

* ✅ Prompt 白名单
* ✅ 实验汇总 & allowed_run_ids
* ✅ BrainDecision schema + 解析 + 校验

接下来两个方向都可以进一步深入：

1. **往下接：让 BrainDecision 真正驱动模型加载 & 信号组合**

   * 写一个 `StrategyRuntime` / `ModelLoader`：

     * 输入：`run_id` 列表；
     * 从磁盘/注册表里加载对应的模型 & feature config；
     * 形成一个「多模型集成器」，根据 `weight` 做加权预测；
   * 在 live/paper orchestrator 中用 BrainDecision 的 `selected` 来决定：今天启用哪几个 run。

2. **往下补：数据 / 特征 / 标签层继续加强**

   * 把 DatasetSanity 的 ClickHouse 版本（`compute_nans/var/corr` 下沉到 SQL）落地；
   * 把 sandbox 特征的「提名 → 计算 → 验证 → 提升为正式特征」流程写成一个 service。

你现在已经让「AI 大脑」从“纯 prompt 胡说”升级成了一个：

> **只能在「历史实验 + prompt 白名单 + run_id 白名单」这三重笼子里活动的策略 selector**。

如果你愿意，下一步我可以专门帮你设计：

* `StrategyRuntime` 的接口（如何根据 run_id 找到模型文件、特征配方、label 定义）
* 多模型集成（stacking/加权平均）怎么跟 BrainDecision 的 `weight` 对接

你只要回一句：**“继续，重点放在 runtime/model loader”** 或 **“继续，先去优化 DatasetSanity 到 ClickHouse”**，我就按其中一个方向往下挖。
好，我们继续，把 **“AI 大脑选出来的 run 真正跑起来”** 这一段落地，也就是：

> PI → HPO → ExperimentRegistry → **BrainDecision → StrategyRuntime/ModelLoader → 信号输出**

这一轮我做三块：

1. 设计策略产物描述（StrategyArtifact）+ 注册索引（ArtifactRegistry）
2. 设计模型加载/运行层（StrategyRuntime / ModelLoader）
3. 把 `BrainDecision` 接到 runtime，给你一个完整的“从 run_id 到 ensemble 预测”的骨架

你这边只需要在合适位置把这些文件/类落地，模型加载细节按你现有框架补齐即可。

---

## 1️⃣ 策略产物 & 索引：`runtime/artifacts.py`

目标：**把 “run_id → 这次训练产出的模型/特征/标签信息” 固化下来**，便于运行层按 run_id 找东西。

> 新建模块：`runtime/artifacts.py`

```python
# runtime/artifacts.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class StrategyArtifact:
    """
    一个训练完成的策略产物描述（metadata）。

    注意：这里不直接放 model 对象，只放路径和关键 metadata。
    真正加载发生在 ModelRuntime 里。
    """
    run_id: str                     # 与 ExperimentRegistry 里的一致
    symbol: str                     # 例如 "TSLA"
    timeframe: str                  # 例如 "1m"
    label_id: str                   # 对应你的 LabelContract/配置
    task: str                       # "reg" / "clf" / "rank" 等
    model_type: str                 # "xgb" / "lgbm" / "sklinear" / "skrf" / "torch" ...
    model_path: str                 # 模型文件路径（相对 or 绝对）
    feature_list: List[str]         # 模型训练时用到的特征名列表
    target_col: str                 # 训练时的标签列名，如 "ret_10m_fwd"
    scaler_path: Optional[str] = None    # 标准化/归一化器路径（如有）
    extra: Dict[str, Any] = None         # 其他 metadata（例如 params, seed, train_period）

    def model_abspath(self, base_dir: Path) -> Path:
        return (base_dir / self.model_path).resolve()

    def scaler_abspath(self, base_dir: Path) -> Optional[Path]:
        if not self.scaler_path:
            return None
        return (base_dir / self.scaler_path).resolve()


class ArtifactRegistry:
    """
    简单的跑时索引：run_id -> StrategyArtifact

    你可以：
    - 从 JSON/JSONL 建这个 registry
    - 或者从 ExperimentRegistry 那边导出一份 artifact_index.json
    """

    def __init__(self, artifacts: Dict[str, StrategyArtifact], base_dir: Path) -> None:
        self._artifacts = artifacts
        self._base_dir = base_dir

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def get(self, run_id: str) -> StrategyArtifact:
        try:
            return self._artifacts[run_id]
        except KeyError as e:
            raise KeyError(f"StrategyArtifact not found for run_id={run_id!r}") from e

    @classmethod
    def from_json(cls, data: Dict[str, Any], base_dir: str | Path) -> "ArtifactRegistry":
        """
        约定简单 JSON 结构：

        {
          "base_dir": "models/",    # 可选, 若有则覆盖 base_dir 参数
          "artifacts": [
            {
              "run_id": "...",
              "symbol": "TSLA",
              "timeframe": "1m",
              "label_id": "ret10m",
              "task": "reg",
              "model_type": "xgb",
              "model_path": "xgb/tsla_1m_ret10m_20251116.model",
              "feature_list": ["close", "vwap", "rsi_14", ...],
              "target_col": "ret_10m_fwd",
              "scaler_path": "scalers/tsla_1m_ret10m_20251116.pkl",
              "extra": { ... }
            },
            ...
          ]
        }
        """
        base_dir = Path(data.get("base_dir", base_dir))
        artifacts_by_id: Dict[str, StrategyArtifact] = {}

        for item in data.get("artifacts", []):
            art = StrategyArtifact(
                run_id=item["run_id"],
                symbol=item["symbol"],
                timeframe=item["timeframe"],
                label_id=item["label_id"],
                task=item["task"],
                model_type=item["model_type"],
                model_path=item["model_path"],
                feature_list=list(item["feature_list"]),
                target_col=item["target_col"],
                scaler_path=item.get("scaler_path"),
                extra=item.get("extra") or {},
            )
            artifacts_by_id[art.run_id] = art

        return cls(artifacts=artifacts_by_id, base_dir=base_dir)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "ArtifactRegistry":
        import json

        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        base_dir = data.get("base_dir", p.parent)
        return cls.from_json(data=data, base_dir=base_dir)
```

🔧 **TODO（你这边）**：

* 在 HPO / 训练完成的时候，把 `ExperimentRecord` 里关于模型文件的路径、特征列表等信息导出成一份 `artifact_index.json`。
* 最简单可以是一个小脚本，从 `experiments/records.jsonl` 里读数据，筛选 `status == "completed"` 的 run，写成上面这种结构。

---

## 2️⃣ 模型加载 & 运行：`runtime/model_runtime.py`

这里我们抽象出两个层次：

1. **ModelLoader**：根据 `StrategyArtifact` 的 `model_type` 和 `model_path` 具体加载模型对象
2. **StrategyRuntime**：封装 “特征对齐 → 预处理 → 调模型 → 输出 y_hat” 的逻辑

> 新建模块：`runtime/model_runtime.py`

```python
# runtime/model_runtime.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from runtime.artifacts import StrategyArtifact, ArtifactRegistry


# ================
# 单个策略模型的运行包装
# ================

@dataclass
class LoadedStrategy:
    """
    已加载到内存的策略模型，提供一个统一的 predict 接口。
    """

    artifact: StrategyArtifact
    model: Any                       # 真正的模型对象（xgb.Booster / sklearn estimator / torch nn.Module ...）
    scaler: Any | None               # 例如 sklearn 的 StandardScaler / MinMaxScaler
    feature_list: List[str]

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        """
        df: 必须至少包含 feature_list 的列。

        返回：一维 numpy 数组，长度与 df 行数一致。
        """
        missing = [c for c in self.feature_list if c not in df.columns]
        if missing:
            raise ValueError(
                f"DataFrame missing required features for run_id={self.artifact.run_id!r}: {missing}"
            )

        X = df[self.feature_list].astype(float).to_numpy()

        if self.scaler is not None:
            X = self.scaler.transform(X)

        # 根据不同模型类型调用不同的 predict 接口
        mt = self.artifact.model_type.lower()

        if mt in {"xgb", "xgboost"}:
            # 假设 model 是 xgboost.Booster 或 sklearn.XGBRegressor
            # 你可以根据自己实际封装做细化，这里写一个宽松版本
            if hasattr(self.model, "predict"):
                y = self.model.predict(X)
            else:
                raise TypeError(f"XGB model for run_id={self.artifact.run_id!r} has no predict()")
        elif mt in {"lgbm", "lightgbm"}:
            if hasattr(self.model, "predict"):
                y = self.model.predict(X)
            else:
                raise TypeError(f"LGBM model for run_id={self.artifact.run_id!r} has no predict()")
        elif mt.startswith("sk"):
            # sklearn 家族 ("sklinear", "skrf" 等)
            if hasattr(self.model, "predict_proba") and self.artifact.task == "clf":
                # 二分类时取正类概率
                proba = self.model.predict_proba(X)
                if proba.shape[1] == 2:
                    y = proba[:, 1]
                else:
                    # 多分类情况你可以自己扩展
                    y = proba.max(axis=1)
            elif hasattr(self.model, "predict"):
                y = self.model.predict(X)
            else:
                raise TypeError(
                    f"SK model for run_id={self.artifact.run_id!r} has neither predict_proba nor predict"
                )
        elif mt in {"torch", "pytorch"}:
            # 占位：你自己的 torch 推理代码
            raise NotImplementedError(
                f"PyTorch runtime not implemented yet for run_id={self.artifact.run_id!r}"
            )
        else:
            raise ValueError(
                f"Unknown model_type={self.artifact.model_type!r} for run_id={self.artifact.run_id!r}"
            )

        return np.asarray(y).reshape(-1)


# ================
# ModelLoader：从磁盘加载模型 + scaler
# ================

class ModelLoader:
    """
    负责根据 StrategyArtifact 加载实际模型 & 预处理器。

    这里给出一个简单的基于 joblib 的默认实现。
    你可以根据真实情况（xgboost 原生 Booster / torch / onnx 等）扩展。
    """

    def __init__(self, registry: ArtifactRegistry) -> None:
        self._reg = registry

    def load_strategy(self, run_id: str) -> LoadedStrategy:
        art = self._reg.get(run_id)
        base_dir = self._reg.base_dir

        model_path = art.model_abspath(base_dir)
        scaler_path = art.scaler_abspath(base_dir)

        # ----- 加载模型 -----
        model = self._load_model_generic(model_path, art)

        # ----- 加载 scaler（如有） -----
        scaler = None
        if scaler_path is not None:
            scaler = self._load_scaler_generic(scaler_path, art)

        return LoadedStrategy(
            artifact=art,
            model=model,
            scaler=scaler,
            feature_list=art.feature_list,
        )

    # ===== 下面是默认实现，你可按实际情况替换 =====

    def _load_model_generic(self, path: Path, art: StrategyArtifact) -> Any:
        import joblib

        if not path.exists():
            raise FileNotFoundError(f"Model file not found for run_id={art.run_id!r}: {path}")

        # 这里假设你训练时都是 joblib.dump() 存的
        # 如果 xgboost / lgbm / torch 用自己的保存格式，你可以在这里做分支
        return joblib.load(path)

    def _load_scaler_generic(self, path: Path, art: StrategyArtifact) -> Any:
        import joblib

        if not path.exists():
            raise FileNotFoundError(
                f"Scaler file not found for run_id={art.run_id!r}: {path}"
            )
        return joblib.load(path)


# ================
# EnsembleRuntime：用 BrainDecision 组合多个策略
# ================

class EnsembleRuntime:
    """
    根据 BrainDecision 中的多个 run_id + 权重，组合成一个 ensemble 信号。

    这里先实现最简单的线性加权 y_hat，再预留 hook 给更复杂逻辑。
    """

    def __init__(self, model_loader: ModelLoader) -> None:
        self._loader = model_loader

    def predict_ensemble(
        self,
        decision,          # BrainDecision 对象
        df: pd.DataFrame,
    ) -> Dict[str, Any]:
        """
        返回结构：
        {
          "y_raw": ndarray,         # 加权后的连续预测值（如 ret_10m 预测）
          "per_run": {run_id: ndarray},  # 每个 run 的单独预测
          "meta": {...}            # 一些元数据
        }
        """
        import numpy as np

        if not decision.selected:
            raise ValueError("EnsembleRuntime.predict_ensemble: decision.selected is empty.")

        per_run: Dict[str, np.ndarray] = {}
        weighted_sum: Optional[np.ndarray] = None
        weight_sum: float = 0.0

        for sel in decision.selected:
            run_id = sel.run_id
            weight = float(sel.weight)

            strat = self._loader.load_strategy(run_id)  # LoadedStrategy
            y = strat.predict(df)

            per_run[run_id] = y

            if weighted_sum is None:
                weighted_sum = weight * y
            else:
                weighted_sum += weight * y

            weight_sum += weight

        if weighted_sum is None:
            raise RuntimeError("No predictions produced, this should not happen.")

        # 如果权重和 < 1，可以选择是否归一化（这里保持“按给定权重线性组合”的语义，不强制归一化）
        y_raw = weighted_sum

        return {
            "y_raw": y_raw,
            "per_run": per_run,
            "meta": {
                "weight_sum": weight_sum,
                "strategy_set_id": decision.strategy_set_id,
                "selected_runs": [s.run_id for s in decision.selected],
                "fallback_action": decision.fallback_action,
                "rationale": decision.rationale,
            },
        }
```

🔧 **TODO（你这边）**：

1. 把训练时保存模型的方式跟这里对齐：如果你现在是 `xgb.Booster().save_model()` 那就需要在 `_load_model_generic` 里写 xgboost 专用 loader。
2. 如果你有 Torch 模型，补上 `elif mt in {"torch", ...}` 分支。
3. 确认 `StrategyArtifact.feature_list` 对应的是训练时真实用到的特征顺序。

---

## 3️⃣ 把 BrainDecision 接到 Runtime：完整调用链示例

现在你有：

* `BrainDecision` / `parse_brain_decision`（上一轮）
* `ArtifactRegistry` / `ModelLoader` / `EnsembleRuntime`（本轮）
* `ExperimentRegistry` / `PromptLibrary` / `build_brain_context`（前面几轮）

我们拼一个“从决策到预测”的 orchestrator 示例：

```python
# orchestrator/brain_strategy_executor.py

from __future__ import annotations

from typing import Dict, Any

import pandas as pd

from brain.decision_types import parse_brain_decision, BrainDecision
from brain.context_builder import build_brain_context   # 你之前的
from brain.prompt_library import PromptLibrary
from experiments.registry import ExperimentRegistry
from runtime.artifacts import ArtifactRegistry
from runtime.model_runtime import ModelLoader, EnsembleRuntime
from llm.llm_client import LLMClient


class BrainStrategyExecutor:
    """
    串起：
      - ExperimentRegistry（历史实验）
      - ArtifactRegistry（run_id -> 模型文件）
      - PromptLibrary（prompt 白名单）
      - LLM（做 BrainDecision）
      - EnsembleRuntime（根据 BrainDecision 调模型）
    """

    def __init__(
        self,
        *,
        exp_registry: ExperimentRegistry,
        artifact_registry: ArtifactRegistry,
        prompt_library: PromptLibrary,
        llm: LLMClient,
    ) -> None:
        self._exp_reg = exp_registry
        self._art_reg = artifact_registry
        self._prompt_lib = prompt_library
        self._llm = llm

        self._model_loader = ModelLoader(artifact_registry)
        self._ensemble = EnsembleRuntime(self._model_loader)

    # ================
    # 第一步：向大脑要 BrainDecision
    # ================

    def request_brain_decision(
        self,
        *,
        symbol: str,
        timeframe: str,
        label_id: str,
        brain_mode_id: str,
        sanity_report: Dict[str, Any] | None = None,
    ) -> BrainDecision:
        ctx = build_brain_context(
            symbol=symbol,
            timeframe=timeframe,
            label_id=label_id,
            brain_mode_id=brain_mode_id,
            prompt_library=self._prompt_lib,
            experiment_registry=self._exp_reg,
            sanity_report=sanity_report,
            max_history_runs=20,
        )

        # 这里假设 LLMClient 有一个 generate_json()，直接返回 dict
        raw_json = self._llm.generate_json(
            system_prompt=ctx["system_prompt"],
            user_prompt=ctx["user_prompt"],
            # 如果你的 LLM 支持 json_schema，可以在这里传入 BRAIN_DECISION_JSON_SCHEMA
        )

        decision = parse_brain_decision(raw_json)

        # 把 allowed_run_ids 关进笼子里（防注入）
        decision.validate(
            allowed_run_ids=ctx["allowed_run_ids"],
            max_strategies=5,
            max_weight_sum=1.0,
        )
        return decision

    # ================
    # 第二步：根据 BrainDecision 运行 ensemble 预测
    # ================

    def run_ensemble_prediction(
        self,
        *,
        decision: BrainDecision,
        df_features: pd.DataFrame,
    ) -> Dict[str, Any]:
        """
        df_features: 已经过特征工程的特征矩阵（含所有可能需要的特征列）。
        """
        return self._ensemble.predict_ensemble(decision, df_features)
```

这样，在你的 live / paper orchestration 代码里，只要：

```python
executor = BrainStrategyExecutor(
    exp_registry=ExperimentRegistry.from_path("experiments/records.jsonl"),
    artifact_registry=ArtifactRegistry.from_json_file("experiments/artifact_index.json"),
    prompt_library=PromptLibrary.from_file("config/prompt_library.json"),
    llm=LLMClient(...),
)

# 1. 向大脑要一个 BrainDecision（今天 TSLA 1m 用哪些 run）
decision = executor.request_brain_decision(
    symbol="TSLA",
    timeframe="1m",
    label_id="ret10m",
    brain_mode_id="v3_fast_market_following_tactical_20251117",
    sanity_report=current_sanity_report,   # 如果你有 DatasetSanity 结果
)

# 2. 对一批 feature 行做 ensemble 预测
result = executor.run_ensemble_prediction(
    decision=decision,
    df_features=tsla_feature_df,  # 例如 1 分钟 bars 的 rolling features
)

y_raw = result["y_raw"]          # ensemble 预测
per_run = result["per_run"]      # 每个 run 的单独预测，可做分析/打分
meta = result["meta"]
```

> ✅ 至此，**从 run_id 到真实模型预测的闭环就打通了**，AI 大脑不再“口嗨”，而是：
>
> * 只能在你给出的 run_id 空间里做选择；
> * 选择出来的 run 会被 runtime 真正加载模型并输出数值信号；
> * 上一层的风控/执行逻辑可以继续在 y_raw 上做 gating（ATR、RVOL、spread 等）。

---

## 小结 & 下一步建议

现在三层的状态大致是：

1. **数据 / 特征 / 标签层**

   * 有 DatasetSanity（DataFrame 版），下一步可以继续做 ClickHouse 侧的统计下沉 & sandbox 特征。

2. **ML / HPO / 实验治理层**

   * PI → HPO → ExperimentRegistry → ArtifactRegistry → ModelLoader 已经有完整骨架；
   * 你只需要让 HPO 写出 `ExperimentRecord + artifact_index.json` 即可。

3. **AI 大脑 & Prompt 安全层**

   * Prompt 白名单、BrainContext、BrainDecision schema、安全校验已经就位；
   * 现在已经可以通过 BrainDecision 驱动真实的 ensemble 预测。

👉 下一步我建议：

* 要么：**回到数据层**，把 DatasetSanity 的 ClickHouse 版落地（真正解决 data corruption 性能问题）；
* 要么：**在这套 runtime 上面加一层规则**，把 y_raw 转为 “开仓/平仓/加减仓” 决策（结合已有的 risk gate）。

你可以继续回一句「继续，先搞 ClickHouse 版 DatasetSanity」或者「继续，把 y_raw → trade action 的规则层写出来」，我就按其中一个方向接着往下落地。
好，我们就按你之前定的方向，把 **DatasetSanity 性能 + 可靠性** 真正升级一版：

* ✅ 把“重”统计（行数、缺失率、分布粗略形状）下沉到 ClickHouse 做；
* ✅ Python 侧只拿一小撮样本做“深度体检”（相关性、共线性、异常值）；
* ✅ 给 PI/HPO 一个统一入口：**训练前先体检，不过关就不训练**。

我这次给你三块落地代码骨架（你可以直接建文件）：

1. `data/ch_sanity.py`：ClickHouse 侧统计
2. 升级版 `data/dataset_sanity.py`：支持传入 ClickHouse stats + 采样
3. 示例：在 PI/HPO pipeline 里如何调用 “CH+采样体检”

---

## 1️⃣ ClickHouse 侧统计模块：`data/ch_sanity.py`

> 新文件：`data/ch_sanity.py`
> 职责：在 ClickHouse 里对指定列做快速汇总统计，针对某个 symbol + 时间窗。

```python
# data/ch_sanity.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect


@dataclass
class CHColumnStats:
    name: str
    n_total: int
    n_null: int
    mean: Optional[float]
    stddev: Optional[float]
    v_min: Optional[float]
    v_max: Optional[float]
    p50: Optional[float]
    p99: Optional[float]

    @property
    def null_ratio(self) -> float:
        if self.n_total == 0:
            return 0.0
        return self.n_null / float(self.n_total)


@dataclass
class CHSanitySummary:
    table: str
    symbol: Optional[str]
    start_ts: Optional[str]
    end_ts: Optional[str]
    stats: List[CHColumnStats]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "symbol": self.symbol,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "stats": [s.__dict__ for s in self.stats],
        }


def _build_where_clause(
    *,
    symbol: Optional[str],
    start_ts: Optional[str],
    end_ts: Optional[str],
    symbol_col: str,
    ts_col: str,
) -> str:
    conds: List[str] = []
    if symbol is not None:
        # 注意这里用占位符参数来绑定 symbol 值，而不是拼字符串
        conds.append(f"{symbol_col} = {{symbol:String}}")
    if start_ts is not None:
        conds.append(f"{ts_col} >= toDateTime({{start_ts:String}})")
    if end_ts is not None:
        conds.append(f"{ts_col} <= toDateTime({{end_ts:String}})")
    if not conds:
        return ""
    return "WHERE " + " AND ".join(conds)


def fetch_column_stats_clickhouse(
    client: clickhouse_connect.driver.client.Client,
    *,
    table: str,
    columns: List[str],
    symbol: Optional[str] = None,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    symbol_col: str = "symbol",
    ts_col: str = "ts",
) -> CHSanitySummary:
    """
    在 ClickHouse 端对每一列做聚合统计，避免把几百万行全拖到 Python。

    返回 CHSanitySummary，可作为 DatasetSanity 的“先验 hint”，
    比如快速发现：
      - 空值率很高的列
      - 完全常数列（min == max）
      - 极端数值（max/p99 特别大）
    """
    stats: List[CHColumnStats] = []

    where_clause = _build_where_clause(
        symbol=symbol, start_ts=start_ts, end_ts=end_ts, symbol_col=symbol_col, ts_col=ts_col
    )

    params: Dict[str, Any] = {}
    if symbol is not None:
        params["symbol"] = symbol
    if start_ts is not None:
        params["start_ts"] = start_ts
    if end_ts is not None:
        params["end_ts"] = end_ts

    # 对每一列单独跑一次聚合查询，简单可靠，性能也足够（全是聚合，IO 开销小）
    for col in columns:
        # 这里只针对数值型列调用 avg/stddev/quantile
        # 如果传的是非数值（如字符串），ClickHouse 会报错。
        # 调用前建议用 system.columns 做一次类型过滤（后面可以加一个 helper）。
        q = f"""
        SELECT
          count()                                AS n_total,
          countIf({col} IS NULL)                 AS n_null,
          avg({col})                             AS mean,
          stddevPop({col})                       AS stddev,
          min({col})                             AS v_min,
          max({col})                             AS v_max,
          quantile(0.5)({col})                   AS p50,
          quantile(0.99)({col})                  AS p99
        FROM {table}
        {where_clause}
        """

        df = client.query_df(q, parameters=params)

        if df.empty:
            stats.append(
                CHColumnStats(
                    name=col,
                    n_total=0,
                    n_null=0,
                    mean=None,
                    stddev=None,
                    v_min=None,
                    v_max=None,
                    p50=None,
                    p99=None,
                )
            )
            continue

        row = df.iloc[0]
        stats.append(
            CHColumnStats(
                name=col,
                n_total=int(row["n_total"]),
                n_null=int(row["n_null"]),
                mean=float(row["mean"]) if row["mean"] is not None else None,
                stddev=float(row["stddev"]) if row["stddev"] is not None else None,
                v_min=float(row["v_min"]) if row["v_min"] is not None else None,
                v_max=float(row["v_max"]) if row["v_max"] is not None else None,
                p50=float(row["p50"]) if row["p50"] is not None else None,
                p99=float(row["p99"]) if row["p99"] is not None else None,
            )
        )

    return CHSanitySummary(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        stats=stats,
    )
```

> ⚠️ 小提醒：
>
> * 这里假设传进来的 `columns` 已经是数值型列（`Float64`/`Int64` 等），否则 `avg(col)` / `stddevPop` 会报错。
> * 你可以在别的 helper 里用 `system.columns` 过滤一次（后面如果你想，我可以帮你写 `get_numeric_columns(table, feature_list)`）。

---

## 2️⃣ 升级 Python 侧 DatasetSanity：支持 ClickHouse 统计 + 采样

> 我们对之前的 `run_dataset_sanity_checks` 做两件事：

1. 增加 `max_rows_for_checks`，自动对大表抽样
2. 接收 `ch_summary: CHSanitySummary | None`，把 CH 端的统计融合进报告

新版本（可以直接覆盖你现在的 `data/dataset_sanity.py`，如果你愿意的话）：

```python
# data/dataset_sanity.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from data.ch_sanity import CHSanitySummary, CHColumnStats


@dataclass
class ColumnIssue:
    column: str
    message: str
    severity: str  # "info" | "warn" | "error"
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasetSanityReport:
    n_rows: int
    n_cols: int
    column_issues: List[ColumnIssue] = field(default_factory=list)
    global_warnings: List[str] = field(default_factory=list)
    ch_summary: Optional[CHSanitySummary] = None
    sample_used: bool = False
    sample_frac: float = 1.0

    @property
    def ok(self) -> bool:
        # 简单规则：没有 severity == "error" 的 issue，则认为体检通过
        return all(issue.severity != "error" for issue in self.column_issues)

    def add_issue(
        self,
        column: str,
        message: str,
        severity: str = "warn",
        **details: Any,
    ) -> None:
        self.column_issues.append(
            ColumnIssue(column=column, message=message, severity=severity, details=details)
        )

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_cols": self.n_cols,
            "ok": self.ok,
            "sample_used": self.sample_used,
            "sample_frac": self.sample_frac,
            "global_warnings": self.global_warnings,
            "column_issues": [ci.__dict__ for ci in self.column_issues],
            "ch_summary": self.ch_summary.to_dict() if self.ch_summary else None,
        }


def _detect_basic_issues(
    df: pd.DataFrame,
    report: DatasetSanityReport,
    *,
    null_ratio_threshold: float = 0.2,
) -> None:
    """
    基础体检：空值、常数列、无穷大、全零等。
    """
    n_rows = len(df)

    for col in df.columns:
        s = df[col]
        if not np.issubdtype(s.dtype, np.number):
            # 暂时只检查数值列；分类/字符串列可以之后扩展
            continue

        n_null = int(s.isna().sum())
        null_ratio = n_null / n_rows if n_rows > 0 else 0.0

        if n_null > 0 and null_ratio > 0:
            sev = "warn" if null_ratio <= null_ratio_threshold else "error"
            report.add_issue(
                column=col,
                message="High null ratio",
                severity=sev,
                n_null=n_null,
                null_ratio=round(null_ratio, 4),
            )

        # 常数列
        nunique = int(s.nunique(dropna=True))
        if nunique <= 1 and n_rows > 0:
            report.add_issue(
                column=col,
                message="Constant or near-constant column",
                severity="warn",
                nunique=nunique,
            )

        # 无穷大
        n_inf = int(np.isinf(s.to_numpy()).sum())
        if n_inf > 0:
            report.add_issue(
                column=col,
                message="Contains inf or -inf",
                severity="error",
                n_inf=n_inf,
            )


def _detect_correlation_issues(
    df: pd.DataFrame,
    report: DatasetSanityReport,
    *,
    corr_threshold: float = 0.98,
    max_cols_for_corr: int = 60,
) -> None:
    """
    共线性检查：只在采样后的 df 上做，列数过多会自动截断。

    - 只对数值列计算 Pearson 相关系数
    - abs(corr) > corr_threshold 的列对，标为 warn
    """
    numeric_cols = [c for c in df.columns if np.issubdtype(df[c].dtype, np.number)]
    if len(numeric_cols) < 2:
        return

    if len(numeric_cols) > max_cols_for_corr:
        # 截断 + 给一条全局 warning
        report.global_warnings.append(
            f"Too many numeric columns ({len(numeric_cols)}); "
            f"limiting correlation check to first {max_cols_for_corr} columns."
        )
        numeric_cols = numeric_cols[:max_cols_for_corr]

    sub = df[numeric_cols]
    corr = sub.corr(method="pearson")

    # 找出 abs(corr) > 阈值 & 非对角
    cols = corr.columns
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c1, c2 = cols[i], cols[j]
            v = corr.iloc[i, j]
            if np.isnan(v):
                continue
            if abs(v) >= corr_threshold:
                msg = f"High correlation between {c1} and {c2}: {v:.4f}"
                report.add_issue(
                    column=c1,
                    message=msg,
                    severity="warn",
                    other_col=c2,
                    corr=float(v),
                )


def _merge_ch_stats(
    ch_summary: Optional[CHSanitySummary],
    report: DatasetSanityReport,
    *,
    null_ratio_error: float = 0.5,
) -> None:
    """
    把 ClickHouse 端统计合并到报告中，用于快速发“粗粒度报警”。
    """
    if ch_summary is None:
        return

    for s in ch_summary.stats:
        # 空值过高
        if s.n_total > 0 and s.null_ratio >= null_ratio_error:
            report.add_issue(
                column=s.name,
                message="Null ratio >= hard threshold at ClickHouse level",
                severity="error",
                n_total=s.n_total,
                n_null=s.n_null,
                null_ratio=round(s.null_ratio, 4),
            )

        # 明显常数列（min == max 且有足够行数）
        if (
            s.v_min is not None
            and s.v_max is not None
            and s.n_total >= 100
            and abs(s.v_max - s.v_min) < 1e-12
        ):
            report.add_issue(
                column=s.name,
                message="Constant column detected from ClickHouse stats",
                severity="warn",
                v_min=s.v_min,
                v_max=s.v_max,
            )

        # 数值极端（只是 warn，由你在上层决定是否过滤）
        if s.p99 is not None and s.p50 is not None:
            if abs(s.p99) > 1000 * max(1.0, abs(s.p50)):
                report.add_issue(
                    column=s.name,
                    message="Heavy tail / extreme outliers (p99 >> p50) from ClickHouse stats",
                    severity="warn",
                    p50=s.p50,
                    p99=s.p99,
                )


def run_dataset_sanity_checks(
    df: pd.DataFrame,
    *,
    ch_summary: Optional[CHSanitySummary] = None,
    max_rows_for_checks: int = 50_000,
    null_ratio_threshold: float = 0.2,
    corr_threshold: float = 0.98,
    max_cols_for_corr: int = 60,
) -> DatasetSanityReport:
    """
    升级版 DatasetSanity：

    - 对超大表自动采样（默认最多 5 万行）
    - 结合 ClickHouse 端的统计结果（如有）
    - 在 Python 端只做“精细检查”（空值、常数列、无穷大、相关性）

    NOTE:
      - 报告中的 n_rows/n_cols 是原始 df 的形状；
      - sample_used 标记是否真的采样；
      - 上层可以用 report.ok 决定“是否允许继续训练/HPO”。
    """
    n_rows, n_cols = df.shape
    report = DatasetSanityReport(
        n_rows=n_rows,
        n_cols=n_cols,
        ch_summary=ch_summary,
        sample_used=False,
        sample_frac=1.0,
    )

    # 先用 ClickHouse 统计做一次粗过滤（高空视角）
    _merge_ch_stats(ch_summary, report)

    # 下面的检查统一在“采样后的 df_sample”上进行
    if n_rows > max_rows_for_checks:
        frac = max_rows_for_checks / float(n_rows)
        df_sample = df.sample(
            n=max_rows_for_checks,
            random_state=42,
        )
        report.sample_used = True
        report.sample_frac = frac
        report.global_warnings.append(
            f"Dataset has {n_rows} rows; sampled {max_rows_for_checks} rows for sanity checks."
        )
    else:
        df_sample = df

    # 基础检查：NaN / inf / 常数列
    _detect_basic_issues(
        df_sample,
        report,
        null_ratio_threshold=null_ratio_threshold,
    )

    # 相关性 / 共线性
    _detect_correlation_issues(
        df_sample,
        report,
        corr_threshold=corr_threshold,
        max_cols_for_corr=max_cols_for_corr,
    )

    return report
```

---

## 3️⃣ 在 PI / HPO pipeline 里使用 CH + 采样体检

最后我们给一个「**从 PI 出 sql → ClickHouse 拉数据 → 体检 → 决定是否交给 HPO**」的示例函数。你可以放在类似 `pi/pi_training_orchestrator.py` 里。

> 关键点：
>
> * ClickHouse 端先做快速统计（`fetch_column_stats_clickhouse`）
> * 拉整体训练集到 Python（或已经你原本的行为）；
> * 体检用采样 + CH stats；
> * `report.ok == False` 时直接中止本次训练，避免浪费 Optuna/HPO 算力。

```python
# pi/pi_training_orchestrator.py  （示例文件名）

from __future__ import annotations

from typing import Any, Dict, List

import clickhouse_connect
import pandas as pd

from data.ch_sanity import fetch_column_stats_clickhouse
from data.dataset_sanity import run_dataset_sanity_checks, DatasetSanityReport
from pi.principal_investigator import PrincipalInvestigator, PIResult
from experiments.registry import ExperimentRegistry  # 假设你已有


class PITrainingOrchestrator:
    """
    把 PI → ClickHouse → DatasetSanity → HPO 连起来的一个简单 orchestrator 示例。

    注意：这里不会真正跑 HPO，只演示到“体检通过后把 df 和 hpo_grid 交出去”这一步。
    """

    def __init__(
        self,
        *,
        ch_cfg: Dict[str, Any],
        llm_cfg: Dict[str, Any],
    ) -> None:
        self._ch_cfg = ch_cfg
        self._client = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=ch_cfg.get("port", 8123),
            username=ch_cfg.get("username", ch_cfg.get("user", "default")),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "default"),
        )
        self._pi = PrincipalInvestigator(ch_cfg=ch_cfg, llm_cfg=llm_cfg)

    def propose_and_load_dataset(
        self,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        goal: str,
        horizon: str,
        numeric_feature_cols: List[str],
    ) -> tuple[PIResult, DatasetSanityReport]:
        """
        1. 调用 PI.propose() 让 AI 设计 feature_recipe + training_plan + HPO grid；
        2. 用 ClickHouse 做 numeric_feature_cols 的快速统计（CHSanitySummary）；
        3. 对 PIResult.dataset_df 做 DatasetSanity（带采样）；
        4. 返回 (pi_result, sanity_report) 给上层：
           - sanity_report.ok == False 时，上层可以直接放弃本次训练，或者降级处理。
        """
        # 1) 让 PI 产生方案 + 查询数据（这里内部会用 build_select_sql + client.query_df）
        pi_result = self._pi.propose(
            table=table,
            symbol=symbol,
            start_ts=start_ts,
            end_ts=end_ts,
            goal=goal,
            horizon=horizon,
        )

        # 2) ClickHouse 端统计（针对你定义好的数值特征）
        ch_summary = fetch_column_stats_clickhouse(
            client=self._client,
            table=table,
            columns=numeric_feature_cols,
            symbol=symbol,
            start_ts=start_ts,
            end_ts=end_ts,
            symbol_col="symbol",
            ts_col="ts",
        )

        # 3) Python 侧体检（采样 + 共线性等）
        sanity_report = run_dataset_sanity_checks(
            pi_result.dataset_df,
            ch_summary=ch_summary,
            max_rows_for_checks=50_000,
        )

        return pi_result, sanity_report
```

上层使用时大概是：

```python
orch = PITrainingOrchestrator(ch_cfg=..., llm_cfg=...)

pi_result, sanity = orch.propose_and_load_dataset(
    table="bars_1m_enriched",
    symbol="TSLA",
    start_ts="2025-11-01 13:30:00",
    end_ts="2025-11-17 20:00:00",
    goal="预测未来10分钟收益率，做方向 & 强度判断",
    horizon="10m",
    numeric_feature_cols=[
        "close", "vwap", "volume", "rv20", "atr_14", "rsi_14", "banddist_down", ...
    ],
)

if not sanity.ok:
    # 硬拒绝：不允许在垃圾数据上训练
    print("Dataset sanity FAILED, skip HPO. Report:", sanity.summary_dict())
else:
    # 在这里把 pi_result.dataset_df + pi_result.hpo_grid 丢给 HPO 模块
    # run_hpo(pi_result.dataset_df, pi_result.hpo_grid, ...)
    pass
```

---

## 接下来可以做什么？

这一轮我们已经把你想要的三件事做掉了：

1. **性能问题**：大规模统计在 ClickHouse 端做，Python 只用采样。
2. **健壮性**：DatasetSanityReport 有明确的 `ok`、issue 列表，可以挂在 HPO 之前，当“硬 gate”。
3. **AI 大脑的“眼睛”**：下一步很容易把 `sanity_report.summary_dict()` 喂给 Brain，让它学会：

   * 哪些 run_id 是在“数据质量糊成一团”的前提下训练出来的；
   * 哪些特征常年有问题，自动降低权重或直接剔除。

如果你觉得这块 ok，我们下一步可以选一个：

* **Option A**：在 AI 大脑调用链上，把 `sanity_report` 纳入 context，让大脑会说 “这批数据不行，我拒绝训练/拒绝出信号”；
* **Option B**：开始设计/落地 **Sandbox 特征机制**，让 PI 在严格 DSL 和 DataContract 下“自己发明新特征”，并通过 DatasetSanity 自动筛选。

你刚才说 “继续”，我这轮优先把 DatasetSanity 性能和结构打牢了。
如果你愿意，下一步我就直接开搞 **Sandbox 特征 DSL + 注册流程**，让 AI 真正有“安全的创造力”。
