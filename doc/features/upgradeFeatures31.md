好，那我们现在**真的把 PI → HPO → ExperimentRegistry 这一条链打通**，做到：

* 每一次 AI 大脑发起的实验，都会变成一条**可追溯的 ExperimentRecord**；
* HPO 不再是“黑匣子脚本”，而是有**统一记录、统一 ID** 的实验；
* 为后面的 **Brain 决策（选 run_id，而不是瞎想新策略）**打好地基。

我会分 3 步来落地：

1. `experiments/types.py`：统一的 Experiment 数据结构。
2. `experiments/registry.py`：JSONL 版 ExperimentRegistry，支持 `log_run()` + 查询。
3. HPO 集成示例：在你现有的 `wf_hpo.py` 或类似文件里**插一个最小 patch**，在每个 trial 结束时写入 ExperimentRegistry。

最后再加一个小小的**BrainDecision schema 雏形**，为后面的 Phase 4A 预留接口。

---

## 一、Experiment 数据结构（experiments/types.py）

这个文件就是**所有实验记录的标准格式**。

> 新建：`experiments/types.py`

```python
# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Any, Dict, List, Optional


# ---------- 1) Metric 集合 ----------

@dataclass
class MetricSet:
    """
    一次模型评估的一组指标。

    你可以只填你有的字段，extra 用来挂任何额外指标：
    - 回测指标：sharpe, sortino, max_dd, n_trades, pnl
    - 分类指标：accuracy, f1, auc, precision, recall
    - 回归指标：rmse, mae, r2, ic 等
    """

    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_dd: Optional[float] = None
    pnl: Optional[float] = None
    n_trades: Optional[int] = None

    accuracy: Optional[float] = None
    f1: Optional[float] = None
    auc: Optional[float] = None
    precision: Optional[float] = None
    recall: Optional[float] = None

    rmse: Optional[float] = None
    mae: Optional[float] = None
    r2: Optional[float] = None
    ic: Optional[float] = None  # 信息系数

    extra: Dict[str, Any] = field(default_factory=dict)


# ---------- 2) Dataset 元信息 ----------

@dataclass
class DatasetMeta:
    """
    本次实验用到的数据集概况（非数据内容），方便事后诊断：

    - table: ClickHouse 源表
    - symbol: 交易标的
    - timeframe: bar 级别，比如 "1m", "5s"
    - rows / cols: 样本数 & 特征列数
    - start_ts / end_ts: 时间范围（字符串即可）
    - label_id: 使用的标签 ID（来自 LabelRegistry）
    """

    table: str
    symbol: str
    timeframe: Optional[str]
    rows: int
    cols: int
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    label_id: Optional[str] = None
    horizon_bars: Optional[int] = None

    extra: Dict[str, Any] = field(default_factory=dict)


# ---------- 3) Model 配置 ----------

@dataclass
class ModelConfig:
    """
    描述当前 run 的模型配置（单个 trial）：

    - model_type: 比如 "xgb", "lgbm", "ridge", "stacking"
    - model_params: 超参数字典（来自 HPO）
    - feature_subset: 实际参与训练的特征名列表
    """

    model_type: str
    model_params: Dict[str, Any]
    feature_subset: List[str] = field(default_factory=list)
    task: Optional[str] = None  # "reg" or "cls"
    decision: Optional[Dict[str, Any]] = None  # 比如 quantile 决策配置

    extra: Dict[str, Any] = field(default_factory=dict)


# ---------- 4) ExperimentRecord ----------

@dataclass
class ExperimentRecord:
    """
    一个 ExperimentRecord = 一次完整的训练 + 评估试验（通常对应 HPO 的一个 trial）。

    关键字段：
    - run_id: 全局唯一 ID（study_name + trial_index + 时间戳）
    - experiment_name: 逻辑分组名，比如 "tsla_1m_ret10m_20251118"
    - created_at: ISO 时间
    - pi_version / brain_mode: 方便 future 回溯（可选）

    - dataset: DatasetMeta
    - model: ModelConfig
    - feature_recipe / training_plan: 来自 PI 的原始 JSON（方便复现）
    - metrics_train/val/test: 指标
    - tags: 方便后面 AI 过滤
    """

    run_id: str
    experiment_name: str
    created_at: str

    symbol: str
    timeframe: Optional[str]

    pi_version: Optional[str] = None
    brain_mode: Optional[str] = None  # 比如 "v3_fast_market_following_tactical_20251117"

    dataset: DatasetMeta = None  # type: ignore
    model: ModelConfig = None  # type: ignore

    feature_recipe: Dict[str, Any] = field(default_factory=dict)
    training_plan: Dict[str, Any] = field(default_factory=dict)

    metrics_train: Optional[MetricSet] = None
    metrics_val: Optional[MetricSet] = None
    metrics_test: Optional[MetricSet] = None

    tags: List[str] = field(default_factory=list)
    notes: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """
        方便 JSON 序列化。dataclass 里嵌套 dataclass 要展开。
        """

        def conv(obj: Any) -> Any:
            if obj is None:
                return None
            if hasattr(obj, "__dataclass_fields__"):
                return asdict(obj)
            return obj

        return {
            "run_id": self.run_id,
            "experiment_name": self.experiment_name,
            "created_at": self.created_at,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pi_version": self.pi_version,
            "brain_mode": self.brain_mode,
            "dataset": conv(self.dataset),
            "model": conv(self.model),
            "feature_recipe": self.feature_recipe,
            "training_plan": self.training_plan,
            "metrics_train": conv(self.metrics_train),
            "metrics_val": conv(self.metrics_val),
            "metrics_test": conv(self.metrics_test),
            "tags": self.tags,
            "notes": self.notes,
            "extra": self.extra,
        }

    @staticmethod
    def now_iso() -> str:
        return datetime.utcnow().isoformat()
```

