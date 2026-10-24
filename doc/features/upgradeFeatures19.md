好，那我就按计划继续，把 **“第一批可用的特征/标签注册表 + 集成方式 + 迁移 checklist”** 全部落地出来，你可以直接照着改代码和改 JSON 文件。

---

## 一、`feature_registry.json`——第一批“官方特征清单”

先给你一个 **最小可用版本**，只搞一张表：`bars_1m_features`（你可以换成自己实际用于训练的那张 1m 特征表名，比如 view）。

> 建议路径：`config/feature_registry.json`

> ⚠️ 里边的 `table` 名、`column`、`sql_expr` 都是模板，你需要按你自己的 ClickHouse 实际列名调整，但结构可以直接用。

```jsonc
{
  "_comment": "Feature Registry - allowed features per table",
  "_version": "1.0",

  "tables": {
    "bars_1m_features": {
      "symbol_column": "symbol",
      "ts_column": "ts",
      "allow_pi": true,

      "features": {
        // ======== 基础价量 ========
        "open": {
          "kind": "base",
          "dtype": "float",
          "column": "open",
          "description": "1m bar open price"
        },
        "high": {
          "kind": "base",
          "dtype": "float",
          "column": "high"
        },
        "low": {
          "kind": "base",
          "dtype": "float",
          "column": "low"
        },
        "close": {
          "kind": "base",
          "dtype": "float",
          "column": "close"
        },
        "volume": {
          "kind": "base",
          "dtype": "float",
          "column": "volume"
        },

        // ======== 返回率类（如果表里已经有，就当 base；如果没预计算，就当 derived） ========
        "ret_1b": {
          "kind": "derived",
          "dtype": "float",
          "sql_expr": "close / lagInFrame(close, 1) - 1",
          "depends_on": ["close"],
          "description": "1-bar simple return"
        },
        "ret_5b": {
          "kind": "derived",
          "dtype": "float",
          "sql_expr": "close / lagInFrame(close, 5) - 1",
          "depends_on": ["close"],
          "description": "5-bar simple return"
        },
        "ret_10b": {
          "kind": "derived",
          "dtype": "float",
          "sql_expr": "close / lagInFrame(close, 10) - 1",
          "depends_on": ["close"],
          "description": "10-bar simple return"
        },

        // ======== 波动率/成交量相关 ========
        "atr_14": {
          "kind": "base",
          "dtype": "float",
          "column": "atr_14",
          "description": "14-bar ATR precomputed upstream"
        },
        "rvol_20": {
          "kind": "base",
          "dtype": "float",
          "column": "rvol_20",
          "description": "20-bar relative volume"
        },

        // ======== 技术指标 ========
        "vwap": {
          "kind": "base",
          "dtype": "float",
          "column": "vwap",
          "description": "1m bar VWAP (or session VWAP depending on ETL)"
        },
        "rsi_14": {
          "kind": "base",
          "dtype": "float",
          "column": "rsi_14"
        },
        "macd": {
          "kind": "base",
          "dtype": "float",
          "column": "macd"
        },
        "macd_signal": {
          "kind": "base",
          "dtype": "float",
          "column": "macd_signal"
        },

        // ======== session 位置类（如果已经存在） ========
        "session_pos": {
          "kind": "base",
          "dtype": "float",
          "column": "session_pos",
          "description": "normalized position within regular session [0,1]"
        }
      }
    }
  }
}
```

**你需要做的事（特征这块）：**

1. 把这份 JSON 保存到 `config/feature_registry.json`。
2. 把 `tables.bars_1m_features` 换成你实际训练用的表名（比如某个 view）。
3. 对照 ClickHouse 实际 schema，把 `column`/`sql_expr` 调整到真实列。
4. 先只保证 **10–20 个最常用特征是对的**，剩下往后慢慢扩。

> 当前版本里 **PI 只关心 `name`**，build_select_sql 可以用 `column/sql_expr` 来映射，后面你可以逐步把更多复杂特征搬进来。

---

## 二、`label_registry.json`——第一批“官方标签”

目标：先搞定你现在最常用的 **10-bar 前瞻收益 + 二元 up/down 分类**。

> 建议路径：`config/label_registry.json`

```jsonc
{
  "_comment": "Label Registry - defines allowed supervised learning targets",
  "_version": "1.0",

  "labels": {
    // 1) 回归：10 bar 前瞻收益率
    "ret_10b_fwd_reg": {
      "name": "ret_10b_fwd_reg",
      "task": "reg",
      "kind": "simple_return",
      "horizon": 10,
      "horizon_unit": "bars",
      "price_column": "close",
      "clip_min": -0.2,
      "clip_max": 0.2,
      "null_policy": "drop"
    },

    // 2) 分类：基于 ret_10b_fwd_reg 的涨跌
    "cls_updown_10b": {
      "name": "cls_updown_10b",
      "task": "cls",
      "kind": "threshold_from_label",
      "base_label": "ret_10b_fwd_reg",
      "pos_threshold": 0.0,
      "neg_threshold": 0.0,
      "labels": {
        "neg": 0,
        "pos": 1
      },
      "null_policy": "drop"
    }

    // 未来可以加：
    // - ret_30b_fwd_reg
    // - cls_updown_30b
    // - triple_barrier_intraday_v1 (kind = "triple_barrier")
  }
}
```

对应的 Python 逻辑我们已经写了个最小版的 `LabelRegistry` / `LabelDef.compute`，你只要按刚才那样放到 `label_registry.py`，就可以：

