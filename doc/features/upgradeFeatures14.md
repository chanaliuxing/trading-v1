好，这一轮我们把 **“AI 大脑 → 机器学习”** 这一段彻底收紧成一个**有契约的流水线**：

1. 给 PI（Principal Investigator）定义一个 **严格的 JSON 契约**（Pydantic schema）。
2. 给 LLM 一个 **固定的 system prompt 模板**，强制按这个契约输出。
3. 新增 **Training Gate（训练闸门）**，在 dataset_sanity 之后再加一层“能训/不能训”的硬判断。
4. 把这些东西**插回 `PrincipalInvestigator.propose()`**，形成完整闭环。

---

## 一、PI 输出契约：`pi/schemas.py`

新建文件：`pi/schemas.py`
（用 Pydantic v1 写法，基本所有环境都兼容）

```python
# pi/schemas.py

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, validator


class DerivedFeatureRef(BaseModel):
    """
    LLM 只能引用 FeatureRegistry 中的衍生特征 id，不允许乱写 SQL。
    """
    id: str = Field(..., description="FeatureRegistry.derived 中的 feature id")
    alias: Optional[str] = Field(
        None,
        description="在训练数据里的列名，不写则用 registry 默认 output_name",
    )


class FeatureRecipeV1(BaseModel):
    """
    告诉系统：用哪个表、哪些原始特征、哪些已登记的衍生特征。
    """
    use_table: Optional[str] = Field(
        None,
        description="如为空则使用调用方传入的 table 名",
    )
    base_features: List[str] = Field(
        default_factory=list,
        description="原始列名（必须出现在 ClickHouse 表和 FeatureRegistry.columns 中）",
    )
    derived: List[DerivedFeatureRef] = Field(
        default_factory=list,
        description="引用 FeatureRegistry.derived 的特征 id 列表",
    )
    notes: Optional[str] = Field(
        None,
        description="可选的说明，方便人类审阅",
    )


class DecisionQuantileConfig(BaseModel):
    """
    简单版本：回归 + 分位数决策。后续可以扩展 mode.
    """
    mode: Literal["quantile"] = "quantile"
    q_long: float = 0.7
    q_short: float = 0.3

    @validator("q_long", "q_short")
    def _check_range(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("q_long / q_short 必须在 (0, 1) 区间内")
        return v

    @validator("q_long")
    def _check_order(cls, v: float, values: Dict[str, Any]) -> float:
        q_short = values.get("q_short")
        if q_short is not None and not (q_short < v):
            raise ValueError("q_short 必须 < q_long")
        return v


class ModelCandidate(BaseModel):
    """
    单个模型候选，用于 HPO grid。
    """
    model_type: str = Field(
        ...,
        description="模型类型，如 'xgb', 'lasso', 'ridge', 'lightgbm' 等",
    )
    model_params: Dict[str, Any] = Field(
        default_factory=dict,
        description="该模型的超参数字典，不写则用默认参数",
    )


class TrainingPlanV1(BaseModel):
    """
    告诉系统：这次实验的任务类型/标签/模型池/决策规则。
    """
    symbol: str = Field(..., description="交易标的，例如 'NVDA'")
    task: Literal["reg", "clf"] = "reg"

    # LabelRegistry 中的 id，比如 "ret_10m_fwd", "tb_10_20_10" 等
    label_id: str = Field(..., description="在 LabelRegistry 中注册的标签 id")

    # 可选：覆盖 LabelRegistry 中的默认 horizon（例如 10 bar → 20 bar）
    label_horizon_bars: Optional[int] = Field(
        None,
        description="如不填则用 LabelRegistry 默认 horizon",
    )

    decision: DecisionQuantileConfig = Field(
        default_factory=DecisionQuantileConfig,
        description="将回归输出转成 long/short/flat 的配置",
    )

    model_candidates: List[ModelCandidate] = Field(
        default_factory=list,
        description="本次 HPO 的模型池，如果为空，将自动插入一个默认 xgb 模型",
    )

    notes: Optional[str] = Field(
        None,
        description="策略意图/特殊约束的人类说明",
    )

    @validator("model_candidates", always=True)
    def _ensure_at_least_one_model(cls, v: List[ModelCandidate]) -> List[ModelCandidate]:
        if not v:
            # 给一个非常保守的默认 xgb
            return [
                ModelCandidate(
                    model_type="xgb",
                    model_params={
                        "max_depth": 5,
                        "n_estimators": 200,
                        "learning_rate": 0.05,
                        "subsample": 0.8,
                        "colsample_bytree": 0.8,
                    },
                )
            ]
        return v


class PIPlanV1(BaseModel):
    """
    LLM 必须输出的顶层对象。
    """
    version: Literal["pi_plan_v1"] = "pi_plan_v1"
    feature_recipe: FeatureRecipeV1
    training_plan: TrainingPlanV1
```

