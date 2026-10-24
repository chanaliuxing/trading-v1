好，那我们就按之前说的，把 **PI → HPO → ExperimentRegistry** 这一条链真正“串起来”，重点做到三件事：

1. 有一个**统一的实验记录格式（ExperimentRun）**
2. 有一个**简单但可靠的 ExperimentRegistry（写 JSONL / 可查询）**
3. 在 **PI + HPO** 之间，知道“该在什么时刻把什么信息写进 Registry”

这一步不动你现有训练逻辑，只是在“边上”加一条**审计 / 实验治理的高速公路**。

---

## 一、实验数据模型：`experiments/types.py`

目标：每一次 “PI + HPO + 回测” 完成后，都可以抽象成一条 `ExperimentRun`。

```python
# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class Metric:
    """单个指标，如 sharpe, win_rate, auc 等。"""

    name: str
    value: float
    higher_is_better: bool = True


@dataclass
class FeatureUsage:
    """
    记录本次实验用到的特征（尤其是 sandbox 特征），便于后续分析：
    - 哪些 sandbox 特征在成功策略中频繁出现
    """

    name: str
    kind: str  # "base" / "derived"
    is_sandbox: bool = False
    importance: Optional[float] = None  # 从 model.feature_importances_ / shap 等来
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasetSummary:
    """
    来自 DatasetSanity 的摘要（已经是 high level，而不是原始 DataFrame）。
    """

    dataset_name: str
    n_rows: int
    n_cols: int
    issues: List[Dict[str, Any]] = field(default_factory=list)
    has_fatal_issues: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SandboxFeatureRecord:
    """
    本次实验中新注册 / 使用的 sandbox 特征信息。
    对应之前 FeatureRegistry 里 is_sandbox=True 的那些。
    """

    name: str
    expr: str
    kind: str = "derived"
    created_by: str = "pi_llm"
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExperimentRun:
    """
    一次完整 “PI+HPO+回测” 的最小审计单元。

    这里原则是：
    - 不试图记录所有细节（比如完整模型对象），那是 Model Store 的事情
    - 只记录足够让你：
      * 复现配置（run_config）
      * 评估质量（metrics）
      * 追溯数据质量（dataset_summary）
      * 分析特征/标签/策略选择（features, label_def, decision_cfg）
    """

    run_id: str

    # 基本身份信息
    created_at: str
    symbol: str
    table: str
    start_ts: str
    end_ts: str
    horizon: str  # 如 "10m", "30m", "1h"

    # 数据 & 标签
    label_def: Dict[str, Any]
    dataset_summary: DatasetSummary

    # 特征 & 模型
    features: List[FeatureUsage]
    sandbox_features: List[SandboxFeatureRecord] = field(default_factory=list)

    model_type: str
    model_params: Dict[str, Any]
    hpo_config: Dict[str, Any] = field(default_factory=dict)

    # 策略 / 决策逻辑（来自 training_plan 的 decision）
    decision_cfg: Dict[str, Any] = field(default_factory=dict)

    # 结果指标
    metrics: List[Metric] = field(default_factory=list)

    # 附加元信息：可以放回测结果路径 / 模型存储位置 / prompt id 等
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        def _convert(obj: Any) -> Any:
            if isinstance(obj, (Metric, FeatureUsage, SandboxFeatureRecord, DatasetSummary)):
                return asdict(obj)
            if isinstance(obj, list):
                return [_convert(x) for x in obj]
            return obj

        d = asdict(self)
        for k, v in list(d.items()):
            d[k] = _convert(v)
        return d
```

> 你以后如果想加字段（例如 `backtest_curve_path`、`model_store_uri`），直接在 `ExperimentRun` 里加即可。

---

## 二、ExperimentRegistry：写 JSONL + 简单索引

我们先做一个**文件级 registry**（简单、皮实、不依赖数据库），放在例如：`./experiments/registry/experiments.jsonl`。

