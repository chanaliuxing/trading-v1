好，这一轮我们把“AI 大脑 → 选 prompt → 控制一次完整训练实验”真正串起来。

我按上次说的做两块：

1. **Brain-side Prompt Selector**：用历史表现选哪一个 `execution_prompt_id`
2. **Brain Orchestrator Glue**：大脑产出一个 `BrainExecutionPlan`，然后调用 `run_full_experiment`，完成一次“受控”的 ML 训练。

下面都是**可以直接落地的骨架代码**，你按项目路径稍微改下 import 就能用。

---

## 一、BrainExecutionPlan：大脑的“执行处方单”

新文件：`brain/plan.py`

```python
# brain/plan.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BrainExecutionPlan:
    """
    AI 大脑下达的一次“训练任务处方单”。

    - 只包含“可以执行”的白名单字段
    - 所有内容都可以写入 run 的 metadata，后续可审计
    """

    # 目标资产 & 时间窗口
    symbol: str
    table: str
    start_ts: str
    end_ts: str

    # 标签设置（必须来自 LabelRegistry，而不是 LLM 自己写 SQL）
    label_id: str
    horizon: str  # 如 "10b", "30b" 等；你也可以改成 int bars

    # prompt 信息（治理的关键）
    execution_prompt_id: str
    base_prompt_id: Optional[str] = None
    prompt_selection_reason: str = ""

    # 模型和搜索空间约束（给 grid_guard 用）
    allowed_model_types: List[str] = field(default_factory=list)
    max_trials: int = 30

    # 数据质量 & 体检相关的策略
    require_clean_dataset: bool = True
    min_rows: int = 5_000
    max_missing_ratio: float = 0.5  # 某特征缺失超过就自动剔除

    # 其他策略风格标签（以后可以扩展）
    mode: str = "training"  # 未来可以有 "live_eval" 等
    notes: str = ""

    # 允许额外附加配置（必须在执行侧重新白名单过滤）
    extra_cfg: Dict[str, Any] = field(default_factory=dict)
```

> 以后所有“AI 大脑下的命令”，都要收敛成这个 `BrainExecutionPlan`，
> 防止 prompt 直接去改 SQL / 超参 / 标签定义。

---

## 二、PromptSelector：用历史 run 结果 + PromptRegistry 选 prompt

新文件：`brain/prompt_selector.py`

```python
# brain/prompt_selector.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from prompt.registry import PromptRegistry
from runs.history_index import RunHistoryIndex, PromptPerformanceSummary
from brain.plan import BrainExecutionPlan


@dataclass
class PromptSelectionContext:
    symbol: str
    table: str
    start_ts: str
    end_ts: str
    label_id: str
    horizon: str
    base_prompt_id: str  # 例如 "v3_fast_market_following"
    since_days: int = 30
    min_runs_for_stats: int = 3
    default_allowed_model_types: Optional[List[str]] = None


class PromptSelector:
    """
    负责：
    - 在给定 base_prompt_id 下，查看历史 run 表现
    - 决定这一次用哪个 execution_prompt_id
    - 生成 BrainExecutionPlan (只管“计划”，不管具体执行)
    """

    def __init__(
        self,
        prompt_registry: PromptRegistry,
        history_index: RunHistoryIndex,
    ) -> None:
        self._prompts = prompt_registry
        self._history = history_index

    # 核心对外 API
    def make_execution_plan(self, ctx: PromptSelectionContext) -> BrainExecutionPlan:
        """
        Step 1: 看历史表现 → 尝试选一个表现最好的 execution_prompt_id
        Step 2: 如果历史太少 → fallback 到 base_prompt 或 latest tactical
        Step 3: 填充 BrainExecutionPlan，交给 orchestrator 执行
        """
        # 1) 尝试基于历史表现选 prompt
        perf_summaries = self._history.summarize_by_prompt(
            symbol=ctx.symbol,
            label_id=ctx.label_id,
            base_prompt_id=ctx.base_prompt_id,
            since_days=ctx.since_days,
        )

        execution_prompt_id: Optional[str] = None
        reason: str = ""

        if perf_summaries and len(perf_summaries[0:n := len(perf_summaries)]) >= ctx.min_runs_for_stats:
            top = perf_summaries[0]
            execution_prompt_id = top.prompt_id
            reason = (
                f"Selected by history stats: prompt_id={top.prompt_id}, "
                f"base={top.base_prompt_id}, "
                f"n_runs={top.n_runs}, "
                f"metric={top.metric_name}, "
                f"mean={top.metric_mean}, "
                f"median={top.metric_median}."
            )
        else:
            # 历史太少或没有 → 使用最新的 tactical 版本
            latest = self._prompts.latest_by_base(ctx.base_prompt_id)
            if latest:
                execution_prompt_id = latest.prompt_id
                reason = (
                    f"No sufficient history for base_id={ctx.base_prompt_id} "
                    f"(found {len(perf_summaries)} runs). "
                    f"Fallback to latest_by_base: {latest.prompt_id}."
                )
            else:
                # 再不行就直接用 base_prompt 本体
                if self._prompts.exists(ctx.base_prompt_id):
                    execution_prompt_id = ctx.base_prompt_id
                    reason = (
                        f"No tactical prompts found for base_id={ctx.base_prompt_id}. "
                        f"Fallback to base prompt itself."
                    )
                else:
                    raise ValueError(
                        f"Neither tactical nor base prompt found for base_id={ctx.base_prompt_id}"
                    )

        meta = self._prompts.get_meta(execution_prompt_id) if execution_prompt_id else None
        base_id = meta.base_id if meta and meta.base_id else ctx.base_prompt_id

        # 默认允许的模型类型（可以以后根据 prompt 风格调整）
        allowed_model_types = ctx.default_allowed_model_types or [
            "xgb",
            "lightgbm",
            "lasso",
            "elasticnet",
            "ridge",
            "logistic",
            "linear_svm",
        ]

        plan = BrainExecutionPlan(
            symbol=ctx.symbol,
            table=ctx.table,
            start_ts=ctx.start_ts,
            end_ts=ctx.end_ts,
            label_id=ctx.label_id,
            horizon=ctx.horizon,
            execution_prompt_id=execution_prompt_id,
            base_prompt_id=base_id,
            prompt_selection_reason=reason,
            allowed_model_types=allowed_model_types,
        )

        return plan
```

