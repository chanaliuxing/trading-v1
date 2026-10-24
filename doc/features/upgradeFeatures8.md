好，直接进 **“标签体系升级 + 接入流水线”** 这一步，不废话。

---

## 一、LabelRegistry：把“标签定义”变成强约束合约

先把“标签长什么样”标准化，避免 LLM 随便编 SQL。

### 1. 定义一个简单的 Label Spec 结构

新文件：`labels/registry.py`

```python
# labels/registry.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LabelOutputSpec:
    value_col: str          # 训练用的 y 值列，比如 "y_ret_10m"
    side_col: Optional[str] = None  # 可选: 多空标签列，如 "y_side"
    extra_cols: Optional[Dict[str, str]] = None  # 比如 {"hit_time": "y_tb_time", "hit_type": "y_tb_type"}


@dataclass
class HorizonReturnLabelSpec:
    """
    简单 horizon return 标签: ret_{horizon}_fwd。
    """
    id: str
    mode: str  # "horizon_return"
    horizon_bars: int
    price_col: str = "close"
    output: LabelOutputSpec = None

    def required_columns(self) -> List[str]:
        return [self.price_col]


@dataclass
class TripleBarrierLabelSpec:
    """
    Triple Barrier 标签（止盈/止损/超时）- path dependent。
    """
    id: str
    mode: str  # "triple_barrier"
    horizon_bars: int
    price_col: str = "close"
    high_col: str = "high"
    low_col: str = "low"
    vol_col: str = "atr_14"  # 或你当前用的 ATR 列名
    up_mult: float = 1.0
    down_mult: float = 1.0
    min_hold_bars: int = 1     # 可选: 至少持仓这么多 bar 再判断
    output: LabelOutputSpec = None

    def required_columns(self) -> List[str]:
        return [self.price_col, self.high_col, self.low_col, self.vol_col]


class LabelRegistry:
    """
    非持久化版注册表：先硬编码几个常用 Label。
    之后你可以扩展为从 JSON/YAML 读取。
    """

    def __init__(self) -> None:
        self._labels: Dict[str, Any] = {}
        self._init_builtin()

    def _init_builtin(self) -> None:
        # 1) 简单 horizon return 标签
        ret_10m = HorizonReturnLabelSpec(
            id="ret_10m",
            mode="horizon_return",
            horizon_bars=10,
            price_col="close",
            output=LabelOutputSpec(
                value_col="y_ret_10m",
                side_col="y_side_10m",
                extra_cols=None,
            ),
        )
        self._labels[ret_10m.id] = ret_10m

        # 2) Triple Barrier 版本（10 bar、1 ATR 上下轨）
        tb_10m_1atr = TripleBarrierLabelSpec(
            id="tb_10m_1atr",
            mode="triple_barrier",
            horizon_bars=10,
            price_col="close",
            high_col="high",
            low_col="low",
            vol_col="atr_14",
            up_mult=1.0,
            down_mult=1.0,
            min_hold_bars=1,
            output=LabelOutputSpec(
                value_col="y_tb_10m_1atr",
                side_col="y_side_tb_10m_1atr",
                extra_cols={
                    "hit_time": "y_tb_10m_1atr_time",
                    "hit_type": "y_tb_10m_1atr_type",
                    "hit_ret": "y_tb_10m_1atr_ret",
                },
            ),
        )
        self._labels[tb_10m_1atr.id] = tb_10m_1atr

    def get(self, label_id: str) -> Any:
        if label_id not in self._labels:
            raise KeyError(f"Unknown label_id={label_id!r}")
        return self._labels[label_id]

    def list_ids(self) -> List[str]:
        return sorted(self._labels.keys())
```

> 之后你要扩展成 JSON 驱动，可以加一个 `load_from_json(path)`，把参数映射到这两个 dataclass 里即可。

---

## 二、LabelEngine：真正算 Horizon + Triple Barrier 标签（无 SQL 幻觉）

新文件：`labels/engine.py`