```python
# experiments/registry.py

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .types import ExperimentRun


class ExperimentRegistry:
    """
    超轻量的实验注册表：
    - 所有实验 append 到一个 JSONL 文件里
    - 提供简单 query 能按 symbol / horizon / 时间过滤
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._log_path = self.root / "experiments.jsonl"

    # --------------- 写入 --------------- #

    def append(self, run: ExperimentRun) -> None:
        line = json.dumps(run.to_dict(), ensure_ascii=False)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # --------------- 读取 / 查询 --------------- #

    def iter_runs(self) -> Iterable[ExperimentRun]:
        if not self._log_path.exists():
            return
        with self._log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                yield self._from_dict(raw)

    def find(
        self,
        *,
        symbol: Optional[str] = None,
        horizon: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> List[ExperimentRun]:
        """
        非高性能版查询，够用就行：
        - 按 symbol / horizon / 时间范围筛选
        """

        def _in_range(run: ExperimentRun) -> bool:
            if symbol and run.symbol != symbol:
                return False
            if horizon and run.horizon != horizon:
                return False
            if since and run.created_at < since:
                return False
            if until and run.created_at > until:
                return False
            return True

        return [r for r in self.iter_runs() if _in_range(r)]

    # --------------- 辅助：反序列化 --------------- #

    def _from_dict(self, raw: Dict[str, Any]) -> ExperimentRun:
        from .types import (
            Metric,
            FeatureUsage,
            SandboxFeatureRecord,
            DatasetSummary,
        )

        metrics = [Metric(**m) for m in raw.get("metrics", [])]
        features = [FeatureUsage(**f) for f in raw.get("features", [])]
        sandbox_features = [
            SandboxFeatureRecord(**s) for s in raw.get("sandbox_features", [])
        ]
        ds_raw = raw.get("dataset_summary") or {}
        ds = DatasetSummary(
            dataset_name=ds_raw.get("dataset_name", ""),
            n_rows=ds_raw.get("n_rows", 0),
            n_cols=ds_raw.get("n_cols", 0),
            issues=ds_raw.get("issues", []),
            has_fatal_issues=ds_raw.get("has_fatal_issues", False),
            meta=ds_raw.get("meta", {}),
        )

        return ExperimentRun(
            run_id=raw["run_id"],
            created_at=raw["created_at"],
            symbol=raw["symbol"],
            table=raw["table"],
            start_ts=raw["start_ts"],
            end_ts=raw["end_ts"],
            horizon=raw["horizon"],
            label_def=raw.get("label_def", {}),
            dataset_summary=ds,
            features=features,
            sandbox_features=sandbox_features,
            model_type=raw["model_type"],
            model_params=raw.get("model_params", {}),
            hpo_config=raw.get("hpo_config", {}),
            decision_cfg=raw.get("decision_cfg", {}),
            metrics=metrics,
            meta=raw.get("meta", {}),
        )
```

> 这是最简版，但已经是“生产可用”的骨架：
>
> * JSONL 方便 append（崩了也不怕）
> * 你以后可以再加一个 ClickHouse / SQLite 的镜像或视图层来加速分析

---

## 三、给 AI 大脑用的“实验摘要构造器”：`experiments/summary.py`

目的：让大脑看到的不是一堆 json，而是类似：

> “TSLA 10m horizon：最近 3 次实验中，XGBoost + VWAP/ATR 组合 Sharpe > 1.3；
> 数据质量 OK；sandbox 特征中 `ma_close_20_minus_vwap` 表现稳定。”

先做个简化版 summary builder：

