好，继续把 **“标签层 + AI 大脑的眼睛”** 搞成机构级版本。
这一轮我们专注两件事：

1. **LabelRegistry v2**：所有标签定义标准化、可审计、可复用（包括 Triple Barrier）。
2. **LabelEngine**：用统一的 LabelSpec 在 `dataset_df` 上算标签 + 统计，供 AI 大脑和 HPO 使用。

> 我会给你完整代码骨架，但不会强行改你所有现有文件。你可以逐步接进去。

---

## 一、LabelSpec & LabelRegistry v2（统一标签契约）

### 1.1 `labels/spec.py`：标签定义 & 统计结构

```python
# labels/spec.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum
from typing import Any, Dict, Optional


class LabelType(str, Enum):
    FORWARD_RETURN = "forward_return"      # 简单的未来收益率
    TRIPLE_BARRIER = "triple_barrier"      # 三重栅栏（止盈/止损/超时）


@dataclass
class LabelSpec:
    """
    单个标签的“契约定义”。

    注意：
    - 完全不允许写 SQL
    - 所有逻辑都在 Python 里实现，避免信息泄露/乱写 target problem
    """

    id: str                      # 唯一 ID，如 "reg_ret_10m" / "tb_bin_10m_1_1"
    label_type: LabelType        # forward_return / triple_barrier

    # 通用字段
    horizon_bars: int            # 向前看的 bar 数，比如 10 表示 10 根 1m K 的窗口
    price_col: str = "close"     # 计算收益/栅栏用的价格列
    task: str = "reg"            # "reg" / "cls"；用于后续训练计划

    # FORWARD_RETURN 特有参数
    ret_normalization: str = "raw"  # "raw" / "log" / "pct"

    # TRIPLE_BARRIER 特有参数
    upper_mult: Optional[float] = None   # 比如 0.01 → +1% 上轨
    lower_mult: Optional[float] = None   # 比如 0.01 → -1% 下轨
    timeout_mode: str = "horizon"        # 未来可以扩展 "rolling" 等

    # 输出列名（可用于兼容老代码）
    label_col: str = "label"
    meta_prefix: str = "label_"          # hit_type, t_hit 等前缀

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["label_type"] = self.label_type.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "LabelSpec":
        d = dict(d)
        d["label_type"] = LabelType(d["label_type"])
        return LabelSpec(**d)


@dataclass
class LabelStats:
    """
    用于写入 BrainContext 的标签分布统计。
    """

    id: str
    n: int
    n_valid: int
    frac_valid: float
    mean: float
    std: float
    min: float
    max: float
    # 对于分类类标签可以添加更多字段，如 pos_frac, neg_frac 等
    pos_frac: Optional[float] = None
    neg_frac: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
```

---

### 1.2 `config/label_registry.json`：示例配置

可以先加一个最小可用版本，后面慢慢扩展。

```jsonc
{
  "_comment": "统一标签注册表 v2",
  "_version": "1.0",
  "labels": [
    {
      "id": "reg_ret_10m",
      "label_type": "forward_return",
      "horizon_bars": 10,
      "price_col": "close",
      "task": "reg",
      "ret_normalization": "pct",
      "label_col": "y_ret_10m",
      "meta_prefix": "lbl_ret10m_"
    },
    {
      "id": "cls_bin_ret_10m_up0",
      "label_type": "forward_return",
      "horizon_bars": 10,
      "price_col": "close",
      "task": "cls",
      "ret_normalization": "pct",
      "label_col": "y_bin_10m_up0",
      "meta_prefix": "lbl_bin10m_"
    },
    {
      "id": "tb_bin_10m_1_1",
      "label_type": "triple_barrier",
      "horizon_bars": 10,
      "price_col": "close",
      "task": "cls",
      "upper_mult": 0.01,
      "lower_mult": 0.01,
      "label_col": "y_tb_10m_1_1",
      "meta_prefix": "lbl_tb_10m_"
    }
  ]
}
```

> 说明：
>
> * `reg_ret_10m`：10 根 bar 未来收益率（回归）。
> * `cls_bin_ret_10m_up0`：10 根 bar 后收益率 > 0 记 1，否则 0（分类）。
> * `tb_bin_10m_1_1`：Triple Barrier（+1%/-1%，谁先 hit 决定 label）。

---

### 1.3 `labels/registry.py`：加载与查找

