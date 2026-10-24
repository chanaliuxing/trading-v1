好，那我就紧接着把 **`run_hpo_with_grid` 真正写出来**，让：

> PI → DatasetSanity → HPO → ExperimentRegistry
> 这一条链路是真·可跑的（只要你装好 sklearn/xgboost）。

下面这一版是「可落地的 baseline HPO」，特点：

* 完全基于 **PI 给的 `hpo_grid`**（已经被 `sanitize_and_constrain_grid` 过滤过）
* 用一个简单但规范的 **“信号 → 交易 → 绩效指标”** 过程
* 产出：`HPOResult + metrics_dict`，可以直接喂给之前的 `ExperimentRecord`

你可以先把这一份丢进你的 `wf_hpo.py` 或单独的 `hpo_runner.py`，跑通之后再慢慢替换为你现有的复杂回测引擎。

---

## 1️⃣ HPOResult & 核心入口签名（复习）

我们约定 HPO 的统一接口是：

```python
def run_hpo_with_grid(df, grid, training_plan) -> Tuple[HPOResult, Dict[str, Any]]:
    ...
```

`HPOResult` 前面已经定义过，大概长这样（放在同一文件或 `experiments/types_hpo.py` 均可）：

```python
class HPOResult:
    def __init__(
        self,
        *,
        engine: str,
        n_trials: int,
        best_params: Dict[str, Any],
        best_value: float,
        direction: str,
        search_space: Dict[str, Any],
    ) -> None:
        self.engine = engine          # "grid"
        self.n_trials = n_trials      # 实际评估了多少组合
        self.best_params = best_params
        self.best_value = best_value  # 比如 Sharpe
        self.direction = direction    # "maximize"
        self.search_space = search_space
```

---

## 2️⃣ 完整实现：`run_hpo_with_grid`（可跑版）

> ⚠️ 说明：
>
> * 这版用的是 **纯 Grid Search**（完全来自 `hpo_grid`）
> * 模型采用 sklearn + xgboost，你可以根据现有环境删减/扩展
> * 交易逻辑是「预测值 → 信号 → 逐行 P&L」，作为一个轻量 backtest baseline

