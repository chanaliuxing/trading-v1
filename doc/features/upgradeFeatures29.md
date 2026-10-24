好～那我们就把 **prompt 白名单这一层真正接上 AI 大脑**，让它只能在你给的这些 prompt 里选，不能乱来。

这一步我帮你落地三个东西：

1. `PromptLibrary`：把你这份大 JSON 解析成一个干净的 “id → 文本 + meta” 的库
2. `BrainPromptPolicy`：根据模式（live / paper / research）和时间，算出 `allowed_prompt_ids`
3. 一个完整示例：如何把它接到 `BrainStrategySelector` 上

---

## 1. PromptLibrary：统一管理 prompt 文本 + meta

> 假设你的这份 JSON 存在 `config/prompt_library.json` 里，就是你发给我的那个结构。

```python
# brain/prompt_library.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


@dataclass
class PromptDef:
    """
    单个 prompt 的定义：
    - id: 唯一 ID（例如 "v3_stable", "v3_fast_market_following_tactical_20251102"）
    - text: 实际给 LLM 用的 prompt 文本
    - created_at: 来自 meta 的时间戳（如果有）
    - base_id: 战术 prompt 的基准 ID（例如 v3_fast_market_following）
    - source: meta.source（例如 auto_evolver）
    - version: meta.version
    """
    id: str
    text: str
    created_at: Optional[datetime] = None
    base_id: Optional[str] = None
    source: Optional[str] = None
    version: int = 1
    extra_meta: Dict[str, Any] = field(default_factory=dict)


class PromptLibrary:
    """
    负责：
    - 从 JSON dict / 文件中加载 prompt_library
    - 统一合并 top-level + prompts 子对象 + meta
    - 提供按 base_id / 时间窗口筛选的能力
    """

    RESERVED_TOP_LEVEL_KEYS = {"_comment", "_usage", "_version", "prompts", "meta"}

    def __init__(self, prompts: Dict[str, PromptDef]) -> None:
        self._prompts = prompts  # id -> PromptDef

    # ------------ 基本访问 API ------------ #

    def get(self, prompt_id: str) -> Optional[PromptDef]:
        return self._prompts.get(prompt_id)

    def get_text(self, prompt_id: str) -> Optional[str]:
        p = self._prompts.get(prompt_id)
        return p.text if p else None

    def list_ids(self) -> List[str]:
        return sorted(self._prompts.keys())

    def to_id_text_dict(self) -> Dict[str, str]:
        """给 BrainStrategySelector 用：id -> text"""
        return {pid: p.text for pid, p in self._prompts.items()}

    # ------------ 策略相关筛选 API ------------ #

    def filter_by_base_id(self, base_id: str) -> List[PromptDef]:
        """
        找出所有 base_id 匹配的 prompt（包括 tactical 版本）。
        注意：如果 meta 里没有 base_id，就不在这里返回。
        """
        out: List[PromptDef] = []
        for p in self._prompts.values():
            if p.base_id == base_id:
                out.append(p)
        # 按创建时间降序
        out.sort(key=lambda x: (x.created_at or datetime.min.replace(tzinfo=timezone.utc)), reverse=True)
        return out

    def recent_tactical_prompts(
        self,
        *,
        base_id: str,
        max_days: int = 5,
        max_count: int = 5,
        now: Optional[datetime] = None,
    ) -> List[PromptDef]:
        """
        返回某个 base_id 对应的最近几天内的战术 prompt。
        """
        if now is None:
            now = datetime.now(timezone.utc)

        all_for_base = self.filter_by_base_id(base_id)
        cutoff = now - timedelta(days=max_days)

        result: List[PromptDef] = []
        for p in all_for_base:
            if p.created_at is None:
                continue
            if p.created_at < cutoff:
                continue
            result.append(p)
            if len(result) >= max_count:
                break
        return result

    # ------------ 构造方法 ------------ #

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PromptLibrary":
        """
        data: 你整个 JSON 文件加载后的 dict。
        合并逻辑：
        - 先读 top-level 中的所有 prompt（除去 reserved keys）
        - 再读 data["prompts"] 子对象，如有同名则覆盖 text（默认认为 prompts 子对象是“执行 prompt”）
        - 再把 data["meta"] 里的信息灌回 PromptDef
        """

        prompts: Dict[str, PromptDef] = {}

        # 1) 先加载 top-level 中的文本
        for key, value in data.items():
            if key in cls.RESERVED_TOP_LEVEL_KEYS:
                continue
            if isinstance(value, str):
                prompts[key] = PromptDef(id=key, text=value)

        # 2) 再加载 "prompts" 子对象：认为是更具体的执行版本 → 覆盖文本
        nested_prompts = data.get("prompts") or {}
        if isinstance(nested_prompts, dict):
            for pid, text in nested_prompts.items():
                if not isinstance(text, str):
                    continue
                if pid in prompts:
                    prompts[pid].text = text
                else:
                    prompts[pid] = PromptDef(id=pid, text=text)

        # 3) meta 信息：created_at / base_id / source / version 等
        meta = data.get("meta") or {}
        if isinstance(meta, dict):
            for pid, meta_info in meta.items():
                if not isinstance(meta_info, dict):
                    continue
                p = prompts.get(pid)
                if not p:
                    # meta 里有，但上面没加载到文本 → 先跳过，必要时可告警
                    continue

                # created_at
                created_at_raw = meta_info.get("created_at")
                created_at: Optional[datetime] = None
                if isinstance(created_at_raw, str):
                    try:
                        # 兼容 2025-11-02T06:38:55.442900+00:00 / ...Z
                        created_at = datetime.fromisoformat(
                            created_at_raw.replace("Z", "+00:00")
                        )
                    except Exception:
                        created_at = None

                p.created_at = created_at or p.created_at
                p.base_id = meta_info.get("base_id") or p.base_id
                p.source = meta_info.get("source") or p.source
                version = meta_info.get("version")
                if isinstance(version, int):
                    p.version = version
                # 其他信息丢到 extra_meta
                p.extra_meta.update(meta_info)

        return cls(prompts=prompts)

    @classmethod
    def from_json_file(cls, path: str) -> "PromptLibrary":
        import json
        from pathlib import Path

        with Path(path).open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)
```

