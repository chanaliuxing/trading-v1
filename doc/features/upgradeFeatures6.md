好，那我们按既定 roadmap，正式把 **Label Contract + Label Engine** 落地，让：

* 训练 / 回测 / 实盘 **都用同一套标签定义**
* 标签 **不再由 LLM 写 SQL**，全部在 Python 里、严格防泄露
* AI 大脑看到的 label 信息是干净、有“体检”的

下面直接给你可以落地的东西：

---

## 一、`label_registry.json` 结构（统一标签定义）

先放一个建议版结构，你可以放在 `config/label_registry.json`：

```jsonc
{
  "_comment": "Central label registry. Single source of truth for all targets used by AI Brain.",
  "_version": "1.0",
  "labels": {
    "ret_10m": {
      "type": "forward_return",
      "horizon": "10m",
      "price_col": "close",
      "output_col": "label_ret_10m",
      "normalize": "pct",           // pct | log
      "clip": 0.2,                  // optional: clip return to [-0.2, 0.2]
      "sanity": {
        "min_non_null_frac": 0.95,
        "min_std": 1e-6,
        "min_pos_frac": 0.05,
        "min_neg_frac": 0.05,
        "max_class_imbalance": 100.0
      }
    },

    "tb_10m_atr1": {
      "type": "triple_barrier",
      "horizon": "10m",
      "price_col": "close",
      "high_col": "high",
      "low_col": "low",
      "vol_col": "atr_14",          // 用 ATR 做动态栅栏，也可以不用，设为 null
      "up_mult": 1.0,               // 上栅栏：close + 1 * ATR
      "down_mult": 1.0,             // 下栅栏：close - 1 * ATR
      "output_col": "label_tb_10m_atr1",
      "classes": [-1, 0, 1],
      "sanity": {
        "min_non_null_frac": 0.95,
        "min_std": 1e-6,
        "min_pos_frac": 0.05,
        "min_neg_frac": 0.05,
        "max_class_imbalance": 100.0
      }
    }
  }
}
```

之后你只需要通过 `label_id`（例如 `"ret_10m"`）就能在**所有 pipeline** 里引用统一定义。

---

## 二、核心实现：`labels/label_engine.py`

新建一个模块，比如放在 `labels/label_engine.py`：