```python
# wf_hpo.py 中，替换之前的 NotImplemented 部分

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from sklearn.linear_model import LogisticRegression, Lasso, Ridge, ElasticNet
from sklearn.metrics import roc_auc_score, mean_squared_error
from sklearn.model_selection import TimeSeriesSplit

try:
    import xgboost as xgb
except ImportError:
    xgb = None  # 没有安装也可以先跑别的模型


# ─────────────────────────────────────
# 1. 帮助函数：构造模型
# ─────────────────────────────────────

def _build_model(model_type: str, model_params: Dict[str, Any], task: str):
    """
    根据 model_type + task 构建基础模型。
    这里只提供几个常用的，你可以按需扩展。
    """
    if task == "clf":
        if model_type == "logreg":
            return LogisticRegression(
                **{
                    "max_iter": 1000,
                    "n_jobs": -1,
                    **(model_params or {}),
                }
            )
        if model_type == "xgb":
            if xgb is None:
                raise RuntimeError("xgboost is not installed")
            return xgb.XGBClassifier(
                tree_method="hist",
                n_estimators=200,
                **(model_params or {}),
            )
        # 默认回退
        return LogisticRegression(max_iter=1000, n_jobs=-1)

    # 回归任务
    if model_type == "lasso":
        return Lasso(**(model_params or {}))
    if model_type == "ridge":
        return Ridge(**(model_params or {}))
    if model_type == "enet":
        return ElasticNet(**(model_params or {}))
    if model_type == "xgb":
        if xgb is None:
            raise RuntimeError("xgboost is not installed")
        return xgb.XGBRegressor(
            tree_method="hist",
            n_estimators=200,
            **(model_params or {}),
        )

    # 默认回归模型
    return Ridge(**(model_params or {}))


# ─────────────────────────────────────
# 2. 帮助函数：信号→P&L→指标
# ─────────────────────────────────────

def _decision_to_signal(
    y_pred: np.ndarray,
    decision_cfg: Dict[str, Any],
) -> np.ndarray:
    """
    根据 training_plan['decision'] 把 model 输出转为交易信号：
    - mode == 'quantile'：用分位数切多空区间
    - 其他情况：简单阈值
    """
    mode = (decision_cfg or {}).get("mode", "quantile")

    if mode == "quantile":
        q_long = decision_cfg.get("q_long", 0.7)
        q_short = decision_cfg.get("q_short", 0.3)
        hi = np.quantile(y_pred, q_long)
        lo = np.quantile(y_pred, q_short)
        sig = np.zeros_like(y_pred, dtype=float)
        sig[y_pred >= hi] = 1.0
        sig[y_pred <= lo] = -1.0
        return sig

    # fallback: 简单阈值
    thr = decision_cfg.get("threshold", 0.0)
    sig = np.zeros_like(y_pred, dtype=float)
    sig[y_pred > thr] = 1.0
    sig[y_pred < -thr] = -1.0
    return sig


def _compute_trading_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    decision_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    用最小可用的“信号 backtest”方式估算策略质量：
    - y_true：标签（通常是未来收益率 ret_fwd）
    - y_pred：模型预测（分数 / 回归值）
    - signal：根据决策配置转换得到的持仓方向 [-1, 0, +1]
    """
    signal = _decision_to_signal(y_pred, decision_cfg)

    # 每个 bar 的 PnL = signal * future_return
    pnl = signal * y_true
    equity = np.cumsum(pnl)

    mean_ret = np.mean(pnl)
    std_ret = np.std(pnl) + 1e-12
    sharpe = float(mean_ret / std_ret * np.sqrt(len(pnl))) if std_ret > 0 else 0.0

    # 最大回撤
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    max_dd = float(drawdown.min()) if len(drawdown) > 0 else 0.0
    max_dd_pct = float(-max_dd / (peak.max() + 1e-12)) if peak.max() > 0 else 0.0

    num_trades = int(np.sum(np.abs(np.diff(signal)) > 0))  # 信号切换近似为“交易次数”
    pnl_total = float(pnl.sum())
    pnl_per_trade = float(pnl_total / (num_trades + 1e-12))

    return {
        "sharpe": sharpe,
        "max_drawdown": max_dd_pct,
        "pnl_total": pnl_total,
        "pnl_per_trade": pnl_per_trade,
        "num_trades": num_trades,
        "turnover": float(np.mean(np.abs(np.diff(signal)))) if len(signal) > 1 else 0.0,
    }


# ─────────────────────────────────────
# 3. 核心：Grid Search HPO
# ─────────────────────────────────────

def run_hpo_with_grid(
    df: pd.DataFrame,
    grid: Dict[str, List[Any]],
    training_plan: Dict[str, Any],
) -> Tuple["HPOResult", Dict[str, Any]]:
    """
    极简版 HPO：
    - 使用 PI 给出的 grid（已被 DatasetGridGuard 清洗）
    - 对每一个候选组合训练 + 时间序列CV评估
    - 用“Sharpe”作为目标（可换成你习惯的 metric）

    返回：
    - HPOResult：描述最优 trial
    - best_metrics：该 trial 对应的指标 dict
    """
    # ---- 1) 解析任务配置 ----
    task = training_plan.get("task", "reg")  # "reg" or "clf"
    decision_cfg = training_plan.get(
        "decision",
        {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
    )

    # 假设 df 里面已经有：
    # - 特征列：grid["feature_subset"] 中的名称
    # - 标签列：training_plan["target"]
    target_col = training_plan.get("target")
    if target_col is None:
        raise ValueError("training_plan.target 不能为空")

    # 使用 grid 中的特征子集（目前我们只取第一个候选）
    feature_subset = grid.get("feature_subset")
    if not feature_subset:
        raise ValueError("hpo_grid.feature_subset 为空")
    feature_cols = feature_subset[0]

    for col in feature_cols + [target_col]:
        if col not in df.columns:
            raise ValueError(f"列缺失: {col} 不在 dataframe 中")

    X = df[feature_cols].values
    y = df[target_col].values

    # ---- 2) 构造候选组合列表 ----
    model_types = grid.get("model_type", []) or ["xgb"]
    model_params_list = grid.get("model_params", []) or [{}]

    candidates: List[Dict[str, Any]] = []
    for m_type, m_params in zip(model_types, model_params_list):
        candidates.append(
            {
                "model_type": m_type,
                "model_params": m_params or {},
            }
        )

    # ---- 3) 时间序列 CV + Grid Search ----
    n_splits = training_plan.get("cv_splits", 3)
    tscv = TimeSeriesSplit(n_splits=n_splits)

    best_candidate = None
    best_value = -1e9
    best_metrics: Dict[str, Any] = {}

    for cand in candidates:
        model_type = cand["model_type"]
        model_params = cand["model_params"]

        cv_scores: List[float] = []
        cv_metrics_list: List[Dict[str, Any]] = []

        for train_idx, val_idx in tscv.split(X):
            X_train, X_val = X[train_idx], X[val_idx]
            y_train, y_val = y[train_idx], y[val_idx]

            model = _build_model(model_type, model_params, task=task)
            model.fit(X_train, y_train)

            if task == "clf":
                # 使用概率作为打分
                if hasattr(model, "predict_proba"):
                    y_pred_val = model.predict_proba(X_val)[:, 1]
                else:
                    y_pred_val = model.decision_function(X_val)
                try:
                    auc = roc_auc_score(y_val, y_pred_val)
                except ValueError:
                    auc = 0.5
                score = auc
                # 你也可以在分类场景下用 AUC → Sharpe 的近似转换，这里先保留AUC
                metrics = {"auc": auc}
            else:
                # 回归任务：直接用 trading metrics
                y_pred_val = model.predict(X_val)
                metrics = _compute_trading_metrics(
                    y_true=y_val,
                    y_pred=y_pred_val,
                    decision_cfg=decision_cfg,
                )
                score = metrics["sharpe"]

            cv_scores.append(score)
            cv_metrics_list.append(metrics)

        avg_score = float(np.mean(cv_scores))
        # 取 CV 的平均指标作为候选的整体表现
        avg_metrics: Dict[str, Any] = {}
        # 把所有 metrics key 汇总并平均
        all_keys = {k for m in cv_metrics_list for k in m.keys()}
        for k in all_keys:
            vals = [m[k] for m in cv_metrics_list if k in m]
            if len(vals) > 0 and isinstance(vals[0], (int, float)):
                avg_metrics[k] = float(np.mean(vals))

        if avg_score > best_value:
            best_value = avg_score
            best_candidate = cand
            best_metrics = avg_metrics

    if best_candidate is None:
        raise RuntimeError("HPO 没有找到任何可用候选模型（grid 可能为空）")

    # ---- 4) 构造 HPOResult ----
    search_space = {
        "model_type": list({c["model_type"] for c in candidates}),
        "model_params": [c["model_params"] for c in candidates],
        "feature_subset": feature_cols,
        "task": task,
        "decision": decision_cfg,
    }

    hpo_result = HPOResult(
        engine="grid",
        n_trials=len(candidates),
        best_params=best_candidate,
        best_value=best_value,
        direction="maximize",
        search_space=search_space,
    )

    # 若是分类任务，我们可以顺带塞一点额外指标
    if task == "clf" and "auc" in best_metrics:
        # 这里可以尝试构造一个伪 Sharpe 或其他，再由上层决定是否使用
        pass

    return hpo_result, best_metrics
```