---

## 二、ExperimentRegistry：统一写 JSONL & 查询（experiments/registry.py）

> 新建：`experiments/registry.py`

这里用 **JSONL 文件** 做一个第一版 registry：

* 简单、可 grep / jq；
* 后面想迁移 ClickHouse 就把 `log_record()` 改成 `INSERT` 即可。

```python
# experiments/registry.py

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional

from .types import ExperimentRecord, DatasetMeta, ModelConfig, MetricSet


class ExperimentRegistry:
    """
    实验登记薄（第一版：JSONL 存储）。

    每条 ExperimentRecord 一行 JSON：
      { "run_id": "...", "experiment_name": "...", ... }

    优点：
    - 易于追加
    - 不需要 DB 迁移
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------- 1) 生成 run_id ----------

    @staticmethod
    def make_run_id(experiment_name: str, trial_index: Optional[int] = None) -> str:
        """
        run_id = {experiment_name}:{trial_index}:{YYYYMMDD-HHMMSS-ms}
        trial_index 可以为 None（例如非 HPO 单次运行）
        """
        ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        if trial_index is None:
            return f"{experiment_name}:{ts}"
        return f"{experiment_name}:{trial_index}:{ts}"

    # ---------- 2) 写入一条记录 ----------

    def log_record(self, record: ExperimentRecord) -> str:
        """
        追加一条 ExperimentRecord 到 JSONL 文件。
        返回 run_id，方便调用方记录。
        """
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        return record.run_id

    # ---------- 3) 读出所有记录（懒加载迭代器） ----------

    def iter_records(self) -> Iterator[ExperimentRecord]:
        """
        简单版本：全文件顺序扫一遍，逐条 yield。
        后面可以加缓存或 ClickHouse。
        """
        if not self._path.exists():
            return
            yield  # 让它成为 generator

        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue

                yield self._from_dict(raw)

    # ---------- 4) 带简单过滤的查询 ----------

    def query(
        self,
        *,
        symbol: Optional[str] = None,
        label_id: Optional[str] = None,
        experiment_name: Optional[str] = None,
        limit: int = 100,
    ) -> List[ExperimentRecord]:
        """
        一个简单 filter，主要给 AI 大脑用来取“候选 run_id 列表”。
        """
        results: List[ExperimentRecord] = []
        for rec in self.iter_records():
            if symbol and rec.symbol != symbol:
                continue
            if experiment_name and rec.experiment_name != experiment_name:
                continue
            if label_id:
                if not rec.dataset or rec.dataset.label_id != label_id:
                    continue
            results.append(rec)
            if len(results) >= limit:
                break
        return results

    # ---------- 5) 内部：从 dict 还原 ExperimentRecord ----------

    def _from_dict(self, raw: Dict) -> ExperimentRecord:
        # DatasetMeta
        ds_raw = raw.get("dataset") or {}
        dataset = DatasetMeta(
            table=ds_raw.get("table", ""),
            symbol=ds_raw.get("symbol", ""),
            timeframe=ds_raw.get("timeframe"),
            rows=ds_raw.get("rows", 0),
            cols=ds_raw.get("cols", 0),
            start_ts=ds_raw.get("start_ts"),
            end_ts=ds_raw.get("end_ts"),
            label_id=ds_raw.get("label_id"),
            horizon_bars=ds_raw.get("horizon_bars"),
            extra=ds_raw.get("extra") or {},
        )

        # ModelConfig
        model_raw = raw.get("model") or {}
        model = ModelConfig(
            model_type=model_raw.get("model_type", ""),
            model_params=model_raw.get("model_params") or {},
            feature_subset=model_raw.get("feature_subset") or [],
            task=model_raw.get("task"),
            decision=model_raw.get("decision"),
            extra=model_raw.get("extra") or {},
        )

        def mk_metric(mraw: Optional[Dict]) -> Optional[MetricSet]:
            if not mraw:
                return None
            return MetricSet(
                sharpe=mraw.get("sharpe"),
                sortino=mraw.get("sortino"),
                max_dd=mraw.get("max_dd"),
                pnl=mraw.get("pnl"),
                n_trades=mraw.get("n_trades"),
                accuracy=mraw.get("accuracy"),
                f1=mraw.get("f1"),
                auc=mraw.get("auc"),
                precision=mraw.get("precision"),
                recall=mraw.get("recall"),
                rmse=mraw.get("rmse"),
                mae=mraw.get("mae"),
                r2=mraw.get("r2"),
                ic=mraw.get("ic"),
                extra=mraw.get("extra") or {},
            )

        rec = ExperimentRecord(
            run_id=raw["run_id"],
            experiment_name=raw["experiment_name"],
            created_at=raw.get("created_at"),
            symbol=raw.get("symbol"),
            timeframe=raw.get("timeframe"),
            pi_version=raw.get("pi_version"),
            brain_mode=raw.get("brain_mode"),
            dataset=dataset,
            model=model,
            feature_recipe=raw.get("feature_recipe") or {},
            training_plan=raw.get("training_plan") or {},
            metrics_train=mk_metric(raw.get("metrics_train")),
            metrics_val=mk_metric(raw.get("metrics_val")),
            metrics_test=mk_metric(raw.get("metrics_test")),
            tags=raw.get("tags") or [],
            notes=raw.get("notes") or "",
            extra=raw.get("extra") or {},
        )
        return rec
```

