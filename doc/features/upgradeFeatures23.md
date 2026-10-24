好，那我们按上次说的两个小目标继续推进：

1. **把新东西“小范围落地 + 回归测试”怎么做**
2. **给 AI 大脑一只“看数据信息的眼睛”：Dataset + Label + Feature 总结模块**

我不会再讲大的路线，只给你**能直接塞进代码库的东西 + 测试思路**。

---

## 一、小范围落地 & 回归测试：怎么不搞崩整套系统

目标：**先只在一个标的 + 一个时间段上验证新架构**，而不是一次性推到全部管线。

### 1. 建一个最小可运行的集成测试函数

新建/补一个 `tests/integration/test_pi_pipeline.py`（名字随意，意思到位就行）：

```python
# tests/integration/test_pi_pipeline.py

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from pi.principal_investigator import PrincipalInvestigator
from features.registry import FeatureRegistry
from labels.registry import LabelRegistry
from sanity.dataset_sanity import run_dataset_sanity_checks


def test_pi_single_symbol_smoke():
    """
    Smoke test:
    - 使用新的 FeatureRegistry + LabelRegistry + DatasetSanity
    - 跑 NVDA + 某一天的 1m 数据，确保全流程不抛异常
    """

    ch_cfg = {
        "host": "localhost",
        "port": 8123,
        "database": "trading",
        "user": "default",
        "password": "",
    }

    llm_cfg = {
        "model": "gpt-4.1-mini",  # 或你现在用的
        "base_url": "http://localhost:11434",  # 示例
        "labels_cfg_path": "config/labels.json",
        "features_cfg_path": "config/feature_registry.json",
    }

    pi = PrincipalInvestigator(ch_cfg, llm_cfg)

    # 选一个你日常测试常用的日期 & 标的
    symbol = "NVDA"
    start_ts = "2025-01-02 09:30:00"
    end_ts   = "2025-01-02 16:00:00"

    goal = "预测未来10分钟收益率并构建日内多空策略"
    horizon = "10m"

    # 只要不 raise 就算 smoke PASS
    result = pi.propose(
        table="bars_1m",
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
    )

    assert isinstance(result.dataset_df, pd.DataFrame)
    assert not result.dataset_df.empty

    # 关键检查：标签应该已经加在 DataFrame 里面
    target_id = result.training_plan.get("target_id")
    assert target_id in result.dataset_df.columns

    # sanity 再跑一遍，看是否OK（如果你已经在 propose 里集成，就可选）
    sanity_report = run_dataset_sanity_checks(result.dataset_df)
    assert sanity_report["status"] in ("ok", "warn")
```

> 这个测试不用一开始就集成到 CI，只要你在本地 `pytest tests/integration/test_pi_pipeline.py` 能跑通，说明：
> **FeatureRegistry + LabelRegistry + DatasetSanity** 三件套在一个 symbol + 一天的范围内能良性工作。

---

### 2. 分层回归测试 Checklist

你每动一块代码，可以按这个节奏跑回归（不用全自动化，先手动就够）：

1. **FeatureRegistry 层**

   * `pytest tests/unit/test_feature_registry.py`
   * 确保：

     * 非法 feature_id 会 raise / 被过滤；
     * `leak_safe_ids()` 正常返回。

2. **LabelRegistry + Label 计算层**

   * `pytest tests/unit/test_label_registry.py`
   * 用一个小 DataFrame（10 行）测试：

     * `apply_label` 计算结果的长度是否一致；
     * label 分布统计是否合理（比如没有 NaN 过多）。

3. **DatasetSanity 层**

   * `pytest tests/unit/test_dataset_sanity.py`
   * 刻意构造几种坏数据：

     * 全零特征；
     * 全常数特征；
     * 单个特征有大量 NaN；
     * label 极度不平衡；
   * 看 sanity_report 能不能打出 “warn/critical” 标记。

4. **PI 集成层**（上面那个 integration test）

   * 只跑一个 symbol + 一天；
   * 看运行时间（应该在可接受范围内，比如 < 30 秒）。

5. **HPO / 模型训练层（之后）**

   * 等我们把 ExperimentRecord/StrategyManifest 接起来，再加测试。

> 这样你每次把 Claude 生成的代码 merge 进去，就有一条“最短路径”去验证整条链路没有被搞崩。

---

## 二、给 AI 大脑一只“能看懂数据质量的眼睛”

现在我们做第二件事：**让 AI 大脑在做决策前，不再是“瞎子摸象”，而是真的看见：**

* 用了哪些特征？
* 标签怎么定义，分布如何？
* 数据质量有没有明显问题（大量缺失、共线性、极度不平衡）？

### 1. 设计一个统一的 Summary 结构

新建：`brain/dataset_summary.py`

目标：输入：

* `df`：已经打好 label、跑过 feature 的 DataFrame；
* `label_meta`：来自 LabelRegistry/compute；
* `feature_ids`：本次训练 / 实验使用的 feature_id 列表；
* `sanity_report`：来自 `run_dataset_sanity_checks(df)`。

输出两个东西：

1. **结构化 summary**（给系统用）：dict
2. **LLM-friendly 文本摘要**（给 AI 大脑 prompt 用）：str

