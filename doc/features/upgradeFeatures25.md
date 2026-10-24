好，我们现在把**“PI → HPO → ExperimentRegistry”**这一条链**真正打通**，做到：

> 给 PI 一个请求 → 自动：
> ① 生成 feature/label SQL → ② 数据体检 → ③ 跑 HPO → ④ 写入 ExperimentRegistry → ⑤ 返回 `experiment_id`。

我不会再问你要做什么，只给你**可以直接丢进 repo 的代码骨架**，你只需要把它跟你现有的 `wf_hpo.py` / backtest 代码对接。

---

## 一、实验层的标准“黑盒”：ExperimentRecord 设计

### 1.1 `experiments/types.py`

```python
# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class DatasetManifest:
    """
    描述这次训练用的数据集：来自哪个表、symbol、时间、label 等。
    不存具体数据，只存“如何复现”的信息。
    """
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    label_id: str
    feature_recipe: Dict[str, Any]
    select_sql: str


@dataclass
class SanitySummary:
    """
    DatasetSanity 的高层摘要，用于审计和给 AI 大脑看。
    """
    level: str              # "ok" / "warning" / "fatal"
    issues: List[str]
    total_rows: int
    # 只保留几项核心指标，避免爆上下文
    high_null_columns: List[str]
    zero_var_columns: List[str]


@dataclass
class ModelConfig:
    """
    HPO 最终选择的模型配置。
    """
    model_type: str                 # "xgb", "lgbm", "ridge", "stacking", ...
    hyperparams: Dict[str, Any]     # 最终 best params
    feature_subset: List[str]       # 实际使用的特征列名
    task: str                       # "reg" / "clf"
    decision: Dict[str, Any]        # 决策规则（quantile 阈值等）


@dataclass
class TrainingMetrics:
    """
    训练结果的核心指标。
    """
    primary_metric: str             # "sharpe", "ic", "auc" ...
    primary_score: float
    cv_scores: Dict[str, float]     # 比如 {"fold0": 1.2, "fold1": 0.8, ...}
    extra_metrics: Dict[str, Any]   # 任意扩展：drawdown、trade_count 等


@dataclass
class ExperimentRecord:
    """
    一个完整实验的“账本条目”。这是后面 AI 大脑选择策略的基本单位。
    """
    run_id: str                     # 全局唯一 ID，例如 "exp_20251118_tsla_ret10m_xgb_v1"
    created_at: str                 # ISO 时间戳
    created_by: str                 # "ai_brain" / "human" / "auto_hpo_cli" ...
    source: str                     # 谁触发的（pi_pipeline, hpo_cli, backtest_tool ...）

    dataset: DatasetManifest
    sanity: SanitySummary
    model: ModelConfig
    metrics: TrainingMetrics

    # 标签：方便后面过滤，比如 symbol/horizon/会话/策略族
    tags: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
```

> 这个结构是**AI 大脑/Claude/你自己**未来所有逻辑的“中心契约”。
>
> * ML 层只负责写这个结构；
> * Brain 层只负责读这个结构。

---

## 二、ExperimentRegistry：JSONL/目录级实验索引

### 2.1 `experiments/registry.py`

