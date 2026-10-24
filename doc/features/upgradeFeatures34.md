好，那我们从 **“PI→HPO→ExperimentRegistry 已经通电”** 往前推一大步：
现在要把这条链真正变成 **AI 大脑可用的“策略池 + 安全选择器”**。

这一轮我们重点做三块：

1. 给 `ExperimentRegistry` 增加 **查询 & 排序能力**（能按 symbol / label / Sharpe 等筛选最佳 run）
2. 定义 **BrainDecision 的严格 JSON Schema + 校验**（防止 LLM 胡说）
3. 实现一个 **Brain Context Builder**：从 Registry 取候选策略 → 压缩成摘要 → 拼成 Prompt → 等大脑选策略

---

## 一、给 ExperimentRegistry 加“查询大脑”的能力

### 1.1 定义 Filter & Candidate 摘要类型

```python
# experiments/query.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .types import ExperimentRecord


@dataclass
class ExperimentFilter:
    symbol: Optional[str] = None
    label_id: Optional[str] = None
    bar_interval: Optional[str] = None

    min_objective: Optional[float] = None  # 比如最小 Sharpe/目标值
    start_after: Optional[str] = None      # ISO 时间字符串
    end_before: Optional[str] = None

    limit: int = 20                        # 返回前 N 条
    sort_by: str = "hpo.best_objective"    # 排序字段
    sort_desc: bool = True                 # 是否倒序（大在前）


@dataclass
class BrainStrategyCandidate:
    run_id: str
    symbol: str
    label_id: str
    bar_interval: str

    model_type: str
    objective: float

    sharpe_valid: Optional[float]
    sortino_valid: Optional[float]

    start_ts: str
    end_ts: str

    # 数据质量 / 标签质量简要信号
    data_quality_ok: bool
    label_balance_ok: bool

    def to_brain_dict(self) -> Dict[str, Any]:
        """转成给 LLM 看的瘦身版 JSON。"""
        return {
            "run_id": self.run_id,
            "symbol": self.symbol,
            "label_id": self.label_id,
            "bar_interval": self.bar_interval,
            "model_type": self.model_type,
            "objective": self.objective,
            "sharpe_valid": self.sharpe_valid,
            "sortino_valid": self.sortino_valid,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "data_quality_ok": self.data_quality_ok,
            "label_balance_ok": self.label_balance_ok,
        }
```

### 1.2 在 Registry 上实现 `.load_all()` + `.query()` + 转候选

假设你的 `ExperimentRegistry` 之前已经有 `append()` 写 jsonl，这里给一个“增强版”草图（你可以按自己目录结构改名）：

```python
# experiments/registry.py

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .types import ExperimentRecord
from .query import ExperimentFilter, BrainStrategyCandidate


class ExperimentRegistry:
    def __init__(self, root_dir: str) -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.file_path = self.root / "experiments.jsonl"

    def append(self, record: ExperimentRecord) -> None:
        line = record.json()  # pydantic BaseModel.json()
        with self.file_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load_all(self) -> List[ExperimentRecord]:
        if not self.file_path.exists():
            return []
        records: List[ExperimentRecord] = []
        with self.file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                records.append(ExperimentRecord.parse_obj(obj))
        return records

    def query(self, flt: ExperimentFilter) -> List[ExperimentRecord]:
        records = self.load_all()

        def _get_nested(d, path, default=None):
            cur = d
            for p in path.split("."):
                if not isinstance(cur, dict) or p not in cur:
                    return default
                cur = cur[p]
            return cur

        out: List[ExperimentRecord] = []
        for r in records:
            if flt.symbol and r.dataset.symbol != flt.symbol:
                continue
            if flt.label_id and r.dataset.label_id != flt.label_id:
                continue
            if flt.bar_interval and r.dataset.bar_interval != flt.bar_interval:
                continue
            if flt.start_after and r.dataset.start_ts <= flt.start_after:
                continue
            if flt.end_before and r.dataset.end_ts >= flt.end_before:
                continue

            if flt.min_objective is not None:
                obj = r.hpo.best_objective if r.hpo else None
                if obj is None or obj < flt.min_objective:
                    continue

            out.append(r)

        # 排序
        sort_key = flt.sort_by

        def _key(rec: ExperimentRecord):
            d = json.loads(rec.json())
            val = _get_nested(d, sort_key, None)
            # None 排到最后
            return (val is None, val)

        out_sorted = sorted(out, key=_key, reverse=flt.sort_desc)
        return out_sorted[: flt.limit]

    def select_candidates_for_brain(
        self,
        *,
        symbol: str,
        label_id: str,
        bar_interval: str,
        min_objective: float = 0.0,
        limit: int = 8,
    ) -> List[BrainStrategyCandidate]:
        flt = ExperimentFilter(
            symbol=symbol,
            label_id=label_id,
            bar_interval=bar_interval,
            min_objective=min_objective,
            limit=limit,
            sort_by="hpo.best_objective",
            sort_desc=True,
        )
        records = self.query(flt)
        candidates: List[BrainStrategyCandidate] = []

        for r in records:
            # valid 集的 Sharpe / Sortino 如果有的话
            sharpe_valid = None
            sortino_valid = None
            if r.hpo and r.hpo.split_metrics:
                for sm in r.hpo.split_metrics:
                    if sm.name.lower() == "valid":
                        sharpe_valid = sm.metrics.get("sharpe")
                        sortino_valid = sm.metrics.get("sortino")
                        break

            sanity = r.sanity.features_sanity or {}
            data_quality_ok = bool(sanity.get("ok", True))
            label_sanity = r.sanity.label_sanity or {}
            label_balance_ok = bool(
                label_sanity.get("label_report", {}).get("class_balance_ok", True)
            )

            candidates.append(
                BrainStrategyCandidate(
                    run_id=r.run_id,
                    symbol=r.dataset.symbol,
                    label_id=r.dataset.label_id,
                    bar_interval=r.dataset.bar_interval,
                    model_type=r.hpo.best_model_type if r.hpo else "",
                    objective=r.hpo.best_objective if r.hpo else 0.0,
                    sharpe_valid=sharpe_valid,
                    sortino_valid=sortino_valid,
                    start_ts=r.dataset.start_ts,
                    end_ts=r.dataset.end_ts,
                    data_quality_ok=data_quality_ok,
                    label_balance_ok=label_balance_ok,
                )
            )

        return candidates
```