```python
# brain/dataset_summary.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import math
import numpy as np
import pandas as pd


@dataclass
class DatasetSummary:
    """
    结构化的 Dataset + Label + Feature 摘要：
    - 既能给系统用（JSON），也能转成简短自然语言喂 LLM。
    """
    n_rows: int
    n_features: int
    feature_ids: List[str]
    label_col: str
    label_meta: Dict[str, Any]
    sanity_status: str
    sanity_flags: List[str]
    label_pos_frac: Optional[float]  # 对回归可为 None
    label_stats: Dict[str, Any]
    notes: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "n_rows": self.n_rows,
            "n_features": self.n_features,
            "feature_ids": self.feature_ids,
            "label_col": self.label_col,
            "label_meta": self.label_meta,
            "sanity_status": self.sanity_status,
            "sanity_flags": self.sanity_flags,
            "label_pos_frac": self.label_pos_frac,
            "label_stats": self.label_stats,
            "notes": self.notes,
        }

    def to_llm_text(self) -> str:
        """
        给 LLM 的简短摘要，几句话，强调风险点。
        """
        parts: List[str] = []

        parts.append(
            f"Dataset size: {self.n_rows} rows, {self.n_features} features."
        )
        parts.append(
            f"Features used: {', '.join(self.feature_ids[:20])}"
            + (" ..." if len(self.feature_ids) > 20 else "")
        )
        parts.append(f"Label column: {self.label_col} (type={self.label_meta.get('type')}).")

        if self.label_meta.get("type") == "classification" and self.label_pos_frac is not None:
            parts.append(
                f"Label class imbalance: positive ratio ≈ {self.label_pos_frac:.4f}."
            )

        parts.append(f"Sanity status: {self.sanity_status}.")
        if self.sanity_flags:
            parts.append("Key data-quality flags: " + "; ".join(self.sanity_flags[:5]))

        if self.notes:
            parts.append("Notes: " + " | ".join(self.notes[:3]))

        return "\n".join(parts)


def build_dataset_summary(
    *,
    df: pd.DataFrame,
    label_col: str,
    label_meta: Dict[str, Any],
    feature_ids: Sequence[str],
    sanity_report: Dict[str, Any],
) -> DatasetSummary:
    n_rows = len(df)
    n_features = len(feature_ids)

    sanity_status = sanity_report.get("status", "unknown")
    sanity_flags = sanity_report.get("flags", [])

    # label 统计
    label_series = df[label_col]
    label_type = label_meta.get("type", "regression")

    label_pos_frac: Optional[float] = None
    label_stats: Dict[str, Any] = {}

    if label_type == "classification":
        # 假设二分类，1 为正类
        pos_mask = (label_series == 1) | (label_series == True)
        pos_count = int(pos_mask.sum())
        label_pos_frac = pos_count / max(n_rows, 1)
        label_stats = {
            "pos_count": pos_count,
            "neg_count": int((~pos_mask).sum()),
        }
    else:
        # 回归
        label_stats = {
            "mean": float(label_series.mean(skipna=True)),
            "std": float(label_series.std(skipna=True)),
            "min": float(label_series.min(skipna=True)),
            "max": float(label_series.max(skipna=True)),
        }

    notes: List[str] = []

    # 简单的一些规则备注
    if label_type == "classification" and label_pos_frac is not None:
        if label_pos_frac < 0.01:
            notes.append("Label is extremely imbalanced (<1% positive).")
        elif label_pos_frac < 0.05:
            notes.append("Label is highly imbalanced (<5% positive).")

    if sanity_status == "critical":
        notes.append("Dataset sanity check marked as CRITICAL; training may be unsafe.")
    elif sanity_status == "warn":
        notes.append("Dataset sanity check raised warnings; inspect flags before trusting results.")

    return DatasetSummary(
        n_rows=n_rows,
        n_features=n_features,
        feature_ids=list(feature_ids),
        label_col=label_col,
        label_meta=label_meta,
        sanity_status=sanity_status,
        sanity_flags=sanity_flags,
        label_pos_frac=label_pos_frac,
        label_stats=label_stats,
        notes=notes,
    )
```

---

### 2. 在 PI 里面接上 DatasetSummary

在 `PrincipalInvestigator.propose()` 的尾部，把 summary 一起输出（或者挂在 PIResult 上）：

```python
# pi/principal_investigator.py 片段补充

from sanity.dataset_sanity import run_dataset_sanity_checks
from brain.dataset_summary import build_dataset_summary

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
    sanity_report: Dict[str, Any] | None = None
    dataset_summary: DatasetSummary | None = None


class PrincipalInvestigator:
    ...
    def propose(...):
        ...
        # === 1. 拉数据 ===
        df = self._client.query_df(sql)

        # === 2. 体检 ===
        sanity_report = run_dataset_sanity_checks(df)

        # === 3. label 计算（上一轮已经加过）===
        df_labeled, label_meta = apply_label(df, label_cfg)
        label_col = label_cfg.output_col

        # === 4. feature_id 列表（和 HPO grid 用的是同一套）===
        feature_ids = grid_sane["feature_subset"][0]  # 你之前 HPO grid 的格式

        # === 5. 构建 DatasetSummary ===
        ds_summary = build_dataset_summary(
            df=df_labeled,
            label_col=label_col,
            label_meta=label_meta,
            feature_ids=feature_ids,
            sanity_report=sanity_report,
        )

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df_labeled,
            hpo_grid=grid_sane,
            sanity_report=sanity_report,
            dataset_summary=ds_summary,
        )
```

> 这样一来：**每次 PI 产出的结果本身就是一个携带“自我说明”的对象**：
> 不仅有 df + hpo_grid，还有一段描述自己数据质量的 summary，可以直接丢给 AI 大脑。

---

### 3. 把 Summary 塞进 AI 大脑的 Prompt

假设你的 AI 大脑有一个类似 `build_brain_context()` 的函数，现在可以把 `DatasetSummary.to_llm_text()` 作为一个固定 section：