> ✅ 现在你这份复杂的 JSON（有 top-level + prompts + meta）会被整理成一个统一的 `PromptLibrary`。
> 后面所有和 prompt 相关的东西，都通过这个类来拿。

---

## 2. BrainPromptPolicy：为不同模式生成 allowed_prompt_ids

我们给 AI 大脑一层 **策略规则**，避免一次性把几十个 tactical prompt 全扔给它。

设计一个简单的策略类：

* live 模式：

  * 永远允许：`v3_stable`（或者你指定的稳定 prompt）
  * 允许：`v3_fast_market_following`（基础快节奏策略）
  * 再加上：最近 N 天内 auto_evolver 生成的 `v3_fast_market_following` 战术 prompt（meta.base_id == v3_fast_market_following）
* paper / research 模式：

  * 可以额外加 `v4_experimental`、`aggressive_scalper` 等

```python
# brain/prompt_policy.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List

from .prompt_library import PromptLibrary


@dataclass
class BrainPromptPolicyConfig:
    """
    配置：不同模式下允许的基础 prompt 和战术 prompt 的窗口。
    """
    stable_prompt_ids: List[str]  # 永远允许，比如 ["v3_stable"]
    fast_base_id: str = "v3_fast_market_following"
    fast_tactical_days: int = 5       # 最近几天的战术 prompt
    fast_tactical_max_count: int = 4  # 最多取几个


class BrainPromptPolicy:
    """
    根据模式（live / paper / research）和当前时间，返回 allowed_prompt_ids。
    """

    def __init__(self, library: PromptLibrary, config: BrainPromptPolicyConfig) -> None:
        self._lib = library
        self._cfg = config

    def for_live_trading(self, now: datetime | None = None) -> List[str]:
        """
        live 模式：非常保守，只允许：
        - stable_prompt_ids
        - fast_base_id（如果存在）
        - 最近 N 天内的战术 prompt
        """
        if now is None:
            now = datetime.now(timezone.utc)

        allowed: List[str] = []

        # 1) 稳定 prompt
        for pid in self._cfg.stable_prompt_ids:
            if self._lib.get(pid) is not None:
                allowed.append(pid)

        # 2) fast base prompt
        base = self._lib.get(self._cfg.fast_base_id)
        if base is not None:
            allowed.append(base.id)

        # 3) 最近 N 天战术 prompt
        tactical = self._lib.recent_tactical_prompts(
            base_id=self._cfg.fast_base_id,
            max_days=self._cfg.fast_tactical_days,
            max_count=self._cfg.fast_tactical_max_count,
            now=now,
        )
        for p in tactical:
            if p.id not in allowed:
                allowed.append(p.id)

        return allowed

    def for_paper_trading(self, now: datetime | None = None) -> List[str]:
        """
        paper 模式：比 live 稍微开放一点，可以把 v4_experimental 之类也加进去。
        """
        allowed = self.for_live_trading(now=now)

        # Paper 模式可选更多实验 prompt
        extra_candidates = [
            "v4_experimental",
            "conservative",
            "aggressive_scalper",
        ]
        for pid in extra_candidates:
            if self._lib.get(pid) is not None and pid not in allowed:
                allowed.append(pid)

        return allowed

    def for_research(self) -> List[str]:
        """
        research 模式：你可以直接开放整个库（或者大部分）。
        这里先简单：全部 ID。
        """
        return self._lib.list_ids()
```

你完全可以按自己风格改配置，比如：

* live：只允许 `["v3_stable", 最近 2 条 tactical_v2]`
* paper：再+1 条 `v4_experimental`
* research：放飞自我

---

## 3. 把 PromptLibrary + Policy 接到 BrainStrategySelector

接下来把前面写好的所有东西串起来：

```python
# brain/select_strategy_example.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict

from llm.llm_client import LLMClient

from experiments.registry import ExperimentRegistry
from brain.prompt_library import PromptLibrary
from brain.prompt_policy import BrainPromptPolicy, BrainPromptPolicyConfig
from brain.selector import BrainStrategySelector


def build_brain_selector(
    *,
    prompt_library_path: str,
    experiment_registry_path: str,
    llm_model: str,
    llm_base_url: str | None = None,
) -> BrainStrategySelector:
    """
    工厂方法：根据本地配置文件，构造一个可以“选策略”的 AI 大脑封装。
    """

    # 1) 加载 prompt library（就是你那份 JSON）
    prompt_lib = PromptLibrary.from_json_file(prompt_library_path)

    # 2) 实验 registry（假设是基于 JSONL 的）
    exp_registry = ExperimentRegistry(jsonl_path=experiment_registry_path)

    # 3) LLM client（可以是 GPT / Claude / DeepSeek 等）
    llm_client = LLMClient(model=llm_model, base_url=llm_base_url)

    # 4) 构造 BrainStrategySelector
    selector = BrainStrategySelector(
        experiment_registry=exp_registry,
        prompt_library=prompt_lib.to_id_text_dict(),
        llm_client=llm_client,
    )

    return selector, prompt_lib, exp_registry


def run_brain_decision_for_symbol(
    *,
    symbol: str,
    horizon: str,
    market_snapshot: Dict[str, Any],
    prompt_lib: PromptLibrary,
    selector: BrainStrategySelector,
    mode: str = "live",  # "live" / "paper" / "research"
) -> None:
    """
    这是 T+0 生产/仿真调用的入口示例：
    - 先根据 mode 算出 allowed_prompt_ids
    - 再调用 BrainStrategySelector.select_strategy(...)
    """

    # 1) 根据 mode 决定 allowed_prompt_ids
    policy_cfg = BrainPromptPolicyConfig(
        stable_prompt_ids=["v3_stable"],  # 可以根据你未来的命名规则调整
        fast_base_id="v3_fast_market_following",
        fast_tactical_days=5,
        fast_tactical_max_count=4,
    )
    policy = BrainPromptPolicy(prompt_lib, policy_cfg)

    if mode == "live":
        allowed_prompt_ids = policy.for_live_trading()
    elif mode == "paper":
        allowed_prompt_ids = policy.for_paper_trading()
    else:
        allowed_prompt_ids = policy.for_research()

    # 2) 调用 AI 大脑做决策（内部会自动做 schema 校验）
    decision = selector.select_strategy(
        symbol=symbol,
        horizon=horizon,
        market_snapshot=market_snapshot,
        allowed_prompt_ids=allowed_prompt_ids,
        max_experiment_summaries=5,
    )

    # 3) 这里只是打印一下，真实系统里你会把 decision 往下游策略执行器传
    print("BrainDecision:", decision)
```