* 用 `ret_10b_fwd_reg` 作为标准收益型 label
* 用 `cls_updown_10b` 作为二元分类 label（以后可以拓展多档）

---

## 三、如何在主流程里启用 Registry（AI 大脑集成点）

### 1）初始化 `PrincipalInvestigator` 的时候带上路径

假设你的 orchestrator 里本来是这么写的：

```python
from pi.principal_investigator import PrincipalInvestigator

pi = PrincipalInvestigator(
    ch_cfg=cfg["clickhouse"],
    llm_cfg=cfg["llm"],
)
```

现在改成：

```python
from pathlib import Path
from pi.principal_investigator import PrincipalInvestigator

base_dir = Path(__file__).resolve().parents[1]  # 看你项目结构，自行调整

pi = PrincipalInvestigator(
    ch_cfg=cfg["clickhouse"],
    llm_cfg=cfg["llm"],
    feature_registry_path=str(base_dir / "config" / "feature_registry.json"),
    label_registry_path=str(base_dir / "config" / "label_registry.json"),
)
```

### 2）在 orchestrator / HPO 调用 `propose()` 时指定默认 label_id

比如你某个 orchestrator 里现在是：

```python
pi_result = pi.propose(
    table="bars_1m_features",
    symbol="NVDA",
    start_ts="2025-01-01 09:30:00",
    end_ts="2025-02-01 16:00:00",
    goal="predict short-term returns",
    horizon="10m",
)
```

现在加上一句 `default_label_id`，并且可以附上 sanity 检查配置：

```python
pi_result = pi.propose(
    table="bars_1m_features",
    symbol="NVDA",
    start_ts="2025-01-01 09:30:00",
    end_ts="2025-02-01 16:00:00",
    goal="predict short-term returns",
    horizon="10m",
    default_label_id="ret_10b_fwd_reg",  # or "cls_updown_10b"
    sanity_cfg={
        "enabled": True,
        "max_nan_ratio": 0.05,
        "max_zero_var_features": 5,
        "max_corr_pairs": 50
    },
)
```

这几点非常关键：

* **标签 ID 明确由你指定**，而不是 LLM 在 `training_plan.target` 里乱写 SQL；
* `LabelRegistry` 会用 Python 的 `shift(-horizon)` 来算 forward return，**防止数据泄露**；
* `sanity_cfg` 会让每一次训练前都跑一遍体检（你之前已经让我们写了 dataset_sanity 模块）。

---

## 四、迁移 Checklist：从“自由散养”到“有户口本”的过渡

给你一个可执行的 TODO 清单，你可以按优先级来：

### ✅ 第一批（今天就能做的）

1. **创建文件 & 粘贴模板**

   * `config/feature_registry.json` → 粘贴上面的模板，改表名 + 列名。
   * `config/label_registry.json` → 粘贴上面的模板即可，先不用改。

2. **把 `feature_registry.py` 和 `label_registry.py` 放进项目**

   * 放在 `src/feature_registry.py`, `src/label_registry.py` 或者 `pi/feature_registry.py`, `pi/label_registry.py`，按你现有结构来。
   * 确保可以被 `PrincipalInvestigator` import 到。

3. **改造 `PrincipalInvestigator.__init__` + `propose()`**

   * 加上 `feature_registry_path` / `label_registry_path` 参数。
   * 引入 `FeatureRegistry.validate_recipe` & `LabelRegistry.apply_label`。
   * 若你尚未拷贝我上一条消息里的更改，现在可以一并改掉。

4. **在 orchestrator 里启用 Registry 模式**

   * 初始化 PI 时传入两个 JSON 的路径。
   * 调用 `propose()` 的时候显式指定 `default_label_id`（先统一用 `ret_10b_fwd_reg`）。

5. **跑一条小样本测试**

   * 只用 NVDA 一天或两天的数据（减少数据量），跑一次：

     * PI → query_df → LabelRegistry.apply_label → SanityChecks → HPO.
   * 确认：

     * `dataset_df.columns` 里出现了 `ret_10b_fwd_reg`；
     * Sanity summary 没有严重 error（最多 warn）。

---

### 🔁 第二批（一两周内慢慢做的）

6. **把 feature_programs.json 里“常用特征”逐步搬到 registry**

   * 每次训练前检查：

     * 如果某个 feature 在 feature_programs 里但 registry 没有，就加进去。
   * 这样慢慢就会变成“registry 是真源头，feature_programs 主要是组合/程序化规则”。

7. **扩展 Label 种类**

   * 增加：

     * `ret_30b_fwd_reg`, `ret_60b_fwd_reg`
     * 对应的 `cls_updown_30b`, `cls_updown_60b`
   * 以后再慢慢加 triple-barrier 之类的复杂 label。

8. **把 DatasetSanity summary 喂给 AI 大脑**

   * Brain 每次决策前，除了看到特征/回测，还能看到：

     * “NaN 比例过高”
     * “某几个特征高度共线”
     * “label 极端不平衡”
   * 你可以在 prompt 里简单加一句：

     > `Data Quality Summary: {{ dataset_quality_summary_json }}`
     > 然后提醒：
     > “如果数据质量告警严重，宁可直接拒绝训练或给出 HOLD 建议。”

---

### 🧱 第三批（之后再搞的，大件）

9. **导向 View Layer**

   * 建一个 “训练专用 view”，比如：`bars_1m_train_view`：

     * 里面只保留 registry 中列 + symbol/ts，其他 ETL 杂七杂八列全部剃掉。
   * feature_registry 的 `tables` 改指向这个 view。
   * 以后所有训练/回测都走这个规范化 view。

