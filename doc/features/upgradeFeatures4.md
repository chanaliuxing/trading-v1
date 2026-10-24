好，继续把**“实验结果 → 指数 → 给大脑看的摘要”**这一层落地。

这一步做完之后，效果是：

* 每次 HPO / 回测结束，都会**自动写入一条实验记录**（Sharpe、IC、最大回撤、使用的特征、label、模型参数……）。
* 这些记录会集中存成一个 **experiment index（JSONL 文件）**。
* 执行大脑在做交易决策前，可以通过一个函数拿到：

  > 「最近 NVDA-1m-10m 这一档最好的 3 个实验表现、Sharpe 区间、回撤风险、样本交易数」
  > 然后被塞进我们上一步的 `build_brain_prompts(..., experiment_summary=...)`。

---

## 一、定义实验记录与指标类型

新文件：`experiments/types.py`

```python
# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional


@dataclass
class Metrics:
    """
    一个回测/实验的核心量化指标。
    没有的字段可以用 None，保持 schema 稳定。
    """
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    ic: Optional[float] = None          # 信息系数
    hit_rate: Optional[float] = None    # 胜率
    max_drawdown: Optional[float] = None
    pnl: Optional[float] = None         # 总收益（可以是绝对或百分比）
    pnl_annualized: Optional[float] = None
    trades: Optional[int] = None
    avg_trade_pnl: Optional[float] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Metrics":
        # 安全地从任意 dict 构建，不存在的字段用默认
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__.keys()})

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ExperimentRecord:
    """
    一次完整实验的“索引”记录，用于写入 JSONL。

    - 一个 run_id 对应一次训练+回测 / 或一次 HPO trial 的代表结果。
    - manifest_path 指向更详细的 JSON manifest。
    """
    run_id: str
    symbol: str
    freq: str             # e.g. "1m", "5s"
    horizon: str          # e.g. "10m"
    label_id: str
    model_type: str
    feature_subset: List[str]
    params: Dict[str, Any]
    metrics: Metrics
    run_ts: str           # ISO8601 时间戳
    data_start_ts: Optional[str] = None
    data_end_ts: Optional[str] = None
    prompt_id: Optional[str] = None     # 用了哪个执行 prompt（可选）
    manifest_path: Optional[str] = None
    tags: Optional[List[str]] = None    # 例如 ["pre-market", "trend", "fast"]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["metrics"] = self.metrics.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperimentRecord":
        m = Metrics.from_dict(d.get("metrics", {}))
        d2 = {**d}
        d2["metrics"] = m
        # feature_subset / tags 可能被序列化成非 list，做一次兜底
        if isinstance(d2.get("feature_subset"), str):
            # 简单 split，真实环境可以按需要解析
            d2["feature_subset"] = [d2["feature_subset"]]
        if isinstance(d2.get("tags"), str):
            d2["tags"] = [d2["tags"]]
        return cls(**d2)
```

---

## 二、实验索引：追加、加载、筛选 Top-N

新文件：`experiments/registry.py`

```python
# experiments/registry.py

from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional

import json

from experiments.types import ExperimentRecord


DEFAULT_INDEX_PATH = Path("experiments/experiment_index.jsonl")


def append_experiment_record(
    record: ExperimentRecord,
    index_path: Path = DEFAULT_INDEX_PATH,
) -> None:
    """
    以 JSONL 形式将实验记录 append 到文件末尾。
    - 不需要一次性加载全部历史（避免随着历史增长变慢）。
    """
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")


def load_experiment_records(
    index_path: Path = DEFAULT_INDEX_PATH,
    *,
    symbol: Optional[str] = None,
    freq: Optional[str] = None,
    horizon: Optional[str] = None,
    label_id: Optional[str] = None,
) -> List[ExperimentRecord]:
    """
    从 JSONL index 里按条件加载所有匹配的记录。
    """
    if not index_path.exists():
        return []

    records: List[ExperimentRecord] = []
    with index_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec = ExperimentRecord.from_dict(raw)

            if symbol and rec.symbol != symbol:
                continue
            if freq and rec.freq != freq:
                continue
            if horizon and rec.horizon != horizon:
                continue
            if label_id and rec.label_id != label_id:
                continue

            records.append(rec)
    return records


def _safe_metric(rec: ExperimentRecord, metric: str, default: float = float("nan")) -> float:
    v = getattr(rec.metrics, metric, None)
    if v is None:
        return default
    return float(v)


def top_n_by_metric(
    records: Iterable[ExperimentRecord],
    metric: str,
    n: int = 3,
    *,
    reverse: bool = True,
) -> List[ExperimentRecord]:
    """
    根据某个 metric 排序，返回 top-n。
    默认 reverse=True 表示越大越好（如 sharpe / ic / pnl）。
    """
    rec_list = list(records)
    rec_list.sort(key=lambda r: _safe_metric(r, metric, default=float("-inf")), reverse=reverse)
    return rec_list[:n]
```