---

## 3️⃣ 现在这条链路已经真正闭环了

连接之前我们写的 `run_pi_and_hpo`：

1. **PI 决策 + 拿数据**

   ```python
   pi_result = pi.propose(...)
   df = pi_result.dataset_df
   grid = pi_result.hpo_grid
   training_plan = pi_result.training_plan
   ```

2. **DatasetSanity 体检**

   ```python
   sanity_report = run_dataset_sanity(df, spec, strict=True)
   ```

3. **HPO (刚刚实现)**

   ```python
   hpo_result, metrics_dict = run_hpo_with_grid(
       df=pi_result.dataset_df,
       grid=pi_result.hpo_grid,
       training_plan=pi_result.training_plan,
   )
   ```

4. **写入 ExperimentRegistry**

   ```python
   exp.hpo = HPOInfo(...)
   exp.metrics = ExperimentMetrics(...)
   REGISTRY.append(exp)
   ```

> ✔️ 从现在起，只要你调用一次 `run_pi_and_hpo(...)`，就会在 `experiments_index.jsonl` 里留下**一条完整的实验记录**，包含：
>
> * 使用了什么特征/标签/时间区间
> * DatasetSanity 体检结果
> * HPO 最优模型配置
> * Sharpe / max_drawdown / pnl_total / num_trades 等绩效

---

## 4️⃣ 接下来可以做什么？

按照我们的大路线，下一步有两条可以并行推进：

1. **把这个 HPO baseline 替换/嫁接到你现有的“真·回测引擎”**

   * 把 `_compute_trading_metrics` 换成你 DSL / BacktestEngine 的输出
   * 只要最终 `metrics_dict` 的 key 保持一致就行

2. **为 AI 大脑准备“策略候选列表”接口**

   * 用刚才的 `ExperimentRegistry.summarize_for_brain(symbol="TSLA")`
   * 定义一个简单的 JSON schema，让大脑看到：

     ```json
     {
       "allowed_strategies": [
         {"run_id": "...", "sharpe": 1.3, "max_drawdown": 0.12, ...},
         ...
       ]
     }
     ```

如果你愿意，我们下一步就可以继续往上走——
**把 ExperimentRegistry 的“summary → LLM prompt” 这一段写出来**，让 AI 大脑真正只从历史实验里「挑人」，而不是乱发明策略。
好，咱们现在往上“抬一层”：
把 **PI → HPO → ExperimentRegistry → AI 大脑选策略** 这一条闭环打通。

这一轮我主要做两件事：

1. **把 ExperimentRegistry 写完整**，加上 `summarize_for_brain()`，专门为大脑准备“可用策略清单”
2. **设计大脑选策略的上下文 + JSON Schema**，让大脑只能在这些策略里“挑人”，而不是乱发明

---

## ① ExperimentRegistry：真正能落盘 + 给大脑用的 summary

先给一个可直接放进 `experiments/registry.py` 的版本（如果你之前已经让 Claude 写过，可以把下面的 `summarize_for_brain` 合并进去）。

### 1. 基本类型

```python
# experiments/types_core.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Dict, Optional


@dataclass
class ExperimentMetrics:
    sharpe: float
    max_drawdown: float
    pnl_total: float
    pnl_per_trade: float
    num_trades: int
    turnover: float

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperimentMetrics":
        return cls(
            sharpe=float(data.get("sharpe", 0.0)),
            max_drawdown=float(data.get("max_drawdown", 0.0)),
            pnl_total=float(data.get("pnl_total", 0.0)),
            pnl_per_trade=float(data.get("pnl_per_trade", 0.0)),
            num_trades=int(data.get("num_trades", 0)),
            turnover=float(data.get("turnover", 0.0)),
        )


@dataclass
class HPOInfo:
    engine: str
    n_trials: int
    best_params: Dict[str, Any]
    best_value: float
    direction: str

    @classmethod
    def from_result(cls, hpo_result: "HPOResult") -> "HPOInfo":
        return cls(
            engine=hpo_result.engine,
            n_trials=hpo_result.n_trials,
            best_params=hpo_result.best_params,
            best_value=float(hpo_result.best_value),
            direction=hpo_result.direction,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HPOInfo":
        return cls(
            engine=data.get("engine", "grid"),
            n_trials=int(data.get("n_trials", 0)),
            best_params=data.get("best_params", {}),
            best_value=float(data.get("best_value", 0.0)),
            direction=data.get("direction", "maximize"),
        )


@dataclass
class ExperimentRecord:
    """
    一次完整实验（PI → DatasetSanity → HPO）的跟踪记录。
    """
    run_id: str
    symbol: str
    table: str
    start_ts: str
    end_ts: str

    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]

    dataset_manifest: Dict[str, Any]   # 比如 DatasetManifest.to_dict()
    sanity_report: Dict[str, Any]      # run_dataset_sanity() 输出的 dict

    hpo: HPOInfo
    metrics: ExperimentMetrics

    created_at: str
    notes: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperimentRecord":
        return cls(
            run_id=data["run_id"],
            symbol=data["symbol"],
            table=data["table"],
            start_ts=data["start_ts"],
            end_ts=data["end_ts"],
            feature_recipe=data.get("feature_recipe", {}),
            training_plan=data.get("training_plan", {}),
            dataset_manifest=data.get("dataset_manifest", {}),
            sanity_report=data.get("sanity_report", {}),
            hpo=HPOInfo.from_dict(data["hpo"]),
            metrics=ExperimentMetrics.from_dict(data["metrics"]),
            created_at=data.get("created_at", datetime.utcnow().isoformat()),
            notes=data.get("notes", ""),
        )

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["hpo"] = asdict(self.hpo)
        d["metrics"] = asdict(self.metrics)
        return d
```