```python
# brain/context_builder.py 片段示意

from typing import Dict, Any
from brain.dataset_summary import DatasetSummary

def build_brain_context(
    *,
    base_prompt: str,
    dataset_summary: DatasetSummary,
    hpo_grid: Dict[str, Any],
    execution_prompt_id: str,
) -> str:
    """
    构建给 AI 大脑的最终 system + user prompt（简化版本）。
    """

    ds_text = dataset_summary.to_llm_text()

    grid_brief = f"""
HPO candidate models:
- model_types: {hpo_grid.get("model_type")}
- task: {hpo_grid.get("task")}
- decision: {hpo_grid.get("decision")}
"""

    # 你可以把 execution_prompt_id 对应的 prompt 也拼进去
    # 这里假设 base_prompt 已经是从 prompt_library 里拿出来的 system 描述
    full_prompt = f"""{base_prompt}

[DATASET SUMMARY]
{ds_text}

[HPO GRID]
{grid_brief}

Your job:
- First, decide if this dataset and label are safe/reasonable for training.
- If dataset sanity is CRITICAL, you must refuse to proceed and explain why.
- If label imbalance or other risks exist, you must explicitly mention them in your decision.
- Otherwise, choose which model_type(s) and feature_subset to use from the provided HPO grid.
- Output JSON only, following the BrainDecision schema.
"""

    return full_prompt.strip()
```

> 这就是“AI 大脑的眼睛”：
>
> * 不再是瞎猜；
> * 它先被喂一份**数据体检报告 + 标签说明 + 特征列表**；
> * 再根据这些信息做决策（要不要训练 / 用哪些模型 / 是否警告）。

---

## 小结：这轮我们实际推进了什么？

这次“继续”我们已经：

1. **给你一套小范围落地 + 回归测试的节奏**：

   * 一个最小集成测试；
   * 五层回归检查 checklist。

2. **实现了 Dataset + Label + Feature Summary 模块**：

   * `DatasetSummary` 数据结构；
   * `build_dataset_summary()` 生成结构化 summary；
   * `to_llm_text()` 生成给大脑看的短摘要；
   * PIResult 带着 summary 一起输出；
   * `build_brain_context()` 示例，把 summary 串进 AI 大脑 prompt。

接下来你可以让 Claude Code 帮你：

* 按这些文件名和接口实际创建代码；
* 跑我给的那几个单测 / 集成测。

等你这边代码差不多落进去、能跑一趟之后，如果你还想继续往前（比如：

* ClickHouse 侧优化 DatasetSanity
* Sandbox 特征的完整实现 + 实验晋升流程
* Phase 4A 的 ExperimentRecord / StrategyManifest / BrainDecision schema

你就像之前一样说一句：**“继续”**，我就从这些里挑下一块继续往下挖。
好，那我就不再复盘，只**围绕这三层直接“写代码 + 说明怎么接到你现有系统”**。
我会按顺序来：

1. 数据 & 特征 & 标签层
2. ML/HPO/实验治理层
3. AI 大脑 & Prompt 安全层

---

## 一、数据 & 特征 & 标签这一层——继续落地

### 1.1 ClickHouse 侧的 DatasetSanity + 采样

> 目标：**不要在 Python 里对几百万行做重统计**，而是把重活丢给 ClickHouse，只在 Python 做轻量检查 & 汇总。

**新文件：`sanity/dataset_sanity_clickhouse.py`**

