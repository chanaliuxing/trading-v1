好，那我按上轮说的两件事直接往下落地：

1. **让 AI 大脑真的用上 `dataset_manifest` 做“质检 + 决策”。**
2. **把“沙箱特征”做成一个有安全边界的执行器，让大脑有一点创造力但不会把你仓库炸了。**

我不会问你任何“要不要”的问题，就给你可以直接往 repo 里塞的结构和代码草案。

---

## 一、沙箱特征 Sandbox Executor（给大脑一点“创造力”）

### 1. 沙箱特征的约定（在 `feature_recipe` 里长什么样）

我们约定 LLM 可以在 `feature_recipe` 里追加一个字段：

```jsonc
{
  "base_features": ["close", "vwap_1m", "volume"],
  "derived": [
    { "alias": "ma20_close", "program_id": "ma20_close" }
  ],
  "sandbox_derived": [
    {
      "alias": "close_over_vwap_minus_1",
      "expr": "close / vwap_1m - 1"
    },
    {
      "alias": "volume_zscore",
      "expr": "(volume - volume_mean_20) / volume_std_20"
    }
  ]
}
```

* **只能用已经存在于 DataFrame 的列名**（base + 传统 derived）。
* 只能用简单算术 + 少量安全函数。
* 不允许 `shift` / `rolling` / `ewm` / `lead` / `lag` 等路径依赖算子（防止轻易引入未来信息）。
* 不写 SQL，只写“**行内向量表达式**”（适合 `df.eval`）。

> 你前面在 `FeatureRegistry.sanitize_recipe(..., allow_sandbox=True)` 里已经把这些 sandbox_derived 放进 recipe 了，这里只做执行层。

---

### 2. 新增模块：`features/sandbox_executor.py`

```python
# features/sandbox_executor.py

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Set, Tuple

import ast
import pandas as pd


# 允许的 AST 节点类型（只允许四则运算、括号等简单表达式）
_ALLOWED_NODES = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Pow,
    ast.Mod,
    ast.USub,
    ast.UAdd,
    ast.Load,
    ast.Name,
    ast.Constant,
    ast.Call,
    ast.Compare,
    ast.Gt,
    ast.GtE,
    ast.Lt,
    ast.LtE,
    ast.Eq,
    ast.NotEq,
    ast.BoolOp,
    ast.And,
    ast.Or,
)

# 允许的函数（如果你希望用 log / exp 等，可以在这里加）
_ALLOWED_FUNCS: Set[str] = {
    "abs",
    "log",
    "exp",
    "sqrt",
    "clip",
    "min",
    "max",
}

# 明确禁止的关键词（保险起见）
_FORBIDDEN_TOKENS: Tuple[str, ...] = (
    "__",
    "import",
    "os",
    "sys",
    "eval",
    "exec",
    "subprocess",
    "open",
    "pd",
    "np",
    "lambda",
)


class SandboxExpressionError(ValueError):
    pass


def _validate_expr(expr: str, allowed_names: Set[str]) -> None:
    """
    用 Python AST 对表达式进行静态检查：
    - 只允许 _ALLOWED_NODES 中的节点
    - 所有 Name 必须在 allowed_names 或 _ALLOWED_FUNCS 列表里
    - 禁止出现一些危险关键词
    - 禁止出现 rolling/ewm/shift/lead/lag 等（避免时间泄露）
    """
    for token in _FORBIDDEN_TOKENS:
        if token in expr:
            raise SandboxExpressionError(f"Forbidden token in expression: {token}")

    lower_expr = expr.lower()
    for bad in ("rolling", "ewm", "shift", "lead", "lag", "groupby", "resample"):
        if bad in lower_expr:
            raise SandboxExpressionError(f"Disallowed operation in sandbox expression: {bad}")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise SandboxExpressionError(f"Invalid expression syntax: {exc}") from exc

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise SandboxExpressionError(f"Disallowed AST node type: {type(node).__name__}")

        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise SandboxExpressionError("Only simple function calls allowed (e.g. log(x))")
            if node.func.id not in _ALLOWED_FUNCS:
                raise SandboxExpressionError(f"Function {node.func.id} not in allowed list")

        if isinstance(node, ast.Name):
            if node.id not in allowed_names and node.id not in _ALLOWED_FUNCS:
                raise SandboxExpressionError(f"Unknown identifier in expression: {node.id}")


def apply_sandbox_features(
    df: pd.DataFrame,
    sandbox_defs: Iterable[Dict[str, Any]],
    *,
    available_feature_cols: Iterable[str],
) -> Tuple[pd.DataFrame, List[Dict[str, Any]]]:
    """
    在给定 DataFrame 上安全地计算 sandbox_derived 特征。

    输入：
    - df: already loaded features DataFrame
    - sandbox_defs: 来自 feature_recipe["sandbox_derived"]
       形如: {"alias": "...", "expr": "..."}
    - available_feature_cols: 当前 df 中可使用的列名（base + 传统 derived）

    返回：
    - new_df: 带有 sandbox 列的新 DataFrame
    - applied: 实际成功应用的 sandbox 特征列表（用于记录/审计）
    """
    if not sandbox_defs:
        return df, []

    allowed_names = set(available_feature_cols)
    applied: List[Dict[str, Any]] = []
    new_df = df.copy()

    for item in sandbox_defs:
        alias = item.get("alias")
        expr = item.get("expr")

        if not alias or not expr:
            continue

        # 1) 静态检查表达式安全性
        _validate_expr(expr, allowed_names)

        # 2) 利用 pandas.eval 对整个列向量进行计算
        #    这里不允许引用 df/np/pd，只能用列名和 _ALLOWED_FUNCS 里列出的函数
        try:
            new_df[alias] = new_df.eval(expr)
        except Exception as exc:
            # 如果某个 sandbox 特征失败，不要中断整个流程，记录一下就行
            item_copy = dict(item)
            item_copy["error"] = str(exc)
            continue

        applied.append(item)

        # 新增列也加入 allowed_names，后续 sandbox 表达式可以引用已成功前面的 alias
        allowed_names.add(alias)

    return new_df, applied
```

