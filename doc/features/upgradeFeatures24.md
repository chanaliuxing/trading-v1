好，那我们就**专心把这条链路打通：**

> `PrincipalInvestigator (PI) → HPO 训练/回测 → ExperimentRegistry 写入`

我会直接给你三个核心拼图：

1. 一个专门“串联整条链”的 orchestrator：`pi_to_hpo_pipeline.py`
2. 一个“PI → HPO 的桥接模块”，抽象出 `trial_runner` 回调：`ml/pi_hpo_bridge.py`
3. 在 HPO trial 里如何写入 `ExperimentRegistry` 的标准写法

你只需要把自己的现有 HPO/回测逻辑当成 `trial_runner` 塞进去，就完成打通。

---

## 一、PI → HPO → Registry 的总控：`pi_to_hpo_pipeline.py`

这个文件的职责：

* 用 ClickHouse + LLM 调用 `PrincipalInvestigator.propose`，拿到 `PIResult`（包含 dataset_df、hpo_grid、dataset_summary、label_id 等）
* 调用 `run_pi_hpo_and_log`，对 grid 里的每个候选模型跑一遍你的 HPO/回测
* 自动把每个候选写成 `ExperimentRecord` 到 `ExperimentRegistry`
* 返回**当前 symbol + label_id** 的最优 Experiment（Sharpe 最大或你定义的指标）

```python
# pipelines/pi_to_hpo_pipeline.py

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from pi.principal_investigator import PrincipalInvestigator, PIResult
from experiments.registry import ExperimentRegistry
from ml.pi_hpo_bridge import run_pi_hpo_and_log, HPORunConfig, TrialRunner

# ====== 你需要自己提供的 trial_runner（下面有详细说明） ======
from ml.my_existing_hpo import run_single_trial  # 这是你现有的 HPO/回测入口


def build_default_ch_cfg() -> Dict[str, Any]:
    # TODO: 换成你真实的 ClickHouse 连接配置（或从 config 文件读取）
    return {
        "host": "localhost",
        "port": 8123,
        "username": "default",
        "password": "",
        "database": "trading",
    }


def build_default_llm_cfg() -> Dict[str, Any]:
    # TODO: 换成你真实的 LLM 配置
    return {
        "model": "gpt-5.1-thinking",
        "base_url": "http://localhost:8000/v1",
    }


def run_pi_to_hpo_once(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    prompt_version: str = "pi_v1",
    code_version: str = "git:HEAD",
    ch_cfg: Optional[Dict[str, Any]] = None,
    llm_cfg: Optional[Dict[str, Any]] = None,
    experiment_log_path: str = "experiments/experiments.jsonl",
) -> None:
    """
    一次完整的流程：
    1) 调 PI 生成 feature + training_plan + hpo_grid + dataset_summary
    2) 调 HPO trial_runner，对 grid 里每个候选跑一遍
    3) 每个候选写入 ExperimentRegistry
    4) 返回最优 Experiment（并打印出来）
    """
    ch_cfg = ch_cfg or build_default_ch_cfg()
    llm_cfg = llm_cfg or build_default_llm_cfg()

    # 1) 调用 PI
    pi = PrincipalInvestigator(
        ch_cfg=ch_cfg,
        llm_cfg=llm_cfg,
    )
    pi_result: PIResult = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
    )

    if not pi_result.label_id:
        raise RuntimeError(
            f"PIResult.label_id 缺失，无法进入 HPO。training_plan={pi_result.training_plan}"
        )

    label_id = pi_result.label_id
    dataset_summary = pi_result.dataset_summary

    print(
        f"[PI] Got plan for {symbol} {start_ts}~{end_ts}, "
        f"label_id={label_id}, sanity={dataset_summary.sanity_level}"
    )

    # 2) 实例化 ExperimentRegistry
    registry = ExperimentRegistry(path=experiment_log_path)

    # 3) 配置 HPO 运行参数（可以先简单一点，后面再进化）
    hpo_cfg = HPORunConfig(
        cv_splits=3,
        min_trades=30,
        metric="sharpe",   # 只是标签，具体含义由你的 trial_runner 决定
    )

    # 4) 调用桥接：PI → HPO → Registry
    run_output = run_pi_hpo_and_log(
        pi_result=pi_result,
        symbol=symbol,
        table=table,
        start_ts=start_ts,
        end_ts=end_ts,
        label_id=label_id,
        code_version=code_version,
        prompt_version=prompt_version,
        registry=registry,
        hpo_cfg=hpo_cfg,
        trial_runner=run_single_trial,   # ⭐ 把你现有 HPO 函数挂上来
    )

    best = run_output.best_experiment

    print("\n[HPO] Best experiment selected:")
    print(
        f"  run_id={best.run_id}, model={best.model_type}, "
        f"sharpe={best.metrics.sharpe}, trades={best.metrics.trade_count}, "
        f"mdd={best.metrics.max_drawdown}"
    )
    print("  artifacts:", best.artifacts)


if __name__ == "__main__":
    # 最简单的手工调用示例，你以后可以改成 argparse / click
    run_pi_to_hpo_once(
        table="bars_1m_features_v1",
        symbol="NVDA",
        start_ts="2025-01-01 09:30:00",
        end_ts="2025-03-31 16:00:00",
        goal="获取稳定的日内多空 alpha",
        horizon="10m",
    )
```

---

## 二、PI → HPO 的桥接模块：`ml/pi_hpo_bridge.py`

### 2.1 定义一个 HPO 运行配置 + 返回结构

```python
# ml/pi_hpo_bridge.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Protocol

import pandas as pd

from pi.principal_investigator import PIResult
from experiments.registry import ExperimentRegistry
from experiments.types import ExperimentRecord, Metrics
from sanity.dataset_summary import DatasetSummary


@dataclass
class HPORunConfig:
    cv_splits: int = 3
    min_trades: int = 30
    metric: str = "sharpe"   # 或 "auc" 等，具体由你的 trial_runner 决定


@dataclass
class HPORunOutput:
    best_experiment: ExperimentRecord
    all_experiments: List[ExperimentRecord]


class TrialRunner(Protocol):
    """
    你的 HPO/回测函数只要满足这个签名，就可以被接进来。

    它的职责：
      - 用给定的 dataset_df + feature_cols + target_col
      - 按 model_type + model_params 训练模型 + 回测
      - 返回 Metrics（sharpe, mdd, trade_count 等）
      - 同时可以把模型、回测结果写本地，然后把路径放在 artifacts 里返回
    """

    def __call__(
        self,
        *,
        dataset_df: pd.DataFrame,
        feature_cols: List[str],
        target_col: str,
        model_type: str,
        model_params: Dict[str, Any],
        task: str,
        hpo_cfg: HPORunConfig,
    ) -> tuple[Metrics, Dict[str, Any]]:
        ...
```

> 注意：`TrialRunner` 返回 `(metrics, artifacts)`：
>
> * `metrics`：你的 backtest/训练结果
> * `artifacts`：比如模型文件路径、回测报告路径等 dict

### 2.2 主函数：对 grid 每一行调用 trial_runner，并写入 `ExperimentRegistry`