```python
# labels/registry.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .spec import LabelSpec


class LabelRegistry:
    """
    从 JSON 加载 LabelSpec 的注册表。
    """

    def __init__(self, specs: Dict[str, LabelSpec]):
        self._specs = specs

    @classmethod
    def from_json(cls, path: Path) -> "LabelRegistry":
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        specs: Dict[str, LabelSpec] = {}
        for item in raw.get("labels", []):
            spec = LabelSpec.from_dict(item)
            specs[spec.id] = spec
        return cls(specs)

    def get(self, label_id: str) -> LabelSpec:
        try:
            return self._specs[label_id]
        except KeyError as e:
            raise KeyError(f"Label id '{label_id}' not found in registry") from e

    def list_ids(self) -> List[str]:
        return sorted(self._specs.keys())
```

---

## 二、LabelEngine：统一算标签（含 triple barrier）

### 2.1 `labels/engine.py`：实现 forward_return & triple_barrier

```python
# labels/engine.py

from __future__ import annotations

from typing import Dict, Tuple

import numpy as np
import pandas as pd

from .spec import LabelSpec, LabelStats, LabelType


def _compute_forward_return(df: pd.DataFrame, spec: LabelSpec) -> pd.Series:
    price = df[spec.price_col].astype(float)
    fwd = price.shift(-spec.horizon_bars)

    if spec.ret_normalization == "log":
        ret = np.log(fwd) - np.log(price)
    else:  # "pct" or "raw"
        ret = (fwd - price) / price if spec.ret_normalization == "pct" else (fwd - price)

    return ret


def _compute_triple_barrier(
    df: pd.DataFrame,
    spec: LabelSpec,
) -> Tuple[pd.Series, Dict[str, pd.Series]]:
    """
    非向量化简版实现：O(N * horizon_bars)，
    对单支标的 + 中等样本量来说足够。
    性能不够时可以后续用 numba/C++ 优化。

    输出:
      - label: 分类标签（1: hit upper, -1: hit lower, 0: timeout/未触达）
      - meta: 额外信息字典（hit_type, t_hit_index, realized_return 等）
    """
    if spec.upper_mult is None or spec.lower_mult is None:
        raise ValueError("Triple barrier requires upper_mult and lower_mult")

    price = df[spec.price_col].astype(float).values
    high = df.get("high", df[spec.price_col]).astype(float).values
    low = df.get("low", df[spec.price_col]).astype(float).values

    n = len(df)
    horizon = spec.horizon_bars

    labels = np.zeros(n, dtype=float)
    hit_type = np.array(["timeout"] * n, dtype=object)
    realized_ret = np.full(n, np.nan, dtype=float)
    t_hit_idx = np.full(n, -1, dtype=int)

    for i in range(n):
        p0 = price[i]
        if not np.isfinite(p0):
            continue

        upper = p0 * (1.0 + spec.upper_mult)
        lower = p0 * (1.0 - spec.lower_mult)

        j_end = min(n, i + horizon + 1)
        if j_end <= i + 1:
            continue

        # 窗口 (i+1 ... j_end-1)
        hi_path = high[i + 1 : j_end]
        lo_path = low[i + 1 : j_end]

        hit_tp_idx = None
        hit_sl_idx = None

        # 顺序扫描找到首次触达
        for j, (h, l) in enumerate(zip(hi_path, lo_path), start=i + 1):
            if hit_tp_idx is None and h >= upper:
                hit_tp_idx = j
            if hit_sl_idx is None and l <= lower:
                hit_sl_idx = j
            if hit_tp_idx is not None or hit_sl_idx is not None:
                break

        if hit_tp_idx is not None or hit_sl_idx is not None:
            if hit_sl_idx is None:
                first = ("tp", hit_tp_idx)
            elif hit_tp_idx is None:
                first = ("sl", hit_sl_idx)
            else:
                # 两个都触发，取先到的
                first = ("tp", hit_tp_idx) if hit_tp_idx < hit_sl_idx else ("sl", hit_sl_idx)

            tag, k = first
            t_hit_idx[i] = k
            hit_type[i] = tag

            p_hit = price[k]
            r = (p_hit - p0) / p0
            realized_ret[i] = r
            labels[i] = 1.0 if tag == "tp" else -1.0
        else:
            # 超时，按 horizon 末尾价格计算收益
            k = j_end - 1
            p_hit = price[k]
            r = (p_hit - p0) / p0
            realized_ret[i] = r
            t_hit_idx[i] = k
            hit_type[i] = "timeout"
            labels[i] = 0.0

    meta = {
        f"{spec.meta_prefix}hit_type": pd.Series(hit_type, index=df.index),
        f"{spec.meta_prefix}t_hit_idx": pd.Series(t_hit_idx, index=df.index),
        f"{spec.meta_prefix}realized_ret": pd.Series(realized_ret, index=df.index),
    }
    return pd.Series(labels, index=df.index, name=spec.label_col), meta


def compute_labels(
    df: pd.DataFrame,
    spec: LabelSpec,
) -> Tuple[pd.DataFrame, LabelStats]:
    """
    在 dataset_df 上根据 LabelSpec 生成标签列 + 元信息，并返回 LabelStats。
    不修改原 df，而是返回新的 DataFrame。
    """
    df = df.copy()

    if spec.label_type == LabelType.FORWARD_RETURN:
        y = _compute_forward_return(df, spec)
    elif spec.label_type == LabelType.TRIPLE_BARRIER:
        y, meta = _compute_triple_barrier(df, spec)
        for col_name, col_series in meta.items():
            df[col_name] = col_series
    else:
        raise ValueError(f"Unsupported label_type: {spec.label_type}")

    df[spec.label_col] = y

    # ---- 统计 ----
    is_valid = y.notna() & np.isfinite(y.values)
    y_valid = y[is_valid]

    n = len(df)
    n_valid = int(is_valid.sum())
    frac_valid = float(n_valid / n) if n > 0 else 0.0

    if n_valid > 0:
        mean = float(y_valid.mean())
        std = float(y_valid.std())
        min_v = float(y_valid.min())
        max_v = float(y_valid.max())
    else:
        mean = std = min_v = max_v = float("nan")

    pos_frac = neg_frac = None
    if spec.task == "cls" and n_valid > 0:
        pos_frac = float((y_valid > 0).sum() / n_valid)
        neg_frac = float((y_valid < 0).sum() / n_valid)

    stats = LabelStats(
        id=spec.id,
        n=n,
        n_valid=n_valid,
        frac_valid=frac_valid,
        mean=mean,
        std=std,
        min=min_v,
        max=max_v,
        pos_frac=pos_frac,
        neg_frac=neg_frac,
    )

    return df, stats
```

