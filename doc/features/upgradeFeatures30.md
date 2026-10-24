好，我们来把 **Sandbox 特征机制** 真正落地成代码，让 AI 可以在“安全围栏”里自己发明新特征，而且每个新特征都可追踪、可审计、可复用。

下面这一轮你可以直接抄到项目里用，不需要你做设计决策。

---

## 一、Sandbox 特征要解决什么？

你现在的痛点是：

* PI 里 `feature_recipe["derived"]` 完全是 LLM 想怎么写 SQL 就怎么写 →
  **有创造力，但危险**：可能

  * 引用不存在的列、
  * 写出有未来泄露的表达式、
  * 或者写出复杂到 ClickHouse 跑不动的式子。

* 但如果完全只允许用静态 `feature_registry`，又会让 AI 失去“发明新因子”的能力。

**Sandbox 机制的目标：**

1. 允许 LLM 提出新特征表达式；
2. 用一个 **表达式安全守卫（FeatureExpressionGuard）** 严格审核；
3. 通过的特征：

   * 写入 `sandbox_features.jsonl`（或者以后写入 CK 表）做审计；
   * 可以在这次训练中使用，但有明确标记：`status = "experimental"`；
4. 未来表现好的 Sandbox 特征，可以**晋升**为正式 `feature_registry` 成员。

---

## 二、新文件：`features/feature_sandbox.py`

> 作用：
>
> * 定义 SandboxFeature 结构
> * 定义 FeatureExpressionGuard（表达式安全检查）
> * 定义 SandboxFeatureRegistry（JSONL 版简单 registry）

你可以新建一个目录 `features/`，里面放这个文件。

```python
# features/feature_sandbox.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import json
import re
from datetime import datetime
import uuid


# =========================
# 1. 表达式安全检查配置
# =========================

@dataclass
class FeatureExpressionGuardConfig:
    """
    控制 Sandbox 特征表达式安全性的配置。

    设计哲学：第一版偏保守，先保证“不会作死”，
    再逐步放开允许更多函数/结构。
    """

    # 允许使用的列名（必须在 PI 的 feature_catalog 里）
    allowed_columns: Set[str]

    # 允许使用的函数名（lowercase），例如 {"abs", "greatest", "least"}
    # 初始可以为空，只允许列 + 常数 + 四则运算
    allowed_functions: Set[str] = field(default_factory=set)

    # SQL 保留字黑名单（出现就直接拒）
    disallowed_keywords: Set[str] = field(
        default_factory=lambda: {
            "select",
            "insert",
            "update",
            "delete",
            "drop",
            "alter",
            "create",
            "join",
            "union",
            "on",
            "where",
            "group",
            "having",
            "order",
            "limit",
            "over",
            "window",
            "case",
            "when",
            "then",
            "end",
        }
    )

    # 是否允许括号（用于控制复杂度）
    allow_parentheses: bool = True

    # 是否允许比较运算符（>, <, = 等）
    # 第一版我们只允许算术表达式，不允许布尔表达式
    allow_comparisons: bool = False


@dataclass
class FeatureExpressionValidationResult:
    ok: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


class FeatureExpressionGuard:
    """
    Sandbox 特征表达式安全检查器。

    只做“语法级”和“黑白名单级”的检查，不尝试真正理解 SQL。
    目标是：尽可能阻止危险/明显错误，而不是证明表达式是完美的。
    """

    _IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

    def __init__(self, cfg: FeatureExpressionGuardConfig) -> None:
        self._cfg = cfg

    def validate(self, expr: str) -> FeatureExpressionValidationResult:
        expr_stripped = expr.strip()
        errors: List[str] = []
        warnings: List[str] = []

        # 1) 长度与基本字符检查
        if not expr_stripped:
            errors.append("Expression is empty.")
            return FeatureExpressionValidationResult(ok=False, errors=errors)

        # 小黑名单：禁止分号和注释符，避免注入
        if ";" in expr_stripped or "--" in expr_stripped or "/*" in expr_stripped:
            errors.append("Expression contains forbidden characters (;, --, /*).")
            return FeatureExpressionValidationResult(ok=False, errors=errors)

        # 2) 关键字黑名单
        lowered = expr_stripped.lower()
        for kw in self._cfg.disallowed_keywords:
            if kw in lowered:
                errors.append(f"Expression contains disallowed keyword: {kw}")
                return FeatureExpressionValidationResult(ok=False, errors=errors)

        # 3) 括号与比较运算符控制
        if not self._cfg.allow_parentheses and ("(" in expr_stripped or ")" in expr_stripped):
            errors.append("Parentheses are not allowed in sandbox expressions.")
            return FeatureExpressionValidationResult(ok=False, errors=errors)

        if not self._cfg.allow_comparisons:
            for op in [">=", "<=", "<>", "!=", "=", ">", "<"]:
                if op in expr_stripped:
                    errors.append(
                        f"Comparison operator '{op}' is not allowed "
                        "in sandbox expressions (only arithmetic expressions are allowed)."
                    )
                    return FeatureExpressionValidationResult(ok=False, errors=errors)

        # 4) 识别所有 identifier，分类到列/函数/未知
        idents = list(self._IDENT_RE.finditer(expr_stripped))
        unknown_idents: Set[str] = set()
        used_columns: Set[str] = set()
        used_functions: Set[str] = set()

        for m in idents:
            ident = m.group(0)
            ident_lower = ident.lower()

            # 数字开头的直接跳过（其实不会匹配）
            if ident[0].isdigit():
                continue

            # 看看是不是列
            if ident in self._cfg.allowed_columns:
                used_columns.add(ident)
                continue

            # 看看是不是允许的函数
            if ident_lower in self._cfg.allowed_functions:
                used_functions.add(ident_lower)
                continue

            # 一些常见 SQL 关键字（但不在 disallowed 里）可以忽略
            if ident_lower in {"and", "or", "not", "as"}:
                continue

            # 其他的一律视为未知，提示错误
            unknown_idents.add(ident)

        if unknown_idents:
            errors.append(
                "Expression references unknown identifiers: "
                + ", ".join(sorted(unknown_idents))
            )

        # 5) 至少要用到 1 个列（否则纯常数没啥意义）
        if not used_columns:
            warnings.append("Expression does not reference any known feature column.")

        ok = not errors
        return FeatureExpressionValidationResult(ok=ok, errors=errors, warnings=warnings)
```