> 这就是 **“AI 大脑的 JSON 契约”**：只要 LLM 输出的 JSON 能被 `PIPlanV1` 解析通过，就意味着：
>
> * 特征只在你允许的空间里选；
> * 标签只在 LabelRegistry 里选；
> * 决策/模型池结构稳定；
> * 没有乱七八糟的 field。

---

## 二、给 LLM 的 system prompt 模板（PI 专用）

你现在的 `PrincipalInvestigator.propose()` 里面 system_prompt 很简单，我们升级一下，让它：

* 清楚地告诉 LLM **输出结构**；
* 告诉它**有哪些字段**，每个字段的含义；
* 强调“不要写 SQL，只写 id 和列名”。

你可以新建一个小模块：`pi/prompts.py`：

```python
# pi/prompts.py

PI_SYSTEM_PROMPT_V1 = """
你是量化研究平台的首席科学家 (Principal Investigator, PI)。

你的任务是：
- 在一个**已定义好的特征和标签空间**内，
- 为给定的交易标的、时间区间和目标，设计：
  - 一份 feature_recipe（用哪些特征）；
  - 一份 training_plan（用什么标签、任务类型、模型池、决策规则）。

⚠️ 重要约束（非常重要）：
1. 你不能自己编写 SQL 表达式。
   - 衍生特征只能通过 id 引用 FeatureRegistry.derived 中的特征。
   - 你只需要写 `{"id": "ret_1", "alias": "ret_1"}` 这样的结构。
2. 你不能自由发明标签。
   - 你只能从 LabelRegistry 中的标签 id 里选择，比如：
     - "ret_10m_fwd"
     - "tb_10_20_10"
   - 具体可用列表由上游在 user_prompt 中提供。
3. 你必须只输出一个 JSON 对象，结构为：

{
  "version": "pi_plan_v1",
  "feature_recipe": {
    "use_table": "<可选，留空则用上游传入的表名>",
    "base_features": ["close", "volume", "..."],
    "derived": [
      { "id": "ret_1", "alias": "ret_1" },
      { "id": "rv_20", "alias": "rv_20" }
    ],
    "notes": "可选说明"
  },
  "training_plan": {
    "symbol": "NVDA",
    "task": "reg",
    "label_id": "ret_10m_fwd",
    "label_horizon_bars": 10,
    "decision": {
      "mode": "quantile",
      "q_long": 0.7,
      "q_short": 0.3
    },
    "model_candidates": [
      {
        "model_type": "xgb",
        "model_params": {
          "max_depth": 5,
          "n_estimators": 400,
          "learning_rate": 0.05
        }
      }
    ],
    "notes": "例如：侧重开盘后30分钟内的动量"
  }
}

4. 你不能输出任何多余文本（如解释、Markdown、注释），只能输出 JSON。
"""
```

> 以后如果你要控制 PI 的行为（例如“今天研究 ORCL 的 mean reversion 策略”），只需要在 `user_prompt` 注入目标、可用 label 列表、可用 derived id 列表即可。

---

## 三、Training Gate（训练闸门）：`ml/training_gate.py`

dataset_sanity 更偏“体检 + 报告”；
Training Gate 是一行话：“**这玩意儿到底能不能训**”。

新建：`ml/training_gate.py`：