> 现在：
>
> * 所有标签都由 `LabelSpec` + `compute_labels()` 产生；
> * Triple Barrier 的路径依赖完全在 Python 内部实现，没有 SQL、没有泄露。

---

## 三、把 LabelRegistry & LabelEngine 接入现有训练链路

接下来我们**不动你旧的 HPO 代码**，只在 orchestrator 这一层，把标签算清楚，再喂给 HPO 和 AI 大脑。

### 3.1 扩展 `PIResult`（在你之前的 `pi/principal_investigator.py` 上追加字段）

在你现有的：

```python
@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
```

基础上，**追加字段**（不破坏原调用）：

```python
# pi/principal_investigator.py 中

from labels.spec import LabelSpec, LabelStats  # 新增 import

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # 新增：标签相关信息（由 orchestrator 或 PI 后处理填充）
    label_spec_id: str | None = None
    label_spec: LabelSpec | None = None
    label_stats: Dict[str, Any] | None = None  # 保存 stats.to_dict()
```

> 注意：
>
> * 这里不强制在 `PrincipalInvestigator.propose()` 里就算标签；
> * 我们可以在 orchestrator 里**统一做标签计算**，然后把结果回填到 `PIResult` 中。

---

### 3.2 在 orchestrator 里应用 LabelRegistry & LabelEngine

对上一轮给你的 `run_ai_brain_controlled_training` 进行**最小 patch**：

```python
# orchestration/run_ai_brain_training.py 中增加 imports

from labels.registry import LabelRegistry
from labels.engine import compute_labels
from labels.spec import LabelSpec
```

在函数开头初始化 LabelRegistry（和 PromptLibrary 一样）：

```python
def run_ai_brain_controlled_training(
    *,
    ch_cfg: Dict[str, Any],
    llm_cfg: Dict[str, Any],
    brain_cfg: Dict[str, Any],
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    run_id: str,
    prompt_library_path: Path,
    label_registry_path: Path,          # <--- 新参数
) -> Dict[str, Any]:
    ...

    label_reg = LabelRegistry.from_json(label_registry_path)
```

然后在：

```python
    pi_result: PIResult = pi.propose(...)
```

之后，**先做标签计算，再构建 BrainContext**：

```python
    # === 3. 先通过 LabelRegistry + LabelEngine 生成标签 ===

    # 1) 从 training_plan 或 brain_cfg 中确定 label_id
    tplan = pi_result.training_plan or {}
    label_id = (
        tplan.get("label_id") or
        brain_cfg.get("default_label_id") or
        f"reg_ret_{horizon}"  # 简单 fallback，例如 horizon="10m"
    )

    label_spec: LabelSpec = label_reg.get(label_id)

    df_with_label, label_stats = compute_labels(pi_result.dataset_df, label_spec)

    # 回填到 PIResult，后续 BrainContext / HPO 都用这个 df
    pi_result.dataset_df = df_with_label
    pi_result.label_spec_id = label_id
    pi_result.label_spec = label_spec
    pi_result.label_stats = label_stats.to_dict()
```

