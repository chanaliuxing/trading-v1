好，那这一轮我们把两块关键能力落地：

1. **标签体系（LabelRegistry + 引擎）** → 彻底解决 “标签随便写 / 泄露未来” 的问题
2. **把 Dataset Sanity 结果接到 HPO / 特征子集上** → 让坏数据、废特征自动被剔除

我会直接给你可以落地的代码版本，你按文件维度加进去即可。

---

## 一、Label Registry：所有标签统一登记管理

### 1.1 `labels/label_registry.json`

先建一个最小但实用的版本（可以后面扩展）：

```jsonc
{
  "_comment": "Global Label Registry – 所有可用训练标签的统一定义",
  "_version": 1,
  "labels": [
    {
      "id": "ret_10b_fwd_reg",
      "type": "regression",
      "base_price_col": "close",
      "horizon_bars": 10,
      "method": "forward_return",
      "params": {
        "return_type": "log",
        "clip_min": -0.5,
        "clip_max": 0.5
      },
      "status": "stable",
      "description": "10-bar 之后的对数收益率，用于回归任务。"
    },
    {
      "id": "ret_10b_fwd_dir3",
      "type": "classification",
      "base_price_col": "close",
      "horizon_bars": 10,
      "method": "forward_return_direction",
      "params": {
        "return_type": "log",
        "up_threshold": 0.001,
        "down_threshold": -0.001
      },
      "status": "stable",
      "description": "10-bar 对数收益率方向标签：+1 / 0 / -1。"
    },
    {
      "id": "tb_10b_3_3_long_short",
      "type": "classification",
      "base_price_col": "close",
      "horizon_bars": 10,
      "method": "triple_barrier",
      "params": {
        "take_profit_pct": 0.03,
        "stop_loss_pct": 0.03,
        "fallback_to_horizon_ret": true,
        "return_type": "log"
      },
      "status": "experimental",
      "description": "简化版 Triple-Barrier：10 bar 窗口，+3%/-3% 栅栏，优先看是否触发 TP/SL，否则看终点收益方向。"
    }
  ]
}
```

你后面要加更多标签，只要按这个 schema 填就行，不再在 LLM / SQL 里乱写 target。

---

## 二、Label Engine：所有标签在 Python 里安全生成

新建 `labels/engine.py`：