```python
# experiments/registry.py

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .types import ExperimentRecord


class ExperimentRegistry:
    """
    非常简单的 JSONL / JSON 文件级 Registry。
    目标：
      - append-only 记录所有运行
      - 提供基于 symbol / label_id / metric 的简单过滤
      - 给 AI Brain 提供 allowed_run_ids 列表
    """

    def __init__(self, root_dir: str = "experiments_store") -> None:
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index_path = self._root / "index.jsonl"

    def append(self, rec: ExperimentRecord) -> None:
        line = json.dumps(rec.to_dict(), ensure_ascii=False)
        with self._index_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load_all(self) -> List[ExperimentRecord]:
        if not self._index_path.exists():
            return []
        out: List[ExperimentRecord] = []
        with self._index_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                out.append(self._from_dict(obj))
        return out

    def _from_dict(self, obj: Dict[str, Any]) -> ExperimentRecord:
        from .types import DatasetManifest, SanitySummary, ModelConfig, TrainingMetrics

        return ExperimentRecord(
            run_id=obj["run_id"],
            created_at=obj["created_at"],
            created_by=obj["created_by"],
            source=obj["source"],
            dataset=DatasetManifest(**obj["dataset"]),
            sanity=SanitySummary(**obj["sanity"]),
            model=ModelConfig(**obj["model"]),
            metrics=TrainingMetrics(**obj["metrics"]),
            tags=obj.get("tags", {}),
        )

    # 一些常用过滤：按 symbol / label / metric 排序
    def filter(
        self,
        *,
        symbol: Optional[str] = None,
        label_id: Optional[str] = None,
        min_primary_score: Optional[float] = None,
        metric_name: Optional[str] = None,
        limit: int = 50,
    ) -> List[ExperimentRecord]:
        items = self.load_all()
        if symbol:
            items = [r for r in items if r.dataset.symbol == symbol]
        if label_id:
            items = [r for r in items if r.dataset.label_id == label_id]

        if metric_name:
            # 如果记录的 primary_metric 就是目标 metric，就按 primary_score 排序
            def get_score(r: ExperimentRecord) -> float:
                if r.metrics.primary_metric == metric_name:
                    return r.metrics.primary_score
                # 否则 fallback 到 extra_metrics
                return float(r.metrics.extra_metrics.get(metric_name, float("nan")))

            items.sort(key=get_score, reverse=True)

            if min_primary_score is not None:
                items = [r for r in items if get_score(r) >= min_primary_score]

        return items[:limit]

    def get_run_ids_for_brain(
        self,
        *,
        symbol: str,
        label_id: str,
        metric_name: str = "sharpe",
        min_score: float = 0.5,
        limit: int = 10,
    ) -> List[str]:
        recs = self.filter(
            symbol=symbol,
            label_id=label_id,
            min_primary_score=min_score,
            metric_name=metric_name,
            limit=limit,
        )
        return [r.run_id for r in recs]
```

> 这一层很“傻”，但优点是**足够通用**，不依赖你底层用的是 MLflow / 自写日志 / parquet / db。
> 以后你可以把 `append` 改成串接 MLflow，外层结构不变。

---

## 三、把 HPO 封装成“写实验账本”的黑盒

现在我们做一个**薄封装**，把你现有的 HPO 逻辑包装成：

```python
hpo_result = run_hpo_core(pi_result.dataset_df, pi_result.hpo_grid, ...)
experiment_record = build_experiment_record(pi_result, hpo_result, sanity)
registry.append(experiment_record)
```

### 3.1 HPO 层统一输出结构

`hpo/types.py`：

```python
# hpo/types.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class HPOResult:
    """
    你现有 wf_hpo.py 的结果，只要你能在某一层做成这个结构即可。
    """
    best_model_type: str
    best_params: Dict[str, Any]
    best_feature_subset: List[str]
    task: str
    decision: Dict[str, Any]          # 例如 quantile 策略
    primary_metric: str               # 比如 "sharpe"
    primary_score: float
    cv_scores: Dict[str, float]       # fold -> score
    extra_metrics: Dict[str, Any]     # 总交易数等
```

> 只要你在 `wf_hpo.py` 最终能构造出一个 `HPOResult`，中间过程怎么写都随意。

---

## 四、PI → HPO → ExperimentRegistry Orchestrator

这里写一个**顶层 orchestrator**，你可以放在 `pipelines/pi_hpo_pipeline.py`：