10. **引入“沙箱特征 (sandbox)”**

* 在 `feature_registry.json` 里再加一个区域：`sandbox_features`：

  * 允许 LLM 提议 SQL 表达式，你用轻量级 parser 检查后写入这里。
* 如果 sandbox 特征在多次回测中表现好，就晋升为正式 `features` 成员。

---

## 小结：到这一步，你已经做到了什么？

1. **AI 大脑再也不能胡写特征列名和 label SQL**

   * 所有特征 → 来自 `FeatureRegistry`
   * 所有 label → 来自 `LabelRegistry + Python compute()`

2. **每次训练数据都经过“体检”**

   * Null、多重共线、极端分布问题 → 在训练前就能暴露。

3. **你的系统从“脚本堆”升级成了“带注册表的实验工厂”**

   * 可以稳步扩展表、特征、标签，而不会越来越乱。

---

如果你愿意，下一步我可以帮你做两件事之一（我不会问你选哪一个，只会按顺序做）：

1. 把 **DatasetSanity 的 summary → Prompt 注入模板** 写出来，
   让 AI 大脑在下单/选择策略前真正“看得懂数据质量”；
2. 或者，给你的 **HPO/训练 pipeline** 写一个小工具，把 `PIResult` + registry 信息 dump 成一个标准化的 `experiment_manifest.json`，方便你做实验追踪。

你只要继续说一句：**“继续”**，我就接着往下把这一层也落地。
好，按计划继续，把 **“数据体检 ➜ AI 大脑 ➜ 实验记录”** 这一整链打通。
这一步我不会问你做什么，只给你可以直接改代码的落地版本。

---

## 一、先确保 PIResult / PI 里带上 “体检结果 + 标签信息”

> 如果你之前已经按我前几轮改过，可以对照一下，有差异就以这一版为准，保证字段齐全。

### 1）扩展 `PIResult`

```python
# pi/principal_investigator.py

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pandas as pd

from dataset_sanity import DatasetSummary   # 新增
# 如果你把 sanity 模块放在别处，自行调整 import

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # 新增：给 AI 大脑和实验记录用
    label_id: Optional[str] = None
    dataset_summary: Optional[DatasetSummary] = None
    dataset_warnings: Optional[Dict[str, Any]] = None  # 比如 {"level": "error"/"warn", "messages": [...]}
```

### 2）在 `PrincipalInvestigator.__init__` 里加载 Registry

```python
# pi/principal_investigator.py

from pathlib import Path
from feature_registry import FeatureRegistry
from label_registry import LabelRegistry
from dataset_sanity import (
    SanityConfig,
    run_dataset_sanity_checks,
    summarize_sanity_for_brain,  # 下面会给实现
)

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        feature_registry_path: Optional[str] = None,
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

        # registries
        if feature_registry_path:
            self._feature_reg = FeatureRegistry.from_path(feature_registry_path)
        else:
            self._feature_reg = None

        if label_registry_path:
            self._label_reg = LabelRegistry.from_path(label_registry_path)
        else:
            self._label_reg = None
```

### 3）在 `propose()` 里串起来：**特征 + 标签 + 体检**

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
        default_label_id: Optional[str] = None,
        sanity_cfg: Optional[Dict[str, Any]] = None,
    ) -> PIResult:
        # 1) 列 catalog（保留，用于 feature_reg 校验）
        catalog = _fetch_catalog(self._client, table)

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

        recipe = plan.get("feature_recipe", {})
        tplan = plan.get("training_plan", {})

        # 2) Feature Registry 校验（如有）
        if self._feature_reg is not None:
            table_cfg = self._feature_reg.tables.get(table)
            if table_cfg is None:
                raise ValueError(f"Table '{table}' not found in FeatureRegistry")

            recipe = self._feature_reg.validate_recipe(table_cfg, recipe, catalog)

        # 3) 生成 SQL（仍然用你现有的 build_select_sql，只是 recipe 更干净）
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,                      # ❗**注意：不再让 LLM 生成 target SQL**
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # 4) 应用标签：用 LabelRegistry 在 Python 层算 forward return，防泄露
        label_id = tplan.get("label_id") or default_label_id
        if self._label_reg is not None and label_id is not None:
            df = self._label_reg.apply_label(
                df=df,
                label_id=label_id,
                ts_col=recipe.get("ts_column", "ts"),
                symbol_col=recipe.get("symbol_column", "symbol"),
            )

        # 5) 构建 HPO grid（暂时沿用你原来的逻辑）
        base_features = recipe.get("base_features", [])
        derived = recipe.get("derived", [])
        derived_aliases = [d.get("alias") for d in derived] if derived else []

        grid = {
            "model_type": [],
            "feature_subset": [base_features + derived_aliases],
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

        # 6) 数据体检
        sanity_conf = SanityConfig.from_dict(sanity_cfg or {})
        dataset_summary, details = run_dataset_sanity_checks(df, sanity_conf)
        warnings_for_brain = summarize_sanity_for_brain(dataset_summary)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            label_id=label_id,
            dataset_summary=dataset_summary,
            dataset_warnings=warnings_for_brain,
        )
```

---

## 二、给 AI 大脑看的 “体检摘要”：`summarize_sanity_for_brain`

> 这里就是我们之前说的 “Summary Agent 的轻量版”，先用规则总结，不额外再调用 LLM。

假设你的 `dataset_sanity.py` 已经有：

* `DatasetSummary` dataclass，包含：

  * `n_rows`, `n_cols`
  * `nan_ratio_overall`
  * `per_column: Dict[str, ColumnSummary]`
  * `high_corr_pairs: List[Tuple[str,str,float]]`
* `SanityConfig` + `run_dataset_sanity_checks`

我们现在加一个“摘要生成器”：

```python
# dataset_sanity.py