```python
# labels/engine.py

from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from labels.registry import (
    HorizonReturnLabelSpec,
    TripleBarrierLabelSpec,
    LabelOutputSpec,
)


def _apply_horizon_return(
    df: pd.DataFrame,
    spec: HorizonReturnLabelSpec,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    简单 horizon return 标签：
    y_ret = (price(t + H) / price(t)) - 1
    y_side = sign(y_ret) with threshold。
    """
    price = df[spec.price_col].astype(float)
    H = spec.horizon_bars
    out_cols = spec.output or LabelOutputSpec(value_col=f"y_ret_{H}")

    # 向前看 H bar → 用负 shift
    fwd_price = price.shift(-H)
    y_ret = (fwd_price / price) - 1.0

    df[out_cols.value_col] = y_ret

    # 可选 side 列
    if out_cols.side_col:
        # 你可以改成 registry 里配置阈值，这里先简单 sign
        side = np.where(y_ret > 0.0, 1, np.where(y_ret < 0.0, -1, 0))
        df[out_cols.side_col] = side

    # manifest：给 AI 大脑用的标签统计
    manifest = {
        "mode": spec.mode,
        "horizon_bars": H,
        "price_col": spec.price_col,
        "value_col": out_cols.value_col,
        "side_col": out_cols.side_col,
        "n_samples": int(df[out_cols.value_col].notna().sum()),
        "nan_ratio": float(df[out_cols.value_col].isna().mean()),
        "value_stats": {
            "min": float(np.nanmin(y_ret)),
            "max": float(np.nanmax(y_ret)),
            "mean": float(np.nanmean(y_ret)),
            "std": float(np.nanstd(y_ret)),
        },
    }
    return df, manifest


def _apply_triple_barrier(
    df: pd.DataFrame,
    spec: TripleBarrierLabelSpec,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Triple Barrier:

    对每个 t：
    - 上轨: price(t) + up_mult * vol(t)
    - 下轨: price(t) - down_mult * vol(t)
    在 [t+1, t+H] 里：
    - 找到第一个 high >= 上轨 → label=+1
    - 或第一个 low <= 下轨 → label=-1
    - 若都没触发，超时: 用 horizon end 的 ret 判断方向
    """
    price = df[spec.price_col].astype(float).to_numpy()
    high = df[spec.high_col].astype(float).to_numpy()
    low = df[spec.low_col].astype(float).to_numpy()
    vol = df[spec.vol_col].astype(float).to_numpy()
    n = len(df)
    H = spec.horizon_bars

    out = spec.output or LabelOutputSpec(
        value_col=f"y_tb_{H}",
        side_col=f"y_side_tb_{H}",
        extra_cols={
            "hit_time": f"y_tb_{H}_time",
            "hit_type": f"y_tb_{H}_type",
            "hit_ret": f"y_tb_{H}_ret",
        },
    )

    y_val = np.full(n, np.nan, dtype=float)
    y_side = np.zeros(n, dtype=int)
    hit_time = np.full(n, np.nan, dtype=float)
    hit_type = np.full(n, "", dtype=object)  # "up" | "down" | "timeout"
    hit_ret = np.full(n, np.nan, dtype=float)

    for i in range(n):
        p0 = price[i]
        if not np.isfinite(p0):
            continue

        up_level = p0 + spec.up_mult * vol[i]
        down_level = p0 - spec.down_mult * vol[i]

        # 查看未来 [i+1, i+H]
        end = min(n - 1, i + H)
        first_hit_idx = None
        first_hit_type = None

        j = i + 1
        while j <= end:
            # 注意：这里用 high/low 是 path-dependent，符合真实交易条件
            if np.isfinite(high[j]) and high[j] >= up_level:
                first_hit_idx = j
                first_hit_type = "up"
                break
            if np.isfinite(low[j]) and low[j] <= down_level:
                first_hit_idx = j
                first_hit_type = "down"
                break
            j += 1

        if first_hit_idx is not None:
            ret = (price[first_hit_idx] / p0) - 1.0
            y_val[i] = ret
            y_side[i] = 1 if first_hit_type == "up" else -1
            hit_time[i] = first_hit_idx - i
            hit_type[i] = first_hit_type
            hit_ret[i] = ret
        else:
            # 超时：用 horizon end 的价格
            if end > i and np.isfinite(price[end]):
                ret = (price[end] / p0) - 1.0
                y_val[i] = ret
                y_side[i] = 1 if ret > 0 else (-1 if ret < 0 else 0)
                hit_time[i] = end - i
                hit_type[i] = "timeout"
                hit_ret[i] = ret

    # 写回 DataFrame
    df[out.value_col] = y_val
    if out.side_col:
        df[out.side_col] = y_side
    if out.extra_cols:
        if "hit_time" in out.extra_cols:
            df[out.extra_cols["hit_time"]] = hit_time
        if "hit_type" in out.extra_cols:
            df[out.extra_cols["hit_type"]] = hit_type
        if "hit_ret" in out.extra_cols:
            df[out.extra_cols["hit_ret"]] = hit_ret

    manifest = {
        "mode": spec.mode,
        "horizon_bars": H,
        "price_col": spec.price_col,
        "high_col": spec.high_col,
        "low_col": spec.low_col,
        "vol_col": spec.vol_col,
        "value_col": out.value_col,
        "side_col": out.side_col,
        "extra_cols": out.extra_cols,
        "n_samples": int(np.isfinite(y_val).sum()),
        "nan_ratio": float(np.mean(~np.isfinite(y_val))),
        "value_stats": {
            "min": float(np.nanmin(y_val)),
            "max": float(np.nanmax(y_val)),
            "mean": float(np.nanmean(y_val)),
            "std": float(np.nanstd(y_val)),
        },
        "side_stats": {
            "n_pos": int((y_side == 1).sum()),
            "n_neg": int((y_side == -1).sum()),
            "n_flat": int((y_side == 0).sum()),
        },
    }
    return df, manifest


def apply_label(
    df: pd.DataFrame,
    label_spec: Any,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    统一入口：根据 spec.mode 调度不同实现。
    返回:
      - 带新 label 列的 df
      - label_manifest（给 dataset_manifest / AI 大脑使用）
    """
    if isinstance(label_spec, HorizonReturnLabelSpec):
        return _apply_horizon_return(df.copy(), label_spec)
    if isinstance(label_spec, TripleBarrierLabelSpec):
        return _apply_triple_barrier(df.copy(), label_spec)
    raise TypeError(f"Unsupported label spec type: {type(label_spec)}")
```