```python
# 继续 ml/pi_hpo_bridge.py

import uuid


def _extract_feature_cols(pi_result: PIResult) -> List[str]:
    """
    当前版本：从 hpo_grid.feature_subset 里取第一项。
    以后你可以扩展为：每个候选有自己的 feature_subset。
    """
    fs_list = pi_result.hpo_grid.get("feature_subset") or []
    if not fs_list:
        raise ValueError("PIResult.hpo_grid.feature_subset 为空，无法进行 HPO。")
    # 这里假设 feature_subset[0] 就是完整特征列表
    return fs_list[0]


def _get_target_col(pi_result: PIResult) -> str:
    """
    兼容你当前的 training_plan：
    - 如果有 training_plan['target'] 就用它
    - 否则默认 'target'
    （未来你切到 LabelRegistry 驱动的时候，可以改成 label_registry.get(label_id)）
    """
    return pi_result.training_plan.get("target", "target")


def run_pi_hpo_and_log(
    *,
    pi_result: PIResult,
    symbol: str,
    table: str,
    start_ts: str,
    end_ts: str,
    label_id: str,
    code_version: str,
    prompt_version: str,
    registry: ExperimentRegistry,
    hpo_cfg: HPORunConfig,
    trial_runner: TrialRunner,
) -> HPORunOutput:
    """
    这是真正“打通”的核心函数：
      PIResult(dataset_df + hpo_grid) → 循环 trial_runner → 写 ExperimentRegistry
    """

    dataset_df: pd.DataFrame = pi_result.dataset_df
    dataset_summary: DatasetSummary = pi_result.dataset_summary

    feature_cols = _extract_feature_cols(pi_result)
    target_col = _get_target_col(pi_result)
    task = pi_result.training_plan.get("task", "reg")

    model_types: List[str] = pi_result.hpo_grid.get("model_type") or []
    model_params_list: List[Dict[str, Any]] = pi_result.hpo_grid.get("model_params") or []

    if not model_types:
        raise ValueError("PIResult.hpo_grid.model_type 为空。")
    if len(model_types) != len(model_params_list):
        raise ValueError("hpo_grid.model_type 与 model_params 长度不一致。")

    experiments: List[ExperimentRecord] = []

    for idx, (mtype, mparams) in enumerate(zip(model_types, model_params_list)):
        run_suffix = f"{idx:02d}"
        run_id = (
            f"{symbol}_{label_id}_"
            f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}_{run_suffix}"
        )

        print(f"[HPO] Running trial {idx+1}/{len(model_types)}: run_id={run_id}, model={mtype}")

        # 1) 调你的 trial_runner 做训练 + 回测
        metrics, artifacts = trial_runner(
            dataset_df=dataset_df,
            feature_cols=feature_cols,
            target_col=target_col,
            model_type=mtype,
            model_params=mparams,
            task=task,
            hpo_cfg=hpo_cfg,
        )

        # 2) 组装 ExperimentRecord（统一格式）
        training_config: Dict[str, Any] = {
            "task": task,
            "target_col": target_col,
            "feature_cols": feature_cols,
            "cv_splits": hpo_cfg.cv_splits,
        }
        hpo_config: Dict[str, Any] = {
            "source": "pi_hpo_grid",
            "grid_index": idx,
            "grid_size": len(model_types),
            "metric": hpo_cfg.metric,
        }

        record = ExperimentRecord.new(
            run_id=run_id,
            symbol=symbol,
            table=table,
            start_ts=start_ts,
            end_ts=end_ts,
            label_id=label_id,
            feature_ids=feature_cols,   # 当前先用列名，未来可以换成 feature_id 列表
            model_type=mtype,
            model_params=mparams,
            hpo_config=hpo_config,
            training_config=training_config,
            metrics=metrics,
            dataset_sanity_level=dataset_summary.sanity_level,
            dataset_issues=dataset_summary.main_issues,
            code_version=code_version,
            prompt_version=prompt_version,
            artifacts=artifacts,
        )

        # 3) 写入 Registry
        registry.append(record)
        experiments.append(record)

    # 4) 在本次 run 的候选里选一个“最优 experiment”返回（Sharpe 优先）
    def _score(rec: ExperimentRecord) -> float:
        if rec.metrics.sharpe is not None:
            return rec.metrics.sharpe
        # 没有 Sharpe，就退化为用 pnl 或 trade_count 排序
        if rec.metrics.pnl is not None:
            return rec.metrics.pnl
        if rec.metrics.trade_count is not None:
            return float(rec.metrics.trade_count)
        return -1e9

    experiments.sort(key=_score, reverse=True)
    best = experiments[0]

    return HPORunOutput(
        best_experiment=best,
        all_experiments=experiments,
    )
```

> ✅ 到这里，这条链路逻辑已经闭环：
> PI 给出 `dataset_df + hpo_grid` → `run_pi_hpo_and_log` 逐个 candidate 跑你的 HPO → 每个 candidate 变成一个 `ExperimentRecord` 写进 JSONL。

---

## 三、你现有 HPO/回测怎么接成 `trial_runner`？

现在就剩一个问题：**把你现有的 HPO/回测逻辑包装成 `TrialRunner`。**

你可以在一个文件里写，比如：`ml/my_existing_hpo.py`：

```python
# ml/my_existing_hpo.py

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from experiments.types import Metrics
from ml.pi_hpo_bridge import HPORunConfig


def run_single_trial(
    *,
    dataset_df: pd.DataFrame,
    feature_cols: List[str],
    target_col: str,
    model_type: str,
    model_params: Dict[str, Any],
    task: str,
    hpo_cfg: HPORunConfig,
) -> Tuple[Metrics, Dict[str, Any]]:
    """
    这是你需要实现/对接的部分。
    可以有两种方式：

    方式 A：调用你现有的 backtest / hpo 代码，拿到真实 Sharpe / MDD / trade_count。
    方式 B：先临时写一个简单版（比如 sklearn + 简单策略），先把链路打通，再逐步替换为正式引擎。
    """

    # ===== A. 典型对接方式（伪代码，示意你要做什么） =====
    #
    # 1) 根据 model_type 选用内部策略类或模型
    # model = build_model_from_type(model_type, model_params)
    #
    # 2) 切出 X / y
    # X = dataset_df[feature_cols].to_numpy()
    # y = dataset_df[target_col].to_numpy()
    #
    # 3) 调用你的 cross-validation + backtest 引擎
    # sharpe, mdd, pnl, win_rate, trade_count, extra_reports = my_backtest_engine(
    #     model=model,
    #     X=X,
    #     y=y,
    #     task=task,
    #     cv_splits=hpo_cfg.cv_splits,
    # )
    #
    # 4) 把结果塞进 Metrics 和 artifacts
    # metrics = Metrics(
    #     sharpe=sharpe,
    #     sortino=None,  # 如果有就填
    #     max_drawdown=mdd,
    #     pnl=pnl,
    #     win_rate=win_rate,
    #     trade_count=trade_count,
    # )
    # artifacts = {
    #     "model_path": saved_model_path,
    #     "backtest_report": report_path,
    # }
    # return metrics, artifacts

    # ===== B. 临时占位实现（不会报错，但指标是假的，只是用来打通流程） =====
    n = len(dataset_df)
    fake_sharpe = np.random.uniform(0.5, 3.0)
    fake_mdd = -np.random.uniform(0.05, 0.3)
    fake_pnl = np.random.uniform(100.0, 1000.0)
    fake_trades = max(hpo_cfg.min_trades, int(n * 0.01))

    metrics = Metrics(
        sharpe=fake_sharpe,
        sortino=None,
        max_drawdown=fake_mdd,
        pnl=fake_pnl,
        win_rate=None,
        trade_count=fake_trades,
    )
    artifacts = {
        "note": "TEMP FAKE METRICS - 请尽快用真实 backtest 替换 run_single_trial。",
    }
    return metrics, artifacts
```