> 有了这个，AI 大脑前面那一步就可以做：
> **“给我 NVDA / 1m / ret_10m_fwd 的 top N 策略候选”** → 返回一个 `BrainStrategyCandidate` 列表 + `allowed_run_ids`。

---

## 二、定义 BrainDecision 的 JSON Schema + 校验

### 2.1 用 Pydantic 定义 Decision 模型

```python
# brain/decision_schema.py

from __future__ import annotations

from typing import List, Literal
from pydantic import BaseModel, Field, validator


class StrategyWeight(BaseModel):
    run_id: str = Field(..., description="ExperimentRegistry 中的 run_id")
    weight: float = Field(..., ge=0.0, le=1.0, description="权重, 0~1")
    role: Literal["primary", "hedge", "experimental"] = "primary"


class BrainDecision(BaseModel):
    decision_type: Literal["SELECT_AND_WEIGHT", "HOLD_OUT", "ABORT"] = "SELECT_AND_WEIGHT"

    symbol: str
    bar_interval: str
    label_id: str

    strategies: List[StrategyWeight] = Field(
        default_factory=list,
        description="当 decision_type=SELECT_AND_WEIGHT 时必须非空",
    )

    rationale: str = Field(
        default="",
        description="简短中文/英文解释, 方便审计和回放",
    )

    @validator("strategies")
    def _non_empty_if_select(cls, v, values):
        if values.get("decision_type") == "SELECT_AND_WEIGHT" and not v:
            raise ValueError("SELECT_AND_WEIGHT 决策必须至少包含一个 strategy")
        return v

    @validator("strategies")
    def _weights_sum_ok(cls, v):
        if not v:
            return v
        s = sum(item.weight for item in v)
        # 允许一点浮点误差
        if not (0.0 < s <= 1.01):
            raise ValueError(f"策略权重之和必须在 (0,1] 内, 当前={s}")
        return v
```

### 2.2 约束 run_id 必须在 allowed_run_ids 内

```python
# brain/decision_validator.py

from __future__ import annotations
from typing import List
from .decision_schema import BrainDecision


class InvalidDecisionError(Exception):
    pass


def validate_decision_vs_allowed_runs(
    decision: BrainDecision,
    allowed_run_ids: List[str],
) -> None:
    allowed_set = set(allowed_run_ids)

    if decision.decision_type != "SELECT_AND_WEIGHT":
        # HOLD_OUT / ABORT 不需要检查 run_id
        return

    for s in decision.strategies:
        if s.run_id not in allowed_set:
            raise InvalidDecisionError(
                f"策略 run_id={s.run_id} 不在 allowed_run_ids 白名单中, "
                f"疑似越权或 prompt 注入"
            )
```

> 至此：
>
> * LLM 无论输出什么，你都会先 `BrainDecision.parse_obj(raw_json)`，然后再 `validate_decision_vs_allowed_runs`。
> * 只要 run_id 不在候选列表里，直接拒绝执行，避免“自创策略”和“调用旧垃圾 run”。

---

## 三、Brain Context Builder：把 Registry + 市场快照 拼成 Prompt

现在我们需要一个统一的方法：

* 输入：

  * symbol / bar_interval / label_id
  * candidates（上面 `BrainStrategyCandidate` 列表）
  * 一些 **实时信息**：当前价、RVOL、spread、仓位等
  * gating 状态（今天是否允许开新仓等）
  * execution_prompt_id（比如你 prompt_library 里的 `"v3_fast_market_following_tactical_20251117"`）

* 输出：

  * system_prompt（从 prompt 库里取）
  * user_prompt（包含 allowed_run_ids + 候选策略摘要 + 实时 market snapshot）
  * allowed_run_ids（让调用方之后做决策校验）

### 3.1 假设你有一个 Prompt Library 访问函数

```python
# brain/prompt_library.py

import json
from pathlib import Path
from typing import Dict

_PROMPTS = None


def load_prompt_library(path: str = "config/prompt_library.json") -> Dict:
    global _PROMPTS
    if _PROMPTS is None:
        _PROMPTS = json.loads(Path(path).read_text(encoding="utf-8"))
    return _PROMPTS


def get_execution_prompt(execution_prompt_id: str) -> str:
    lib = load_prompt_library()
    # 顶层 / 子 key 都允许
    if execution_prompt_id in lib:
        return lib[execution_prompt_id]
    if "prompts" in lib and execution_prompt_id in lib["prompts"]:
        return lib["prompts"][execution_prompt_id]
    raise KeyError(f"execution_prompt_id={execution_prompt_id} not found in prompt library")
```