---

## 三、给 LLM 的“实验摘要”：压缩成一小段结构化信息

新文件：`experiments/summary.py`

```python
# experiments/summary.py

from __future__ import annotations

from typing import Any, Dict, List, Optional

import math

from experiments.registry import (
    load_experiment_records,
    top_n_by_metric,
    DEFAULT_INDEX_PATH,
)
from experiments.types import ExperimentRecord


def _metric_range(records: List[ExperimentRecord], field: str) -> Optional[Dict[str, float]]:
    vals = []
    for r in records:
        v = getattr(r.metrics, field, None)
        if v is not None and not math.isnan(float(v)):
            vals.append(float(v))
    if not vals:
        return None
    return {"min": min(vals), "max": max(vals)}


def build_experiment_summary_for_llm(
    *,
    symbol: str,
    freq: str,
    horizon: str,
    label_id: str,
    index_path=DEFAULT_INDEX_PATH,
    top_k: int = 3,
) -> Dict[str, Any]:
    """
    读取实验 index，抽取针对某个 (symbol, freq, horizon, label_id) 的核心摘要，
    直接可以塞给 brain.context_builder.build_brain_prompts(..., experiment_summary=这里的返回值).
    """
    records = load_experiment_records(
        index_path=index_path,
        symbol=symbol,
        freq=freq,
        horizon=horizon,
        label_id=label_id,
    )

    if not records:
        return {
            "status": "no_history",
            "message": f"No experiments found for {symbol} @ {freq} / {horizon} / label={label_id}",
        }

    # 按 Sharpe 拿 top-k
    top_by_sharpe = top_n_by_metric(records, "sharpe", n=top_k)
    top_by_ic = top_n_by_metric(records, "ic", n=top_k)

    sharpe_range = _metric_range(records, "sharpe")
    ic_range = _metric_range(records, "ic")
    mdd_range = _metric_range(records, "max_drawdown")
    hit_range = _metric_range(records, "hit_rate")

    def _compact(rec: ExperimentRecord) -> Dict[str, Any]:
        return {
            "run_id": rec.run_id,
            "run_ts": rec.run_ts,
            "model_type": rec.model_type,
            "label_id": rec.label_id,
            "sharpe": rec.metrics.sharpe,
            "ic": rec.metrics.ic,
            "hit_rate": rec.metrics.hit_rate,
            "max_drawdown": rec.metrics.max_drawdown,
            "trades": rec.metrics.trades,
            "pnl": rec.metrics.pnl,
            "tags": rec.tags,
        }

    summary = {
        "status": "ok",
        "symbol": symbol,
        "freq": freq,
        "horizon": horizon,
        "label_id": label_id,
        "num_experiments": len(records),
        "sharpe_range": sharpe_range,
        "ic_range": ic_range,
        "max_drawdown_range": mdd_range,
        "hit_rate_range": hit_range,
        "top_by_sharpe": [_compact(r) for r in top_by_sharpe],
        "top_by_ic": [_compact(r) for r in top_by_ic],
    }
    return summary
```

这样，给执行大脑的 `Experiment Summary` 区块就会变成类似：