```python
# sanity/dataset_sanity_clickhouse.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd
import clickhouse_connect


@dataclass
class ColumnStats:
    name: str
    non_null: int
    nulls: int
    min: Optional[float]
    max: Optional[float]
    mean: Optional[float]
    stddev: Optional[float]


@dataclass
class DatasetSanityConfig:
    max_null_ratio: float = 0.2
    min_non_null_rows: int = 1_000
    max_zero_variance_cols: int = 10
    max_strong_corr_pairs: int = 20
    corr_threshold: float = 0.98
    sample_rows_for_corr: int = 50_000


@dataclass
class DatasetSanityIssue:
    level: str  # "info" | "warn" | "error"
    code: str
    message: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DatasetSanityReport:
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    row_count: int
    col_stats: List[ColumnStats]
    issues: List[DatasetSanityIssue]

    @property
    def has_error(self) -> bool:
        return any(i.level == "error" for i in self.issues)


def compute_basic_stats_ch(
    client: clickhouse_connect.driver.Client,
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    columns: List[str],
) -> pd.DataFrame:
    """
    在 ClickHouse 端一次性对所有数值列做基础统计。
    返回格式：
      col_name, non_null, nulls, min, max, mean, stddev
    """
    cols_expr = ", ".join(
        [
            f"""
            '{{col}}' AS col_name,
            countIf({col} IS NOT NULL) AS non_null,
            countIf({col} IS NULL) AS nulls,
            min({col}) AS min,
            max({col}) AS max,
            avg({col}) AS mean,
            stddevPop({col}) AS stddev
            """.format(col=col)
            for col in columns
        ]
    )

    # 用数组 + array join 的写法也可以，这里保持简单直白，多行 UNION ALL
    union_parts = []
    for col in columns:
        union_parts.append(
            f"""
            SELECT
                '{col}' AS col_name,
                countIf({col} IS NOT NULL) AS non_null,
                countIf({col} IS NULL) AS nulls,
                min({col}) AS min,
                max({col}) AS max,
                avg({col}) AS mean,
                stddevPop({col}) AS stddev
            FROM {table}
            WHERE symbol = %(symbol)s
              AND ts >= %(start_ts)s
              AND ts < %(end_ts)s
            """
        )

    sql = " UNION ALL ".join(union_parts)

    df = client.query_df(
        sql,
        parameters={"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
    )
    return df


def sample_rows_for_corr_ch(
    client: clickhouse_connect.driver.Client,
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    columns: List[str],
    n_rows: int,
) -> pd.DataFrame:
    """
    从 ClickHouse 随机采样 n_rows 行（用于相关性和零方差检查）。
    """
    cols_expr = ", ".join(["ts", "symbol"] + columns)
    sql = f"""
    SELECT {cols_expr}
    FROM {table}
    WHERE symbol = %(symbol)s
      AND ts >= %(start_ts)s
      AND ts < %(end_ts)s
    ORDER BY rand()
    LIMIT %(limit)s
    """
    return client.query_df(
        sql,
        parameters={
            "symbol": symbol,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "limit": n_rows,
        },
    )


def run_dataset_sanity_from_clickhouse(
    client: clickhouse_connect.driver.Client,
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    feature_cols: List[str],
    label_col: Optional[str] = None,
    cfg: Optional[DatasetSanityConfig] = None,
) -> DatasetSanityReport:
    cfg = cfg or DatasetSanityConfig()

    # 1) 行数
    count_sql = f"""
    SELECT count() AS row_count
    FROM {table}
    WHERE symbol = %(symbol)s
      AND ts >= %(start_ts)s
      AND ts < %(end_ts)s
    """
    cnt_df = client.query_df(
        count_sql,
        parameters={"symbol": symbol, "start_ts": start_ts, "end_ts": end_ts},
    )
    row_count = int(cnt_df["row_count"].iloc[0])

    issues: List[DatasetSanityIssue] = []

    if row_count < cfg.min_non_null_rows:
        issues.append(
            DatasetSanityIssue(
                level="error",
                code="ROW_COUNT_TOO_SMALL",
                message=f"row_count={row_count} < min_non_null_rows={cfg.min_non_null_rows}",
            )
        )

    # 2) 基础统计（非空比例等）
    stats_df = compute_basic_stats_ch(
        client,
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        columns=feature_cols + ([label_col] if label_col else []),
    )

    col_stats: List[ColumnStats] = []
    high_null_cols = []
    for _, row in stats_df.iterrows():
        col_name = row["col_name"]
        non_null = int(row["non_null"])
        nulls = int(row["nulls"])
        total = non_null + nulls
        null_ratio = nulls / total if total > 0 else 0.0

        col_stats.append(
            ColumnStats(
                name=col_name,
                non_null=non_null,
                nulls=nulls,
                min=row["min"],
                max=row["max"],
                mean=row["mean"],
                stddev=row["stddev"],
            )
        )

        if null_ratio > cfg.max_null_ratio:
            high_null_cols.append((col_name, null_ratio))

    if high_null_cols:
        issues.append(
            DatasetSanityIssue(
                level="warn",
                code="HIGH_NULL_RATIO",
                message="Columns with high null ratio",
                details={"columns": high_null_cols},
            )
        )

    # 3) 采样 + 零方差/高相关性检查
    if row_count > 0 and feature_cols:
        sample_df = sample_rows_for_corr_ch(
            client,
            table=table,
            symbol=symbol,
            start_ts=start_ts,
            end_ts=end_ts,
            columns=feature_cols,
            n_rows=min(cfg.sample_rows_for_corr, row_count),
        )

        # 去掉全空列
        numeric_df = sample_df[feature_cols].select_dtypes(include=["number"])
        zero_var_cols = [c for c in numeric_df.columns if numeric_df[c].std() == 0]

        if zero_var_cols:
            level = "warn"
            if len(zero_var_cols) > cfg.max_zero_variance_cols:
                level = "error"
            issues.append(
                DatasetSanityIssue(
                    level=level,
                    code="ZERO_VARIANCE",
                    message="Zero variance columns detected",
                    details={"columns": zero_var_cols},
                )
            )

        # 相关性
        if numeric_df.shape[1] >= 2:
            corr = numeric_df.corr()
            strong_pairs = []
            cols = list(corr.columns)
            for i in range(len(cols)):
                for j in range(i + 1, len(cols)):
                    v = corr.iloc[i, j]
                    if abs(v) >= cfg.corr_threshold:
                        strong_pairs.append((cols[i], cols[j], float(v)))

            if strong_pairs:
                level = "warn"
                if len(strong_pairs) > cfg.max_strong_corr_pairs:
                    level = "warn"  # 共线性一般是 warn，不一定致命
                issues.append(
                    DatasetSanityIssue(
                        level=level,
                        code="HIGH_CORRELATION",
                        message="Highly correlated feature pairs",
                        details={"pairs": strong_pairs},
                    )
                )

    return DatasetSanityReport(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        row_count=row_count,
        col_stats=col_stats,
        issues=issues,
    )
```

> ✅ 用法：所有从 ClickHouse 拉数据训练/回测的入口，在拼好 SQL 之前/之后都走一遍这个 `run_dataset_sanity_from_clickhouse`，数据烂直接 `raise`，并且打结构化日志。

---

### 1.2 DatasetSummary：喂给 AI 大脑看的“简历”

**新文件：`sanity/dataset_summary.py`**

```python
# sanity/dataset_summary.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

from .dataset_sanity_clickhouse import (
    DatasetSanityReport,
    DatasetSanityIssue,
)


@dataclass
class DatasetSummary:
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    row_count: int
    feature_cols: List[str]
    label_col: Optional[str]
    sanity_level: str      # "ok" | "warn" | "error"
    main_issues: List[Dict[str, Any]]

    def to_llm_text(self) -> str:
        """
        转成给 AI 大脑看的自然语言/半结构化摘要。
        只保留重点，不塞一大坨细节进去，防止信息过载。
        """
        lines = [
            f"Dataset overview:",
            f"- table={self.table}, symbol={self.symbol}",
            f"- period={self.start_ts}~{self.end_ts}, rows={self.row_count}",
            f"- features={len(self.feature_cols)}, label={self.label_col or 'None'}",
            f"- sanity_level={self.sanity_level}",
        ]
        if self.main_issues:
            lines.append("- main_issues:")
            for issue in self.main_issues:
                lines.append(
                    f"  * [{issue['level']}] {issue['code']}: {issue['message']}"
                )
        else:
            lines.append("- main_issues: none")

        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def summarize_sanity_report(
    report: DatasetSanityReport,
    *,
    feature_cols: List[str],
    label_col: Optional[str],
) -> DatasetSummary:
    if report.has_error:
        level = "error"
    elif any(i.level == "warn" for i in report.issues):
        level = "warn"
    else:
        level = "ok"

    # 只挑最重要的若干条
    main_issues: List[Dict[str, Any]] = []
    for issue in report.issues:
        main_issues.append(
            {
                "level": issue.level,
                "code": issue.code,
                "message": issue.message,
            }
        )
        if len(main_issues) >= 10:
            break

    return DatasetSummary(
        table=report.table,
        symbol=report.symbol,
        start_ts=report.start_ts,
        end_ts=report.end_ts,
        row_count=report.row_count,
        feature_cols=feature_cols,
        label_col=label_col,
        sanity_level=level,
        main_issues=main_issues,
    )
```