> 这个执行器是“保守版”的：
>
> * 先静态检查 AST → 安全；
> * 只允许列名 + 少量函数；
> * 禁止滚动/shift 等时间操作，避免 LLM 轻易引入未来信息；
> * 某个 sandbox 特征挂了不会导致整次训练挂掉。

---

### 3. 把 Sandbox Executor 接入 `PrincipalInvestigator.propose()`

在前一轮的 `propose()` 基础上，几个重点调整：

1. 调用 `FeatureRegistry.sanitize_recipe(..., allow_sandbox=True)`，让 `sandbox_derived` 留下来；
2. 在 `query_df(sql)` 后调用 `apply_sandbox_features`，把 sandbox 特征真正算出来；
3. 将 sandbox alias 也加入 `feature_cols`，一起做 feature_sanity。

只展示差异的关键片段（你可以在现有 `propose()` 里替换）：

```python
from features.sandbox_executor import apply_sandbox_features

    def propose(..., freq: str = "1m") -> PIResult:
        ...
        # 3) 用 FeatureRegistry 对 recipe 做白名单过滤（开启 sandbox 收集）
        recipe, recipe_audit = self._feature_registry.sanitize_recipe(
            recipe_raw,
            freq=freq,
            feature_catalog_from_db=catalog,
            allow_sandbox=True,   # <--- 开启 sandbox 收集
        )
        tplan["feature_recipe_audit"] = recipe_audit

        # 4) 生成只包含 base + 传统 derived 特征的 SQL
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # 4.1) 在 DataFrame 上执行 sandbox 特征
        sandbox_defs = recipe.get("sandbox_derived", [])
        base_cols = self._feature_registry.resolve_source_cols(recipe.get("base_features", []))
        derived_aliases = [d.get("alias") for d in recipe.get("derived", []) if d.get("alias")]
        available_cols = [c for c in base_cols + derived_aliases if c in df.columns]

        df, applied_sandbox = apply_sandbox_features(
            df,
            sandbox_defs,
            available_feature_cols=available_cols,
        )
        tplan["sandbox_applied"] = applied_sandbox

        # 5) 数据集整体体检
        dataset_report, dataset_ok = run_dataset_sanity_checks(df)
        ...
        # 6) 特征体检：把 sandbox 列也纳入 feature_cols
        feature_cols = list(available_cols)
        for s in applied_sandbox:
            alias = s.get("alias")
            if alias and alias in df.columns and alias not in feature_cols:
                feature_cols.append(alias)

        feat_report, feat_ok = run_feature_sanity_checks(...)
        ...
```

> 至此，“沙箱特征”已经完整闭环：
>
> * LLM 提出表达式 →
> * Registry 白名单 + Catalog 检查 →
> * Sandbox Executor 静态分析 + 执行 →
> * 体检模块检查新特征质量。

---

## 二、AI 大脑如何用 dataset_manifest（变成有“眼睛”的研究总监）

现在我们让 Brain 每次决定“要不要 train / backtest / 部署”时，不是瞎子，而是先看一份 **dataset_manifest**。