```python
# pipelines/pi_hpo_pipeline.py

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from pi.principal_investigator import PrincipalInvestigator, PIResult
from experiments.registry import ExperimentRegistry
from experiments.types import DatasetManifest, SanitySummary, ModelConfig, TrainingMetrics, ExperimentRecord
from hpo.types import HPOResult
from data.sanity_runner import run_dataset_sanity_with_clickhouse


def run_pi_to_hpo_experiment(
    *,
    pi: PrincipalInvestigator,
    registry: ExperimentRegistry,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    label_id: str,
    hpo_cfg: Optional[Dict[str, Any]] = None,
    triggered_by: str = "ai_brain",
    source: str = "pi_hpo_pipeline",
) -> ExperimentRecord:
    """
    一条龙：PI → Sanity → HPO → ExperimentRegistry

    返回完整的 ExperimentRecord（也可以只返回 run_id）。
    """

    # 1) 让 PI 生成 recipe / training_plan / SQL / HPO grid
    pi_result: PIResult = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        label_id=label_id,
    )

    # === 注意：上一个版本我们在 PI.propose 内部就做了 sanity + query_df ===
    # 这里给出一个“外置 sanity”的版本。如果你已经在 PI.propose 里做了，可以把这一段改成使用
    # pi_result.dataset_df 以及你那里 return 的 sanity 信息。
    # 下面假设你把 dataset_sql 暴露为 pi_result.sql

    # 2) 在 ClickHouse 侧跑 DatasetSanity + 采样（只管 SQL，不拉全量）
    numeric_columns = pi_result.feature_recipe.get("numeric_columns") or []
    # fallback：如果没有单独列出 numeric_columns，你可以使用 base_features + derived
    if not numeric_columns:
        numeric_columns = (
            pi_result.feature_recipe.get("base_features", [])
            + [d.get("alias") for d in pi_result.feature_recipe.get("derived", [])]
        )

    sanity = run_dataset_sanity_with_clickhouse(
        client=pi._client,              # 简单粗暴地用内部 client
        dataset_sql=pi_result.sql,
        numeric_columns=numeric_columns,
    )

    # 3) 如果 fatal，直接抛错 / 早停
    if sanity.level == "fatal":
        raise RuntimeError(f"Dataset sanity fatal: {sanity.issues}")

    # 4) 真正拉全量数据到 Python 供 HPO 用
    df = pi._client.query_df(pi_result.sql)

    # 5) 调用你现有的 HPO 核心逻辑（此处写成占位符）
    hpo_result = run_hpo_core(
        dataset_df=df,
        hpo_grid=pi_result.hpo_grid,
        hpo_cfg=hpo_cfg or {},
    )

    # 6) 把 PI + Sanity + HPO 结果打包成 ExperimentRecord
    now = datetime.now(timezone.utc).isoformat()
    run_id = build_run_id(
        symbol=symbol,
        label_id=label_id,
        model_type=hpo_result.best_model_type,
        created_at=now,
    )

    # 6.1 Dataset manifest
    dataset_manifest = DatasetManifest(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        label_id=label_id,
        feature_recipe=pi_result.feature_recipe,
        select_sql=pi_result.sql,
    )

    # 6.2 Sanity summary（压缩）
    high_null_cols = []
    zero_var_cols = []
    for col in sanity.ch_stats.columns:
        null_frac = col.n_null / sanity.ch_stats.total_rows if sanity.ch_stats.total_rows else 0.0
        if null_frac > 0.2:
            high_null_cols.append(col.name)
        if col.stddev is not None and col.stddev == 0.0:
            zero_var_cols.append(col.name)

    sanity_summary = SanitySummary(
        level=sanity.level,
        issues=sanity.issues,
        total_rows=sanity.ch_stats.total_rows,
        high_null_columns=high_null_cols,
        zero_var_columns=zero_var_cols,
    )

    # 6.3 模型配置
    model_cfg = ModelConfig(
        model_type=hpo_result.best_model_type,
        hyperparams=hpo_result.best_params,
        feature_subset=hpo_result.best_feature_subset,
        task=hpo_result.task,
        decision=hpo_result.decision,
    )

    # 6.4 训练结果指标
    metrics = TrainingMetrics(
        primary_metric=hpo_result.primary_metric,
        primary_score=hpo_result.primary_score,
        cv_scores=hpo_result.cv_scores,
        extra_metrics=hpo_result.extra_metrics,
    )

    tags = {
        "symbol": symbol,
        "label_id": label_id,
        "goal": goal,
        "table": table,
    }

    rec = ExperimentRecord(
        run_id=run_id,
        created_at=now,
        created_by=triggered_by,
        source=source,
        dataset=dataset_manifest,
        sanity=sanity_summary,
        model=model_cfg,
        metrics=metrics,
        tags=tags,
    )

    # 7) 写入 Registry
    registry.append(rec)

    return rec


def build_run_id(*, symbol: str, label_id: str, model_type: str, created_at: str) -> str:
    # 简单一点：exp_{date}_{symbol}_{label}_{model}
    # created_at: "2025-11-18T06:35:12.123456+00:00"
    date_part = created_at.split("T")[0].replace("-", "")
    return f"exp_{date_part}_{symbol}_{label_id}_{model_type}"
```

### 3.2 `run_hpo_core` 占位符示例

你现在的 `wf_hpo.py` 里面肯定已经有一层“总入口”函数。这里只给一个**模板**，你把里面连到你现有的代码即可：