```python
# ml/training_gate.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class TrainingGateConfig:
    """
    简单的默认阈值，可以后续根据经验/统计调整。
    """
    min_rows: int = 5_000                 # 数据太少直接不训
    max_null_frac_per_col: float = 0.2    # 单列最多 20% NaN
    max_constant_col_frac: float = 0.5    # 常数列超过 50% 就当垃圾
    min_label_nonnull_frac: float = 0.9   # 标签非空比例
    min_label_pos_frac: float = 0.05      # label > 0 的比例，避免几乎都是一边
    min_label_neg_frac: float = 0.05      # label < 0 的比例


@dataclass
class TrainingGateResult:
    ok: bool
    reasons: List[str]
    metrics: Dict[str, Any]


def run_training_gate(
    df: pd.DataFrame,
    *,
    label_col: str,
    cfg: Optional[TrainingGateConfig] = None,
) -> TrainingGateResult:
    """
    基于简单可解释的指标，判断这批数据是否值得进入训练/HPO。

    - 不做复杂统计，只做“保底防守线”：
      - 行数足够多
      - NaN 不离谱
      - 常数列比例不离谱
      - 标签非空比例 & 两边样本都够
    """
    if cfg is None:
        cfg = TrainingGateConfig()

    reasons: List[str] = []
    metrics: Dict[str, Any] = {}

    n_rows = int(len(df))
    metrics["n_rows"] = n_rows

    if n_rows < cfg.min_rows:
        reasons.append(f"rows < min_rows: {n_rows} < {cfg.min_rows}")

    # ---- NaN 统计 ----
    null_fracs = df.isna().mean()
    max_null_frac = float(null_fracs.max() if len(null_fracs) else 0.0)
    metrics["max_null_frac_per_col"] = max_null_frac

    if max_null_frac > cfg.max_null_frac_per_col:
        reasons.append(
            f"max_null_frac_per_col={max_null_frac:.3f} > "
            f"{cfg.max_null_frac_per_col:.3f}"
        )

    # ---- 常数列统计 ----
    constant_cols: List[str] = []
    for col in df.columns:
        # 忽略时间和 symbol 这类结构列可以自行过滤，这里先全部扫描
        nunique = df[col].nunique(dropna=True)
        if nunique <= 1:
            constant_cols.append(col)

    const_frac = float(len(constant_cols) / max(len(df.columns), 1))
    metrics["constant_col_frac"] = const_frac
    metrics["constant_cols"] = constant_cols

    if const_frac > cfg.max_constant_col_frac:
        reasons.append(
            f"constant_col_frac={const_frac:.3f} > {cfg.max_constant_col_frac:.3f}"
        )

    # ---- 标签分布 ----
    if label_col not in df.columns:
        reasons.append(f"label_col={label_col!r} 不在 DataFrame 列中")
        return TrainingGateResult(ok=False, reasons=reasons, metrics=metrics)

    label_series = df[label_col]
    label_notna = label_series.dropna()
    n_label = int(len(label_notna))
    label_nonnull_frac = float(n_label / max(n_rows, 1))
    metrics["label_nonnull_frac"] = label_nonnull_frac

    if label_nonnull_frac < cfg.min_label_nonnull_frac:
        reasons.append(
            f"label_nonnull_frac={label_nonnull_frac:.3f} < "
            f"{cfg.min_label_nonnull_frac:.3f}"
        )

    if n_label > 0:
        # 简单假设：label 为实数，>0 视为“正样本”，<0 视为“负样本”
        arr = label_notna.to_numpy(dtype=float)
        pos_frac = float((arr > 0).mean())
        neg_frac = float((arr < 0).mean())
        metrics["label_pos_frac"] = pos_frac
        metrics["label_neg_frac"] = neg_frac

        if pos_frac < cfg.min_label_pos_frac:
            reasons.append(
                f"label_pos_frac={pos_frac:.3f} < {cfg.min_label_pos_frac:.3f}"
            )
        if neg_frac < cfg.min_label_neg_frac:
            reasons.append(
                f"label_neg_frac={neg_frac:.3f} < {cfg.min_label_neg_frac:.3f}"
            )
    else:
        reasons.append("标签列全部为 NaN")

    ok = len(reasons) == 0
    return TrainingGateResult(ok=ok, reasons=reasons, metrics=metrics)
```

> 这个 Gate 是**强约束**：
>
> * 通过 → 才允许进入 HPO / 训练；
> * 不通过 → 直接 FAIL，理由写在 `reasons` 里，喂给 AI 大脑 & 日志。

---

## 四、把这一切接回 `PrincipalInvestigator.propose()`

现在我们需要把几个东西串起来：

1. 调 LLM → 得到 raw JSON；
2. 用 `PIPlanV1` 解析校验；
3. 用 `build_select_sql()` 拉特征；
4. 用 `LabelRegistry` 打标签；
5. 用 `run_dataset_sanity_checks()` 做体检（你前面已经有了）；
6. 用 `run_training_gate()` 决定训不训；
7. 构造 HPO grid。

下面是 **新的 `propose` 方法**（只贴方法体，你可以直接替换 class 里的同名方法）：