```python
# labels/engine.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json
import numpy as np
import pandas as pd


@dataclass
class LabelDef:
    id: str
    type: str               # "regression" / "classification"
    base_price_col: str
    horizon_bars: int
    method: str             # "forward_return" / "forward_return_direction" / "triple_barrier"
    params: Dict[str, Any]
    status: str
    description: str

    @property
    def is_active(self) -> bool:
        return self.status not in {"disabled", "deprecated"}


class LabelRegistry:
    def __init__(self, labels: List[LabelDef]) -> None:
        self._by_id: Dict[str, LabelDef] = {l.id: l for l in labels}

    @classmethod
    def from_json_file(cls, path: str | Path) -> "LabelRegistry":
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        labels: List[LabelDef] = []
        for item in raw.get("labels", []):
            labels.append(
                LabelDef(
                    id=item["id"],
                    type=item["type"],
                    base_price_col=item["base_price_col"],
                    horizon_bars=int(item["horizon_bars"]),
                    method=item["method"],
                    params=item.get("params", {}) or {},
                    status=item.get("status", "stable"),
                    description=item.get("description", ""),
                )
            )
        return cls(labels)

    def get(self, label_id: str) -> LabelDef:
        if label_id not in self._by_id:
            raise KeyError(f"Unknown label id: {label_id}")
        return self._by_id[label_id]

    def list_ids(self, *, active_only: bool = True) -> List[str]:
        return sorted(
            l.id for l in self._by_id.values()
            if (not active_only or l.is_active)
        )


# 单例式加载
_default_registry: Optional[LabelRegistry] = None


def get_label_registry(path: str | Path | None = None) -> LabelRegistry:
    global _default_registry
    if _default_registry is not None and path is None:
        return _default_registry
    if path is None:
        path = Path(__file__).with_name("label_registry.json")
    _default_registry = LabelRegistry.from_json_file(path)
    return _default_registry


# ---------- 具体生成逻辑 ----------

def _forward_return(
    prices: pd.Series,
    horizon: int,
    *,
    return_type: str = "log",
    clip_min: Optional[float] = None,
    clip_max: Optional[float] = None,
) -> pd.Series:
    fwd = prices.shift(-horizon)
    if return_type == "log":
        ret = np.log(fwd) - np.log(prices)
    else:
        ret = (fwd / prices) - 1.0
    if clip_min is not None or clip_max is not None:
        ret = ret.clip(lower=clip_min, upper=clip_max)
    # 末尾不完整的 horizon，返回 NaN
    ret.iloc[-horizon:] = np.nan
    return ret


def _forward_return_direction(
    prices: pd.Series,
    horizon: int,
    *,
    return_type: str = "log",
    up_threshold: float = 0.0,
    down_threshold: float = 0.0,
) -> pd.Series:
    ret = _forward_return(
        prices,
        horizon,
        return_type=return_type,
        clip_min=None,
        clip_max=None,
    )
    up = up_threshold
    down = down_threshold
    # +1 / 0 / -1
    labels = pd.Series(index=prices.index, dtype="float64")
    labels[ret > up] = 1.0
    labels[ret < down] = -1.0
    labels[(ret <= up) & (ret >= down)] = 0.0
    labels.iloc[-horizon:] = np.nan
    return labels


def _triple_barrier_simple(
    close: pd.Series,
    high: pd.Series,
    low: pd.Series,
    horizon: int,
    *,
    take_profit_pct: float,
    stop_loss_pct: float,
    return_type: str = "log",
    fallback_to_horizon_ret: bool = True,
) -> pd.Series:
    """
    简化版 Triple-Barrier：
    - 在 [i+1, i+horizon] 窗口内：
      如果低点 <= SL 栅栏 → -1
      elif 高点 >= TP 栅栏 → +1
      否则：如果 fallback=True，用终点收益方向；否则记 0。
    - 不追求严格“先触达时间”，但已经是 path-dependent，不再是纯 horizon ret。
    """
    n = len(close)
    idx = close.index
    labels = pd.Series(index=idx, dtype="float64")

    upper_mult = 1.0 + take_profit_pct
    lower_mult = 1.0 - stop_loss_pct

    for i in range(n):
        p0 = close.iloc[i]
        if pd.isna(p0):
            labels.iloc[i] = np.nan
            continue

        end = min(i + horizon, n - 1)
        if i + 1 > end:
            labels.iloc[i] = np.nan
            continue

        window_high = high.iloc[i + 1 : end + 1]
        window_low = low.iloc[i + 1 : end + 1]

        upper = p0 * upper_mult
        lower = p0 * lower_mult

        hit_sl = (window_low <= lower).any()
        hit_tp = (window_high >= upper).any()

        if hit_sl and not hit_tp:
            labels.iloc[i] = -1.0
        elif hit_tp and not hit_sl:
            labels.iloc[i] = 1.0
        elif hit_tp and hit_sl:
            # 两个都触发：简单处理为看终点收益方向
            if fallback_to_horizon_ret:
                fwd = close.iloc[end]
                if return_type == "log":
                    r = np.log(fwd) - np.log(p0)
                else:
                    r = (fwd / p0) - 1.0
                labels.iloc[i] = np.sign(r) if not np.isnan(r) else 0.0
            else:
                labels.iloc[i] = 0.0
        else:
            # 都没触发 → fallback or 0
            if fallback_to_horizon_ret:
                fwd = close.iloc[end]
                if return_type == "log":
                    r = np.log(fwd) - np.log(p0)
                else:
                    r = (fwd / p0) - 1.0
                labels.iloc[i] = np.sign(r) if not np.isnan(r) else 0.0
            else:
                labels.iloc[i] = 0.0

    # 尾部不足 horizon 的样本标 NaN
    labels.iloc[-horizon:] = np.nan
    return labels


@dataclass
class LabelAttachResult:
    df: pd.DataFrame
    target_col: str
    label_def: LabelDef
    meta: Dict[str, Any]


def attach_label(
    df: pd.DataFrame,
    label_def: LabelDef,
    *,
    target_col: str = "target",
) -> LabelAttachResult:
    """
    在现有 DataFrame 上附加一列 target。

    要求：
    - df 至少包含 base_price_col
    - triple_barrier 需要 high/low
    """
    if label_def.base_price_col not in df.columns:
        raise ValueError(f"DataFrame missing base price column '{label_def.base_price_col}'")

    prices = df[label_def.base_price_col]
    h = label_def.horizon_bars
    m = label_def.method
    p = label_def.params or {}

    if m == "forward_return":
        y = _forward_return(
            prices,
            h,
            return_type=p.get("return_type", "log"),
            clip_min=p.get("clip_min"),
            clip_max=p.get("clip_max"),
        )
    elif m == "forward_return_direction":
        y = _forward_return_direction(
            prices,
            h,
            return_type=p.get("return_type", "log"),
            up_threshold=p.get("up_threshold", 0.0),
            down_threshold=p.get("down_threshold", 0.0),
        )
    elif m == "triple_barrier":
        if "high" not in df or "low" not in df:
            raise ValueError("Triple-barrier label requires 'high' and 'low' columns.")
        y = _triple_barrier_simple(
            close=prices,
            high=df["high"],
            low=df["low"],
            horizon=h,
            take_profit_pct=float(p.get("take_profit_pct", 0.03)),
            stop_loss_pct=float(p.get("stop_loss_pct", 0.03)),
            return_type=p.get("return_type", "log"),
            fallback_to_horizon_ret=bool(p.get("fallback_to_horizon_ret", True)),
        )
    else:
        raise ValueError(f"Unsupported label method: {m}")

    out_df = df.copy()
    out_df[target_col] = y

    meta = {
        "label_id": label_def.id,
        "type": label_def.type,
        "horizon_bars": label_def.horizon_bars,
        "method": label_def.method,
        "params": label_def.params,
        "status": label_def.status,
        "description": label_def.description,
    }

    return LabelAttachResult(
        df=out_df,
        target_col=target_col,
        label_def=label_def,
        meta=meta,
    )
```