> 这里有一个非常重要的点：
> **AI 大脑的“选择 prompt”逻辑本身是纯数据驱动的，并且是可重放/可审计的。**
> 你以后可以再把这层改成 LLM + RunHistory 的混合决策，但基础是这个索引器。

---

## 三、Brain Orchestrator：从 ExecutionPlan → run_full_experiment

现在写一个 orchestrator，把前面所有砖块真正串起来：

* 加载 `PromptRegistry`
* 加载 `RunHistoryIndex`
* 用 `PromptSelector` 生成 `BrainExecutionPlan`
* 根据 plan 组装 `run_full_experiment()` 所需的 cfg（只在白名单里取字段）
* 执行后把结果写入 JSONL（前几轮已经搞过）

新文件：`brain/orchestrator.py`

```python
# brain/orchestrator.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional
import uuid

from prompt.registry import PromptRegistry
from runs.history_index import RunHistoryIndex
from brain.prompt_selector import PromptSelector, PromptSelectionContext
from brain.plan import BrainExecutionPlan
from brain.narrator import narrate_dataset_quality

from pi.principal_investigator import PrincipalInvestigator, PIResult
from runs.context import ModelRunContext
from hpo.runner import run_hpo  # 假设你有一个独立的 HPO 入口


def run_brain_controlled_experiment(brain_cfg: Dict[str, Any]) -> ModelRunContext:
    """
    AI 大脑控制的端到端训练流程：

    1. 加载 PromptRegistry + RunHistoryIndex
    2. 由 PromptSelector 生成 BrainExecutionPlan
    3. 按计划调用 PrincipalInvestigator（带 FeatureRegistry / LabelRegistry / Sanity）
    4. 体检 & 质量摘要
    5. 按计划执行 HPO（限定 allowed_model_types / grid_guard）
    6. 将全量信息写入 ModelRunContext + JSONL

    参数 brain_cfg 建议最小字段:
    {
      "clickhouse": {...},
      "llm": {...},
      "prompt_library_path": ".../prompt_library.json",
      "model_runs_path": ".../model_runs.jsonl",
      "feature_registry_path": ".../feature_registry.json",
      "label_registry_path": ".../label_registry.json",
      "base_prompt_id": "v3_fast_market_following",
      "symbol": "TSLA",
      "table": "bars_1m_features",
      "start_ts": "2025-10-01 09:30:00",
      "end_ts": "2025-10-31 16:00:00",
      "horizon": "10b",
      "label_id": "ret_10b_fwd_reg",
      "freq": "1m",
      "grid_guard": {...},
      "sanity": {...},
      "max_trials": 30
    }
    """

    # -------- 1) PromptRegistry + RunHistoryIndex --------
    prompt_registry = PromptRegistry.from_file(Path(brain_cfg["prompt_library_path"]))
    history_index = RunHistoryIndex.from_jsonl(Path(brain_cfg["model_runs_path"]))

    # -------- 2) PromptSelector → BrainExecutionPlan --------
    ps_ctx = PromptSelectionContext(
        symbol=brain_cfg["symbol"],
        table=brain_cfg["table"],
        start_ts=brain_cfg["start_ts"],
        end_ts=brain_cfg["end_ts"],
        label_id=brain_cfg["label_id"],
        horizon=brain_cfg["horizon"],
        base_prompt_id=brain_cfg["base_prompt_id"],
        since_days=brain_cfg.get("history_since_days", 30),
        min_runs_for_stats=brain_cfg.get("min_runs_for_stats", 3),
        default_allowed_model_types=brain_cfg.get("allowed_model_types"),
    )

    selector = PromptSelector(prompt_registry, history_index)
    exec_plan: BrainExecutionPlan = selector.make_execution_plan(ps_ctx)

    # -------- 3) PrincipalInvestigator: 生成特征 + 标签 + grid --------
    pi = PrincipalInvestigator(
        ch_cfg=brain_cfg["clickhouse"],
        llm_cfg=brain_cfg.get("llm"),
        feature_registry_path=brain_cfg.get("feature_registry_path"),
        label_registry_path=brain_cfg.get("label_registry_path"),
    )

    pi_result: PIResult = pi.propose(
        table=exec_plan.table,
        symbol=exec_plan.symbol,
        start_ts=exec_plan.start_ts,
        end_ts=exec_plan.end_ts,
        goal=brain_cfg.get("goal", "intraday alpha"),
        horizon=exec_plan.horizon,
        guard_cfg=brain_cfg.get("grid_guard"),
        sanity_cfg=brain_cfg.get("sanity"),
        default_label_id=exec_plan.label_id,
    )

    # 体检、质量摘要已经在 PIResult 里
    sanity = pi_result.sanity_result
    quality = pi_result.dataset_quality_summary

    # 如果 require_clean_dataset 则在这里直接 fail fast
    if exec_plan.require_clean_dataset and sanity.has_critical_issues():
        # 这里你可以抛异常，或者写入一个特殊的 run 记录
        raise RuntimeError(f"Critical dataset issues found: {sanity.list_critical_issues()}")

    # -------- 4) 给 AI 大脑看的数据质量摘要（可选） --------
    dataset_summary_txt = narrate_dataset_quality(
        sanity=sanity,
        quality=quality,
    )
    # 你可以选择：
    # - 把这段文字存入 ModelRunContext.notes
    # - 或者在后续 LLM prompt 里使用

    # -------- 5) HPO / 模型训练 --------
    # 将 allowed_model_types 注入 grid_guard，保证搜索空间受控
    grid_guard = dict(brain_cfg.get("grid_guard", {}))
    grid_guard["allow_model_types"] = exec_plan.allowed_model_types

    # 假设 run_hpo 接受这些参数（你可以根据现有 hpo 代码稍微调整）
    hpo_result = run_hpo(
        df=pi_result.dataset_df,
        hpo_grid=pi_result.hpo_grid,
        grid_guard=grid_guard,
        max_trials=exec_plan.max_trials or brain_cfg.get("max_trials", 30),
        label_column=pi_result.sanity_result.label_def.name,
    )

    # hpo_result 里应包含:
    #   - best_model_type
    #   - best_params
    #   - best_score
    #   - metric_name
    #   - backtest_summary（可选）

    run_id = brain_cfg.get("run_id") or str(uuid.uuid4())

    ctx = ModelRunContext.from_pi_result(
        run_id=run_id,
        pi_result=pi_result,
        table=exec_plan.table,
        symbol=exec_plan.symbol,
        start_ts=exec_plan.start_ts,
        end_ts=exec_plan.end_ts,
        freq=brain_cfg.get("freq", "1m"),
        execution_prompt_id=exec_plan.execution_prompt_id,
        base_prompt_id=exec_plan.base_prompt_id,
    )

    ctx.best_model_type = hpo_result.best_model_type
    ctx.best_params = hpo_result.best_params
    ctx.best_score = hpo_result.best_score
    ctx.metric_name = hpo_result.metric_name
    ctx.backtest_summary = getattr(hpo_result, "backtest_summary", None)

    # 把 prompt 选择原因 和 dataset 体检摘要挂在 notes 上
    ctx.notes = "\n".join(
        [
            f"[PromptSelection] {exec_plan.prompt_selection_reason}",
            "[DatasetQualitySummary]",
            dataset_summary_txt,
        ]
    )

    # -------- 6) 写入 JSONL（和之前 run logger 一致） --------
    runs_path = Path(brain_cfg["model_runs_path"])
    runs_path.parent.mkdir(parents=True, exist_ok=True)
    with runs_path.open("a", encoding="utf-8") as f:
        f.write(ctx.to_json() + "\n")

    return ctx
```

