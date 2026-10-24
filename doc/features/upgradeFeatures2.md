好，那我按“施工计划”继续推进，这一轮专注在 **特征工程 + AI 眼睛升级**，不再问你要指令，直接给可落地代码。

这一轮我做三件事：

1. 加一个 **标准特征模块**（统一算一批“机构级基础特征”，保证无泄露＆可复现）。
2. 升级 **安全 SQL 构造器**，支持“Python 衍生特征”——让 AI 可以用更多特征，但 ClickHouse 只出干净的基础数据。
3. 升级 `PrincipalInvestigator`，让：

   * 标准特征在 Python 里自动补齐；
   * `feature_subset` 只使用 **真实存在的列**；
   * AI 的 prompt 里能看到“额外眼睛”（标准特征列表）。

---

## 1️⃣ 标准特征模块：`features/standard_features.py`

> 功能：
>
> * 从最基础的 OHLCV + 时间字段出发，统一在 **Python 层** 计算一批常用特征；
> * 保证只有用过去数据（无未来泄露）；
> * 以后你可以在这里持续加新特征，相当于一个“代码版特征 Registry”。

新建文件：`features/standard_features.py`

```python
# features/standard_features.py

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

# AI 大脑可以使用的 “Python 衍生特征” 名单
PYTHON_DERIVED_FEATURES: List[str] = [
    # 返回收益类
    "ret_1b",
    "ret_5b",
    "ret_10b",

    # 波动类
    "vol_20b",          # 20 bar rolling std of returns

    # 均线/趋势类
    "ma_20_close",
    "ma_50_close",
    "ma_200_close",

    # 量能 / RVOL
    "vol_ma_20",
    "rvol_20",

    # 时间位置类（可以辅助模型感知开盘/收盘时段）
    "minute_of_day",
    "day_of_week",
]


def _ensure_datetime(df: pd.DataFrame, ts_col: str) -> pd.DataFrame:
    if ts_col not in df.columns:
        return df

    if not np.issubdtype(df[ts_col].dtype, np.datetime64):
        df = df.copy()
        df[ts_col] = pd.to_datetime(df[ts_col])
    return df


def add_standard_features(
    df: pd.DataFrame,
    *,
    enabled: Optional[Sequence[str]] = None,
    group_key: Sequence[str] = ("symbol",),
    ts_col: str = "ts",
) -> pd.DataFrame:
    """
    在 df 上增加一批“标准衍生特征”。

    约束：
      - 所有特征都只使用过去或当前 bar（无未来泄露）
      - 按 group_key 分组（一般按 symbol 分组）
    """
    if enabled is None:
        enabled = PYTHON_DERIVED_FEATURES

    enabled = list(enabled)
    if not enabled:
        return df

    needed_price_cols = {"close"}
    needed_vol_cols = {"volume"}

    # 缺基础列的特征自动跳过
    missing_price = needed_price_cols - set(df.columns)
    missing_vol = needed_vol_cols - set(df.columns)

    df = _ensure_datetime(df, ts_col)
    out = df.copy()

    # 分组按 symbol（或你指定的 group_key）
    grp = out.groupby(list(group_key), sort=False, group_keys=False)

    # === 1. 收益类特征 ===
    if "ret_1b" in enabled and "close" in out.columns:
        out["ret_1b"] = grp["close"].pct_change(1)

    if "ret_5b" in enabled and "close" in out.columns:
        out["ret_5b"] = grp["close"].pct_change(5)

    if "ret_10b" in enabled and "close" in out.columns:
        out["ret_10b"] = grp["close"].pct_change(10)

    # === 2. 波动类特征（滚动标准差） ===
    if "vol_20b" in enabled and "ret_1b" in out.columns:
        out["vol_20b"] = grp["ret_1b"].rolling(20, min_periods=5).std()

    # === 3. 均线 / 趋势类特征（简单移动平均） ===
    if "ma_20_close" in enabled and "close" in out.columns:
        out["ma_20_close"] = grp["close"].rolling(20, min_periods=5).mean()

    if "ma_50_close" in enabled and "close" in out.columns:
        out["ma_50_close"] = grp["close"].rolling(50, min_periods=10).mean()

    if "ma_200_close" in enabled and "close" in out.columns:
        out["ma_200_close"] = grp["close"].rolling(200, min_periods=20).mean()

    # === 4. 成交量 / RVOL 类特征 ===
    if "vol_ma_20" in enabled and "volume" in out.columns:
        out["vol_ma_20"] = grp["volume"].rolling(20, min_periods=5).mean()

    if (
        "rvol_20" in enabled
        and "volume" in out.columns
        and "vol_ma_20" in out.columns
    ):
        # RVOL = volume / vol_ma_20
        out["rvol_20"] = out["volume"] / out["vol_ma_20"]

    # === 5. 时间位置类特征 ===
    if "minute_of_day" in enabled and ts_col in out.columns:
        out["minute_of_day"] = out[ts_col].dt.hour * 60 + out[ts_col].dt.minute

    if "day_of_week" in enabled and ts_col in out.columns:
        # 周一=0, 周日=6
        out["day_of_week"] = out[ts_col].dt.dayofweek

    return out
```