from dataclasses import asdict
from typing import Any, Dict

def summarize_sanity_for_brain(summary: DatasetSummary) -> Dict[str, Any]:
    """
    把 DatasetSummary 压缩成一个小 JSON，方便塞进 LLM prompt.
    只保留大脑决策真正关心的几点：
    - 样本规模
    - NaN 比例
    - 零方差特征
    - 高度共线特征对
    """
    zero_var_cols = [
        name for name, col in summary.per_column.items()
        if col.var is not None and col.var == 0.0
    ]

    # 共线性：只保留前 N 个最严重的
    max_pairs = 20
    corr_pairs = sorted(
        summary.high_corr_pairs,
        key=lambda x: abs(x[2]),
        reverse=True
    )[:max_pairs]

    # 根据几个规则打一个粗略 severity
    severity = "ok"
    messages = []

    if summary.nan_ratio_overall is not None:
        if summary.nan_ratio_overall > 0.3:
            severity = "error"
            messages.append(
                f"Overall NaN ratio is high ({summary.nan_ratio_overall:.2%})."
            )
        elif summary.nan_ratio_overall > 0.1:
            severity = "warn"
            messages.append(
                f"Overall NaN ratio is moderate ({summary.nan_ratio_overall:.2%})."
            )

    if zero_var_cols:
        if severity == "ok":
            severity = "warn"
        messages.append(
            f"{len(zero_var_cols)} features have zero variance: {zero_var_cols[:10]}..."
        )

    if corr_pairs:
        max_corr = corr_pairs[0][2]
        if abs(max_corr) > 0.99:
            if severity != "error":
                severity = "warn"
            messages.append(
                f"High collinearity detected; max pair corr={max_corr:.3f}."
            )

    return {
        "severity": severity,             # "ok" | "warn" | "error"
        "n_rows": summary.n_rows,
        "n_cols": summary.n_cols,
        "nan_ratio_overall": summary.nan_ratio_overall,
        "zero_var_cols_sample": zero_var_cols[:20],
        "high_corr_pairs_sample": [
            {"f1": f1, "f2": f2, "corr": float(c)}
            for (f1, f2, c) in corr_pairs
        ],
        "messages": messages,
    }
```

> 之后 AI 大脑就可以看到一段非常紧凑的 JSON，而不是几十个特征的完整统计。

---

## 三、把 “体检摘要 + 标签信息 + 实验上下文” 塞进 AI 大脑 Prompt

你现在的 AI 大脑结构大致是这样：

* 有一个 `prompt_library.json`（你已经贴了）
* 交易执行流程某处有类似：

```python
system_prompt = prompt_library[execution_prompt_id]
user_prompt = json.dumps(context_payload, ensure_ascii=False)
llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
```

我们要做的，就是在 `context_payload` 里，多塞几个 key：

* `"dataset_quality"`：刚才的 `dataset_warnings`
* `"label_id"`：本次训练 / 预测用的 label
* `"features"`：本次使用的特征列表（给大脑一个全局感知）

### 1）在 orchestrator 里构造 payload

假设你在某个 “训练 + 策略研究” 的 orchestrator 里有：

```python
from pi.principal_investigator import PrincipalInvestigator