```python
# labels/label_engine.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import json
import math

import numpy as np
import pandas as pd


# ---------- 工具函数：时间尺度解析 ----------

def _parse_duration_to_seconds(s: str) -> int:
    """
    把 "10m" / "5s" / "1h" 这类字符串转成秒数。
    """
    s = s.strip().lower()
    if not s:
        raise ValueError("Empty duration string")

    unit = s[-1]
    try:
        value = int(s[:-1])
    except ValueError as exc:
        raise ValueError(f"Invalid duration format: {s}") from exc

    if unit == "s":
        return value
    if unit == "m":
        return value * 60
    if unit == "h":
        return value * 3600
    raise ValueError(f"Unsupported duration unit: {unit} in {s}")


def _steps_for_horizon(horizon: str, freq: str) -> int:
    """
    根据 horizon 和 bar 频率，计算「前看步数」。
    例如 horizon="10m", freq="1m" → 10 步。
    """
    h_sec = _parse_duration_to_seconds(horizon)
    f_sec = _parse_duration_to_seconds(freq)
    steps = max(1, h_sec // f_sec)
    return steps


# ---------- Dataclasses ----------

@dataclass
class LabelSanityConfig:
    min_non_null_frac: float = 0.9
    min_std: float = 1e-8
    min_pos_frac: float = 0.0
    min_neg_frac: float = 0.0
    max_class_imbalance: float = 1e6  # max(pos/neg) 或 neg/pos


@dataclass
class LabelSpec:
    id: str
    type: str                   # "forward_return" | "triple_barrier" ...
    horizon: str
    price_col: str
    output_col: str

    # forward_return
    normalize: str = "pct"      # "pct" or "log"
    clip: Optional[float] = None

    # triple_barrier
    high_col: Optional[str] = None
    low_col: Optional[str] = None
    vol_col: Optional[str] = None
    up_mult: Optional[float] = None
    down_mult: Optional[float] = None
    classes: Optional[Tuple[int, ...]] = None

    sanity: Optional[LabelSanityConfig] = None


# ---------- Registry ----------

class LabelRegistry:
    """
    中央标签注册表：从 JSON 加载，按 label_id 查询 LabelSpec。
    """

    def __init__(self, labels: Dict[str, LabelSpec]) -> None:
        self._labels = labels

    @classmethod
    def from_path(cls, path: str | Path) -> "LabelRegistry":
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        labels: Dict[str, LabelSpec] = {}
        for label_id, cfg in (raw.get("labels") or {}).items():
            sanity_cfg = cfg.get("sanity") or {}
            sanity = LabelSanityConfig(
                min_non_null_frac=sanity_cfg.get("min_non_null_frac", 0.9),
                min_std=sanity_cfg.get("min_std", 1e-8),
                min_pos_frac=sanity_cfg.get("min_pos_frac", 0.0),
                min_neg_frac=sanity_cfg.get("min_neg_frac", 0.0),
                max_class_imbalance=sanity_cfg.get("max_class_imbalance", 1e6),
            )

            labels[label_id] = LabelSpec(
                id=label_id,
                type=cfg["type"],
                horizon=cfg["horizon"],
                price_col=cfg["price_col"],
                output_col=cfg["output_col"],
                normalize=cfg.get("normalize", "pct"),
                clip=cfg.get("clip"),
                high_col=cfg.get("high_col"),
                low_col=cfg.get("low_col"),
                vol_col=cfg.get("vol_col"),
                up_mult=cfg.get("up_mult"),
                down_mult=cfg.get("down_mult"),
                classes=tuple(cfg.get("classes", [])) or None,
                sanity=sanity,
            )
        return cls(labels)

    def get(self, label_id: str) -> LabelSpec:
        try:
            return self._labels[label_id]
        except KeyError as exc:
            raise KeyError(f"Unknown label_id: {label_id}") from exc

    def list_ids(self) -> list[str]:
        return sorted(self._labels.keys())


# ---------- Label Engine 核心 ----------

class LabelEngine:
    """
    唯一负责「根据 LabelSpec 在 DataFrame 上生成标签」的组件。
    训练 / 回测 / 实盘 **都应该只依赖这里**，禁止 LLM 写 target SQL。
    """

    def __init__(self, registry: LabelRegistry) -> None:
        self._registry = registry

    @classmethod
    def from_path(cls, path: str | Path) -> "LabelEngine":
        return cls(LabelRegistry.from_path(path))

    # ---- 主入口 ----

    def attach_label(
        self,
        df: pd.DataFrame,
        *,
        label_id: str,
        freq: str,
        inplace: bool = False,
    ) -> pd.DataFrame:
        """
        按 label_id 生成标签列，并挂到 df 上。
        - freq: 当前 bars 的频率，例如 "1m" / "5s"
        """
        spec = self._registry.get(label_id)
        label_series = self._compute_label(df, spec, freq=freq)

        if inplace:
            df[spec.output_col] = label_series
            return df
        else:
            out = df.copy()
            out[spec.output_col] = label_series
            return out

    def compute_label_only(
        self,
        df: pd.DataFrame,
        *,
        label_id: str,
        freq: str,
    ) -> pd.Series:
        """
        如果只想拿标签 Series。
        """
        spec = self._registry.get(label_id)
        return self._compute_label(df, spec, freq=freq)

    def sanity_check(
        self,
        df: pd.DataFrame,
        *,
        label_id: str,
    ) -> tuple[Dict[str, Any], bool]:
        """
        对标签分布做简易体检，避免训练在严重不平衡/全 0 的 label 上。
        返回: (report, ok)
        """
        spec = self._registry.get(label_id)
        sanity_cfg = spec.sanity or LabelSanityConfig()
        if spec.output_col not in df.columns:
            raise ValueError(
                f"Label column {spec.output_col} not found in df; "
                f"did you call attach_label() first?"
            )
        y = df[spec.output_col]
        report, ok = _check_label_distribution(y, sanity_cfg)
        return report, ok

    # ---- 内部实现 ----

    def _compute_label(
        self,
        df: pd.DataFrame,
        spec: LabelSpec,
        *,
        freq: str,
    ) -> pd.Series:
        if spec.type == "forward_return":
            return _compute_forward_return(df, spec, freq=freq)
        elif spec.type == "triple_barrier":
            return _compute_triple_barrier(df, spec, freq=freq)
        else:
            raise ValueError(f"Unsupported label type: {spec.type}")


# ---------- 具体 label 计算实现 ----------

def _compute_forward_return(
    df: pd.DataFrame,
    spec: LabelSpec,
    *,
    freq: str,
) -> pd.Series:
    """
    计算简单的前视收益率：
    - horizon 决定向前看的步数
    - normalize: pct / log
    - clip: 限制极值
    """
    steps = _steps_for_horizon(spec.horizon, freq)
    if spec.price_col not in df.columns:
        raise KeyError(f"price_col={spec.price_col} not in DataFrame columns")

    price = pd.to_numeric(df[spec.price_col], errors="coerce").astype(float)
    fwd = price.shift(-steps)

    if spec.normalize == "pct":
        ret = (fwd / price) - 1.0
    elif spec.normalize == "log":
        ret = (np.log(fwd) - np.log(price))
    else:
        raise ValueError(f"Unsupported normalize mode: {spec.normalize}")

    # 最后几行因为没有足够未来数据，标记为 NaN
    ret.iloc[-steps:] = np.nan

    if spec.clip is not None and spec.clip > 0:
        ret = ret.clip(lower=-spec.clip, upper=spec.clip)

    return ret


def _compute_triple_barrier(
    df: pd.DataFrame,
    spec: LabelSpec,
    *,
    freq: str,
) -> pd.Series:
    """
    经典 Triple-Barrier:
    - 根据 entry price + ATR（或固定百分比）设置上下栅栏
    - 在 horizon 内谁先被触发，就是什么标签
    - 如果都没触发，就看 horizon 末尾价格涨跌方向
    输出: {-1, 0, 1} + NaN（尾部不足 horizon 的样本）
    """
    required_cols = [spec.price_col, spec.high_col, spec.low_col]
    for c in required_cols:
        if c and c not in df.columns:
            raise KeyError(f"Required column for triple_barrier missing: {c}")

    steps = _steps_for_horizon(spec.horizon, freq)

    price = pd.to_numeric(df[spec.price_col], errors="coerce").to_numpy(dtype="float64")
    high = pd.to_numeric(df[spec.high_col], errors="coerce").to_numpy(dtype="float64")
    low = pd.to_numeric(df[spec.low_col], errors="coerce").to_numpy(dtype="float64")

    if spec.vol_col:
        if spec.vol_col not in df.columns:
            raise KeyError(f"vol_col={spec.vol_col} not found for triple_barrier")
        vol = pd.to_numeric(df[spec.vol_col], errors="coerce").to_numpy(dtype="float64")
    else:
        vol = np.ones_like(price)

    up_mult = spec.up_mult if spec.up_mult is not None else 0.0
    down_mult = spec.down_mult if spec.down_mult is not None else 0.0

    n = len(df)
    labels = np.full(n, np.nan, dtype="float64")

    for i in range(n):
        # 不足 horizon 的尾部：直接 NaN
        if i + 1 >= n or i + steps >= n:
            labels[i] = np.nan
            continue

        p0 = price[i]
        if not np.isfinite(p0):
            labels[i] = np.nan
            continue

        vol_i = vol[i]
        if not np.isfinite(vol_i) or vol_i <= 0:
            vol_i = 1.0

        up_level = p0 + up_mult * vol_i
        dn_level = p0 - down_mult * vol_i

        # 从下一根 bar 开始检查，最多看 steps 根
        end_idx = min(n - 1, i + steps)
        label = 0.0

        for j in range(i + 1, end_idx + 1):
            hj = high[j]
            lj = low[j]
            if np.isfinite(hj) and hj >= up_level:
                label = 1.0
                break
            if np.isfinite(lj) and lj <= dn_level:
                label = -1.0
                break

        # 如果没触发栅栏，用 horizon 尾点收益方向
        if label == 0.0:
            p_end = price[end_idx]
            if np.isfinite(p_end):
                ret = (p_end / p0) - 1.0
                if ret > 0:
                    label = 1.0
                elif ret < 0:
                    label = -1.0
                else:
                    label = 0.0
            else:
                label = np.nan

        labels[i] = label

    return pd.Series(labels, index=df.index, name=spec.output_col)


# ---------- Label 分布体检 ----------

def _check_label_distribution(
    y: pd.Series,
    cfg: LabelSanityConfig,
) -> Tuple[Dict[str, Any], bool]:
    """
    对标签做一些非常基础但实用的体检：
    - 非空比例
    - 标准差
    - 正负样本比例（如果是连续值，就按 >0, <0 来分）
    - 类别极端不平衡检测
    """
    total = len(y)
    non_null = y.notna().sum()
    non_null_frac = non_null / total if total > 0 else 0.0

    y_valid = y.dropna()
    std = float(y_valid.std()) if len(y_valid) > 0 else 0.0

    pos_frac = 0.0
    neg_frac = 0.0
    imbalance = 1.0

    if len(y_valid) > 0:
        # 连续值 or {-1,0,1} 都用这个规则：
        pos = (y_valid > 0).sum()
        neg = (y_valid < 0).sum()
        pos_frac = pos / len(y_valid)
        neg_frac = neg / len(y_valid)

        if pos > 0 and neg > 0:
            imbalance = max(pos / neg, neg / pos)
        else:
            imbalance = math.inf

    report = {
        "total": total,
        "non_null": non_null,
        "non_null_frac": non_null_frac,
        "std": std,
        "pos_frac": pos_frac,
        "neg_frac": neg_frac,
        "class_imbalance": imbalance,
        "thresholds": {
            "min_non_null_frac": cfg.min_non_null_frac,
            "min_std": cfg.min_std,
            "min_pos_frac": cfg.min_pos_frac,
            "min_neg_frac": cfg.min_neg_frac,
            "max_class_imbalance": cfg.max_class_imbalance,
        },
    }

    ok = True
    if non_null_frac < cfg.min_non_null_frac:
        ok = False
    if std < cfg.min_std:
        ok = False
    if pos_frac < cfg.min_pos_frac:
        ok = False
    if neg_frac < cfg.min_neg_frac:
        ok = False
    if imbalance > cfg.max_class_imbalance:
        ok = False

    report["ok"] = ok
    return report, ok
```