> 关键点：
>
> * **所有标签计算都在 Python / Pandas 层完成**，不会再让 LLM 写任何 `target` SQL。
> * Triple barrier 是 path-dependent，用 high/low 做真正的“谁先碰到”的逻辑。
> * `manifest` 把标签的分布和样本数写出来，后面会并入 `dataset_manifest` 让 AI 大脑一起看。

---

## 三、把 LabelEngine 接进现有流水线

### 1. PrincipalInvestigator：不再让 LLM 写 target SQL

我们需要改造之前的 `PrincipalInvestigator.propose`，让它只负责：

* 选特征 + 算 training_plan
* 生成 SELECT SQL：只拿 **特征列 + label 所需的原始列（close/high/low/vol）**
* 不再传 `target` 下去

**修改版示意（核心 diff）**：

```python
# pi/principal_investigator.py (只展示 propose 部分的关键修改)

from labels.registry import LabelRegistry
from labels.engine import apply_label

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
        label_id: str,                   # ★ 新增：指定用哪个 label 合约
        guard_cfg: Optional[Dict[str, Any]] = None,
    ) -> PIResult:
        catalog = _fetch_catalog(self._client, table)
        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"特征: {', '.join(catalog)}\n"
            f"预测窗口: {horizon}\n"
            f"标签: {label_id}\n"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
            "不要编写 target SQL，target 由后端 LabelEngine 负责。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {})
        tplan = plan.get("training_plan", {})

        # 1) 从 LabelRegistry 取 label_spec
        label_registry = LabelRegistry()
        label_spec = label_registry.get(label_id)

        # 2) 根据 recipe + label_spec.required_columns 决定 SELECT 的列
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,                  # ★ 不再由 LLM 提供 target SQL
            feature_catalog=catalog,
            extra_columns=label_spec.required_columns(),  # ★ 新增: label 需要的列
        )

        df = self._client.query_df(sql)

        # 3) 在 df 上应用标签
        df_with_label, label_manifest = apply_label(df, label_spec)

        # 4) 构建 HPO grid（加上 target_col 信息）
        target_col = label_spec.output.value_col
        grid = {
            "model_type": [],
            "feature_subset": [recipe.get("base_features", []) + [d.get("alias") for d in recipe.get("derived", [])]],
            "task": [tplan.get("task", "reg")],
            "decision": [tplan.get("decision", {"mode": "quantile", "q_long": 0.7, "q_short": 0.3})],
            "model_params": [],
            "target_col": [target_col],   # ★ 让 HPO 明确知道 y 列
        }
        for cand in tplan.get("model_candidates", []) or []:
            grid["model_type"].append(cand.get("model_type", "xgb"))
            grid["model_params"].append(cand.get("model_params", {}))

        guard = (guard_cfg or {}) | {
            "allow_params": ["model_type", "feature_subset", "task", "decision", "model_params", "target_col"]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        # 5) dataset_manifest 里加上 label_manifest（如果你前面已有 DatasetSanity，可以 merge）
        dataset_manifest = {
            # ... 你现有的 feature/data manifest ...
            "label": label_manifest,
        }

        return PIResult(
            feature_recipe=recipe,
            training_plan={**tplan, "target_col": target_col},
            sql=sql,
            dataset_df=df_with_label,
            hpo_grid=grid_sane,
            # 如果你之前给 PIResult 加过 dataset_manifest，这里一起返回
            # dataset_manifest=dataset_manifest,
        )
```