> 这就是完整的“AI 大脑控制机器学习”的第一条闭环路径：
> **历史表现 → prompt 决策 → PI（特征+标签+体检）→ HPO → 结果回写 → 继续给下一轮提供经验。**

---

## 本轮完成的关键效果

1. **所有“执行 prompt”都不再是硬编码字符串，而是：**

   * 来自 `prompt_library.json` 的白名单
   * 经 `PromptSelector` 按历史表现挑选
   * 写入 `ModelRunContext` 里，变成可审计的元数据

2. **AI 大脑不再直接碰 SQL / Label / 特征公式：**

   * 它只负责决定：**“我这次用哪个 prompt、哪个 label_id、哪类模型池”**
   * SQL、Label shift、特征公式都由 Python Registry / 合约层实现

3. **每一次 run 都变成：**

   * 有 prompt 身份
   * 有数据体检报告
   * 有训练结果（score / backtest）
   * 这些都进入 `model_runs.jsonl`，供下一轮决策

---

## 下一轮要干什么？

到这一轮，**训练侧闭环已经建立**。
下一步按我们最初的总蓝图，重点有两个方向可以继续：

1. **把同一套 “BrainExecutionPlan + PromptRegistry + HistoryIndex” 扩展到“实盘决策 LLM”那一侧**

   * 让 `v3_fast_market_following_*` 这些 prompt 在**真实盘口决策**中也有同样的溯源与评估逻辑
   * 用“对数收益 / DD / 命中率 / 滑点”等真实交易数据评价每个 prompt