---

### 1.3 FeatureRegistry & LabelRegistry——强约束入口

> 目标：所有特征 & 标签，用**统一注册表**管理，PI 和 HPO 不再“裸写名字”。

**新文件：`registry/feature_registry.py`**

```python
# registry/feature_registry.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

import json
from pathlib import Path


@dataclass
class FeatureSpec:
    id: str              # 逻辑名，如 "ma_close_20"
    column: str          # ClickHouse 实际列名
    expr: str            # SQL 表达式（可选）
    leak_safe: bool      # 是否确定无未来信息
    group: str           # "price", "volume", "volatility", ...


@dataclass
class FeatureRegistry:
    by_id: Dict[str, FeatureSpec]
    by_column: Dict[str, FeatureSpec]

    @classmethod
    def load(cls, path: str | Path) -> "FeatureRegistry":
        data = json.loads(Path(path).read_text())
        specs = []
        for item in data["features"]:
            specs.append(
                FeatureSpec(
                    id=item["id"],
                    column=item["column"],
                    expr=item.get("expr", item["column"]),
                    leak_safe=bool(item.get("leak_safe", True)),
                    group=item.get("group", "other"),
                )
            )
        return cls(
            by_id={s.id: s for s in specs},
            by_column={s.column: s for s in specs},
        )

    def ensure_ids_exist(self, ids: List[str]) -> None:
        missing = [fid for fid in ids if fid not in self.by_id]
        if missing:
            raise ValueError(f"Unknown feature ids: {missing}")

    def ensure_leak_safe(self, ids: List[str]) -> None:
        unsafe = [fid for fid in ids if not self.by_id[fid].leak_safe]
        if unsafe:
            raise ValueError(f"Leak-unsafe features used in recipe: {unsafe}")
```

**一个简单的 JSON 例子（`configs/feature_registry.json`）**：

```json
{
  "features": [
    {
      "id": "close",
      "column": "close",
      "expr": "close",
      "leak_safe": true,
      "group": "price"
    },
    {
      "id": "ret_1m",
      "column": "ret_1m",
      "expr": "ret_1m",
      "leak_safe": true,
      "group": "return"
    },
    {
      "id": "rv_5m",
      "column": "rv_5m",
      "expr": "rv_5m",
      "leak_safe": true,
      "group": "volatility"
    }
  ]
}
```

---

**新文件：`registry/label_registry.py`**

```python
# registry/label_registry.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

import json
from pathlib import Path


@dataclass
class LabelSpec:
    id: str                  # "ret_10m_fwd", "tb_10m_1atr"
    name: str
    horizon_sec: int
    method: str              # "simple_return" | "triple_barrier"
    params: Dict[str, Any]


@dataclass
class LabelRegistry:
    by_id: Dict[str, LabelSpec]

    @classmethod
    def load(cls, path: str | Path) -> "LabelRegistry":
        data = json.loads(Path(path).read_text())
        specs = []
        for item in data["labels"]:
            specs.append(
                LabelSpec(
                    id=item["id"],
                    name=item["name"],
                    horizon_sec=int(item["horizon_sec"]),
                    method=item["method"],
                    params=item.get("params", {}),
                )
            )
        return cls(by_id={s.id: s for s in specs})

    def get(self, label_id: str) -> LabelSpec:
        if label_id not in self.by_id:
            raise KeyError(f"Unknown label_id={label_id}")
        return self.by_id[label_id]
```

**Label JSON 示例：`configs/label_registry.json`**

```json
{
  "labels": [
    {
      "id": "ret_10m_fwd",
      "name": "10-min forward return",
      "horizon_sec": 600,
      "method": "simple_return",
      "params": {
        "price_col": "close"
      }
    }
  ]
}
```

> ✅ 真正路径依赖（triple barrier）以后就把 `method` 写成 `"triple_barrier"`，`params` 存 tp/sl/time 三个参数，但由 **Python 实现计算**，LLM 只能选 `label_id`，不能自己写 SQL。

---

### 1.4 把这一层接回 PI（PrincipalInvestigator）

我们对你给的 `PrincipalInvestigator` 做一个“轻量升级版”，**强制经过 Registry + Sanity**。

**修改版示意：`pi/principal_investigator.py`（只展示关键变更）**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql

from sanity.dataset_sanity_clickhouse import (
    run_dataset_sanity_from_clickhouse,
    DatasetSanityConfig,
)
from sanity.dataset_summary import summarize_sanity_report, DatasetSummary
from registry.feature_registry import FeatureRegistry
from registry.label_registry import LabelRegistry


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
    dataset_summary: DatasetSummary
    label_id: Optional[str]


def _fetch_catalog(client, table: str) -> List[str]:
    q = "SELECT name FROM system.columns WHERE table = {table:String}"
    df = client.query_df(q, parameters={"table": table})
    return sorted(df["name"].tolist())