---

## 三、HPO 集成：在 trial 结束时写入 ExperimentRegistry

这里我给你一个**最小 patch 思路**，你可以对照你自己的 `wf_hpo.py` 或 `hpo_cli_upgraded.py` 改。

假设你 HPO 流程大致是：

```python
# 伪代码结构（你已经有的）
def run_hpo(pi_result: PIResult, study_name: str):
    def objective(trial):
        # 1) 从 pi_result.hpo_grid 里取 model_type / params
        # 2) 切分 train/val/test
        # 3) 训练 & 回测
        # 4) 返回一个 score

    study = optuna.create_study(...)
    study.optimize(objective, n_trials=...)
```

现在我们做两件事：

1. 在外层创建 `ExperimentRegistry` 实例；
2. 在 `objective(trial)` 里，训练完后，创建 `ExperimentRecord` 并 `registry.log_record(record)`。

### 3.1 HPO 集成示例（片段）

> 比如你可以新建一个 `ml/hpo_runner.py`，专门负责把 PIResult + HPO + Registry 串起来：

```python
# ml/hpo_runner.py

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Tuple

import numpy as np
import optuna
from sklearn.model_selection import train_test_split

from experiments.registry import ExperimentRegistry
from experiments.types import DatasetMeta, ModelConfig, ExperimentRecord, MetricSet
from pi.principal_investigator import PIResult  # 你现有的 dataclass


def run_hpo_with_registry(
    pi_result: PIResult,
    *,
    study_name: str,
    experiment_name: str,
    registry_path: str = "experiments/records.jsonl",
    symbol: str,
    timeframe: str,
    label_id: str,
    horizon_bars: int,
    brain_mode: str = "",
    pi_version: str = "",
    n_trials: int = 20,
) -> None:
    """
    把 PI → HPO → ExperimentRegistry 串起来的一条龙。

    - pi_result: PrincipalInvestigator.propose() 的返回值
    - study_name: Optuna study 名
    - experiment_name: 用来分组的逻辑名称，比如 "tsla_1m_ret10m_20251118"
    """

    registry = ExperimentRegistry(Path(registry_path))

    df = pi_result.dataset_df
    # 约定 label 列名就是 "label"
    if "label" not in df.columns:
        raise ValueError("PIResult.dataset_df is expected to contain a 'label' column.")

    X = df.drop(columns=["label"])
    y = df["label"]

    # 这里仅示例，实际你可能有更复杂的时序切分
    X_train, X_tmp, y_train, y_tmp = train_test_split(X, y, test_size=0.4, shuffle=False)
    X_val, X_test, y_val, y_test = train_test_split(X_tmp, y_tmp, test_size=0.5, shuffle=False)

    feature_names = list(X.columns)

    # 从 training_plan/hpo_grid 里就可以拿到 model_type / param 搜索空间
    base_tplan = pi_result.training_plan
    base_grid = pi_result.hpo_grid

    def objective(trial: optuna.Trial) -> float:
        # ========== 1) 为本 trial 采样模型和参数 ==========
        # 这里只写一个简单示例：从 grid 里选一个候选，再用 trial.suggest_xxx 细化
        model_types = base_grid["model_type"]
        base_params_list = base_grid["model_params"]

        # 按 index 选择候选
        idx = trial.suggest_int("model_index", 0, len(model_types) - 1)
        model_type = model_types[idx]
        base_params = base_params_list[idx] or {}

        # 举例：对学习率做细化（真正实现你可以用你之前的 hpo 逻辑）
        lr = trial.suggest_float("learning_rate", 1e-4, 1e-1, log=True)
        params = dict(base_params)
        params["learning_rate"] = lr

        # ========== 2) 训练模型（示例，以 XGBoost 为例） ==========
        # 这里不写死具体库，只示意你在这一步构建 model，fit，然后算指标。
        # 你可以用你现有的 train_and_evaluate_xgb() 等函数。

        # 假代码：返回一个 dict，里面含 val/test 指标
        metrics_train, metrics_val, metrics_test = train_and_eval_stub(
            X_train, y_train,
            X_val, y_val,
            X_test, y_test,
            model_type=model_type,
            params=params,
        )

        # ========== 3) 写入 ExperimentRegistry ==========
        # build DatasetMeta
        dataset_meta = DatasetMeta(
            table=base_tplan.get("use_table", ""),  # 没有就空
            symbol=symbol,
            timeframe=timeframe,
            rows=len(df),
            cols=df.shape[1] - 1,  # 不计 label 列
            start_ts=base_tplan.get("start_ts"),
            end_ts=base_tplan.get("end_ts"),
            label_id=label_id,
            horizon_bars=horizon_bars,
            extra={},
        )

        # build ModelConfig
        model_cfg = ModelConfig(
            model_type=model_type,
            model_params=params,
            feature_subset=feature_names,  # 第一版可以直接用全部特征
            task=base_tplan.get("task"),
            decision=base_tplan.get("decision"),
            extra={"trial_params": trial.params},
        )

        # MetricSet 转换
        m_train = MetricSet(**metrics_train)
        m_val = MetricSet(**metrics_val)
        m_test = MetricSet(**metrics_test)

        # 生成 run_id
        run_id = registry.make_run_id(experiment_name, trial.number)

        record = ExperimentRecord(
            run_id=run_id,
            experiment_name=experiment_name,
            created_at=ExperimentRecord.now_iso(),
            symbol=symbol,
            timeframe=timeframe,
            pi_version=pi_version,
            brain_mode=brain_mode,
            dataset=dataset_meta,
            model=model_cfg,
            feature_recipe=pi_result.feature_recipe,
            training_plan=pi_result.training_plan,
            metrics_train=m_train,
            metrics_val=m_val,
            metrics_test=m_test,
            tags=[label_id, symbol, timeframe],
            notes="",
            extra={"study_name": study_name},
        )

        registry.log_record(record)

        # ========== 4) 返回优化目标（例如负的 val_loss 或 -val_rmse） ==========
        # 这里假设我们希望 maximize Sharpe，如果没有就用 -val_rmse 之类
        score = m_val.sharpe
        if score is None:
            # fallback：用 -rmse 之类
            if m_val.rmse is not None:
                score = -m_val.rmse
            else:
                # 最差情况随便给个 0，避免崩掉
                score = 0.0

        return score

    study = optuna.create_study(
        study_name=study_name,
        direction="maximize",  # 假设我们最大化 Sharpe
    )
    study.optimize(objective, n_trials=n_trials)
```