> 现在，“标签是什么 + 怎么算”只受 **LabelRegistry + LabelEngine** 控制，LLM 只能说“我要哪个 label_id”。

---

### 2. Orchestrator：把 label_id 和 label_def 真正连起来

在上一轮我们写的 `run_brain_controlled_hpo(...)` 里，已经有 `label_id` / `label_def` 字段。现在要做的只是：

* 调用 `pi.propose(...)` 时把 `label_id` 传进去；
* `label_def` 可以直接来自 `LabelRegistry.get(label_id)` 的 `asdict` 或简要 metadata。

示意修改：

```python
# orchestrators/brain_hpo_orchestrator.py

from dataclasses import asdict
from labels.registry import LabelRegistry

def run_brain_controlled_hpo(..., label_id: str, ...):
    label_registry = LabelRegistry()
    label_spec = label_registry.get(label_id)
    label_def = asdict(label_spec) if hasattr(label_spec, "__dict__") else {"id": label_id}

    # 1) PI 现在需要 label_id
    pi_result = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
        label_id=label_id,
    )

    df = pi_result.dataset_df
    dataset_manifest = pi_result.dataset_manifest   # 假设这里已经包含 label_manifest

    ...
    # start_experiment_run(...) 里传入 label_id / label_manifest
    exp_logger = start_experiment_run(
        log_dir=log_dir,
        symbol=symbol,
        table=table,
        freq=freq,
        goal=goal,
        horizon=horizon,
        label_id=label_id,
        label_manifest=label_def,      # 或 dataset_manifest["label"]
        ...
    )

    # HPO 用 training_plan 里的 target_col
    best_trial = run_hpo(
        df=df,
        target_col=pi_result.training_plan["target_col"],
        hpo_grid=adjusted_grid,
        config=training_plan.get("hpo_config", {}),
        experiment_logger=exp_logger,
    )
```

---

## 四、这一轮小结 & 下一步

这一轮已经落地：

1. ✅ **LabelRegistry**：

   * 标准化了 `ret_10m` 和 `tb_10m_1atr` 两种标签定义；
   * 每个 label 有固定的 id、模式、所需列、输出列名。

2. ✅ **LabelEngine**：

   * 在 **Python 层** 计算标签，避免 LLM 写 target SQL；
   * 支持 horizon return + triple barrier（path-dependent）。

3. ✅ **PI + Orchestrator 接入**：

   * PI 只决定特征和训练计划，不碰 target SQL；
   * 所有 label 通过 label_id + LabelEngine 统一计算；
   * HPO 明确拿 `target_col`，Audit 记录 label 信息。

---

你只要回复一个字：**“继续”**，
我下一步就按计划去做：

> 把 **LabelSanity / DatasetManifest 的 label 部分** 做细（不平衡检测、极端偏态、正负样本占比），并把这块的信息优先级提高，让 AI 大脑在 `DatasetGate.decide()` 时能明确知道：
> “这个标签分布是否健康 / 是否适合当前任务类型（cls vs reg）”。
好，我们直接把这一步**落地成代码**：
把「标签体检」真正接进 DatasetSanity + Gate + AI 大脑。

我会做 3 件事：

1. `labels/sanity.py`：专门的 Label 体检模块
2. 扩展 `dataset_sanity.py`：把 label 体检结果并入 `dataset_manifest`
3. 调整 Gate / Orchestrator：让 AI 大脑和 Gate 都真正用上这些信息

---

## 一、Label 体检模块：`labels/sanity.py`

目标：给定 **DataFrame + label_spec / label_cols + task_type**，输出一份结构化的 `LabelSanityReport`。