> 建议：
>
> * **现在就可以用 “B. 临时占位实现” 把链路跑通**，验证：
>
>   * PI 能正常生成 hpo_grid 和 dataset_df；
>   * `run_pi_to_hpo_once` 能写出 experiments.jsonl；
>   * `ExperimentRegistry.load_all()` 能读到多条记录。
> * 然后你再逐步把 `run_single_trial` 里的伪代码替换成你真实的 backtest / HPO 引擎调用。

---

## 四、现在这条链路真正达成了什么？

从系统视角看，现在已经：

1. **PI 层**

   * 不再裸奔：被 FeatureRegistry + LabelRegistry + DatasetSanity 包裹。
   * 输出的 `PIResult` 包含：

     * `dataset_df`（训练数据）
     * `hpo_grid`（候选模型列表）
     * `dataset_summary`（体检结果）
     * `label_id`（标签定义）

2. **HPO / 训练层**

   * 有了统一的 `TrialRunner` 协议：

     * 无论你用的是 XGBoost + Optuna，还是你自己的策略类 + BacktestEngine，都可以被包进去。
   * `run_pi_hpo_and_log` 将 PI 的输出转成一系列 ExperimentRecord，并写入 Registry。

3. **ExperimentRegistry 层**

   * 每个候选模型都有结构化记录：

     * run_id / symbol / table / 时间窗口
     * label_id, feature_ids
     * model_type, model_params
     * metrics（Sharpe, MDD, trade_count, pnl …）
     * dataset_sanity_level / issues
     * artifacts（模型/报告路径）

下一步，如果你愿意，我们就可以顺势把：

> **ExperimentRegistry → BrainController → 实盘策略选择**

这一段也串成一条“Strategy Factory → AI 大脑选策略 → 执行”的完整闭环。

你这边先按上面代码，把：

* `pi_to_hpo_pipeline.py`
* `ml/pi_hpo_bridge.py`
* `ml/my_existing_hpo.py`（先用假 metrics 版）

放进项目里，跑一遍看看有没有 import / 路径上的问题。
如果跑的时候遇到报错（尤其是和你现有代码冲突的地方），直接把错误贴给我，我们就按错误逐个修正。
好，那我们从你刚刚打通的：

> **PI → HPO → ExperimentRegistry**

继续往前推，把这条链路真正“接到 AI 大脑眼前”，并且把 Prompt 白名单 / 安全也落进去。

这一步我会做三件事：

1. 给 `ExperimentRegistry` 增加查询 & 过滤能力（方便 AI 大脑用）。
2. 做一个 **Experiment 总结模块**：把一堆 trials 压缩成“小卡片 + 聚合统计”，专门给 LLM 看。
3. 做一个 **Brain 上下文构建器 + Prompt 白名单**：保证大脑只能在白名单策略 + 白名单 prompt 里做选择。

> ⚠️ 注意：下面代码都假设你之前已经有 `ExperimentRecord`, `Metrics`, `ExperimentRegistry` 这些类型。字段名字如果和你现有实现有出入，你只要对齐字段名即可，结构是通用的。

---

## 一、给 ExperimentRegistry 加上 query / filter 能力

先在 `experiments/registry.py` 里加两个小方法：`load_all` 和 `query`。

```python
# experiments/registry.py （在你已有 ExperimentRegistry 上补充）

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Optional

import json

from .types import ExperimentRecord


class ExperimentRegistry:
    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    def append(self, record: ExperimentRecord) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), default=str) + "\n")

    # ✅ 新增：一次性加载全部记录
    def load_all(self) -> List[ExperimentRecord]:
        records: List[ExperimentRecord] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                records.append(ExperimentRecord.from_dict(obj))
        return records

    # ✅ 新增：按 symbol / label_id / 时间过滤
    def query(
        self,
        *,
        symbol: Optional[str] = None,
        label_id: Optional[str] = None,
        min_start_ts: Optional[str] = None,
        max_end_ts: Optional[str] = None,
    ) -> List[ExperimentRecord]:
        all_recs = self.load_all()
        out: List[ExperimentRecord] = []
        for rec in all_recs:
            if symbol and rec.symbol != symbol:
                continue
            if label_id and rec.label_id != label_id:
                continue
            if min_start_ts and rec.start_ts < min_start_ts:
                continue
            if max_end_ts and rec.end_ts > max_end_ts:
                continue
            out.append(rec)
        return out
```

> 这样 AI 大脑就可以说：“给我 NVDA + label=ret_10m 的所有实验”，不需要碰底层 JSON 文件。

---

## 二、ExperimentSummary：把一堆 trial 压缩成 LLM 友好的“小卡片”

新建 `experiments/summary.py`：

```python
# experiments/summary.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Tuple

from .types import ExperimentRecord


@dataclass
class ExperimentSummaryItem:
    run_id: str
    symbol: str
    label_id: str
    model_type: str
    sharpe: float | None
    max_drawdown: float | None
    trade_count: int | None
    pnl: float | None
    dataset_sanity_level: str
    dataset_issues: List[str]
    code_version: str
    prompt_version: str


@dataclass
class ExperimentSummaryBundle:
    """压缩后的摘要，给 AI 大脑做“眼睛输入”。"""

    symbol: str
    label_id: str
    n_total: int
    n_usable: int
    filter_reason: str
    top_items: List[ExperimentSummaryItem]
    # 推荐策略的 run_id（比如 Sharpe 最大）
    best_run_id: str | None
    # 一段简短自然语言摘要，可以直接塞进 LLM prompt
    natural_summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "label_id": self.label_id,
            "n_total": self.n_total,
            "n_usable": self.n_usable,
            "filter_reason": self.filter_reason,
            "best_run_id": self.best_run_id,
            "top_items": [asdict(x) for x in self.top_items],
            "natural_summary": self.natural_summary,
        }
```

核心的 summarizer：