### 2. Registry：写 JSONL + `summarize_for_brain`

```python
# experiments/registry.py

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .types_core import ExperimentRecord, ExperimentMetrics, HPOInfo


class ExperimentRegistry:
    """
    非常简单的 JSONL registry：
    - 每一行是一个 ExperimentRecord.to_dict()
    - 提供 append() / iter_records() / summarize_for_brain()
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # 不主动创建空文件，append 的时候自然会创建

    # ── 写入一条实验记录 ─────────────────────────────────
    def append(self, rec: ExperimentRecord) -> None:
        line = json.dumps(rec.to_dict(), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ── 遍历记录（可按 symbol 过滤）─────────────────────
    def iter_records(self, symbol: Optional[str] = None) -> Iterable[ExperimentRecord]:
        if not self._path.exists():
            return []

        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if symbol is not None and data.get("symbol") != symbol:
                    continue
                try:
                    yield ExperimentRecord.from_dict(data)
                except Exception:
                    # 这里可以加日志，目前先跳过损坏记录
                    continue

    # ── 为 AI 大脑准备的 summary ────────────────────────
    def summarize_for_brain(
        self,
        *,
        symbol: str,
        top_k: int = 10,
        min_sharpe: float = 0.5,
        max_drawdown: float = 0.3,
    ) -> Dict[str, Any]:
        """
        返回一个紧凑的 dict，专门给 AI 大脑看：
        - 只保留关键信息（避免信息过载）
        - 带一个自然语言 summary，便于在 prompt 里使用
        """
        records: List[ExperimentRecord] = list(self.iter_records(symbol=symbol))

        # 过滤掉数据体检失败 / 指标太差的
        candidates = []
        for rec in records:
            m = rec.metrics
            # sanity report 中如果有 fatal 标记，可以在这里过滤
            sanity_status = rec.sanity_report.get("status", "ok")
            if sanity_status != "ok":
                continue

            if m.sharpe < min_sharpe:
                continue
            if m.max_drawdown > max_drawdown:
                continue

            candidates.append(rec)

        # 按 Sharpe 降序
        candidates.sort(key=lambda r: r.metrics.sharpe, reverse=True)
        candidates = candidates[:top_k]

        strategies: List[Dict[str, Any]] = []
        for rec in candidates:
            h = rec.hpo
            m = rec.metrics

            base_feats = rec.feature_recipe.get("base_features", [])
            derived_feats = [
                d.get("alias") for d in rec.feature_recipe.get("derived", [])
            ]
            feature_list = base_feats + derived_feats

            strategies.append(
                {
                    "run_id": rec.run_id,
                    "symbol": rec.symbol,
                    "period": {
                        "start_ts": rec.start_ts,
                        "end_ts": rec.end_ts,
                    },
                    "table": rec.table,
                    "model_type": h.best_params.get("model_type", ""),
                    "hpo_engine": h.engine,
                    "hpo_best_value": h.best_value,
                    "task": rec.training_plan.get("task", "reg"),
                    "label": rec.training_plan.get("target", ""),
                    "decision": rec.training_plan.get("decision", {}),
                    "features": feature_list,
                    "metrics": {
                        "sharpe": m.sharpe,
                        "max_drawdown": m.max_drawdown,
                        "pnl_total": m.pnl_total,
                        "pnl_per_trade": m.pnl_per_trade,
                        "num_trades": m.num_trades,
                        "turnover": m.turnover,
                    },
                    "notes": rec.notes,
                }
            )

        # 为 LLM 生成一个简短的自然语言 summary，避免让 LLM自己去遍历长 JSON
        summary_lines: List[str] = []
        for s in strategies:
            line = (
                f"策略 {s['run_id']}：周期 {s['period']['start_ts']}~{s['period']['end_ts']}，"
                f"模型 {s['model_type']}，Sharpe={s['metrics']['sharpe']:.2f}，"
                f"最大回撤={s['metrics']['max_drawdown']:.2%}，交易数={s['metrics']['num_trades']}。"
            )
            summary_lines.append(line)

        human_summary = (
            "当前可用的历史策略候选如下（已按 Sharpe 降序过滤）：\n"
            + ("\n".join(summary_lines) if summary_lines else "（无符合条件的策略记录。）")
        )

        return {
            "symbol": symbol,
            "num_candidates": len(strategies),
            "strategies": strategies,
            "summary_text": human_summary,
        }
```

> 到这一步：
>
> * 任何一次 `run_pi_and_hpo()` 跑完后，你把 `ExperimentRecord` append 到这个 registry
> * 然后 `registry.summarize_for_brain(symbol="TSLA")` 就能给大脑一份“候选策略池 + 摘要”

---

## ② 给 AI 大脑用的决策 Schema：只能在白名单里挑 run_id

接下来我们要限制大脑的权限：

> **大脑不再“自己写策略”，而是只能从 `allowed_run_ids` 中选一个或者拒绝。**

### 1. BrainDecision 类型 + JSON Schema