---

## 三、Sandbox Feature Registry（JSONL）

继续在同一个文件里加：

```python
# =========================
# 2. Sandbox 特征对象 & Registry
# =========================

@dataclass
class SandboxFeature:
    """
    一条 Sandbox 特征记录。

    - id: 全局唯一 ID（uuid4）
    - alias: 在 SQL 里使用的列别名（例如 "sf_zscore_rsi_14"）
    - expression: SQL 表达式片段（只用于 SELECT 子句）
    - base_table: 特征基于的逻辑表/视图名（例如 "bars_1m_tsla"）
    - created_at: ISO 时间戳（UTC）
    - status: "experimental" / "promoted" / "disabled"
    - source: "pi_llm" / "manual" / "auto_evolver" 等
    - meta: 任意补充信息（比如 validation_result, symbol, horizon 等）
    """

    id: str
    alias: str
    expression: str
    base_table: str
    created_at: str
    status: str = "experimental"
    source: str = "pi_llm"
    meta: Dict[str, Any] = field(default_factory=dict)


class SandboxFeatureRegistry:
    """
    基于 JSON Lines 的简单 Sandbox 特征注册表。

    文件格式：每行一条 JSON，字段对应 SandboxFeature。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        # 确保目录存在
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 写入 ----------

    def append(self, feature: SandboxFeature) -> None:
        """
        追加写一条 Sandbox 特征。
        """
        with self._path.open("a", encoding="utf-8") as f:
            json.dump(asdict(feature), f, ensure_ascii=False)
            f.write("\n")

    # ---------- 读取 ----------

    def load_all(self) -> List[SandboxFeature]:
        if not self._path.exists():
            return []
        out: List[SandboxFeature] = []
        with self._path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                out.append(SandboxFeature(**data))
        return out

    def list_by_table(
        self,
        base_table: str,
        status_in: Optional[Sequence[str]] = None,
    ) -> List[SandboxFeature]:
        """
        按 base_table 和 status 过滤。
        默认返回所有状态。
        """
        status_set = set(status_in) if status_in is not None else None
        result = []
        for feat in self.load_all():
            if feat.base_table != base_table:
                continue
            if status_set is not None and feat.status not in status_set:
                continue
            result.append(feat)
        return result

    # ---------- 晋升/禁用（可选） ----------

    def update_status(self, feature_id: str, new_status: str) -> None:
        """
        简单实现：全量重写文件。
        用于把某个特征从 experimental -> promoted 等。
        """
        features = self.load_all()
        modified = False
        for feat in features:
            if feat.id == feature_id:
                feat.status = new_status
                modified = True
                break
        if not modified:
            return

        # 重写
        with self._path.open("w", encoding="utf-8") as f:
            for feat in features:
                json.dump(asdict(feat), f, ensure_ascii=False)
                f.write("\n")
```