```python
# pi/principal_investigator.py (只展示 propose 方法重写版本)

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql
from pi.schemas import PIPlanV1
from pi.prompts import PI_SYSTEM_PROMPT_V1
from features.registry import get_registry as get_feature_registry
from labels.registry import (
    get_registry as get_label_registry,
    add_label_column,
)
from sanity.dataset_sanity import run_dataset_sanity_checks
from ml.training_gate import TrainingGateConfig, run_training_gate


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame  # 已包含 label 列
    hpo_grid: Dict[str, Any]


def _fetch_catalog(client, table: str) -> List[str]:
    q = "SELECT name FROM system.columns WHERE table = {table:String}"
    df = client.query_df(q, parameters={"table": table})
    return sorted(df["name"].tolist())


class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(self, ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None) -> None:
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
        # 1. 拉 ClickHouse schema
        catalog = _fetch_catalog(self._client, table)

        # 2. 构造 user_prompt，把“可用特征 & 可用 label”告诉 LLM
        feat_reg = get_feature_registry()
        label_reg = get_label_registry()

        available_base = feat_reg.list_columns()
        available_derived = feat_reg.list_derived_ids()
        available_labels = label_reg.list_label_ids()  # 你在 LabelRegistry 里实现这个方法

        user_prompt = (
            f"研究目标: {goal}\n"
            f"训练表: {table}\n"
            f"标的: {symbol}\n"
            f"时间区间: {start_ts} ~ {end_ts}\n"
            f"预测窗口: {horizon}\n\n"
            f"[可用 base_features]\n{available_base}\n\n"
            f"[可用 derived feature id]\n{available_derived}\n\n"
            f"[可用 label_id]\n{available_labels}\n"
        )

        # 3. 调 LLM，拿到 raw JSON
        raw_plan = self._llm.generate(
            system_prompt=PI_SYSTEM_PROMPT_V1,
            user_prompt=user_prompt,
        )

        if isinstance(raw_plan, str):
            try:
                raw_plan_obj = json.loads(raw_plan)
            except json.JSONDecodeError as e:
                raise ValueError(f"PI LLM 输出不是合法 JSON: {e} | content={raw_plan[:200]!r}")
        elif isinstance(raw_plan, dict):
            raw_plan_obj = raw_plan
        else:
            raise TypeError(f"PI LLM 输出类型不支持: {type(raw_plan)}")

        # 4. 用 Pydantic 做强校验
        plan = PIPlanV1.parse_obj(raw_plan_obj)
        recipe = plan.feature_recipe.dict()
        tplan = plan.training_plan.dict()

        # 5. 构造安全 SQL（没有 label 表达式，只拉特征）
        sql = build_select_sql(
            table=recipe.get("use_table") or table,
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,  # target 不再放 SQL，全部在 Python 打标签
            feature_catalog=catalog,
        )

        df = self._client.query_df(sql)

        # 6. 在 Python 里打标签（LabelRegistry 保证无泄露）
        label_id = tplan["label_id"]
        label_spec = label_reg.get_spec(label_id)

        label_horizon = tplan.get("label_horizon_bars") or label_spec.default_horizon_bars
        df = add_label_column(
            df=df,
            label_id=label_id,
            horizon_bars=label_horizon,
            cfg=None,  # 如有额外配置可在 tplan 中扩展
        )
        label_col = label_spec.output_name

        # 7. 数据体检（sanity），如果发现严重问题可以在函数里直接 raise
        _ = run_dataset_sanity_checks(df, label_col=label_col)

        # 8. 训练闸门（战术硬阈值）
        gate_cfg = TrainingGateConfig()
        gate_result = run_training_gate(df, label_col=label_col, cfg=gate_cfg)
        if not gate_result.ok:
            # 这里你可以选择 raise，或者返回一个特殊状态给上游
            reason_str = "; ".join(gate_result.reasons)
            raise ValueError(f"Training gate failed for {symbol}: {reason_str}")

        # 9. 构造 HPO grid
        # 注意：feature_subset 只用 base_features+derived alias，不含 label
        derived_aliases = [d.get("alias") for d in (recipe.get("derived") or []) if d.get("alias")]
        feature_subset = (recipe.get("base_features") or []) + derived_aliases

        grid = {
            "model_type": [],
            "feature_subset": [feature_subset],
            "task": [tplan.get("task", "reg")],
            "decision": [tplan.get("decision", {"mode": "quantile", "q_long": 0.7, "q_short": 0.3})],
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
        )
```