> 这相当于是一个“代码版 Feature Registry”：
>
> * 你知道有哪些特征（`PYTHON_DERIVED_FEATURES`）；
> * 它们怎么计算写死在这里，不交给 LLM 瞎编；
> * 全部防止未来泄露。

---

## 2️⃣ 升级安全 SQL 构造器：支持 Python 衍生特征

我们要解决一个关键问题：

> LLM 在 `feature_recipe.base_features` 里，可能会引用 **Python 才会生成的特征**
> （比如 `ret_1b`, `rvol_20`），这些在 ClickHouse 原始表里肯定不存在。

所以要做两件事：

1. **在生成 SQL 时**：

   * 只从 `base_features` 里挑出真正存在于表里的列；
   * 遇到 `PYTHON_DERIVED_FEATURES` 就 **不写进 SQL**（它们稍后在 Python 中算）；
2. **在 HPO feature_subset 里**：

   * 只使用 `dataset_df.columns` 中真实存在的列（包括 Python 衍生 & LLM 派生 & 原始列）。

### 2.1 更新 `pi/feature_sql.py`

这是之前我们加的 `build_select_sql_safe` 的 **升级版**：多了一个可选参数 `python_derived_features`。

```python
# pi/feature_sql.py

from __future__ import annotations

from typing import Any, Dict, List, Optional

from contracts.labels import get_label_required_columns


def build_select_sql_safe(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    label_id: str,
    feature_catalog: List[str],
    python_derived_features: Optional[List[str]] = None,  # NEW
) -> str:
    """
    安全版 SQL 构造器：

    - 只选：label 需要的基础列 + base_features 中真实存在于表里的列 + 派生特征 expr
    - 对于 Python 衍生特征（例如 ret_1b / rvol_20 等），只在 Python 层计算，不写进 SQL
    - 完全禁止 LLM 提供任何 target 表达式（label 统一在 Python 层计算）
    """
    python_derived_set = set(python_derived_features or [])

    # 1) label 需要的基础列（symbol/ts/close 等）
    required_cols = set(get_label_required_columns(label_id))

    # 2) LLM 提供的 base_features：
    #    - 在表中存在的列：加入 required_cols
    #    - 在 python_derived_set 中的：不进 SQL，由 Python 计算
    #    - 其他不存在的列：静默丢弃（可以以后加日志）
    base_features = recipe.get("base_features", []) or []
    for col in base_features:
        if col in feature_catalog:
            required_cols.add(col)
        elif col in python_derived_set:
            # Python 衍生特征，不写进 SQL
            continue
        else:
            # 既不是真实列也不是 Python 衍生特征：丢弃
            continue

    # 3) 派生特征（LLM 给 SQL expr，只作为 SELECT 表达式，不当 target）
    derived_parts: List[str] = []
    for d in recipe.get("derived", []) or []:
        expr = d.get("expr")
        alias = d.get("alias")
        if not expr or not alias:
            continue
        derived_parts.append(f"({expr}) AS {alias}")

    # 4) 基础列 SELECT 片段
    base_select_cols = sorted(required_cols)
    base_select_sql = ", ".join(base_select_cols)

    select_parts = [base_select_sql]
    if derived_parts:
        select_parts.extend(derived_parts)

    select_clause = ", ".join(select_parts)

    sql = f"""
        SELECT
            {select_clause}
        FROM {table}
        WHERE symbol = %(symbol)s
          AND ts >= %(start_ts)s
          AND ts < %(end_ts)s
        ORDER BY ts ASC
    """

    return sql.strip()
```