---

## 四、将 Sandbox 嵌入 PI：AI 提出的派生特征全部走“审核 + 记录”

现在改造你现有的 `PrincipalInvestigator`。

核心思想：

1. LLM 提出 `feature_recipe["derived"]`；
2. 对每个 derived：

   * 用 `FeatureExpressionGuard` 检查；
   * 不通过 → 丢弃并记录（避免炸库）；
   * 通过 → 写入 SandboxRegistry，标记为 experimental；
3. **只有通过审核的 derived 特征才会真的进入 `build_select_sql`。**

下面是在你现有代码基础上添加的关键部分（只展示变化点）：

```python
# pi/principal_investigator.py

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd
from datetime import datetime
from pathlib import Path
import uuid

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql

# 新增：
from features.feature_sandbox import (
    FeatureExpressionGuard,
    FeatureExpressionGuardConfig,
    SandboxFeatureRegistry,
    SandboxFeature,
)


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
    # 可选：把 sandbox 特征也返回出去，方便上游记录/展示
    sandbox_features: Optional[List[SandboxFeature]] = None


class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(self, ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None) -> None:
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

        # 新增：Sandbox Feature Registry
        sandbox_path = Path(ch_cfg.get("sandbox_registry_path", "config/sandbox_features.jsonl"))
        self._sandbox_registry = SandboxFeatureRegistry(sandbox_path)

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
            f"特征: {', '.join(catalog)}\n"
            f"预测窗口: {horizon}"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # =========================
        # 1) Sandbox 派生特征审核
        # =========================

        derived_raw = recipe.get("derived", []) or []
        base_features = recipe.get("base_features", []) or []

        # 安全配置：允许使用的列就是当前表的 catalog
        expr_guard = FeatureExpressionGuard(
            FeatureExpressionGuardConfig(
                allowed_columns=set(catalog),
                # 先不允许函数，后续你可以逐步加入安全函数
                allowed_functions=set(),
                allow_parentheses=True,
                allow_comparisons=False,
            )
        )

        derived_clean: List[Dict[str, Any]] = []
        sandbox_features: List[SandboxFeature] = []

        for d in derived_raw:
            alias = d.get("alias")
            expr = d.get("expr") or d.get("expression")

            if not alias or not expr:
                # 忽略不完整的定义
                continue

            vres = expr_guard.validate(expr)
            if not vres.ok:
                # 这里可以加日志：记录被拒绝的表达式和错误原因
                # logger.warning(f"Sandbox feature rejected: alias={alias}, expr={expr}, errors={vres.errors}")
                continue

            # 通过审核 → 写入 Sandbox Registry
            sf = SandboxFeature(
                id=str(uuid.uuid4()),
                alias=alias,
                expression=expr,
                base_table=recipe.get("use_table", table),
                created_at=datetime.utcnow().isoformat(),
                status="experimental",
                source="pi_llm",
                meta={
                    "symbol": symbol,
                    "horizon": horizon,
                    "validation": vres.to_dict(),
                },
            )
            self._sandbox_registry.append(sf)
            sandbox_features.append(sf)

            # 进入真正可用的 derived 列表
            derived_clean.append({"alias": alias, "expr": expr})

        # 用清洗后的 derived 覆盖原来的
        recipe["derived"] = derived_clean

        # =========================
        # 2) 构建 SQL + 后续 HPO Grid
        # =========================

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

        grid = {
            "model_type": [],
            "feature_subset": [
                base_features + [d.get("alias") for d in recipe.get("derived", [])]
            ],
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
            sandbox_features=sandbox_features,
        )
```