```python
# 继续 experiments/summary.py

def _score(rec: ExperimentRecord) -> float:
    m = rec.metrics
    if m.sharpe is not None:
        return m.sharpe
    if m.pnl is not None:
        return m.pnl
    if m.trade_count is not None:
        return float(m.trade_count)
    return -1e9


def summarize_for_brain(
    experiments: Iterable[ExperimentRecord],
    *,
    min_trades: int = 30,
    min_sharpe: float = -10.0,
    allowed_sanity: Tuple[str, ...] = ("ok", "warning"),
    top_k: int = 5,
) -> ExperimentSummaryBundle | None:
    """把一堆 ExperimentRecord 变成“小卡片 + 聚合摘要”。

    返回 None 表示：没找到任何可用策略（比如 trades 不够 or 数据 fatal）。
    """
    exps = list(experiments)
    if not exps:
        return None

    symbol = exps[0].symbol
    label_id = exps[0].label_id

    usable: List[ExperimentRecord] = []
    for rec in exps:
        m = rec.metrics
        if rec.dataset_sanity_level not in allowed_sanity:
            continue
        if m.trade_count is not None and m.trade_count < min_trades:
            continue
        if m.sharpe is not None and m.sharpe < min_sharpe:
            continue
        usable.append(rec)

    if not usable:
        return ExperimentSummaryBundle(
            symbol=symbol,
            label_id=label_id,
            n_total=len(exps),
            n_usable=0,
            filter_reason="All experiments filtered out by sanity/trades/Sharpe thresholds.",
            top_items=[],
            best_run_id=None,
            natural_summary=(
                f"For {symbol}/{label_id}, no experiments passed the quality filters. "
                "AI Brain should avoid using learned strategies for now."
            ),
        )

    usable.sort(key=_score, reverse=True)
    selected = usable[:top_k]

    items: List[ExperimentSummaryItem] = []
    for rec in selected:
        m = rec.metrics
        items.append(
            ExperimentSummaryItem(
                run_id=rec.run_id,
                symbol=rec.symbol,
                label_id=rec.label_id,
                model_type=rec.model_type,
                sharpe=m.sharpe,
                max_drawdown=m.max_drawdown,
                trade_count=m.trade_count,
                pnl=m.pnl,
                dataset_sanity_level=rec.dataset_sanity_level,
                dataset_issues=rec.dataset_issues,
                code_version=rec.code_version,
                prompt_version=rec.prompt_version,
            )
        )

    best = items[0]

    # ✅ 给 LLM 的自然语言摘要（短小 + 重点）
    natural_summary = (
        f"For symbol={symbol}, label={label_id}, we have {len(exps)} experiments, "
        f"{len(usable)} passed quality filters (sanity in {allowed_sanity}, "
        f"trades≥{min_trades}, Sharpe≥{min_sharpe}). "
        f"Best run is {best.run_id} using model_type={best.model_type} "
        f"with Sharpe={best.sharpe}, max_drawdown={best.max_drawdown}, "
        f"trades={best.trade_count}."
    )

    return ExperimentSummaryBundle(
        symbol=symbol,
        label_id=label_id,
        n_total=len(exps),
        n_usable=len(usable),
        filter_reason="OK",
        top_items=items,
        best_run_id=best.run_id,
        natural_summary=natural_summary,
    )
```

> 这一步的意义：
>
> * AI 大脑**不用再扫一堆 JSONL**，只看 summary_bundle 就知道：
>
>   * 这个 symbol/label 有多少策略可用
>   * 哪些 run_id 是高质量候选
>   * 哪个 run_id 是当前“候选冠军”

后面我们要让大脑在 **“allowed_run_ids = top_items.run_id”** 里做选择，而不是随便生成策略。

---

## 三、Prompt 白名单：把你那份大 JSON 变成真正的“控制阀门”

基于你给的 prompt JSON，新建 `brain/prompt_library.py`：

```python
# brain/prompt_library.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List


class PromptNotFoundError(KeyError):
    pass


class PromptLibrary:
    """简单的 Prompt 白名单封装。

    - 只允许通过 prompt_id 获取 prompt 文本
    - prompt_id 必须出现在 prompt_library.json 中
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        with self._path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        self._meta = raw.get("meta", {})
        # 顶层 prompt（如 v3_stable / aggressive_scalper）
        top_level = {
            k: v
            for k, v in raw.items()
            if not k.startswith("_") and k not in ("prompts", "meta")
        }
        # prompts 子字典
        nested = raw.get("prompts", {})

        # 优先使用 prompts 子字典，如果重复，nested 覆盖顶层
        merged: Dict[str, str] = {}
        merged.update(top_level)
        merged.update(nested)

        self._prompts = merged

    def list_ids(self) -> List[str]:
        return sorted(self._prompts.keys())

    def get_prompt(self, prompt_id: str) -> str:
        try:
            return self._prompts[prompt_id]
        except KeyError as e:
            raise PromptNotFoundError(
                f"Prompt id '{prompt_id}' not found in library {self._path}"
            ) from e

    def has(self, prompt_id: str) -> bool:
        return prompt_id in self._prompts
```

> 之后：
>
> * AI 大脑 / 策略代码 **只能** 传 `execution_prompt_id="v3_fast_market_following_tactical_20251117_v2"` 这种 ID。
> * 真正的 `system_prompt` 一律通过 `PromptLibrary.get_prompt()` 拿，禁止自由拼接乱写。

这样就结掉了“Prompt 注入”的一大块风险。

---

## 四、Brain 上下文构建：Experiment + Prompt → LLM 输入

接下来，把上面的东西粘起来：新建 `brain/context_builder.py`：

```python
# brain/context_builder.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from experiments.registry import ExperimentRegistry
from experiments.summary import summarize_for_brain, ExperimentSummaryBundle
from brain.prompt_library import PromptLibrary, PromptNotFoundError


@dataclass
class BrainContext:
    """给 AI 大脑调用 LLM 时的完整上下文打包。"""

    execution_prompt_id: str      # 白名单中的 prompt id
    system_prompt: str            # 真正的 system prompt
    # 给 LLM 的“任务输入”，通常会转成 user_prompt/JSON
    brain_input: Dict[str, Any]
    # 限制大脑只能从这些 run_id 中选择策略
    allowed_run_ids: List[str]

    def to_llm_payload(self) -> Dict[str, Any]:
        """你可以在调用 LLM 时使用这个结构。"""
        return {
            "system_prompt": self.system_prompt,
            "user_payload": self.brain_input,
        }
```

构建函数：

```python
# 继续 brain/context_builder.py

def build_brain_context_for_symbol(
    *,
    symbol: str,
    label_id: str,
    registry: ExperimentRegistry,
    prompt_lib: PromptLibrary,
    execution_prompt_id: str,
    account_state: Dict[str, Any],
    market_state: Dict[str, Any],
    min_trades: int = 30,
    min_sharpe: float = -10.0,
    top_k: int = 5,
) -> BrainContext:
    """
    入口：给某个 symbol 构建一份 AI 大脑可用的上下文。
    - 从 ExperimentRegistry 拉历史实验
    - 过滤 + 排序，生成 summary_bundle
    - 从 PromptLibrary 取出 system_prompt
    - 最终返回 BrainContext
    """

    # 1) 拉历史实验
    exps = registry.query(symbol=symbol, label_id=label_id)
    summary: ExperimentSummaryBundle | None = summarize_for_brain(
        exps,
        min_trades=min_trades,
        min_sharpe=min_sharpe,
        top_k=top_k,
    )

    if summary is None:
        # 没任何实验，AI 大脑应该更保守：只使用 rule-based / baseline 策略
        allowed_run_ids: List[str] = []
        summary_dict: Dict[str, Any] = {
            "symbol": symbol,
            "label_id": label_id,
            "n_total": 0,
            "n_usable": 0,
            "natural_summary": (
                f"No prior experiments for {symbol}/{label_id}. "
                "AI Brain should behave as if no learned strategies exist."
            ),
            "top_items": [],
        }
    else:
        summary_dict = summary.to_dict()
        allowed_run_ids = [item["run_id"] for item in summary_dict["top_items"]]

    # 2) 检查 prompt id 合法性
    if not prompt_lib.has(execution_prompt_id):
        raise PromptNotFoundError(
            f"Execution prompt id '{execution_prompt_id}' not found in PromptLibrary"
        )

    system_prompt = prompt_lib.get_prompt(execution_prompt_id)

    # 3) 构造给 LLM 的“脑内状态输入”
    brain_input: Dict[str, Any] = {
        "symbol": symbol,
        "label_id": label_id,
        "account_state": account_state,   # 账户持仓 / 现金 / 风险敞口
        "market_state": market_state,     # 当前价格 / 波动率 / session / news_signal 等
        "experiments_summary": summary_dict,
        "allowed_run_ids": allowed_run_ids,
        "rules": {
            "must_use_allowed_run_ids_only": True,
            "fallback_if_no_runs": "use_baseline_or_hold_bias",
        },
    }

    return BrainContext(
        execution_prompt_id=execution_prompt_id,
        system_prompt=system_prompt,
        brain_input=brain_input,
        allowed_run_ids=allowed_run_ids,
    )
```