### 3.2 简单的训练 stub（你可以替换成真实实现）

```python
def train_and_eval_stub(
    X_train, y_train,
    X_val, y_val,
    X_test, y_test,
    *,
    model_type: str,
    params: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """
    这里先写一个假实现，为了把 Registry 跑通。

    - 真实项目中你用已有的 train_xgb / train_lgbm / stacking_model。
    - 返回三个 dict，字段名对 MetricSet 是兼容的即可。
    """

    # 这里随便做一个非常 naive 的示例
    import numpy as np

    # 假设我们什么也不训练，随机预测
    rng = np.random.default_rng(42)
    y_hat_val = rng.normal(size=len(y_val))
    y_hat_test = rng.normal(size=len(y_test))

    # 计算几项指标（这里只做示例）
    def mse(y_true, y_pred):
        return float(np.mean((y_true - y_pred) ** 2))

    rmse_val = mse(y_val.to_numpy(), y_hat_val) ** 0.5
    rmse_test = mse(y_test.to_numpy(), y_hat_test) ** 0.5

    metrics_train = {"rmse": None}
    metrics_val = {"rmse": rmse_val}
    metrics_test = {"rmse": rmse_test}

    return metrics_train, metrics_val, metrics_test
```

> 你真正落地的时候，只需要把 `train_and_eval_stub` 替换成你现在在用的训练函数，把你已有的 Sharpe / 回测指标塞进 `MetricSet` 就可以了，ExperimentRegistry 部分不用动。

---

## 四、为 AI 大脑预留接口：BrainDecision schema 雏形

这一步我们先不接 Broker、不接 live trading，只做一个**结构定义 + 校验**，让以后 AI 大脑输出时有参考。