> 这样：
>
> * 不管 PI / LLM 说了什么 `target_sql`，**真正的标签一律走 LabelRegistry**；
> * Triple Barrier、forward return 都统一规范了；
> * AI 大脑的输入中可以看到 `label_stats`（通过 BrainContext）。

---

### 3.3 确保 BrainContext 能看到标签信息（如你之前的 context_builder）

在你之前的 `brain/context_builder.py`（我之前已经给过一个版本），你只需要确认：

* 从 `pi_result.label_spec` 和 `pi_result.label_stats` 中提取信息；
* 例如：

```python
# brain/context_builder.py 里的 build_brain_context 里新增一小段：

label_section = {
    "label_spec_id": pi_result.label_spec_id,
    "label_spec": (
        pi_result.label_spec.to_dict()
        if pi_result.label_spec is not None
        else None
    ),
    "label_stats": pi_result.label_stats,
}

# 然后在返回的 BrainContext dict 中包含 label_section
```

> 这样 AI 大脑就能看到：
>
> * 当前实验用的是哪一种标签（ret / triple barrier，参数、horizon 等）；
> * 标签分布是否严重不平衡 / 是否几乎全是 0；
> * 可以据此拒绝训练或要求调整。

---

## 四、目前为止，你的“数据+特征+标签+大脑”状态

现在整个链条已经是这样的：

1. **PI**：基于 ClickHouse + Prompt 库，给出 feature_recipe + training_plan + hpo_grid + dataset_df（纯特征 + 基础价格）。

2. **LabelRegistry + LabelEngine（本轮新增）**

   * 所有标签定义落在 `config/label_registry.json`。
   * `compute_labels()` 用统一逻辑生成 label + label_stats。
   * 支持 Triple Barrier 和简单 forward return，避免 LLM 随手写 SQL。

3. **DatasetSanity + LabelStats**（你之前已经有 dataset_sanity；这次多了 label_stats）

   * dataset_sanity 报告特征层的质量；
   * label_stats 报告标签层的质量。

4. **BrainContext**

   * 统一包装 PIResult + dataset_sanity + label_stats → 给 AI 大脑看。

5. **AI 大脑（BrainTrainDecision）**

   * 基于 BrainContext 决定：reject / accept / adjust；
   * 可调整 hpo_grid、training_plan 中关于任务、阈值等内容。

6. **DecisionApplicator + GridGuard**

   * 把大脑的建议安全 merge 成最终训练配置。

7. **HPO / 训练引擎**

   * 用带标签的 `dataset_df` + `training_plan_final` + `hpo_grid_final` 进行训练。

---

如果你再说一次 **“继续”**，我下一步会专注：

* **特征层的 Registry & 稳定化**（FeatureRegistry v2）：

  * 把你现有的 `feature_programs.json` / feature SQL 整理成正式的注册表；
  * 支持 “safe base features” + “sandbox features”（Gemini 提到的沙箱创造力）；
  * 在 SQL 生成层彻底杜绝 LLM 自己拼列名和随意 join。
* 并且把这些信息也接入 BrainContext，让 AI 大脑能看到：

  * 哪些特征是 base（稳定）、哪些是实验性的；
  * 共线/冗余特征的告警，指导它自动关掉垃圾特征组合。
好，我们现在把 **“特征层”**这一块也收拾成机构级：
让 AI 大脑只能用**干净、可审计的特征**，同时保留一点“沙箱创造力”，但所有表达式都经过安全检查。

我会按这样来落地：

1. 建一个统一的 `FeatureRegistry v2`（JSON + Python）。
2. 给 LLM 的特征输出加“白名单 + 沙箱校验”两道门。
3. 改造 `build_select_sql` 用 Registry + 校验过的 sandbox features。
4. 把特征信息也塞进 BrainContext 给 AI 大脑看。

---

## 一、FeatureRegistry v2：特征“户口本”

### 1.1 `features/spec.py`：特征定义结构

```python
# features/spec.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FeatureKind(str, Enum):
    COLUMN = "column"      # 直接来自表的列
    DERIVED = "derived"    # 通过 SQL 表达式计算（注册表内）
    ALIAS = "alias"        # 指向另一个特征 id（比如 group/缩写）


@dataclass
class FeatureDef:
    """
    单个特征定义。
    不允许在这里写 SELECT/FROM/JOIN，只能是单列表达式。
    """

    id: str                          # 全局唯一 id，例如 "close", "ta_rsi_14"
    kind: FeatureKind
    expr: str                        # SQL 表达式，如 "close" 或 "rsi(close, 14)"
    description: str = ""
    group: Optional[str] = None      # "price", "volume", "momentum" 等
    tags: List[str] = field(default_factory=list)
    is_experimental: bool = False    # 标记是否实验性特征

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kind"] = self.kind.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "FeatureDef":
        d = dict(d)
        d["kind"] = FeatureKind(d["kind"])
        return FeatureDef(**d)
```