```python
# experiments/summary.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .registry import ExperimentRegistry
from .types import ExperimentRun


@dataclass
class ExperimentSummary:
    """
    面向 LLM 的摘要：一条 summary 对应一条 ExperimentRun 的“精简可读版”。
    """

    run_id: str
    created_at: str
    symbol: str
    horizon: str
    model_type: str
    key_metrics: Dict[str, float]
    top_features: List[str]
    sandbox_features: List[str]
    data_quality_note: str
    decision_note: str

    def to_prompt_snippet(self) -> str:
        # 转成可嵌入 prompt 的自然语言片段
        metrics_str = ", ".join(
            f"{k}={v:.3f}" for k, v in self.key_metrics.items()
        )
        feats_str = ", ".join(self.top_features[:10]) if self.top_features else "N/A"
        sandbox_str = ", ".join(self.sandbox_features[:10]) if self.sandbox_features else "None"

        txt = (
            f"- run_id={self.run_id} | {self.created_at} | {self.symbol} | horizon={self.horizon}\n"
            f"  model={self.model_type}, metrics: {metrics_str}\n"
            f"  top_features: {feats_str}\n"
            f"  sandbox_features: {sandbox_str}\n"
            f"  data_quality: {self.data_quality_note}\n"
            f"  decision_cfg: {self.decision_note}\n"
        )
        return txt


class ExperimentSummarizer:
    def __init__(self, registry: ExperimentRegistry) -> None:
        self._registry = registry

    def build_summaries_for_context(
        self,
        *,
        symbol: str,
        horizon: str,
        limit: int = 5,
    ) -> List[ExperimentSummary]:
        """
        按时间倒序取最近的 N 次实验，构建可供大脑阅读的 summary 列表。
        """

        runs = self._registry.find(symbol=symbol, horizon=horizon)
        # 按 created_at 降序
        runs = sorted(runs, key=lambda r: r.created_at, reverse=True)[:limit]

        return [self._summarize_run(r) for r in runs]

    def _summarize_run(self, run: ExperimentRun) -> ExperimentSummary:
        key_metrics = {}
        for m in run.metrics:
            # 你可以按需要过滤，只挑 sharpe / win_rate / max_dd 等关键项
            key_metrics[m.name] = m.value

        # 简单按 importance 排序选前 10 个特征
        sorted_feats = sorted(
            run.features,
            key=lambda f: (f.importance if f.importance is not None else 0.0),
            reverse=True,
        )
        top_features = [f.name for f in sorted_feats[:10]]

        sandbox_feats = [s.name for s in run.sandbox_features]

        # data_quality_note: 从 dataset_summary 生成一行短句
        if run.dataset_summary.has_fatal_issues:
            data_note = "FATAL issues in dataset (see issues)."
        elif run.dataset_summary.issues:
            data_note = "Minor issues: " + "; ".join(
                i.get("msg", i.get("code", "issue")) for i in run.dataset_summary.issues
            )
        else:
            data_note = "Data quality OK."

        decision_note = ""
        mode = run.decision_cfg.get("mode")
        if mode == "quantile":
            decision_note = (
                f"quantile decision (q_long={run.decision_cfg.get('q_long')}, "
                f"q_short={run.decision_cfg.get('q_short')})"
            )
        elif mode:
            decision_note = f"decision mode={mode}"
        else:
            decision_note = "decision config not specified."

        return ExperimentSummary(
            run_id=run.run_id,
            created_at=run.created_at,
            symbol=run.symbol,
            horizon=run.horizon,
            model_type=run.model_type,
            key_metrics=key_metrics,
            top_features=top_features,
            sandbox_features=sandbox_feats,
            data_quality_note=data_note,
            decision_note=decision_note,
        )
```

> 以后在 AI 大脑构建 prompt 时，就可以做类似：

```python
summarizer = ExperimentSummarizer(registry)
summaries = summarizer.build_summaries_for_context(symbol="TSLA", horizon="10m")

context_snippets = "\n".join(s.to_prompt_snippet() for s in summaries)
# 把 context_snippets 塞到 system_prompt/user_prompt 的某个“历史实验经验”字段里
```

---

## 四、在 HPO 结束时，**真正落一条 ExperimentRun**

现在我们假设你有一个 HPO 流程，大概长这样（伪代码）：