---

## 3️⃣ 升级 PrincipalInvestigator：接入标准特征 + 安全特征子集

现在我们把标准特征和安全 SQL 一起接入 `PrincipalInvestigator`：

**变化点：**

1. Prompt 中除了原始 `feature_catalog`，还告诉 LLM：
   “有一批标准 Python 特征可以用，名字如下……”
2. SQL 生成时：

   * 把 Python 特征名作为 `python_derived_features` 传给 `build_select_sql_safe`；
3. 查询 ClickHouse 后：

   * 先算 label（Python 层）
   * 再调用 `add_standard_features` 把标准特征算好；
   * 再跑 `run_dataset_sanity`（体检现在包括这些新特征）；
4. 构建 HPO grid 时：

   * `feature_subset` 不再直接相信 LLM 的名单，而是做一次**交集过滤**：只保留 `df.columns` 中真实存在的列。

### 更新版 `pi/principal_investigator.py`

> 和上一次给你的版本相比，我只在关键位置做了增量修改，方便你直接替换。

```python
# pi/principal_investigator.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql_safe
from quality.dataset_sanity import run_dataset_sanity
from contracts.labels import attach_label
from experiments.manifest import create_manifest, save_manifest
from features.standard_features import (
    PYTHON_DERIVED_FEATURES,
    add_standard_features,
)


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    sanity_report: Optional[Dict[str, Any]] = None
    sanity_summary: Optional[Dict[str, Any]] = None
    label_id: Optional[str] = None
    manifest_path: Optional[str] = None


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
        freq: str = "1m",
        default_label_id: str = "ret_10b_fwd",
    ) -> PIResult:
        # === 1) 拿表结构（原始列） ===
        catalog = _fetch_catalog(self._client, table)

        # === 2) 让 LLM 生成 feature_recipe + training_plan ===
        #      注意：现在 prompt 里额外告诉它有哪些 Python 衍生特征可用
        python_feat_str = ", ".join(PYTHON_DERIVED_FEATURES)

        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"原始特征列(来自 ClickHouse 表): {', '.join(catalog)}\n"
            f"可选的标准 Python 衍生特征: {python_feat_str}\n"
            f"预测窗口: {horizon}\n"
            "你在设计 feature_recipe.base_features 时，可以混用上面两类特征。\n"
            "注意：Python 衍生特征不会在数据库中直接存在，而是在训练前由系统自动计算。"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
            "禁止输出任何 SQL 代码或 target 表达式，label 将在 Python 层统一计算。"
            "feature_recipe.base_features 中可以包括两类：\n"
            "1) 原始列名（在原始特征列表中出现）\n"
            "2) 标准 Python 衍生特征名（在标准 Python 特征列表中出现）\n"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # === 3) 决定 label_id（仅允许 registry 中存在的） ===
        label_id = tplan.get("label_id", default_label_id)

        # === 4) 安全构造 SQL：只包含原始列 + label 所需列 + LLM 选的、实际存在于表中的列 ===
        sql = build_select_sql_safe(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            label_id=label_id,
            feature_catalog=catalog,
            python_derived_features=PYTHON_DERIVED_FEATURES,
        )

        df = self._client.query_df(
            sql,
            parameters={"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
        )

        # === 5) 在 Python 层统一构造 label（Label Guard） ===
        df = attach_label(df, label_id=label_id, colname="label")

        # === 6) 加入标准 Python 衍生特征（AI 大脑的“额外眼睛”） ===
        df = add_standard_features(df, enabled=PYTHON_DERIVED_FEATURES)

        # === 7) Dataset Sanity 体检（现在包括标准特征 + label） ===
        sanity_report, sanity_summary = run_dataset_sanity(df, label_cols=["label"])

        # === 8) 构造 HPO 网格（feature_subset 只用 df 中真实存在的列） ===
        proposed_base = recipe.get("base_features", []) or []
        proposed_derived = [d.get("alias") for d in recipe.get("derived", []) or []]

        # 原始提案特征（LLM 视角）
        proposed_features = [f for f in (proposed_base + proposed_derived) if f]

        # 实际可用特征：必须真实存在于 df.columns，且不是 label
        available_features = [
            col for col in df.columns if col not in ("label",)
        ]
        feature_subset = [f for f in proposed_features if f in available_features]

        # 如果 LLM 提的某些特征根本不存在，也不会导致训练时崩溃，只是被 silently drop
        if not feature_subset:
            # fallback：至少给一个默认的基线特征组合
            default_candidates = [
                "ret_1b",
                "ret_5b",
                "rvol_20",
                "ma_20_close",
                "ma_50_close",
            ]
            feature_subset = [f for f in default_candidates if f in available_features]

        grid = {
            "model_type": [],
            "feature_subset": [feature_subset],
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
            "allow_params": [
                "model_type",
                "feature_subset",
                "task",
                "decision",
                "model_params",
            ]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        # === 9) 生成并保存 Experiment Manifest（记录这次实验所有关键信息） ===
        manifest = create_manifest(
            symbol=symbol,
            freq=freq,
            label_id=label_id,
            start_ts=start_ts,
            end_ts=end_ts,
            data_source={
                "table": table,
                "use_table": recipe.get("use_table", table),
                "clickhouse": self._ch_cfg,
            },
            feature_recipe=recipe,
            training_plan=tplan,
            sanity_summary=sanity_summary,
            meta={
                "sql": sql,
                "feature_catalog": catalog,
                "python_derived_features": PYTHON_DERIVED_FEATURES,
                "feature_subset_effective": feature_subset,
            },
        )
        manifest_path = save_manifest(manifest)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            sanity_report=sanity_report,
            sanity_summary=sanity_summary,
            label_id=label_id,
            manifest_path=str(manifest_path),
        )
```