> 新建：`brain/decision_types.py`

```python
# brain/decision_types.py

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


@dataclass
class SelectedRun:
    """
    AI 大脑挑选出的某个 run_id，以及它在组合里的角色/权重。
    """

    run_id: str
    weight: float  # 0~1，后续可以不要求 sum=1，但至少校验范围
    role: str = "primary"  # primary / hedge / neutral 等
    notes: str = ""


@dataclass
class BrainDecision:
    """
    LLM 输出的最终决策结构（后续会做 JSON schema + 校验）：

    - allowed_run_ids: 上游给大脑的“候选池”
    - selected: 大脑真正选出来的组合（必须是候选池子里的子集）
    """

    timestamp: str
    brain_version: str
    mode: str  # "offline_eval" / "paper_trading" / "live_trading"

    symbol: str
    timeframe: str
    label_id: str

    allowed_run_ids: List[str] = field(default_factory=list)
    selected: List[SelectedRun] = field(default_factory=list)

    rationale: str = ""  # 大脑自己的总结

    meta: Dict[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        """
        一些基础校验，防止大脑越界：
        - selected.run_id 必须在 allowed_run_ids 里
        - weight 合法
        """
        allowed = set(self.allowed_run_ids)
        if not allowed:
            raise ValueError("BrainDecision.allowed_run_ids cannot be empty")

        for s in self.selected:
            if s.run_id not in allowed:
                raise ValueError(
                    f"Selected run_id {s.run_id} not in allowed_run_ids"
                )
            if not (0.0 <= s.weight <= 1.0):
                raise ValueError(
                    f"Selected weight must be in [0,1], got {s.weight} for {s.run_id}"
                )

    @staticmethod
    def now_iso() -> str:
        return datetime.utcnow().isoformat()
```

以后你在 **Phase 4A** 可以这样串起来：

1. 用 `ExperimentRegistry.query(symbol="TSLA", label_id="ret_10m_reg")` 拉 50 条 run；
2. 做一个 summary（比如按 Sharpe 排序 top 10）；
3. 把这些 run 的简要信息 + run_id 列表喂给 LLM，要求 LLM 输出严格 JSON（匹配 BrainDecision schema）；
4. 用 `BrainDecision.validate()` 做硬校验，防止越界；
5. 把 `decision.selected` 交给下一层 orchestrator 去跑（paper 或 live）。

---

## 到这里，PI → HPO → ExperimentRegistry 基本打通了

现在这条链路已经具备：

* PI：已经有 **Label Guard + Sandbox 特征**（在你前面那一轮我们已经设计完）；
* HPO：可以通过 `run_hpo_with_registry()` 把每个 trial 记到 `experiments/records.jsonl`；
* Registry：后面 AI 大脑可以通过 `ExperimentRegistry.query()` 找到过去最好的 run，做组合。

---

如果你愿意，下一步我可以继续：

* 给 `ExperimentRegistry` 写一个 **summary builder**，专门给 LLM 准备 briefing（每条 run 一两行摘要）；
* 或者直接开始设计 **“Brain Context Builder”**：从 Registry → 构建 LLM prompt 输入（包括你之前的 prompt_library.json、DatasetSanity 报告等）。

你可以继续回我一句「继续」，我就往这条线（AI 大脑 & Prompt 安全）上接着往下铺。
好，我们现在开始把 **「AI 大脑层」真正管上 ExperimentRegistry**，先给它准备好两样东西：

1. **实验总结器（Experiment Summary）**：把 JSONL 里的一堆 run，压成几行给 LLM 看得懂的 briefing；
2. **大脑上下文构建器（Brain Context Builder）**：把「prompt + 实验摘要 + allowed_run_ids + 数据体检结果」打成一个统一的上下文结构，交给调用 LLM 的 orchestrator。

这一轮我直接给你**可以落地的代码骨架**，你只用按文件名拷进去，然后后面我们再慢慢把细节接上你现有的训练代码。

---

## 一、Experiment Summary：给 AI 大脑看的「run 总结」

> 新建：`experiments/summary.py`

目标：

* 从 `ExperimentRegistry` 取到 `List[ExperimentRecord]`；
* 按某个“主指标”（比如 Sharpe 或 -RMSE）排序；
* 生成：

  * `allowed_run_ids`：候选池；
  * `summary_text`：给 LLM 看的短摘要（自然语言）；
  * `table`：如果你想在日志中打印一个可读表。