2. **完善 FeatureRegistry & LabelRegistry 的具体内容**

   * 真正把你现在散落在 feature_programs.json、各种 SQL 里的特征/标签梳理进 Registry
   * 给出一个“最小可用版本”的 Registry 实例（带 10~20 个核心特征 + 2~3 种标准标签）

为了继续保持节奏，我建议**下一轮先做方向 2**：

> 直接给你写一个 `feature_registry.json` + `label_registry.json` 的**模板版本**，
> 并给出对应的加载类（类似 PromptRegistry），让你可以开始一点一点把现有特征和标签迁进来。

你只要再说一句：**“继续”**，我下一步就专门落地 **FeatureRegistry & LabelRegistry 的最小可用版本 + 加载/校验代码**，把“数据/特征/标签”这一块真正收口到一个可治理的抽象层里。
好，我们按计划把 **FeatureRegistry + LabelRegistry 最小可用版本**落地，让：

* 特征 → 有“户口本”和“身份证”（registry）
* 标签 → 从**注册表生成**，不是 LLM 随口写 SQL
* PI / AI 大脑 → 只能在**白名单特征 + 白名单标签**里活动

我分 4 步走：

1. 设计 `feature_registry.json` 模板
2. 实现 `FeatureRegistry` Python 类
3. 设计 `label_registry.json` 模板
4. 改造 `PrincipalInvestigator`，挂上这两个 registry（保持向后兼容）

---

## 1. `feature_registry.json`：特征户口本（最小可用版）

新文件建议放：`config/feature_registry.json`

这是一个**最小版 schema + 示例**，你可以先填一部分常用特征，后面慢慢补：