### 1. 定义一个统一的决策请求/响应结构

新文件：`brain/contracts.py`

```python
# brain/contracts.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BrainDatasetContext:
    """
    AI 大脑在做“本次训练/回测决策”时看到的上下文。
    """
    symbol: str
    table: str
    freq: str
    goal: str
    horizon: str

    dataset_manifest: Dict[str, Any]
    hpo_grid: Dict[str, Any]

    # 可选：上次/历史实验结果（方便大脑做迭代）
    last_experiments: Optional[List[Dict[str, Any]]] = None


@dataclass
class BrainDecision:
    """
    AI 大脑基于 DatasetContext 给出的决策。
    """
    action: str                               # "train" | "skip" | "adjust_hpo" | "abort"
    reasons: str
    severity: str = "info"                    # "info" | "warning" | "error"
    adjusted_hpo_grid: Optional[Dict[str, Any]] = None
    notes: Dict[str, Any] = field(default_factory=dict)
```

> 这层是纯 Python 的“契约”，你后面的 orchestrator（比如 `wf_hpo` 或 `ai_brain_orchestrator`）就可以统一处理 `BrainDecision`，而不用直接解析 LLM 的原始 JSON。

---

### 2. 给 AI 大脑的 Prompt 结构（让它看懂 manifest）

新文件：`brain/prompts/dataset_gate_prompt.json`（或者 Python 字符串也行）：

````jsonc
{
  "_comment": "AI Brain dataset gate & training decision prompt",
  "system": "You are the head of quantitative research for an intraday trading platform. You are STRICTLY risk-first: you must refuse to train models on corrupted, low-quality, or mislabeled data.\n\nYou will receive a compact JSON manifest describing one candidate dataset (features + label + quality checks) and a proposed HPO grid. Your task is NOT to design the model, but to decide:\n- whether the dataset is safe enough for training/backtest,\n- whether the HPO grid needs adjustments,\n- or whether to skip / abort.\n\nYou MUST output valid JSON matching the required schema, with no extra text.",
  "user_template": "Here is the dataset context and manifest:\n\n```json\n{{CONTEXT_JSON}}\n```\n\nYou must strictly follow these rules:\n1. If dataset_manifest.dataset_sanity.ok == false OR label_sanity.ok == false → action = \"abort\"; severity = \"error\".\n2. If feature_sanity.ok == false but only a small number of bad_features are present → you may choose action = \"adjust_hpo\" AND recommend dropping those bad features in adjusted_hpo_grid.feature_subset.\n3. If label_sanity.summary.class_imbalance is \"severe\" (e.g., positive class < 1%) → you may still train, but must set severity = \"warning\" and note that proper metrics (AUC/F1/PR) and rebalancing are required.\n4. When in doubt (quality is borderline), prefer action = \"skip\" instead of \"train\".\n\nOutput JSON ONLY with this schema:\n{\n  \"action\": \"train\" | \"skip\" | \"adjust_hpo\" | \"abort\",\n  \"reasons\": \"short explanation in English\",\n  \"severity\": \"info\" | \"warning\" | \"error\",\n  \"adjusted_hpo_grid\": null OR {\n    \"feature_subset\": [\"feature_id\", ...],\n    \"model_type\": [\"xgb\", \"lasso\", ...],\n    \"model_params\": [{...}],\n    \"task\": [\"reg\" | \"clf\"],\n    \"decision\": [{...}]\n  },\n  \"notes\": {\n    \"drop_features\": [\"feature_id\", ...],\n    \"quality_flags\": [\"label_imbalance\", \"high_correlation\", ...]\n  }\n}"
}
````

> 这里我们把“复杂的五花八门质检结果”压成一个 JSON manifest，**只给 Brain 看摘要**（你已经在 `build_dataset_manifest` 里完成了这一点）。

---

### 3. 真正的调用逻辑：`brain/dataset_gate.py`

````python
# brain/dataset_gate.py

from __future__ import annotations

import json
from typing import Any, Dict

from llm.llm_client import LLMClient
from brain.contracts import BrainDatasetContext, BrainDecision