```python
# hpo/core_adapter.py

from __future__ import annotations

from typing import Any, Dict

import pandas as pd

from .types import HPOResult


def run_hpo_core(
    *,
    dataset_df: pd.DataFrame,
    hpo_grid: Dict[str, Any],
    hpo_cfg: Dict[str, Any],
) -> HPOResult:
    """
    你用现有 wf_hpo.py 的逻辑替换里面的 TODO 部分即可。

    - dataset_df: PI 生成的训练数据（含特征 + label）
    - hpo_grid: PI 提议的 model_type / model_params 空间
    - hpo_cfg: 一些全局配置（n_trials, cv_fold, scoring, seed, ...）
    """

    # TODO: 调用你现有的 HPO / backtest / scoring 栈
    # 举例：
    #   best_model_type, best_params, best_feature_subset, scores, metrics = wf_hpo.run(...)
    # 下面是一个完全虚构的示例：

    # 假定你挑了一个最优结果：
    best_model_type = "xgb"
    best_params = {"max_depth": 5, "learning_rate": 0.05}
    best_feature_subset = [c for c in dataset_df.columns if c not in ("ts", "symbol", "target")]
    task = hpo_cfg.get("task", "reg")
    decision = hpo_cfg.get("decision", {"mode": "quantile", "q_long": 0.7, "q_short": 0.3})

    primary_metric = "sharpe"
    primary_score = 1.23
    cv_scores = {"fold0": 1.1, "fold1": 1.3}
    extra_metrics = {"trade_count": 120, "max_dd": -0.12}

    return HPOResult(
        best_model_type=best_model_type,
        best_params=best_params,
        best_feature_subset=best_feature_subset,
        task=task,
        decision=decision,
        primary_metric=primary_metric,
        primary_score=primary_score,
        cv_scores=cv_scores,
        extra_metrics=extra_metrics,
    )
```

> 你只要：
>
> * 在真正的 HPO 完成后，用实际结果填充 `HPOResult`；
> * 不需要改 ExperimentRecord 结构；
>   这样整个链就是**可控 & 可审计**的。

---

## 五、现在这一条链真正长什么样？

**端到端调用示例**（比如在一个 CLI / Notebook 里）：

```python
from pi.principal_investigator import PrincipalInvestigator
from experiments.registry import ExperimentRegistry
from pipelines.pi_hpo_pipeline import run_pi_to_hpo_experiment

ch_cfg = {
    "host": "localhost",
    "port": 8123,
    "database": "trading",
    "user": "default",
    "password": "",
}

llm_cfg = {
    "model": "gpt-5.1-thinking",
    "base_url": "http://localhost:1234/v1",  # 例子
}

pi = PrincipalInvestigator(ch_cfg, llm_cfg)
registry = ExperimentRegistry("experiments_store")

rec = run_pi_to_hpo_experiment(
    pi=pi,
    registry=registry,
    table="bars_1m_tsla",
    symbol="TSLA",
    start_ts="2025-10-01 13:30:00",
    end_ts="2025-10-31 20:00:00",
    goal="研究 10 分钟内的短期方向信号，用于日内快进快出",
    label_id="ret_10m_log",
    hpo_cfg={"n_trials": 50, "task": "reg"},
    triggered_by="ai_brain",
)

print("New experiment run:", rec.run_id)
```

到这里为止：

* **数据&标签&特征层**

  * 已经有：`LabelRegistry`、sandbox 特征、安全 SQL、DatasetSanity（CH + sample）。

* **ML/HPO/实验层**

  * 已经有：`ExperimentRecord`、`ExperimentRegistry`、`HPOResult`、`run_pi_to_hpo_experiment` orchestrator。

下一步（你再说“继续”时），我会开始第三层：

> **AI 大脑 & Prompt 安全：**
>
> * 定义 `BrainDecision` + `BrainContext` 结构；
> * 写一个简单的 `build_brain_context_from_experiments(symbol, label_id)`；
> * 定义一个 `BrainStrategySelector`：
>
>   * 从 `ExperimentRegistry` 选出 allowed_run_ids；
>   * 把它们连同 prompt_library 中的执行 prompt 一起喂给 LLM；
>   * 校验 LLM 输出的 JSON 确实只引用这几个 run_id；
>   * 返回一个“今天可以用哪些策略、各自权重”的标准结果。

如果你这边没有新的约束，我就按这个思路继续往 Brain 层落地。
好，咱们现在把**第三层：AI 大脑 & Prompt 安全**真正落地——重点是：