> 现在：**所有标签都统一在 `labels/label_registry.json` + `labels/engine.py` 里定义和生成**，
> LLM 不能直接写 target SQL，也不会再出现“target 列和 DataFrame 不匹配”的 data corruption。

---

## 三、改造 PrincipalInvestigator：强制走 LabelRegistry + DatasetSanity

下面是在你上一个版本 PI 的基础上，**再加上标签 & 数据体检** 的升级版（可以理解为“新版覆盖旧版”）。

### 3.1 假设的 DatasetSanity 接口

我先假设你前面已按我的方案有一个 `dataset_sanity` 模块，接口类似：

```python
# dataset_sanity/core.py 里类似这样的东西

from dataclasses import dataclass
from typing import Dict, List, Set


@dataclass
class DatasetSanityResult:
    ok: bool
    summary: str
    zero_variance_cols: List[str]
    high_missing_cols: List[str]
    constant_cols: List[str]
    highly_correlated_pairs: List[tuple]  # [(col1, col2, corr), ...]
    diagnostics: Dict[str, any]


def run_dataset_sanity_checks(
    df,
    *,
    ignore_cols=None,
    cfg=None,
) -> DatasetSanityResult:
    ...
```

接下来我们把它用起来（不要再新写一套）。

### 3.2 升级版 `PrincipalInvestigator`