---

### 1.2 `config/feature_registry.json`：示例注册表

先来一个最小可用版本，你可以按自己实际特征慢慢补。

```jsonc
{
  "_comment": "统一特征注册表 v2，所有 LLM 可用特征必须在这里有定义",
  "_version": "1.0",
  "features": [
    {
      "id": "ts",
      "kind": "column",
      "expr": "ts",
      "description": "timestamp",
      "group": "meta",
      "tags": ["ordering"]
    },
    {
      "id": "symbol",
      "kind": "column",
      "expr": "symbol",
      "description": "ticker symbol",
      "group": "meta",
      "tags": ["filter"]
    },
    {
      "id": "open",
      "kind": "column",
      "expr": "open",
      "description": "OHLC open price",
      "group": "price"
    },
    {
      "id": "high",
      "kind": "column",
      "expr": "high",
      "description": "OHLC high price",
      "group": "price"
    },
    {
      "id": "low",
      "kind": "column",
      "expr": "low",
      "description": "OHLC low price",
      "group": "price"
    },
    {
      "id": "close",
      "kind": "column",
      "expr": "close",
      "description": "OHLC close price",
      "group": "price"
    },
    {
      "id": "volume",
      "kind": "column",
      "expr": "volume",
      "description": "trading volume",
      "group": "volume"
    },
    {
      "id": "vwap",
      "kind": "column",
      "expr": "vwap",
      "description": "volume weighted average price",
      "group": "price"
    },
    {
      "id": "ta_rsi_14",
      "kind": "derived",
      "expr": "rsi(close, 14)",
      "description": "14-period RSI on close",
      "group": "momentum",
      "tags": ["indicator", "rsi"]
    },
    {
      "id": "ta_atr_14",
      "kind": "derived",
      "expr": "atr(high, low, close, 14)",
      "description": "14-period ATR",
      "group": "volatility",
      "tags": ["indicator", "atr"]
    },
    {
      "id": "close_over_vwap",
      "kind": "derived",
      "expr": "close / vwap",
      "description": "distance vs VWAP",
      "group": "rel_price",
      "tags": ["vwap", "relative"]
    }
  ]
}
```

> 后续：你只需要在这个 JSON 里增加/修改特征定义，AI 大脑就能“合法使用”；所有 SQL 表达式都集中在一处，便于审计。

---

### 1.3 `features/registry.py`：加载 + 查询

```python
# features/registry.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .spec import FeatureDef


class FeatureRegistry:
    """
    从 JSON 加载特征定义，并提供查询接口。
    """

    def __init__(self, defs: Dict[str, FeatureDef]):
        self._defs = defs

    @classmethod
    def from_json(cls, path: Path) -> "FeatureRegistry":
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        defs: Dict[str, FeatureDef] = {}
        for item in raw.get("features", []):
            fd = FeatureDef.from_dict(item)
            defs[fd.id] = fd
        return cls(defs)

    def get(self, fid: str) -> FeatureDef:
        try:
            return self._defs[fid]
        except KeyError as e:
            raise KeyError(f"Feature id '{fid}' not found in registry") from e

    def try_get(self, fid: str) -> Optional[FeatureDef]:
        return self._defs.get(fid)

    def has(self, fid: str) -> bool:
        return fid in self._defs

    def list_ids(self) -> List[str]:
        return sorted(self._defs.keys())

    def list_by_group(self, group: str) -> List[str]:
        return sorted(fid for fid, fd in self._defs.items() if fd.group == group)

    def resolve_expr(self, fid: str) -> str:
        """返回某个特征的 SQL 表达式。"""
        fd = self.get(fid)
        return fd.expr

    def filter_existing(self, feature_ids: Iterable[str]) -> List[str]:
        """过滤成在 Registry 里存在的特征。"""
        return [fid for fid in feature_ids if fid in self._defs]
```

---

## 二、Sandbox 特征：给 AI 一点创造力，但要戴“安全帽”

我们允许 LLM 继续在 recipe 里输出一些 `derived` 表达式，但分两类：