> 到这里，`PrincipalInvestigator.propose()` 已经从一个“随便调用 LLM 拼 SQL 的危险函数”，变成一个：
>
> * **有 schema 的 LLM 规划器**（PIPlanV1）；
> * **有白名单特征 & 标签空间**；
> * **有数据体检 + 训练闸门**；
> * **输出稳定 HPO grid** 的“研究工厂入口”。

---

## 五、简单测试计划（专门测这条链路）

现在你可以用一个非常具体的 checklist 来测这条链路：

1. **Happy Path 测试（应当通过）**

   * 在 ClickHouse 里选一个你最干净的 1m 表，比如 `bars_1m`，NVDA 最近 60 天。
   * 在 `feature_registry.json` 里保证有：`["open","high","low","close","volume","vwap"]` + `ret_1/ret_5/rv_20`。
   * 在 `label_registry.json` 里保证有一个 `ret_10m_fwd`（time_return 类型）。
   * 调用 `PrincipalInvestigator.propose(...)`：

     * `goal="测试 ret_10m_fwd 的动量预测"`
     * `horizon="10m"`
   * 期望结果：

     * 不报错，返回 `PIResult`；
     * `dataset_df` 有 label 列；
     * HPO grid 至少有一个模型。

2. **数据不够多（应该被 Gate 拦下来）**

   * 把 `start_ts` 和 `end_ts` 缩短到只有十几分钟（几十行）。
   * 再调 `propose()`。
   * 期望：抛出 `Training gate failed... rows < min_rows`。

3. **标签极度不平衡（应该被 Gate 提醒）**

   * 用一个超短 horizon（比如 1 bar）+ 很平的标的（或者只取一小段几乎单边上升/下跌数据）。
   * 看 `run_training_gate` 的 `metrics["label_pos_frac"]` / `label_neg_frac`。
   * 你可以在日志里打印，当 < 0.05 时 Gate 拦截。

4. **LLM 输出错 schema（是否能被 Pydantic 抓住）**

   * 临时把 `PI_SYSTEM_PROMPT_V1` 改成故意少字段（比如不写 `version`），跑一遍。
   * 期望：`PIPlanV1.parse_obj` 抛出 ValidationError，而不是默默吃掉。

---

如果你愿意，下一步我会 **专门给 AI 大脑写一份 “工具说明书”**，把：

* FeatureRegistry 的“眼睛”；
* LabelRegistry 的“标签空间”；
* DatasetSanity + TrainingGate 的“红线”；
* HPO grid 的“可调旋钮”；

都整理成一份统一的“工具 API 文档 + Prompt 模板”，让 AI 大脑可以**自我循环研究策略**，但始终在你划好的轨道里运行。
好，那我们按计划往下“砌砖”，这一轮专注在两件事：

1. **把 Label 彻底标准化（LabelRegistry + 打标签函数）**
2. **真正把它接到刚才的 `PrincipalInvestigator.propose()` 里，让“标签不再乱写 SQL”**

这一轮结束之后，你的整条链路会变成：

> ClickHouse 拉特征 → Python 统一打 label → 体检 (sanity) → 训练闸门 (gate) → HPO

---

## 一、LabelRegistry 配置文件：`labels/registry.json`

先定义一个官方“标签清单”，以后 AI / 人都只能从这里选，不许乱发明 👇

```jsonc
{
  "_comment": "LabelRegistry – approved labels for AI Brain / PI",
  "_version": 1,
  "labels": [
    {
      "id": "ret_10m_fwd",
      "type": "time_return",
      "base_col": "close",
      "default_horizon_bars": 10,
      "output_name": "label_ret_10m_fwd",
      "params": {
        "mode": "log"  // log return: ln(price_{t+H} / price_t)
      },
      "description": "10-bar forward log return of close"
    },
    {
      "id": "ret_30m_fwd",
      "type": "time_return",
      "base_col": "close",
      "default_horizon_bars": 30,
      "output_name": "label_ret_30m_fwd",
      "params": {
        "mode": "log"
      },
      "description": "30-bar forward log return of close"
    },
    {
      "id": "tb_10_20_10",
      "type": "triple_barrier",
      "base_col": "close",
      "default_horizon_bars": 10,
      "output_name": "label_tb_10_20_10",
      "params": {
        "up_pct": 0.02,       // 上沿 +2%
        "down_pct": 0.01,     // 下沿 -1%
        "neutral_as": 0       // 没碰上下沿则记 0（neutral）
      },
      "description": "Triple barrier: +2% / -1% / 10 bars"
    }
  ]
}
```