```python
# pi/core.py  (升级版)

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql
from features.registry import get_registry as get_feature_registry
from features.sandbox_validator import filter_valid_sandbox_features
from labels.engine import get_label_registry, attach_label
from dataset_sanity.core import run_dataset_sanity_checks, DatasetSanityResult


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame          # 经过 label + 体检 + 特征过滤后的最终 df
    hpo_grid: Dict[str, Any]
    sandbox_rejected: List[Dict[str, Any]]
    sanity_result: DatasetSanityResult


def _fetch_catalog(client, table: str) -> List[str]:
    q = "SELECT name FROM system.columns WHERE table = {table:String}"
    df = client.query_df(q, parameters={"table": table})
    return sorted(df["name"].tolist())


class PrincipalInvestigator:
    """LLM-driven feature + training plan generator (with registry + label + sanity)."""

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
        self._feature_registry = get_feature_registry()
        self._label_registry = get_label_registry()

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
        default_label_id: str = "ret_10b_fwd_reg",
    ) -> PIResult:
        # ---------- 1) 拉底表 catalog ----------
        catalog = _fetch_catalog(self._client, table)

        # ---------- 2) 构造 prompt ----------
        available_feature_ids = self._feature_registry.list_ids(table=table, active_only=True)
        available_label_ids = self._label_registry.list_ids(active_only=True)

        user_prompt = (
            f"研究目标: {goal}\n"
            f"表: {table}\n"
            f"标的: {symbol}\n"
            f"时间区间: {start_ts} ~ {end_ts}\n"
            f"可用特征ID列表: {', '.join(available_feature_ids)}\n"
            f"可用标签ID列表: {', '.join(available_label_ids)}\n"
            f"预测窗口 / horizon: {horizon}\n\n"
            "请基于以上信息，返回 JSON：\n"
            "{\n"
            '  "feature_recipe": {\n'
            '    "use_table": "bars_1m",\n'
            '    "base_feature_ids": ["close", "volume", ...],\n'
            '    "extra_feature_ids": ["rvol_20", ...],\n'
            '    "sandbox_features": [\n'
            '      {"alias": "my_new_feature", "expr": "some SQL expr", "description": "why"}\n'
            "    ]\n"
            "  },\n"
            '  "training_plan": {\n'
            '    "task": "reg" 或 "clf",\n'
            '    "symbol": "NVDA",\n'
            '    "label_id": "ret_10b_fwd_reg",\n'
            '    "model_candidates": [\n'
            '      {"model_type": "xgb", "model_params": {...}},\n'
            '      {"model_type": "lasso", "model_params": {...}}\n'
            "    ],\n"
            '    "decision": {"mode": "quantile", "q_long": 0.7, "q_short": 0.3}\n'
            "  }\n"
            "}\n"
            "注意：base_feature_ids/extra_feature_ids 必须从给定的 feature id 列表中选择；"
            "label_id 必须从给定的 label id 列表中选择。"
        )
        system_prompt = (
            "你是量化平台的首席科学家，负责特征与训练方案设计。"
            "你只能使用给定的特征ID和标签ID。"
            "必须返回 JSON。"
        )

        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        if isinstance(plan, str):
            import json
            plan = json.loads(plan)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # ---------- 3) 解析特征 ----------
        base_ids: List[str] = recipe.get("base_feature_ids", []) or recipe.get("base_features", [])
        extra_ids: List[str] = recipe.get("extra_feature_ids", [])
        sandbox_specs: List[Dict[str, Any]] = recipe.get("sandbox_features", [])

        for fid in base_ids + extra_ids:
            if fid not in available_feature_ids:
                raise ValueError(f"LLM chose unknown feature id: {fid}")

        all_ids = base_ids + extra_ids

        select_items = self._feature_registry.build_select_items(
            table=recipe.get("use_table", table),
            feature_ids=all_ids,
        )

        allowed_cols = self._feature_registry.allowed_column_aliases_for_table(table)
        ok_sandbox, rejected_sandbox = filter_valid_sandbox_features(
            sandbox_specs,
            allowed_columns=allowed_cols,
        )
        for spec in ok_sandbox:
            alias = spec["alias"].strip()
            expr = spec["expr"].strip()
            select_items.append(f"{expr} AS {alias}")

        # alias 列集合
        feature_aliases: List[str] = []
        for fid in all_ids:
            f = self._feature_registry.get(fid)
            feature_aliases.append(f.alias)
        for spec in ok_sandbox:
            feature_aliases.append(spec["alias"].strip())

        # ---------- 4) 只拉特征 + OHLCV，不在 SQL 里算 target ----------
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            select_items=select_items,
            # target=None  # 不再用 SQL 计算 target
            target=None,
            feature_catalog=catalog,
        )
        df_raw = self._client.query_df(sql)

        # ---------- 5) 通过 LabelRegistry 附加标签 ----------
        label_id = tplan.get("label_id") or tplan.get("target") or default_label_id
        label_def = self._label_registry.get(label_id)
        label_attach = attach_label(df_raw, label_def, target_col="target")
        df_labeled = label_attach.df
        target_col = label_attach.target_col

        # 根据 label 类型自动设置 task（防止 LLM 乱写）
        task = tplan.get("task")
        if not task:
            task = "reg" if label_def.type == "regression" else "clf"
        tplan["task"] = task
        tplan["label_id"] = label_id
        tplan["target_col"] = target_col
        tplan["label_meta"] = label_attach.meta

        # ---------- 6) Dataset Sanity 体检 ----------
        ignore_cols = set((sanity_cfg or {}).get("ignore_cols", []))
        ignore_cols.update({"ts", "symbol", target_col})

        sanity = run_dataset_sanity_checks(
            df_labeled,
            ignore_cols=ignore_cols,
            cfg=sanity_cfg,
        )

        # 自动剔除明显 bad 的特征（0 方差 / 常量 / 高缺失）
        bad_cols = set(sanity.zero_variance_cols) | set(sanity.constant_cols) | set(sanity.high_missing_cols)
        cleaned_feature_aliases = [c for c in feature_aliases if c not in bad_cols]

        if not cleaned_feature_aliases:
            raise RuntimeError(
                "After sanity filtering, no usable features remain. "
                f"Original: {len(feature_aliases)}, removed: {len(bad_cols)}"
            )

        # 按清洗后的特征列重新裁剪 DataFrame
        keep_cols = ["ts", "symbol"] + cleaned_feature_aliases + [target_col]
        df_final = df_labeled[keep_cols].copy()

        # ---------- 7) 构造 HPO 网格 ----------
        grid = {
            "model_type": [],
            "feature_subset": [cleaned_feature_aliases],
            "task": [task],
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

        # ---------- 8) 回写 recipe / 体检信息 ----------
        recipe_out = {
            **recipe,
            "resolved_feature_aliases": feature_aliases,
            "cleaned_feature_aliases": cleaned_feature_aliases,
            "sandbox_features_ok": ok_sandbox,
            "sandbox_features_rejected": rejected_sandbox,
        }

        tplan["sanity_summary"] = sanity.summary
        tplan["sanity_bad_cols"] = list(bad_cols)

        return PIResult(
            feature_recipe=recipe_out,
            training_plan=tplan,
            sql=sql,
            dataset_df=df_final,
            hpo_grid=grid_sane,
            sandbox_rejected=rejected_sandbox,
            sanity_result=sanity,
        )
```

**关键变化点：**

1. **target 完全从 SQL 中移除** → 改由 `LabelRegistry + attach_label` 统一生成
2. **task 自动与 label type 对齐**（regression / classification），防止 LLM 乱写
3. **DatasetSanity 结果直接作用于 feature_subset**：

   * 0 方差 / 常量 / 高缺失的列直接踢出
   * HPO 的 `feature_subset` 用的是清洗后的 `cleaned_feature_aliases`
   * 最终 `dataset_df` 只包含：`ts, symbol, cleaned_features, target`