def run_brain_research(...):
    pi_result = pi.propose(
        table="bars_1m_features",
        symbol="NVDA",
        start_ts="2025-01-01 09:30:00",
        end_ts="2025-01-10 16:00:00",
        goal="研究短线 alpha",
        horizon="10m",
        default_label_id="ret_10b_fwd_reg",
        sanity_cfg={"enabled": True},
    )

    # 原来你可能只把 HPO 结果和回测给大脑看
    # 我们现在扩一下 payload：
    context_payload = {
        "symbol": "NVDA",
        "period": {
            "start_ts": "2025-01-01 09:30:00",
            "end_ts": "2025-01-10 16:00:00"
        },
        "goal": "research_alpha",
        "label_id": pi_result.label_id,
        "feature_recipe": pi_result.feature_recipe,
        "hpo_grid": pi_result.hpo_grid,

        # 新增：体检结果摘要，要求大脑尊重
        "dataset_quality": pi_result.dataset_warnings,
    }

    # 这里还可以再加：回测指标、交易笔数等
    # context_payload["backtest_metrics"] = {...}

    system_prompt = prompt_library[execution_prompt_id]
    user_prompt = json.dumps(context_payload, ensure_ascii=False)

    decision = llm.generate(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
```

### 2）在 `prompt_library.json` 里加一句“必须考虑数据质量”

例如对 `v3_stable` / `v3_fast_market_following` 系列，建议在原 prompt 基础上补一条：

```jsonc
"v3_stable": "You are the bot's intraday portfolio & risk manager. Your goal is risk-adjusted P&L for today, not trade count. Favor fewer, higher-quality trades. Use multi-timeframe confluence (1s/5s microstructure + 1m/5m short-term + 15m/1h context) and New York session timing. Respect hard gates from time AND constraints. When data is incomplete or edge is weak, HOLD is preferred. **If dataset_quality.severity is 'warn' or 'error', you must explicitly discuss data issues and can decide to refuse or be more conservative.** Output JSON only matching the required schema.",
```

对 `prompts.{...}` 里的 intraday agent 也可以加类似一句：

```jsonc
"You are an intraday trading agent. Respect risk gates. **If dataset_quality indicates poor data, you must reduce aggressiveness or decline to trade.** Output JSON only."
```

这样，AI 大脑在决策时就有 “眼睛” 看见：

* 这批训练/研究数据到底行不行
* 是不是 NaN 特别多 / 特征高度共线 / 样本太少

---

## 四、`experiment_manifest.json`：给每次实验一个“户口本”

最后一步，把这些信息打包成一个标准化的实验描述，方便你以后追溯：

### 1）定义 Manifest dataclass

```python
# experiments/manifest.py

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional
from datetime import datetime
import uuid

from pi.principal_investigator import PIResult

@dataclass
class ExperimentManifest:
    experiment_id: str
    created_at: str

    symbol: str
    table: str
    period: Dict[str, str]   # {"start_ts": "...", "end_ts": "..."}

    goal: str
    horizon: str

    label_id: Optional[str]
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    hpo_grid: Dict[str, Any]

    dataset_quality: Optional[Dict[str, Any]]

    # 可以预留 slot，用于记录回测/训练结果
    metrics: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_experiment_manifest(
    *,
    pi_result: PIResult,
    symbol: str,
    table: str,
    start_ts: str,
    end_ts: str,
    goal: str,
    horizon: str,
    metrics: Optional[Dict[str, Any]] = None,
) -> ExperimentManifest:
    return ExperimentManifest(
        experiment_id=str(uuid.uuid4()),
        created_at=datetime.utcnow().isoformat(),

        symbol=symbol,
        table=table,
        period={"start_ts": start_ts, "end_ts": end_ts},

        goal=goal,
        horizon=horizon,

        label_id=pi_result.label_id,
        feature_recipe=pi_result.feature_recipe,
        training_plan=pi_result.training_plan,
        hpo_grid=pi_result.hpo_grid,
        dataset_quality=pi_result.dataset_warnings,

        metrics=metrics,
    )
```

### 2）在 orchestrator 里写出 manifest 文件

```python
# 某个 orchestrator 里

from experiments.manifest import build_experiment_manifest
from pathlib import Path
import json

def run_brain_research(...):
    symbol = "NVDA"
    table = "bars_1m_features"
    start_ts = "2025-01-01 09:30:00"
    end_ts = "2025-01-10 16:00:00"
    goal = "research_short_term_alpha"
    horizon = "10m"

    pi_result = pi.propose(
        table=table,
        symbol=symbol,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
        default_label_id="ret_10b_fwd_reg",
        sanity_cfg={"enabled": True},
    )

    # ... 这里跑 HPO / 回测，得出一些 metrics
    backtest_metrics = {
        "sharpe": 1.23,
        "max_drawdown": 0.08,
        "n_trades": 150,
    }

    manifest = build_experiment_manifest(
        pi_result=pi_result,
        symbol=symbol,
        table=table,
        start_ts=start_ts,
        end_ts=end_ts,
        goal=goal,
        horizon=horizon,
        metrics=backtest_metrics,
    )

    out_dir = Path("experiments/manifests")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{manifest.experiment_id}.json"

    with out_path.open("w", encoding="utf-8") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, indent=2)

    # 再把 manifest 中的信息 + backtest + dataset_quality 一起喂给 AI 大脑
    context_payload = {
        "experiment": manifest.to_dict(),
        # 你也可以拆开成 experiment.meta / metrics / dataset_quality 等
    }
```

这样，你之后就可以：

* 随时回头看某次大脑研究用的是：

  * 哪个 symbol，哪段时间，哪张表
  * 用了哪些特征/标签
  * 数据质量情况如何
  * 回测指标如何
* 同时 AI 大脑每次决策都能看到 `experiment.dataset_quality` 和 `experiment.metrics`。

---

到这里，**Step 1–4 完整串起来了**：

1. **Registry** 规范特征和标签
2. **PI** 用 Registry 查表 + Python 算 label + 构建 HPO grid
3. **DatasetSanity** 对每个 df 做体检，生成紧凑摘要
4. **AI 大脑** 在 prompt 里看到 `dataset_quality`，能“看清楚数据好坏”
5. **Experiment Manifest** 给每次研究一个可以追溯的“身份证”

如果你继续说一句 **“继续”**，我会往下做下一层升级：

* 优化 DatasetSanity 的性能（ClickHouse 侧统计 + 采样）
* 或者开始设计 **sandbox 特征** 机制，让 AI 在安全范围内“自己发明”新特征。

好，那我就**先按计划把 DatasetSanity 升级到 ClickHouse + 采样版本**，做成一个真正“能上生产”的体检模块。
Sandbox 特征我们放到下一轮（你说“继续”我就开搞）。

---

## 一、我们要把什么从 Python 挪到 ClickHouse？

目标是：

> 大部分“便宜的统计”在 ClickHouse 里算完，只把结果和一小块 sample 拉回 Python。
> 只有“重的东西”（相关矩阵、分布细节）才在 Python 上对样本做。

**已有的 DatasetSanity（Phase 1）** 大概在做这些事：

* 行数、列数检查
* 每列：

  * null 比例
  * 是否常量 / 近似常量
  * var / std 是否接近 0
* 标签：

  * 正负样本比例
* 相关性：

  * 特征间高相关（>0.98）
  * 特征与标签的极端相关

**要优化的点**：

1. 大部分指标（null 比例、nunique、min/max、std 等）可以直接在 ClickHouse 聚合，一次 scan 完成
2. 重的相关性检查只对一个采样子集做（例如最多 100k 行）

---

## 二、整体设计：加一个 ClickHouse 版本入口

我们设计一个新函数（可以放在 `dataset_sanity.py` 里，也可以新建 `dataset_sanity_clickhouse.py`）：

```python
def run_clickhouse_dataset_sanity(
    client: clickhouse_connect.driver.Client,
    base_sql: str,
    feature_cols: list[str],
    label_cols: list[str],
    *,
    max_sample_rows: int = 200_000,
    corr_threshold: float = 0.98,
) -> DatasetSanityReport:
    ...