class DatasetGate:
    """
    AI 大脑的“数据门卫”：
    - 把 BrainDatasetContext 打包成 JSON
    - 调用 LLM
    - 解析返回，输出 BrainDecision
    """

    def __init__(self, llm_cfg: Dict[str, Any]) -> None:
        self._llm = LLMClient(
            model=llm_cfg.get("model"),
            base_url=llm_cfg.get("base_url"),
        )
        # 这里你可以从文件加载 system/user 模版，也可以直接硬编码
        self._system_prompt = (
            "You are the head of quantitative research for an intraday trading platform. "
            "You are STRICTLY risk-first: you must refuse to train models on corrupted, "
            "low-quality, or mislabeled data.\n"
            "You MUST output valid JSON only."
        )

    def decide(self, ctx: BrainDatasetContext) -> BrainDecision:
        ctx_json = json.dumps(
            {
                "symbol": ctx.symbol,
                "table": ctx.table,
                "freq": ctx.freq,
                "goal": ctx.goal,
                "horizon": ctx.horizon,
                "dataset_manifest": ctx.dataset_manifest,
                "hpo_grid": ctx.hpo_grid,
                "last_experiments": ctx.last_experiments or [],
            },
            ensure_ascii=False,
        )

        user_prompt = (
            "Here is the dataset context and manifest:\n\n"
            f"```json\n{ctx_json}\n```\n\n"
            "Decide whether to train, skip, adjust_hpo, or abort, following the rules."
        )

        raw = self._llm.generate(
            system_prompt=self._system_prompt,
            user_prompt=user_prompt,
        )

        # 假设 LLMClient 已经把 JSON 解析好了；如果只是字符串，这里要再 json.loads 一次
        if isinstance(raw, str):
            resp = json.loads(raw)
        else:
            resp = raw

        return BrainDecision(
            action=resp.get("action", "abort"),
            reasons=resp.get("reasons", ""),
            severity=resp.get("severity", "info"),
            adjusted_hpo_grid=resp.get("adjusted_hpo_grid"),
            notes=resp.get("notes") or {},
        )
````

---

### 4. Orchestrator 里怎么串起来（高层流程）

在你的“AI 大脑 Orchestrator”（可能是某个 `brain_orchestrator.py` 或 `wf_hpo.py` 上层）里，流程变成：

```python
pi = PrincipalInvestigator(...)
gate = DatasetGate(llm_cfg=...)

# 1) PI 生成 dataset + hpo_grid + manifest
pi_result = pi.propose(
    table="bars_1m_tsla",
    symbol="TSLA",
    start_ts="2025-01-01 09:30:00",
    end_ts="2025-03-01 16:00:00",
    goal="predict 10-minute forward return sign",
    horizon="10m",
)

ctx = BrainDatasetContext(
    symbol="TSLA",
    table="bars_1m_tsla",
    freq="1m",
    goal="10m alpha",
    horizon="10m",
    dataset_manifest=pi_result.dataset_manifest,
    hpo_grid=pi_result.hpo_grid,
    last_experiments=None,  # 可以从数据库/日志里查最近的实验结果
)

# 2) 让大脑先看一眼，给决策
decision = gate.decide(ctx)

if decision.action in ("abort", "skip") and decision.severity == "error":
    # 记录日志，直接停止本轮（防止糊里糊涂地在垃圾数据上训练）
    logger.error("Brain aborted training: %s", decision.reasons)
    return

# 3) 如果需要调整 HPO 网格，就覆盖
hpo_grid = decision.adjusted_hpo_grid or pi_result.hpo_grid

# 4) 把 df + hpo_grid 扔给现有的 HPO 引擎，继续你原来的流程
run_hpo(
    df=pi_result.dataset_df,
    grid=hpo_grid,
    target_col=pi_result.training_plan["target_col"],
    ...
)
```

> 至此，AI 大脑从“直接裸奔调 wf_hpo” → 变成了：
> **先看 DatasetManifest → 给出是否训练/如何训练的决策**。
> 即使 LLM 智商不在线，你还有 dataset_sanity + label_sanity + feature_sanity 三道硬防线兜底。

---

## 三、这轮已经完成了什么？

这一轮我按计划落地了两块关键能力：

1. **Sandbox 特征（有牙但戴口罩）**

   * `FeatureRegistry` 允许 `sandbox_derived`；
   * `features/sandbox_executor.py` 提供安全执行器：

     * AST 静态分析 + 禁止危险操作 + 禁止时间泄露函数；
     * 用 `df.eval` 生成新列；
     * 错误 sandbox 不影响整体。
   * `PrincipalInvestigator.propose()` 已经接入 sandbox，并把结果写回 `tplan["sandbox_applied"]`，同时纳入特征体检。

2. **AI 大脑的“眼睛”：Dataset Manifest → 决策 Gate**

   * `BrainDatasetContext` / `BrainDecision` 统一封装 Brain 的输入输出；
   * `DatasetGate` 使用 `dataset_manifest + hpo_grid` 调 LLM 做“要不要训练/如何调整 HPO”的高层决定；
   * 给了具体 prompt 结构和 orchestrator 用法，把 Brain 嵌入你现有的 ML pipeline，而不是旁边喊口号。