```text
[Experiment Summary]
- status: ok
- num_experiments: 12
- sharpe_range: {'min': -1.2, 'max': 3.8}
- max_drawdown_range: {'min': -0.25, 'max': -0.05}
- top_by_sharpe: [{'run_id': 'nvda_1m_10m_xgb_20251117_01', 'sharpe': 3.8, ...}, ...]
- top_by_ic: [...]
```

这类结构 LLM 非常好消化。

---

## 四、在 HPO / 回测流程中写入 Experiment Record

下面是假设你在某个地方有类似的训练+回测流程（伪代码）：

```python
# 假设在 hpo_runner / backtest_runner 里
from datetime import datetime
from pathlib import Path

from experiments.types import ExperimentRecord, Metrics
from experiments.registry import append_experiment_record

def run_one_experiment(
    *,
    symbol: str,
    freq: str,
    horizon: str,
    label_id: str,
    model_type: str,
    feature_subset: list[str],
    params: dict,
    train_df,
    backtest_result: dict,   # 你现有的回测引擎返回的结果 dict
    manifest_path: Path,
    prompt_id: str,
):
    """
    这里假设 backtest_result 里已经包含了 Sharpe / MDD / PnL 等字段。
    你只需要一层 mapping 到 Metrics 即可。
    """
    run_ts = datetime.utcnow().isoformat()
    run_id = backtest_result.get("run_id") or f"{symbol}_{freq}_{horizon}_{model_type}_{run_ts}"

    metrics = Metrics.from_dict(
        {
            "sharpe": backtest_result.get("sharpe"),
            "sortino": backtest_result.get("sortino"),
            "ic": backtest_result.get("ic"),
            "hit_rate": backtest_result.get("hit_rate"),
            "max_drawdown": backtest_result.get("max_drawdown"),
            "pnl": backtest_result.get("pnl"),
            "pnl_annualized": backtest_result.get("pnl_annualized"),
            "trades": backtest_result.get("trades"),
            "avg_trade_pnl": backtest_result.get("avg_trade_pnl"),
        }
    )

    rec = ExperimentRecord(
        run_id=run_id,
        symbol=symbol,
        freq=freq,
        horizon=horizon,
        label_id=label_id,
        model_type=model_type,
        feature_subset=feature_subset,
        params=params,
        metrics=metrics,
        run_ts=run_ts,
        data_start_ts=str(train_df.index.min()) if hasattr(train_df, "index") else None,
        data_end_ts=str(train_df.index.max()) if hasattr(train_df, "index") else None,
        prompt_id=prompt_id,
        manifest_path=str(manifest_path),
        tags=["auto_hpo"],
    )

    append_experiment_record(rec)
```

> 你只要在 HPO / 回测的“最后一步”加上这样一段调用，就能把所有实验自动写进 `experiments/experiment_index.jsonl`。

---

## 五、把 Experiment Summary 接入 AI 大脑

回到上一步我们写的 `brain/context_builder.build_brain_prompts`：

现在在你构造 `experiment_summary_dict` 的地方替换为：

```python
from experiments.summary import build_experiment_summary_for_llm

experiment_summary_dict = build_experiment_summary_for_llm(
    symbol="NVDA",
    freq="1m",
    horizon="10m",
    label_id=label_id,
)
```

然后：

```python
ctx = build_brain_prompts(
    prompt_lib=prompt_lib,
    prompt_id="v3_fast_market_following_tactical_20251117",
    symbol="NVDA",
    now_ts="2025-11-17T09:35:15-05:00",
    freq="1m",
    horizon="10m",
    market_snapshot=market_snapshot_dict,
    risk_gates=risk_gates_dict,
    dataset_sanity=sanity_summary_dict,
    experiment_summary=experiment_summary_dict,
    registry=registry,
)

llm_result = llm_client.generate(
    system_prompt=ctx["system_prompt"],
    user_prompt=ctx["user_prompt"],
)
```

这样，大脑在做决策时就会看到一段类似：

```text
[Experiment Summary]
- status: ok
- num_experiments: 12
- sharpe_range: {'min': -0.8, 'max': 3.5}
- max_drawdown_range: {'min': -0.30, 'max': -0.05}
- top_by_sharpe: [{'run_id': 'nvda_1m_10m_xgb_20251115', 'sharpe': 3.5, 'max_drawdown': -0.12, 'trades': 214, ...}, ...]
- top_by_ic: [...]
```