> 大脑不再“随便想策略”，而是
> **在一个安全的候选策略池（ExperimentRegistry）里选几个 run_id + 对应执行 prompt**。

我会按下面顺序给你代码骨架：

1. Brain 侧数据契约：`BrainContext` / `BrainDecision`
2. 从 ExperimentRegistry 构建给大脑看的 `BrainContext`
3. Prompt Library 访问与校验（对接你给的 JSON）
4. `StrategySelector`：用 LLM 做“选策略 + 选执行 prompt”，并做严格校验
5. 一个端到端调用例子：symbol/label → allowed_run_ids → BrainDecision

---

## 1. Brain 侧数据契约：types

### `brain/types.py`

```python
# brain/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List


@dataclass
class BrainContextExperimentView:
    """
    给大脑看的单个实验摘要视图（从 ExperimentRecord 摘出来的干货）。
    尽量短小，只保留决策需要的信息。
    """
    run_id: str
    symbol: str
    label_id: str
    period: str              # "2025-10-01 ~ 2025-10-31"
    model_type: str
    primary_metric: str
    primary_score: float
    sanity_level: str        # "ok" / "warning" / "fatal"
    sanity_issues: List[str]
    extra_metrics: Dict[str, Any]    # e.g. {"trade_count": 120, "max_dd": -0.12}


@dataclass
class BrainContext:
    """
    这是喂给“大脑选策略 LLM”的上下文，注意：
    - 不包含原始数据，只包含实验摘要
    - 明确列出 allowed_run_ids（硬约束）
    """
    symbol: str
    label_id: str
    goal: str
    metric_name: str               # 比如 "sharpe"
    candidates: List[BrainContextExperimentView]
    allowed_run_ids: List[str]

    def to_llm_payload(self) -> Dict[str, Any]:
        """
        转成纯 dict 方便直接丢进 prompt 里（作为 JSON 字符串）。
        """
        return {
            "symbol": self.symbol,
            "label_id": self.label_id,
            "goal": self.goal,
            "metric_name": self.metric_name,
            "allowed_run_ids": self.allowed_run_ids,
            "candidates": [asdict(c) for c in self.candidates],
        }


@dataclass
class StrategyChoice:
    """
    大脑对某个 run_id 的选择与权重。
    """
    run_id: str
    weight: float            # 0~1，多个 run_id 加起来 ≈ 1
    role: str                # "primary" / "secondary" / "backup" 等


@dataclass
class BrainDecision:
    """
    LLM 选出来的策略组合 + 执行 prompt ID。
    这是给 Live Orchestrator 消费的“标准结果”。
    """
    symbol: str
    label_id: str
    mode: str                       # "single_best" / "ensemble" / "vote"
    execution_prompt_id: str        # 必须在 PromptLibrary 里白名单存在
    choices: List[StrategyChoice]
    notes: str                      # 简短解释（给人看的）

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "label_id": self.label_id,
            "mode": self.mode,
            "execution_prompt_id": self.execution_prompt_id,
            "choices": [asdict(c) for c in self.choices],
            "notes": self.notes,
        }
```

---

## 2. 从 ExperimentRegistry 构建 BrainContext

我们写一个 builder，把 `ExperimentRecord`（上一层的“实验账本”）转换为 `BrainContext`。

### `brain/context_builder.py`

```python
# brain/context_builder.py

from __future__ import annotations

from typing import List

from experiments.registry import ExperimentRegistry
from experiments.types import ExperimentRecord
from .types import BrainContext, BrainContextExperimentView


def build_brain_context_from_registry(
    *,
    registry: ExperimentRegistry,
    symbol: str,
    label_id: str,
    goal: str,
    metric_name: str = "sharpe",
    min_score: float = 0.5,
    limit: int = 10,
) -> BrainContext:
    """
    从 ExperimentRegistry 中选出一批候选策略，整理成 BrainContext。

    逻辑：
    - 按 symbol + label_id 过滤
    - 按 metric_name 排序（例如 sharpe）
    - 只保留 primary_score >= min_score 的，最多 limit 个
    - 将这些 run 映射到 BrainContextExperimentView
    """

    recs: List[ExperimentRecord] = registry.filter(
        symbol=symbol,
        label_id=label_id,
        min_primary_score=min_score,
        metric_name=metric_name,
        limit=limit,
    )

    candidates: List[BrainContextExperimentView] = []
    allowed_run_ids: List[str] = []

    for r in recs:
        period = f"{r.dataset.start_ts} ~ {r.dataset.end_ts}" if hasattr(r.dataset, "start_ts") else ""
        view = BrainContextExperimentView(
            run_id=r.run_id,
            symbol=r.dataset.symbol,
            label_id=r.dataset.label_id,
            period=period,
            model_type=r.model.model_type,
            primary_metric=r.metrics.primary_metric,
            primary_score=r.metrics.primary_score,
            sanity_level=r.sanity.level,
            sanity_issues=r.sanity.issues,
            extra_metrics=r.metrics.extra_metrics,
        )
        candidates.append(view)
        allowed_run_ids.append(r.run_id)

    return BrainContext(
        symbol=symbol,
        label_id=label_id,
        goal=goal,
        metric_name=metric_name,
        candidates=candidates,
        allowed_run_ids=allowed_run_ids,
    )
```