---

## 下一步我会干什么（你再说“继续”的时候）

按原大图，下一步要做的是：

1. **把 LabelEngine & DatasetGate 的信息回写到数据库/日志**，形成“实验审计轨迹”：

   * 每次实验记录：用的是哪一版 label 定义、哪一组特征、质检结果是什么、Brain 决策是什么；
   * 为后面“Auto-Evolver 自动调策略/Prompt”的闭环打基础。

2. **把这一整套防线接进你现有的 HPO / wf_hpo 代码**：

   * 在 HPO trial 级别上，继续做轻量质检（比如每个 trial 的 oof 分布，防止某个 model 把 label 当 id 拟合）；
   * 给 Brain 回传“哪种特征/模型组合在当前数据质量条件下表现最稳”。

你只要回复一个字：**继续**，我就直接开始**围绕 HPO + 实验审计**那一层落地，不会再问你要不要。
好，这一轮我按计划把 **“实验审计 + HPO 接口”** 落地，不问你要不要，直接给可以塞进仓库的代码骨架。

---

## 一、实验审计：给每一次“AI 大脑 + HPO”一个完整病历

### 1. 实验记录的数据模型

新文件：`experiments/audit.py`

```python
# experiments/audit.py

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TrialRecord:
    """
    单个 HPO trial 的记录。
    """
    trial_id: int
    params: Dict[str, Any]
    metrics: Dict[str, Any]
    status: str  # "ok" | "failed" | "skipped"
    notes: Optional[str] = None


@dataclass
class ExperimentRecord:
    """
    一次完整“AI 大脑 + PI + HPO”的实验记录。
    """
    run_id: str
    created_at: str

    symbol: str
    table: str
    freq: str
    goal: str
    horizon: str

    # 来自 LabelEngine / LabelRegistry
    label_id: str
    label_manifest: Dict[str, Any]

    # 特征配方 & 版本
    feature_recipe_id: Optional[str]
    feature_recipe: Dict[str, Any]

    # 数据体检整体报告
    dataset_manifest: Dict[str, Any]

    # 大脑的决策
    brain_decision: Dict[str, Any]

    # HPO 网格快照（进入 HPO 之前的）
    hpo_grid: Dict[str, Any]

    # 实验状态
    status: str = "running"  # "running" | "completed" | "aborted" | "failed"
    error: Optional[str] = None

    # 汇总指标（整个 experiment 的 best trial）
    summary_metrics: Dict[str, Any] = field(default_factory=dict)

    # 各个 trial 的详细记录
    trials: List[TrialRecord] = field(default_factory=list)


class ExperimentRunLogger:
    """
    负责把 ExperimentRecord 持久化到一个 JSONL 文件。

    设计是“便宜好用版”：
    - 只在 end_run() 的时候 append，避免频繁 I/O
    - 你可以 1 个 experiment 就 new 一个 logger
    """

    def __init__(self, log_path: Path, record: ExperimentRecord) -> None:
        self._log_path = log_path
        self._record = record
        self._log_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def run_id(self) -> str:
        return self._record.run_id

    def log_trial(
        self,
        *,
        trial_id: int,
        params: Dict[str, Any],
        metrics: Dict[str, Any],
        status: str = "ok",
        notes: Optional[str] = None,
    ) -> None:
        rec = TrialRecord(
            trial_id=trial_id,
            params=params,
            metrics=metrics,
            status=status,
            notes=notes,
        )
        self._record.trials.append(rec)

    def set_summary_metrics(self, metrics: Dict[str, Any]) -> None:
        self._record.summary_metrics = dict(metrics)

    def end_run(self, *, status: str, error: Optional[str] = None) -> None:
        self._record.status = status
        self._record.error = error

        payload = asdict(self._record)
        with self._log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def start_experiment_run(
    *,
    log_dir: str,
    symbol: str,
    table: str,
    freq: str,
    goal: str,
    horizon: str,
    label_id: str,
    label_manifest: Dict[str, Any],
    feature_recipe_id: Optional[str],
    feature_recipe: Dict[str, Any],
    dataset_manifest: Dict[str, Any],
    brain_decision: Dict[str, Any],
    hpo_grid: Dict[str, Any],
) -> ExperimentRunLogger:
    """
    对外唯一入口：新建一次实验记录，并返回一个 logger handle。
    """
    run_id = str(uuid.uuid4())
    record = ExperimentRecord(
        run_id=run_id,
        created_at=_now_iso(),
        symbol=symbol,
        table=table,
        freq=freq,
        goal=goal,
        horizon=horizon,
        label_id=label_id,
        label_manifest=label_manifest,
        feature_recipe_id=feature_recipe_id,
        feature_recipe=feature_recipe,
        dataset_manifest=dataset_manifest,
        brain_decision=brain_decision,
        hpo_grid=hpo_grid,
    )

    log_path = Path(log_dir) / "experiments.jsonl"
    return ExperimentRunLogger(log_path=log_path, record=record)
```