> 至此，**PI → HPO → ExperimentRegistry → BrainStrategySelector → PromptPolicy** 这一条线基本闭合：
>
> * 数据 & 特征 & 标签：已经在前面的 DatasetSanity + LabelContract + FeatureRegistry 里加锁
> * ML/HPO/实验治理：通过 `ExperimentRegistry` 和 `ExperimentSummarizer` 提供“策略候选池”
> * AI 大脑 & Prompt 安全：Brain 只能在 **已存在 run_id + 已白名单的 prompt_id** 中选

---

## 接下来可以干什么？

你现在已经有了：

* 数据体检模块（DatasetSanity）
* PI → HPO → ExperimentRegistry（已经打通）
* prompt 白名单（PromptLibrary + Policy）
* 策略选择脑（BrainStrategySelector + BrainDecisionValidator）

**下一步可以分两条线并行：**

1. **往下接：执行层 skeleton（Phase 4B 雏形）**

   * 根据 `BrainDecision.chosen_run_id` 从 ExperimentRegistry / model store 加载对应模型 & 特征配置
   * 根据 `execution_prompt_id` 调你的 “执行 Agent”，生成具体下单 JSON（目前仍然可以走 paper）
   * 写一个 `ExecutionPlan` dataclass，把 “策略 + prompt + 风控参数” 封装成执行计划（还不必连 IBKR）

2. **往上补：DatasetSanity 的 ClickHouse 端优化 + Sandbox 特征真正接入**

   * 把 “Null / Inf / variance / collinearity” 这类统计下沉到 ClickHouse 侧
   * 把 Sandbox 特征的 schema + 血缘记录写入 ExperimentRun，方便以后做 feature governance

如果你愿意，我们下一步可以专门做 **“执行层 skeleton + ExecutionPlan”**，让整个链条从 “选策略” 变成 “能给出一份可执行计划（但还不下单）”。
好，我们现在把 **“选好策略 → 变成可执行计划 → 交给执行层”** 这一段骨架搭出来，这就是 Phase 4B 的前半截（不连券商，只到「生成可以执行的计划」）。

下面所有东西你可以按模块丢到 `brain/` 或 `execution/` 目录里。

---

## 1. ExecutionPlan & 运行时配置：统一描述“这次要怎么跑模型”

先定义几种核心结构：

* `StrategyRuntimeConfig`：从实验记录里抽出来的、模型运行所需最小信息
* `OrderSpec`：一条“逻辑订单”（还没发给 IBKR）
* `ExecutionPlan`：AI 大脑对这一次调用的完整意图（用哪个 run，风险上限，多大仓位，用哪个执行 prompt）

```python
# execution/types.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StrategyRuntimeConfig:
    """
    从 ExperimentRecord/Registry 中抽取出来的运行时配置：
    - run_id: 使用哪个训练好的 run
    - model_type: xgb / lgbm / stacking / ...
    - artifact_uri: 模型文件存放位置（本地路径 or S3/MinIO 等）
    - feature_names: 在线推理时需要的特征列表（必须和训练一致）
    - label_name: 仅用于日志/解释，推理时一般不用
    - task_type: "reg", "clf", "long_short", ...
    - meta: 额外信息，如训练窗口、数据版本、数据源表名等
    """
    run_id: str
    model_type: str
    artifact_uri: str
    feature_names: List[str]
    label_name: Optional[str] = None
    task_type: str = "reg"
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderSpec:
    """
    一条“逻辑订单”，还没送到券商。
    ExecutionPlan 的输出就是一组这样的 OrderSpec。
    """
    symbol: str
    side: str          # "BUY" / "SELL"
    size: float        # 股数或合约数
    order_type: str    # "MKT" / "LMT" / "STP" / "STP_LMT" ...
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    time_in_force: str = "DAY"
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionPlan:
    """
    AI 大脑最终给执行层的“计划书”。

    - strategy: 选用的模型 run + 特征配置
    - mode: "live" / "paper" / "research"
    - max_risk_fraction: 本次计划占用总权益的最大比例（例如 0.02）
    - desired_gross_exposure: 目标总名义敞口（可选，给执行层做进一步计算）
    - execution_prompt_id: 执行 Agent 用的 prompt ID（从 PromptLibrary 里拿文本）
    - metadata: 存一些解释性信息（brain_decision 原文、rationale 等）

    注意：ExecutionPlan 本身不包含“已经计算好的订单”，
    真正的订单由执行器根据最新行情 + 模型输出推导出来。
    """
    strategy: StrategyRuntimeConfig
    mode: str
    max_risk_fraction: float
    execution_prompt_id: str
    desired_gross_exposure: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
```

---

## 2. 从 BrainDecision + ExperimentRegistry 生成 ExecutionPlan

假设你 Phase 4A 里的 `BrainDecision` 大概长这样（字段名你可以对照改一下）：

```python
# brain/types.py（示例）

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class BrainDecision:
    chosen_run_id: str
    execution_prompt_id: str
    mode: str            # "live" / "paper" / "research"
    max_risk_fraction: float
    rationale: str
    fallback: Optional[str] = None
    extra: Dict[str, Any] = None
```

再假设 ExperimentRegistry/Record 大概类似这样：

```python
# experiments/types.py（示例）

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class ExperimentRecord:
    run_id: str
    model_type: str
    task_type: str
    feature_names: List[str]
    label_name: Optional[str]
    artifact_uri: str
    metrics: Dict[str, float]
    tags: Dict[str, Any]
    meta: Dict[str, Any]
```

我们写一个转换器，把 `ExperimentRecord + BrainDecision` → `ExecutionPlan`：