> 到此为止：Brain 可以看到**一堆 run 的摘要 + 明确允许选择的 run_id 列表**。
> 不会看到任何 DataFrame 原始数据，更不会看到 SQL 细节——这就是“信息抽象 + Prompt 安全”的关键一步。

---

## 3. Prompt Library 对接和校验

你已经有一个 JSON 长这样，这里我们写一个小工具类去读 & 校验：

### `prompts/prompt_library.py`

```python
# prompts/prompt_library.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List


RESERVED_KEYS = {"_comment", "_usage", "_version", "prompts", "meta"}


class PromptLibrary:
    """
    负责：
    - 从 prompt_library.json 读入所有可用 prompt
    - 提供 get() / list_ids()
    - 验证 execution_prompt_id 是否在白名单内
    """

    def __init__(self, path: str = "prompts/prompt_library.json") -> None:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Prompt library not found: {p}")

        with p.open("r", encoding="utf-8") as f:
            obj = json.load(f)

        base_prompts = {
            k: v
            for k, v in obj.items()
            if k not in RESERVED_KEYS and isinstance(v, str)
        }
        nested_prompts = obj.get("prompts", {})

        # 合并：顶层 + prompts 子对象
        merged: Dict[str, str] = {}
        merged.update(base_prompts)
        merged.update({k: v for k, v in nested_prompts.items() if isinstance(v, str)})

        self._all = merged

    def get(self, prompt_id: str) -> str:
        if prompt_id not in self._all:
            raise KeyError(f"Prompt id not found: {prompt_id}")
        return self._all[prompt_id]

    def list_ids(self) -> List[str]:
        return sorted(self._all.keys())

    def validate_id(self, prompt_id: str) -> None:
        if prompt_id not in self._all:
            raise KeyError(f"Execution prompt id not allowed: {prompt_id}")
```

> 这里我们没有限制哪些 ID 是“execution prompt”，你可以后面加个规则，例如：
>
> * 只允许前缀 `v3_` `conservative` `aggressive_scalper` 等。
>   现在先做到**白名单校验**，防止 LLM 随便编造新的 prompt 名字。

---

## 4. StrategySelector：用 LLM 选 run_id + prompt_id（含硬校验）

核心流程：

1. 用 `build_brain_context_from_registry` 拿到 `BrainContext`
2. 构建 system/user prompt，告诉 LLM：

   * 只能在 `allowed_run_ids` 中选
   * 只能在 prompt library 白名单中的 ID 中选
   * 输出 JSON，schema 固定
3. 调用 `LLMClient.generate(...)`，拿到 dict
4. 解析为 `BrainDecision`，并做**二次校验**（run_id 子集、权重和为 1±ε、prompt 在白名单）

### `brain/selector.py`