> 这一步完成之后，**AI 大脑每次被调用的“眼睛输入”就固定成：**

* 哪个 symbol / label；
* 账户和市场状态；
* 针对该 symbol/label 的历史实验摘要（包括推荐的 run_id 列表）；
* 一个**清楚写明“只能从 allowed_run_ids 里选”的规则块**。

---

## 五、下一小步：BrainDecision schema（结构化决策）

最后补一块（骨架即可）：给 AI 大脑的 **输出** 定义一个硬 Schema，确保它：

* 只能选择我们给它的 `allowed_run_ids`；
* 只能选 whitelist 里的 `execution_prompt_id`（其实已经在输入层控制了）；
* 只能输出规定的 action 字段。

新建 `brain/decision_types.py`：

```python
# brain/decision_types.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional


ActionType = Literal["HOLD", "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "REDUCE", "INCREASE"]


@dataclass
class BrainDecision:
    symbol: str
    ts: str
    execution_prompt_id: str
    selected_run_id: Optional[str]
    action: ActionType
    size_fraction: float   # 相对于允许的 max_position_fraction
    rationale_short: str
    meta: Dict[str, Any]

    @classmethod
    def from_llm_dict(cls, obj: Dict[str, Any], allowed_run_ids: List[str], allowed_prompt_ids: List[str]) -> "BrainDecision":
        # 这里做严格校验，任何不符合的字段直接 raise
        symbol = str(obj.get("symbol", "")).upper()
        ts = str(obj.get("ts", ""))

        execution_prompt_id = obj.get("execution_prompt_id")
        if execution_prompt_id not in allowed_prompt_ids:
            raise ValueError(f"execution_prompt_id '{execution_prompt_id}' not in allowed_prompt_ids")

        selected_run_id = obj.get("selected_run_id")
        if selected_run_id is not None and selected_run_id not in allowed_run_ids:
            raise ValueError(f"selected_run_id '{selected_run_id}' not in allowed_run_ids")

        action = obj.get("action")
        if action not in ["HOLD", "OPEN_LONG", "OPEN_SHORT", "CLOSE_LONG", "CLOSE_SHORT", "REDUCE", "INCREASE"]:
            raise ValueError(f"Invalid action '{action}'")

        size_fraction = float(obj.get("size_fraction", 0.0))
        if not (0.0 <= size_fraction <= 1.0):
            raise ValueError(f"size_fraction must be in [0,1], got {size_fraction}")

        rationale_short = str(obj.get("rationale_short", "")).strip()
        meta = obj.get("meta") or {}

        return cls(
            symbol=symbol,
            ts=ts,
            execution_prompt_id=execution_prompt_id,
            selected_run_id=selected_run_id,
            action=action,  # type: ignore[arg-type]
            size_fraction=size_fraction,
            rationale_short=rationale_short,
            meta=meta,
        )
```

> 这样你最终的流水线就是：

1. **PI → HPO → ExperimentRegistry**：你已经打通了（我们刚补了 `run_pi_hpo_and_log`）。
2. **ExperimentRegistry → ExperimentSummary → BrainContext**：通过 `summarize_for_brain` + `build_brain_context_for_symbol` 完成。
3. **Brain（LLM）→ BrainDecision（硬校验）**：LLM 输出 JSON → `BrainDecision.from_llm_dict()` 做严格验证 → 再交给执行层。

---

## 接下来怎么走？

按你的要求：“不要问你要干什么，按计划一步一步落地”，我会按这个顺序继续：

1. ✅（已经做）PI → HPO → ExperimentRegistry 链路梳理 +代码骨架
2. ✅（本次）ExperimentRegistry 查询 + ExperimentSummary + BrainContext + Prompt 白名单 + BrainDecision 骨架
3. 下一步我会继续回到 **“数据 & 特征 & 标签”** 那一层，把：

   * LabelRegistry 的落地（包括 path-dependent label，比如 triple barrier）
   * Sandbox 特征的 SQL 安全检查器
   * ClickHouse 侧统计的 DatasetSanity 优化
     系统化做完。

你这边可以先把这几个新模块按需要塞进 repo 里，名字可以照抄，也可以微调；有任何 import/字段不匹配导致的具体报错，你直接把错误贴给我，我们就按错误一行一行对齐。
好，那我直接继续往**“数据 & 特征 & 标签”这一层**砸下去，把之前说的三块真的落地成代码骨架（你可以直接丢进 repo 里改字段名）：

1. **LabelRegistry**：统一管理 label 定义，防止泄露 & 保证可复现。
2. **Sandbox 特征**：让 AI 在安全语法沙箱里“发明”新 SQL 特征。
3. **DatasetSanity 的 ClickHouse 优化**：重度统计放到 ClickHouse，Python 只做轻量校验 + 采样。

之后再把这些**接回 PI → HPO → ExperimentRegistry** 链路。

---

## 一、LabelRegistry：统一的标签合约（防泄露）

目标：

* **禁止** LLM 自己写 `target` SQL。
* 所有 label 必须在注册表中声明：`id / horizon / type / sql_expr`。
* 支持简单 label（forward return）+ 留钩子给后面 `triple_barrier`。

### 1.1 类型定义

`labels/types.py`：

```python
# labels/types.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional


LabelType = Literal["forward_return_simple", "classification_threshold", "triple_barrier"]


@dataclass
class LabelDef:
    id: str                     # 例如 "ret_10m"
    type: LabelType
    horizon_sec: int            # 600, 1800, ...
    base_column: str            # 通常是 "close"
    description: str
    # 对于简单 label，给出 ClickHouse SQL 片段（不含 AS）
    # 例如 "log(close_fwd / close)"
    ch_expr_template: str
    # 未来可以扩展 triple barrier 的参数
    params: Dict[str, Any]

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "LabelDef":
        return cls(
            id=obj["id"],
            type=obj["type"],
            horizon_sec=int(obj["horizon_sec"]),
            base_column=obj["base_column"],
            description=obj.get("description", ""),
            ch_expr_template=obj["ch_expr_template"],
            params=obj.get("params", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "horizon_sec": self.horizon_sec,
            "base_column": self.base_column,
            "description": self.description,
            "ch_expr_template": self.ch_expr_template,
            "params": self.params,
        }
```