```python
# hpo/runner.py（示意）

from experiments.registry import ExperimentRegistry
from experiments.types import (
    ExperimentRun,
    Metric,
    FeatureUsage,
    DatasetSummary,
    SandboxFeatureRecord,
)
from utils.uuid import uuid7  # 假设你有，或者用 uuid.uuid4()

def run_hpo_and_log(
    *,
    pi_result,       # PIResult
    dataset_sanity,  # DatasetSanityReport
    model_trainer,   # 封装好的 HPO + 训练逻辑
    registry: ExperimentRegistry,
) -> Any:
    """
    1. 用 pi_result.hpo_grid 跑 HPO
    2. 选出 best_trial / best_model / metrics
    3. 写入 ExperimentRegistry
    """

    df = pi_result.dataset_df
    grid = pi_result.hpo_grid

    # 1) 跑 HPO（这里用伪代码占位）
    hpo_result = model_trainer.run(
        df=df,
        hpo_grid=grid,
    )
    # 假设 hpo_result 提供：
    # - best_model_type
    # - best_model_params
    # - best_metrics: dict[str, float]
    # - feature_importances: dict[feature_name, importance]

    # 2) 构造 ExperimentRun
    now = datetime.utcnow().isoformat()
    run_id = hpo_result.run_id  # 如果 HPO 系统自己有 id 就用它，否则用 uuid

    # DatasetSummary 从 DatasetSanityReport 来（接口按你真实实现调整）
    ds = DatasetSummary(
        dataset_name=dataset_sanity.dataset_name,
        n_rows=dataset_sanity.n_rows,
        n_cols=dataset_sanity.n_cols,
        issues=[i.to_dict() for i in dataset_sanity.issues],
        has_fatal_issues=dataset_sanity.has_fatal_issues,
        meta={},
    )

    # 特征 usage（包括 sandbox 与非 sandbox）
    features_usage: List[FeatureUsage] = []
    fi = hpo_result.feature_importances or {}
    for feat in pi_result.feature_recipe.get("base_features", []):
        features_usage.append(
            FeatureUsage(
                name=feat,
                kind="base",
                is_sandbox=False,
                importance=fi.get(feat),
            )
        )
    for d in pi_result.feature_recipe.get("derived", []):
        alias = d.get("alias")
        if not alias:
            continue
        is_sandbox = False
        if pi_result.sandbox_features:
            is_sandbox = any(sf.name == alias for sf in pi_result.sandbox_features)
        features_usage.append(
            FeatureUsage(
                name=alias,
                kind="derived",
                is_sandbox=is_sandbox,
                importance=fi.get(alias),
            )
        )

    # sandbox 特征 detail 记录
    sandbox_records: List[SandboxFeatureRecord] = []
    for sf in pi_result.sandbox_features or []:
        sandbox_records.append(
            SandboxFeatureRecord(
                name=sf.name,
                expr=sf.expr,
                kind=sf.kind.value if hasattr(sf, "kind") else "derived",
                created_by=sf.created_by,
                meta=sf.meta,
            )
        )

    # metrics 列表
    metrics = [
        Metric(name=k, value=v, higher_is_better=True)
        for k, v in (hpo_result.best_metrics or {}).items()
    ]

    # label_def：你可以从 training_plan/recipe 或者固定模板拿
    label_def = pi_result.training_plan.get("label_def") or {
        "type": "ret_fwd",
        "horizon": pi_result.training_plan.get("horizon", None),
        "column": pi_result.training_plan.get("target", None),
    }

    decision_cfg = pi_result.training_plan.get("decision") or {}

    exp = ExperimentRun(
        run_id=run_id,
        created_at=now,
        symbol=pi_result.training_plan.get("symbol", None) or "UNKNOWN",
        table=pi_result.feature_recipe.get("use_table", None) or "UNKNOWN",
        start_ts=pi_result.training_plan.get("start_ts", ""),
        end_ts=pi_result.training_plan.get("end_ts", ""),
        horizon=pi_result.training_plan.get("horizon", "UNKNOWN"),

        label_def=label_def,
        dataset_summary=ds,
        features=features_usage,
        sandbox_features=sandbox_records,

        model_type=hpo_result.best_model_type,
        model_params=hpo_result.best_model_params,
        hpo_config=hpo_result.hpo_config or {},
        decision_cfg=decision_cfg,

        metrics=metrics,
        meta={
            "pi_goal": pi_result.training_plan.get("goal"),
            "pi_prompt_id": pi_result.training_plan.get("prompt_id"),
        },
    )

    # 3) 写入 registry
    registry.append(exp)

    return hpo_result
```

> 这段是**对接点示意**：你不需要照抄字段名，但流程一定要有这三步：
>
> 1. PI + HPO 把该有的东西都算完
> 2. 把数据质量 / 特征 / sandbox / 模型 / 指标打包成一个 `ExperimentRun`
> 3. `ExperimentRegistry.append(exp)` 持久化

---

## 五、现在这条链上已经具备的能力

到这里，**PI → HPO → ExperimentRegistry** 这一条链在设计上已经具备：

1. **数据层**

   * DatasetSanity（之前做的） + `DatasetSummary` 写进 ExperimentRun
   * Sandbox 特征引擎：所有新特征可追溯 & 有体检记录

2. **机器学习 / HPO / 实验治理层**

   * 每次 HPO 有一个 run_id，对应一条 `ExperimentRun`
   * 指标、特征重要度、sandbox 使用情况都能追溯
   * 以后可以基于 `experiments.jsonl` 做统计：

     * 哪些 sandbox 特征反复出现在高 Sharpe 实验中
     * 哪些 label_def/decision_cfg 组合更稳