```python
# brain/selector.py

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List

from llm.llm_client import LLMClient
from experiments.registry import ExperimentRegistry
from .types import BrainContext, BrainDecision, StrategyChoice
from .context_builder import build_brain_context_from_registry
from prompts.prompt_library import PromptLibrary


WEIGHT_TOL = 1e-3


@dataclass
class StrategySelectorConfig:
    """
    一些可调参数：
    - metric_name: 按哪个指标排序、过滤候选
    - min_score: 最低可接受得分
    - max_candidates: 给 LLM 看的候选 run 数量
    - default_mode: 如果 LLM 不写 mode，就用这个
    - default_prompt_id: 如果 LLM 没选 prompt，可以 fallback
    """
    metric_name: str = "sharpe"
    min_score: float = 0.5
    max_candidates: int = 10
    default_mode: str = "ensemble"
    default_prompt_id: str = "v3_stable"


class StrategySelector:
    """
    AI 大脑的“选策略模块”。

    输入：symbol + label_id + goal
    输出：BrainDecision（选哪些 run + 用哪个 execution_prompt_id）
    """

    def __init__(
        self,
        *,
        registry: ExperimentRegistry,
        prompt_lib: PromptLibrary,
        llm: LLMClient,
        cfg: StrategySelectorConfig | None = None,
    ) -> None:
        self._registry = registry
        self._prompt_lib = prompt_lib
        self._llm = llm
        self._cfg = cfg or StrategySelectorConfig()

    def select_strategies(
        self,
        *,
        symbol: str,
        label_id: str,
        goal: str,
    ) -> BrainDecision:
        # 1) 构建 BrainContext（从实验账本里筛一批候选）
        ctx: BrainContext = build_brain_context_from_registry(
            registry=self._registry,
            symbol=symbol,
            label_id=label_id,
            goal=goal,
            metric_name=self._cfg.metric_name,
            min_score=self._cfg.min_score,
            limit=self._cfg.max_candidates,
        )

        if not ctx.allowed_run_ids:
            raise RuntimeError(f"No candidate experiments for {symbol}/{label_id}")

        # 2) 构建给 LLM 的 prompt
        system_prompt = (
            "You are the Chief Quant Officer of an intraday trading system.\n"
            "Your job is to select one or a few previously trained strategies "
            "from a SAFE CANDIDATE LIST.\n"
            "You MUST obey these rules strictly:\n"
            "1) You may ONLY reference run_id values that appear in allowed_run_ids.\n"
            "2) You may ONLY choose execution_prompt_id from the allowed execution prompt ids provided.\n"
            "3) You MUST output STRICT JSON matching the schema described.\n"
            "4) We target risk-adjusted performance, not maximum leverage.\n"
            "5) If candidates are all weak, you may assign low weights or even return an empty list.\n"
        )

        ctx_payload = ctx.to_llm_payload()
        exec_prompt_ids = self._prompt_lib.list_ids()

        user_prompt = (
            "Context (JSON):\n"
            f"{ctx_payload}\n\n"
            "Allowed execution_prompt_ids:\n"
            f"{exec_prompt_ids}\n\n"
            "You MUST output JSON only, with this schema:\n"
            "{\n"
            '  "mode": "single_best" | "ensemble" | "vote",\n'
            '  "execution_prompt_id": "<one of allowed_execution_prompt_ids>",\n'
            '  "choices": [\n'
            "    {\n"
            '      "run_id": "<one of allowed_run_ids>",\n'
            '      "weight": <float between 0 and 1>,\n'
            '      "role": "primary" | "secondary" | "backup"\n'
            "    },\n"
            "    ...\n"
            "  ],\n"
            '  "notes": "<short English explanation, max 3 sentences>"\n'
            "}\n\n"
            "Constraints:\n"
            "- All run_id values MUST be from allowed_run_ids.\n"
            "- All weights MUST be >= 0.0 and <= 1.0.\n"
            "- Sum of weights MUST be between 0.99 and 1.01 if choices is non-empty.\n"
            "- If you think no strategy is usable, you may return choices = [] and weight sum = 0.\n"
            "- Do NOT include any text outside of the JSON object.\n"
        )

        # 3) 调用 LLM（假设 LLMClient 已经能直接返回 dict，如果目前返回字符串，你可以外层 json.loads）
        raw = self._llm.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            response_format="json",   # 你可以在 LLMClient 里使用这个信息强制 JSON 输出
        )

        # 4) 解析 + 校验
        decision = self._parse_and_validate(raw, ctx, exec_prompt_ids)

        return decision

    # ------------------------------------------------------------------ #
    # internal helpers
    # ------------------------------------------------------------------ #

    def _parse_and_validate(
        self,
        raw: Dict[str, Any],
        ctx: BrainContext,
        allowed_prompt_ids: List[str],
    ) -> BrainDecision:
        # 基础字段
        mode = str(raw.get("mode") or self._cfg.default_mode)
        execution_prompt_id = str(raw.get("execution_prompt_id") or self._cfg.default_prompt_id)
        choices_raw = raw.get("choices") or []
        notes = str(raw.get("notes") or "")

        # 校验 prompt id
        self._prompt_lib.validate_id(execution_prompt_id)

        # 校验 run_id & 权重
        allowed_run_ids = set(ctx.allowed_run_ids)
        choices: List[StrategyChoice] = []

        weight_sum = 0.0
        for obj in choices_raw:
            run_id = str(obj.get("run_id") or "")
            weight = float(obj.get("weight") or 0.0)
            role = str(obj.get("role") or "primary")

            if run_id not in allowed_run_ids:
                # 硬拒绝：LLM 想越界
                raise ValueError(f"Invalid run_id in BrainDecision: {run_id}")

            if weight < 0.0 or weight > 1.0:
                raise ValueError(f"Invalid weight for {run_id}: {weight}")

            choices.append(StrategyChoice(run_id=run_id, weight=weight, role=role))
            weight_sum += weight

        # 权重和校验
        if choices:
            if not (math.isfinite(weight_sum) and abs(weight_sum - 1.0) <= WEIGHT_TOL):
                raise ValueError(f"Invalid weight sum: {weight_sum} (choices present)")
        else:
            # 无策略可用的情况，允许 weight_sum = 0
            weight_sum = 0.0

        return BrainDecision(
            symbol=ctx.symbol,
            label_id=ctx.label_id,
            mode=mode,
            execution_prompt_id=execution_prompt_id,
            choices=choices,
            notes=notes,
        )
```