```jsonc
{
  "_comment": "Feature Registry - Defines allowed features per table",
  "_version": "1.0",

  "tables": {
    "bars_1m_features": {
      "symbol_column": "symbol",
      "ts_column": "ts",
      "allow_pi": true,

      "features": {
        "close": {
          "kind": "base",
          "dtype": "float",
          "column": "close",
          "description": "1m bar close price"
        },
        "open": {
          "kind": "base",
          "dtype": "float",
          "column": "open"
        },
        "high": {
          "kind": "base",
          "dtype": "float",
          "column": "high"
        },
        "low": {
          "kind": "base",
          "dtype": "float",
          "column": "low"
        },
        "volume": {
          "kind": "base",
          "dtype": "float",
          "column": "volume"
        },

        "ret_1b": {
          "kind": "derived",
          "dtype": "float",
          "sql_expr": "close / lag(close, 1) - 1",
          "depends_on": ["close"],
          "description": "1-bar simple return"
        },

        "vwap": {
          "kind": "derived",
          "dtype": "float",
          "sql_expr": "vwap",
          "depends_on": ["vwap"],
          "description": "pre-computed VWAP in table"
        },

        "rsi_14": {
          "kind": "derived",
          "dtype": "float",
          "sql_expr": "rsi_14",
          "depends_on": ["rsi_14"],
          "description": "14-period RSI precomputed upstream"
        }
      }
    }
  }
}
```

要点：

* 后端 ClickHouse 的真实列名写在 `column` 或 `sql_expr` 里；
* `kind` 目前先支持 `"base"` / `"derived"`，未来可以扩展 `"lag"`, `"rolling"` 等；
* **PI / LLM 只能用这里登记过的特征名**（如 `"ret_1b"`, `"vwap"`），不能自己造新列名。

> 先不用一次性把所有特征都登记进去，**先把最常用的 10–20 个做起来**，后面慢慢演化。

---

## 2. `FeatureRegistry`：Python 加载 + 校验

新文件：`feature_registry.py`

```python
# feature_registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import json


@dataclass
class FeatureDef:
    name: str
    kind: str          # "base" / "derived" / ...
    dtype: str         # "float" / "int" / "bool" / ...
    column: Optional[str] = None  # 对应的物理列名（base 特征常用）
    sql_expr: Optional[str] = None  # 生成该特征的 SQL 表达式
    depends_on: List[str] = None
    description: str = ""

    def is_base(self) -> bool:
        return self.kind == "base"

    def is_derived(self) -> bool:
        return self.kind == "derived"


@dataclass
class TableFeatureConfig:
    table: str
    symbol_column: str
    ts_column: str
    allow_pi: bool
    features: Dict[str, FeatureDef]


class FeatureRegistry:
    """
    中央特征注册表：
    - 从 JSON 文件加载
    - 针对每个 table 返回允许的 FeatureDef 列表
    - 对 PI/LLM 输出的 feature_recipe 做合法性校验
    """

    def __init__(self, tables: Dict[str, TableFeatureConfig]) -> None:
        self._tables = tables

    # --------- 加载 ---------
    @classmethod
    def from_file(cls, path: Path) -> "FeatureRegistry":
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        tables_cfg: Dict[str, TableFeatureConfig] = {}
        tables_raw = raw.get("tables", {})
        for tbl_name, tbl in tables_raw.items():
            feats: Dict[str, FeatureDef] = {}
            for fname, fconf in tbl.get("features", {}).items():
                fd = FeatureDef(
                    name=fname,
                    kind=fconf["kind"],
                    dtype=fconf.get("dtype", "float"),
                    column=fconf.get("column"),
                    sql_expr=fconf.get("sql_expr"),
                    depends_on=fconf.get("depends_on", []),
                    description=fconf.get("description", ""),
                )
                feats[fname] = fd

            tcfg = TableFeatureConfig(
                table=tbl_name,
                symbol_column=tbl.get("symbol_column", "symbol"),
                ts_column=tbl.get("ts_column", "ts"),
                allow_pi=bool(tbl.get("allow_pi", True)),
                features=feats,
            )
            tables_cfg[tbl_name] = tcfg

        return cls(tables_cfg)

    # --------- 查询接口 ---------
    def has_table(self, table: str) -> bool:
        return table in self._tables

    def get_table_config(self, table: str) -> TableFeatureConfig:
        if table not in self._tables:
            raise KeyError(f"Unknown feature table: {table}")
        return self._tables[table]

    def list_feature_names(self, table: str) -> List[str]:
        return sorted(self.get_table_config(table).features.keys())

    def get_feature(self, table: str, name: str) -> FeatureDef:
        tcfg = self.get_table_config(table)
        if name not in tcfg.features:
            raise KeyError(f"Feature '{name}' not registered for table '{table}'")
        return tcfg.features[name]

    # --------- 校验 PI 的 feature_recipe ---------
    def validate_recipe(self, table: str, recipe: Dict[str, Any]) -> Dict[str, Any]:
        """
        对 LLM 生成的 feature_recipe 进行“安全收紧”：
        - base_features 必须在 registry 里
        - derived 列表中的 alias 和依赖也必须在 registry 里
        - 如有未知特征，直接剔除或报错（当前策略：剔除 + 记录）
        """
        tcfg = self.get_table_config(table)

        base = recipe.get("base_features", []) or []
        derived = recipe.get("derived", []) or []

        allowed = set(tcfg.features.keys())

        # 1) base_features 过滤
        clean_base = [f for f in base if f in allowed]

        # 2) derived 别名 / depends_on 校验
        clean_derived = []
        for d in derived:
            alias = d.get("alias")
            source = d.get("source")  # LLM 可能给出 SQL 片段，暂时忽略
            uses = d.get("uses", []) or []

            if alias and alias in allowed:
                # registry 中已定义该特征，则允许
                clean_derived.append(d)
            else:
                # alias 不在 registry，看看是否可以“映射”到某个已存在特征
                # 当前版本：直接丢弃，防止 LLM 造新列名
                continue

            # 依赖特征是否都在 allowed 中？
            bad_dep = [u for u in uses if u not in allowed]
            if bad_dep:
                # 简单做法：记录一下，暂不强制报错
                d["_ignored_bad_dep"] = bad_dep

        recipe_sane = dict(recipe)
        recipe_sane["base_features"] = clean_base
        recipe_sane["derived"] = clean_derived
        return recipe_sane
```