4. `training_plan` 里写入：`label_id`, `target_col`, `label_meta`, `sanity_summary`, `sanity_bad_cols` —— 方便后面喂给 AI 大脑做决策。

---

## 四、接下来你需要做的最小动作

1. **新增文件：**

   * `labels/label_registry.json`
   * `labels/engine.py`

2. **确认 / 对齐：**

   * `dataset_sanity.core.run_dataset_sanity_checks` 的返回结构里，
     保证有：`zero_variance_cols`, `constant_cols`, `high_missing_cols`, `summary` 这些字段即可。
     （字段名不一致的话，你把 PI 里的引用改一下就行）

3. **更新：**

   * 用上面的 “升级版 `PrincipalInvestigator`” 覆盖你现在的 PI 实现（或者合并核心改动）。

---

## 五、下一步（你再说一次「继续」我就做）

在这一轮，我们已经把：

* **标签 → 收紧到 LabelRegistry**
* **target → 在 Python 中统一生成**
* **DatasetSanity → 真正影响 HPO 的特征子集**
* **PI → 管住了特征 ID / label ID / sandbox 表达式**

下一步可以做的就是我之前说的：

1. 设计一个 **Dataset/Label Summary Agent**

   * 把 `sanity_result.summary + label_meta` 压缩成一段非常短的自然语言摘要
   * 专门给 AI 大脑看，让它能根据“数据可靠度 + 标签定义”动态调整策略 / 置信度

2. 把这个 Summary Agent 接入你现有的 **AI 大脑 orchestrator / prompt flow**

   * 让大脑每次决定是否训练 / 是否下单时，能感知到“这批数据是不是垃圾”。

如果你觉得现在这一步没问题，就再说一声「继续」，我就直接把 SummaryAgent + 大脑接入的那一层也落地掉。
好，那这一轮我们干两件事：

1. 把 **Dataset + Label 的体检结果压缩成一段给 AI 大脑看的“总结文案”**
2. 把这个总结 **接到训练流水线里**，让大脑真正“看得见数据质量和标签定义”，而不是瞎练

我会继续按“可直接落地”的标准给你代码。

---

## 一、`DatasetSummaryAgent`：把一堆统计变成一句人话

新建一个模块，比如：

`brain/dataset_summary.py`