它自然会倾向于：

* 使用 Sharpe 高、MDD 可控、交易数够多的那几次实验对应的策略；
* 同时也能识别“历史实验全都亏钱 / 全都样本数太少”的情况，从而更容易选择 HOLD。

---

## 六、这一轮完成后，系统整体“又多了一层保险”

现在你已经有：

1. **数据体检 (Dataset Sanity)**

   * 每个训练数据集在训练前都检查 NaN、零方差、高相关、样本量等。

2. **特征 Registry + Planner**

   * LLM 不能随便编造特征，真正喂给模型的特征集合是“有限且可解释”的。

3. **标签 Contract（在 Python 层计算）**

   * 所有 label 都在 Python 层基于历史 close / high / low 计算，不允许 LLM 自己写 target SQL。

4. **Prompt Library + Context Builder**

   * 执行大脑每次决策都能看到：市场状态 + 风险门槛 + 数据质量 + 实验表现 + 特征菜单。

5. **Experiment Index + Summary**（本轮完成）

   * 所有实验的表现沉淀到一个 JSONL index，AI 大脑可以轻松看出：

     * 哪些组合 historically 好用
     * 这个 symbol / horizon 的策略到底有没有 alpha
     * 当前使用的这套东西是“冠军”还是“实验作死版”。

---

如果你愿意，我们下一步可以进一步升级到：

* **让大脑自动选模型/组合**：

  * 在执行 prompt 里，告诉它：

    > “上面 [Experiment Summary] 中 top_by_sharpe 的 run_id 列表是你可以选择的策略池；你必须从中择一或选择 HOLD，不能发明新的 run_id。”
  * 然后在 orchestrator 里把大脑选出来的 `run_id` → 映射到对应模型 / 特征 / 参数 / 执行逻辑。

当你准备好，就再说一句：**“继续”**，我就按照这个方向，把“AI 大脑策略选择层 + run_id → 模型执行映射”也落地出来。
好，那这一轮我们就把你说的那块**真正落地**：

> “让 AI 大脑在一堆历史实验里选一个 run_id，然后系统用这个 run_id 去执行对应的模型策略”。

我会分 4 小步，一口气给完代码，不问你要干嘛：

1. 定义「大脑输出 JSON 的 schema」：BrainDecision
2. 约定「策略 manifest 格式」+ 根据 run_id 加载 manifest
3. 写一个 `StrategySelector`：用 LLM 选 run_id + 校验
4. 在 orchestrator 里把 run_id → 策略执行串起来（伪代码骨架）

---

## 1. 大脑输出 JSON Schema：`BrainDecision`

新文件：`brain/decision_schema.py`