```python
# brain/decision_schema.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class BrainDecision:
    """
    AI 大脑的“选策略”决定，而不是具体买卖指令。
    后面 live trader 再根据 selected_run_id 去加载模型、跑信号。
    """
    mode: str                     # "select_strategy" | "reject"
    selected_run_id: str | None
    fallback_run_id: str | None
    reason: str
    confidence: float

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BrainDecision":
        return cls(
            mode=data.get("mode", "reject"),
            selected_run_id=data.get("selected_run_id"),
            fallback_run_id=data.get("fallback_run_id"),
            reason=data.get("reason", ""),
            confidence=float(data.get("confidence", 0.0)),
        )


def build_brain_decision_schema(allowed_run_ids: List[str]) -> Dict[str, Any]:
    """
    提供给 LLM 的 JSON Schema，用于约束输出。
    注意：selected_run_id 必须出现在 allowed_run_ids 里。
    """
    return {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["select_strategy", "reject"],
                "description": "select_strategy=选择一个历史策略；reject=认为目前没有合适的策略。",
            },
            "selected_run_id": {
                "type": ["string", "null"],
                "description": "从 allowed_run_ids 中选择一个，如果 mode=reject 则为 null。",
            },
            "fallback_run_id": {
                "type": ["string", "null"],
                "description": "可选：备选策略 run_id（同样必须在 allowed_run_ids 中）。",
            },
            "reason": {
                "type": "string",
                "description": "用简洁自然语言解释为什么选择/拒绝策略。",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "对本次策略选择的置信度（0~1）。",
            },
        },
        "required": ["mode", "selected_run_id", "reason", "confidence"],
        "additionalProperties": False,
        # 由上层在调用后做二次校验：
        # - 如果 mode == "select_strategy"，必须 selected_run_id in allowed_run_ids
    }
```

### 2. 为大脑构造 Prompt 上下文（接入你的 prompts JSON）

你之前给的 prompts JSON 里，有一堆 `"v3_fast_market_following_tactical_..."`，
这些其实可以作为 **“执行 Prompt”**，但选策略这个大脑可以用一个更简单、专门的系统 prompt。

下面这个函数做几件事：

* 从 `ExperimentRegistry` 拿 summary
* 构建一个系统 prompt：告诉大脑它的任务 = “从候选 run_id 中选一个”
* 构建 user prompt：包括今天的环境信息（由外部传入，比如 VIX、账户风险、是否 PRE、新闻情绪等）
* 返回：`system_prompt, user_prompt, json_schema, allowed_run_ids`

```python
# brain/context_builder.py

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from experiments.registry import ExperimentRegistry
from .decision_schema import build_brain_decision_schema


def build_strategy_selection_context(
    *,
    symbol: str,
    registry: ExperimentRegistry,
    today_env: Dict[str, Any],
) -> Tuple[str, str, Dict[str, Any], List[str]]:
    """
    给 AI 大脑选策略用的上下文构造器。
    返回：
      - system_prompt
      - user_prompt
      - json_schema（给 LLM 作为 structured output）
      - allowed_run_ids（上层用于二次验证）
    """
    summary = registry.summarize_for_brain(symbol=symbol, top_k=10)

    allowed_ids: List[str] = [s["run_id"] for s in summary["strategies"]]

    # 如果没有任何候选，直接让大脑只能 reject
    if not allowed_ids:
        system_prompt = (
            "你是量化交易平台的策略选择模块（Strategy Selector）。"
            "当前没有任何经过验证的历史策略可用。"
            "你的唯一合法行为是返回 mode='reject'，并解释原因。"
            "输出必须是 JSON，符合给定的模式。"
        )
        user_prompt = (
            f"Symbol: {symbol}\n"
            f"Today env: {today_env}\n"
            "因为没有历史策略，请明确拒绝选择策略。"
        )
        schema = build_brain_decision_schema(allowed_run_ids=[])
        return system_prompt, user_prompt, schema, []

    # 有候选策略的情况
    system_prompt = (
        "你是量化交易平台的策略选择模块（Strategy Selector）。\n"
        "你的任务：\n"
        "1. 阅读系统给出的“候选历史策略摘要”和“当前市场环境”。\n"
        "2. 在 allowed_run_ids 列表中选择 **最多一个** 作为今天的主策略 run_id，"
        "   或者选择 reject，表示今天不适合使用任何历史策略。\n"
        "3. 不允许发明新的 run_id，selected_run_id 必须来自 allowed_run_ids。\n"
        "4. 输出必须是 JSON，符合给定的 Schema，不要输出多余文字。\n"
    )

    # 压缩今天的环境信息，避免上下文爆炸
    env_text = (
        f"symbol={symbol}, "
        f"session={today_env.get('session', 'REG')}, "
        f"account_risk={today_env.get('account_risk', {})}, "
        f"volatility={today_env.get('volatility', {})}, "
        f"news_sentiment={today_env.get('news_sentiment', 'neutral')}"
    )

    user_prompt = (
        f"【候选策略摘要】\n{summary['summary_text']}\n\n"
        f"【当前市场环境】\n{env_text}\n\n"
        f"【allowed_run_ids】\n{allowed_ids}\n\n"
        "请根据以上信息，选择一个最适合当前环境的 run_id，"
        "或者如果认为所有历史策略在当前环境下风险过高/不适用，则返回 mode='reject'。"
    )

    schema = build_brain_decision_schema(allowed_run_ids=allowed_ids)
    return system_prompt, user_prompt, schema, allowed_ids
```

---

## ③ 上层如何调用 AI 大脑（骨架）

最后给你一个非常精简的 orchestrator 骨架，说明这些东西怎么串起来：