新文件：`labels/sanity.py`

```python
# labels/sanity.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from labels.registry import HorizonReturnLabelSpec, TripleBarrierLabelSpec, LabelOutputSpec


@dataclass
class LabelSanityConfig:
    """
    针对标签分布的体检阈值。
    可根据经验/回测慢慢调优。
    """
    # 最小有效样本数（非 NaN 的 label）
    min_samples: int = 5_000

    # 对回归标签: 允许的最大 |z-score|（均值偏移）
    max_abs_mean_z_reg: float = 1.5

    # 对分类标签: 允许的最大 majority 比例
    max_majority_ratio_cls: float = 0.99  # >0.99 认为极度不平衡

    # 连续标签中极端值的比例上限 (如 >99% 分位 |y| > limit)
    extreme_tail_ratio_reg: float = 0.02  # >2% 极端尾部

    # 打印/记录用
    enable_debug: bool = False


@dataclass
class LabelSanityReport:
    """
    标签健康报告：既能给 Gate 用，也能塞进 dataset_manifest 给 AI 大脑看。
    """
    label_id: str
    value_col: str
    side_col: Optional[str]

    task_type: str  # "reg" | "cls"

    n_samples: int
    nan_ratio: float

    # 回归统计
    mean: Optional[float] = None
    std: Optional[float] = None
    min: Optional[float] = None
    max: Optional[float] = None
    p1: Optional[float] = None
    p50: Optional[float] = None
    p99: Optional[float] = None
    abs_mean_z: Optional[float] = None
    extreme_tail_ratio: Optional[float] = None

    # 分类统计 (side)
    n_pos: Optional[int] = None
    n_neg: Optional[int] = None
    n_zero: Optional[int] = None
    majority_ratio: Optional[float] = None

    # 体检结论
    is_ok: bool = True
    issues: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _get_label_output_spec(label_spec: Any) -> LabelOutputSpec:
    """
    从 label_spec 上拿到 output 定义。
    （如果你后面扩展了 label spec 类型，这里一并支持。）
    """
    if hasattr(label_spec, "output") and isinstance(label_spec.output, LabelOutputSpec):
        return label_spec.output
    # fallback: 简单构造
    return LabelOutputSpec(
        value_col=getattr(label_spec, "value_col", "y"),
        side_col=getattr(label_spec, "side_col", None),
    )


def run_label_sanity_checks(
    df: pd.DataFrame,
    *,
    label_id: str,
    label_spec: Any,
    task_type: str,  # "reg" | "cls"
    cfg: Optional[LabelSanityConfig] = None,
) -> LabelSanityReport:
    """
    核心入口：对标签进行体检。
    注意：这里默认 df 已经算好了标签列（来自 LabelEngine.apply_label）。
    """
    cfg = cfg or LabelSanityConfig()

    output = _get_label_output_spec(label_spec)
    value_col = output.value_col
    side_col = output.side_col

    issues: Dict[str, Any] = {}

    if value_col not in df.columns:
        raise KeyError(f"Label value_col={value_col!r} not in DataFrame columns")

    y = df[value_col].to_numpy()
    is_finite = np.isfinite(y)
    n_samples = int(is_finite.sum())
    nan_ratio = float((~is_finite).mean())

    # 初始化 report
    report = LabelSanityReport(
        label_id=label_id,
        value_col=value_col,
        side_col=side_col,
        task_type=task_type,
        n_samples=n_samples,
        nan_ratio=nan_ratio,
        issues={},
    )

    if n_samples < cfg.min_samples:
        issues["too_few_samples"] = {
            "n_samples": n_samples,
            "min_required": cfg.min_samples,
        }

    # === 回归任务：检查分布 ===
    if task_type == "reg":
        y_valid = y[is_finite]
        if y_valid.size > 0:
            mean = float(np.nanmean(y_valid))
            std = float(np.nanstd(y_valid))
            report.mean = mean
            report.std = std
            report.min = float(np.nanmin(y_valid))
            report.max = float(np.nanmax(y_valid))
            report.p1 = float(np.nanpercentile(y_valid, 1))
            report.p50 = float(np.nanpercentile(y_valid, 50))
            report.p99 = float(np.nanpercentile(y_valid, 99))

            if std > 0:
                abs_mean_z = abs(mean / std)
                report.abs_mean_z = abs_mean_z
                if abs_mean_z > cfg.max_abs_mean_z_reg:
                    issues["mean_bias"] = {
                        "abs_mean_z": abs_mean_z,
                        "max_allowed": cfg.max_abs_mean_z_reg,
                    }

            # 极端尾部比例：|y| > p99 的比例
            tail_thresh = np.nanpercentile(np.abs(y_valid), 99)
            extreme_mask = np.abs(y_valid) > tail_thresh
            extreme_ratio = float(extreme_mask.mean())
            report.extreme_tail_ratio = extreme_ratio
            if extreme_ratio > cfg.extreme_tail_ratio_reg:
                issues["too_heavy_tails"] = {
                    "extreme_tail_ratio": extreme_ratio,
                    "max_allowed": cfg.extreme_tail_ratio_reg,
                }

    # === 分类任务：检查标签不平衡 ===
    if side_col and side_col in df.columns:
        side = df[side_col].to_numpy()
        n_pos = int((side == 1).sum())
        n_neg = int((side == -1).sum())
        n_zero = int((side == 0).sum())

        report.n_pos = n_pos
        report.n_neg = n_neg
        report.n_zero = n_zero

        total = n_pos + n_neg + n_zero
        if total > 0:
            majority = max(n_pos, n_neg, n_zero)
            majority_ratio = majority / total
            report.majority_ratio = float(majority_ratio)
            if task_type in ("cls", "classification") and majority_ratio > cfg.max_majority_ratio_cls:
                issues["label_imbalance"] = {
                    "majority_ratio": majority_ratio,
                    "max_allowed": cfg.max_majority_ratio_cls,
                    "n_pos": n_pos,
                    "n_neg": n_neg,
                    "n_zero": n_zero,
                }

    report.issues = issues or None
    report.is_ok = not bool(issues)

    if cfg.enable_debug:
        print("[LabelSanity] report =", report.to_dict())

    return report
```