### 1.2 Registry：从 JSON 读写 + 提供安全 API

`labels/registry.py`：

```python
# labels/registry.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .types import LabelDef


class LabelNotFoundError(KeyError):
    pass


class LabelRegistry:
    """标签注册表：统一 label 定义，禁止随意改 SQL."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"Label registry file not found: {self._path}")

        with self._path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        items = raw.get("labels") or []
        self._labels: Dict[str, LabelDef] = {}
        for obj in items:
            ld = LabelDef.from_dict(obj)
            self._labels[ld.id] = ld

    def list_ids(self) -> List[str]:
        return sorted(self._labels.keys())

    def get(self, label_id: str) -> LabelDef:
        try:
            return self._labels[label_id]
        except KeyError as e:
            raise LabelNotFoundError(f"Label '{label_id}' not found") from e
```

### 1.3 Label → ClickHouse SQL 片段（无泄露）

我们约定所有 label 都是**未来 N 秒的 return**，用 `lead` 或 `anyLast` 类函数实现，但你现在已经有自己的 bar ETL，我用一个通用模板，你可以对齐你的 schema：

`labels/ch_expr.py`：

```python
# labels/ch_expr.py

from __future__ import annotations

from typing import Dict

from .types import LabelDef


def build_label_expr(label: LabelDef, *, time_col: str = "ts", base_table_alias: str = "") -> str:
    """
    给定 LabelDef，返回 ClickHouse 的 label 表达式片段（不带 AS）。

    NOTE:
    - 这里只处理 simple forward_return 类型
    - triple_barrier 留接口，不在这里实现路径依赖逻辑
    """

    if label.type == "forward_return_simple":
        # 假设你有 align 好的未来 close 列，比如在 VIEW 里已经计算好了 close_fwd_600s
        # 或者你用 window 函数 LEAD(close, N) OVER ...
        # 这里我们仅替换模板里的 {base_column} 和 {horizon_sec}
        expr = label.ch_expr_template.format(
            base_column=label.base_column,
            horizon_sec=label.horizon_sec,
        )
        return expr

    if label.type == "classification_threshold":
        expr = label.ch_expr_template.format(
            base_column=label.base_column,
            horizon_sec=label.horizon_sec,
            **label.params,
        )
        return expr

    if label.type == "triple_barrier":
        # 建议这里直接调用你后面打算实现的 triple-barrier 视图或 UDF
        expr = label.ch_expr_template.format(
            base_column=label.base_column,
            horizon_sec=label.horizon_sec,
            **label.params,
        )
        return expr

    raise ValueError(f"Unsupported label type: {label.type}")
```

### 1.4 一个简单的 label_registry.json 示例

```json
{
  "_comment": "Label registry v1.0",
  "labels": [
    {
      "id": "ret_10m_log",
      "type": "forward_return_simple",
      "horizon_sec": 600,
      "base_column": "close",
      "description": "Log-return over next 10 minutes based on close price",
      "ch_expr_template": "log( close_fwd_{horizon_sec} / {base_column} )",
      "params": {}
    },
    {
      "id": "ret_10m_updown",
      "type": "classification_threshold",
      "horizon_sec": 600,
      "base_column": "close",
      "description": "1 if log-return > up_th, -1 if < dn_th, else 0",
      "ch_expr_template": "multiIf( log(close_fwd_{horizon_sec}/{base_column}) > {up_th}, 1, log(close_fwd_{horizon_sec}/{base_column}) < {dn_th}, -1, 0 )",
      "params": {
        "up_th": 0.002,
        "dn_th": -0.002
      }
    }
  ]
}
```

> ✅ 这样一来：
>
> * **Label 定义集中**在一个 JSON，便于治理。
> * PI / HPO / Brain 只能用 `label_id`，不能改里面的 SQL。
> * 以后你做 triple barrier，只要把 ch_expr_template 换成对 triple-barrier 视图/UDF 的调用即可。

---

## 二、Sandbox 特征：让 AI 在“安全 SQL 沙箱”里发挥

目标：

* 允许 PI/AI 提议新的 `sql_expr`，但必须通过**安全检查**：

  * 只能用允许的列名；
  * 只能用白名单函数；
  * 不能有 `;`、`DROP`、`ALTER` 等；
  * 不允许随便引用 label / future-only 列（你可以不给它这些列名）。

### 2.1 类型 + 简单 Registry

`features/sandbox_types.py`：

```python
# features/sandbox_types.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass
class SandboxFeature:
    alias: str
    sql_expr: str
    description: str
    source: str           # "pi_llm", "human", "auto_evolver", ...
    created_by: str       # email 或 "system"
    created_at: str       # ISO 时间戳

    meta: Dict[str, Any]

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "SandboxFeature":
        return cls(
            alias=obj["alias"],
            sql_expr=obj["sql_expr"],
            description=obj.get("description", ""),
            source=obj.get("source", "unknown"),
            created_by=obj.get("created_by", "unknown"),
            created_at=obj.get("created_at", ""),
            meta=obj.get("meta", {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alias": self.alias,
            "sql_expr": self.sql_expr,
            "description": self.description,
            "source": self.source,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "meta": self.meta,
        }
```

`features/sandbox_registry.py`：

```python
# features/sandbox_registry.py

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

from .sandbox_types import SandboxFeature


class SandboxFeatureRegistry:
    """用于持久化已经通过安全检查的 sandbox 特征."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            with self._path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
            items = raw.get("features") or []
        else:
            items = []
        self._features: Dict[str, SandboxFeature] = {
            obj["alias"]: SandboxFeature.from_dict(obj) for obj in items
        }

    def list_aliases(self) -> List[str]:
        return sorted(self._features.keys())

    def get(self, alias: str) -> SandboxFeature:
        return self._features[alias]

    def add_or_update(self, feat: SandboxFeature) -> None:
        self._features[feat.alias] = feat
        self._flush()

    def _flush(self) -> None:
        data = {"features": [f.to_dict() for f in self._features.values()]}
        with self._path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
```

### 2.2 Sandbox 安全检查器（第 1 版）

`features/sandbox_guard.py`：