* **注册表特征**：`alias` 命中 FeatureRegistry → 直接用 registry 的 expr，忽略 LLM 的 expr（防止被污染）。
* **沙箱特征**：`alias` 不在 registry → 把 `expr` 当 sandbox SQL，先做**严格校验**。

  * 只允许：表内列名 + 一小撮函数 + 算术运算；
  * 禁止：`SELECT` / `FROM` / `JOIN` / 子查询 / 注释 / DDL / DML 等。

### 2.1 `features/sandbox_validator.py`：表达式安全检查

```python
# features/sandbox_validator.py

from __future__ import annotations

import re
from typing import Iterable, List, Set


_SQL_FORBIDDEN_PATTERNS = [
    r";",              # 多条语句
    r"--",             # 行注释
    r"/\*",            # 块注释
    r"\bselect\b",
    r"\bfrom\b",
    r"\bjoin\b",
    r"\bunion\b",
    r"\binsert\b",
    r"\bupdate\b",
    r"\bdelete\b",
    r"\bdrop\b",
    r"\balter\b",
    r"\bcreate\b",
    r"\btruncate\b",
    r"\bmerge\b",
    r"\bwith\b"
]

# 允许的函数和关键字（可根据 ClickHouse 实际情况补充）
_ALLOWED_FUNCS = {
    "abs", "log", "exp", "sqrt",
    "greatest", "least",
    "round", "floor", "ceil",
    "if", "coalesce",
    # 也可以加自定义 UDF，如 rsi、atr 等（如果你允许 sandbox 调这些）
    "rsi", "atr"
}

_ALLOWED_KEYWORDS = {
    "case", "when", "then", "else", "end",
    "and", "or", "not",
    "null", "is"
}


def _tokenize_identifiers(expr: str) -> List[str]:
    """
    粗略切词：提取所有形如 [a-zA-Z_][a-zA-Z0-9_]* 的标识符。
    """
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)


def validate_sandbox_sql_expr(
    expr: str,
    allowed_columns: Iterable[str],
    allowed_funcs: Iterable[str] | None = None,
) -> None:
    """
    如果表达式不安全，抛出 ValueError。

    只做“黑名单 + 白名单”风控，不保证 100% 语法正确，
    但可以阻止绝大部分危险/乱写 SQL。
    """
    s = expr.strip()
    s_lower = s.lower()

    # 1) 禁止出现的模式
    for pat in _SQL_FORBIDDEN_PATTERNS:
        if re.search(pat, s_lower):
            raise ValueError(f"Sandbox SQL expression contains forbidden pattern: {pat!r}")

    # 2) 标识符白名单检查
    cols: Set[str] = set(allowed_columns)
    allowed_funcs_set: Set[str] = set(a.lower() for a in (allowed_funcs or _ALLOWED_FUNCS))

    tokens = _tokenize_identifiers(s)
    for t in tokens:
        tl = t.lower()
        if tl in _ALLOWED_KEYWORDS:
            continue
        if tl in cols:
            continue
        if tl in allowed_funcs_set:
            continue
        # ClickHouse 内置类型/关键字，按需放行，如 "float64", "int32" 等
        if tl in {"float64", "float32", "int32", "int64", "uint32", "uint64"}:
            continue

        # 剩下的就是未知标识符 → 禁止
        raise ValueError(f"Sandbox SQL expression uses unknown identifier: {t!r}")
```

> 这个 validator 很“保守”：
>
> * 只要出现任何未知 ident，就直接拒绝；
> * 后续你可以通过增加 `_ALLOWED_FUNCS` 或 `_ALLOWED_KEYWORDS` 来慢慢放宽。

---

## 三、改造 `build_select_sql`：统一用 FeatureRegistry + Sandbox 校验

假设你现在的 `pi/feature_sql.py` 大致这样：

```python
# 原来
def build_select_sql(table, symbol, start_ts, end_ts, recipe, target, feature_catalog):
    ...
```

我们写一个 **v2 版本**，兼容老的 `feature_recipe` 结构，同时接入 FeatureRegistry & sandbox 校验。

### 3.1 新版 `build_select_sql`（建议整体替换原文件内容）