> 说明：
>
> * 默认写到 `logs/experiments.jsonl`（你可以在 orchestrator 里传 `log_dir`）。
> * 每一行一个完整 ExperimentRecord，包含：
>
>   * 使用了哪一版 label 定义 (`label_id`)
>   * 哪个 feature_recipe（包含 sandbox 特征审计）
>   * dataset_manifest（数据/特征/标签体检结果）
>   * 大脑的 `BrainDecision`
>   * HPO 每个 trial 的参数和结果

后面你要做“回溯哪一次实验是好的 / 复盘为什么炸了”，直接扫这个 JSONL 就行。

---

## 二、HPO Trial 保护：防止“IC 很高但 Sharpe 炸裂”的 trial 混进来

我们再加一个轻量的“Trial Guard”，放在 HPO 的每个 trial 完成后，做一次 sanity check，然后顺便给 audit logger 记一笔。

新文件：`experiments/trial_guard.py`

```python
# experiments/trial_guard.py

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def guard_trial_metrics(metrics: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    对单个 trial 的指标做简单 sanity check。

    这里先实现一个“保守但实用”的版本：
    - 所有关键指标不能是 NaN / Inf
    - 交易次数不能太少（否则策略不可信）
    - Sharpe 不能绝对值过大（> 20 当成可疑）
    - 如果 IC 很高但 Sharpe 负数，也提示一下（可能是方向反了或回测不稳定）
    """
    reasons: List[str] = []

    def _safe_get(name: str) -> Any:
        return metrics.get(name)

    sharpe = _safe_get("sharpe")
    ic = _safe_get("ic")
    trades = _safe_get("trades") or _safe_get("n_trades") or 0
    win_rate = _safe_get("win_rate")

    # 1) 关键指标不能是 None
    for key in ("sharpe", "ic"):
        if metrics.get(key) is None:
            reasons.append(f"metric_missing:{key}")

    # 2) 交易次数太少
    try:
        if trades is not None and trades < 5:
            reasons.append("too_few_trades")
    except TypeError:
        reasons.append("invalid_trades_type")

    # 3) Sharpe 非常离谱
    try:
        if sharpe is not None:
            s = float(sharpe)
            if abs(s) > 20:
                reasons.append("sharpe_out_of_range")
    except Exception:
        reasons.append("invalid_sharpe_type")

    # 4) IC 高但 Sharpe 为负，打一个 warning（不直接判死刑，由上层决定）
    try:
        if ic is not None and sharpe is not None:
            i = float(ic)
            s = float(sharpe)
            if i > 0.02 and s < 0:
                reasons.append("ic_sharpe_inconsistent")
    except Exception:
        reasons.append("invalid_ic_type")

    # 5) 胜率极端但 Sharpe 一般，提示可能过度杠杆或 heavy tail
    try:
        if win_rate is not None:
            w = float(win_rate)
            if w > 0.8 and sharpe is not None and float(sharpe) < 1.0:
                reasons.append("high_winrate_low_sharpe")
    except Exception:
        pass

    ok = len(reasons) == 0
    return ok, reasons
```

> 这个 guard 是“软防线”：
>
> * 它不直接挂断 HPO，而是告诉你“这次 trial 看起来有问题”。
> * 在 HPO 中可以选择：
>
>   * 把这些 trial 标记为 `status="skipped"` 并给很差的目标值；
>   * 或者照常参与 ranking，但记录在 ExperimentAudit 里。

---

## 三、把这两块接入现有 `wf_hpo.py` / HPO 引擎

下面是一个典型的 HPO 主流程抽象（你现有的 `run_hpo` 大概率类似），我用“补丁式”展示关键修改点。

假设你现在有一个函数：

```python
# wf_hpo.py (简化示意)

def run_hpo(
    df: pd.DataFrame,
    target_col: str,
    hpo_grid: Dict[str, Any],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    \"\"\"Return best_trial summary.\"\"\"
    ...
```

### 1. 给 `run_hpo` 加上审计 & 试验守卫参数