```python
# brain/dataset_summary.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from dataset_sanity.core import DatasetSanityResult
from labels.engine import LabelAttachResult, LabelDef


@dataclass
class DatasetQualitySummary:
    """
    体检结果 + 标签信息 → 压缩成 AI 大脑能看懂的一段话。
    """
    dataset_name: str
    quality_score: float          # 0–100
    severity: str                 # "ok" / "warning" / "critical"
    reasons: List[str]
    suggested_actions: List[str]
    llm_text: str                 # 专门给 LLM prompt 用的短文案
    meta: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _compute_quality_score(
    sanity: DatasetSanityResult,
    *,
    label_stats_issue: bool,
) -> Tuple[float, str, List[str], List[str]]:
    """
    简单的启发式打分器 – 足够稳定即可，不追求学术严谨。
    """
    score = 100.0
    reasons: List[str] = []
    actions: List[str] = []

    # 1) 缺失值
    n_missing_cols = len(sanity.high_missing_cols or [])
    if n_missing_cols > 0:
        penalty = min(30.0, 5.0 * n_missing_cols)
        score -= penalty
        reasons.append(f"{n_missing_cols} 个特征缺失率过高（> 阈值）")
        actions.append("考虑删除高缺失特征或用更合理的方式填补缺失值。")

    # 2) 常量 / 零方差
    n_zero_var = len(sanity.zero_variance_cols or []) + len(sanity.constant_cols or [])
    if n_zero_var > 0:
        penalty = min(20.0, 3.0 * n_zero_var)
        score -= penalty
        reasons.append(f"{n_zero_var} 个特征几乎不变化（常量或零方差）")
        actions.append("这些特征对模型贡献极小，建议从特征集合中剔除。")

    # 3) 高相关（多重共线）
    n_corr_pairs = len(sanity.highly_correlated_pairs or [])
    if n_corr_pairs > 0:
        penalty = min(15.0, 2.0 * n_corr_pairs)
        score -= penalty
        reasons.append(f"存在 {n_corr_pairs} 对高度相关特征（|corr| > 阈值）")
        actions.append("考虑在其中只保留一部分，或使用正则化模型缓解共线性。")

    # 4) 标签问题
    if label_stats_issue:
        score -= 15.0
        reasons.append("标签分布极度不平衡或几乎为常量。")
        actions.append("考虑重新设计标签阈值、horizon 或采用重采样/加权损失。")

    # 保证在 [0, 100]
    score = float(max(0.0, min(100.0, score)))

    if score >= 85:
        severity = "ok"
    elif score >= 60:
        severity = "warning"
    else:
        severity = "critical"

    if not reasons:
        reasons.append("未发现明显的结构性数据问题。")
    if not actions:
        actions.append("当前数据质量可接受，可以继续训练；后续可在模型检验阶段进一步监控。")

    return score, severity, reasons, actions


def _summarize_label_stats(
    df: pd.DataFrame,
    attach_result: LabelAttachResult,
) -> Dict[str, Any]:
    """
    对 target 列做一个轻量统计，用于给大脑看。
    """
    col = attach_result.target_col
    label_def: LabelDef = attach_result.label_def

    out: Dict[str, Any] = {
        "label_id": label_def.id,
        "type": label_def.type,
        "horizon_bars": label_def.horizon_bars,
        "method": label_def.method,
        "params": label_def.params,
    }

    series = df[col].dropna()
    n = len(series)
    out["n_non_null"] = int(n)
    out["n_total"] = int(len(df))

    if n == 0:
        out["issue"] = "no_valid_labels"
        return out

    if label_def.type == "regression":
        mean = float(series.mean())
        std = float(series.std(ddof=1) if n > 1 else 0.0)
        p1 = float(series.quantile(0.01))
        p99 = float(series.quantile(0.99))
        out.update(
            {
                "mean": mean,
                "std": std,
                "p01": p1,
                "p99": p99,
            }
        )
        # 判定是否几乎为常量
        out["issue"] = "ok"
        if std < 1e-6:
            out["issue"] = "almost_constant"
    else:
        # 分类 – 按唯一值统计比例
        value_counts = series.value_counts(normalize=True).to_dict()
        counts_abs = series.value_counts().to_dict()
        out["class_ratio"] = {str(k): float(v) for k, v in value_counts.items()}
        out["class_count"] = {str(k): int(v) for k, v in counts_abs.items()}

        # 简单不平衡判定：最大类别 > 0.98
        if value_counts and max(value_counts.values()) > 0.98:
            out["issue"] = "extreme_imbalance"
        else:
            out["issue"] = "ok"

    return out


def build_dataset_quality_summary(
    *,
    dataset_name: str,
    df: pd.DataFrame,
    sanity: DatasetSanityResult,
    label_attach_result: LabelAttachResult,
    max_cols_in_list: int = 6,
) -> DatasetQualitySummary:
    """
    核心入口：用在训练 orchestrator / HPO 之前，先跑完体检，再生成给 LLM 的一段总结。
    """
    # 1) 标签统计
    label_stats = _summarize_label_stats(df, label_attach_result)
    label_issue = label_stats.get("issue") not in (None, "ok")

    # 2) 综合打分
    score, severity, reasons, actions = _compute_quality_score(
        sanity,
        label_stats_issue=label_issue,
    )

    # 3) 拼装一段专门给 LLM 的短文案（不超过几百字）
    label_def: LabelDef = label_attach_result.label_def

    def _short_list(name: str, cols: List[str]) -> str:
        if not cols:
            return ""
        cols_sorted = sorted(cols)
        if len(cols_sorted) <= max_cols_in_list:
            return f"{name}: {', '.join(cols_sorted)}。"
        else:
            head = ", ".join(cols_sorted[:max_cols_in_list])
            return f"{name}: {head} 等 {len(cols_sorted)} 个。"

    parts: List[str] = []

    parts.append(
        f"数据集 {dataset_name} 的总体数据质量评估为 {severity.upper()} (score={score:.1f}/100)。"
    )

    if sanity.high_missing_cols:
        parts.append(
            _short_list("高缺失特征(缺失率过高)", sanity.high_missing_cols)
        )
    if sanity.zero_variance_cols or sanity.constant_cols:
        merged = list(set((sanity.zero_variance_cols or []) + (sanity.constant_cols or [])))
        parts.append(
            _short_list("几乎为常量或零方差的特征", merged)
        )
    if sanity.highly_correlated_pairs:
        # 只列出几对示例
        examples = sanity.highly_correlated_pairs[:max_cols_in_list]
        pair_str = "; ".join(
            f"{a}~{b}(corr={corr:.2f})" for a, b, corr in examples
        )
        parts.append(
            f"存在 {len(sanity.highly_correlated_pairs)} 对高度相关特征，例如: {pair_str}。"
        )

    # 标签描述
    label_desc = (
        f"标签: id={label_def.id}, type={label_def.type}, "
        f"horizon={label_def.horizon_bars} bars, method={label_def.method}。"
    )
    parts.append(label_desc)

    if label_def.type == "regression":
        if label_stats.get("n_non_null", 0) > 0:
            parts.append(
                f"回归标签统计: mean={label_stats.get('mean'):.4g}, "
                f"std={label_stats.get('std'):.4g}, "
                f"p1={label_stats.get('p01'):.4g}, p99={label_stats.get('p99'):.4g}。"
            )
    else:
        class_ratio = label_stats.get("class_ratio") or {}
        if class_ratio:
            # 只展示前几类
            items = list(class_ratio.items())
            items_sorted = sorted(items, key=lambda kv: float(kv[1]), reverse=True)
            top = items_sorted[:max_cols_in_list]
            ratio_str = ", ".join(f"{k}: {v*100:.1f}%" for k, v in top)
            parts.append(f"分类标签分布: {ratio_str}。")

    if label_issue:
        parts.append(f"标签存在潜在问题(issue={label_stats.get('issue')})，需要谨慎使用该数据进行训练或回测。")

    # 只给 LLM 一段 concise 文本
    llm_text = " ".join(parts)

    meta: Dict[str, Any] = {
        "dataset_name": dataset_name,
        "score": score,
        "severity": severity,
        "reasons": reasons,
        "suggested_actions": actions,
        "sanity": {
            "summary": sanity.summary,
            "zero_variance_cols": sanity.zero_variance_cols,
            "constant_cols": sanity.constant_cols,
            "high_missing_cols": sanity.high_missing_cols,
            "highly_correlated_pairs": getattr(sanity, "highly_correlated_pairs", []),
            "diagnostics": sanity.diagnostics,
        },
        "label_stats": label_stats,
    }

    return DatasetQualitySummary(
        dataset_name=dataset_name,
        quality_score=score,
        severity=severity,
        reasons=reasons,
        suggested_actions=actions,
        llm_text=llm_text,
        meta=meta,
    )
```