> 现在我们有了一个**独立、可单元测试**的 Label 体检模块。
> 后面所有 Gate / 大脑，都用这一个标准输出。

---

## 二、扩展 DatasetSanity：把 Label 体检并入 dataset_manifest

假设前面我们已经有一个 `dataset_sanity.py`，里面有：

* `run_dataset_sanity_checks(df, config)` 返回 `DatasetSanityResult`，其中包含 `feature_stats`、`row_counts` 等；
* `DatasetManifest` 里有关于列、特征的 meta。

现在我们要做的是：

1. 增加一个可选的 `label_sanity` 字段
2. 在 orchestrator 里，算完 label 后，调用 `run_label_sanity_checks` 并把结果塞进 manifest

### 1. 扩展 DatasetManifest 结构（示意）

```python
# dataset_sanity.py (示意结构，重点在新增字段)

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

@dataclass
class DatasetManifest:
    """
    总的 dataset 内容摘要：给人看 + 给大脑看。
    之前已经有的数据 + 新增的 label_sanity。
    """
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    n_rows: int
    n_cols: int
    feature_summary: Dict[str, Any]
    # 新增：整体体检结果（之前已有）
    data_sanity: Dict[str, Any]

    # ★ 新增：标签体检结果
    label_sanity: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
```

> 你真实工程里也许叫 `DatasetSanityReport` / `DatasetReport`，名字不重要，关键是多了一个 `label_sanity` 字段。

---

## 三、在 Orchestrator 中真正把 LabelSanity 接起来

我们现在串起完整链条：

1. PI → `df_with_label`, `label_spec`
2. DataSanity → `data_sanity_report`
3. LabelSanity → `label_sanity_report`
4. 合并成 `dataset_manifest` → 交给 Gate 和 AI 大脑

### 1. Orchestrator 合并体检结果

在上一轮的 orchestrator 基础上，补上这一段：