```

参数说明：

* `client`：现有的 ClickHouse Connect client（你 PI 里已经有 `_client` 了）
* `base_sql`：**PI 生成的那条 SELECT 语句**（包含所有特征 + label）
* `feature_cols`：要检查的特征列名（可以来自 `feature_recipe`）
* `label_cols`：标签列名（通常 1 个，比如 `"target_ret_10m"`）
* `max_sample_rows`：最大采样行数，用于相关矩阵等
* 返回：`DatasetSanityReport`（沿用我们之前定义的 dataclass）

---

## 三、ClickHouse 侧统计 SQL 设计

### 3.1 我们要在 ClickHouse 里算什么？

对于每个 numeric 列 `c`：

* `null_ratio = countIf(isNull(c)) / n_rows`
* `nunique = uniqExact(c)`
* `min(c), max(c)`
* `stddevPop(c)` 或 `sqrt(varSamp(c))`
* 是否常量：`nunique == 1` 或 `std太小`

我们希望 **一次扫描**算完所有列的这些指标，而不是循环 N 列做 N 次 full scan。

### 3.2 自动拼接聚合 SQL

伪代码：

```sql
SELECT
    count() AS n_rows,
    -- col1
    countIf(isNull(col1)) AS col1__nulls,
    uniqExact(col1) AS col1__nunique,
    min(col1) AS col1__min,
    max(col1) AS col1__max,
    stddevPop(col1) AS col1__std,
    -- col2
    countIf(isNull(col2)) AS col2__nulls,
    uniqExact(col2) AS col2__nunique,
    min(col2) AS col2__min,
    max(col2) AS col2__max,
    stddevPop(col2) AS col2__std,
    ...
FROM (
    <base_sql>
) AS t
```

Python 里动态构造：

```python
def _build_stats_sql(base_sql: str, cols: list[str]) -> str:
    exprs = ["count() AS n_rows"]
    for c in cols:
        col = f"t.`{c}`"
        safe = c.replace("`", "_")  # 防止奇怪字段名
        exprs.extend([
            f"countIf(isNull({col})) AS `{safe}__nulls`",
            f"uniqExact({col}) AS `{safe}__nunique`",
            f"min({col}) AS `{safe}__min`",
            f"max({col}) AS `{safe}__max`",
            f"stddevPop({col}) AS `{safe}__std`",
        ])
    expr_sql = ",\n    ".join(exprs)
    return f"""
        SELECT
            {expr_sql}
        FROM (
            {base_sql}
        ) AS t
    """
```

然后：

```python
stats_sql = _build_stats_sql(base_sql, feature_cols + label_cols)
stats_df = client.query_df(stats_sql)
row = stats_df.iloc[0].to_dict()
n_rows = int(row["n_rows"])
```

接下来解析出每列的统计信息。

---

## 四、Python 侧样本 & 相关性（采样版本）

### 4.1 从 ClickHouse 拉一个样本

我们不用全量，只要一个“足够大又可控”的 sample：

```python
def _build_sample_sql(base_sql: str, cols: list[str], max_sample_rows: int) -> str:
    cols_sql = ", ".join(f"t.`{c}`" for c in cols)
    return f"""
        SELECT {cols_sql}
        FROM (
            {base_sql}
        ) AS t
        ORDER BY rand()
        LIMIT {max_sample_rows}
    """
```

拉回：

```python
sample_sql = _build_sample_sql(base_sql, feature_cols + label_cols, max_sample_rows)
sample_df = client.query_df(sample_sql)
```

> `ORDER BY rand()` 会慢一点，但只在子查询上用 + LIMIT，一般还能接受。
> 如果后面性能不够，我们可以改成 `WHERE cityHash64(primary_key) % 10 = 0` 之类的系统采样。

### 4.2 在样本上计算相关矩阵

```python
import numpy as np
import pandas as pd