> 你可以先只保留 `ret_10m_fwd` / `ret_30m_fwd` 这两类，等跑通之后再慢慢加 triple barrier 家族。

---

## 二、LabelRegistry Python 实现：`labels/registry.py`

### 1. 定义 LabelSpec & Registry

```python
# labels/registry.py

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Union

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field, validator


class LabelSpec(BaseModel):
    """
    单个标签的元数据 + 默认配置。

    - type = "time_return":  H bar 之后的收益率（log 或 simple）
    - type = "triple_barrier": Triple Barrier 标签（+1/-1/0）
    """

    id: str = Field(..., description="Label ID, 如 ret_10m_fwd")
    type: Literal["time_return", "triple_barrier"] = Field(
        ...,
        description="标签类型",
    )
    base_col: str = Field(
        "close",
        description="用于计算标签的基础价格列，如 close",
    )
    default_horizon_bars: int = Field(
        ...,
        gt=0,
        description="默认前瞻窗口（bar 数），例如 10 bar / 30 bar",
    )
    output_name: str = Field(
        ...,
        description="在 DataFrame 中生成的标签列名",
    )
    params: Dict[str, Any] = Field(
        default_factory=dict,
        description="标签类型的额外参数，例如 mode/up_pct/down_pct 等",
    )
    description: Optional[str] = Field(
        None,
        description="人类可读说明，方便日后维护",
    )

    # ---- 统一的打标签入口 ----
    def add_label_column(
        self,
        df: pd.DataFrame,
        *,
        horizon_bars: Optional[int] = None,
        cfg: Optional[Dict[str, Any]] = None,
    ) -> pd.DataFrame:
        """
        返回一个新的 DataFrame，增加 output_name 列作为标签。
        不修改原 df（.copy）。
        """
        if self.base_col not in df.columns:
            raise ValueError(f"LabelSpec[{self.id}] base_col={self.base_col!r} 不在 DataFrame 列中")

        h = horizon_bars or self.default_horizon_bars
        if h <= 0:
            raise ValueError(f"horizon_bars 必须 > 0, got {h}")

        merged_params = dict(self.params)
        if cfg:
            merged_params.update(cfg)

        if self.type == "time_return":
            return _add_time_return_label(
                df,
                base_col=self.base_col,
                horizon=h,
                output_name=self.output_name,
                params=merged_params,
            )
        elif self.type == "triple_barrier":
            return _add_triple_barrier_label(
                df,
                base_col=self.base_col,
                horizon=h,
                output_name=self.output_name,
                params=merged_params,
            )
        else:
            raise ValueError(f"Unsupported label type: {self.type}")


class LabelRegistry(BaseModel):
    """
    LabelRegistry 的内存表示。

    以后可以扩展：
    - versioning
    - 实验环境 / 生产环境分支
    """

    _version: int = Field(1, alias="_version")
    labels: List[LabelSpec]

    class Config:
        allow_population_by_field_name = True

    # ==== 查询接口 ====

    def list_label_ids(self) -> List[str]:
        return [lbl.id for lbl in self.labels]

    def get_spec(self, label_id: str) -> LabelSpec:
        for lbl in self.labels:
            if lbl.id == label_id:
                return lbl
        raise KeyError(f"Label id {label_id!r} not found in LabelRegistry")

    # ==== 工具方法 ====

    @classmethod
    def from_json(cls, path: Union[str, Path]) -> "LabelRegistry":
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.parse_obj(data)
```

### 2. 模块级 singleton + 便捷函数

```python
# 继续 labels/registry.py

_DEFAULT_REGISTRY_PATH = Path(__file__).with_name("registry.json")
_registry_singleton: Optional[LabelRegistry] = None


def get_registry(path: Optional[Union[str, Path]] = None) -> LabelRegistry:
    """
    懒加载的单例 LabelRegistry。
    - 第一次调用时从 JSON 读
    - 后续直接复用（除非你显式传 path）
    """
    global _registry_singleton
    if path is None and _registry_singleton is not None:
        return _registry_singleton

    reg_path = Path(path) if path is not None else _DEFAULT_REGISTRY_PATH
    registry = LabelRegistry.from_json(reg_path)

    if path is None:
        _registry_singleton = registry
    return registry


def add_label_column(
    df: pd.DataFrame,
    *,
    label_id: str,
    horizon_bars: Optional[int] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """
    对外统一接口：根据 label_id 在 df 上打标签，返回新的 df。

    - Label 定义全部来自 registry.json
    - 不允许 LLM 自己写 target SQL，从根上避免 label 泄露
    """
    registry = get_registry()
    spec = registry.get_spec(label_id)
    return spec.add_label_column(df, horizon_bars=horizon_bars, cfg=cfg)
```