这就是完整的 **Label Engine**，特点：

* **不依赖 LLM**，全部是 deterministic 代码
* 支持：

  * 简单 `forward_return`
  * Triple-Barrier（止盈 / 止损 / 时间）
* 自带 label 分布体检（非空、标准差、正负比例、极端不平衡）

---

## 三、把 Label Engine 接到 PI（PrincipalInvestigator）

我们现在把你的 `PrincipalInvestigator` 也接上 label 引擎，**禁止 LLM 随意写 target SQL**。

原来的 PI 代码（你之前给我的）里有这一段：

```python
plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

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
)
df = self._client.query_df(sql)
```

### 修改点 1：构造时挂上 LabelEngine

```python
from labels.label_engine import LabelEngine

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
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
        self._label_engine = LabelEngine.from_path(
            label_registry_path or "config/label_registry.json"
        )
```

### 修改点 2：PI 只让 LLM 选 **label_id**，不写 SQL

你可以约定：`training_plan` 里只允许字段：

* `label_id`（比如 `"ret_10m"` 或 `"tb_10m_atr1"`）
* `freq` / `task` / `model_candidates` 等

然后改 `propose()` 这一段：

```python
plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

recipe = plan.get("feature_recipe", {})
tplan = plan.get("training_plan", {})

# 1) 用 feature_recipe 生成只包含特征的 SQL（不再让 LLM 写 target 表达式）
sql = build_select_sql(
    table=recipe.get("use_table", table),
    symbol=tplan.get("symbol", symbol),
    start_ts=start_ts,
    end_ts=end_ts,
    recipe=recipe,
    target=None,                  # <--- 这里固定为 None / 去掉 target
    feature_catalog=catalog,
)
df = self._client.query_df(sql)

# 2) 用 LabelEngine 生成标签
label_id = tplan.get("label_id", "ret_10m")
freq = tplan.get("freq", "1m")   # 频率可以跟 bars 表匹配

df = self._label_engine.attach_label(
    df,
    label_id=label_id,
    freq=freq,
    inplace=True,
)

# 3) 对标签做体检，如果不过关，干脆报错，防止浪费算力
label_report, label_ok = self._label_engine.sanity_check(df, label_id=label_id)
if not label_ok:
    raise RuntimeError(f"Label sanity check failed for {label_id}: {label_report}")

# 4) 把 label 信息写回 training_plan，供后续 HPO / 训练使用
label_spec = self._label_engine._registry.get(label_id)
tplan["label_id"] = label_id
tplan["target_col"] = label_spec.output_col
tplan["label_sanity_report"] = label_report
```