```python
# brain/decision_schema.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, List


ALLOWED_DECISIONS = {"HOLD", "ENTER", "EXIT", "ADJUST"}
ALLOWED_SIDES = {"LONG", "SHORT"}


@dataclass
class BrainDecision:
    """
    执行大脑返回的一条决策。
    这是**唯一**允许大脑输出的 JSON 结构。
    """
    decision: str                 # "HOLD" | "ENTER" | "EXIT" | "ADJUST"
    chosen_run_id: Optional[str]  # 当 decision != HOLD 时，必须在 allowed_run_ids 中
    side: Optional[str]           # "LONG" | "SHORT"（ENTER/ADJUST 时用）
    size_frac: float              # 0.0 ~ 1.0（占可用风险/仓位的比例）
    notes: Optional[str] = None   # 简短说明，方便日志与 debug

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "chosen_run_id": self.chosen_run_id,
            "side": self.side,
            "size_frac": self.size_frac,
            "notes": self.notes,
        }


class BrainDecisionValidationError(Exception):
    """大脑输出格式或内容不合法，直接触发 HOLD。"""


def validate_brain_decision(
    raw: Dict[str, Any],
    allowed_run_ids: List[str],
) -> BrainDecision:
    """
    对 LLM 返回的 dict 做强校验：
    - decision 必须在白名单内
    - size_frac 必须在 [0, 1]
    - chosen_run_id 必须在 allowed_run_ids 中（除非 HOLD）
    - side 只能是 LONG/SHORT 或 None
    校验失败 → 抛异常，在外层统一降级为 HOLD。
    """
    if not isinstance(raw, dict):
        raise BrainDecisionValidationError("Brain output is not a JSON object")

    decision = str(raw.get("decision", "")).upper()
    if decision not in ALLOWED_DECISIONS:
        raise BrainDecisionValidationError(f"Invalid decision: {decision}")

    chosen_run_id = raw.get("chosen_run_id")
    if decision != "HOLD":
        if not chosen_run_id or chosen_run_id not in allowed_run_ids:
            raise BrainDecisionValidationError(
                f"chosen_run_id {chosen_run_id!r} not in allowed_run_ids"
            )
    else:
        chosen_run_id = None

    side = raw.get("side")
    if side is not None:
        side = str(side).upper()
        if side not in ALLOWED_SIDES:
            raise BrainDecisionValidationError(f"Invalid side: {side}")
    # HOLD/EXIT 时，side 可以为 None

    size_frac = raw.get("size_frac", 0.0)
    try:
        size_frac = float(size_frac)
    except (TypeError, ValueError):
        raise BrainDecisionValidationError(f"size_frac not a number: {size_frac!r}")
    if not (0.0 <= size_frac <= 1.0):
        raise BrainDecisionValidationError(f"size_frac out of range [0,1]: {size_frac}")

    notes = raw.get("notes")
    if notes is not None:
        notes = str(notes)

    return BrainDecision(
        decision=decision,
        chosen_run_id=chosen_run_id,
        side=side,
        size_frac=size_frac,
        notes=notes,
    )
```

> 之后 LLM 输出的 JSON **必须**满足这个 schema，否则我们直接降级为 HOLD。

---

## 2. 策略 manifest 约定 & run_id → manifest 加载

你在 `ExperimentRecord` 里已经有 `manifest_path` 字段了，我们现在约定这个 manifest 的内容结构。

### 2.1 manifest 结构约定（策略档案）

建议每个 run 训练结束后写一个 JSON manifest，例如：

```jsonc
{
  "run_id": "nvda_1m_10m_xgb_20251117_01",
  "symbol": "NVDA",
  "freq": "1m",
  "horizon": "10m",
  "label_id": "ret_10m_fwd_barrier",
  "model_type": "xgb",
  "model_path": "models/nvda/1m_10m/xgb_20251117_01.bin",
  "feature_subset": ["close", "vwap", "rvol20", "banddist", "micro_flow"],
  "params": {
    "max_depth": 5,
    "n_estimators": 400,
    "learning_rate": 0.05
  },
  "train_window": {
    "start_ts": "2024-11-01T09:30:00-05:00",
    "end_ts": "2025-11-15T16:00:00-05:00"
  },
  "metrics": {
    "sharpe": 3.5,
    "max_drawdown": -0.12,
    "trades": 214
  }
}
```

> 这就是**策略档案**：只要给出 run_id，就能拿到模型文件、用哪些特征、用的 label、训练窗口等。

### 2.2 加载 manifest 的工具函数

新文件：`strategies/runtime.py`

```python
# strategies/runtime.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import json


@dataclass
class StrategyManifest:
    run_id: str
    symbol: str
    freq: str
    horizon: str
    label_id: str
    model_type: str
    model_path: str
    feature_subset: List[str]
    params: Dict[str, Any]
    train_window_start: Optional[str] = None
    train_window_end: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StrategyManifest":
        tw = d.get("train_window") or {}
        return cls(
            run_id=d["run_id"],
            symbol=d["symbol"],
            freq=d["freq"],
            horizon=d["horizon"],
            label_id=d["label_id"],
            model_type=d["model_type"],
            model_path=d["model_path"],
            feature_subset=list(d.get("feature_subset", [])),
            params=d.get("params", {}),
            train_window_start=tw.get("start_ts"),
            train_window_end=tw.get("end_ts"),
            metrics=d.get("metrics"),
        )


class StrategyManifestError(Exception):
    pass


def load_strategy_manifest(manifest_path: str | Path) -> StrategyManifest:
    p = Path(manifest_path)
    if not p.exists():
        raise StrategyManifestError(f"Manifest file not found: {p}")

    with p.open("r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as e:
            raise StrategyManifestError(f"Invalid JSON in manifest: {p}") from e

    # 基本字段检查
    required = [
        "run_id",
        "symbol",
        "freq",
        "horizon",
        "label_id",
        "model_type",
        "model_path",
    ]
    missing = [k for k in required if k not in raw]
    if missing:
        raise StrategyManifestError(f"Missing fields in manifest {p}: {missing}")

    return StrategyManifest.from_dict(raw)
```