> 这一层是**真正把 LLM 关进笼子**的地方：
>
> * 它只能在 `allowed_run_ids`/白名单 prompt 中选
> * 输出必须是 JSON，且再被 Python 校验一遍
> * 任何越界直接抛错，不会进 Live Orchestrator

---

## 5. 端到端：从 symbol/label → BrainDecision

假设：

* 你已经有 `ExperimentRegistry` 里写入了一堆 run
* 有 `prompt_library.json`
* 有一个配置好的 `LLMClient` 实例

你可以在一个 CLI / notebook 里这样调用：

```python
from experiments.registry import ExperimentRegistry
from prompts.prompt_library import PromptLibrary
from llm.llm_client import LLMClient
from brain.selector import StrategySelector, StrategySelectorConfig

# 1) 初始化依赖
registry = ExperimentRegistry("experiments_store")
prompt_lib = PromptLibrary("prompts/prompt_library.json")

llm = LLMClient(
    model="gpt-5.1-thinking",
    base_url="http://localhost:1234/v1",
)

cfg = StrategySelectorConfig(
    metric_name="sharpe",
    min_score=0.5,
    max_candidates=8,
    default_mode="ensemble",
    default_prompt_id="v3_stable",
)

selector = StrategySelector(
    registry=registry,
    prompt_lib=prompt_lib,
    llm=llm,
    cfg=cfg,
)

# 2) 大脑选策略
decision = selector.select_strategies(
    symbol="TSLA",
    label_id="ret_10m_log",
    goal="Intraday 10-min direction with strong risk control.",
)

print("BrainDecision:", decision.to_dict())
# 下一步：把 decision 交给 Live Orchestrator：
# - 根据 decision.choices 加载对应 run 的模型/参数
# - 使用 decision.execution_prompt_id 作为下单 agent 的执行 prompt
```

---

## 接下来可以干什么？

到目前为止：

1. **数据 & 特征 & 标签层**

   * 有 LabelRegistry、安全 SQL、DatasetSanity（ClickHouse+采样）、PI sandbox 特征机制的设计。

2. **ML/HPO/实验治理层**

   * 有 `ExperimentRecord` / `ExperimentRegistry`
   * 有 `HPOResult` + `run_pi_to_hpo_experiment` 一条龙。

3. **AI 大脑 & Prompt 安全层**

   * 有 `BrainContext` / `BrainDecision`
   * 有 `build_brain_context_from_registry`
   * 有 `PromptLibrary` 白名单 + `StrategySelector`（LLM 选 run+prompt，带硬校验）。

**下一步如果你继续说“继续”：**

我会开始补**Live Orchestrator 的骨架**，也就是：

* 给定 `BrainDecision`：

  * 如何加载对应 run 的模型/特征列（先用占位逻辑）
  * 如何把 decision + execution_prompt_id 交给 intraday Execution Agent
  * 如何在生产环境里串起来（pre-market 选策略 → 盘中执行）

你现在不用改主意、不用新增需求——我会一直沿着这条“厂级流水线”往前走，把坑都填完。