```python
# orchestrators/brain_hpo_orchestrator.py (新增/修改部分)

from labels.registry import LabelRegistry
from labels.engine import apply_label
from labels.sanity import run_label_sanity_checks, LabelSanityConfig
from dataset_sanity import run_dataset_sanity_checks, DatasetManifest, DatasetSanityConfig

def run_brain_controlled_hpo(...):
    ...
    label_registry = LabelRegistry()
    label_spec = label_registry.get(label_id)

    # 1) 通过 PI 拿到特征 + 原始列
    pi_result = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
        label_id=label_id,
    )
    df = pi_result.dataset_df

    # 注意：如果你在 PI 里面已经调用了 apply_label，可以跳过这一步
    # df, label_manifest_from_engine = apply_label(df, label_spec)

    # 2) 先做数据体检 (feature / 数值层)
    data_sanity_cfg = DatasetSanityConfig()
    data_sanity_report = run_dataset_sanity_checks(df, cfg=data_sanity_cfg)

    # 3) 再做标签体检
    label_sanity_cfg = LabelSanityConfig()
    task_type = pi_result.training_plan.get("task", "reg")
    label_sanity_report = run_label_sanity_checks(
        df,
        label_id=label_id,
        label_spec=label_spec,
        task_type=task_type,
        cfg=label_sanity_cfg,
    )

    # 4) 合并成 dataset_manifest
    manifest = DatasetManifest(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        n_rows=int(len(df)),
        n_cols=int(df.shape[1]),
        feature_summary=data_sanity_report.feature_summary,  # 举例
        data_sanity=data_sanity_report.to_dict(),
        label_sanity=label_sanity_report.to_dict(),
    )

    # 5) 把 manifest 传给 Gate + 实验 logger + AI 大脑
    gate_decision = DatasetGate().decide(manifest.to_dict(), mode="train")

    if not gate_decision["is_ok"]:
        # 这里可以直接终止，或者降级模式 / 记录日志
        logger.warning("DatasetGate blocked training: %s", gate_decision)
        return {
            "status": "blocked_by_dataset_gate",
            "reason": gate_decision,
            "dataset_manifest": manifest.to_dict(),
        }

    exp_logger = start_experiment_run(
        log_dir=log_dir,
        symbol=symbol,
        table=table,
        freq=freq,
        goal=goal,
        horizon=horizon,
        label_id=label_id,
        label_manifest=label_sanity_report.to_dict(),
        dataset_manifest=manifest.to_dict(),
        ...
    )

    # 6) 正常跑 HPO
    best_trial = run_hpo(
        df=df,
        target_col=pi_result.training_plan["target_col"],
        hpo_grid=adjusted_grid,
        config=training_plan.get("hpo_config", {}),
        experiment_logger=exp_logger,
    )

    ...
```

---

## 四、让 DatasetGate 同时看「数据 + 标签」两个维度

之前的 Gate 可能只看数据分布、NaN 等。现在我们让它对 label 做专门判断。

示意实现：`dataset_gate.py`

```python
# dataset_gate.py

from __future__ import annotations

from typing import Any, Dict, List, Optional


class DatasetGate:
    """
    训练/推理前的数据健康闸门。
    之前已经做了数据方面的检查，现在加上 label_sanity。
    """

    def __init__(self) -> None:
        # 可以把这些阈值配置化，这里先写死
        self.max_nan_ratio = 0.3
        self.min_rows = 5_000

    def decide(self, manifest: Dict[str, Any], mode: str = "train") -> Dict[str, Any]:
        """
        manifest: DatasetManifest.to_dict() 的结果
        mode: "train" | "inference"
        返回: {"is_ok": bool, "severity": "ok|warn|block", "reasons": [...]}。
        """

        reasons: List[str] = []
        severity = "ok"

        data_sanity = manifest.get("data_sanity") or {}
        label_sanity = manifest.get("label_sanity") or {}

        # === 1) 基础数据体检 ===
        n_rows = manifest.get("n_rows", 0)
        if n_rows < self.min_rows and mode == "train":
            severity = "block"
            reasons.append(f"too_few_rows: {n_rows} < {self.min_rows}")

        overall_nan_ratio = data_sanity.get("overall_nan_ratio")
        if overall_nan_ratio is not None and overall_nan_ratio > self.max_nan_ratio:
            severity = "block"
            reasons.append(f"too_many_nans: overall_nan_ratio={overall_nan_ratio:.3f}")

        # 你之前在 data_sanity_report 里定义的其他问题，如：
        # - columns_with_zero_variance
        # - highly_correlated_pairs
        # 可以在这里调整为 warn/block

        # === 2) 标签体检 ===
        if label_sanity:
            label_id = label_sanity.get("label_id")
            issues = label_sanity.get("issues") or {}
            task_type = label_sanity.get("task_type", "reg")

            if issues:
                # 默认：有 issues 就至少 warn
                if severity == "ok":
                    severity = "warn"

                # 针对关键问题直接 block（训练模式）
                if "too_few_samples" in issues and mode == "train":
                    severity = "block"
                if "label_imbalance" in issues and mode == "train" and task_type.startswith("cls"):
                    severity = "block"

                reasons.append(f"label_sanity_issues[{label_id}]: {issues}")

        return {
            "is_ok": severity in ("ok", "warn"),
            "severity": severity,
            "reasons": reasons,
        }
```