```python
# execution/plan_builder.py

from __future__ import annotations

from typing import Any

from execution.types import ExecutionPlan, StrategyRuntimeConfig
from experiments.registry import ExperimentRegistry
from brain.types import BrainDecision


class ExecutionPlanBuilder:
    """
    核心职责：
    - 根据 BrainDecision.chosen_run_id 从 ExperimentRegistry 取出相应 ExperimentRecord
    - 抽取运行时需要的字段，构建 StrategyRuntimeConfig
    - 组合成 ExecutionPlan，供下一步执行器使用
    """

    def __init__(self, registry: ExperimentRegistry) -> None:
        self._registry = registry

    def build_from_decision(self, decision: BrainDecision) -> ExecutionPlan:
        # 1) 找 run 记录
        record = self._registry.get_by_run_id(decision.chosen_run_id)
        if record is None:
            raise ValueError(f"Experiment run_id='{decision.chosen_run_id}' not found in registry")

        # 2) 构造 StrategyRuntimeConfig
        runtime_cfg = StrategyRuntimeConfig(
            run_id=record.run_id,
            model_type=record.model_type,
            artifact_uri=record.artifact_uri,
            feature_names=record.feature_names,
            label_name=record.label_name,
            task_type=getattr(record, "task_type", "reg"),
            meta={
                "metrics": record.metrics,
                "tags": record.tags,
                "meta": record.meta,
            },
        )

        # 3) 构造 ExecutionPlan
        plan = ExecutionPlan(
            strategy=runtime_cfg,
            mode=decision.mode,
            max_risk_fraction=decision.max_risk_fraction,
            execution_prompt_id=decision.execution_prompt_id,
            # 这里 desired_gross_exposure 你可以让 BrainDecision 带进来，也可以后面再算
            desired_gross_exposure=decision.extra.get("desired_gross_exposure")
            if decision.extra
            else None,
            metadata={
                "brain_rationale": decision.rationale,
                "brain_extra": decision.extra or {},
            },
        )
        return plan
```

> 至此，**选好的 run_id + execution_prompt_id** 已经被封装成一个干净的 `ExecutionPlan`，可以独立测试，不需要连 LLM 或 HPO。

---

## 3. ModelRuntime：运行时加载模型（先给接口和 stub）

下一步是 “根据 `StrategyRuntimeConfig` 把模型和特征管道加载起来”。

这部分跟你的 ML 框架强相关（XGBoost / LightGBM / sklearn / PyTorch / ONNX…），我们先用一个简单的接口 + stub，后面你填具体实现：

```python
# execution/model_runtime.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from execution.types import StrategyRuntimeConfig


class ModelRuntimeError(Exception):
    pass


@dataclass
class ModelRuntime:
    """
    一个统一的“模型运行时封装”：
    - 持有已经加载好的模型对象
    - 暴露 predict(...) 接口供执行器调用
    """
    cfg: StrategyRuntimeConfig
    model: Any

    def predict(self, features: pd.DataFrame) -> pd.Series | pd.DataFrame:
        """
        输入：features DataFrame，包含 cfg.feature_names 所需列
        输出：预测值（Series 或 DataFrame），由具体模型决定 shape
        """
        # 这里假设所有模型都有 predict 接口；不同框架你可以在子类里覆盖
        return self.model.predict(features[self.cfg.feature_names])


class ModelRuntimeLoader:
    """
    根据 StrategyRuntimeConfig 从 artifact_uri 加载对应模型。

    注意：
    - 这里先做一个简单的分发，后面你可以细化到不同 model_type 的具体 Loader。
    """

    def load(self, cfg: StrategyRuntimeConfig) -> ModelRuntime:
        if cfg.model_type in ("xgb", "xgboost"):
            model = self._load_xgb(cfg.artifact_uri)
        elif cfg.model_type in ("lgbm", "lightgbm"):
            model = self._load_lgbm(cfg.artifact_uri)
        elif cfg.model_type in ("sklearn", "logreg", "ridge", "lasso", "elasticnet"):
            model = self._load_sklearn(cfg.artifact_uri)
        else:
            raise ModelRuntimeError(f"Unsupported model_type={cfg.model_type}")

        return ModelRuntime(cfg=cfg, model=model)

    # 以下都是 stub，你根据自己已有的存储方式实现即可
    def _load_xgb(self, uri: str) -> Any:
        """
        典型实现：
        - 如果 uri 以 "file://" 开头，去掉前缀，用 xgboost.Booster().load_model()
        - 如果是 S3/MinIO，可以先下载到本地临时路径，再 load
        """
        import xgboost as xgb

        path = self._resolve_uri_to_path(uri)
        booster = xgb.Booster()
        booster.load_model(path)
        return booster

    def _load_lgbm(self, uri: str) -> Any:
        import lightgbm as lgb

        path = self._resolve_uri_to_path(uri)
        return lgb.Booster(model_file=path)

    def _load_sklearn(self, uri: str) -> Any:
        import joblib

        path = self._resolve_uri_to_path(uri)
        return joblib.load(path)

    def _resolve_uri_to_path(self, uri: str) -> str:
        """
        非常简化的版本：只处理本地路径 or file://。
        未来你可以扩展支持 s3://, minio:// 等。
        """
        if uri.startswith("file://"):
            return uri[len("file://") :]
        # 也可以加一个根目录：
        return uri
```

> 🔧 这里你只要把 `artifact_uri` 的写入规范（HPO/训练阶段）和 loader 保持一致，就行了。现在 ExecutionPlan → StrategyRuntimeConfig → ModelRuntime 这条链就通了。

---

## 4. Orchestrator Skeleton：从 ExecutionPlan → Orders（不连券商）

最后一段，我们写一个 **执行骨架**：

* 输入：`ExecutionPlan` + 最新的特征行（或一小段窗口）
* 步骤：

  1. 用 `ModelRuntimeLoader` 加载模型
  2. 对最新特征做一次 `predict`
  3. 用一个简单的 “决策规则” 把预测转成多空/仓位建议
  4. 生成若干 `OrderSpec`（这里只写一个非常简单的例子，方便你替换）