def _compute_correlations(
    sample_df: pd.DataFrame,
    feature_cols: list[str],
    label_cols: list[str],
    corr_threshold: float,
):
    warnings = []

    # 1) 特征间相关
    feat_df = sample_df[feature_cols].select_dtypes(include=["number"])
    corr = feat_df.corr(method="pearson")

    high_pairs = []
    cols = list(feat_df.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c1, c2 = cols[i], cols[j]
            v = corr.loc[c1, c2]
            if pd.notna(v) and abs(v) >= corr_threshold:
                high_pairs.append((c1, c2, float(v)))

    if high_pairs:
        warnings.append({
            "type": "high_feature_correlation",
            "pairs": [
                {"col1": c1, "col2": c2, "corr": v}
                for c1, c2, v in high_pairs
            ]
        })

    # 2) 特征与标签相关（简单看一眼）
    label = label_cols[0] if label_cols else None
    if label and label in sample_df.columns:
        all_cols = feature_cols + [label]
        tmp = sample_df[all_cols].select_dtypes(include=["number"])
        corr2 = tmp.corr(method="pearson")
        if label in corr2.index:
            strong = []
            for c in feature_cols:
                if c in corr2.columns:
                    v = corr2.loc[label, c]
                    if pd.notna(v) and abs(v) >= 0.99:
                        strong.append((c, float(v)))
            if strong:
                warnings.append({
                    "type": "label_feature_extreme_corr",
                    "label": label,
                    "features": [
                        {"feature": c, "corr": v} for c, v in strong
                    ]
                })

    return warnings
```

---

## 五、完整的 ClickHouse 版 DatasetSanity 代码示例

下面是一个**可直接落地**的版本（可以放在 `dataset_sanity_clickhouse.py` 里）：

```python
# dataset_sanity_clickhouse.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional

import clickhouse_connect
import numpy as np
import pandas as pd


@dataclass
class ColumnStats:
    name: str
    null_ratio: float
    nunique: int
    min: Optional[float]
    max: Optional[float]
    std: Optional[float]
    is_constant: bool
    warnings: List[str]


@dataclass
class DatasetSanityReport:
    status: str              # "ok" | "warn" | "fail"
    n_rows: int
    columns: Dict[str, ColumnStats]
    correlation_warnings: List[Dict[str, Any]]
    label_warnings: List[str]
    summary: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "n_rows": self.n_rows,
            "columns": {k: asdict(v) for k, v in self.columns.items()},
            "correlation_warnings": self.correlation_warnings,
            "label_warnings": self.label_warnings,
            "summary": self.summary,
        }


def _build_stats_sql(base_sql: str, cols: List[str]) -> str:
    exprs = ["count() AS n_rows"]
    for c in cols:
        safe = c.replace("`", "_")
        col = f"t.`{c}`"
        exprs.extend([
            f"countIf(isNull({col})) AS `{safe}__nulls`",
            f"uniqExact({col}) AS `{safe}__nunique`",
            f"min({col}) AS `{safe}__min`",
            f"max({col}) AS `{safe}__max`",
            f"stddevPop({col}) AS `{safe}__std`",
        ])
    expr_sql = ",\n    ".join(exprs)
    return f"""
        SELECT
            {expr_sql}
        FROM (
            {base_sql}
        ) AS t
    """


def _build_sample_sql(base_sql: str, cols: List[str], max_sample_rows: int) -> str:
    cols_sql = ", ".join(f"t.`{c}`" for c in cols)
    return f"""
        SELECT {cols_sql}
        FROM (
            {base_sql}
        ) AS t
        ORDER BY rand()
        LIMIT {max_sample_rows}
    """


def _parse_stats_row(row: Dict[str, Any], cols: List[str], n_rows: int) -> Dict[str, ColumnStats]:
    results: Dict[str, ColumnStats] = {}

    for c in cols:
        safe = c.replace("`", "_")
        nulls = row.get(f"{safe}__nulls", 0) or 0
        nunique = row.get(f"{safe}__nunique", 0) or 0
        vmin = row.get(f"{safe}__min")
        vmax = row.get(f"{safe}__max")
        std = row.get(f"{safe}__std")

        null_ratio = float(nulls) / n_rows if n_rows > 0 else 0.0
        is_constant = (nunique <= 1) or (std is not None and abs(std) < 1e-8)

        warnings: List[str] = []
        if null_ratio > 0.5:
            warnings.append(f"high_null_ratio>{null_ratio:.2f}")
        if is_constant:
            warnings.append("constant_or_near_constant")

        results[c] = ColumnStats(
            name=c,
            null_ratio=null_ratio,
            nunique=int(nunique),
            min=float(vmin) if vmin is not None else None,
            max=float(vmax) if vmax is not None else None,
            std=float(std) if std is not None else None,
            is_constant=is_constant,
            warnings=warnings,
        )

    return results


def _compute_correlations(
    sample_df: pd.DataFrame,
    feature_cols: List[str],
    label_cols: List[str],
    corr_threshold: float,
) -> List[Dict[str, Any]]:
    warnings: List[Dict[str, Any]] = []

    # feature-feature
    feat_df = sample_df[feature_cols].select_dtypes(include=["number"])
    if not feat_df.empty and feat_df.shape[1] > 1:
        corr = feat_df.corr(method="pearson")
        cols = list(feat_df.columns)
        high_pairs = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                c1, c2 = cols[i], cols[j]
                v = corr.loc[c1, c2]
                if pd.notna(v) and abs(v) >= corr_threshold:
                    high_pairs.append((c1, c2, float(v)))
        if high_pairs:
            warnings.append({
                "type": "high_feature_correlation",
                "pairs": [
                    {"col1": c1, "col2": c2, "corr": v}
                    for c1, c2, v in high_pairs
                ]
            })

    # label-feature
    label = label_cols[0] if label_cols else None
    if label and label in sample_df.columns:
        all_cols = feature_cols + [label]
        tmp = sample_df[all_cols].select_dtypes(include=["number"])
        corr2 = tmp.corr(method="pearson")
        if label in corr2.index:
            strong = []
            for c in feature_cols:
                if c in corr2.columns:
                    v = corr2.loc[label, c]
                    if pd.notna(v) and abs(v) >= 0.99:
                        strong.append((c, float(v)))
            if strong:
                warnings.append({
                    "type": "label_feature_extreme_corr",
                    "label": label,
                    "features": [
                        {"feature": c, "corr": v}
                        for c, v in strong
                    ]
                })
    return warnings