### 3.2 构建大脑上下文

```python
# brain/context_builder.py

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from experiments.query import BrainStrategyCandidate
from .prompt_library import get_execution_prompt


def build_brain_context(
    *,
    symbol: str,
    bar_interval: str,
    label_id: str,
    execution_prompt_id: str,
    candidates: List[BrainStrategyCandidate],
    market_snapshot: Dict[str, Any],
    gates_state: Dict[str, Any],
) -> Tuple[str, str, List[str]]:
    """
    返回:
      - system_prompt: 来自 prompt library
      - user_prompt: 包含 allowed_run_ids + 策略摘要 + 实时市场信息
      - allowed_run_ids: 用于决策校验
    """
    system_prompt = get_execution_prompt(execution_prompt_id)

    allowed_run_ids = [c.run_id for c in candidates]

    # 把候选策略压成一个简洁的 JSON 列表字符串
    strategies_for_brain = [c.to_brain_dict() for c in candidates]

    # 你可以根据自己喜好用更漂亮的模板，这里偏结构化一点
    user_payload: Dict[str, Any] = {
        "instruction": (
            "你是本交易系统的策略选择器。"
            "只能在 allowed_run_ids 列表中的策略之间选择和分配权重，"
            "不能发明新的 run_id。"
            "当信号不清晰或风控不允许时，可以选择 HOLD_OUT 或 ABORT。"
            "最终必须输出严格符合 BrainDecision JSON Schema 的 JSON。"
        ),
        "symbol": symbol,
        "bar_interval": bar_interval,
        "label_id": label_id,
        "allowed_run_ids": allowed_run_ids,
        "strategy_candidates": strategies_for_brain,
        "market_snapshot": market_snapshot,
        "gates_state": gates_state,
        "output_schema_hint": {
            "decision_type": "SELECT_AND_WEIGHT | HOLD_OUT | ABORT",
            "strategies": [
                {"run_id": "<one of allowed_run_ids>", "weight": 0.5, "role": "primary"}
            ],
            "rationale": "short explanation why",
        },
    }

    # 为了让 LLM 更稳地输出 JSON，可以在这里直接 json.dumps，
    # 再在 user_prompt 外面加一点自然语言
    import json

    user_prompt = (
        "下面是本次可用的策略池和市场状态，请根据说明输出一个 JSON 决策对象：\n\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
    )

    return system_prompt, user_prompt, allowed_run_ids
```

---

## 四、把一切串起来的“示意调用”

给你一个“伪主流程”，展示从 **Registry→候选→Context→LLM→决策校验** 的完整闭环：

```python
# brain/strategy_selector.py

from __future__ import annotations

from typing import Any, Dict

from experiments.registry import ExperimentRegistry
from hpo.adapter import run_hpo_with_grid  # 只是示例，不一定这里用
from .context_builder import build_brain_context
from .decision_schema import BrainDecision
from .decision_validator import validate_decision_vs_allowed_runs
from llm.llm_client import LLMClient


def select_strategies_for_symbol(
    *,
    symbol: str,
    bar_interval: str,
    label_id: str,
    execution_prompt_id: str,
    registry: ExperimentRegistry,
    market_snapshot: Dict[str, Any],
    gates_state: Dict[str, Any],
    llm: LLMClient,
) -> BrainDecision:
    # 1) 从 Registry 中选候选策略
    candidates = registry.select_candidates_for_brain(
        symbol=symbol,
        label_id=label_id,
        bar_interval=bar_interval,
        min_objective=0.0,  # 你可以传更高门槛
        limit=8,
    )
    if not candidates:
        # 没有任何可用策略，直接 HOLD_OUT
        return BrainDecision(
            decision_type="HOLD_OUT",
            symbol=symbol,
            bar_interval=bar_interval,
            label_id=label_id,
            strategies=[],
            rationale="No eligible strategies found in ExperimentRegistry",
        )

    # 2) 构建 LLM 上下文
    system_prompt, user_prompt, allowed_run_ids = build_brain_context(
        symbol=symbol,
        bar_interval=bar_interval,
        label_id=label_id,
        execution_prompt_id=execution_prompt_id,
        candidates=candidates,
        market_snapshot=market_snapshot,
        gates_state=gates_state,
    )

    # 3) 调用 LLM（这里示例：让它返回 JSON dict）
    raw = llm.generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_format="json",  # 具体看你 LLMClient 怎么封装
    )

    # 4) 解析 & 校验
    decision = BrainDecision.parse_obj(raw)
    validate_decision_vs_allowed_runs(decision, allowed_run_ids)

    return decision
```

> 到这里为止：
>
> * **数据/特征/标签层**：已经有 DatasetSanity + LabelSpec + PIResult 规范 + LabelGuard（不让 LLM 写 target SQL）。
> * **ML/HPO/实验治理层**：有 PI→HPO→ExperimentRegistry→查询 + 候选 summarizer。
> * **AI 大脑层**：有 Prompt whitelist、BrainDecision JSON Schema、run_id 白名单校验、Context Builder。

---

## 下一步怎么推进？

如果你愿意，下一步我们可以：