```python
# execution/orchestrator.py

from __future__ import annotations

from dataclasses import dataclass
from typing import List

import pandas as pd

from execution.types import ExecutionPlan, OrderSpec
from execution.model_runtime import ModelRuntimeLoader


@dataclass
class ExecutionContext:
    """
    执行时需要的一些上下文（可以按需扩展）：
    - symbol: 当前标的
    - account_equity: 当前账户净值（用来算风险/仓位）
    - current_position: 当前持仓（股数）
    - last_price: 当前价格（或者从 features 里拿）
    """
    symbol: str
    account_equity: float
    current_position: float
    last_price: float


class ExecutionOrchestrator:
    """
    把 ExecutionPlan + 实时特征 + 账户状态 → OrderSpec 列表。

    注意：
    - 这里不连券商，只是返回“应该下什么单”的结构。
    - 真正发给 IBKR 的那一层可以单独写 adapter，把 OrderSpec → IBKR API 调用。
    """

    def __init__(self, model_loader: ModelRuntimeLoader | None = None) -> None:
        self._loader = model_loader or ModelRuntimeLoader()

    def run_once(
        self,
        plan: ExecutionPlan,
        ctx: ExecutionContext,
        latest_features: pd.DataFrame,
    ) -> List[OrderSpec]:
        """
        - latest_features: 通常是 1 行（当前时刻特征），也可以是一小段时间窗口。
        """

        # 1) 加载模型
        runtime = self._loader.load(plan.strategy)

        # 2) 预测
        preds = runtime.predict(latest_features)
        # 简单起见，假设是单列 Series
        if isinstance(preds, pd.DataFrame):
            preds = preds.iloc[:, 0]
        score = float(preds.iloc[-1])

        # 3) 根据分数做一个极简 demo 决策（你以后可以替换为复杂的规则 or 另一个 Agent）
        #   假设 -1 ~ +1：<-0.2 认为偏空，>0.2 认为偏多，中间 HOLD
        orders: List[OrderSpec] = []
        target_side = "HOLD"

        if score > 0.2:
            target_side = "LONG"
        elif score < -0.2:
            target_side = "SHORT"

        # 4) 把 target_side 转成具体订单（这里只是示例）
        max_risk_value = ctx.account_equity * plan.max_risk_fraction
        max_shares_by_risk = max_risk_value / ctx.last_price if ctx.last_price > 0 else 0

        # 简单一点：目标仓位 = max_shares_by_risk
        # （真实逻辑你可以考虑已有仓位、信号强弱、ATR 等）
        if target_side == "LONG":
            # 如果已经有多头，就不再加；如果是空头则先平仓
            desired_pos = max_shares_by_risk
            delta = desired_pos - ctx.current_position
            if delta > 1:  # 大于 1 股再动
                orders.append(
                    OrderSpec(
                        symbol=ctx.symbol,
                        side="BUY",
                        size=delta,
                        order_type="MKT",
                        meta={
                            "reason": "model_long_signal",
                            "score": score,
                            "max_risk_fraction": plan.max_risk_fraction,
                        },
                    )
                )
        elif target_side == "SHORT":
            desired_pos = -max_shares_by_risk
            delta = desired_pos - ctx.current_position
            if delta < -1:
                # delta 为负，说明需要净卖出
                orders.append(
                    OrderSpec(
                        symbol=ctx.symbol,
                        side="SELL",
                        size=abs(delta),
                        order_type="MKT",
                        meta={
                            "reason": "model_short_signal",
                            "score": score,
                            "max_risk_fraction": plan.max_risk_fraction,
                        },
                    )
                )
        else:
            # HOLD：你可以选择什么都不做，或者平一部分仓位
            pass

        return orders
```

> ⚠️ 上面第 3/4 步只是一个「demo 逻辑」，方便你把 ExecutionPlan 跑通；
> 真实生产里你可以替换成：
>
> * 另一个 LLM Agent（Execution Agent），用 `execution_prompt_id` + 模型输出 + 账户信息生成更聪明的订单 JSON；
> * 或者用一套规则引擎（ATR 止损 + RVOL + VWAP 等）把 score → position。

---

## 5. 整条链路打通示例

最后，给你一个「从 BrainDecision 一直到 OrderSpec」的 demo 流程，你可以写一个 `scripts/demo_brain_to_orders.py` 来集成测试：

```python
# scripts/demo_brain_to_orders.py

from __future__ import annotations

import pandas as pd

from brain.strategy_selector import BrainStrategySelector  # 你已有的
from brain.prompt_library import PromptLibrary
from brain.prompt_policy import BrainPromptPolicy, BrainPromptPolicyConfig
from execution.plan_builder import ExecutionPlanBuilder
from execution.orchestrator import ExecutionOrchestrator, ExecutionContext
from experiments.registry import ExperimentRegistry


def main():
    # 1) 加载 registry & prompt_library & brain selector（Phase 4A 已完成这块）
    registry = ExperimentRegistry(jsonl_path="data/experiments.jsonl")
    prompt_lib = PromptLibrary.from_json_file("config/prompt_library.json")

    # 假设你已有 selector 工厂
    selector = BrainStrategySelector(
        experiment_registry=registry,
        prompt_library=prompt_lib.to_id_text_dict(),
        llm_client=...,  # 你的 LLMClient
    )

    # 2) 用 PromptPolicy 约束 allowed_prompt_ids
    policy_cfg = BrainPromptPolicyConfig(
        stable_prompt_ids=["v3_stable"],
        fast_base_id="v3_fast_market_following",
        fast_tactical_days=5,
        fast_tactical_max_count=4,
    )
    policy = BrainPromptPolicy(prompt_lib, policy_cfg)
    allowed_prompt_ids = policy.for_live_trading()

    # 3) 调 Brain 做“选策略”决策
    decision = selector.select_strategy(
        symbol="NVDA",
        horizon="1m",
        market_snapshot={"rvol20": 3.1, "session": "REG"},  # 只做示例
        allowed_prompt_ids=allowed_prompt_ids,
        max_experiment_summaries=5,
    )

    # 4) 把 BrainDecision → ExecutionPlan
    plan_builder = ExecutionPlanBuilder(registry)
    plan = plan_builder.build_from_decision(decision)

    # 5) 构造 ExecutionContext + 最新特征（这里手造一行假数据）
    ctx = ExecutionContext(
        symbol="NVDA",
        account_equity=100_000.0,
        current_position=0.0,
        last_price=120.0,
    )

    latest_features = pd.DataFrame(
        [
            {
                # 必须包含 plan.strategy.feature_names 里列出的字段；
                # 这里只是示例，你真实环境里应来自 ClickHouse / Redis / Kafka 等实时流水线。
                "close": 120.0,
                "vwap_1m": 119.8,
                "rsi_14": 60.0,
                # ...
            }
        ]
    )

    # 6) 执行一次：ExecutionPlan + ctx + 特征 → OrderSpec 列表
    orchestrator = ExecutionOrchestrator()
    orders = orchestrator.run_once(plan, ctx, latest_features)

    print("Generated orders:")
    for o in orders:
        print(o)


if __name__ == "__main__":
    main()
```