现在你就有了一个非常清晰的“中间层”：

* 下游只要看：`DatasetQualitySummary.llm_text`（短文案）
* 如果需要更细节：看 `reasons / suggested_actions / meta["sanity"] / meta["label_stats"]`

---

## 二、把 SummaryAgent 接到 PI / HPO / AI 大脑上

### 2.1 先接到 `PrincipalInvestigator`（训练入口）

在上一轮我们升级过 `PrincipalInvestigator`，现在只需要轻微再改一下，在返回 `PIResult` 时带上 `dataset_quality_summary`。

修改 `pi/core.py` 里 `PIResult` 和 `propose()`：

```python
# 片段 – 在文件顶部 dataclass 部分加一个字段

from brain.dataset_summary import DatasetQualitySummary, build_dataset_quality_summary

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame          # 经过 label + 体检 + 特征过滤后的最终 df
    hpo_grid: Dict[str, Any]
    sandbox_rejected: List[Dict[str, Any]]
    sanity_result: DatasetSanityResult
    dataset_quality_summary: DatasetQualitySummary   # 🔴 新增
```

然后在 `propose()` 函数的末尾、`return PIResult(...)` 之前，插入：

```python
        # ---------- 8bis) Dataset Quality Summary – 专门给 LLM / 日志用 ----------
        dq_summary = build_dataset_quality_summary(
            dataset_name=f"{table}:{symbol}:{start_ts}~{end_ts}",
            df=df_final,
            sanity=sanity,
            label_attach_result=label_attach,
        )

        # 原来的 return 替换为：
        return PIResult(
            feature_recipe=recipe_out,
            training_plan=tplan,
            sql=sql,
            dataset_df=df_final,
            hpo_grid=grid_sane,
            sandbox_rejected=rejected_sandbox,
            sanity_result=sanity,
            dataset_quality_summary=dq_summary,   # 🔴 新增
        )
```

这样，**从 PI 往后所有用 `PIResult` 的地方，都能拿到一个结构化的“体检+标签总结”**：

* `result.dataset_quality_summary.llm_text` → 给 AI 大脑看
* `result.dataset_quality_summary.quality_score/severity` → 给 HPO / 调度器决定要不要继续训练
* `result.dataset_quality_summary.meta` → 入库做审计 (model registry / run table)

---

### 2.2 在 HPO / 训练 orchestrator 里使用这个 summary

假设你的 HPO 入口类似下面（伪代码，风格接近 `wf_hpo.py`）：

```python
# hpo/run_experiment.py 伪代码

from pi.core import PrincipalInvestigator
from hpo.runner import run_hpo

def run_experiment(cfg):
    pi = PrincipalInvestigator(cfg["clickhouse"], cfg.get("llm"))
    pi_result = pi.propose(
        table=cfg["table"],
        symbol=cfg["symbol"],
        start_ts=cfg["start_ts"],
        end_ts=cfg["end_ts"],
        goal=cfg["goal"],
        horizon=cfg["horizon"],
        guard_cfg=cfg.get("grid_guard"),
        sanity_cfg=cfg.get("sanity"),
        default_label_id=cfg.get("default_label_id", "ret_10b_fwd_reg"),
    )

    # 🔴 在这里多做一步质量门控
    dq = pi_result.dataset_quality_summary
    print("[DatasetQuality]", dq.severity, dq.quality_score, dq.reasons)

    if dq.severity == "critical":
        # 这里你可以：
        # - 直接抛异常终止本次实验
        # - 或者写入某个 run_status 表，标记为 "blocked_bad_data"
        raise RuntimeError(
            f"Dataset quality is CRITICAL (score={dq.quality_score:.1f}). "
            "Abort HPO to avoid wasting compute."
        )

    # 否则进入正常 HPO
    best_model = run_hpo(
        df=pi_result.dataset_df,
        hpo_grid=pi_result.hpo_grid,
        training_plan=pi_result.training_plan,
        # 你也可以把 dq.meta 一并传进去用于日志
        extra_meta={"dataset_quality": dq.to_dict()},
    )
```