```python
# pi/feature_sql.py（替换为 v2 版本；如不放心可以先叫 build_select_sql_v2 再渐进切换）

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from features.registry import FeatureRegistry
from features.sandbox_validator import validate_sandbox_sql_expr


def _normalize_base_features(
    recipe: Dict[str, Any],
    feature_catalog: List[str],
    feat_reg: FeatureRegistry,
) -> List[str]:
    """
    归一化 base_features：
    - 优先从 recipe["base_features"] 读取
    - 确保是表内实际存在的列，或 registry 里存在的特征 id
    """
    requested = recipe.get("base_features") or []
    if not isinstance(requested, list):
        requested = []

    exist_cols = set(feature_catalog)
    cleaned: List[str] = []

    for name in requested:
        # 1) 表内原始列
        if name in exist_cols:
            cleaned.append(name)
            continue
        # 2) Registry 里的派生特征 —— 可以视为 “base feature”
        if feat_reg.has(name):
            cleaned.append(name)
            continue
        # 3) 其他：忽略
        # （可加日志：记录被丢弃的列名）
    return cleaned


def _normalize_derived_features(
    recipe: Dict[str, Any],
    feature_catalog: List[str],
    feat_reg: FeatureRegistry,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    处理 recipe["derived"]：

    返回:
    - valid_derived: List[(alias, expr)] —— 已经通过 registry 或 sandbox 校验的派生特征
    - rejected_aliases: 被拒绝的 alias 列表（可用于日志）
    """
    exist_cols = set(feature_catalog)
    derived = recipe.get("derived") or []
    if not isinstance(derived, list):
        derived = []

    valid: List[Tuple[str, str]] = []
    rejected: List[str] = []

    for item in derived:
        if not isinstance(item, dict):
            continue
        alias = item.get("alias")
        expr = item.get("expr", "")

        if not alias:
            continue

        # 1) 如果 alias 在 Registry 中，则优先使用 Registry 表达式，忽略 LLM 提供的 expr
        fd = feat_reg.try_get(alias)
        if fd is not None:
            valid.append((alias, fd.expr))
            continue

        # 2) 否则，当作沙箱特征，expr 必须通过校验
        try:
            validate_sandbox_sql_expr(expr, allowed_columns=exist_cols)
        except ValueError:
            rejected.append(alias)
            continue

        valid.append((alias, expr))

    return valid, rejected


def build_select_sql(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    target: Dict[str, Any] | None,
    feature_catalog: List[str],
    feat_reg: FeatureRegistry,
) -> str:
    """
    v2 版本：所有特征都要么来自 FeatureRegistry，要么来自 sandbox validator。
    """
    # 1) 规范化 base_features
    base_features = _normalize_base_features(recipe, feature_catalog, feat_reg)

    # registry 中的 “列类特征” 可以看作 base_features（例如 close, volume, vwap）
    # 如果需要，你可以自动加上 registry 的某些基础特征：
    # base_features = feat_reg.filter_existing(base_features)  # 可按需打开

    # 2) 规范化 derived
    derived_pairs, rejected_aliases = _normalize_derived_features(
        recipe,
        feature_catalog,
        feat_reg,
    )
    # TODO: rejected_aliases 可以写入日志，或者塞进 BrainContext 的 warnings

    select_exprs: List[str] = []

    # 3) 基础列：如果在 registry 里，则用 expr，否则直接用列名
    for name in base_features:
        fd = feat_reg.try_get(name)
        if fd is not None:
            select_exprs.append(f"{fd.expr} AS {name}")
        else:
            # 原始列
            select_exprs.append(name)

    # 4) 派生特征
    for alias, expr in derived_pairs:
        select_exprs.append(f"{expr} AS {alias}")

    # 5) target 相关列 —— 此处不再允许 LLM 写 target SQL，仅允许声明 id
    #    真正的标签在 Python 里由 LabelEngine 计算，这里只保证有 price_col 等基础列即可。
    #    因此这里不再拼接 target_expr，避免 label 泄露。

    # 确保 ts/symbol 列被选出
    if "ts" not in [c.split(" AS ")[-1] for c in select_exprs]:
        select_exprs.insert(0, "ts")
    if "symbol" not in [c.split(" AS ")[-1] for c in select_exprs]:
        select_exprs.insert(1, "symbol")

    select_clause = ",\n       ".join(select_exprs)

    # 6) 构造 WHERE / ORDER BY
    # 这里仍然是最简单的字符串拼接，实际使用中可以用参数化 / 防注入
    sql = f"""
SELECT
       {select_clause}
FROM {table}
WHERE symbol = '{symbol}'
  AND ts >= '{start_ts}'
  AND ts <= '{end_ts}'
ORDER BY ts ASC
"""
    return sql.strip()
```

> 这样一来：

* LLM 再也不能随便写 `SELECT * FROM other_table` 这种鬼东西；
* 所有特征要么来自 `FeatureRegistry`，要么通过 sandbox 校验；
* 标签仍然是由 LabelEngine 在 Python 层计算（上一轮我们已完成），SQL 完全不碰 target。

---

## 四、接入 PrincipalInvestigator：不改 LLM 协议，先在内部“收紧”

在 `pi/principal_investigator.py` 里，我们之前是这样：