> **到这里为止**：从 ClickHouse 拉出来的是纯特征，然后统一在 Python 端算标签，并通过 `label_registry.json` 管理。LLM **只能选 label_id**，不再有写 SQL 的机会。

---

## 四、回测也可以用同一个 Label Engine（可选）

如果你在回测中想对比 “策略决策 vs 真正 label”，可以在 `RuntimeBacktester` 里按同样方式调用：

```python
from labels.label_engine import LabelEngine

class RuntimeBacktester:
    def __init__(..., label_registry_path: Optional[str] = None, ...):
        ...
        self._label_engine = LabelEngine.from_path(
            label_registry_path or "config/label_registry.json"
        )

    def run(...):
        ...
        # 拉 bars 之后，直接生成 label 列（可选）
        bars = self._load_bars(cfg)
        bars = self._label_engine.attach_label(
            bars,
            label_id=cfg.label_id,
            freq=cfg.freq,
            inplace=True,
        )
        ...
```

这样：

* **训练数据**
* **回测统计**
* **未来任何需要 label 的地方**

都会走同一套 **LabelEngine + label_registry.json**，你只需要维护一个 “source of truth”。

---

## 五、我们现在完成了计划里的哪一步？

按我们之前的大计划，现在已经完成：

1. ✅ Dataset Sanity 模块：体检数据集（NaN / Inf / 零方差等）
2. ✅ Label Engine + Label Contract：

   * `label_registry.json` 统一标签定义
   * `LabelEngine` 统一计算逻辑
   * `LabelSanityConfig` 做标签的最小体检
   * PI 已经接上 label 引擎，禁止 LLM 写 target SQL
3. ✅ 回测 Runtime 和实盘 Runtime 用同一条链路（上一轮 + 这一轮）

---

## 接下来我会做什么（你继续回一句“继续”就好）

下一步按原计划，我会：

* 把 **Feature Registry** 和现在的 `feature_programs.json`、`build_select_sql` 串联起来，
* 给你一个 **标准化的特征注册表 + 安全白名单**，
* 再加上特征级的体检（比如强共线性、恒定特征、inf/NaN 列），
* 并把这层的信息汇总成一个紧凑 `dataset_manifest`，喂给 AI 大脑当“眼睛”的输入。

你只要回复一个字：**继续**，我就直接给出 Feature Registry + 代码落地，不再问你要啥。
好，我们直接落地下一个大块：**特征注册表 + 特征体检 + dataset_manifest 给 AI 大脑当“眼睛”用**。
我不会再问你要做什么，就按之前说的路线一步步往前推。

---

## 一、特征注册表 `feature_registry.json`

目标：