> 要点总结：

* LLM 依然可以提 `derived`，但：

  * 没有 `alias`/`expr` 的一律忽略；
  * 表达式不通过 Guard 的一律丢弃；
  * 通过 Guard 的会：

    * 写入 `sandbox_features.jsonl`；
    * 加入到 `recipe["derived"]`，参与本次训练；
* 将来你可以在 **ExperimentRegistry** 中记录 `pi_result.sandbox_features`，做到：

  * 这次实验具体用了哪些实验性特征；
  * 哪些特征在多个 run 里反复出现并表现好 → 晋升。

---

## 五、到这里我们完成了什么？

**数据 & 特征层：**

* ✅ DatasetSanity：ClickHouse + 采样版体检（之前已经给了代码）
* ✅ Sandbox 特征机制：

  * 表达式安全守卫（防 SQL+逻辑乱飞）
  * Sandbox registry（JSONL，可审计）
  * PI 自动写入 & 使用 Sandbox 特征

接下来在这三大层里：

1. **数据 & 特征 & 标签层：**

   * 还差：Label Guard（统一在 Python/Backtest 侧生成标签，禁止 LLM 写 target SQL）；
2. **机器学习/HPO/实验治理层：**

   * 已有：HPO Grid Guard、ExperimentRegistry 框架草图
   * 要做：PI → HPO → ExperimentRecord 的完整埋点；
3. **AI 大脑 & Prompt 安全层：**

   * 已有：Prompt Library + execution_prompt_id 思路
   * 要做：BrainDecision schema + run_id 白名单约束（Phase 4A 那条线）

你刚刚说的主线是：

> 1. 数据 & 特征 & 标签这一层
> 2. 机器学习/HPO/实验治理这一层
> 3. AI 大脑 & Prompt 安全这一层

我们在 1 里面已经把 **DatasetSanity + Sandbox 特征**打完地基。

**下一步我建议：**

👉 把 **Label Guard + 标签 Registry** 落地：

* 统一用一个 `labels/label_registry.py` 管所有 `ret_10m_fwd / triple_barrier_xxx`；
* PI 只能引用已经注册的 label_id；
* 真正的 label 计算逻辑全部在 Python（或 Backtest Engine）里，严格保证没有未来泄露。

如果你继续一句“继续”，我就直接给你 `label_registry.py` + `label_computer.py` 的完整骨架，并展示如何在 PI/HPO 里强制只用注册标签。
好，那我们现在**实打实把“标签层 + Label Guard”落地**，让：

* AI 大脑 **不能乱写 target SQL**；
* 所有标签都走一个 **可审计、可复用、无未来泄露** 的统一通道。

我会分三块：

1. `labels/label_registry.py`：统一管理 label 定义（meta 信息 + 所需列）。
2. `labels/label_computer.py`：在 **Python 里计算标签**，只用过去列 + shift。
3. 改造 `PrincipalInvestigator`：从 LLM 产生的 `training_plan` 里 **只接受 label_id**，然后用 Registry + Computer 完成剩下工作，彻底禁止“AI 自己写 target 表达式”。

---

## 一、Label Registry：所有标签的“户口本”

> 新文件：`labels/label_registry.py`

### 1.1 `LabelSpec` 数据结构