```python
from pi.feature_sql import build_select_sql

...

sql = build_select_sql(
    table=recipe.get("use_table", table),
    symbol=tplan.get("symbol", symbol),
    start_ts=start_ts,
    end_ts=end_ts,
    recipe=recipe,
    target=tplan.get("target"),
    feature_catalog=catalog,
)
```

现在我们只需要给 `PrincipalInvestigator.__init__` 增加一个 FeatureRegistry，并在调用 `build_select_sql` 时传进去。

### 4.1 修改 `PrincipalInvestigator` 构造函数

```python
# pi/principal_investigator.py

from pathlib import Path

from features.registry import FeatureRegistry

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        feature_registry_path: Optional[Path] = None,
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

        # 新增：加载 FeatureRegistry
        if feature_registry_path is None:
            # 可以给一个默认路径，比如 config/feature_registry.json
            feature_registry_path = Path("config/feature_registry.json")
        self._feat_reg = FeatureRegistry.from_json(feature_registry_path)
```

### 4.2 在 `propose()` 里使用新版 `build_select_sql`

```python
    def propose(... ) -> PIResult:
        catalog = _fetch_catalog(self._client, table)
        ...

        recipe = plan.get("feature_recipe", {})
        tplan = plan.get("training_plan", {})
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=tplan.get("target"),
            feature_catalog=catalog,
            feat_reg=self._feat_reg,          # <--- 新增
        )
        df = self._client.query_df(sql)
        ...
```

> 这样做的好处是：
>
> * 对 LLM 协议完全“后向兼容”（它仍然输出 `base_features` 和 `derived`）；
> * 但真正生效的特征已经被你这层“硬规则 + sandbox validator”牢牢钳住了。

---

## 五、把特征信息也塞进 BrainContext（给 AI 大脑看的“特征小抄”）

最后一小步，是把**这次实际生效的特征列表**告诉 AI 大脑，方便它做决策（比如：发现只剩 3 个特征就果断 reject）。

在构建 BrainContext 的地方（你之前已经有一个 `build_brain_context()`），可以增加：

```python
# 假设你在 orchestration 里已经有 pi_result 和 dataset_sanity / label_stats 等

feature_section = {
    "base_features_effective": pi_result.training_plan.get("base_features_effective"),  # 下面建议赋值
    "derived_features_effective": pi_result.training_plan.get("derived_features_effective"),
    "feature_warnings": pi_result.training_plan.get("feature_warnings", []),
}
```

你可以在 `build_select_sql` 返回前把这些信息塞进一个 side-structure，或者在 `PrincipalInvestigator.propose()` 里自己构造，例如：

```python
# 在 propose() 里，在调用 build_select_sql 之前：

# 用 registry + sandbox 归一化后的名单（可以从 build_select_sql 的内部结果返回，也可以重复计算一遍）
# 简单做法：在 propose 前先跑一遍 _normalize_xxx，然后：
recipe["base_features_effective"] = base_features_cleaned
recipe["derived_features_effective"] = [d["alias"] for d in valid_derived]
recipe["feature_warnings"] = {
    "rejected_sandbox_aliases": rejected_aliases,
}
```

> 这样 AI 大脑就能看到：

* 当前训练真正用到哪些 base/derived 特征；
* 有哪些 sandbox 特征被拒绝（说明策略太“花哨”/不受信任）；
* 可以据此降低信任度，或者建议简化特征集。

---

## 小结：现在特征层达到什么水平？

到这里，你的系统在“数据 + 特征 + 标签 + 大脑”四层的状态大致是：

1. **数据层**：

   * dataset_sanity 模块对每个 `dataset_df` 做体检（NaN/Inf/零方差/相关性等）。
2. **特征层（本轮完成的）**：

   * `FeatureRegistry v2` 管住所有标准特征；
   * `SandboxValidator` 管住 LLM 即兴表达式；
   * `build_select_sql` 统一按 registry + sandbox 安全表达式来拼 SQL。
3. **标签层**：

   * `LabelRegistry + LabelEngine` 负责 forward_return + triple barrier，彻底禁止 LLM 自写 target SQL。
4. **大脑层**：

   * BrainContext 里有 dataset_sanity + label_stats + feature_section，AI 大脑可以基于这些“体检报告”来决定是否训练/怎么训练。

---

如果你再说一次“继续”，我会把重点放到 **HPO 网格 + 模型池那一层的 GridGuard/ModelGuard**：

* 限定可用模型类型/参数范围（防止 LLM 给你搞出 1000 depth 的 XGB 或 1024 树 × 1000 轮）；
* 引入简单的 **Model Registry**，区分“稳定模型”（可线上）和“实验模型”；
* 把这些元数据也喂给 AI 大脑，让它知道自己在玩的是“玩具模型”还是“生产候选模型”。