* 给 **AI 大脑 / PI / HPO** 一个统一的 **特征白名单**；
* 防止 LLM 随便用 ClickHouse 里某个奇怪列名；
* 先“描述特征”，暂时不强行重构你现有的 `feature_programs.json` / `build_select_sql`，减少改动量；
* 预留“沙箱特征”口子，未来允许有限度的“新公式”实验。

建议放在：`config/feature_registry.json`，结构示例：

```jsonc
{
  "_comment": "Central feature registry. Whitelist of features allowed for AI Brain.",
  "_version": "1.0",
  "features": {
    "close": {
      "kind": "base",                  // base = 原始列; derived = 已知衍生特征; sandbox = 临时特征
      "source_col": "close",           // 在 ClickHouse 表里的真实列名
      "group": "price",                // 分组（价格/成交量/波动率/订单流...）
      "dtype": "float",
      "allowed_freq": ["1s", "5s", "1m", "5m", "15m", "1h"],
      "description": "Last traded price."
    },

    "volume": {
      "kind": "base",
      "source_col": "volume",
      "group": "volume",
      "dtype": "float",
      "allowed_freq": ["1s", "5s", "1m", "5m", "15m", "1h"]
    },

    "vwap_1m": {
      "kind": "base",                  // 已经在表里算好的，可以仍视作 base
      "source_col": "vwap_1m",
      "group": "price",
      "dtype": "float",
      "allowed_freq": ["1m"],
      "description": "1-minute VWAP bar."
    },

    "rvol20": {
      "kind": "derived",               // 由某个 feature_program 算出来
      "source_col": "rvol20",          // build_select_sql 结果里的列名/alias
      "group": "volume",
      "dtype": "float",
      "allowed_freq": ["1m", "5m"],
      "program_id": "rvol",            // 可选：对应 feature_programs.json 里的 program 名称
      "description": "20-period relative volume."
    },

    "ma20_close": {
      "kind": "derived",
      "source_col": "ma20_close",
      "group": "trend",
      "dtype": "float",
      "allowed_freq": ["1m", "5m"],
      "program_id": "ma",
      "description": "20-period moving average of close."
    }
  }
}
```

**关键点：**

* Registry 不负责“怎么计算”特征，只负责“允许哪些特征出现、这些特征叫什么、类型和分组是什么”。
* 真实 SQL 仍然可以留在 `feature_programs.json` / `build_select_sql` 里，避免大手术。
* `kind = "sandbox"` 将来留给 LLM 生成的新特征（下一步再做）。

---

## 二、特征注册表代码：`features/feature_registry.py`

新建目录 `features/feature_registry.py`：

```python
# features/feature_registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import json


@dataclass
class FeatureSpec:
    id: str
    kind: str                 # "base" | "derived" | "sandbox"
    source_col: str           # 实际 DataFrame / ClickHouse 里的列名/alias
    group: str
    dtype: str                # "float" | "int" | "bool" | "category" | ...
    allowed_freq: Tuple[str, ...]
    program_id: Optional[str] = None
    description: str = ""


class FeatureRegistry:
    """
    中央特征注册表：从 JSON 加载，按 feature_id 查询 FeatureSpec。
    """

    def __init__(self, features: Dict[str, FeatureSpec]) -> None:
        self._features = features

    @classmethod
    def from_path(cls, path: str | Path) -> "FeatureRegistry":
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        features: Dict[str, FeatureSpec] = {}
        for fid, cfg in (raw.get("features") or {}).items():
            features[fid] = FeatureSpec(
                id=fid,
                kind=cfg.get("kind", "base"),
                source_col=cfg["source_col"],
                group=cfg.get("group", "other"),
                dtype=cfg.get("dtype", "float"),
                allowed_freq=tuple(cfg.get("allowed_freq", [])),
                program_id=cfg.get("program_id"),
                description=cfg.get("description", ""),
            )
        return cls(features)

    def get(self, feature_id: str) -> FeatureSpec:
        try:
            return self._features[feature_id]
        except KeyError as exc:
            raise KeyError(f"Unknown feature_id: {feature_id}") from exc

    def list_ids(self) -> List[str]:
        return sorted(self._features.keys())

    def resolve_source_cols(self, feature_ids: List[str]) -> List[str]:
        """
        把一组 feature_id 映射成实际 DataFrame 里的列名列表。
        """
        cols: List[str] = []
        for fid in feature_ids:
            spec = self.get(fid)
            cols.append(spec.source_col)
        return cols

    # --- 核心：对 LLM 提出来的 recipe 做白名单过滤 ---

    def sanitize_recipe(
        self,
        recipe: Dict[str, Any],
        *,
        freq: str,
        feature_catalog_from_db: List[str],
        allow_sandbox: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        对 LLM 生成的 feature_recipe 进行过滤 & 标准化：

        - base_features 只能来自 registry，并且必须在 ClickHouse 表中实际存在
        - derived 里的 alias 也必须在 registry 里登记（kind=derived 或 sandbox）
        - 未登记的特征：
          - 如果 allow_sandbox=False → 直接丢弃
          - 如果 allow_sandbox=True → 放到 sandbox 列表里（后面可选地进一步处理）

        返回:
        - cleaned_recipe: 清洗后的 recipe
        - audit_info: 一些审计信息（丢弃了啥、sandbox 了啥）
        """
        base_feats = recipe.get("base_features", []) or []
        derived_defs = recipe.get("derived", []) or []

        known_features = set(self._features.keys())
        feature_catalog = set(feature_catalog_from_db)

        cleaned_base: List[str] = []
        dropped_base: List[str] = []

        for fid in base_feats:
            if fid not in known_features:
                dropped_base.append(fid)
                continue
            spec = self.get(fid)
            if spec.source_col not in feature_catalog:
                # 注册表里有，但当前表没有这个列
                dropped_base.append(fid)
                continue
            if freq and spec.allowed_freq and freq not in spec.allowed_freq:
                # 频率不匹配也丢弃
                dropped_base.append(fid)
                continue
            cleaned_base.append(fid)

        cleaned_derived: List[Dict[str, Any]] = []
        sandbox_derived: List[Dict[str, Any]] = []
        dropped_derived: List[Dict[str, Any]] = []

        for d in derived_defs:
            alias = d.get("alias")
            if not alias:
                dropped_derived.append(d)
                continue

            # 派生特征现在按 alias 作为 feature_id
            if alias in known_features:
                spec = self.get(alias)
                if spec.kind in ("derived", "sandbox"):
                    cleaned_derived.append(d)
                else:
                    dropped_derived.append(d)
            else:
                # 不在 registry 里：
                if allow_sandbox:
                    sandbox_derived.append(d)
                else:
                    dropped_derived.append(d)

        cleaned_recipe = dict(recipe)
        cleaned_recipe["base_features"] = cleaned_base
        cleaned_recipe["derived"] = cleaned_derived
        if allow_sandbox:
            cleaned_recipe["sandbox_derived"] = sandbox_derived

        audit_info = {
            "dropped_base": dropped_base,
            "dropped_derived": dropped_derived,
            "sandbox_derived": sandbox_derived,
        }

        return cleaned_recipe, audit_info
```