```python
# labels/label_registry.py

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import json


@dataclass
class LabelSpec:
    """
    统一的标签定义（metadata），不做任何计算，只描述“标签是什么”。

    - id: 唯一 ID（在 training_plan / HPO / ExperimentRecord 里引用）
    - name: 人看的名字
    - method: 计算方法，比如 "simple_return", "direction", "triple_barrier"
    - base_price_col: 用哪个价格列做基准（比如 "close"）
    - horizon_bars: 前瞻多少 bar（对 1m bar 就是多少分钟）
    - problem_type: "reg" 或 "cls"
    - required_columns: 计算这个 label 需要哪些列（例如 ["close", "high", "low"]）
    - params: 其它参数（比如 triple barrier 的阈值、超参等）
    """

    id: str
    name: str
    method: str
    base_price_col: str
    horizon_bars: int
    problem_type: str  # "reg" or "cls"
    required_columns: List[str] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """
        在加载时做一种轻量的自检，防止配置写错。
        """
        if self.problem_type not in ("reg", "cls"):
            raise ValueError(f"Invalid problem_type for label {self.id}: {self.problem_type}")

        if self.horizon_bars <= 0:
            raise ValueError(f"horizon_bars must be > 0 for label {self.id}")

        if not self.base_price_col:
            raise ValueError(f"base_price_col is required for label {self.id}")
```

### 1.2 `LabelRegistry`：从 JSON 文件加载/查询标签

```python
class LabelRegistry:
    """
    Label 注册表：从一个 JSON 文件里加载所有 LabelSpec。

    文件格式示例见下面注释。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._labels: Dict[str, LabelSpec] = {}
        if path.exists():
            self._load()
        else:
            # 你可以根据需要选择是允许为空，还是直接报错
            # 我这里允许为空，但会在 get() 时报错
            pass

    def _load(self) -> None:
        with self._path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        labels = {}
        for item in raw.get("labels", []):
            spec = LabelSpec(**item)
            spec.validate()
            labels[spec.id] = spec

        self._labels = labels

    def list_ids(self) -> List[str]:
        return sorted(self._labels.keys())

    def get(self, label_id: str) -> LabelSpec:
        try:
            return self._labels[label_id]
        except KeyError:
            raise KeyError(
                f"Label '{label_id}' not found in registry "
                f"(available: {', '.join(self.list_ids())})"
            )
```

### 1.3 示例配置文件：`config/labels.json`

> 这个文件由你手动维护或后面用工具生成。
> 没有“魔法数写死在代码里”，所有阈值都在这里，可被 HPO/脚本调参。

```jsonc
{
  "_comment": "统一 label 配置，未来可以被 HPO 或统计脚本自动调整参数",
  "labels": [
    {
      "id": "ret_10m_reg",
      "name": "10-bar forward simple return (regression)",
      "method": "simple_return",
      "base_price_col": "close",
      "horizon_bars": 10,
      "problem_type": "reg",
      "required_columns": ["close"],
      "params": {}
    },
    {
      "id": "dir_10m_cls",
      "name": "10-bar forward direction (classification, sign of return)",
      "method": "direction_sign",
      "base_price_col": "close",
      "horizon_bars": 10,
      "problem_type": "cls",
      "required_columns": ["close"],
      "params": {
        "eps": 0.0
      }
    },
    {
      "id": "tb_30m_cls",
      "name": "Triple barrier 30-bar classification",
      "method": "triple_barrier",
      "base_price_col": "close",
      "horizon_bars": 30,
      "problem_type": "cls",
      "required_columns": ["close", "high", "low"],
      "params": {
        "up_pct": 0.02,
        "down_pct": 0.02
      }
    }
  ]
}
```

> `up_pct/down_pct` 这些参数完全可以后面用 **数据驱动/HPO** 来调整，我们这里只是定义结构，符合你“参数不能是死的魔法数”的原则——真正用的时候由脚本写回这个 JSON 或用别的配置层管理。

---

## 二、LabelComputer：统一在 Python 里计算标签

> 新文件：`labels/label_computer.py`

### 2.1 简单/方向标签