```python
# experiments/summary.py

from __future__ import annotations

from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from .types import ExperimentRecord, MetricSet


def _primary_score(rec: ExperimentRecord) -> Tuple[str, float]:
    """
    为排序选一个主指标，并返回 (metric_name, score).

    策略：
    1. 有 val Sharpe → 用 Sharpe
    2. 否则有 val IC → 用 IC
    3. 否则有 val RMSE → 用 -RMSE（越小越好）
    4. 否则 0
    """
    m = rec.metrics_val
    if m is None:
        return "none", 0.0

    if m.sharpe is not None:
        return "sharpe", float(m.sharpe)
    if m.ic is not None:
        return "ic", float(m.ic)
    if m.rmse is not None:
        return "neg_rmse", float(-m.rmse)

    return "none", 0.0


def rank_experiments(
    records: List[ExperimentRecord],
    *,
    max_items: int = 20,
) -> List[Tuple[ExperimentRecord, str, float]]:
    """
    按主指标从高到低排序，返回 [(rec, metric_name, score), ...].
    """
    scored: List[Tuple[ExperimentRecord, str, float]] = []
    for rec in records:
        metric_name, score = _primary_score(rec)
        scored.append((rec, metric_name, score))

    scored.sort(key=lambda x: x[2], reverse=True)
    return scored[:max_items]


def build_experiment_summary_text(
    ranked: List[Tuple[ExperimentRecord, str, float]],
    *,
    symbol: Optional[str] = None,
    label_id: Optional[str] = None,
) -> str:
    """
    把 ranked 实验列表压缩成一段给 LLM 看的自然语言摘要。
    """

    if not ranked:
        return "No historical experiments found for this configuration."

    lines: List[str] = []
    header = []

    if symbol:
        header.append(f"Symbol={symbol}")
    if label_id:
        header.append(f"Label={label_id}")

    if header:
        lines.append("Historical experiments (" + ", ".join(header) + "):")
    else:
        lines.append("Historical experiments:")

    for idx, (rec, metric_name, score) in enumerate(ranked, start=1):
        m = rec.metrics_val or MetricSet()
        sharpe = f"{m.sharpe:.2f}" if m.sharpe is not None else "n/a"
        ic = f"{m.ic:.3f}" if m.ic is not None else "n/a"
        rmse = f"{m.rmse:.4f}" if m.rmse is not None else "n/a"
        n_trades = (
            str(m.n_trades) if m.n_trades is not None else "n/a"
        )

        lines.append(
            f"{idx}. run_id={rec.run_id} | model={rec.model.model_type} | "
            f"metric={metric_name}={score:.4f} | Sharpe={sharpe} | "
            f"IC={ic} | RMSE={rmse} | trades={n_trades}"
        )

    return "\n".join(lines)


def build_experiment_briefing(
    records: List[ExperimentRecord],
    *,
    symbol: Optional[str] = None,
    label_id: Optional[str] = None,
    max_items: int = 20,
) -> Dict[str, any]:
    """
    统一输出结构，给 AI 大脑的 context builder 使用。

    返回：
    {
      "allowed_run_ids": [...],
      "summary_text": "....",
      "ranked": [
         {
            "run_id": "...",
            "score_metric": "...",
            "score": 1.23,
            "model_type": "...",
            "timeframe": "...",
            "label_id": "...",
            "sharpe": ...,
            ...
         },
         ...
      ]
    }
    """

    ranked = rank_experiments(records, max_items=max_items)

    allowed_run_ids = [rec.run_id for rec, _, _ in ranked]
    summary_text = build_experiment_summary_text(
        ranked, symbol=symbol, label_id=label_id
    )

    ranked_table: List[Dict[str, any]] = []
    for rec, metric_name, score in ranked:
        m = rec.metrics_val or MetricSet()
        ranked_table.append(
            {
                "run_id": rec.run_id,
                "symbol": rec.symbol,
                "timeframe": rec.timeframe,
                "label_id": rec.dataset.label_id if rec.dataset else None,
                "model_type": rec.model.model_type,
                "score_metric": metric_name,
                "score": score,
                "sharpe": m.sharpe,
                "ic": m.ic,
                "rmse": m.rmse,
                "n_trades": m.n_trades,
                "tags": rec.tags,
            }
        )

    return {
        "allowed_run_ids": allowed_run_ids,
        "summary_text": summary_text,
        "ranked": ranked_table,
    }
```

这个文件现在完成了：

* 从 `ExperimentRegistry.query()` 拿出来的 `records`；
* 变成 `allowed_run_ids` + LLM 可读的 `summary_text`；
* 保留一个结构化的 `ranked` 方便日志 or UI。

---

## 二、PromptLibrary 封装：给大脑一个「白名单 prompt」接口

你的 `prompt` JSON 已经非常完整了，我们只需要：

* 做一个轻量的 loader，加载 JSON 文件；
* 做一个 `PromptLibrary` 类：**只允许通过 ID 取 prompt**，杜绝“免费拼 prompt”。

> 新建：`brain/prompt_library.py`