1. 写一个 **完整的集成测试脚本**，模拟：

   * 从 fake PIResult + fake HPO 填充几条 ExperimentRecord
   * 用 Registry 查询出候选
   * 用一个“假 LLM”（返回硬编码 JSON）来测试 `select_strategies_for_symbol`
   * 确保整个链路完全不依赖真实 LLM，也能跑通

2. 再往前接：

   * 把 `BrainDecision` 映射到 **实盘/回测执行器**：

     * 对于 `SELECT_AND_WEIGHT`：加载对应 run_id 的模型（Phase 4B），生成信号
     * 对于 `HOLD_OUT` / `ABORT`：直接不下单 or 触发高级风控动作。

你先消化一下这一轮的代码，如果你愿意，我们下一步就写集成 smoke test，把 **“大脑选策略”** 这一段也通电。
好，按我们刚才那条路线，现在正式把 **“PI → HPO 入口统一 + 强制 DatasetSanity + 写上特征/标签版本信息”** 落地一版代码草图。

我分 3 个小块给你：

1. `dataset_sanity.py`：标准化的体检模块（强制关卡）
2. `registry/versioning.py`：特征/标签版本号读取
3. 在训练入口（比如 `wf_hpo.py`）里，把 PI → DatasetSanity → ExperimentRecord 串起来

> 下面都是“可以直接放进项目里”的代码草图，你可以按你的目录结构微调文件路径。

---

## 1️⃣ 新增：`dataset_sanity.py`（数据体检模块）

```python
# core/dataset_sanity.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


class DatasetSanityError(RuntimeError):
    """Raised when dataset fails mandatory sanity checks."""


@dataclass
class DatasetSpec:
    """描述这份数据集的“身份”+元信息，用于日志和审计。"""
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    bar_interval: str
    feature_set_id: str
    label_id: str
    feature_registry_version: str
    label_registry_version: str


@dataclass
class DatasetSanityReport:
    ok: bool
    reason: str
    n_rows: int
    n_cols: int
    missing_ratio_by_col: Dict[str, float]
    zero_variance_cols: List[str]
    inf_cols: List[str]
    duplicate_rows: int
    label_name: Optional[str] = None
    label_positive_ratio: Optional[float] = None
    label_stats: Optional[Dict[str, Any]] = None
    spec: Optional[Dict[str, Any]] = None


def _detect_label_col(df: pd.DataFrame, label_id: str) -> Optional[str]:
    """
    简单约定：
    - 如果 df 里有列名和 label_id 完全相同，就用它
    - 否则尝试 'target', 'y', 'label' 这些常见名字
    """
    if label_id in df.columns:
        return label_id
    for cand in ("target", "label", "y"):
        if cand in df.columns:
            return cand
    return None


def run_dataset_sanity(
    df: pd.DataFrame,
    spec: DatasetSpec,
    *,
    strict: bool = True,
    max_missing_ratio: float = 0.4,      # 单列缺失率超过则告警
    min_rows: int = 500,                 # 行太少视为不可用
) -> DatasetSanityReport:
    """
    对训练用 DataFrame 做一轮轻量但强制的体检：
    - 检查空值/Inf/零方差
    - 检查重复行
    - 检查标签分布是否完全失衡
    若 strict=True 且不通过，则抛 DatasetSanityError，阻止训练继续。
    """

    if df is None:
        raise DatasetSanityError("Dataset is None")

    n_rows, n_cols = df.shape
    if n_rows < min_rows:
        reason = f"too_few_rows: {n_rows} < {min_rows}"
        report = DatasetSanityReport(
            ok=False,
            reason=reason,
            n_rows=n_rows,
            n_cols=n_cols,
            missing_ratio_by_col={},
            zero_variance_cols=[],
            inf_cols=[],
            duplicate_rows=0,
            spec=asdict(spec),
        )
        if strict:
            raise DatasetSanityError(reason)
        return report

    # 缺失率
    missing_ratio_by_col: Dict[str, float] = {}
    for col in df.columns:
        missing_ratio_by_col[col] = float(df[col].isna().mean())

    # 零方差数值列
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    zero_variance_cols: List[str] = []
    for col in numeric_cols:
        if df[col].nunique(dropna=True) <= 1:
            zero_variance_cols.append(col)

    # Inf 检测
    inf_cols: List[str] = []
    for col in numeric_cols:
        if np.isinf(df[col].to_numpy()).any():
            inf_cols.append(col)

    # 重复行
    duplicate_rows = int(df.duplicated().sum())

    # 标签分布（如果找到 label 列）
    label_col = _detect_label_col(df, spec.label_id)
    label_name: Optional[str] = None
    label_positive_ratio: Optional[float] = None
    label_stats: Optional[Dict[str, Any]] = None

    if label_col is not None:
        label_name = label_col
        series = df[label_col].dropna()
        if series.dtype == bool or series.isin([0, 1]).all():
            # 二元标签
            positive = float((series == 1).mean())
            label_positive_ratio = positive
        # 简单统计
        label_stats = {
            "min": float(series.min()) if len(series) else None,
            "max": float(series.max()) if len(series) else None,
            "mean": float(series.mean()) if len(series) else None,
            "std": float(series.std()) if len(series) else None,
        }

    # 组装 “是否通过” 逻辑：
    hard_fail_reasons: List[str] = []

    # 任意一列缺失率 > 阈值
    bad_missing_cols = [c for c, r in missing_ratio_by_col.items() if r > max_missing_ratio]
    if bad_missing_cols:
        hard_fail_reasons.append(
            f"columns_exceed_missing_ratio: {bad_missing_cols} (> {max_missing_ratio})"
        )

    if inf_cols:
        hard_fail_reasons.append(f"columns_with_inf: {inf_cols}")

    if len(zero_variance_cols) > 0 and len(zero_variance_cols) == len(numeric_cols) and numeric_cols:
        # 所有数值列都零方差，肯定不行
        hard_fail_reasons.append("all_numeric_zero_variance")

    if label_name is not None and label_positive_ratio is not None:
        # 例子：正样本比例 < 0.1% 或 > 99.9% 认为严重失衡
        if label_positive_ratio < 0.001 or label_positive_ratio > 0.999:
            hard_fail_reasons.append(
                f"label_extreme_imbalance: {label_positive_ratio:.4f}"
            )

    ok = len(hard_fail_reasons) == 0
    reason = "ok" if ok else "; ".join(hard_fail_reasons)

    report = DatasetSanityReport(
        ok=ok,
        reason=reason,
        n_rows=n_rows,
        n_cols=n_cols,
        missing_ratio_by_col=missing_ratio_by_col,
        zero_variance_cols=zero_variance_cols,
        inf_cols=inf_cols,
        duplicate_rows=duplicate_rows,
        label_name=label_name,
        label_positive_ratio=label_positive_ratio,
        label_stats=label_stats,
        spec=asdict(spec),
    )

    if strict and not ok:
        # fail-closed：不通过就直接阻止后续训练
        raise DatasetSanityError(reason)

    return report
```