> 这一步的意义：以后所有的 **feature_recipe** 都要先经过 `FeatureRegistry.sanitize_recipe`，才能喂给 `build_select_sql`。这样就算 LLM 乱写，也会被挡掉。

---

## 三、特征体检模块：`features/feature_sanity.py`

我们把“重分析”控制在一个**轻量、可采样**的函数里，避免 GEMINI 提醒的性能陷阱。

```python
# features/feature_sanity.py

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd


def run_feature_sanity_checks(
    df: pd.DataFrame,
    *,
    feature_cols: List[str],
    sample_frac: float = 0.2,
    max_corr_features: int = 40,
    corr_threshold: float = 0.98,
) -> Tuple[Dict[str, Any], bool]:
    """
    对一批特征做基础体检：
    - 每列 NaN 比例
    - 每列 是否常数列 (std=0 或近似 0)
    - 采样后的简单相关性矩阵，发现极度共线的特征对

    为了性能：
    - 默认只采样 sample_frac（例如 20%）行
    - 对相关性只在前 max_corr_features 个特征上做

    返回:
    - report: dict，包含 per_feature 和 correlation 信息
    - ok: bool，整体是否“可接受”（不代表完美，只是没有明显爆炸）
    """
    if df.empty:
        return {"error": "empty_dataframe"}, False

    # 采样
    if 0.0 < sample_frac < 1.0 and len(df) > 0:
        df_sample = df.sample(frac=sample_frac, random_state=42)
    else:
        df_sample = df

    per_feature: Dict[str, Any] = {}
    bad_features: List[str] = []

    for col in feature_cols:
        if col not in df_sample.columns:
            per_feature[col] = {"missing_in_df": True}
            bad_features.append(col)
            continue

        series = pd.to_numeric(df_sample[col], errors="coerce")
        total = len(series)
        non_null = series.notna().sum()
        non_null_frac = non_null / total if total > 0 else 0.0
        std = float(series.std()) if non_null > 0 else 0.0

        col_report = {
            "non_null_frac": non_null_frac,
            "std": std,
            "is_constant_like": bool(non_null > 0 and std < 1e-8),
        }

        # 基本规则：非空 < 0.9 或 几乎常数 的特征 标记为 bad
        if non_null_frac < 0.9 or col_report["is_constant_like"]:
            col_report["flag_bad"] = True
            bad_features.append(col)
        else:
            col_report["flag_bad"] = False

        per_feature[col] = col_report

    # 相关性（只在数值型 & 限定数量上做）
    num_cols = []
    for col in feature_cols:
        if col in df_sample.columns and pd.api.types.is_numeric_dtype(df_sample[col]):
            num_cols.append(col)
        if len(num_cols) >= max_corr_features:
            break

    corr_pairs: List[Dict[str, Any]] = []

    if len(num_cols) >= 2:
        corr = df_sample[num_cols].corr()
        # 避免遍历所有组合，这里简单 O(n^2)，但 n 已被 max_corr_features 限制
        for i, col_i in enumerate(num_cols):
            for j in range(i + 1, len(num_cols)):
                col_j = num_cols[j]
                val = corr.loc[col_i, col_j]
                if np.isfinite(val) and abs(val) >= corr_threshold:
                    corr_pairs.append(
                        {
                            "feature_a": col_i,
                            "feature_b": col_j,
                            "corr": float(val),
                        }
                    )

    report = {
        "per_feature": per_feature,
        "high_corr_pairs": corr_pairs,
    }

    # 决定整体是否 ok：有 bad_features 或 超过一定数量的高相关对 就返回 False
    ok = not bad_features and len(corr_pairs) <= 50  # 50 是比较宽松的门槛

    report["bad_features"] = bad_features
    report["ok"] = ok

    return report, ok
```