```python
# features/sandbox_guard.py

from __future__ import annotations

import re
from typing import Iterable, List, Set


SQL_FORBIDDEN_TOKENS = [
    ";",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "INSERT",
    "UPDATE",
    "DELETE",
    "ATTACH",
    "DETACH",
    "SYSTEM",
]

# 允许的函数（你可以根据自己 ClickHouse 实战继续加）
ALLOWED_FUNCS = {
    "abs",
    "log",
    "exp",
    "sqrt",
    "pow",
    "greatest",
    "least",
    "if",
    "multiIf",
    "coalesce",
    "toFloat64",
    "toFloat32",
    "toInt32",
    "toInt64",
    "lagInFrame",     # 如果你打算允许 lag(n)
    "leadInFrame",    # 如果你打算允许 lead(n) （注意 label 泄露）
    "avg",
    "sum",
    "stddevPop",
    "stddevSamp",
    "median",
    "quantile",
    "movingAvg",
    "movingSum",
}


IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class SandboxFeatureError(ValueError):
    pass


def extract_identifiers(sql_expr: str) -> List[str]:
    """非常粗糙的标识符提取，只为做白名单检查."""
    return IDENT_RE.findall(sql_expr)


def sandbox_guard(
    sql_expr: str,
    *,
    allowed_columns: Iterable[str],
    allowed_funcs: Iterable[str] = (),
) -> None:
    """
    如果不安全则 raise SandboxFeatureError；安全则返回 None。

    检查内容：
    - 禁止出现危险关键字（DROP/ALTER/...）
    - 标识符必须属于 allowed_columns 或 ALLOWED_FUNCS
    """
    text = sql_expr.upper()
    for token in SQL_FORBIDDEN_TOKENS:
        if token in text:
            raise SandboxFeatureError(f"Forbidden token '{token}' in sandbox expression")

    allowed_columns_set: Set[str] = {c.upper() for c in allowed_columns}
    allowed_funcs_set: Set[str] = {f.upper() for f in (set(ALLOWED_FUNCS) | set(allowed_funcs))}

    idents = extract_identifiers(sql_expr)
    for ident in idents:
        up = ident.upper()
        # 简单 heuristic：函数名后面紧跟 "("，这里不做 AST 解析，只做白名单
        if up in allowed_funcs_set:
            continue
        # 数学函数 / 内置关键字略过（比如 LOG, IF 等也在 allowed_funcs）
        if up in {"AND", "OR", "NOT", "CASE", "WHEN", "THEN", "ELSE", "END"}:
            continue
        # 列名检查
        if up not in allowed_columns_set:
            raise SandboxFeatureError(
                f"Identifier '{ident}' is not in allowed columns or function whitelist"
            )
```

> 第一版比较“笨”，但优点是**简单 + 可控**，你以后可以逐渐换成 AST 解析或 SQL Linter。

### 2.3 在 PI 中使用 Sandbox 特征

假设你的 `feature_recipe["derived"]` 里有：

```json
{
  "alias": "close_vwap_spread",
  "sql_expr": "(close - vwap) / vwap",
  "mode": "sandbox"
}
```

在 `pi/feature_sql.py`（或 `PrincipalInvestigator.propose()`）中：

```python
from features.sandbox_guard import sandbox_guard
from features.sandbox_registry import SandboxFeatureRegistry
from features.sandbox_types import SandboxFeature
from datetime import datetime, timezone

# 假设已经拿到 feature_catalog 列表（ClickHouse 表所有列名）


def _process_derived_features(
    derived_specs: list[dict],
    feature_catalog: list[str],
    sandbox_registry: SandboxFeatureRegistry,
) -> list[dict]:
    """把 derived 特征里 mode=sandbox 的走一遍 guard + 写入 registry."""
    out: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()

    for d in derived_specs:
        mode = d.get("mode", "builtin")
        alias = d.get("alias")
        sql_expr = d.get("sql_expr")

        if not alias or not sql_expr:
            continue

        if mode == "sandbox":
            sandbox_guard(sql_expr, allowed_columns=feature_catalog)
            # 通过检查，写入 registry（可选）
            feat = SandboxFeature(
                alias=alias,
                sql_expr=sql_expr,
                description=d.get("description", ""),
                source=d.get("source", "pi_llm"),
                created_by=d.get("created_by", "ai_brain"),
                created_at=now,
                meta={"symbol_hint": d.get("symbol"), "table_hint": d.get("table")},
            )
            sandbox_registry.add_or_update(feat)

        out.append(d)

    return out
```

---

## 三、DatasetSanity：把重计算挪到 ClickHouse + Python 采样

你之前已经有一个 `run_dataset_sanity_checks(df, manifest)`（我们前面设计过）。现在做优化版：

1. 先让 ClickHouse 对**整个数据集**算空值率、基本统计；
2. Python 只拉一小批 sample（比如 5–10%）做相关性矩阵、简单分布分析。

### 3.1 ClickHouse 侧统计

`data/sanity_clickhouse.py`：

```python
# data/sanity_clickhouse.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

import clickhouse_connect


@dataclass
class ColumnStats:
    name: str
    n: int
    n_null: int
    n_nan: int
    n_zero: int
    mean: float | None
    stddev: float | None
    min: float | None
    max: float | None


@dataclass
class DatasetStatsCH:
    total_rows: int
    columns: List[ColumnStats]


def build_stats_sql(inner_sql: str, numeric_columns: Iterable[str]) -> str:
    parts = ["count() AS n_total"]
    for col in numeric_columns:
        parts.append(f"sum(isNull({col})) AS n_null_{col}")
        parts.append(f"sum(isNaN(toFloat64OrNull({col}))) AS n_nan_{col}")
        parts.append(f"sum({col} = 0) AS n_zero_{col}")
        parts.append(f"avg(toFloat64OrNull({col})) AS mean_{col}")
        parts.append(f"stddevPop(toFloat64OrNull({col})) AS stddev_{col}")
        parts.append(f"min({col}) AS min_{col}")
        parts.append(f"max({col}) AS max_{col}")
    select_clause = ",\n  ".join(parts)
    return f"SELECT\n  {select_clause}\nFROM (\n  {inner_sql}\n) AS sub"
```

执行并解析：

```python
# 继续 data/sanity_clickhouse.py

def compute_dataset_stats_ch(
    client: clickhouse_connect.driver.Client,
    inner_sql: str,
    numeric_columns: Iterable[str],
) -> DatasetStatsCH:
    sql = build_stats_sql(inner_sql, numeric_columns)
    df = client.query_df(sql)
    row = df.iloc[0]
    total_rows = int(row["n_total"])

    cols: List[ColumnStats] = []
    for col in numeric_columns:
        cs = ColumnStats(
            name=col,
            n=total_rows,
            n_null=int(row.get(f"n_null_{col}", 0) or 0),
            n_nan=int(row.get(f"n_nan_{col}", 0) or 0),
            n_zero=int(row.get(f"n_zero_{col}", 0) or 0),
            mean=float(row.get(f"mean_{col}")) if row.get(f"mean_{col}") is not None else None,
            stddev=float(row.get(f"stddev_{col}")) if row.get(f"stddev_{col}") is not None else None,
            min=float(row.get(f"min_{col}")) if row.get(f"min_{col}") is not None else None,
            max=float(row.get(f"max_{col}")) if row.get(f"max_{col}") is not None else None,
        )
        cols.append(cs)

    return DatasetStatsCH(total_rows=total_rows, columns=cols)
```

### 3.2 Python 采样 + 轻量 sanity 规则

`data/sanity_runner.py`（桥接 ClickHouse + Pandas）：