3. **AI 大脑 & Prompt 安全层（初步）**

   * `ExperimentSummarizer` 可以给 AI 大脑提供“历史实验经验摘要”
   * 下一步可以做：

     * 在构造 AI 大脑的 prompt 时，只允许它在 `allowed_run_ids`、`allowed_feature_aliases` 范围内选策略/特征
     * 用实验数据教它：“哪些表达式/特征在历史上被频繁拒绝/表现很差”

---

如果你愿意，下一步我可以继续往上走一层：

* 把 **ExperimentSummarizer** 和你 prompt 里 `execution_prompt_id` / `tactical_xxx` 那套结合起来，
* 设计一版：**“AI 大脑如何根据 ExperimentRegistry 自动选择策略 / 特征 / prompt 版本”** 的安全决策流程。
好，我们现在顺着刚才那条链往上走一层：
在 **「PI → HPO → ExperimentRegistry」已经打通** 的基础上，把这一条线接到 **AI 大脑 & Prompt 安全** 上，让“大脑”只能在 **已知实验 / 已知策略 / 已知 prompt** 里做选择，而不是乱发明。

我这一步会做三件事：

1. 定义 **BrainDecision 决策 Schema**（大脑输出什么）
2. 定义 **安全的 Prompt 上下文构建器**（大脑能看到什么）
3. 定义 **Selector 流程**：如何调用 LLM → 校验 → 得到一个安全的决策对象

> 这一整块都是 Phase 4A 范围内：只负责“想清楚 + 决策”，不负责真正下单。

---

## 一、BrainDecision：AI 大脑的“唯一合法输出格式”

我们先定一个最小但实用的决策结构：

* 大脑不能直接写 SQL 或特征表达式
* 只能从：

  * 已存在的 **ExperimentRun.run_id** 列表里选一个
  * 已存在的 **prompt library** 里选一个 `execution_prompt_id`
* 决策里附带一点配置（风险偏好、是否允许加 sandbox 特征等）

```python
# brain/types.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class BrainDecision:
    """
    AI 大脑的“唯一合法输出”：
    - 只能在 allowed_run_ids 中选策略
    - 只能在 allowed_prompt_ids 中选执行 prompt
    - 不允许写自由文本 SQL/特征
    """

    # 必选：使用哪个历史实验作为“策略模板”
    chosen_run_id: str

    # 必选：使用哪个 execution_prompt_id（来自 prompt_library）
    execution_prompt_id: str

    # 可选：策略运行级别的风险配置（交给下游执行器解释）
    risk_mode: str = "normal"  # "normal" / "conservative" / "aggressive"
    max_position_fraction: float = 0.3  # 单策略最多用多少仓位

    # 是否允许在这个策略的基础上启用 sandbox 特征
    allow_sandbox_features: bool = False

    # 大脑解释（方便日志与审计）
    rationale: str = ""

    # 以后扩展用
    extra: Dict[str, Any] = field(default_factory=dict)
```

---

## 二、BrainDecision 校验器：**强约束 + 明确报错**

大脑输出 JSON 后，必须经过一个“闸门”：

* `chosen_run_id` 必须在 `allowed_run_ids` 里
* `execution_prompt_id` 必须在 `allowed_prompt_ids` 里
* 数值要在合理范围，比如 `max_position_fraction ∈ (0,1]`