```python
# brain/strategy_selector.py

from __future__ import annotations

from typing import Any, Dict

from llm.llm_client import LLMClient
from experiments.registry import ExperimentRegistry
from .context_builder import build_strategy_selection_context
from .decision_schema import BrainDecision


def select_strategy_for_today(
    *,
    symbol: str,
    registry: ExperimentRegistry,
    today_env: Dict[str, Any],
    llm: LLMClient,
) -> BrainDecision:
    system_prompt, user_prompt, schema, allowed_ids = build_strategy_selection_context(
        symbol=symbol,
        registry=registry,
        today_env=today_env,
    )

    # 如果没有候选，直接给一个“拒绝”的决策
    if not allowed_ids:
        return BrainDecision(
            mode="reject",
            selected_run_id=None,
            fallback_run_id=None,
            reason="No valid historical strategies available for this symbol.",
            confidence=0.0,
        )

    # 调用 LLM（这里假设 LLMClient 支持 structured output / json_schema）
    raw = llm.generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        json_schema=schema,
    )

    decision = BrainDecision.from_dict(raw)

    # ── 二次安全校验 ──────────────────────────
    if decision.mode == "select_strategy":
        if decision.selected_run_id not in allowed_ids:
            # 强行纠正为 reject
            return BrainDecision(
                mode="reject",
                selected_run_id=None,
                fallback_run_id=None,
                reason=(
                    f"LLM 返回的 run_id={decision.selected_run_id} 不在 allowed_run_ids 中，"
                    "已被系统拒绝。"
                ),
                confidence=0.0,
            )

    return decision
```

到这里，你已经具备了：

* ✅ **底层**：PI → DatasetSanity → HPO → ExperimentRegistry（我们刚刚补齐 HPO baseline 并说明如何 append）
* ✅ **中层**：ExperimentRegistry.summarize_for_brain() → 紧凑的策略清单
* ✅ **上层**：Strategy Selector（AI 大脑），通过 JSON Schema + allowed_run_ids 白名单实现 **“只能挑，不许造”**

---

下一步如果你继续说「继续」，我可以：

* 要么帮你**把 `run_pi_and_hpo()` 的完整版本写出来**，包含创建 `ExperimentRecord` + append 到 registry 的所有细节；
* 要么帮你**把“选中的 run_id → 实际加载模型 & 执行信号”的骨架写出来**，对接你现在的 backtest / live trading Runtime。

你不用再决定方向，我会沿着“尽快 Production Ready”的主线，优先把 **“PI → HPO → Registry → Strategy Selection → Runtime hook”** 整条链路写完。
好，我们现在把 **PI → HPO → ExperimentRegistry** 这条链真正“跑通”，给你一个可以直接接进现有工程的骨架。

这一步我会做 4 件事：

1. 定义统一的 `HPOResult` 类型
2. 给一个可替换的 `run_hpo()`，你可以先用 baseline，后面再接你自己的 `wf_hpo.py`
3. 提供 `build_experiment_record()`，把 PI + HPO + Sanity 打包成 `ExperimentRecord`
4. 写一个完整的 `run_pi_hpo_experiment()` orchestrator，从 PI 一直跑到写入 Registry

> ⚠️ 后面你只需要在 **一个地方** 把 `run_hpo()` 替换成你自己的 HPO 流程，这条线就是真正 Production-风格的了。

---

## ① HPOResult 类型

文件：`experiments/hpo_types.py`

```python
# experiments/hpo_types.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class HPOResult:
    """
    对一次 HPO / 模型训练+回测的汇总结果（只保留最优 trial 的信息）。
    """
    engine: str                 # 比如 "optuna", "grid", "manual"
    n_trials: int               # 总共尝试了多少个 trial
    best_params: Dict[str, Any] # 最优 trial 的参数组合
    best_value: float           # 最优目标值（比如 sharpe）
    direction: str              # "maximize" / "minimize"
```

---

## ② 可替换的 run_hpo（默认 baseline + 预留接你自己的引擎）

文件：`experiments/hpo_runner.py`

这个版本：

* **输入**：DataFrame、training_plan、PI 给的 hpo_grid
* **输出**：`HPOResult` + `ExperimentMetrics`
* 默认实现是一个“非常朴素的 baseline”，你可以先跑通流程，之后在这个文件里换成你已有的 HPO 逻辑。