class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        *,
        feature_registry_path: str = "configs/feature_registry.json",
        label_registry_path: str = "configs/label_registry.json",
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

        self._feature_registry = FeatureRegistry.load(feature_registry_path)
        self._label_registry = LabelRegistry.load(label_registry_path)

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
        catalog = _fetch_catalog(self._client, table)

        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"特征列: {', '.join(catalog)}\n"
            f"预测窗口: {horizon}\n"
            f"注意: 只能引用已有的特征 ID 和 label_id，不要自己写 SQL 表达式。"
        )
        system_prompt = (
            "你是量化平台的首席科学家。"
            "必须返回 JSON，包含 feature_recipe 和 training_plan。"
            "其中 feature_recipe.base_feature_ids 是特征 ID 列表，"
            "training_plan.label_id 是标签 ID。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {})
        tplan = plan.get("training_plan", {})

        # 1) 用 FeatureRegistry 约束特征
        base_ids: List[str] = recipe.get("base_feature_ids", [])
        self._feature_registry.ensure_ids_exist(base_ids)
        self._feature_registry.ensure_leak_safe(base_ids)

        # 把 feature_ids 映射回 ClickHouse 列名
        base_columns = [self._feature_registry.by_id[fid].column for fid in base_ids]

        # 2) 用 LabelRegistry 约束标签
        label_id = tplan.get("label_id")
        label_col: Optional[str] = None
        if label_id:
            label_spec = self._label_registry.get(label_id)
            # 暂时假设 simple_return 直接用 params.price_col 作为 label 列
            label_col = label_spec.params.get("price_col", "close")
        else:
            label_spec = None

        # 3) 在 ClickHouse 做数据体检（不含 label 计算）
        sanity_report = run_dataset_sanity_from_clickhouse(
            self._client,
            table=table,
            symbol=symbol,
            start_ts=start_ts,
            end_ts=end_ts,
            feature_cols=base_columns,
            label_col=label_col,
            cfg=DatasetSanityConfig(),
        )

        dataset_summary = summarize_sanity_report(
            sanity_report,
            feature_cols=base_columns,
            label_col=label_col,
        )

        if dataset_summary.sanity_level == "error":
            # 交给上层/大脑决定是否放弃这次实验
            raise RuntimeError(
                f"Dataset sanity error for {table}/{symbol} {start_ts}~{end_ts}: "
                f"{dataset_summary.main_issues}"
            )

        # 4) 构建 SQL（这里仍然用你现有的 build_select_sql，后续可以改成 label_registry 驱动）
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,      # TODO: 逐步改成用 feature_ids/label_id 驱动
            target=tplan.get("target"),
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # 5) HPO grid 构建（基本沿用你原来的逻辑）
        derived_aliases = [d.get("alias") for d in recipe.get("derived", [])]
        grid = {
            "model_type": [],
            "feature_subset": [base_columns + derived_aliases],
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

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            dataset_summary=dataset_summary,
            label_id=label_id,
        )
```

> ✅ 至此：
>
> * 所有训练计划都**必须走 FeatureRegistry + LabelRegistry + DatasetSanity**；
> * dataset_summary 已经 ready，后面可以直接喂给 AI 大脑做决策。

---

## 二、ML/HPO/实验治理这一层——继续落地

> 目标：每一次训练/HPO/backtest，都变成一个结构化的 **ExperimentRecord**，集中管理。

### 2.1 实验类型与记录结构

**新文件：`experiments/types.py`**

```python
# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime


@dataclass
class Metrics:
    sharpe: Optional[float] = None
    sortino: Optional[float] = None
    max_drawdown: Optional[float] = None
    pnl: Optional[float] = None
    win_rate: Optional[float] = None
    trade_count: Optional[int] = None


@dataclass
class ExperimentRecord:
    run_id: str
    created_at: str

    symbol: str
    table: str
    start_ts: str
    end_ts: str

    label_id: str
    feature_ids: List[str]

    model_type: str
    model_params: Dict[str, Any]

    hpo_config: Dict[str, Any]
    training_config: Dict[str, Any]

    metrics: Metrics

    code_version: str
    prompt_version: str

    dataset_sanity_level: str
    dataset_issues: List[Dict[str, Any]]

    artifacts: Dict[str, Any]   # model_path, backtest_report_path, etc.

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["metrics"] = asdict(self.metrics)
        return d

    @classmethod
    def new(
        cls,
        *,
        run_id: str,
        symbol: str,
        table: str,
        start_ts: str,
        end_ts: str,
        label_id: str,
        feature_ids: List[str],
        model_type: str,
        model_params: Dict[str, Any],
        hpo_config: Dict[str, Any],
        training_config: Dict[str, Any],
        metrics: Metrics,
        dataset_sanity_level: str,
        dataset_issues: List[Dict[str, Any]],
        code_version: str = "unknown",
        prompt_version: str = "unknown",
        artifacts: Optional[Dict[str, Any]] = None,
    ) -> "ExperimentRecord":
        return cls(
            run_id=run_id,
            created_at=datetime.utcnow().isoformat(),
            symbol=symbol,
            table=table,
            start_ts=start_ts,
            end_ts=end_ts,
            label_id=label_id,
            feature_ids=feature_ids,
            model_type=model_type,
            model_params=model_params,
            hpo_config=hpo_config,
            training_config=training_config,
            metrics=metrics,
            code_version=code_version,
            prompt_version=prompt_version,
            dataset_sanity_level=dataset_sanity_level,
            dataset_issues=dataset_issues,
            artifacts=artifacts or {},
        )
```

---

### 2.2 实验 Registry：写 JSONL + 查询

**新文件：`experiments/registry.py`**

```python
# experiments/registry.py

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from .types import ExperimentRecord


class ExperimentRegistry:
    """
    极简实现：所有 ExperimentRecord 写到一个 JSONL 文件。
    以后可以替换成 ClickHouse / Postgres 存储。
    """

    def __init__(self, path: str | Path = "experiments/experiments.jsonl") -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: ExperimentRecord) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def load_all(self) -> List[ExperimentRecord]:
        if not self._path.exists():
            return []

        rows: List[ExperimentRecord] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                d = json.loads(line)
                # 这里只做简化反序列化，生产可用再补齐
                from .types import Metrics, ExperimentRecord as ER

                rec = ER(
                    run_id=d["run_id"],
                    created_at=d["created_at"],
                    symbol=d["symbol"],
                    table=d["table"],
                    start_ts=d["start_ts"],
                    end_ts=d["end_ts"],
                    label_id=d["label_id"],
                    feature_ids=d["feature_ids"],
                    model_type=d["model_type"],
                    model_params=d["model_params"],
                    hpo_config=d["hpo_config"],
                    training_config=d["training_config"],
                    metrics=Metrics(**d.get("metrics", {})),
                    code_version=d.get("code_version", "unknown"),
                    prompt_version=d.get("prompt_version", "unknown"),
                    dataset_sanity_level=d.get("dataset_sanity_level", "unknown"),
                    dataset_issues=d.get("dataset_issues", []),
                    artifacts=d.get("artifacts", {}),
                )
                rows.append(rec)
        return rows

    def select_best_runs(
        self,
        *,
        symbol: Optional[str] = None,
        label_id: Optional[str] = None,
        min_trades: int = 50,
        max_mdd: Optional[float] = None,
        top_k: int = 10,
    ) -> List[ExperimentRecord]:
        """
        简单版本：按 Sharpe 排序，过滤交易数、回撤。
        """
        all_recs = self.load_all()
        filtered: List[ExperimentRecord] = []
        for r in all_recs:
            if symbol and r.symbol != symbol:
                continue
            if label_id and r.label_id != label_id:
                continue
            if r.metrics.trade_count is not None and r.metrics.trade_count < min_trades:
                continue
            if max_mdd is not None and r.metrics.max_drawdown is not None:
                if r.metrics.max_drawdown < -abs(max_mdd):  # 注意符号
                    continue
            if r.dataset_sanity_level == "error":
                continue
            filtered.append(r)

        filtered.sort(
            key=lambda r: (r.metrics.sharpe if r.metrics.sharpe is not None else -1e9),
            reverse=True,
        )
        return filtered[:top_k]