---

## 四、构建给 AI 大脑的“眼睛”：`dataset_manifest`

这个 manifest 就是我们之前说的：**不把整个 df 喂给 LLM，而是喂一个“小总结”**。

新建 `data_quality/dataset_manifest.py`：

```python
# data_quality/dataset_manifest.py

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd


def build_dataset_manifest(
    *,
    df: pd.DataFrame,
    symbol: str,
    table: str,
    start_ts: str,
    end_ts: str,
    freq: str,
    feature_cols: List[str],
    label_col: str,
    dataset_sanity_report: Dict[str, Any],
    feature_sanity_report: Dict[str, Any],
    label_sanity_report: Dict[str, Any],
) -> Dict[str, Any]:
    """
    把各种体检信息汇总成一个紧凑的 JSON，用于：
    - 存档 / logging
    - 喂给 AI 大脑做决策说明

    注意：这里只放聚合信息，不放原始行数据。
    """
    n_rows = int(len(df))
    n_features = len(feature_cols)

    manifest: Dict[str, Any] = {
        "meta": {
            "symbol": symbol,
            "table": table,
            "freq": freq,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "n_rows": n_rows,
            "n_features": n_features,
            "label_col": label_col,
        },
        "dataset_sanity": dataset_sanity_report,
        "feature_sanity": {
            "ok": feature_sanity_report.get("ok", True),
            "bad_features": feature_sanity_report.get("bad_features", []),
            "high_corr_pairs_sample": feature_sanity_report.get("high_corr_pairs", [])[:20],
        },
        "label_sanity": {
            "ok": label_sanity_report.get("ok", True),
            "summary": {
                "non_null_frac": label_sanity_report.get("non_null_frac"),
                "std": label_sanity_report.get("std"),
                "pos_frac": label_sanity_report.get("pos_frac"),
                "neg_frac": label_sanity_report.get("neg_frac"),
                "class_imbalance": label_sanity_report.get("class_imbalance"),
            },
        },
    }
    return manifest
```

以后 AI 大脑那边的 prompt 可以只看 `dataset_manifest`，而不是直接看大 DataFrame。

---

## 五、把这一切接回 `PrincipalInvestigator.propose()`

我们现在要修改你之前的 `PrincipalInvestigator`：

* 接入 `FeatureRegistry`；
* 对 LLM 产出的 `feature_recipe` 做白名单过滤；
* 在获取 df 后跑：

  * dataset_sanity（你前面已经落地）
  * feature_sanity（新加）
  * label_sanity（上一轮已经在 LabelEngine 里实现）
* 最后构建 `dataset_manifest`，放进 `PIResult` 里，让 AI 大脑“看体检报告”。

### 1. 更新 `PIResult` dataclass（增加 manifest）

```python
from dataclasses import dataclass
from typing import Any, Dict
import pandas as pd

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
    dataset_manifest: Dict[str, Any]   # <--- 新增
```

> 注意：用的时候如果旧代码还没改，你可以先给一个默认 `{}`，或者给 dataclass 设置默认值：`dataset_manifest: Dict[str, Any] = field(default_factory=dict)`。

---

### 2. 在 `PrincipalInvestigator.__init__` 加载 FeatureRegistry

```python
from features.feature_registry import FeatureRegistry
from labels.label_engine import LabelEngine

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        label_registry_path: Optional[str] = None,
        feature_registry_path: Optional[str] = None,
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
        self._label_engine = LabelEngine.from_path(
            label_registry_path or "config/label_registry.json"
        )
        self._feature_registry = FeatureRegistry.from_path(
            feature_registry_path or "config/feature_registry.json"
        )
```

---

### 3. 修改 `propose()` 主流程

重点改动：

1. 用 `_feature_registry.sanitize_recipe` 清洗 recipe；
2. `build_select_sql` 不再接受 `target`；
3. 取回 df 后，先跑 dataset_sanity + feature_sanity；
4. 调用 LabelEngine 附加 label + label_sanity；
5. 构建 `dataset_manifest`；
6. 最终返回 `PIResult`。