---

## 三、具体标签算法实现（核心：无泄露）

### 1. Time Return 型标签：`time_return`

只看未来固定 H bar 的价格：

```python
# 继续 labels/registry.py

def _add_time_return_label(
    df: pd.DataFrame,
    *,
    base_col: str,
    horizon: int,
    output_name: str,
    params: Dict[str, Any],
) -> pd.DataFrame:
    """
    计算固定 horizon 的前瞻收益率。

    - mode = "log": ln(p_{t+H} / p_t)
    - mode = "simple": (p_{t+H} / p_t) - 1
    - 尾部最后 horizon 行因为未来不够，统一设为 NaN，避免 horizon 漂移
    """
    mode = params.get("mode", "log")
    if mode not in ("log", "simple"):
        raise ValueError(f"Unsupported time_return mode: {mode!r}")

    out_df = df.copy()
    price = out_df[base_col].astype(float).to_numpy()
    n = len(price)

    # 用 shift(-horizon) 实现 p_{t+H}
    # 但是要小心尾部行，这些行应为 NaN
    fwd = np.empty_like(price, dtype=float)
    fwd[:] = np.nan

    if n > horizon:
        fwd[:-horizon] = price[horizon:]

    with np.errstate(divide="ignore", invalid="ignore"):
        if mode == "log":
            ret = np.log(fwd / price)
        else:  # simple
            ret = (fwd / price) - 1.0

    # 对于 horizon 不完整的尾部，保持 NaN（fwd 本身就是 NaN）
    out_df[output_name] = ret
    return out_df
```

> ✅ 保证只看 `t+1..t+H`，不碰更远的未来；
> ✅ 尾部不足 H bar 的行全部 NaN，防止“label horizon 漂移”。

---

### 2. Triple Barrier 型标签：`triple_barrier`

简化版实现（足够正确，但大数据建议以后迁到 ClickHouse / numba 优化）：

```python
# 继续 labels/registry.py

def _add_triple_barrier_label(
    df: pd.DataFrame,
    *,
    base_col: str,
    horizon: int,
    output_name: str,
    params: Dict[str, Any],
) -> pd.DataFrame:
    """
    Triple Barrier 标签（+1/-1/0）简化实现：

    - 对每个 t：
        - p0 = price[t]
        - 上沿 = p0 * (1 + up_pct)
        - 下沿 = p0 * (1 - down_pct)
        - 观察未来 1..H bar 的价格序列：
            - 如果先触碰上沿 → label = +1
            - 如果先触碰下沿 → label = -1
            - 如果都没触碰：
                - 默认 label = neutral_as（一般为 0）
                - 或者可选用 horizon 末尾收益率的符号（扩展用）

    ⚠️ 复杂度 O(N * H)，大样本建议后续用向量化/numba/ClickHouse 重写。
    """
    up_pct = float(params.get("up_pct", 0.02))
    down_pct = float(params.get("down_pct", 0.01))
    neutral_as = params.get("neutral_as", 0)

    out_df = df.copy()
    price = out_df[base_col].astype(float).to_numpy()
    n = len(price)

    labels = np.full(n, np.nan, dtype=float)

    if horizon <= 0:
        raise ValueError(f"horizon must be > 0, got {horizon}")

    for i in range(n):
        # 不允许 horizon 不完整的尾部，保持 NaN
        if i + horizon >= n:
            break

        p0 = price[i]
        up_level = p0 * (1.0 + up_pct)
        down_level = p0 * (1.0 - down_pct)

        window = price[i + 1 : i + 1 + horizon]  # 长度 = horizon

        # 找到第一次触碰时间（相对索引）
        hit_up = np.where(window >= up_level)[0]
        hit_down = np.where(window <= down_level)[0]

        first_up = hit_up[0] if len(hit_up) > 0 else None
        first_down = hit_down[0] if len(hit_down) > 0 else None

        if first_up is None and first_down is None:
            labels[i] = neutral_as
        elif first_up is not None and first_down is None:
            labels[i] = 1.0
        elif first_up is None and first_down is not None:
            labels[i] = -1.0
        else:
            # 都有：谁先触达就按谁的方向记
            if first_up < first_down:
                labels[i] = 1.0
            elif first_down < first_up:
                labels[i] = -1.0
            else:
                # 极端情况：同一根 K 同时触及上下（几乎不可能），当作 neutral
                labels[i] = neutral_as

    out_df[output_name] = labels
    return out_df
```