这一步的效果是：

* **坏数据 / 坏标签 直接在训练前被拦截**
* 不是靠你“感觉不对”才去看日志，而是系统有明确的 gate

---

### 2.3 把 `llm_text` 真正喂给 AI 大脑

你前面给了一个 **prompt 库** (`execution_prompt_id → prompt text`)，AI 大脑在执行时会从库里拿：

* 例如：

  * `v3_stable`
  * `v3_fast_market_following_tactical_20251117_v2`
    等等。

这些都是风险管理 / 下单决策用的 prompt。

现在我们要做的是：**在大脑下一次调用 LLM 做“研究 / 评估 / 决策”时，把 dataset quality 一段话带进去**。

假设你有一个“AI 大脑控制 ML 训练”的 orchestrator，大概长这样（伪代码）：

```python
# brain/orchestrator.py 伪代码

from llm.llm_client import LLMClient
from prompt_library import get_prompt_by_id

def ask_brain_to_evaluate_experiment(pi_result, prompt_id: str = "v3_stable"):
    dq = pi_result.dataset_quality_summary
    training_plan = pi_result.training_plan

    system_prompt = get_prompt_by_id(prompt_id)

    user_prompt = (
        "你是量化平台的首席科学家，下面是本次训练数据集的质量与标签概况，"
        "以及即将执行的训练计划，请你判断：\n"
        "1) 是否值得基于此数据进行训练？\n"
        "2) 是否需要调整任务类型、标签或特征集合？\n"
        "3) 如继续训练，哪些风险需要在回测/实盘中重点关注？\n\n"
        "【数据质量总结】\n"
        f"{dq.llm_text}\n\n"
        "【模型训练计划】\n"
        f"{training_plan}\n\n"
        "请输出 JSON：\n"
        "{\n"
        '  "decision": "train" 或 "skip",\n'
        '  "reasons": ["原因1", "原因2"],\n'
        '  "adjustments": {\n'
        '    "task": null 或 "reg"/"clf",\n'
        '    "label_id": null 或 新的 label_id,\n'
        '    "notes": "给人类看的备注"\n'
        "  }\n"
        "}\n"
    )

    llm = LLMClient(...)
    resp = llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
    # 解析 resp JSON，决定后续动作...
    return resp
```

这样就实现了：

* **AI 大脑每次“批准一个训练任务”之前，都能看到：**

  * 数据质量（score / severity / 关键问题）
  * 标签定义（horizon / method / imbalance）
  * 当前训练计划（任务类型 / 模型池 / 决策规则）

而不是：

* “只看一堆 feature name + 模型 hyperparam，完全不知道数据有多烂”。

---

## 三、这一轮我们实际完成了什么？

这一轮已经往前推进了两大步：

1. ✅ **把 DatasetSanity + LabelEngine 提炼成 `DatasetQualitySummary`**

   * 用一个统一的数据结构承载：

     * 质量分数 (0–100)
     * 严重程度（ok / warning / critical）
     * 关键原因
     * 建议动作
     * 给 LLM 看的一段短文案 `llm_text`
     * 全量 meta（sanity + label stats）

2. ✅ **把 SummaryAgent 接入 `PrincipalInvestigator` 和 HPO / Brain**

   * `PIResult` 现在会带上 `dataset_quality_summary`
   * HPO 可以用它来做“是否继续训练”的 gate
   * AI 大脑可以在 prompt 里看见数据质量和标签信息，做更聪明的决策

---

### 下一步我打算做什么？

按之前的大规划，接下来可以继续往两个方向走（你不用选，我会自己按优先级来）：

1. **把 Feature Registry 这块再收紧**：

   * 给 FeatureRegistry 增加 “来源表 / 依赖列 / 粒度 / 是否可在该 horizon 使用”等元数据
   * 配合 DatasetSanity 和 LabelRegistry，把“可用特征集合”进一步做成一个安全的子空间

2. **把 HPO / Model Registry 的“实验记录 + 数据质量 + 标签定义”统一落到一张 run 表或 JSON**：

   * 让每一次模型训练都有：

     * 使用了哪个 dataset（时间段 / symbol / 频率）
     * 数据质量分数多少
     * 使用了哪个 label_id / method / horizon
     * 哪些特征被剔除了、为什么

**这样你回头看一条模型时，不只是看到 “Sharpe=2.3”，还能看到“当时是用一套垃圾标签+垃圾特征调出来的”，不会被假 signal 欺骗。**

如果你继续说一句「继续」，我就往 **Feature Registry 元数据 + Model Run 记录** 方向推进，把“特征空间”和“实验审计”也完全升级。