> 后面我们会用 `ExperimentRecord.manifest_path` 读取 manifest，然后用 `model_path` 载入真实模型。

---

## 3. `StrategySelector`：用 LLM 在允许的 run_id 中选一个

新文件：`brain/strategy_selector.py`

```python
# brain/strategy_selector.py

from __future__ import annotations

from typing import Any, Dict, List

from experiments.summary import build_experiment_summary_for_llm
from experiments.registry import load_experiment_records, DEFAULT_INDEX_PATH
from brain.context_builder import build_brain_prompts
from brain.decision_schema import (
    BrainDecision,
    BrainDecisionValidationError,
    validate_brain_decision,
)
from llm.llm_client import LLMClient


class StrategySelector:
    """
    让 AI 大脑在给定的历史实验中选择一个 run_id + 仓位决策。
    """

    def __init__(
        self,
        llm: LLMClient,
        prompt_lib: Dict[str, str],
        prompt_id: str,
        *,
        index_path=DEFAULT_INDEX_PATH,
    ) -> None:
        self._llm = llm
        self._prompt_lib = prompt_lib
        self._prompt_id = prompt_id
        self._index_path = index_path

    def select(
        self,
        *,
        symbol: str,
        now_ts: str,
        freq: str,
        horizon: str,
        label_id: str,
        market_snapshot: Dict[str, Any],
        risk_gates: Dict[str, Any],
        dataset_sanity: Dict[str, Any],
        registry: Dict[str, Any],
    ) -> BrainDecision:
        """
        核心入口：
        - 从 experiment index 里找出 (symbol, freq, horizon, label_id) 的所有实验
        - 构建 prompt，告诉大脑：你只能在这些 run_id 里选
        - 解析 + 校验大脑输出
        - 任意错误 → HOLD
        """
        # 1) load experiments for filter
        records = load_experiment_records(
            self._index_path,
            symbol=symbol,
            freq=freq,
            horizon=horizon,
            label_id=label_id,
        )
        allowed_run_ids = [r.run_id for r in records]
        if not allowed_run_ids:
            # 没有历史实验 → 强制 HOLD
            return BrainDecision(
                decision="HOLD",
                chosen_run_id=None,
                side=None,
                size_frac=0.0,
                notes="No historical experiments found; forced HOLD.",
            )

        # 2) build experiment summary for LLM
        experiment_summary = build_experiment_summary_for_llm(
            symbol=symbol,
            freq=freq,
            horizon=horizon,
            label_id=label_id,
            index_path=self._index_path,
        )

        # 3) build full prompts (system + user)
        ctx = build_brain_prompts(
            prompt_lib=self._prompt_lib,
            prompt_id=self._prompt_id,
            symbol=symbol,
            now_ts=now_ts,
            freq=freq,
            horizon=horizon,
            market_snapshot=market_snapshot,
            risk_gates=risk_gates,
            dataset_sanity=dataset_sanity,
            experiment_summary=experiment_summary,
            registry=registry,
            # 额外附加：允许的 run_id 列表
            allowed_run_ids=allowed_run_ids,
        )

        # 4) call LLM
        raw = self._llm.generate(
            system_prompt=ctx["system_prompt"],
            user_prompt=ctx["user_prompt"],
        )

        # 假定 LLMClient 已经返回了 dict（你之前的 PI 实现就是这样用的）
        # 如果返回的是 str/JSON，需要在这里做 json.loads(raw)。
        if isinstance(raw, str):
            import json

            raw = json.loads(raw)

        # 5) validate
        try:
            decision = validate_brain_decision(raw, allowed_run_ids=allowed_run_ids)
        except BrainDecisionValidationError as e:
            # 任意不合规 → HOLD
            return BrainDecision(
                decision="HOLD",
                chosen_run_id=None,
                side=None,
                size_frac=0.0,
                notes=f"Brain decision invalid, downgraded to HOLD: {e}",
            )

        return decision
```