---

到这里为止，我们已经把你想要的三层关系里的**第三层（AI 大脑 & Prompt 安全）和第二层（实验治理的一部分）跟“执行骨架”连接上了**：

1. **数据/特征/标签层**：

   * DatasetSanity / LabelContract / FeatureRegistry / Sandbox 机制（前面几轮我们已经设计 + 写过代码）

2. **机器学习/HPO/实验治理层**：

   * PI → HPO → ExperimentRegistry → ExperimentSummary → BrainStrategySelector（已打通逻辑）
   * 现在又加了 `StrategyRuntimeConfig` & `ExecutionPlan`，把训练结果往“执行侧”拉了一步

3. **AI 大脑 & Prompt 安全层**：

   * PromptLibrary + PromptPolicy + BrainDecisionSchema（Phase 4A）
   * 现在 Brain 的输出真正变成一个可执行的 `ExecutionPlan`

**下一步如果你继续要我推下去**，我会建议：

* 针对 **ExecutionOrchestrator** 写一组单元测试（mock 模型输出 → 检查生成的订单是否符合风险约束）；
* 然后把 **DatasetSanity 的 ClickHouse 版本 + Sandbox 特征落地**，让整条链的 “输入” 和 “输出” 都更稳。
好，那我们就按之前说的，先把 **DatasetSanity 性能优化（ClickHouse + 采样版）** 真正落地成一套可以直接用的模块，并且说明怎么挂到你现在的 PI → HPO 流里。

这一步做完，你的“数据体检”就从玩具版升级成 **ClickHouse 驱动 + 采样 + 可配置** 的版本，真正适合你几百万行、上百列的高频表。

---

## 一、目标回顾：我们要解决什么？

数据体检现在的问题是：

* 以前的版本是在 **Pandas 层对整张 DataFrame 做检查**：

  * 几百万行 × 上百列 → 相关性矩阵 / 分布检查非常慢
  * HPO / 实验会被拖得很长
* 我们希望：

  1. **尽量在 ClickHouse 端做聚合统计**（空值率、常数列、简单统计量）
  2. 只在 **采样后的 DataFrame 上做重的检查**（比如相关性矩阵）
  3. 输出统一的 `sanity_report`，后面既可以给 AI 大脑总结，也可以给 HPO/PI 做硬门槛

所以这次我给你落地一个独立模块：

> `dataops/dataset_sanity_ck.py`
> （专门负责 “从 ClickHouse + base_sql 生成 sanity_report”）

---

## 二、ClickHouse 版 DatasetSanity 模块设计

### 1. 配置 Dataclass

```python
# dataops/dataset_sanity_ck.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
import numpy as np
import clickhouse_connect


@dataclass
class ClickhouseSanityConfig:
    """
    控制性能和阈值的配置。

    - max_rows_for_sample: 用于相关性/分布检查的最大采样行数
    - corr_max_features: 最多对多少个特征算相关性（避免 200×200 这种矩阵）
    - high_corr_threshold: 大于这个绝对值的相关性被视为“高相关”
    - missing_warn_threshold: 空值超过多少比例发出 warning
    - missing_error_threshold: 空值超过多少比例视为 error
    - zero_variance_error: 是否把零方差特征视为 error
    """

    max_rows_for_sample: int = 100_000
    corr_max_features: int = 30
    high_corr_threshold: float = 0.95

    missing_warn_threshold: float = 0.2
    missing_error_threshold: float = 0.5

    zero_variance_error: bool = True

    # 未来可以再扩展其他规则
    extra: Dict[str, Any] = field(default_factory=dict)
```

### 2. Sanity 报告结构

```python
@dataclass
class SanityIssue:
    level: str   # "info" / "warn" / "error"
    code: str    # 机器可读，比如 "MISSING_RATE_HIGH", "ZERO_VARIANCE"
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SanityReport:
    """
    统一的体检报告结构，既给人看，也给 AI 看。
    """
    ok: bool
    issues: List[SanityIssue]
    basic_stats: pd.DataFrame  # 每列的 count / null_rate / min / max / mean / std
    high_corr_pairs: pd.DataFrame  # 高相关特征对（基于采样）
```

---

## 三、ClickHouse 驱动的 DatasetSanity 实现

### 1. 主类：ClickhouseDatasetSanity

```python
class ClickhouseDatasetSanity:
    """
    基于 ClickHouse 的 Dataset Sanity 检查器。

    用法：
    - 传入：ClickHouse client、base_sql（PI 生成的那个 SELECT ...）、需要检查的列列表
    - 它会：
      1. 在 ClickHouse 端计算各列的基础统计（count、null_count、distinct_count、min/max/mean/std）
      2. 从 base_sql 抽样 max_rows_for_sample 行到 pandas
      3. 在采样数据上计算相关性矩阵，找出高相关特征对
      4. 根据阈值生成 issues 列表和 ok 标志
    """

    def __init__(self, client: clickhouse_connect.driver.Client, cfg: Optional[ClickhouseSanityConfig] = None) -> None:
        self._client = client
        self._cfg = cfg or ClickhouseSanityConfig()

    # ---------- 对外主入口 ----------

    def run(
        self,
        base_sql: str,
        feature_cols: Sequence[str],
        label_cols: Optional[Sequence[str]] = None,
    ) -> SanityReport:
        """
        :param base_sql: PI/HPO 构建的 SELECT ... 语句（不带 LIMIT）
        :param feature_cols: 需要检查的特征列
        :param label_cols: 可选，标签列（用于检查 missing / 常数等）
        """

        label_cols = label_cols or []
        all_cols = list(dict.fromkeys(list(feature_cols) + list(label_cols)))

        # 1) 基础统计（ClickHouse 端）
        basic_stats = self._compute_basic_stats(base_sql, all_cols)

        # 2) 采样数据（用于相关性 & 分布检查）
        sample_df = self._sample_dataframe(base_sql, all_cols)

        # 3) 生成 issues
        issues: List[SanityIssue] = []
        issues.extend(self._check_missingness(basic_stats))
        issues.extend(self._check_zero_variance(basic_stats))
        high_corr_pairs = self._check_correlation(sample_df, feature_cols, issues)

        # ok: 没有 error 级别的 issue
        ok = not any(iss.level == "error" for iss in issues)

        return SanityReport(
            ok=ok,
            issues=issues,
            basic_stats=basic_stats,
            high_corr_pairs=high_corr_pairs,
        )
```