```python
# labels/label_computer.py

from __future__ import annotations

from typing import Any
import numpy as np
import pandas as pd

from .label_registry import LabelSpec


class LabelComputer:
    """
    统一的标签计算器：根据 LabelSpec 在 DataFrame 上生成 label Series。

    所有 label 计算逻辑都集中这里，避免散落在 SQL / 各种 notebook 里。
    """

    def compute(self, df: pd.DataFrame, spec: LabelSpec) -> pd.Series:
        method = spec.method

        if method == "simple_return":
            return self._simple_return(df, spec)
        elif method == "direction_sign":
            return self._direction_sign(df, spec)
        elif method == "triple_barrier":
            return self._triple_barrier(df, spec)
        else:
            raise ValueError(f"Unknown label method: {method}")

    # ---------- 1) simple_return: 连续收益 ----------

    def _simple_return(self, df: pd.DataFrame, spec: LabelSpec) -> pd.Series:
        col = spec.base_price_col
        h = spec.horizon_bars
        if col not in df.columns:
            raise KeyError(f"Column '{col}' not found in dataframe for label {spec.id}")

        # 前瞻 h bar 的价格
        fwd = df[col].shift(-h)
        cur = df[col]

        # 简单收益率 (P_{t+h} / P_t - 1)
        ret = (fwd / cur) - 1.0

        return ret

    # ---------- 2) direction_sign: 收益率符号 ----------

    def _direction_sign(self, df: pd.DataFrame, spec: LabelSpec) -> pd.Series:
        eps = float(spec.params.get("eps", 0.0))
        ret = self._simple_return(df, spec)

        # sign: > eps → 1, < -eps → -1, 其他 0
        out = pd.Series(0, index=df.index, dtype="int8")
        out[ret > eps] = 1
        out[ret < -eps] = -1
        return out
```

### 2.2 Triple Barrier（简化版）

> 注意：这是一个**标准的路径依赖标签**算法，O(N * horizon_bars)，对你目前 1m/5m bar 的规模足够。高频级别后面可以再优化为 Numba/向量化。

```python
    # ---------- 3) triple_barrier: 标准三栅栏算法（简化版） ----------

    def _triple_barrier(self, df: pd.DataFrame, spec: LabelSpec) -> pd.Series:
        """
        - up_pct / down_pct: 百分比阈值（从 spec.params 读取）
        - horizon_bars: 最大持仓时间
        标签含义（典型定义）：
          +1: 在 horizon 内先触及上轨
          -1: 在 horizon 内先触及下轨
           0: 都没触及，按最终收益方向（或 0）打标

        这里实现的是一个比较教科书式的版本，你后续可以根据实盘逻辑微调。
        """
        close_col = spec.base_price_col
        if close_col not in df.columns:
            raise KeyError(f"Column '{close_col}' not in df for label {spec.id}")

        for c in ["high", "low"]:
            if c not in df.columns:
                raise KeyError(f"Column '{c}' not in df for triple_barrier label {spec.id}")

        up_pct = float(spec.params.get("up_pct", 0.02))
        down_pct = float(spec.params.get("down_pct", 0.02))
        h = spec.horizon_bars

        close = df[close_col].to_numpy()
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        n = len(df)

        labels = np.zeros(n, dtype="int8")

        for i in range(n):
            entry = close[i]
            if not np.isfinite(entry):
                labels[i] = 0
                continue

            up_level = entry * (1.0 + up_pct)
            down_level = entry * (1.0 - down_pct)

            # 向前看最多 h bar
            end = min(n, i + h + 1)
            hit_label = 0

            for j in range(i + 1, end):
                if high[j] >= up_level:
                    hit_label = 1
                    break
                if low[j] <= down_level:
                    hit_label = -1
                    break

            if hit_label != 0:
                labels[i] = hit_label
            else:
                # 都没 hit，用最终收益方向
                final_price = close[end - 1]
                ret = (final_price / entry) - 1.0
                if ret > 0:
                    labels[i] = 1
                elif ret < 0:
                    labels[i] = -1
                else:
                    labels[i] = 0

        return pd.Series(labels, index=df.index)
```

---

## 三、改造 PI：只允许用 label_id，不允许写 target SQL

> 文件：`pi/principal_investigator.py`

在上一次的基础上（已经加了 Sandbox Feature），我们现在再加：

1. `LabelRegistry` 和 `LabelComputer`；
2. 从 `training_plan` 里拿 `label_id`，强制去 registry 查；
3. 构造 SQL 时只加 `label_spec.required_columns`，不允许再传 `target=tplan["target"]` 这类东西；
4. `query_df` 后，用 `LabelComputer` 生成 `label` 列。