> 这一块就是我们说的“强制体检层”。后面任何训练，只要经过这个函数，就不会在垃圾数据上浪费 HPO。

---

## 2️⃣ 新增：`registry/versioning.py`（特征/标签版本号）

简单先做一个**可用版**：从 JSON 里读 `_version` 字段；未来你可以把 checksum/hash 也加进去。

```python
# registry/versioning.py

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]  # 视你的项目结构调整
CFG_DIR = ROOT / "config"


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def get_feature_registry_version() -> str:
    """
    默认从 config/feature_registry.json 读取：
    {
      "_version": "eq_intraday_v1",
      ...
    }
    """
    path = CFG_DIR / "feature_registry.json"
    data = _load_json(path)
    return str(data.get("_version", "unknown"))


@lru_cache(maxsize=1)
def get_label_registry_version() -> str:
    """
    默认从 config/label_registry.json 读取：
    {
      "_version": "ret_labels_v1",
      ...
    }
    """
    path = CFG_DIR / "label_registry.json"
    data = _load_json(path)
    return str(data.get("_version", "unknown"))
```

对应的两个 JSON 你可以先简单放一个版本号：

```jsonc
// config/feature_registry.json
{
  "_version": "eq_intraday_v1",
  "sets": {
    "eq_intraday_v1": {
      "description": "US equities intraday 1m features v1",
      "features": [
        "ts", "symbol", "open", "high", "low", "close",
        "volume", "vwap", "rvol20", "atr14", "ret_1m"
      ]
    }
  }
}
```

```jsonc
// config/label_registry.json
{
  "_version": "ret_labels_v1",
  "labels": {
    "ret_10m_fwd_v1": {
      "description": "10-minute forward return",
      "horizon": "10m"
    }
  }
}
```

---

## 3️⃣ 修改训练入口：PI → DatasetSanity → ExperimentRecord

假设你有一个类似 `wf_hpo.py` 的主入口，现在改成统一在这里**强制调用 DatasetSanity**，并把版本信息写进 ExperimentRecord。

```python
# wf_hpo.py 里的关键片段（示意）

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from pi.principal_investigator import PrincipalInvestigator
from core.dataset_sanity import DatasetSpec, run_dataset_sanity, DatasetSanityError
from registry.versioning import (
    get_feature_registry_version,
    get_label_registry_version,
)
from experiments.registry import ExperimentRecord, ExperimentRegistry  # 假设你已有


def run_pi_and_hpo(
    ch_cfg: Dict[str, Any],
    llm_cfg: Dict[str, Any],
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    run_id: str,
) -> ExperimentRecord:
    """
    统一入口：
    1) 调用 PI 生成 feature_recipe + training_plan + dataset_df + hpo_grid
    2) 对 dataset_df 做强制 sanity check
    3) 写 ExperimentRecord（包含数据体检 + 特征/标签版本）
    4) 再跑 HPO / 训练
    """

    pi = PrincipalInvestigator(ch_cfg=ch_cfg, llm_cfg=llm_cfg)
    pi_result = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
    )

    feature_registry_version = get_feature_registry_version()
    label_registry_version = get_label_registry_version()

    # 从 training_plan 里优先取，取不到就用默认
    tplan = pi_result.training_plan
    feature_set_id = tplan.get("feature_set_id", "eq_intraday_v1")
    label_id = tplan.get("label_id", "ret_10m_fwd_v1")
    bar_interval = tplan.get("bar_interval", "1m")

    spec = DatasetSpec(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        bar_interval=bar_interval,
        feature_set_id=feature_set_id,
        label_id=label_id,
        feature_registry_version=feature_registry_version,
        label_registry_version=label_registry_version,
    )

    # === 强制体检：fail-closed ===
    try:
        sanity_report = run_dataset_sanity(pi_result.dataset_df, spec, strict=True)
    except DatasetSanityError as e:
        # 这里也可以写一条 ExperimentRecord 标记为 failed，然后抛出
        exp = ExperimentRecord(
            run_id=run_id,
            status="failed_sanity",
            dataset={
                "spec": asdict(spec),
                "sanity": {
                    "ok": False,
                    "reason": str(e),
                },
            },
            hpo={},
            metrics={},
        )
        ExperimentRegistry.append(exp)
        raise

    # 如果能走到这里，说明 sanity 通过，可以开始 HPO / 训练
    # 下面是简化示意
    hpo_result, metrics = run_hpo_with_grid(
        df=pi_result.dataset_df,
        grid=pi_result.hpo_grid,
        training_plan=tplan,
    )

    exp = ExperimentRecord(
        run_id=run_id,
        status="completed",
        dataset={
            "spec": asdict(spec),
            "sanity": asdict(sanity_report),
        },
        hpo={
            "grid": pi_result.hpo_grid,
            "best_params": hpo_result.best_params,
        },
        metrics=metrics,
    )

    ExperimentRegistry.append(exp)
    return exp


def run_hpo_with_grid(df, grid, training_plan):
    """
    这里用你现有的 HPO 引擎替换，我只给出接口形状。
    返回 (hpo_result, metrics) 即可。
    """
    # ...
    raise NotImplementedError
```