### 2. ClickHouse 端基础统计查询

我们用 `base_sql` 当子查询，用一条 SQL 把所有列的统计算出来：

```python
    # ---------- 内部方法：基础统计 ----------

    def _compute_basic_stats(self, base_sql: str, cols: Sequence[str]) -> pd.DataFrame:
        """
        在 ClickHouse 端对所有列一次性做基础统计。
        输出 DataFrame，列包括：
        - col
        - count
        - null_count
        - distinct_count
        - null_rate
        - min
        - max
        - mean
        - std
        """

        # 为每个列生成一组聚合表达式
        agg_exprs = []
        for col in cols:
            # 注意：这里假设 base_sql 里的列已经是你要用的别名
            agg_exprs.append(
                f"'{col}' AS col_{col}, "
                f"count() AS cnt_{col}, "
                f"sum(isNull({col})) AS null_{col}, "
                f"uniqExact({col}) AS distinct_{col}, "
                f"min({col}) AS min_{col}, "
                f"max({col}) AS max_{col}, "
                f"avgOrNull(toFloat64OrNull({col})) AS mean_{col}, "
                f"stddevPop(toFloat64OrNull({col})) AS std_{col}"
            )

        # 把每个列的聚合结果 union all 起来（每列一行）
        # 注意：这里用子查询再 select，是为了生成统一结构
        union_sql_parts = []
        for col in cols:
            union_sql_parts.append(
                f"""
                SELECT
                    '{col}' AS col,
                    cnt_{col} AS count,
                    null_{col} AS null_count,
                    distinct_{col} AS distinct_count,
                    min_{col} AS min,
                    max_{col} AS max,
                    mean_{col} AS mean,
                    std_{col} AS std
                FROM (
                    SELECT
                        count() AS cnt_{col},
                        sum(isNull({col})) AS null_{col},
                        uniqExact({col}) AS distinct_{col},
                        min({col}) AS min_{col},
                        max({col}) AS max_{col},
                        avgOrNull(toFloat64OrNull({col})) AS mean_{col},
                        stddevPop(toFloat64OrNull({col})) AS std_{col}
                    FROM ({base_sql}) AS t
                )
                """
            )

        final_sql = " UNION ALL ".join(union_sql_parts)
        df = self._client.query_df(final_sql)

        # 计算 null_rate
        df["null_rate"] = df["null_count"] / df["count"].where(df["count"] != 0, 1)
        return df
```

> ✅ 说明：
>
> * 这里为了简单，每列用一段子查询 + UNION ALL，代码好理解。
> * 性能上对你典型几十个特征是没问题的（ClickHouse 聚合很快）；如果未来上百列，你可以再优化拼 SQL 的方式。

### 3. 从 ClickHouse 抽样到 Pandas

```python
    # ---------- 内部方法：采样 ----------

    def _sample_dataframe(self, base_sql: str, cols: Sequence[str]) -> pd.DataFrame:
        """
        从 base_sql 抽样 max_rows_for_sample 行，用于相关性等重检查。
        实现思路：
        - 用子查询包一层，加 LIMIT
        - 注意：LIMIT 不是严格的随机采样，但通常足够；如果要严格随机，可以加 ORDER BY rand()
        """

        limit = self._cfg.max_rows_for_sample
        col_list = ", ".join(cols)

        sample_sql = f"""
        SELECT {col_list}
        FROM ({base_sql}) AS t
        ORDER BY rand()
        LIMIT {limit}
        """

        df = self._client.query_df(sample_sql)
        return df
```

> 如果你更关心严格随机，可以在外层加 `WHERE rand() < p`，但 `ORDER BY rand()` + LIMIT 对很多场景也够用了。

### 4. 缺失值 & 零方差检查

```python
    # ---------- 内部方法：缺失 & 零方差 ----------

    def _check_missingness(self, basic_stats: pd.DataFrame) -> List[SanityIssue]:
        issues: List[SanityIssue] = []
        warn_thr = self._cfg.missing_warn_threshold
        err_thr = self._cfg.missing_error_threshold

        for _, row in basic_stats.iterrows():
            col = row["col"]
            null_rate = float(row.get("null_rate", 0.0))

            if null_rate >= err_thr:
                issues.append(
                    SanityIssue(
                        level="error",
                        code="MISSING_RATE_HIGH",
                        message=f"Column '{col}' has null_rate={null_rate:.3f} >= error_threshold={err_thr:.3f}",
                        details={"col": col, "null_rate": null_rate},
                    )
                )
            elif null_rate >= warn_thr:
                issues.append(
                    SanityIssue(
                        level="warn",
                        code="MISSING_RATE_WARN",
                        message=f"Column '{col}' has null_rate={null_rate:.3f} >= warn_threshold={warn_thr:.3f}",
                        details={"col": col, "null_rate": null_rate},
                    )
                )
        return issues

    def _check_zero_variance(self, basic_stats: pd.DataFrame) -> List[SanityIssue]:
        issues: List[SanityIssue] = []
        if not self._cfg.zero_variance_error:
            return issues

        for _, row in basic_stats.iterrows():
            col = row["col"]
            std = row.get("std")
            # std 为 None 或 0，都视为常数列
            if std is None or (isinstance(std, (int, float)) and float(std) == 0.0):
                issues.append(
                    SanityIssue(
                        level="error",
                        code="ZERO_VARIANCE",
                        message=f"Column '{col}' appears to have zero variance.",
                        details={"col": col, "std": std},
                    )
                )
        return issues
```

### 5. 相关性检查（采样数据上）