### 3.1 `__init__`：挂上 LabelRegistry & LabelComputer

```python
from labels.label_registry import LabelRegistry, LabelSpec
from labels.label_computer import LabelComputer
# 以及之前的 Sandbox import ...

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(self, ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None) -> None:
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

        # Sandbox registry（之前加过）
        sandbox_path = Path(ch_cfg.get("sandbox_registry_path", "config/sandbox_features.jsonl"))
        self._sandbox_registry = SandboxFeatureRegistry(sandbox_path)

        # NEW: Label registry + computer
        label_path = Path(ch_cfg.get("label_registry_path", "config/labels.json"))
        self._label_registry = LabelRegistry(label_path)
        self._label_computer = LabelComputer()
```

### 3.2 `propose()` 里：强制用 `label_id`

只展示关键变化（在你之前的版本基础上继续改）：

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
        catalog = _fetch_catalog(self._client, table)

        # ...（略）LLM 调用、Sandbox 特征审核部分，沿用上一个版本 ...

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # =========================
        # 1) Label Guard: 只接受 label_id
        # =========================

        label_id = tplan.get("label_id")
        if not label_id:
            # 你可以选择默认一个，也可以直接报错，我建议第一版直接报错。
            raise ValueError(
                "training_plan must include 'label_id' referencing a registered label. "
                "Example: {'label_id': 'ret_10m_reg'}"
            )

        label_spec = self._label_registry.get(label_id)

        # 确保 label 所需列在 catalog 里，否则直接 fail-fast
        missing_cols = [c for c in label_spec.required_columns if c not in catalog]
        if missing_cols:
            raise ValueError(
                f"Label '{label_id}' requires columns {missing_cols}, "
                f"but they are not present in table '{table}'."
            )

        # =========================
        # 2) Sandbox 派生特征审核（沿用上一个版本）
        # =========================

        derived_raw = recipe.get("derived", []) or []
        base_features = recipe.get("base_features", []) or []

        expr_guard = FeatureExpressionGuard(
            FeatureExpressionGuardConfig(
                allowed_columns=set(catalog),
                allowed_functions=set(),
                allow_parentheses=True,
                allow_comparisons=False,
            )
        )

        derived_clean: List[Dict[str, Any]] = []
        sandbox_features: List[SandboxFeature] = []

        for d in derived_raw:
            alias = d.get("alias")
            expr = d.get("expr") or d.get("expression")
            if not alias or not expr:
                continue

            vres = expr_guard.validate(expr)
            if not vres.ok:
                # logger.warning(...)  # 可选
                continue

            sf = SandboxFeature(
                id=str(uuid.uuid4()),
                alias=alias,
                expression=expr,
                base_table=recipe.get("use_table", table),
                created_at=datetime.utcnow().isoformat(),
                status="experimental",
                source="pi_llm",
                meta={
                    "symbol": symbol,
                    "horizon": horizon,
                    "validation": vres.to_dict(),
                },
            )
            self._sandbox_registry.append(sf)
            sandbox_features.append(sf)

            derived_clean.append({"alias": alias, "expr": expr})

        recipe["derived"] = derived_clean

        # =========================
        # 3) 构建 SQL：不再允许 target SQL，只加 label 所需列
        # =========================

        # EXTRA: 保证 label 所需列也被选出来
        extra_columns = label_spec.required_columns

        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            # CHANGED: 原来这里有 target=tplan.get("target")
            # 现在完全禁止 LLM 写 target SQL
            extra_columns=extra_columns,
            feature_catalog=catalog,
        )

        df = self._client.query_df(sql)

        # =========================
        # 4) 在 Python 侧计算 label，避免任何 SQL target 泄露
        # =========================

        label_series = self._label_computer.compute(df, label_spec)

        # 按需拼到 df 里（或单独返回）
        df = df.copy()
        df["label"] = label_series

        # 你可以选择在 DatasetSanity 里检查 label 的 NaN / 分布

        # =========================
        # 5) HPO Grid 构造（保持不变）
        # =========================

        grid = {
            "model_type": [],
            "feature_subset": [
                base_features + [d.get("alias") for d in recipe.get("derived", [])]
            ],
            "task": [tplan.get("task", label_spec.problem_type)],
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
            sandbox_features=sandbox_features,
            # 可选：把 label_spec 也带出去，方便 ExperimentRegistry 记录
            # label_spec=label_spec,
        )