```python
# brain/validator.py

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set, Tuple

from .types import BrainDecision


class BrainDecisionValidationError(Exception):
    pass


class BrainDecisionValidator:
    """
    负责把 LLM 输出的原始 JSON → BrainDecision（带强校验）
    """

    def __init__(
        self,
        *,
        allowed_run_ids: Iterable[str],
        allowed_prompt_ids: Iterable[str],
    ) -> None:
        self.allowed_run_ids: Set[str] = set(allowed_run_ids)
        self.allowed_prompt_ids: Set[str] = set(allowed_prompt_ids)

    def parse_and_validate(self, raw: Dict[str, Any]) -> BrainDecision:
        """
        raw: LLM 输出的 JSON（已经是 dict）
        返回: 通过校验的 BrainDecision，否则抛异常
        """
        errors: List[str] = []

        chosen_run_id = raw.get("chosen_run_id")
        if not isinstance(chosen_run_id, str):
            errors.append("chosen_run_id must be a string")
        elif chosen_run_id not in self.allowed_run_ids:
            errors.append(
                f"chosen_run_id={chosen_run_id!r} not in allowed_run_ids"
            )

        execution_prompt_id = raw.get("execution_prompt_id")
        if not isinstance(execution_prompt_id, str):
            errors.append("execution_prompt_id must be a string")
        elif execution_prompt_id not in self.allowed_prompt_ids:
            errors.append(
                f"execution_prompt_id={execution_prompt_id!r} not in allowed_prompt_ids"
            )

        risk_mode = raw.get("risk_mode", "normal")
        if risk_mode not in ("normal", "conservative", "aggressive"):
            errors.append(
                f"risk_mode must be one of ['normal','conservative','aggressive'], got {risk_mode!r}"
            )

        max_pos = raw.get("max_position_fraction", 0.3)
        try:
            max_pos_f = float(max_pos)
        except Exception:
            errors.append("max_position_fraction must be a number")
            max_pos_f = 0.3
        else:
            if not (0.0 < max_pos_f <= 1.0):
                errors.append("max_position_fraction must be in (0,1]")

        allow_sandbox = bool(raw.get("allow_sandbox_features", False))
        rationale = raw.get("rationale", "")
        if not isinstance(rationale, str):
            rationale = str(rationale)

        extra = raw.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {"_raw_extra": extra}

        if errors:
            raise BrainDecisionValidationError(
                "Invalid BrainDecision: " + "; ".join(errors)
            )

        return BrainDecision(
            chosen_run_id=chosen_run_id,
            execution_prompt_id=execution_prompt_id,
            risk_mode=risk_mode,
            max_position_fraction=max_pos_f,
            allow_sandbox_features=allow_sandbox,
            rationale=rationale,
            extra=extra,
        )
```

> 这一步非常关键：**无论 LLM 再怎么“发疯”，只要过不了这个 validator，就不会进到实盘。**

---

## 三、给 AI 大脑构造 Prompt 的“安全上下文”：`BrainContextBuilder`

这个 builder 做几件事：

1. 从 `ExperimentRegistry` 拉出 **历史成功实验的 summary**（刚刚的 `ExperimentSummarizer`）
2. 把当前任务的约束写清楚：

   * symbol / horizon / 当天市场状态
   * 允许选择的 run_id 列表
   * 允许选择的 execution_prompt_id 列表
3. 把 **BrainDecision 的 JSON Schema**（简化版）嵌入_prompt，让 LLM 按 schema 输出