> ✅ 同样只看未来 1..H bar；
> ✅ horizon 不完整尾巴直接 NaN；
> ✅ 无标签泄露。

---

## 四、回到 `PrincipalInvestigator.propose()` 的接线

在上一轮我给你的新版 `propose()` 里，这几行现在已经有真实实现：

```python
from labels.registry import (
    get_registry as get_label_registry,
    add_label_column,
)
```

把“打标签”这一步改为调用我们刚写的 `add_label_column`：

```python
label_id = tplan["label_id"]
label_reg = get_label_registry()
label_spec = label_reg.get_spec(label_id)

label_horizon = tplan.get("label_horizon_bars") or label_spec.default_horizon_bars
df = add_label_column(
    df=df,
    label_id=label_id,
    horizon_bars=label_horizon,
    cfg=None,  # 如果以后想给某个实验微调 up_pct/down_pct，就在 tplan 里透传
)
label_col = label_spec.output_name
```

后面的：

* `run_dataset_sanity_checks(df, label_col=label_col)`
* `run_training_gate(df, label_col=label_col, ...)`

就可以真正生效了（Gate 会看这个 `label_col` 的 NaN 比例、正负样本比例等）。

---

## 五、你现在可以做的最小集成测试

你可以用下面这段伪代码，在任何 Notebook 里本地快速 sanity 测试：

```python
from labels.registry import add_label_column, get_registry
import pandas as pd

# 假设你已经从 ClickHouse 拉了一段 1m 数据：
# df: columns = ["ts", "symbol", "open", "high", "low", "close", "volume", ...]

# 1. 看看 registry 正常加载
reg = get_registry()
print(reg.list_label_ids())
# -> ["ret_10m_fwd", "ret_30m_fwd", "tb_10_20_10", ...]

# 2. 打一个最简单的 ret_10m_fwd label
df_labeled = add_label_column(df, label_id="ret_10m_fwd", horizon_bars=None)

print(df_labeled.columns)
# 应该多出 "label_ret_10m_fwd"

print(df_labeled[["ts", "close", "label_ret_10m_fwd"]].head(15))
print(df_labeled[["ts", "close", "label_ret_10m_fwd"]].tail(15))  # 尾部 horizon 行应该是 NaN

# 3. 再试 triple barrier（小段数据就行）
df_tb = add_label_column(df, label_id="tb_10_20_10")
print(df_tb["label_tb_10_20_10"].value_counts(dropna=False))
```

确认这些都 OK 后，再用同一个时间段去跑一次：

```python
pi = PrincipalInvestigator(ch_cfg=..., llm_cfg=...)
res = pi.propose(
    table="bars_1m",
    symbol="NVDA",
    start_ts="2025-02-01 09:30:00",
    end_ts="2025-02-15 16:00:00",
    goal="测试 ret_10m_fwd 动量预测",
    horizon="10m",
)
```

如果执行成功：

* `res.dataset_df` 里会有 `label_ret_10m_fwd`；
* HPO grid 是干净的结构；
* Gate 没把它挡住。

---

## 下一步要做什么？

按照我们原来的路线图，下一步我会：

1. **整理 AI 大脑工具说明书**：

   * 把 FeatureRegistry / LabelRegistry / Sanity / TrainingGate / HPO grid 全部用“工具 API 文档”的形式写出来；
   * 提供一份专门给“大脑”的 Prompt 模板，让它**知道**自己有哪些按钮可以按、哪些是红线。

2. **加一个“Sandbox 衍生特征”通道**：

   * 允许 PI 在特定受控模式下提出新 SQL 衍生特征；
   * 通过简单语法/依赖检查后，临时注册为 `temp_xxx`，跑完实验再由你决定是否晋升到正式 FeatureRegistry。

你现在不用做选择，我会直接按这个顺序继续往下落地。