```python
# brain/prompt_library.py

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass
class PromptMeta:
    prompt_id: str
    created_at: Optional[str] = None
    source: Optional[str] = None
    base_id: Optional[str] = None
    reviewer: Optional[str] = None
    signature: Optional[str] = None
    version: Optional[int] = None


class PromptLibrary:
    """
    对 prompt_library.json 的一个安全封装。

    使用方式：
      lib = PromptLibrary.from_file("prompt_library.json")
      system_prompt = lib.get_prompt("v3_fast_market_following_tactical_20251117")
      meta = lib.get_meta("v3_fast_market_following_tactical_20251117")
    """

    def __init__(self, raw: Dict) -> None:
        self._raw = raw
        self._prompts: Dict[str, str] = {}

        # 顶层的一些通用 ID (v3_stable, conservative, aggressive_scalper...)
        for key, value in raw.items():
            if key.startswith("_"):
                continue
            if key in ("prompts", "meta"):
                continue
            if isinstance(value, str):
                self._prompts[key] = value

        # 嵌套 prompts 下的 ID
        for key, value in (raw.get("prompts") or {}).items():
            if isinstance(value, str):
                self._prompts[key] = value

        self._meta_raw: Dict[str, Dict] = raw.get("meta") or {}

    @classmethod
    def from_file(cls, path: str | Path) -> "PromptLibrary":
        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return cls(raw)

    def get_prompt(self, prompt_id: str) -> str:
        """
        如果 prompt_id 不存在，抛异常（不 silent fallback），防止大脑传错 ID。
        """
        if prompt_id not in self._prompts:
            raise KeyError(f"Prompt id not found: {prompt_id}")
        return self._prompts[prompt_id]

    def get_meta(self, prompt_id: str) -> PromptMeta:
        m = self._meta_raw.get(prompt_id) or {}
        return PromptMeta(
            prompt_id=prompt_id,
            created_at=m.get("created_at"),
            source=m.get("source"),
            base_id=m.get("base_id"),
            reviewer=m.get("reviewer"),
            signature=m.get("signature"),
            version=m.get("version"),
        )

    def list_ids(self) -> Dict[str, str]:
        """
        方便 debug：返回 {id: first40chars} 字典。
        """
        return {k: v[:40] for k, v in self._prompts.items()}
```

现在，大脑 orchestrator 想用 prompt 必须通过：

```python
lib = PromptLibrary.from_file("config/prompt_library.json")
system_prompt = lib.get_prompt(brain_mode_id)
meta = lib.get_meta(brain_mode_id)
```

不允许 runtime 拼接不在白名单里的新 system prompt——**大脑的“人格”和“风格”被配置锁死**。

---

## 三、Brain Context Builder：把「prompt + experiments + 数据体检」拼成大脑输入

现在我们组合：

* `PromptLibrary`：选 system prompt；
* `ExperimentRegistry` + `build_experiment_briefing()`：给大脑一份「过去表现最好策略」摘要；
* `DatasetSanity` 报告（你前面已经有模块；这里假定返回的是 dict）。

> 新建：`brain/context_builder.py`

```python
# brain/context_builder.py

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, List, Optional

from experiments.registry import ExperimentRegistry
from experiments.summary import build_experiment_briefing
from experiments.types import ExperimentRecord
from brain.prompt_library import PromptLibrary


def _format_dataset_warnings(sanity_report: Optional[Dict[str, Any]]) -> str:
    """
    把 DatasetSanity 报告压成给 LLM 看的一段文字。
    这里假定你之前的 DatasetSanity 返回类似结构：
    {
      "ok": bool,
      "issues": [
         {"level": "error"|"warn", "code": "HIGH_NAN", "msg": "..."},
         ...
      ],
      "stats": {...}
    }
    """
    if not sanity_report:
        return "No dataset sanity report provided."

    if sanity_report.get("ok", True):
        base = "Dataset sanity check: OK. No blocking issues detected."
    else:
        base = "Dataset sanity check: WARNING. Some issues detected."

    issues = sanity_report.get("issues") or []
    if not issues:
        return base

    lines = [base, "Key issues:"]
    for item in issues[:10]:  # 至多列 10 条，避免爆 context
        level = item.get("level", "warn").upper()
        code = item.get("code", "UNKNOWN")
        msg = item.get("msg", "")
        lines.append(f"- [{level}] {code}: {msg}")

    return "\n".join(lines)


def build_brain_context(
    *,
    symbol: str,
    timeframe: str,
    label_id: str,
    brain_mode_id: str,
    prompt_library: PromptLibrary,
    experiment_registry: ExperimentRegistry,
    sanity_report: Optional[Dict[str, Any]] = None,
    max_history_runs: int = 20,
) -> Dict[str, Any]:
    """
    构造 AI 大脑调用 LLM 时需要的上下文对象。

    返回结构示例：
    {
      "system_prompt": "...",
      "user_prompt": "...",   # 你可以在 orchestrator 再拼承载 runtime feature snapshot
      "allowed_run_ids": [...],
      "experiments_summary_text": "...",
      "dataset_sanity_text": "...",
      "brain_mode_id": "...",
      "symbol": "...",
      "timeframe": "...",
      "label_id": "...",
    }
    """

    # 1) 选 system prompt
    system_prompt = prompt_library.get_prompt(brain_mode_id)

    # 2) 查询历史实验
    records: List[ExperimentRecord] = experiment_registry.query(
        symbol=symbol,
        label_id=label_id,
        # experiment_name 可选：例如 "tsla_1m_ret10m_20251118"
        limit=max_history_runs,
    )

    briefing = build_experiment_briefing(
        records,
        symbol=symbol,
        label_id=label_id,
        max_items=max_history_runs,
    )

    # 3) 数据体检摘要
    dataset_sanity_text = _format_dataset_warnings(sanity_report)

    # 4) 用户 prompt（只放「任务说明」，行情 snapshot 由 orchestrator 动态拼）
    user_prompt_header = (
        f"You are deciding which previously trained strategy run(s) "
        f"to activate for symbol={symbol}, timeframe={timeframe}, label={label_id}.\n\n"
        "You MUST only select from the provided allowed_run_ids.\n"
        "Do NOT invent new run_id or new models.\n"
    )

    user_prompt_body = (
        "==== Historical Strategy Performance ====\n"
        f"{briefing['summary_text']}\n\n"
        "==== Dataset Sanity Report ====\n"
        f"{dataset_sanity_text}\n"
    )

    # orchestrator 在真正调用 LLM 时，可以再 append:
    #   - 当前市场状态快照摘要
    #   - 当前持仓 / 账户风险约束
    # 这里先只关心“静态策略选择”相关内容。

    user_prompt = user_prompt_header + "\n" + user_prompt_body

    ctx: Dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "brain_mode_id": brain_mode_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "label_id": label_id,
        "allowed_run_ids": briefing["allowed_run_ids"],
        "experiments_ranked": briefing["ranked"],
        "dataset_sanity_text": dataset_sanity_text,
    }
    return ctx
```