```python
    # ---------- 内部方法：相关性 ----------

    def _check_correlation(
        self,
        sample_df: pd.DataFrame,
        feature_cols: Sequence[str],
        issues: List[SanityIssue],
    ) -> pd.DataFrame:
        """
        在采样数据上算相关性矩阵，只挑前 corr_max_features 个特征。
        把 |corr| >= high_corr_threshold 的 pair 记录到 high_corr_pairs，同时生成 warn。
        """

        if sample_df.empty:
            return pd.DataFrame(columns=["col_a", "col_b", "corr"])

        numeric_cols = [c for c in feature_cols if np.issubdtype(sample_df[c].dtype, np.number)]
        if not numeric_cols:
            return pd.DataFrame(columns=["col_a", "col_b", "corr"])

        # 限制最多 corr_max_features 个特征
        numeric_cols = numeric_cols[: self._cfg.corr_max_features]
        df_num = sample_df[numeric_cols].copy()

        # 去除全 NaN 的列，避免 corr 报错
        df_num = df_num.dropna(axis=1, how="all")
        if df_num.shape[1] <= 1:
            return pd.DataFrame(columns=["col_a", "col_b", "corr"])

        corr_mat = df_num.corr(method="pearson")
        high_pairs = []
        thr = self._cfg.high_corr_threshold

        cols = corr_mat.columns.tolist()
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                a, b = cols[i], cols[j]
                val = corr_mat.iloc[i, j]
                if pd.notnull(val) and abs(val) >= thr:
                    high_pairs.append((a, b, float(val)))

        if not high_pairs:
            return pd.DataFrame(columns=["col_a", "col_b", "corr"])

        high_corr_df = pd.DataFrame(high_pairs, columns=["col_a", "col_b", "corr"])

        issues.append(
            SanityIssue(
                level="warn",
                code="HIGH_CORRELATION",
                message=f"{len(high_pairs)} feature pairs have |corr| >= {thr}",
                details={
                    "threshold": thr,
                    "pairs": high_pairs[:10],  # details 里只放前 10 个，避免太长
                },
            )
        )
        return high_corr_df
```

到这里，**ClickHouse + 采样版 DatasetSanity 已经是一个完整模块**了。

---

## 四、如何挂到 PI → HPO 流里？

你现在的 `PrincipalInvestigator.propose(...)` 大致是：

```python
plan = self._llm.generate(...)
recipe = plan.get("feature_recipe", {})
tplan = plan.get("training_plan", {})

sql = build_select_sql(...)
df = self._client.query_df(sql)

# 然后交给 HPO / 实验引擎
```

我们现在做两个关键增强：

1. **先做体检，再加载 DataFrame**（或者加载同时顺便采样，防止浪费）
2. **如果出现 error 级别的 issue，直接中止这次训练**，避免把垃圾数据送进 HPO

### 修改示例：在 PI 里插入 ClickhouseDatasetSanity

```python
# pi/principal_investigator.py（示例片段）

from dataops.dataset_sanity_ck import (
    ClickhouseDatasetSanity,
    ClickhouseSanityConfig,
)

class PrincipalInvestigator:
    ...

    def __init__(..., ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None) -> None:
        ...
        self._client = clickhouse_connect.get_client(...)
        # 新增：准备一个 sanity checker
        self._sanity = ClickhouseDatasetSanity(
            client=self._client,
            cfg=ClickhouseSanityConfig(
                max_rows_for_sample=100_000,
                corr_max_features=30,
                high_corr_threshold=0.97,
                missing_warn_threshold=0.1,
                missing_error_threshold=0.6,
            ),
        )

    def propose(...)-> PIResult:
        ...
        sql = build_select_sql(...)

        # 从 recipe 里拿出 base_features + derived aliases，作为要体检的 feature 列
        base_features = recipe.get("base_features", [])
        derived_features = [d.get("alias") for d in recipe.get("derived", [])]
        feature_cols = list(dict.fromkeys(base_features + derived_features))

        target_name = tplan.get("target")  # 如果 build_select_sql 已经把 target 列 alias 好

        # 1) 在 ClickHouse 端先跑一次体检
        sanity_report = self._sanity.run(
            base_sql=sql,
            feature_cols=feature_cols,
            label_cols=[target_name] if target_name else [],
        )

        # 2) 如果有 error，直接抛异常，中断这次 PI 提案
        if not sanity_report.ok:
            # 你可以定义一个专门的异常类型 DatasetSanityError
            error_msgs = [
                f"[{iss.code}] {iss.message}" for iss in sanity_report.issues if iss.level == "error"
            ]
            msg = "Dataset sanity check failed:\n" + "\n".join(error_msgs)
            # 也可以把 full report 记录到日志 / registry
            raise RuntimeError(msg)

        # 3) 如果 ok，再真正去取训练用 DataFrame
        df = self._client.query_df(sql)

        ...
        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            # 你也可以考虑把 sanity_report 一起返回，给后面链路用
        )
```

> ✅ 这样一来：
>
> * **每一次 PI 提案 / HPO 训练前都会先做体检**；
> * 如果数据在列层面已经烂掉（大量缺失 / 零方差），直接 fail fast；
> * 对你那些 “data corruption” 的情况，至少不会再静悄悄地进入训练。

---

## 五、下一步怎么走？

这一轮我们完成的是：

* ✅ 一个 **ClickHouse + 采样驱动的 DatasetSanity 模块**
* ✅ 清晰的 `SanityReport` / `SanityIssue` 结构
* ✅ 在 `PrincipalInvestigator.propose` 里插入体检的接入点（示例逻辑）

**下一步我建议：**

1. 写一两个 **最小集成测试**：

   * 用一条简单的 base_sql（比如从你现有 1m bar 表里选 2–3 个列）
   * 手动制造：

     * 一个全是 null 的列
     * 一个常数列
     * 两个高度相关的列（例如 col_b = col_a * 2）
   * 调用 `ClickhouseDatasetSanity.run(...)`，确认 issues / ok 标志符合预期

2. 然后我们可以开始落地 **Sandbox 特征机制**：

   * 允许 AI 在安全约束下提出新特征表达式（只引用过去的列，不跨表，不 lookahead）
   * 这些表达式入库到一个 `sandbox_features.jsonl` 或 CK 表
   * 有审查 / 提拔机制：表现好的 sandbox 特征变成正式 feature_registry 成员

如果你愿意，我们下一步就直接写：

* `feature_sandbox.py`：

  * `SandboxFeature` dataclass
  * 表达式校验（只允许某些函数 / 列名 / no future leakage）
  * 与现有 `feature_registry` 的衔接方式（PI 可以把 “正式特征 + 当日 sandbox 特征” 一起用）

你只要继续一句：**“继续”**，我就直接给你 Sandbox 特征的落地代码。