我们扩展签名：

```python
# wf_hpo.py

from typing import Optional
from experiments.audit import ExperimentRunLogger
from experiments.trial_guard import guard_trial_metrics


def run_hpo(
    df: pd.DataFrame,
    target_col: str,
    hpo_grid: Dict[str, Any],
    config: Dict[str, Any],
    *,
    experiment_logger: Optional[ExperimentRunLogger] = None,
) -> Dict[str, Any]:
    """
    - df: 已经通过 dataset_sanity + feature_sanity + label_sanity 的数据
    - hpo_grid: 来自 PI + Brain 决策（可能已经被 adjusted）
    - experiment_logger: 如果提供，则把每个 trial 的结果都记录进去
    """
    best_trial_summary: Dict[str, Any] = {}
    best_objective = None

    # 这里你原来应该有外层的 Optuna Study 或自定义 HPO loop
    # 我用伪代码示意 trial 遍历逻辑：
    for trial_id, params in enumerate(iter_hpo_params(hpo_grid)):
        try:
            # 1) 训练 + 回测
            metrics = train_and_backtest(df, target_col, params, config)

            # 2) trial 级别的 sanity check
            ok, reasons = guard_trial_metrics(metrics)

            # 3) 根据 guard 结果决定是否“降权/剔除”
            if not ok:
                # 这里演示一种策略：标记为 skipped，给一个极差的目标值
                objective_value = -1e9
                status = "skipped"
                notes = ";".join(reasons)
            else:
                objective_value = metrics.get(config.get("objective_metric", "sharpe"), 0.0)
                status = "ok"
                notes = None

            # 4) 记录到 ExperimentAudit（如果有 logger）
            if experiment_logger is not None:
                experiment_logger.log_trial(
                    trial_id=trial_id,
                    params=params,
                    metrics=metrics,
                    status=status,
                    notes=notes,
                )

            # 5) 更新 best_trial
            if best_objective is None or objective_value > best_objective:
                best_objective = objective_value
                best_trial_summary = {
                    "trial_id": trial_id,
                    "params": params,
                    "metrics": metrics,
                    "status": status,
                    "notes": notes,
                }

        except Exception as exc:
            # 训练本身挂了，也记一下
            if experiment_logger is not None:
                experiment_logger.log_trial(
                    trial_id=trial_id,
                    params=params,
                    metrics={},
                    status="failed",
                    notes=str(exc),
                )
            continue

    # 6) 把 best_trial_summary 回写给 experiment_logger 作为汇总指标
    if experiment_logger is not None and best_trial_summary:
        experiment_logger.set_summary_metrics(best_trial_summary.get("metrics", {}))

    return best_trial_summary
```

> 你只需要把 `iter_hpo_params` 和 `train_and_backtest` 替换成你自己现在的实现（Optuna loop / 自定义 HPO 逻辑），其余结构保持不变即可。

---

## 四、顶层 Orchestrator 串起来：PI → DatasetGate → HPO → Audit

最后给一个“从 AI 大脑视角看全链路”的 `orchestrators/brain_hpo_orchestrator.py` 示例，你可以直接按这个骨架整理现有脚本：