> 关键点是：
>
> * **任何训练 run 都必须经过 `run_dataset_sanity(..., strict=True)`**
> * **体检结果 + 特征/标签版本号 被写入 ExperimentRecord.dataset**
> * 如果体检不过，status 用 `failed_sanity` 标记，并写日志。

---

## 接下来我会做什么

到这里，**Step 1（训练入口统一封装 + 强制 DatasetSanity + 特征/标签版本写入）** 已经有完整落地方案：

* 有体检模块代码
* 有版本号读取逻辑
* 有训练入口的集成方式

下一步我会按之前的计划：

1. 把 **“所有 HPO / 训练入口都必须写 ExperimentRecord”**（Step M1）铺平；
2. 再继续往上串 AI 大脑那层（从 ExperimentRegistry 选策略 → BrainDecision）。

如果你 ok，我们就直接在下一步开始补 **HPO → ExperimentRecord 写入的统一封装**。
好，我们现在把 **“HPO → ExperimentRegistry 写入”** 这一段真正打通，做到：

> 任何一次从 PI 出发的训练 / HPO，都留下一个结构化的 `ExperimentRecord`，以后 AI 大脑只需要查这个“实验账本”就能选策略。

我分 3 步：

1. 定义统一的实验类型：`ExperimentMetrics` / `HPOInfo` / `ExperimentRecord`
2. 实现 `ExperimentRegistry`（先用 JSONL 文件落地，之后你可以换成 ClickHouse / DuckDB）
3. 把 HPO 入口 `run_hpo_with_grid(...)` 改成**自动写入 / 更新** ExperimentRecord

---

## 1️⃣ 新文件：`experiments/types.py`

```python
# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ExperimentMetrics:
    """核心绩效指标 —— 尽量与你现有 backtest 输出字段对齐。"""
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_drawdown: Optional[float] = None
    win_rate: Optional[float] = None
    pnl_total: Optional[float] = None
    pnl_per_trade: Optional[float] = None
    num_trades: Optional[int] = None
    turnover: Optional[float] = None
    # 预留扩展字段
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class HPOInfo:
    """记录一次 HPO / 训练过程的关键信息。"""
    hpo_engine: str  # 比如 "optuna", "grid", "random_search"
    n_trials: int
    best_params: Dict[str, Any] = field(default_factory=dict)
    best_value: Optional[float] = None  # 比如最佳 Sharpe 或 loss
    direction: str = "maximize"  # or "minimize"
    search_space: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExperimentRecord:
    """
    一次完整“实验 run”的记录：
    从 PI → 数据集 → 训练 / HPO → 结果。
    这一条记录会被写入 JSONL / 库表，被 AI 大脑用来选策略。
    """
    run_id: str
    created_at: str
    symbol: str
    table: str
    start_ts: str
    end_ts: str
    bar_interval: str
    feature_set_id: str
    label_id: str
    feature_registry_version: str
    label_registry_version: str

    status: str  # "pending", "failed_sanity", "failed_hpo", "completed"

    # 来自 PI / DatasetSanity / HPO
    dataset_spec: Dict[str, Any] = field(default_factory=dict)
    dataset_sanity: Dict[str, Any] = field(default_factory=dict)
    hpo: HPOInfo = field(default_factory=lambda: HPOInfo(hpo_engine="unknown", n_trials=0))
    metrics: ExperimentMetrics = field(default_factory=ExperimentMetrics)

    # 补充信息
    notes: str = ""
    tags: List[str] = field(default_factory=list)
    # 比如 "tsla", "intraday", "v3_fast_market_following"
    prompt_id: Optional[str] = None
    strategy_name: Optional[str] = None  # 给 AI 大脑看的“策略名”

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def new_pending(
        cls,
        *,
        run_id: str,
        symbol: str,
        table: str,
        start_ts: str,
        end_ts: str,
        bar_interval: str,
        feature_set_id: str,
        label_id: str,
        feature_registry_version: str,
        label_registry_version: str,
        dataset_spec: Dict[str, Any],
        prompt_id: Optional[str] = None,
        strategy_name: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ) -> "ExperimentRecord":
        return cls(
            run_id=run_id,
            created_at=datetime.utcnow().isoformat(),
            symbol=symbol,
            table=table,
            start_ts=start_ts,
            end_ts=end_ts,
            bar_interval=bar_interval,
            feature_set_id=feature_set_id,
            label_id=label_id,
            feature_registry_version=feature_registry_version,
            label_registry_version=label_registry_version,
            status="pending",
            dataset_spec=dataset_spec,
            dataset_sanity={},
            hpo=HPOInfo(hpo_engine="unknown", n_trials=0),
            metrics=ExperimentMetrics(),
            notes="",
            tags=tags or [],
            prompt_id=prompt_id,
            strategy_name=strategy_name,
        )
```