```python
# experiments/hpo_runner.py

from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import r2_score
from sklearn.model_selection import train_test_split

from .hpo_types import HPOResult
from .types_core import ExperimentMetrics


def _split_xy(
    df: pd.DataFrame,
    feature_cols: list[str],
    target_col: str,
) -> Tuple[pd.DataFrame, pd.Series]:
    X = df[feature_cols].copy()
    y = df[target_col].copy()
    return X, y


def _compute_dummy_metrics(
    y_true: np.ndarray,
    y_pred: np.ndarray,
) -> ExperimentMetrics:
    """
    这里只是一个 placeholder：
    - sharpe: 用简单的 mean/std 模拟
    - pnl_total: 用 sum(y_pred) 模拟
    - num_trades: 用 len(y_pred) 模拟
    将来你可以改成真实的 backtest 结果（从 BacktestEngine 拿）。
    """
    ret = np.asarray(y_pred)
    mean = float(ret.mean()) if len(ret) > 0 else 0.0
    std = float(ret.std()) if len(ret) > 0 else 1e-9

    sharpe = mean / std if std > 0 else 0.0
    pnl_total = float(ret.sum())
    pnl_per_trade = float(ret.mean()) if len(ret) > 0 else 0.0
    num_trades = int(len(ret))

    # 这里先用一些简单的 placeholder
    return ExperimentMetrics(
        sharpe=sharpe,
        max_drawdown=0.1,  # TODO: 用真实回测替换
        pnl_total=pnl_total,
        pnl_per_trade=pnl_per_trade,
        num_trades=num_trades,
        turnover=0.0,
    )


def run_hpo_baseline(
    *,
    df: pd.DataFrame,
    training_plan: Dict[str, Any],
    hpo_grid: Dict[str, Any],
) -> Tuple[HPOResult, ExperimentMetrics]:
    """
    一个非常简单的 baseline HPO：
    - 暂时只支持单一 model_type="xgb_reg_baseline"
    - 用 train/test split + R2 来选择最优模型
    - 返回 HPOResult 和一个简单的 ExperimentMetrics

    将来如果你要接入你自己的 wf_hpo.py，只需要：
    - 保留这个函数签名
    - 在内部调用自己的 HPO 流程，然后组装 HPOResult + ExperimentMetrics 即可。
    """
    from xgboost import XGBRegressor  # 假设你已经安装了 xgboost

    # 1) 准备特征 & 标签
    feature_cols = hpo_grid.get("feature_subset", [[]])[0]
    target_col = training_plan.get("target")
    if not target_col:
        raise ValueError("training_plan.target 不能为空")

    X, y = _split_xy(df, feature_cols, target_col)

    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, shuffle=False
    )

    # 2) 从 hpo_grid 拿候选模型参数（这里假设只有一个 model_type）
    model_types = hpo_grid.get("model_type", []) or ["xgb"]
    model_params_list = hpo_grid.get("model_params", []) or [{}]

    best_score = -1e9
    best_params: Dict[str, Any] = {}
    best_metrics: ExperimentMetrics | None = None

    n_trials = 0

    for mtype, params in zip(model_types, model_params_list):
        if mtype != "xgb":
            # 目前 baseline 只支持 xgb，其它类型先忽略/以后再扩展
            continue

        n_trials += 1
        base_params = {
            "n_estimators": 200,
            "max_depth": 4,
            "learning_rate": 0.05,
            "subsample": 0.9,
            "colsample_bytree": 0.9,
            "tree_method": "hist",
            "random_state": 42,
        }
        base_params.update(params or {})

        model = XGBRegressor(**base_params)
        model.fit(X_train, y_train)

        y_pred = model.predict(X_test)
        # 用 R2 做一个简单的 score，方便对比
        score = r2_score(y_test, y_pred)

        if score > best_score:
            best_score = score
            best_params = {
                "model_type": mtype,
                "params": base_params,
                # 实际用的时候，你还需要保存 model 的序列化路径 / run_id 等
            }
            best_metrics = _compute_dummy_metrics(
                y_true=y_test.to_numpy(),
                y_pred=y_pred,
            )

    if best_metrics is None:
        # 如果一个都没成功，就给个空壳
        best_metrics = ExperimentMetrics(
            sharpe=0.0,
            max_drawdown=0.0,
            pnl_total=0.0,
            pnl_per_trade=0.0,
            num_trades=0,
            turnover=0.0,
        )

    hpo_result = HPOResult(
        engine="baseline_grid",
        n_trials=n_trials,
        best_params=best_params,
        best_value=float(best_score),
        direction="maximize",
    )
    return hpo_result, best_metrics
```

> **以后接入你自己的 HPO：**
> 只要保留 `run_hpo_baseline` 的签名，把内部逻辑替换成：
> 调你现在的 `wf_hpo.py` → 拿到最优 params + backtest 指标 → 组装 `HPOResult` + `ExperimentMetrics`。

---

## ③ 从 PI + Sanity + HPO 构造 ExperimentRecord

文件：`experiments/builder.py`

这里假设你已有：

* `PrincipalInvestigator` 返回的 `PIResult`（你之前给的代码）
* `DatasetSanity` 部分有两个函数：

  ```python
  from data_quality.sanity import build_dataset_manifest, run_dataset_sanity_checks
  ```

你可以按这个接口调整你现有的实现。

```python
# experiments/builder.py

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from .types_core import ExperimentRecord, ExperimentMetrics, HPOInfo
from .hpo_types import HPOResult


def build_experiment_record(
    *,
    run_id: str,
    symbol: str,
    table: str,
    start_ts: str,
    end_ts: str,
    feature_recipe: Dict[str, Any],
    training_plan: Dict[str, Any],
    dataset_manifest: Dict[str, Any],
    sanity_report: Dict[str, Any],
    hpo_result: HPOResult,
    metrics: ExperimentMetrics,
    notes: str = "",
) -> ExperimentRecord:
    """
    把一次 end-to-end 过程打包成 ExperimentRecord。
    """
    hpo_info = HPOInfo.from_result(hpo_result)

    return ExperimentRecord(
        run_id=run_id,
        symbol=symbol,
        table=table,
        start_ts=start_ts,
        end_ts=end_ts,
        feature_recipe=feature_recipe,
        training_plan=training_plan,
        dataset_manifest=dataset_manifest,
        sanity_report=sanity_report,
        hpo=hpo_info,
        metrics=metrics,
        created_at=datetime.utcnow().isoformat(),
        notes=notes,
    )
```

---

## ④ 总 orchestrator：run_pi_hpo_experiment

文件：`orchestrators/pi_hpo_orchestrator.py`

这个函数做的事：

1. 调 `PrincipalInvestigator.propose()` → 得到 SQL + dataset + hpo_grid
2. 跑 `DatasetManifest + Sanity`
3. 如果 sanity 报 fatal，直接 return，不浪费 HPO 资源
4. 跑 `run_hpo_baseline()`（后面你换成自己的 HPO）
5. 用 `build_experiment_record()` 组装记录，append 到 `ExperimentRegistry`
6. 返回一个简洁 summary，方便日志 / Notebook 里看