> 这样一来：
>
> * 如果 label 样本太少 / 极度不平衡，训练直接被 Gate 拦截；
> * 如果只是轻微偏态，可以先放行但打一个 `warn`，AI 大脑在 prompt 中能看到这些原因。

---

## 五、给 AI 大脑看的「人话摘要」(可选 but 推荐)

为避免 “信息过载”，你可以增加一个小工具函数，把 dense 的统计数据压缩成自然语言摘要字符串，直接塞进 prompt。

新文件：`dataset_summary_text.py`

```python
# dataset_summary_text.py

from __future__ import annotations

from typing import Any, Dict


def summarize_label_for_llm(label_sanity: Dict[str, Any]) -> str:
    """
    把 label_sanity dict 转成一段短文，给 LLM 看。
    """
    if not label_sanity:
        return "Label sanity report is missing."

    lid = label_sanity.get("label_id", "?")
    t = label_sanity.get("task_type", "reg")
    n = label_sanity.get("n_samples", 0)
    nan_ratio = label_sanity.get("nan_ratio", 0.0)

    issues = label_sanity.get("issues") or {}

    lines = [f"Label [{lid}] summary:"]
    lines.append(f"- task_type={t}, n_samples={n}, nan_ratio={nan_ratio:.3f}")

    if t == "reg":
        mean = label_sanity.get("mean")
        std = label_sanity.get("std")
        p1 = label_sanity.get("p1")
        p50 = label_sanity.get("p50")
        p99 = label_sanity.get("p99")
        abs_mean_z = label_sanity.get("abs_mean_z")
        lines.append(f"- value_stats: mean={mean:.5f}, std={std:.5f}, p1={p1:.5f}, p50={p50:.5f}, p99={p99:.5f}")
        lines.append(f"- abs_mean_z={abs_mean_z:.3f}")

    if label_sanity.get("majority_ratio") is not None:
        lines.append(
            f"- side distribution: pos={label_sanity.get('n_pos')}, "
            f"neg={label_sanity.get('n_neg')}, zero={label_sanity.get('n_zero')}, "
            f"majority_ratio={label_sanity.get('majority_ratio'):.3f}"
        )

    if issues:
        lines.append(f"- issues: {issues}")
    else:
        lines.append("- issues: none")

    return "\n".join(lines)
```

在调用 AI 大脑时，把这段文字放进 prompt 的 `context` 里：

```python
from dataset_summary_text import summarize_label_for_llm

label_text = summarize_label_for_llm(manifest.label_sanity)
user_prompt = f"""
[Dataset summary]
...

[Label summary]
{label_text}

You must respect these constraints and avoid overfitting on rare label patterns.
"""
```

---

## 六、这一轮完成的东西 & 下一步

✅ 本轮已经落地的内容：

1. `labels/sanity.py`

   * `LabelSanityConfig`：可调阈值
   * `LabelSanityReport`：统一的标签健康报告
   * `run_label_sanity_checks(...)`：实际对 DataFrame 做体检

2. 扩展 `DatasetManifest`：新增 `label_sanity` 字段

3. Orchestrator 中：

   * 调用 `run_dataset_sanity_checks` + `run_label_sanity_checks`
   * 构建 `dataset_manifest`，传给 Gate + 实验 logger + AI 大脑

4. `DatasetGate`：

   * 同时看 data_sanity + label_sanity
   * 标签极度不平衡 / 样本太少时直接 block 训练

5. 可选的 `summarize_label_for_llm`：防止大脑被统计细节淹没

---

**下一步（我会继续自动推进，不问你）：**

> 把「FeatureRegistry + SQL FeatureView」那一块开始落地：
>
> * 把你现在的特征定义（`feature_programs.json` 那套）整理成一个 `FeatureRegistry`；
> * 保留「固定特征 + 沙箱特征」两层；
> * 配合 `PrincipalInvestigator`，彻底禁止 LLM 随便拼 SQL 列名。

你只要继续说：**“继续”**，我就直接把 FeatureRegistry + View 层和现有 `build_select_sql` 串起来。