```python
# data/sanity_runner.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

import pandas as pd
import clickhouse_connect

from .sanity_clickhouse import compute_dataset_stats_ch, DatasetStatsCH, ColumnStats


@dataclass
class DatasetSanityResult:
    level: str            # "ok" / "warning" / "fatal"
    issues: List[str]
    ch_stats: DatasetStatsCH
    sample_df: pd.DataFrame


def evaluate_stats(stats: DatasetStatsCH, *, max_null_frac: float = 0.2, max_zero_var_frac: float = 0.9) -> Tuple[str, List[str]]:
    issues: List[str] = []

    if stats.total_rows == 0:
        return "fatal", ["Dataset has zero rows"]

    for col in stats.columns:
        null_frac = col.n_null / stats.total_rows if stats.total_rows else 0.0
        if null_frac > max_null_frac:
            issues.append(f"Column {col.name}: high null fraction={null_frac:.2f}")

        if col.stddev is not None and col.stddev == 0.0:
            # 纯常数列
            issues.append(f"Column {col.name}: zero variance")

    level = "ok"
    if any("zero variance" in msg for msg in issues) or any("high null" in msg for msg in issues):
        level = "warning"
    if stats.total_rows < 100:
        issues.append("Very few rows (<100)")
        level = "fatal"

    return level, issues


def run_dataset_sanity_with_clickhouse(
    *,
    client: clickhouse_connect.driver.Client,
    dataset_sql: str,
    numeric_columns: Iterable[str],
    sample_rows: int = 20_000,
) -> DatasetSanityResult:
    # 1) ClickHouse 全量统计
    ch_stats = compute_dataset_stats_ch(client, dataset_sql, numeric_columns)
    level, issues = evaluate_stats(ch_stats)

    # 2) 拉一小部分 sample 到 Python（用于相关性 / 分布）
    sample_sql = f"{dataset_sql}\nLIMIT {sample_rows}"
    sample_df = client.query_df(sample_sql)

    # 这里你可以在 sample_df 上调用之前写的 Python 版 DatasetSanity：
    # - 相关性矩阵
    # - label 分布
    # - 极端值检测
    # 现在先留个 TODO：
    #
    # extra_level, extra_issues = run_python_sanity(sample_df)
    # level = max(level, extra_level, key=LEVEL_ORDER.index)
    # issues.extend(extra_issues)

    return DatasetSanityResult(
        level=level,
        issues=issues,
        ch_stats=ch_stats,
        sample_df=sample_df,
    )
```

---

## 四、把这些重新接回 PI → HPO → ExperimentRegistry 链路

最后一步是**接线**，让 PI 在生成 SQL 时：

* 用 **LabelRegistry** 生成 `target` 列；
* 用 **SandboxGuard** 管 derived 特征；
* 用 **ClickHouse SanityRunner** 在训练前“体检”数据；
* 把 sanity 结果写进 ExperimentRecord。

### 4.1 更新 PrincipalInvestigator.propose 签名（加 label_id）

原来：

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
) -> PIResult:
    ...
```

建议改成：

```python
from labels.registry import LabelRegistry
from labels.ch_expr import build_label_expr
from data.sanity_runner import run_dataset_sanity_with_clickhouse
from features.sandbox_registry import SandboxFeatureRegistry
from features.sandbox_guard import sandbox_guard, SandboxFeatureError

class PrincipalInvestigator:
    def __init__(self, ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None,
                 label_registry_path: str = "config/label_registry.json",
                 sandbox_registry_path: str = "config/sandbox_features.json") -> None:
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
        self._label_registry = LabelRegistry(label_registry_path)
        self._sandbox_registry = SandboxFeatureRegistry(sandbox_registry_path)

    def propose(
        self,
        *,
        table: str,
        symbol: str,
        start_ts: str,
        end_ts: str,
        goal: str,
        label_id: str,
        guard_cfg: Optional[Dict[str, Any]] = None,
    ) -> PIResult:
        catalog = _fetch_catalog(self._client, table)

        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"特征: {', '.join(catalog)}\n"
            f"标签: {label_id}\n"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {})
        tplan = plan.get("training_plan", {})

        # 1) 标签固定从 LabelRegistry 拿
        label_def = self._label_registry.get(label_id)
        target_expr = build_label_expr(label_def)
        target_alias = label_id

        # 2) 处理 derived 特征里的 sandbox
        derived = recipe.get("derived") or []
        processed_derived = _process_derived_features(
            derived_specs=derived,
            feature_catalog=catalog,
            sandbox_registry=self._sandbox_registry,
        )
        recipe["derived"] = processed_derived

        # 3) 构建 SELECT SQL，强制使用我们的 target_expr
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=target_alias,           # 这里传 label 别名
            target_expr=target_expr,       # 你需要在 build_select_sql 中增加这个参数
            feature_catalog=catalog,
        )

        # 4) 在训练前先跑 ClickHouse 体检 + 采样
        # numeric_columns 可以从 recipe 里推，也可以简单用 catalog 里已知的数值列映射
        numeric_columns = recipe.get("numeric_columns", catalog)  # 先简单用 catalog 替代
        sanity = run_dataset_sanity_with_clickhouse(
            client=self._client,
            dataset_sql=sql,
            numeric_columns=numeric_columns,
        )

        if sanity.level == "fatal":
            # 这里可以选择 raise，或者返回一个 special PIResult
            raise RuntimeError(f"Dataset sanity fatal: {sanity.issues}")

        # 通过 sanity 检查后才真正拉全量数据
        df = self._client.query_df(sql)

        # ... 构造 HPO grid（同之前） ...
        grid = {
            "model_type": [],
            "feature_subset": [recipe.get("base_features", []) + [d.get("alias") for d in processed_derived]],
            "task": [tplan.get("task", "reg")],
            "decision": [tplan.get("decision", {"mode": "quantile", "q_long": 0.7, "q_short": 0.3})],
            "model_params": [],
        }
        for cand in tplan.get("model_candidates", []) or []:
            grid["model_type"].append(cand.get("model_type", "xgb"))
            grid["model_params"].append(cand.get("model_params", {}))

        guard = (guard_cfg or {}) | {"allow_params": ["model_type", "feature_subset", "task", "decision", "model_params"]}
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
        )
```

> 这里的 `build_select_sql` 需要你稍微改一下：
>
> * 支持 `target_expr` 参数，把 label 的 SQL 拼进去；
> * 确保 label 列不会被当成特征再用回来（防止数据泄露）。

---

## 接下来我会做什么？

你刚才说要针对三层继续落地，我现在在**第一层（数据/特征/标签）**已经给了：

1. LabelRegistry + LabelDef + ClickHouse 表达式生成；
2. Sandbox 特征 + 安全检查 + Registry；
3. DatasetSanity 的 ClickHouse 优化 + Python 采样；
4. 并把这三块重新嵌回 PI → HPO → ExperimentRegistry 的入口 `PrincipalInvestigator.propose()`。

下一轮我会转回第二层和第三层：

* **第二层（ML/HPO/实验治理）**：

  * 补充 DatasetManifest（写入 ExperimentRecord）
  * 给 HPO / Backtest 写统一的 experiment logging 封装（不散落到处写 JSON）

* **第三层（AI 大脑 / Prompt 安全）**：

  * 把 `BrainDecision` 用在一个简单的 “mock live loop” 里，给出一条 end-to-end 的伪代码：

    * 从 registry 选策略
    * 构建 BrainContext
    * 调 LLM
    * 校验 BrainDecision
    * 输出标准化的交易意图

你可以先不改任何现有逻辑，把这些新文件（labels/*, features/*, data/*, brain/*）放进去，看 import 报错，我们再对齐。
现在我继续往下走。