> 以后，任何地方要搞特征列表，都应该从 `FeatureRegistry` 出发，
> 而不是让 LLM 自己写 `SELECT foo, bar, baz`。

---

## 3. `label_registry.json`：标签定义合约（防止泄露 / 幻觉）

目标：**标签定义全部在 Python 里实现，Registry 只描述参数，不存 SQL 表达式**。

新文件：`config/label_registry.json`

```jsonc
{
  "_comment": "Label Registry - defines allowed targets for training",
  "_version": "1.0",

  "labels": {
    "ret_10b_fwd_reg": {
      "name": "ret_10b_fwd_reg",
      "task": "reg",                 // "reg" / "cls"
      "kind": "simple_return",       // 用 Python 代码解释
      "horizon": 10,                 // 10 bars
      "horizon_unit": "bars",
      "price_column": "close",
      "clip_min": -0.2,
      "clip_max": 0.2,
      "null_policy": "drop"          // "drop" / "keep"
    },

    "cls_updown_10b": {
      "name": "cls_updown_10b",
      "task": "cls",
      "kind": "threshold_from_label",
      "base_label": "ret_10b_fwd_reg",
      "pos_threshold": 0.0,
      "neg_threshold": 0.0,
      "labels": {
        "neg": 0,
        "pos": 1
      },
      "null_policy": "drop"
    }

    // 未来可以加 triple-barrier, path-dependent 等种类
  }
}
```

> 这样做的好处：**LLM 再也无法直接写 label SQL**，所有 forward-looking 的 shift
> 都在我们可控的 Python 函数里完成，DataLeak 风险一下大幅下降。

---

## 4. `LabelRegistry`：Python 实现 + 标签计算

新文件：`label_registry.py`