---

## 2️⃣ 新文件：`experiments/registry.py`（JSONL 版 Registry）

先用**文件型 JSONL** 做 index（简单、透明），后面换成 ClickHouse 表也很容易 —— 只要保持 `append()` 和 `update()` 接口不变就好。

```python
# experiments/registry.py

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .types import ExperimentRecord


class ExperimentRegistry:
    """
    极简版实验注册表：
    - 用一个 JSONL 文件保存所有 ExperimentRecord
    - 提供 append / update / load / list_recent 等接口
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)
        self._index_path = self._root / "experiments_index.jsonl"

    # -------- 写入 / 更新 --------

    def append(self, rec: ExperimentRecord) -> None:
        """
        追加一条实验记录（常用于 pending / completed / failed）。
        """
        with self._index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")

    def load_all(self) -> List[Dict]:
        """
        简单读回所有记录（生产环境可以改成流式/分页）。
        """
        if not self._index_path.exists():
            return []
        rows: List[Dict] = []
        with self._index_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    # 可以加日志
                    continue
        return rows

    def list_recent(
        self,
        *,
        symbol: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """
        筛选最近的实验，用于：
        - AI 大脑看“有哪些策略已经训练好了”
        - 人工查看最近训练记录
        """
        rows = self.load_all()
        rows = sorted(rows, key=lambda r: r.get("created_at", ""), reverse=True)

        out: List[Dict] = []
        for r in rows:
            if symbol is not None and r.get("symbol") != symbol:
                continue
            if status is not None and r.get("status") != status:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def find_by_run_id(self, run_id: str) -> Optional[Dict]:
        for r in self.load_all():
            if r.get("run_id") == run_id:
                return r
        return None

    # -------- 给 AI 大脑用的 summary --------

    def summarize_for_brain(
        self,
        *,
        symbol: str,
        min_sharpe: float = 0.8,
        max_mdd: float = 0.2,
        limit: int = 10,
    ) -> List[Dict]:
        """
        把最近的、质量不错的实验压缩成 LLM 易读的 summary 格式。
        这里只做一个简单版本，后面可以再引入更复杂的评分。
        """
        rows = self.list_recent(symbol=symbol, status="completed", limit=200)

        candidates: List[Dict] = []
        for r in rows:
            metrics = r.get("metrics") or {}
            sharpe = metrics.get("sharpe")
            mdd = metrics.get("max_drawdown")

            if sharpe is None or mdd is None:
                continue
            if sharpe < min_sharpe:
                continue
            if mdd > max_mdd:
                continue

            candidates.append(
                {
                    "run_id": r["run_id"],
                    "symbol": r["symbol"],
                    "strategy_name": r.get("strategy_name"),
                    "prompt_id": r.get("prompt_id"),
                    "sharpe": sharpe,
                    "max_drawdown": mdd,
                    "num_trades": metrics.get("num_trades"),
                    "pnl_total": metrics.get("pnl_total"),
                    "tags": r.get("tags", []),
                }
            )

        # 简单按 Sharpe 降序排序
        candidates = sorted(candidates, key=lambda c: c["sharpe"], reverse=True)
        return candidates[:limit]
```

> ✅ 这个 Registry：
>
> * 已经有 `append()` / `list_recent()` / `summarize_for_brain()`
> * 后面 Phase 4 的 AI 大脑只要调用 `summarize_for_brain(symbol="TSLA")` 就能拿到 top run 列表。

---

## 3️⃣ 改造 HPO 入口：让每次训练自动写 ExperimentRecord

现在把之前的 `run_pi_and_hpo` 进一步改造：

* 一开始就创建一个 `pending` 的 ExperimentRecord，并写入 Registry
* 通过 DatasetSanity 后，跑 HPO
* 成功的话更新 status → `completed`，并写入 metrics / HPOInfo
* 失败的话 status → `failed_hpo`