这样，你的大脑 orchestrator 调用链会变成：

```python
from brain.prompt_library import PromptLibrary
from brain.context_builder import build_brain_context
from experiments.registry import ExperimentRegistry
from brain.decision_types import BrainDecision, SelectedRun

# 1) 初始化
prompt_lib = PromptLibrary.from_file("config/prompt_library.json")
registry = ExperimentRegistry(Path("experiments/records.jsonl"))

# 2) 某次决策前，拿到 DatasetSanity 报告（可选）
sanity_report = current_dataset_sanity_report  # 来自前面的 pipeline

# 3) 构造 context
ctx = build_brain_context(
    symbol="TSLA",
    timeframe="1m",
    label_id="ret_10m_reg",
    brain_mode_id="v3_fast_market_following_tactical_20251117",
    prompt_library=prompt_lib,
    experiment_registry=registry,
    sanity_report=sanity_report,
)

# 4) 交给 LLM（这里 pseudo 调用）
raw_json = call_llm_json(
    system_prompt=ctx["system_prompt"],
    user_prompt=ctx["user_prompt"],
    # 并且要求它输出符合 BrainDecision schema 的 JSON
)

# 5) 解析 & 校验
decision = parse_brain_decision_from_json(raw_json)
decision.validate()

# 6) 把 decision.selected 交给你的 orchestrator 去决定：
#    - 用哪些 run_id 的模型
#    - 各自的权重 / 角色
#    - 是 paper 还是 live
```

> `parse_brain_decision_from_json` 我们可以下一步一起写（用 Pydantic 或手写校验），保证 LLM 输出的 JSON 必须合法，否则整个决策直接 fail，不会落到交易层。

---

## 接下来可以做什么？

按你之前的三层拆分，这一轮我们已经把 **第 3 层（AI 大脑 & Prompt 安全）** 的骨干搭起来了：

* ✅ Prompt 白名单（PromptLibrary）
* ✅ ExperimentRegistry + Summary → 大脑的“记忆库”
* ✅ Brain Context Builder → 把 prompt + 实验 + 体检 合成一份「专业 briefing」

接下来可以继续两条线里的任意一条：

1. **继续 AI 大脑层**

   * 写 `parse_brain_decision_from_json()` + JSON schema 校验；
   * 定义 LLM 调用时的 `response_format` 模板，把 BrainDecision 的字段严格写死；
   * 设计几个示例 prompt + 示例 JSON 输出，用来做集成测试。

2. **回到底层数据 / ML 层**

   * 把 DatasetSanity 的 ClickHouse 版本优化真正落地（现在只是 python 层）；
   * 把 sandbox 特征的注册 / 评估 / 升级流程写成一个 service。

你现在只要回我一句「继续」，我就选 **AI 大脑层**，先把 `BrainDecision JSON schema + 解析 + 单元测试思路` 全部写出来，这样整个链路基本就差“接你真实训练代码”和“接你的下单模块”了。