```python
# label_registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import json
import pandas as pd
import numpy as np


@dataclass
class LabelDef:
    name: str
    task: str            # "reg" / "cls"
    kind: str            # "simple_return" / "threshold_from_label" / ...
    horizon: int
    horizon_unit: str    # currently only "bars"
    price_column: str = "close"
    clip_min: Optional[float] = None
    clip_max: Optional[float] = None
    null_policy: str = "drop"

    # for threshold_from_label
    base_label: Optional[str] = None
    pos_threshold: Optional[float] = None
    neg_threshold: Optional[float] = None
    labels: Optional[Dict[str, int]] = None

    def compute(self, df: pd.DataFrame) -> pd.Series:
        """
        用于 simple_return 的最小实现：
        ret_10b_fwd = close[t+10] / close[t] - 1.0

        NOTE: 我们只用 shift(-horizon)，不让任何人写自己的 SQL。
        """
        if self.kind == "simple_return":
            if self.horizon_unit != "bars":
                raise ValueError(f"Unsupported horizon_unit: {self.horizon_unit}")
            if self.price_column not in df.columns:
                raise KeyError(f"price_column '{self.price_column}' not found in df")

            px = df[self.price_column].astype(float)
            fwd_px = px.shift(-self.horizon)
            ret = fwd_px / px - 1.0

            if self.clip_min is not None or self.clip_max is not None:
                ret = ret.clip(lower=self.clip_min, upper=self.clip_max)

            return ret

        raise NotImplementedError(f"Label kind '{self.kind}' not implemented in compute()")


class LabelRegistry:
    """
    负责编排：
    - 从 JSON 加载所有 LabelDef
    - 提供 get_label()，并且可根据 LabelDef 把列计算到 df 上
    """

    def __init__(self, labels: Dict[str, LabelDef]) -> None:
        self._labels = labels

    @classmethod
    def from_file(cls, path: Path) -> "LabelRegistry":
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        labels_cfg: Dict[str, LabelDef] = {}
        for lid, conf in raw.get("labels", {}).items():
            ld = LabelDef(
                name=conf["name"],
                task=conf["task"],
                kind=conf["kind"],
                horizon=int(conf.get("horizon", 0)),
                horizon_unit=conf.get("horizon_unit", "bars"),
                price_column=conf.get("price_column", "close"),
                clip_min=conf.get("clip_min"),
                clip_max=conf.get("clip_max"),
                null_policy=conf.get("null_policy", "drop"),
                base_label=conf.get("base_label"),
                pos_threshold=conf.get("pos_threshold"),
                neg_threshold=conf.get("neg_threshold"),
                labels=conf.get("labels"),
            )
            labels_cfg[lid] = ld

        return cls(labels_cfg)

    def get(self, label_id: str) -> LabelDef:
        if label_id not in self._labels:
            raise KeyError(f"Unknown label_id: {label_id}")
        return self._labels[label_id]

    # shortcut：直接在 df 上添加 label 列
    def apply_label(self, df: pd.DataFrame, label_id: str, column_name: Optional[str] = None) -> pd.DataFrame:
        label_def = self.get(label_id)
        col = column_name or label_def.name
        s = label_def.compute(df)
        df = df.copy()
        df[col] = s
        if label_def.null_policy == "drop":
            df = df.dropna(subset=[col])
        return df
```

> 这个版本先只实现 `simple_return`，
> 你以后需要 triple-barrier、path-dependent，可以在 `compute()` 里扩展。

---

## 5. 把 Registry 接入 `PrincipalInvestigator`（向后兼容）

现在我们给 `PrincipalInvestigator` 加两个可选参数：

* `feature_registry_path: Optional[Path]`
* `label_registry_path: Optional[Path]`

> 如果不传，就维持你现在“自由模式”；
> 如果传了，就启用“强治理模式”。

### 5.1 更新 `PIResult` dataclass

在原来的基础上，加上 label_def / sanity_result（如果你前面已经加了，可以略过）：

```python
# pi/principal_investigator.py

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import pandas as pd

from feature_registry import FeatureRegistry, TableFeatureConfig, FeatureDef
from label_registry import LabelRegistry, LabelDef
from dataset_sanity import DatasetSanityResult, run_dataset_sanity_checks  # 之前已经设计过


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # NEW:
    table: str = ""
    symbol: str = ""
    label_id: Optional[str] = None
    label_def: Optional[LabelDef] = None
    sanity_result: Optional[DatasetSanityResult] = None
    dataset_quality_summary: Optional[Dict[str, Any]] = None
```

### 5.2 更新 `PrincipalInvestigator.__init__`

```python
class PrincipalInvestigator:
    """LLM-driven feature + training plan generator, but constrained by registry."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        feature_registry_path: Optional[str] = None,
        label_registry_path: Optional[str] = None,
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

        # NEW: 可选加载特征/标签 registry
        self._feature_registry: Optional[FeatureRegistry] = None
        if feature_registry_path:
            self._feature_registry = FeatureRegistry.from_file(Path(feature_registry_path))

        self._label_registry: Optional[LabelRegistry] = None
        if label_registry_path:
            self._label_registry = LabelRegistry.from_file(Path(label_registry_path))
```

### 5.3 更新 `propose()`：用 Registry 收紧特征 + 生成标签

核心改动几点：