```

> ✅ 到这里，**所有 run_id 都有了统一“台账”**，后面 AI 大脑只需在这个 Registry 返回的列表里选策略。

---

### 2.3 HPO / 回测流程里接入 ExperimentRecord（示意）

你现在的 HPO 主脚本可能叫 `wf_hpo.py` 或 `hpo_cli_upgraded.py`。
在“某个 trial 最终评估完”之后，加一个类似这样的写入：

```python
# wf_hpo.py 里某个 trial 完成后（示意，伪代码）

from experiments.types import Metrics, ExperimentRecord
from experiments.registry import ExperimentRegistry

# 假设这些变量在 HPO 过程中都有：
# run_id, symbol, table, start_ts, end_ts
# label_id, feature_ids
# model_type, best_params
# hpo_config, training_config
# sharpe, sortino, max_dd, pnl, win_rate, trade_count
# dataset_summary (来自 PIResult)

metrics = Metrics(
    sharpe=sharpe,
    sortino=sortino,
    max_drawdown=max_dd,
    pnl=pnl,
    win_rate=win_rate,
    trade_count=trade_count,
)

record = ExperimentRecord.new(
    run_id=run_id,
    symbol=symbol,
    table=table,
    start_ts=start_ts,
    end_ts=end_ts,
    label_id=label_id,
    feature_ids=feature_ids,
    model_type=model_type,
    model_params=best_params,
    hpo_config=hpo_config,
    training_config=training_config,
    metrics=metrics,
    dataset_sanity_level=dataset_summary.sanity_level,
    dataset_issues=dataset_summary.main_issues,
    code_version="git:HEAD",            # 可以用真实 git hash
    prompt_version="pi_v1",             # 或者 PI 调用时传入
    artifacts={
        "model_path": model_path,
        "backtest_report": backtest_report_path,
    },
)

registry = ExperimentRegistry()
registry.append(record)
```

> ✅ 这样一来：
>
> * 你过去所有 HPO 结果都能以统一形式写入 JSONL；
> * 后面 AI 大脑可以直接查 `ExperimentRegistry.select_best_runs(...)` 拿可选策略池。

---

## 三、AI 大脑 & Prompt 安全这一层——继续落地

> 目标：**大脑不再自由发挥**，只在：
>
> * DatasetSummary
> * ExperimentRegistry 的候选 run_id
> * Prompt Library 白名单
>   这三个范围内做决策，输出符合 schema 的 JSON。

### 3.1 BrainDecision 强 schema

**新文件：`brain/schemas.py`**

```python
# brain/schemas.py

from __future__ import annotations

from typing import List, Literal, Optional
from pydantic import BaseModel, Field, validator


class StrategySelection(BaseModel):
    run_id: str = Field(..., description="Selected experiment run id")
    weight: float = Field(
        1.0,
        description="Relative weight for ensemble. 0~1.",
        ge=0.0,
        le=1.0,
    )


class BrainDecision(BaseModel):
    """
    AI 大脑的唯一合法输出格式。
    """

    mode: Literal["abort", "select_strategies", "request_new_training"]
    reason: str = Field(
        ...,
        description="Short natural language rationale",
        max_length=512,
    )

    # mode == "select_strategies"
    selections: Optional[List[StrategySelection]] = None

    # mode == "request_new_training"
    requested_label_id: Optional[str] = None
    requested_horizon: Optional[str] = None
    requested_symbol: Optional[str] = None

    @validator("selections", always=True)
    def validate_selections(cls, v, values):
        mode = values.get("mode")
        if mode == "select_strategies":
            if not v or len(v) == 0:
                raise ValueError("selections must be non-empty when mode=select_strategies")
        return v

    @validator("requested_label_id", always=True)
    def validate_request_fields(cls, v, values):
        mode = values.get("mode")
        if mode == "request_new_training" and not v:
            raise ValueError("requested_label_id is required when mode=request_new_training")
        return v