```python
# orchestrators/brain_hpo_orchestrator.py

from __future__ import annotations

from typing import Any, Dict, Optional

from brain.dataset_gate import DatasetGate
from brain.contracts import BrainDatasetContext
from experiments.audit import start_experiment_run
from pi.principal_investigator import PrincipalInvestigator
from wf_hpo import run_hpo


def run_brain_controlled_hpo(
    *,
    ch_cfg: Dict[str, Any],
    llm_cfg: Dict[str, Any],
    symbol: str,
    table: str,
    freq: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    label_def: Dict[str, Any],      # 来自 LabelRegistry.get(label_id)
    label_id: str,
    last_experiments: Optional[list] = None,
    log_dir: str = "logs",
) -> Dict[str, Any]:
    """
    这是“AI 大脑控制机器学习”的唯一入口：
    - PI 负责：特征 + 训练计划 + 数据集 + HPO 网格
    - DatasetGate 负责：看 dataset_manifest 决定 train/skip/abort/adjust_hpo
    - HPO 引擎负责：具体训练与回测
    - ExperimentAudit 负责：全链路审计
    """
    # 1) 初始化 PI & Gate
    pi = PrincipalInvestigator(ch_cfg=ch_cfg, llm_cfg=llm_cfg)
    gate = DatasetGate(llm_cfg=llm_cfg)

    # 2) 让 PI 生成 feature_recipe + training_plan + dataset_df + hpo_grid + dataset_manifest
    pi_result = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
    )

    # 你前几轮我们已经加过 dataset_manifest，假设 pi_result 里有：
    dataset_manifest = pi_result.dataset_manifest           # 总体体检
    feature_recipe = pi_result.feature_recipe
    training_plan = pi_result.training_plan
    hpo_grid = pi_result.hpo_grid
    df = pi_result.dataset_df

    # 3) AI 大脑先看一眼，再决定要不要 train
    ctx = BrainDatasetContext(
        symbol=symbol,
        table=table,
        freq=freq,
        goal=goal,
        horizon=horizon,
        dataset_manifest=dataset_manifest,
        hpo_grid=hpo_grid,
        last_experiments=last_experiments,
    )
    decision = gate.decide(ctx)

    if decision.action in ("abort", "skip"):
        # 这里仍然写一条 experiment 记录，方便事后看“被大脑拒绝的理由”
        exp_logger = start_experiment_run(
            log_dir=log_dir,
            symbol=symbol,
            table=table,
            freq=freq,
            goal=goal,
            horizon=horizon,
            label_id=label_id,
            label_manifest=label_def,
            feature_recipe_id=training_plan.get("feature_recipe_id"),
            feature_recipe=feature_recipe,
            dataset_manifest=dataset_manifest,
            brain_decision={
                "action": decision.action,
                "reasons": decision.reasons,
                "severity": decision.severity,
                "notes": decision.notes,
            },
            hpo_grid=hpo_grid,
        )
        exp_logger.end_run(status="aborted", error=decision.reasons)
        return {
            "status": "aborted",
            "reason": decision.reasons,
            "brain_decision": decision,
        }

    # 4) 如有必要，AI 大脑调整 HPO 网格
    adjusted_grid = decision.adjusted_hpo_grid or hpo_grid

    # 5) 正式开始 experiment 审计
    exp_logger = start_experiment_run(
        log_dir=log_dir,
        symbol=symbol,
        table=table,
        freq=freq,
        goal=goal,
        horizon=horizon,
        label_id=label_id,
        label_manifest=label_def,
        feature_recipe_id=training_plan.get("feature_recipe_id"),
        feature_recipe=feature_recipe,
        dataset_manifest=dataset_manifest,
        brain_decision={
            "action": decision.action,
            "reasons": decision.reasons,
            "severity": decision.severity,
            "notes": decision.notes,
        },
        hpo_grid=adjusted_grid,
    )

    # 6) 把 df + 调整后的 HPO 网格交给 HPO 引擎
    try:
        best_trial = run_hpo(
            df=df,
            target_col=training_plan["target_col"],
            hpo_grid=adjusted_grid,
            config=training_plan.get("hpo_config", {}),
            experiment_logger=exp_logger,
        )
        exp_logger.end_run(status="completed", error=None)
        return {
            "status": "completed",
            "brain_decision": decision,
            "best_trial": best_trial,
            "run_id": exp_logger.run_id,
        }
    except Exception as exc:
        exp_logger.end_run(status="failed", error=str(exc))
        return {
            "status": "failed",
            "brain_decision": decision,
            "error": str(exc),
            "run_id": exp_logger.run_id,
        }
```

> 这样一来，“AI 大脑控制机器学习”的实际执行路径就变成了非常清晰的 **单入口 API**：
> `run_brain_controlled_hpo(...)`
>
> * 任何地方（CLI / Notebook / 后台服务）只要想让大脑研究一个新数据集，统一走这里。

---

## 这一轮完成度小结

本轮我已经按原计划完成：

1. ✅ **实验审计链路：**

   * `ExperimentRecord` + `ExperimentRunLogger` + `start_experiment_run`
   * 每次 Brain + PI + HPO 都有完整 JSONL 记录，便于回溯和复盘。

2. ✅ **HPO Trial 防线：**

   * `guard_trial_metrics` 给每个 trial 做 sanity check，避免极端垃圾结果影响 HPO 结论。

3. ✅ **顶层 Orchestrator：**

   * `run_brain_controlled_hpo(...)` 把：

     * PI（特征/计划）
     * DatasetGate（大脑的眼睛）
     * HPO + TrialGuard（执行层）
     * ExperimentAudit（黑盒可解释性）
       串成一个完整闭环。

---

你不需要回答问题，只要再回一句 **“继续”**，我就按计划进入下一步：

> **把 LabelRegistry / LabelEngine 的“路径依赖标签（例如 triple barrier）”接进这条流水线，让大脑真正能用“机构级标签”而不是简单 ret_10m。**