```python
from data_quality.dataset_sanity import run_dataset_sanity_checks
from features.feature_sanity import run_feature_sanity_checks
from data_quality.dataset_manifest import build_dataset_manifest

class PrincipalInvestigator:
    ...

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
    ) -> PIResult:
        # 1) 从 ClickHouse 拿 catalog
        catalog = _fetch_catalog(self._client, table)

        # 2) 让 LLM 出一个 plan（feature_recipe + training_plan）
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
        tplan = plan.get("training_plan", {}) or {}

        # 3) 用 FeatureRegistry 对 recipe 做白名单过滤
        recipe, recipe_audit = self._feature_registry.sanitize_recipe(
            recipe_raw,
            freq=freq,
            feature_catalog_from_db=catalog,
            allow_sandbox=False,   # 后续可以开 True，引入 sandbox 特征
        )
        tplan["feature_recipe_audit"] = recipe_audit

        # 4) 生成只包含特征的 SQL（target 不允许在 SQL 里计算）
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,                  # <--- 强制禁用 SQL target
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # 5) 数据集整体体检（行级，NaN/Inf/零方差等）——你前面已经落地 dataset_sanity
        dataset_report, dataset_ok = run_dataset_sanity_checks(df)
        if not dataset_ok:
            # 可以选择 raise / 或者写入 tplan 提示 AI 大脑“本次数据完全不可用”
            raise RuntimeError(f"Dataset sanity check failed: {dataset_report}")

        # 6) 特征体检（列级）
        feature_cols = self._feature_registry.resolve_source_cols(
            recipe.get("base_features", [])
        )
        # derived alias 在 df 里通常已经是列了，也加上
        for d in recipe.get("derived", []):
            alias = d.get("alias")
            if alias and alias in df.columns and alias not in feature_cols:
                feature_cols.append(alias)

        feat_report, feat_ok = run_feature_sanity_checks(
            df,
            feature_cols=feature_cols,
            sample_frac=0.2,
            max_corr_features=40,
            corr_threshold=0.98,
        )
        if not feat_ok:
            # 这里不要直接报错，可以写入 tplan 让 AI 大脑自己判断要不要放弃
            tplan["feature_sanity_ok"] = False
            tplan["feature_sanity_report"] = feat_report
        else:
            tplan["feature_sanity_ok"] = True
            tplan["feature_sanity_report"] = feat_report

        # 7) 生成标签 + 标签体检
        label_id = tplan.get("label_id", "ret_10m")
        df = self._label_engine.attach_label(
            df,
            label_id=label_id,
            freq=tplan.get("freq", freq),
            inplace=True,
        )
        label_report, label_ok = self._label_engine.sanity_check(
            df,
            label_id=label_id,
        )
        if not label_ok:
            raise RuntimeError(f"Label sanity check failed for {label_id}: {label_report}")

        label_spec = self._label_engine._registry.get(label_id)
        tplan["label_id"] = label_id
        tplan["target_col"] = label_spec.output_col
        tplan["label_sanity_report"] = label_report

        # 8) 构建 HPO 网格（原逻辑不变）
        grid = {
            "model_type": [],
            "feature_subset": [
                recipe.get("base_features", [])
                + [d.get("alias") for d in recipe.get("derived", [])]
            ],
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

        # 9) 把 dataset_manifest 打包好，供 AI 大脑使用
        from data_quality.dataset_manifest import build_dataset_manifest

        dataset_manifest = build_dataset_manifest(
            df=df,
            symbol=symbol,
            table=table,
            start_ts=start_ts,
            end_ts=end_ts,
            freq=freq,
            feature_cols=feature_cols,
            label_col=label_spec.output_col,
            dataset_sanity_report=dataset_report,
            feature_sanity_report=feat_report,
            label_sanity_report=label_report,
        )

        tplan["dataset_manifest"] = dataset_manifest

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            dataset_manifest=dataset_manifest,
        )
```

---

## 六、这一轮我们已经完成了什么？

这一轮已经把：

* ✅ **Feature Registry** 落地（JSON + Python 类）；
* ✅ **Feature Sanity Checks** 落地（NaN、常数列、极高相关对）；
* ✅ **Dataset Manifest** 落地，并接入 PI；
* ✅ **PI.propose()** 链路升级：feature 白名单 → dataset & feature & label 体检 → manifest 输出。

到这里：

* AI 大脑每次要训练模型时，它看到的不是“裸数据”，而是：

  * “这次数据的体检报告（行级 + 特征级 + 标签级）”
  * “特征来源 + 配置 + HPO 网格”
* 数据与特征、标签的 **腐烂问题** 基本都有防守底线。

---

## 下一步我会做什么？

照我们最初的大计划，下一步就是：

* 把这些 `dataset_manifest` 和 `label_sanity_report` 真正 **接进 AI 大脑的 prompt**，
* 让大脑学会：“如果 manifest 报告严重问题，就直接拒绝训练 / 回测”，
* 同时设计 **sandbox 特征** 的安全边界（给创造力留一点口子，但不破坏安全）。

你如果继续一句：**继续**，我下一轮就直接落地：

* `Brain` 层如何使用 `dataset_manifest`（prompt 结构 + JSON schema）；
* sandbox 特征的“安全执行器”轮廓（只允许依赖历史列且不包含未来信息）。

不问你要做什么，直接给代码和结构。