```

---

### 3.2 构造大脑上下文：DatasetSummary + 实验候选列表

**新文件：`brain/context_builder.py`**

```python
# brain/context_builder.py

from __future__ import annotations

from typing import List

from sanity.dataset_summary import DatasetSummary
from experiments.types import ExperimentRecord


def build_brain_user_prompt(
    *,
    dataset_summary: DatasetSummary,
    candidate_runs: List[ExperimentRecord],
) -> str:
    """
    把 DatasetSummary + 实验候选列表拼成一个 user_prompt；system_prompt 用 Prompt Library 控制。
    """
    lines: List[str] = []

    # 1) 数据集摘要
    lines.append(dataset_summary.to_llm_text())
    lines.append("")

    # 2) 候选策略列表（只给关键信息）
    if candidate_runs:
        lines.append("Candidate strategies (pre-validated runs):")
        for r in candidate_runs:
            lines.append(
                f"- run_id={r.run_id}, symbol={r.symbol}, label_id={r.label_id}, "
                f"model={r.model_type}, sharpe={r.metrics.sharpe}, "
                f"trades={r.metrics.trade_count}, mdd={r.metrics.max_drawdown}"
            )
    else:
        lines.append("No candidate strategies available for this symbol/label.")

    lines.append("")
    lines.append(
        "You must output a JSON object matching the BrainDecision schema. "
        "Do not invent new fields. "
        "Choose mode='select_strategies' only if at least one candidate is suitable; "
        "otherwise you may use mode='abort' or 'request_new_training'."
    )

    return "\n".join(lines)
```

---

### 3.3 调用 LLM + 严格校验

**新文件：`brain/runner.py`**

```python
# brain/runner.py

from __future__ import annotations

import json
from typing import List

from llm.llm_client import LLMClient
from sanity.dataset_summary import DatasetSummary
from experiments.types import ExperimentRecord
from experiments.registry import ExperimentRegistry
from .schemas import BrainDecision
from .context_builder import build_brain_user_prompt


class BrainController:
    def __init__(
        self,
        llm_client: LLMClient,
        *,
        registry: ExperimentRegistry | None = None,
        prompt_library: dict | None = None,
        default_prompt_id: str = "v3_stable",
    ) -> None:
        self._llm = llm_client
        self._registry = registry or ExperimentRegistry()
        self._prompt_library = prompt_library or {}
        self._default_prompt_id = default_prompt_id

    def _get_system_prompt(self, prompt_id: str | None) -> str:
        pid = prompt_id or self._default_prompt_id
        # prompt_library 顶层或 prompts 子项都支持
        if pid in self._prompt_library:
            return self._prompt_library[pid]
        prompts = self._prompt_library.get("prompts", {})
        if pid in prompts:
            return prompts[pid]
        raise KeyError(f"Unknown prompt_id={pid}")

    def decide_for_dataset(
        self,
        *,
        dataset_summary: DatasetSummary,
        symbol: str,
        label_id: str,
        prompt_id: str | None = None,
    ) -> BrainDecision:
        # 1) 从 Registry 选候选策略
        candidates: List[ExperimentRecord] = self._registry.select_best_runs(
            symbol=symbol,
            label_id=label_id,
            min_trades=50,
            max_mdd=0.2,   # TODO: 可以参数化
            top_k=10,
        )

        # 2) 构造 LLM 上下文
        system_prompt = self._get_system_prompt(prompt_id)
        user_prompt = build_brain_user_prompt(
            dataset_summary=dataset_summary,
            candidate_runs=candidates,
        )

        # 3) 调用 LLM —— 这里假设 LLMClient 返回 text，我们再 parse；如果你已经有结构化输出，就直接用
        raw = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        # raw 可能是 dict 或 str，兼容一下
        if isinstance(raw, dict):
            data = raw
        else:
            # 尝试找到第一段 JSON
            text = str(raw)
            # 简化：假设整个回答就是 JSON
            data = json.loads(text)

        decision = BrainDecision.parse_obj(data)
        return decision
```

> ✅ 这样：
>
> * 大脑的输出被 **BrainDecision schema** 严格约束；
> * 只在 `ExperimentRegistry.select_best_runs(...)` 返回的 run_id 里挑策略；
> * Prompt Library 受 `prompt_id` 白名单控制；
> * DatasetSummary 告诉大脑“这批数据值不值得用”。

---

## 小结：这三层目前的“落地状态”

### 1. 数据 & 特征 & 标签层

* ✅ ClickHouse 侧 DatasetSanity（基础统计 + 采样 + 高相关/零方差）
* ✅ DatasetSummary（给大脑看简版“体检报告”）
* ✅ FeatureRegistry / LabelRegistry（统一入口，防止 LLM 乱造特征/标签）
* ✅ PrincipalInvestigator 接入 Registry + Sanity + Summary

### 2. ML/HPO/实验治理层

* ✅ ExperimentRecord / Metrics 结构
* ✅ ExperimentRegistry（JSONL 存储 + 简单“选最优 run”接口）
* ✅ HPO 回写 ExperimentRecord 的调用示例（你需要在实际 HPO 脚本里插入）

### 3. AI 大脑 & Prompt 安全层

* ✅ BrainDecision schema（Pydantic 校验）
* ✅ DatasetSummary + CandidateRuns → user_prompt 的构造
* ✅ BrainController：从 Registry 拿候选 → 调用 LLM → parse & validate → 返回 BrainDecision

---

下一步如果你愿意，我们可以针对其中**任何一条链路做“端到端集成示例”**，比如：

> 从 `PrincipalInvestigator.propose` → HPO → 写 ExperimentRecord → BrainController.decide_for_dataset → 输出一个 `mode='select_strategies'` 的 JSON → 落到实盘策略选择。

你可以直接跟我说：「先把 PI → HPO → ExperimentRegistry 这一条打通」，我就专门写这一条的串联示例和 TODO。