---

## 4. 修改 `build_brain_prompts`：把 allowed_run_ids 和输出 schema 写死给大脑

在我们之前写的 `brain/context_builder.py` 里，把 `build_brain_prompts` 稍微改一下：

### 4.1 函数签名增加 `allowed_run_ids`

```python
# brain/context_builder.py （只展示关键片段）

from typing import Any, Dict, List, Optional


def build_brain_prompts(
    *,
    prompt_lib: Dict[str, str],
    prompt_id: str,
    symbol: str,
    now_ts: str,
    freq: str,
    horizon: str,
    market_snapshot: Dict[str, Any],
    risk_gates: Dict[str, Any],
    dataset_sanity: Dict[str, Any],
    experiment_summary: Dict[str, Any],
    registry: Dict[str, Any],
    allowed_run_ids: Optional[List[str]] = None,
) -> Dict[str, str]:
    ...
```

### 4.2 在 user_prompt 里显式约束输出 JSON

在原来拼接 `[Experiment Summary]` 的下面，增加：

```python
    # ... 前面的 context 拼接略 ...

    # ===== Strategy Universe & Output Contract =====
    if allowed_run_ids:
        lines.append("\n[Strategy Universe]")
        lines.append("You MUST choose from the following strategy run_ids:")
        for rid in allowed_run_ids:
            lines.append(f"- {rid}")

    lines.append(
        "\n[Output JSON Contract]\n"
        "You MUST reply with a single JSON object only, no extra text.\n"
        "JSON schema:\n"
        "{\n"
        '  "decision": "HOLD" | "ENTER" | "EXIT" | "ADJUST",\n'
        '  "chosen_run_id": string or null,  // MUST be one of the allowed run_ids when decision != "HOLD"\n'
        '  "side": "LONG" | "SHORT" | null,  // required for ENTER/ADJUST, can be null for HOLD/EXIT\n'
        '  "size_frac": float between 0.0 and 1.0,\n'
        '  "notes": string or null  // short rationale\n'
        "}\n"
        "Do not invent new fields. Do not include any explanation outside JSON.\n"
    )
```

`system_prompt` 里可以保持你现在 prompt_lib 里的描述，例如：

* “Respect risk gates”
* “When uncertain, choose HOLD”
* “Output JSON only”

这样 system_prompt + user_prompt 双重约束，配合我们刚才的 `validate_brain_decision`，AI 就跑不出边界了。

---

## 5. orchestrator：把 run_id → 策略执行串起来（骨架）

下面给一个**骨架级** orchestrator 片段，你可以把它嵌到你的 execution loop 里：