```python
# brain/context_builder.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from experiments.registry import ExperimentRegistry
from experiments.summary import ExperimentSummarizer
from .types import BrainDecision


@dataclass
class BrainContext:
    """
    给 LLMClient 用的 prompt 结构：
    - system_prompt: 角色+约束
    - user_prompt: 当前任务 + 历史实验摘要 + 约束列表
    """

    system_prompt: str
    user_prompt: str
    allowed_run_ids: List[str]
    allowed_prompt_ids: List[str]


class BrainContextBuilder:
    def __init__(
        self,
        *,
        experiment_registry: ExperimentRegistry,
        prompt_library: Dict[str, str],  # id -> prompt 文本（比如你现在的 prompt json）
    ) -> None:
        self._registry = experiment_registry
        self._summarizer = ExperimentSummarizer(experiment_registry)
        self._prompt_library = prompt_library

    def build_for_symbol(
        self,
        *,
        symbol: str,
        horizon: str,
        market_snapshot: Dict[str, Any],
        allowed_prompt_ids: List[str],
        max_experiment_summaries: int = 5,
    ) -> BrainContext:
        """
        根据 symbol + horizon + 市场快照，为 AI 大脑构造一次“策略选择”的上下文。
        """

        # 1) 拉最近 N 条实验 summary
        summaries = self._summarizer.build_summaries_for_context(
            symbol=symbol,
            horizon=horizon,
            limit=max_experiment_summaries,
        )
        summary_text = "\n".join(s.to_prompt_snippet() for s in summaries)
        allowed_run_ids = [s.run_id for s in summaries]

        # 2) system_prompt：角色 + 输出要求 + JSON Schema
        schema_desc = self._build_brain_decision_schema_text()

        system_prompt = (
            "You are the strategies director of an intraday trading AI platform. "
            "Your job is NOT to invent new models or features, but to choose the best existing strategy configuration "
            "for the given symbol and horizon.\n\n"
            "You MUST obey the following rules:\n"
            "1. Only choose from the allowed_run_ids and allowed_prompt_ids that will be provided.\n"
            "2. NEVER invent run_ids or prompt_ids that are not in the allowed lists.\n"
            "3. Your output MUST be a single JSON object matching the BrainDecision schema.\n\n"
            "BrainDecision schema (JSON):\n"
            f"{schema_desc}\n"
        )

        # 3) user_prompt：当前任务 + 历史实验摘要 + 约束列表
        market_desc = self._format_market_snapshot(market_snapshot)

        user_prompt = (
            f"Symbol: {symbol}\n"
            f"Horizon: {horizon}\n"
            f"Current market snapshot:\n{market_desc}\n\n"
            "Historical experiment summaries (recent strategies):\n"
            f"{summary_text}\n\n"
            "Allowed strategy run_ids:\n"
            f"{allowed_run_ids}\n\n"
            "Allowed execution_prompt_ids:\n"
            f"{allowed_prompt_ids}\n\n"
            "Your task:\n"
            "- Select ONE run_id from allowed_run_ids as the base strategy.\n"
            "- Select ONE execution_prompt_id from allowed_prompt_ids.\n"
            "- Set a suitable risk_mode and max_position_fraction based on the market snapshot and metrics.\n"
            "- Decide whether sandbox features are allowed for this run.\n"
            "- Provide a brief rationale.\n\n"
            "Return ONLY a JSON object with fields: "
            "chosen_run_id, execution_prompt_id, risk_mode, max_position_fraction, "
            "allow_sandbox_features, rationale, extra.\n"
        )

        return BrainContext(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            allowed_run_ids=allowed_run_ids,
            allowed_prompt_ids=allowed_prompt_ids,
        )

    # --------------- 辅助：schema 文本 & 市场快照格式化 --------------- #

    def _build_brain_decision_schema_text(self) -> str:
        """
        这里用“人读得懂”的 JSON schema（不是严格 JSON Schema 标准），主要给 LLM 看。
        """
        return (
            '{\n'
            '  "chosen_run_id": "string, MUST be one of allowed_run_ids",\n'
            '  "execution_prompt_id": "string, MUST be one of allowed_prompt_ids",\n'
            '  "risk_mode": "string, one of [\\"normal\\", \\"conservative\\", \\"aggressive\\"]",\n'
            '  "max_position_fraction": "number in (0, 1]",\n'
            '  "allow_sandbox_features": "boolean",\n'
            '  "rationale": "string, brief explanation in English or Chinese",\n'
            '  "extra": "object, optional additional info"\n'
            '}\n'
        )

    def _format_market_snapshot(self, snap: Dict[str, Any]) -> str:
        """
        把市场快照 dict 转成人类/LLM都能读的多行字符串。
        你可以以后用更复杂格式，这里先简单实现。
        """
        lines = []
        for k, v in snap.items():
            lines.append(f"- {k}: {v}")
        return "\n".join(lines)
```

> 这里已经把你 prompt-library 那一套（比如 `v3_fast_market_following`, `conservative` 等）纳入了“允许列表”。
> 以后你只要在调用时传入 `allowed_prompt_ids=["v3_stable", "v3_fast_market_following_tactical_20251117", ...]`，大脑就只能在这些 ID 中选。

---

## 四、真正调用：BrainStrategySelector

最后做一个完整的 Selector，把上面的东西串起来：

* 给它：symbol / horizon / 市场快照
* 它：构造 BrainContext → 调 LLM → parse JSON → validator 校验 → 返回 BrainDecision