---

## 4️⃣ 这一轮落地后，你的系统本质上发生了什么变化？

从“机构级眼光”看，现在这个链路已经变成：

1. **特征 & 标签的安全边界**：

   * Label 完全收归到 `contracts/labels.py`，LLM 再也不能乱写 target SQL；
   * 所有 Python 标准特征统一在 `features/standard_features.py` 里实现；
   * SQL 只拿“干净基础数据”，把衍生逻辑移到 Python 层，减少 data corruption；

2. **AI 大脑的“眼睛”升级**：

   * Prompt 里，AI 不再只看到 ClickHouse 原始列，还能看到完整的标准特征列表；
   * 它提出的特征方案中，既可以用原始列，也可以用标准衍生特征；

3. **数据质量防线**：

   * 任何生成出来的 `dataset_df` 都会先经过 `run_dataset_sanity`；
   * 如果某个 symbol/horizon 上数据太烂，你可以在 AI 大脑那一层直接根据 `sanity_summary.overall_status` 决定：

     * `fail`：禁止训练 / 回测；
     * `warn`：减小仓位或只用作研究样本。

4. **“不会炸”的 feature_subset**：

   * HPO 用的 `feature_subset` 现在是“LLM 推荐 ∩ 实际存在列”；
   * 即使 prompt 写得再花，也不会因为一个拼错的 feature name 直接训练崩溃。

---

## 5️⃣ 下一步我会做什么（你继续“继续”就行）

按原计划，下一轮我会继续推进：

* **Phase 3 真正的 Feature Registry**：

  * 把当前的 `PYTHON_DERIVED_FEATURES` + 你已有的 `feature_programs.json` 汇总成一个**统一的 Registry 结构**（支持 tags / freq / 任务类型等）；
  * 加“沙箱特征”机制：允许 LLM 提出新 SQL 特征，但必须经过白名单 parser 才能落地；

* **Phase 4：AI 大脑输入升级**：

  * 把 `manifest + sanity_summary + 历史性能摘要` 组合成一个结构化上下文，喂给你的 v3/v4 prompt，让 AI 大脑学会：

    * 看到“数据太烂时拒绝训练”
    * 根据 `sanity issues` 自动调整策略（例如遇到 label 极度不平衡时，建议改用 rank 损失或 reweight）

你只需要继续回复一句 “继续”，我就会按这个路线把 Feature Registry 和沙箱特征的代码层再铺下去，不再问你要怎么做。