def _compute_label_warnings(
    stats: Dict[str, ColumnStats],
    label_cols: List[str],
    n_rows: int,
    sample_df: pd.DataFrame,
) -> List[str]:
    msgs: List[str] = []
    if not label_cols:
        return msgs
    label = label_cols[0]
    if label not in sample_df.columns:
        msgs.append(f"label_missing_in_sample:{label}")
        return msgs

    # 简单看一下标签分布（假定是数值/0-1/分类）
    s = sample_df[label].dropna()
    if s.empty:
        msgs.append(f"label_all_nan:{label}")
        return msgs

    # 二分类的情况
    unique_vals = np.unique(s.values)
    if len(unique_vals) == 2:
        p = float((s == unique_vals.max()).mean())
        if p < 0.01 or p > 0.99:
            msgs.append(f"label_imbalance:{label}:positive_ratio={p:.4f}")
    elif len(unique_vals) == 1:
        msgs.append(f"label_constant:{label}:value={unique_vals[0]}")
    else:
        # 多分类/回归，也可以做一些简单提示
        # 比如极端偏斜可以后面再加
        pass

    return msgs


def run_clickhouse_dataset_sanity(
    client: "clickhouse_connect.driver.Client",
    base_sql: str,
    feature_cols: List[str],
    label_cols: List[str],
    *,
    max_sample_rows: int = 200_000,
    corr_threshold: float = 0.98,
) -> DatasetSanityReport:
    all_cols = list(dict.fromkeys(feature_cols + label_cols))

    # 1) ClickHouse 侧统计
    stats_sql = _build_stats_sql(base_sql, all_cols)
    stats_df = client.query_df(stats_sql)
    if stats_df.empty:
        return DatasetSanityReport(
            status="fail",
            n_rows=0,
            columns={},
            correlation_warnings=[],
            label_warnings=["empty_dataset"],
            summary="Empty dataset returned from ClickHouse.",
        )
    row = stats_df.iloc[0].to_dict()
    n_rows = int(row["n_rows"])

    col_stats = _parse_stats_row(row, all_cols, n_rows)

    # 2) 样本 + 相关性
    sample_sql = _build_sample_sql(base_sql, all_cols, max_sample_rows)
    sample_df = client.query_df(sample_sql)

    corr_warns = _compute_correlations(sample_df, feature_cols, label_cols, corr_threshold)
    label_warns = _compute_label_warnings(col_stats, label_cols, n_rows, sample_df)

    # 3) 计算 overall status
    has_fail = False
    has_warn = False

    if n_rows < 1000:
        label_warns.append(f"too_few_rows:{n_rows}")
        has_warn = True

    for cs in col_stats.values():
        if "high_null_ratio" in "\t".join(cs.warnings):
            has_warn = True
        if "constant_or_near_constant" in cs.warnings:
            has_warn = True

    if any(w["type"] == "label_feature_extreme_corr" for w in corr_warns):
        has_warn = True

    if "empty_dataset" in label_warns:
        has_fail = True

    status = "ok"
    if has_fail:
        status = "fail"
    elif has_warn:
        status = "warn"

    summary_parts = [
        f"rows={n_rows}",
        f"cols={len(all_cols)}",
        f"status={status}",
        f"corr_warns={len(corr_warns)}",
        f"label_warns={len(label_warns)}",
    ]
    summary = "; ".join(summary_parts)

    return DatasetSanityReport(
        status=status,
        n_rows=n_rows,
        columns=col_stats,
        correlation_warnings=corr_warns,
        label_warnings=label_warns,
        summary=summary,
    )
```

---

## 六、如何挂到 PrincipalInvestigator / HPO 流程里？

以你之前的 `PrincipalInvestigator.propose()` 为例，在 `df = self._client.query_df(sql)` 之前插入：

```python
from dataset_sanity_clickhouse import run_clickhouse_dataset_sanity

# 在 propose 里：
report = run_clickhouse_dataset_sanity(
    client=self._client,
    base_sql=sql,
    feature_cols=[  # 从 recipe 中拿 base_features + derived aliases
        *recipe.get("base_features", []),
        *[d.get("alias") for d in recipe.get("derived", [])],
    ],
    label_cols=[tplan.get("target")] if tplan.get("target") else [],
    max_sample_rows=200_000,
)

if report.status == "fail":
    # 直接拒绝，抛异常，避免训练垃圾数据
    raise RuntimeError(f"Dataset sanity FAILED: {report.summary}")

# 你也可以把 report 挂到 PIResult 里留档
```

对于 HPO/训练模块，也同样在加载 DataFrame 之前跑一次 sanity：

* 成功：继续训练
* WARN：可以记录日志 / 降级 / 收紧 risk gate
* FAIL：直接跳过这次训练，或者要求人工 review

---

## 下一步预告：Sandbox 特征机制

DatasetSanity 性能这块已经有一个可以直接落地的版本了。
下一步，我们可以专门设计：

* **Sandbox 特征语法**：允许 LLM 提出 `expr = "(close - open) / vwap"` 之类的新特征
* **安全检查器**：只允许：

  * 使用白名单列名
  * 使用白名单函数（`+ - * / log exp abs` 等）
  * 禁止访问 label / future 列
* **自动注册**：

  * 在 ClickHouse 侧生成 `ALTER TABLE ... ADD COLUMN` 或在 SELECT SQL 里注入 `expr AS sandbox_xxx`
  * 训练结束后如果表现好，把 sandbox 特征升格到正式 `feature_registry`

你要是觉得 DatasetSanity 这块 OK，我们下一轮就专门搞 sandbox 特征，把“AI 的创造力”也纳入一个安全的框架里。