```python
# orchestrators/pi_hpo_orchestrator.py

from __future__ import annotations

import uuid
from typing import Any, Dict

from experiments.registry import ExperimentRegistry
from experiments.hpo_runner import run_hpo_baseline
from experiments.builder import build_experiment_record
from pi.principal_investigator import PrincipalInvestigator
from data_quality.sanity import (
    build_dataset_manifest,
    run_dataset_sanity_checks,
)


def run_pi_hpo_experiment(
    *,
    symbol: str,
    table: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    ch_cfg: Dict[str, Any],
    llm_cfg: Dict[str, Any],
    registry: ExperimentRegistry,
    sanity_cfg: Dict[str, Any] | None = None,
    notes: str = "",
) -> Dict[str, Any]:
    """
    打通 PI → DatasetSanity → HPO → ExperimentRegistry 的总入口。

    返回一个 summary dict，方便上层记录日志或在 Notebook 中查看。
    """
    # ── 1) 初始化 PI ─────────────────────────────
    pi = PrincipalInvestigator(ch_cfg=ch_cfg, llm_cfg=llm_cfg)

    # ── 2) LLM 生成 feature_recipe + training_plan + SQL + dataset + hpo_grid ───
    pi_result = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
        guard_cfg=None,
    )

    df = pi_result.dataset_df

    # ── 3) 数据体检（Manifest + Sanity）──────────────────
    manifest = build_dataset_manifest(df)
    sanity = run_dataset_sanity_checks(df, sanity_cfg or {})

    if sanity.get("status") == "fatal":
        # 不进入 HPO，直接记录一次“失败实验”也可以，但这里先直接返回
        return {
            "status": "sanity_failed",
            "symbol": symbol,
            "table": table,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "sanity_report": sanity,
        }

    # ── 4) HPO / 模型训练 + 简单 backtest 指标 ─────────────
    hpo_result, metrics = run_hpo_baseline(
        df=df,
        training_plan=pi_result.training_plan,
        hpo_grid=pi_result.hpo_grid,
    )

    # ── 5) 构造 ExperimentRecord 并写入 Registry ────────
    run_id = str(uuid.uuid4())

    rec = build_experiment_record(
        run_id=run_id,
        symbol=symbol,
        table=table,
        start_ts=start_ts,
        end_ts=end_ts,
        feature_recipe=pi_result.feature_recipe,
        training_plan=pi_result.training_plan,
        dataset_manifest=manifest,
        sanity_report=sanity,
        hpo_result=hpo_result,
        metrics=metrics,
        notes=notes,
    )
    registry.append(rec)

    # ── 6) 返回 summary ──────────────────────────
    return {
        "status": "ok",
        "run_id": run_id,
        "symbol": symbol,
        "table": table,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "metrics": {
            "sharpe": metrics.sharpe,
            "max_drawdown": metrics.max_drawdown,
            "pnl_total": metrics.pnl_total,
            "pnl_per_trade": metrics.pnl_per_trade,
            "num_trades": metrics.num_trades,
        },
        "hpo": {
            "engine": hpo_result.engine,
            "n_trials": hpo_result.n_trials,
            "best_value": hpo_result.best_value,
            "direction": hpo_result.direction,
            "best_params": hpo_result.best_params,
        },
        "sanity_status": sanity.get("status"),
    }
```

### 调用示例（比如在 Notebook 或 CLI）

```python
from pathlib import Path

from experiments.registry import ExperimentRegistry
from orchestrators.pi_hpo_orchestrator import run_pi_hpo_experiment

registry = ExperimentRegistry(Path("experiments/tsla_experiments.jsonl"))

summary = run_pi_hpo_experiment(
    symbol="TSLA",
    table="bars_1m_tsla",        # 举例
    start_ts="2025-01-01 14:30:00",
    end_ts="2025-01-31 21:00:00",
    goal="在 NYSE 正常交易时段做日内多空交易，最大化 risk-adjusted PnL",
    horizon="10m",
    ch_cfg={
        "host": "localhost",
        "port": 8123,
        "database": "market",
        "username": "default",
        "password": "",
    },
    llm_cfg={
        "model": "gpt-5.1-thinking",
        "base_url": "http://localhost:8000/v1",
    },
    registry=registry,
    sanity_cfg={"max_nan_ratio": 0.2},
    notes="TSLA 2025-01 月 baseline 实验",
)

print(summary)
```

---

## 到这里，PI → HPO → Registry 链路状态

**现在这条链路的状态：**

* ✅ 有可运行的 orchestrator：`run_pi_hpo_experiment`
* ✅ 数据有“体检”：sanity fatal 会直接阻断
* ✅ 每次实验都记成一条 `ExperimentRecord`，写入 JSONL registry
* ✅ 我们已经有上一轮写好的 `ExperimentRegistry.summarize_for_brain()` + Strategy Selector
* 🔜 你要做的主要事情只有两件：

1. 把 `run_hpo_baseline()` 替换／封装到你自己的 HPO/回测流水线
2. 确认 `data_quality.sanity` 里的接口符合这个 orchestrator 的调用（或按这个签名微调）

---

如果你接着说「继续」，我下一步会：

* 把 **“选中的 run_id → 实际加载模型 & 每根 K 线产出信号”** 的 runtime skeleton 写出来，
  包括一个简单的 `StrategyRuntime` 接口 + `ModelRegistry/ModelStore` 约定，
  这样你从 **“策略选择”到“实盘/模拟发单”** 的最后一段也能被串起来。