```python
# brain/selector.py

from __future__ import annotations

import json
from typing import Any, Dict, List

from llm.llm_client import LLMClient

from experiments.registry import ExperimentRegistry
from .context_builder import BrainContextBuilder
from .validator import BrainDecisionValidator, BrainDecisionValidationError
from .types import BrainDecision


class BrainStrategySelector:
    def __init__(
        self,
        *,
        experiment_registry: ExperimentRegistry,
        prompt_library: Dict[str, str],  # id -> prompt 文本
        llm_client: LLMClient,
    ) -> None:
        self._registry = experiment_registry
        self._prompt_library = prompt_library
        self._llm = llm_client
        self._context_builder = BrainContextBuilder(
            experiment_registry=experiment_registry,
            prompt_library=prompt_library,
        )

    def select_strategy(
        self,
        *,
        symbol: str,
        horizon: str,
        market_snapshot: Dict[str, Any],
        allowed_prompt_ids: List[str],
        max_experiment_summaries: int = 5,
    ) -> BrainDecision:
        """
        顶层接口：
        - 构造 context
        - 调 LLM 输出 JSON
        - 验证合法性
        - 返回 BrainDecision
        """

        ctx = self._context_builder.build_for_symbol(
            symbol=symbol,
            horizon=horizon,
            market_snapshot=market_snapshot,
            allowed_prompt_ids=allowed_prompt_ids,
            max_experiment_summaries=max_experiment_summaries,
        )

        # 如果没有可用实验，直接抛错或返回一个“不要交易”的决策（看你习惯）
        if not ctx.allowed_run_ids:
            raise RuntimeError(
                f"No experiment runs available for symbol={symbol}, horizon={horizon}"
            )

        validator = BrainDecisionValidator(
            allowed_run_ids=ctx.allowed_run_ids,
            allowed_prompt_ids=ctx.allowed_prompt_ids,
        )

        # 1) 调用 LLM
        raw = self._llm.generate(
            system_prompt=ctx.system_prompt,
            user_prompt=ctx.user_prompt,
            # 你也可以在 llm_client 里指定 "response_format": "json_object" 等
        )

        # 确保 raw 是 dict（如果是 str 就先 json.loads）
        if isinstance(raw, str):
            try:
                raw_json = json.loads(raw)
            except Exception as e:
                raise BrainDecisionValidationError(
                    f"LLM output is not valid JSON: {e}"
                )
        elif isinstance(raw, dict):
            raw_json = raw
        else:
            raise BrainDecisionValidationError(
                f"LLM output must be JSON object or JSON string, got {type(raw)}"
            )

        # 2) 校验 + 转换
        decision = validator.parse_and_validate(raw_json)
        return decision
```

> 至此，你的大脑在“选策略”阶段已经被关进了一个 **非常窄但安全的走廊**：
>
> * 它只能在 `ExperimentRegistry` 里的 run_id 之间挑
> * 它只能在 `prompt_library` 里的 prompt_id 之间挑
> * 再蠢，也不可能突然写一条乱七八糟的 SQL 或新指标跑到实盘去

---

## 五、现在三层是怎么串起来的？

完整的链现在变成这样：

1. **PI 层**

   * 调 ClickHouse，拿到 `dataset_df`
   * 通过 LLM 生成 feature_recipe + training_plan（但已经受 FeatureRegistry / LabelContract 限制）
   * 返回 `PIResult`（包含 hpo_grid）

2. **HPO / 训练 层**

   * 用 `PIResult.hpo_grid` 跑 HPO
   * 得到 best_model / best_metrics / feature_importances
   * 同时把 DatasetSanityReport、feature usage、sandbox 特征记录等打包成一个 `ExperimentRun`
   * 调 `ExperimentRegistry.append(exp)` 写入 JSONL

3. **AI 大脑层（现在这一步做的）**

   * `BrainStrategySelector.select_strategy(...)`：

     * 通过 `ExperimentSummarizer` 拉最近 N 个 run 的 summary
     * 构造包含 `allowed_run_ids` + `allowed_prompt_ids` 的 prompt
     * 调 LLM 拿一个 JSON 决策
     * 用 `BrainDecisionValidator` 做强校验
     * 返回 `BrainDecision`（只包含 run_id + prompt_id + 风险设定）

4. **执行层（Phase 4B，以后再接）**

   * 根据 `BrainDecision.chosen_run_id` → 从 registry 或 model store 里加载对应模型 & 策略配置
   * 根据 `execution_prompt_id` → 加载对应 execution prompt，让“执行 agent”来生成具体下单指令（但也在硬 gates 里）
   * 把这些结果送给 IBKR / paper account 执行

---

如果你愿意，下一步我可以：

* 直接以你现有的 prompt-library JSON（那段很长的 `v3_xxx`、`prompts`、`meta`）为例，写一个 `load_prompt_library()` + `allowed_prompt_ids` 选取策略，比如：

  * 正式交易日：只允许 `v3_stable` + 最近 3 天的 `v3_fast_market_following_tactical_xxx_v2`
  * 实验模式：允许 `v4_experimental` 这种更激进的 prompt，但仍然在白名单里。