```

---

### 3.3 小补丁：`build_select_sql` 支持 `extra_columns`

你现在的 `pi/feature_sql.py` 里大概有类似：

```python
def build_select_sql(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    target: Optional[str],
    feature_catalog: List[str],
) -> str:
    ...
```

我们建议改成：

```python
def build_select_sql(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    extra_columns: Optional[List[str]] = None,
    feature_catalog: Optional[List[str]] = None,
) -> str:
    """
    - recipe.base_features + derived 决定特征列
    - extra_columns 用于 label / 其它辅助列（例如 high/low）
    - 不再接受 target SQL 字符串，标签计算统一在 Python 侧完成
    """
    base_features = recipe.get("base_features", []) or []
    derived = recipe.get("derived", []) or []
    derived_exprs = [f"{d['expr']} AS {d['alias']}" for d in derived]

    # union 列集合（注意去重）
    cols = list(dict.fromkeys(base_features + (extra_columns or [])))

    select_cols = []
    for c in cols:
        select_cols.append(c)

    select_clause = ", ".join(select_cols + derived_exprs)

    sql = f"""
    SELECT
        {select_clause}
    FROM {table}
    WHERE symbol = '{{symbol}}'
      AND ts >= '{{start_ts}}'
      AND ts <= '{{end_ts}}'
    ORDER BY ts ASC
    """.strip()

    # 你可能已经有 parameterized SQL，这里只是示例
    return sql
```

核心点只有一个：

> **不再允许 `target=...` 这种字符串从 LLM 流入 SQL。
> 标签一律在 Python 的 LabelComputer 里计算。**

---

## 到这里，我们完成了什么？

在你最关心的三层中：

1. **数据 & 特征 & 标签层**

   * ✅ DatasetSanity（之前已给方案，ClickHouse+采样）
   * ✅ Sandbox Features（派生特征安全审核 + JSONL 审计）
   * ✅ Label Registry（所有标签有“户口本” + 统一 meta）
   * ✅ Label Guard：PI 只能用 `label_id`，禁止 target SQL
   * ✅ LabelComputer：所有标签在 Python 侧、无未来泄露地计算

2. **机器学习/HPO/实验治理层**

   * PI 现在输出的 `dataset_df` 中已经包含：

     * 干净的特征列
     * 一列统一名字的 `label`
   * HPO/ExperimentRegistry 可以直接把 `label_id`、`sandbox_features`、`feature_subset` 等写入 `ExperimentRecord`。

3. **AI 大脑 & Prompt 安全层**

   * 大脑在 `training_plan` 里只能填 `label_id`（来自 whitelist），已经从“瞎写 SQL”变成“在安全菜单里点菜”。

---

## 下一步建议（你再说“继续”我就开写）

我建议下一步按这条线继续往前推：

1. **在 HPO 层落地 ExperimentRegistry 写入**：

   * 每一次 PI → HPO run 把：

     * `label_id`
     * `feature_subset`
     * `sandbox_feature_ids`
     * `model_type/model_params`
     * `metrics`（train/val/test）
   * 写入统一的 `ExperimentRecord`（JSONL/ClickHouse/Parquet 均可）；

2. **然后做 Phase 4A：BrainDecision schema + 策略选择**：

   * 让 AI 大脑从这些已经登记好的 `run_id` 里选，而不是再“发明新模型”。

如果你觉得方向 OK，就像以前一样回我一个“继续”，我就直接给：

* `experiments/registry.py` 的完整实现骨架；
* 一个 HPO 结束时如何调用 `ExperimentRegistry.log_run(...)` 的示例；
* 以及一个最小可用的 `BrainDecision` schema（JSON schema + pydantic）用于以后 Phase 4A。