```python
# wf_hpo.py 关键片段（更新版）

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Tuple

from pi.principal_investigator import PrincipalInvestigator
from core.dataset_sanity import DatasetSpec, run_dataset_sanity, DatasetSanityError
from registry.versioning import (
    get_feature_registry_version,
    get_label_registry_version,
)
from experiments.types import ExperimentRecord, ExperimentMetrics, HPOInfo
from experiments.registry import ExperimentRegistry


EXPERIMENTS_ROOT = Path("/path/to/experiments")  # 换成你的路径
REGISTRY = ExperimentRegistry(EXPERIMENTS_ROOT)


def run_pi_and_hpo(
    ch_cfg: Dict[str, Any],
    llm_cfg: Dict[str, Any],
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    run_id: str,
    prompt_id: str = "v3_fast_market_following",
    strategy_name: str = "intraday_alpha_v1",
) -> ExperimentRecord:
    pi = PrincipalInvestigator(ch_cfg=ch_cfg, llm_cfg=llm_cfg)
    pi_result = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
    )

    feature_registry_version = get_feature_registry_version()
    label_registry_version = get_label_registry_version()

    tplan = pi_result.training_plan
    feature_set_id = tplan.get("feature_set_id", "eq_intraday_v1")
    label_id = tplan.get("label_id", "ret_10m_fwd_v1")
    bar_interval = tplan.get("bar_interval", "1m")

    dataset_spec = {
        "table": table,
        "symbol": symbol,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "bar_interval": bar_interval,
        "feature_set_id": feature_set_id,
        "label_id": label_id,
    }

    # 1️⃣ 创建 pending 记录
    exp = ExperimentRecord.new_pending(
        run_id=run_id,
        symbol=symbol,
        table=table,
        start_ts=start_ts,
        end_ts=end_ts,
        bar_interval=bar_interval,
        feature_set_id=feature_set_id,
        label_id=label_id,
        feature_registry_version=feature_registry_version,
        label_registry_version=label_registry_version,
        dataset_spec=dataset_spec,
        prompt_id=prompt_id,
        strategy_name=strategy_name,
        tags=[symbol, "intraday", "pi_hpo"],
    )
    REGISTRY.append(exp)

    # 2️⃣ DatasetSanity：不通过则写 failed_sanity + 抛错
    spec = DatasetSpec(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        bar_interval=bar_interval,
        feature_set_id=feature_set_id,
        label_id=label_id,
        feature_registry_version=feature_registry_version,
        label_registry_version=label_registry_version,
    )

    try:
        sanity_report = run_dataset_sanity(pi_result.dataset_df, spec, strict=True)
    except DatasetSanityError as e:
        exp.status = "failed_sanity"
        exp.dataset_sanity = {
            "ok": False,
            "reason": str(e),
        }
        REGISTRY.append(exp)  # 写一条失败记录
        raise

    # 3️⃣ 通过体检，开始 HPO
    hpo_result, metrics_dict = run_hpo_with_grid(
        df=pi_result.dataset_df,
        grid=pi_result.hpo_grid,
        training_plan=tplan,
    )

    # 4️⃣ 更新 ExperimentRecord 为 completed
    exp.status = "completed"
    exp.dataset_sanity = asdict(sanity_report)
    exp.hpo = HPOInfo(
        hpo_engine=hpo_result.engine,
        n_trials=hpo_result.n_trials,
        best_params=hpo_result.best_params,
        best_value=hpo_result.best_value,
        direction=hpo_result.direction,
        search_space=hpo_result.search_space,
    )
    exp.metrics = ExperimentMetrics(
        sharpe=metrics_dict.get("sharpe"),
        sortino=metrics_dict.get("sortino"),
        max_drawdown=metrics_dict.get("max_drawdown"),
        win_rate=metrics_dict.get("win_rate"),
        pnl_total=metrics_dict.get("pnl_total"),
        pnl_per_trade=metrics_dict.get("pnl_per_trade"),
        num_trades=metrics_dict.get("num_trades"),
        turnover=metrics_dict.get("turnover"),
        extra={k: v for k, v in metrics_dict.items() if k not in {
            "sharpe", "sortino", "max_drawdown", "win_rate",
            "pnl_total", "pnl_per_trade", "num_trades", "turnover"
        }},
    )

    REGISTRY.append(exp)
    return exp


# 你现有的 HPO 引擎，只需要保证返回值结构一致即可：
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
        self.engine = engine
        self.n_trials = n_trials
        self.best_params = best_params
        self.best_value = best_value
        self.direction = direction
        self.search_space = search_space


def run_hpo_with_grid(df, grid, training_plan) -> Tuple[HPOResult, Dict[str, Any]]:
    """
    这里用你现有的 HPO 逻辑实现：
    - 把 grid 转为搜索空间
    - 调用 Optuna / 自己的 HPO
    - 返回 (HPOResult, metrics_dict)
    """
    # TODO: 用你的真实代码替换
    raise NotImplementedError
```

> 到这里为止：
>
> * **PI → DatasetSanity → HPO → ExperimentRegistry** 全链路已经有清晰的落地形状
> * 每跑一次训练，就会在 `experiments_index.jsonl` 写入 2～3 行：pending / failed_sanity / completed
> * AI 大脑以后只需要调用 `ExperimentRegistry.summarize_for_brain(symbol="TSLA")` 就能“看见”有哪些策略可用

---

### 下一步怎么继续推进到 Production

在 **PI → HPO → ExperimentRegistry** 这条链路上，剩下的关键步骤基本是：

1. **用你现有的 HPO/回测代码填上 `run_hpo_with_grid()`**，让它真正返回 `HPOResult + metrics_dict`
2. 把 `ExperimentRegistry` 的 JSONL 存储路径放到一个统一的 config / env 里
3. 在 AI 大脑那层（Brain Orchestrator）：

   * 读取 `summarize_for_brain(...)`
   * 把这些 run_id / 策略候选项塞进 LLM Prompt 的 “allowed_strategies” 区域
   * 强制 LLM 只能从这些 run_id 中选（而不是自己发明）

如果你愿意，我们下一步可以直接做第 1 件事：
**把一个真实的 HPO 入口改成兼容 `HPOResult + metrics_dict` 的形式**，我帮你写模板，你只要把你现有的 backtest / optuna 调用粘进去就好。