1. 如果有 `FeatureRegistry`，先用官方 catalog 替换 `feature_catalog`；
2. 对 LLM 输出的 `feature_recipe` 做 `validate_recipe()`；
3. 如果有 `LabelRegistry`，忽略 LLM 提供的 `target` 字段，改用 `default_label_id`；
4. 从 `df` 出来后，调用 `LabelRegistry.apply_label()` 生成 label 列；
5. 跑 `run_dataset_sanity_checks` 做体检。

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
        sanity_cfg: Optional[Dict[str, Any]] = None,
        default_label_id: Optional[str] = None,
    ) -> PIResult:
        # ---------- 1) 确定可用特征目录（catalog） ----------
        if self._feature_registry and self._feature_registry.has_table(table):
            tcfg = self._feature_registry.get_table_config(table)
            catalog = self._feature_registry.list_feature_names(table)
        else:
            # fallback: 直接从 system.columns 读（旧行为）
            catalog = _fetch_catalog(self._client, table)
            tcfg = None  # no registry

        # ---------- 2) 调 LLM 拿 plan（但不直接信任） ----------
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
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe_raw = plan.get("feature_recipe", {}) or {}
        tplan_raw = plan.get("training_plan", {}) or {}

        # ---------- 3) 用 FeatureRegistry 收紧 feature_recipe ----------
        if self._feature_registry and tcfg:
            recipe = self._feature_registry.validate_recipe(table, recipe_raw)
        else:
            recipe = recipe_raw

        tplan = dict(tplan_raw)

        # ---------- 4) Label 选择：用 LabelRegistry + 参数，而不是 LLM 的 target ----------
        label_id: Optional[str] = default_label_id
        label_def: Optional[LabelDef] = None

        if not label_id:
            # 如果没传 default_label_id，可以从 tplan 里读一个 id，
            # 但仍然必须通过 registry 校验
            candidate = tplan.get("label_id")
            if candidate and self._label_registry:
                label_id = candidate

        if self._label_registry and label_id:
            label_def = self._label_registry.get(label_id)
            # 确保 task 对齐
            tplan["task"] = label_def.task
            tplan["label_id"] = label_id
            # 注意：绝对不要信任 LLM 提供的 "target" SQL
            tplan.pop("target", None)
        else:
            # 没有 label registry 时，保留旧行为（但不推荐）
            label_id = None
            label_def = None

        # ---------- 5) 构造特征 + label 的 SELECT SQL ----------
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,  # 不再让 LLM 提供 target SQL
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # ---------- 6) 在 Python 层计算 label 列 ----------
        if self._label_registry and label_id:
            df = self._label_registry.apply_label(df, label_id=label_id)
        # 否则保留原 df，不额外加列（旧模式）

        # ---------- 7) 生成 HPO grid ----------
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

        # ---------- 8) 数据体检 ----------
        sanity_result: Optional[DatasetSanityResult] = None
        quality_summary: Optional[Dict[str, Any]] = None
        if sanity_cfg:
            sanity_result = run_dataset_sanity_checks(df, sanity_cfg)
            quality_summary = sanity_result.to_summary_dict()

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            table=table,
            symbol=symbol,
            label_id=label_id,
            label_def=label_def,
            sanity_result=sanity_result,
            dataset_quality_summary=quality_summary,
        )
```

> 到这里为止，你的 **PI → HPO → Brain Orchestrator** 已经有了完整“铁三角约束”：
>
> * 特征：来自 `FeatureRegistry`
> * 标签：来自 `LabelRegistry`
> * Prompt：来自 `PromptRegistry` + RunHistory 策略选择

---

## 接下来怎么做？

这一轮我们已经：

* 给特征、标签都立了“户口本 + Python 合约”；
* 把 PI 从“LLM 自由发挥”收紧到“LLM 在白名单里调参”；
* 保留了老行为（不传 registry 路径就用旧逻辑），方便你渐进迁移。

**下一步（我会自动往前推，不用你再指挥）：**

* 给你一个 **最小可用的 `feature_registry.json` + `label_registry.json` 实例**，
  针对你现在最常用的 `bars_1m_features`、1m/5m 基础价量 + 常用指标 + 1–3 个标准 label。

并且会：

* 给一个小的 **迁移 checklist**：
  哪些 feature 先登记、哪些 label 先迁入，
  以及如何一步步把旧的 feature_programs.json 收口到 registry。

你只要继续说一句：**“继续”**，我就直接给你这两个 registry 的“首批条目模板 + TODO 清单”，可以边改边用。