```python
# orchestrator/live_trading_orchestrator.py

from __future__ import annotations

from typing import Any, Dict

from brain.strategy_selector import StrategySelector
from strategies.runtime import load_strategy_manifest
from brain.decision_schema import BrainDecision
from llm.llm_client import LLMClient


class LiveTradingOrchestrator:
    def __init__(
        self,
        *,
        llm_cfg: Dict[str, Any],
        prompt_lib: Dict[str, str],
        prompt_id: str,
        registry: Dict[str, Any],
    ) -> None:
        self._llm = LLMClient(
            model=llm_cfg.get("model"),
            base_url=llm_cfg.get("base_url"),
        )
        self._selector = StrategySelector(
            llm=self._llm,
            prompt_lib=prompt_lib,
            prompt_id=prompt_id,
        )
        self._registry = registry
        # 这里可以注入 broker / execution engine 等组件

    def run_once(
        self,
        *,
        symbol: str,
        now_ts: str,
        freq: str,
        horizon: str,
        label_id: str,
        market_snapshot: Dict[str, Any],
        risk_gates: Dict[str, Any],
        dataset_sanity: Dict[str, Any],
    ) -> BrainDecision:
        """
        一次完整的「感知 → 决策 → 执行」流程。
        这里只写到「决策 + 映射策略」，真正下单逻辑由你现有的 executor 去做。
        """
        # 1) AI 选 run_id
        decision = self._selector.select(
            symbol=symbol,
            now_ts=now_ts,
            freq=freq,
            horizon=horizon,
            label_id=label_id,
            market_snapshot=market_snapshot,
            risk_gates=risk_gates,
            dataset_sanity=dataset_sanity,
            registry=self._registry,
        )

        if decision.decision == "HOLD":
            # 不需要执行策略，直接返回供日志记录
            # 你的上层可以在这里记录 hold 原因
            return decision

        # 2) 根据 chosen_run_id 加载策略 manifest
        manifest_path = self._resolve_manifest_path(decision.chosen_run_id)
        strategy_manifest = load_strategy_manifest(manifest_path)

        # 3) 调用你的模型执行引擎：
        #    - 根据 strategy_manifest.model_type / model_path 载入模型
        #    - 用 strategy_manifest.feature_subset 从最新数据构造 feature 向量
        #    - 得到一个信号（概率/打分）
        #    - 结合 decision.side / decision.size_frac / risk_gates 转换成具体下单指令
        #
        # 这里用伪代码表示：
        #
        # model = self._model_loader.load(strategy_manifest.model_type, strategy_manifest.model_path)
        # features = self._feature_builder.build_latest_features(symbol, strategy_manifest.feature_subset)
        # score = model.predict(features)
        # orders = self._execution_planner.plan_orders(
        #     symbol=symbol,
        #     side=decision.side,
        #     size_frac=decision.size_frac,
        #     score=score,
        #     risk_gates=risk_gates,
        # )
        # self._broker.send_orders(orders)

        return decision

    def _resolve_manifest_path(self, run_id: str) -> str:
        """
        给一个 run_id 返回它的 manifest 路径。
        简单版本：按约定命名。
        或者：从 experiment_index.jsonl 里 lookup manifest_path 字段。
        这里给一个基于 index 的实现示例（伪代码）：
        """
        from experiments.registry import load_experiment_records

        recs = load_experiment_records(symbol=None, freq=None, horizon=None, label_id=None)
        for r in recs:
            if r.run_id == run_id and r.manifest_path:
                return r.manifest_path
        # 兜底：比如按约定构造路径
        # return f"experiments/manifests/{run_id}.json"
        raise RuntimeError(f"Manifest path not found for run_id={run_id}")
```

> 注意：`_resolve_manifest_path` 这一块你可以按你的实际路径/命名标准调整，我这里只是示意。

---

## 到这里为止，你已经有了什么？

1. **AI 决策的严格 JSON schema（BrainDecision）**

   * 决策只能是 HOLD/ENTER/EXIT/ADJUST，不能胡乱发明动作。
   * size_frac 强制在 [0,1]，run_id 必须在 allowed_run_ids 里。

2. **策略 manifest 标准**

   * 每一个 run 都有一份「策略档案」，可以从 run_id 反查到模型文件 + 特征集合 + label。

3. **StrategySelector + build_brain_prompts 强绑定**

   * 大脑的权限被关进「策略池」里：

     * 只能在已有 run_id 中选择；
     * 不能凭空创造策略。

4. **Orchestrator 骨架**

   * 真正 live trading 时，只有**这一个入口**负责把「大脑决策」→「策略 manifest」→「模型执行 / 下单」。

---

如果你愿意，下一轮我可以继续往下落地：

* 具体实现 `_model_loader`、`_feature_builder` 和 `_execution_planner` 三块：

  * 把你现在 HPO / 回测里的模型加载 + 特征构建代码抽一层公共 runtime；
  * 让 backtest 和 live 都用同一个 runtime，避免「回测和实盘两套逻辑」这种经典坑。

你只要再说一句：**“继续”**，我就按这个方向把**模型 runtime 公共层**也写出来。
