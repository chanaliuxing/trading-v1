好，那我们现在把 **sandbox 特征机制** 真正落地，让 AI 在一个“安全沙盒”里自己发明特征，而且不搞坏数据。

我会按这几个步骤来给你完整方案和代码骨架：

1. 约定：LLM 可以怎么“提” sandbox 特征（JSON 协议）
2. 实现：sandbox 表达式的安全编译器（Python AST → ClickHouse SQL）
3. 接入：`build_select_sql` 支持 `sandbox_derived` 并生成 SQL
4. 风险控制：数量/复杂度限制 + 与 DatasetSanity 联动
5. 升级路径：如何把 sandbox 特征晋升为正式特征

---

## 1. LLM 协议：让 AI 知道怎么提“沙盒特征”

我们在现有的 `feature_recipe` 上加一个字段：`sandbox_derived`。

典型的 LLM 输出（简化示例）：

```jsonc
{
  "feature_recipe": {
    "use_table": "bars_1m",
    "base_features": [
      "close", "open", "high", "low", "volume", "vwap"
    ],
    "derived": [
      // 现有正式特征，仍然走 registry
      {
        "alias": "ret_1m",
        "kind": "return",
        "params": {"window": 1}
      }
    ],
    "sandbox_derived": [
      {
        "alias": "sb_body_ratio",
        "expr": "(close - open) / (high - low + 1e-6)",
        "note": "K线实体长度占总波动区间比例"
      },
      {
        "alias": "sb_vol_price_mixed",
        "expr": "log(volume + 1) * (close - vwap)",
        "note": "交易量加权的一分钟偏离程度"
      }
    ]
  },
  "training_plan": {
    "task": "reg",
    "target": "ret_10m_fwd",
    "symbol": "NVDA",
    ...
  }
}
```

**约束：**

* `sandbox_derived` 是可选；最多 N 个（例如 5 个）。
* `expr` 使用 **Python 风格数学表达式**（而不是 SQL 关键字）：

  * 变量名 = 原始列名（限于 `feature_catalog`）
  * 支持：`+ - * /`、括号、数字常量
  * 支持少量白名单函数：`abs(x)`, `log(x)`, `sqrt(x)`, `exp(x)`, `tanh(x)` 等
* 不允许：

  * 访问未来：不允许 `lead`, `future`, `fwd_*` 函数
  * 字符串拼接、比较操作、布尔运算等（先版本禁止）

你可以在 `PrincipalInvestigator` 的 system prompt 里加一段硬规则（交给大脑）：

```text
For field "feature_recipe.sandbox_derived":

- You MAY propose at most 5 sandbox features.
- Each must be a JSON object with: alias (string), expr (string), note (string).
- "expr" must be a pure arithmetic expression using only:
  - allowed base columns from the given feature catalog
  - operators: +, -, *, /, parentheses
  - numeric constants: e.g. 0.5, 1e-3
  - functions: abs, log, sqrt, exp, tanh

You MUST NOT use any of: lead, future, now, today, case, if, select, from, join, union.
These expressions will be compiled and validated; invalid ones will be rejected.
```

---

## 2. 实现 sandbox 表达式编译器（AST → ClickHouse SQL）

新建文件：`features/sandbox_dsl.py`

```python
# features/sandbox_dsl.py

from __future__ import annotations

import ast
from typing import Dict, Iterable, Set


class SandboxExprError(ValueError):
    pass


_ALLOWED_BIN_OPS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",  # 如需更保守，可以移除，强制使用 safe_div
}

_ALLOWED_UNARY_OPS = {
    ast.UAdd: "+",
    ast.USub: "-",
}

# Python 函数名 → ClickHouse 函数名
_ALLOWED_FUNCS: Dict[str, str] = {
    "abs": "abs",
    "log": "log",
    "sqrt": "sqrt",
    "exp": "exp",
    "tanh": "tanh",
}

# 可选：定义 safe_div，鼓励 LLM 使用
def _safe_div_sql(a_sql: str, b_sql: str) -> str:
    # 避免 0 除错误：if(b = 0 OR isNull(b)) THEN 0 ELSE a / b
    return f"ifOrNull({b_sql} = 0 OR isNull({b_sql}), 0, {a_sql} / {b_sql})"


def compile_sandbox_expr_to_clickhouse(
    expr: str,
    allowed_columns: Iterable[str],
    *,
    max_nodes: int = 48,
) -> str:
    """
    将 LLM 给出的 Python 风格表达式，编译成 ClickHouse SQL 片段。

    - 只允许访问 allowed_columns 中的列名
    - 限制 AST 节点数量，防止复杂度爆炸
    """
    expr = expr.strip()
    if not expr:
        raise SandboxExprError("sandbox expr is empty")

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise SandboxExprError(f"Invalid sandbox expr syntax: {e}") from e

    # 控制复杂度
    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > max_nodes:
        raise SandboxExprError(
            f"Sandbox expr too complex (nodes={node_count}, max={max_nodes})"
        )

    allowed_cols: Set[str] = set(allowed_columns)

    def _compile(node: ast.AST) -> str:
        if isinstance(node, ast.Expression):
            return _compile(node.body)

        # 常量
        if isinstance(node, ast.Constant):
            # 只允许数字
            if isinstance(node.value, (int, float)):
                return repr(node.value)
            raise SandboxExprError(f"Unsupported constant type: {type(node.value)}")

        # 变量（列名）
        if isinstance(node, ast.Name):
            col = node.id
            if col not in allowed_cols:
                raise SandboxExprError(
                    f"Column '{col}' not in allowed feature catalog."
                )
            # 视你 ClickHouse 命名习惯决定要不要加反引号
            return f"`{col}`"

        # 一元运算符
        if isinstance(node, ast.UnaryOp):
            op = _ALLOWED_UNARY_OPS.get(type(node.op))
            if not op:
                raise SandboxExprError(f"Unary op not allowed: {ast.dump(node.op)}")
            return f"({op}{_compile(node.operand)})"

        # 二元运算符
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Div):
                # 如果希望更安全，这里可以改用 safe_div
                left = _compile(node.left)
                right = _compile(node.right)
                return f"({left} / {right})"
                # 或者:
                # return _safe_div_sql(_compile(node.left), _compile(node.right))

            op = _ALLOWED_BIN_OPS.get(type(node.op))
            if not op:
                raise SandboxExprError(
                    f"Binary op not allowed: {ast.dump(node.op)}"
                )
            left = _compile(node.left)
            right = _compile(node.right)
            return f"({left} {op} {right})"

        # 函数调用
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise SandboxExprError(
                    f"Only simple function calls allowed: {ast.dump(node.func)}"
                )
            func_name = node.func.id
            ch_func = _ALLOWED_FUNCS.get(func_name)
            if not ch_func:
                raise SandboxExprError(f"Function '{func_name}' not allowed.")
            if len(node.args) == 0:
                raise SandboxExprError(f"Function '{func_name}' requires arguments.")

            args_sql = ", ".join(_compile(a) for a in node.args)
            return f"{ch_func}({args_sql})"

        # 禁止所有其它 AST 节点类型
        raise SandboxExprError(
            f"Unsupported AST node in sandbox expr: {ast.dump(node)}"
        )

    return _compile(tree)
```

> 这个模块是 **核心安全阀**：
>
> * LLM 给出的 `expr` 先变成 Python AST
> * 我们用白名单规则一个节点一个节点地编译
> * 一旦出现非法操作（未知列、奇怪函数、太复杂），直接抛 `SandboxExprError`，该特征自动丢弃或标记为 warning

---

## 3. 接入 build_select_sql：让 sandbox 特征变成真正 SQL 列

假设你现有的 `pi.feature_sql.build_select_sql` 结构大致是这样（伪代码）：

```python
# pi/feature_sql.py (伪示意)

def build_select_sql(table, symbol, start_ts, end_ts, recipe, target, feature_catalog):
    base_cols = recipe.get("base_features", [])
    derived_specs = recipe.get("derived", [])

    select_parts = []

    # 1) base features
    for col in base_cols:
        select_parts.append(f"{col}")

    # 2) derived features: registry/模板生成 SQL
    for d in derived_specs:
        sql_expr = build_derived_expr(d, feature_catalog)
        select_parts.append(f"{sql_expr} AS {d['alias']}")

    # 3) target / label
    if target:
        target_expr = build_target_expr(target, feature_catalog)
        select_parts.append(f"{target_expr} AS y")

    # 4) 组装 WHERE / ORDER BY
    ...
```

我们现在扩展它，让它支持 `sandbox_derived`，并用刚才的编译器：

```python
# pi/feature_sql.py

from typing import Any, Dict, Iterable, List

from features.sandbox_dsl import compile_sandbox_expr_to_clickhouse, SandboxExprError


def _build_sandbox_columns(
    sandbox_specs: Iterable[Dict[str, Any]],
    feature_catalog: List[str],
    max_sandbox_features: int = 5,
) -> List[str]:
    """
    把 sandbox_derived 里的 spec 转成 ["<expr> AS <alias>", ...] 形式。
    """
    cols: List[str] = []
    used_aliases = set()

    for i, spec in enumerate(sandbox_specs):
        if i >= max_sandbox_features:
            # 超出数量限制，静默忽略或者加 warning 日志
            break

        alias = (spec.get("alias") or "").strip()
        expr = (spec.get("expr") or "").strip()

        if not alias or not expr:
            # 略过不完整 spec
            continue

        if alias in used_aliases:
            # 避免重复 alias
            continue

        try:
            ch_expr = compile_sandbox_expr_to_clickhouse(
                expr=expr,
                allowed_columns=feature_catalog,
            )
        except SandboxExprError as e:
            # 可以打日志，但不要让整个流程崩溃
            # logger.warning("Sandbox feature rejected: alias=%s, reason=%s", alias, e)
            continue

        cols.append(f"{ch_expr} AS `{alias}`")
        used_aliases.add(alias)

    return cols
```

然后在 `build_select_sql` 里插入：

```python
def build_select_sql(
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    target: Dict[str, Any] | None,
    feature_catalog: List[str],
) -> str:
    base_cols = recipe.get("base_features", [])
    derived_specs = recipe.get("derived", [])
    sandbox_specs = recipe.get("sandbox_derived", []) or []

    select_parts: List[str] = []

    # 1) base features（安全性可以在别处校验子集关系）
    for col in base_cols:
        if col not in feature_catalog:
            # 可以选择忽略或抛异常，这里建议忽略并打 warning
            # logger.warning("Base feature %s not in catalog, skipped", col)
            continue
        select_parts.append(f"`{col}`")

    # 2) 正式 derived 特征（走原有逻辑）
    for d in derived_specs:
        expr_sql = build_derived_expr(d, feature_catalog)
        select_parts.append(f"{expr_sql} AS `{d['alias']}`")

    # 3) sandbox 特征（新加）
    sandbox_cols = _build_sandbox_columns(sandbox_specs, feature_catalog)
    select_parts.extend(sandbox_cols)

    # 4) 目标列
    if target:
        target_expr = build_target_expr(target, feature_catalog)
        select_parts.append(f"{target_expr} AS `y`")

    select_clause = ",\n    ".join(select_parts)

    sql = f"""
    SELECT
        {select_clause}
    FROM {table}
    WHERE
        symbol = %(symbol)s
        AND ts >= %(start_ts)s
        AND ts < %(end_ts)s
    ORDER BY ts
    """

    return sql
```

> 至此，流程就通了：
>
> * LLM 提 `sandbox_derived` →
> * Python AST 编译器验证 + 生成 ClickHouse SQL →
> * `build_select_sql` 拼进 SELECT 列 →
> * DatasetSanity 在 df 层统一体检。

---

## 4. 风险控制：数量 & 复杂度 + 与 DatasetSanity 联动

### 数量 / 复杂度控制

我们已经在两个地方做了“刹车”：

1. `_build_sandbox_columns` 限制最大特征数 `max_sandbox_features`（典型值 3–5）。
2. `compile_sandbox_expr_to_clickhouse` 限制 AST 节点数 `max_nodes`（典型值 48）。

你可以在全局 `guard_cfg` 里暴露这些参数，例如：

```python
# 在 PrincipalInvestigator.propose 里
guard = (guard_cfg or {}) | {
    "allow_params": [...],
    "sandbox": {
        "max_features": 5,
        "max_nodes": 48,
    },
}
```

然后 `_build_sandbox_columns`/编译器读 `guard["sandbox"]`。

### DatasetSanity 联动

* sandbox 特征生成的 NaN / Inf / 常量列
  → 会被 `run_dataset_sanity_checks` 检出：

  * `feature_all_nan`, `feature_zero_variance`, `feature_has_inf` 等
* 在 SanityReport 层面：

  * 这些会进入 `feature_notes` + `risk_flags`
  * AI 大脑看到 `risk_flags` 中某个 sandbox 特征问题太多，可以在后续决策中放弃该 run
* 训练/HPO 端可以做一层过滤：

  * 如果某个 sandbox 特征全 NaN / 零方差 → 从特征集合里剔除
  * 甚至可以在运行中统计“sandbox 特征的留存率”，用来指导后续 auto-evolver

---

## 5. sandbox → 正式特征 的晋升机制（架构草图）

这个阶段不要求立即代码全写完，但我们可以先把“玩法”确定：

### 5.1 feature_registry.json 的基本结构

```jsonc
{
  "_version": "1.0",
  "features": {
    "body_ratio": {
      "id": "body_ratio",
      "table": "bars_1m",
      "expr": "(close - open) / (high - low + 1e-6)",
      "kind": "price_shape",
      "tags": ["sandbox_promoted", "intraday"],
      "created_at": "2025-11-20T01:23:45Z",
      "source": "sandbox_promotion",
      "description": "K线实体长度占总波动区间比例"
    }
  }
}
```

LLM 在 feature_recipe 里只需要写：

```json
"derived": [
  { "alias": "body_ratio", "id": "body_ratio" }
]
```

`build_derived_expr` 会从 `feature_registry` 中查 expr。

### 5.2 晋升脚本（pseudo）

新建 `tools/promote_sandbox_features.py`：

```python
# tools/promote_sandbox_features.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from experiments.registry import load_all_experiments
from feature_registry import load_registry, save_registry


@dataclass
class SandboxUsage:
    expr: str
    alias: str
    runs: List[str]
    avg_sharpe: float
    count: int


def find_good_sandbox_features(
    min_runs: int = 3,
    min_avg_sharpe: float = 1.0,
) -> List[SandboxUsage]:
    exps = load_all_experiments()
    candidates: Dict[str, SandboxUsage] = {}

    for exp in exps:
        sr = (exp.sanity_report or {})
        if sr.get("level") == "error":
            continue

        metrics = exp.metrics or {}
        sharpe = float(metrics.get("sharpe", 0.0))

        # 假设 artifacts 里记录了本次 run 的 sandbox 定义
        sb_defs = (exp.artifacts or {}).get("sandbox_features") or []
        for sb in sb_defs:
            key = sb["expr"].strip()
            if not key:
                continue
            usage = candidates.get(key)
            if not usage:
                usage = SandboxUsage(
                    expr=key,
                    alias=sb["alias"],
                    runs=[],
                    avg_sharpe=0.0,
                    count=0,
                )
                candidates[key] = usage
            usage.runs.append(exp.run_id)
            usage.count += 1
            usage.avg_sharpe += sharpe

    good: List[SandboxUsage] = []
    for u in candidates.values():
        if u.count == 0:
            continue
        u.avg_sharpe /= u.count
        if u.count >= min_runs and u.avg_sharpe >= min_avg_sharpe:
            good.append(u)

    return good


def promote_to_registry():
    reg = load_registry()
    good = find_good_sandbox_features()

    for u in good:
        feature_id = u.alias  # 或者生成一个规范化 id
        if feature_id in reg["features"]:
            continue

        reg["features"][feature_id] = {
            "id": feature_id,
            "table": "bars_1m",  # 可以从 exp.config 里读
            "expr": u.expr,
            "kind": "sandbox_promoted",
            "tags": ["sandbox_promoted"],
            "source": "sandbox_promotion",
            "created_at": "...",
            "description": f"Promoted from sandbox ({u.count} runs, avg_sharpe={u.avg_sharpe:.2f})",
        }

    save_registry(reg)
```

这个脚本可以你手动跑，或者每晚 cron 一次，逐步把“表现稳定的 sandbox 特征”升级成正式特征。

---

## 这轮我们完成了什么？

✅ 定义了 LLM 用来提 sandbox 特征的协议（`sandbox_derived`）
✅ 写了一个 **安全的 AST → ClickHouse 编译器**，完全限制表达式能力
✅ 在 `build_select_sql` 中接入了 sandbox 特征生成逻辑
✅ 在系统层面加了 **数量 / 复杂度限制**，避免大脑过度发挥
✅ 设计了 sandbox 特征的 **晋升机制**，将实验中表现好的组合，慢慢沉淀进正式 `feature_registry`

---

如果你让 Claude Code 按这一轮的代码落地，下一步我建议：

* 我们再走一轮小的集成测试计划：

  1. 手写一个 feature_recipe，包含 2–3 个 sandbox_derived，跑 PI → HPO → DatasetSanity，看输出 df 和 SanityReport。
  2. 故意写一个非法 expr（未知列 / 非法函数 / 太复杂），确认会被优雅拒绝，而不是整个流程炸掉。

你如果懒得写测试计划，我也可以帮你把这几个“集成验证步骤”列得很细，甚至连 pytest 的用例骨架都给你。
好，那我们按计划进入 **Step 2：Label Contract + Label Guard 重构**，直接给你“能落地的设计 + 代码骨架”，方便你让 Claude Code 去实现。

---

## 一、我们要解决什么问题？

当前标签的典型问题：

1. **谁都能乱定义 label**

   * LLM 可以在 `training_plan.target` 里随便写 SQL 表达式（甚至未来泄露）。
2. **label 定义不统一**

   * 不同实验可能用略微不同的 `ret_10m` 定义（log return? simple return? 是否去手续费？）。
3. **缺少路径依赖的真实交易标签**

   * 真实交易是“三维栅栏”：止盈 / 止损 / 时间到期，谁先触发谁决定 label。
4. **Sanity 不检查 label 本身**

   * label 全 0 / 极度不平衡都没被拦下来。

**目标：**

* 所有 label 定义都从一个中心“标签注册表”里来（Label Registry）。
* LLM 只说 “target_id = xxx”，**不能写 SQL**。
* 标签计算逻辑全部在 Python 里，严格控制未来窗口和路径。
* DatasetSanity 把 label 分布也检查进来。
* 实验记录里带上 label 信息，AI 大脑可以看到“这个实验用的是哪种标签”。

---

## 二、Label Registry 设计（统一标签定义）

### 2.1 一个简单的 labels.json

新建文件：`config/labels.json`（可以跟 feature_registry.json 一样放 config 目录）

```json
{
  "_comment": "Label Registry - canonical definitions for model targets",
  "_version": "1.0",

  "fwd_ret_10m_reg": {
    "kind": "forward_return",
    "horizon": "10m",
    "price_col": "close",
    "return_mode": "log",        // "log" or "simple"
    "task": "regression",
    "clip": [-0.2, 0.2],         // optional
    "min_valid_horizon_pct": 0.8
  },

  "fwd_ret_10m_long_short": {
    "kind": "forward_return",
    "horizon": "10m",
    "price_col": "close",
    "return_mode": "log",
    "task": "classification",
    "thresholds": {
      "long": 0.003,
      "short": -0.003
    },
    "classes": ["short", "flat", "long"],
    "min_valid_horizon_pct": 0.8
  },

  "tb_10m_1atr_1atr": {
    "kind": "triple_barrier",
    "horizon": "10m",
    "price_col": "close",
    "vol_col": "atr_14",               // 波动率参考
    "upper_k": 1.0,
    "lower_k": 1.0,
    "task": "classification",
    "classes": ["short", "flat", "long"],
    "encoding": {
      "long_hit": "long",
      "short_hit": "short",
      "time_expired": "flat"
    },
    "min_valid_horizon_pct": 0.8
  }
}
```

要点：

* 所有 label 都有一个 **id**（如 `fwd_ret_10m_reg`），以后 LLM 只允许填这个 id。
* 每个 label 定义清楚：类型（kind）、horizon、price_col、任务类型（回归/分类）、阈值等。
* 支持简单 forward return 和 triple barrier。

---

### 2.2 LabelConfig & Registry 加载

新建：`labels/registry.py`

```python
# labels/registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import json


@dataclass
class LabelConfig:
    label_id: str
    kind: str          # "forward_return" | "triple_barrier" | ...
    horizon: str       # "10m" / "30m" / "1h"
    price_col: str
    task: str          # "regression" | "classification"
    raw_cfg: Dict[str, Any]

    @property
    def horizon_minutes(self) -> int:
        # 你也可以做成更通用的 parse，支持 "1h" 等
        s = self.horizon.strip().lower()
        if s.endswith("m"):
            return int(s[:-1])
        raise ValueError(f"Unsupported horizon format: {self.horizon}")


class LabelRegistry:
    def __init__(self, cfg_path: str | Path):
        self._cfg_path = Path(cfg_path)
        self._labels: Dict[str, LabelConfig] = {}
        self._load()

    def _load(self) -> None:
        data = json.loads(self._cfg_path.read_text(encoding="utf-8"))
        for label_id, cfg in data.items():
            if label_id.startswith("_"):
                continue
            self._labels[label_id] = LabelConfig(
                label_id=label_id,
                kind=cfg["kind"],
                horizon=cfg["horizon"],
                price_col=cfg["price_col"],
                task=cfg["task"],
                raw_cfg=cfg,
            )

    def get(self, label_id: str) -> LabelConfig:
        if label_id not in self._labels:
            raise KeyError(f"Unknown label_id: {label_id}")
        return self._labels[label_id]

    def exists(self, label_id: str) -> bool:
        return label_id in self._labels
```

> 以后你在任何地方需要 label，只需要：
> `label_cfg = label_registry.get("fwd_ret_10m_reg")`
> 然后交给统一的 label 计算函数。

---

## 三、标签计算核心逻辑（Label Guard）

新建：`labels/compute.py`

```python
# labels/compute.py

from __future__ import annotations

from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

from .registry import LabelConfig


def apply_label(
    df: pd.DataFrame,
    label_cfg: LabelConfig,
    *,
    symbol_col: str = "symbol",
    ts_col: str = "ts",
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    核心入口：
    - 输入：仅包含特征的 df（尚未包含 label）
    - 输出：带 label 的 df, 以及 label_meta（分布、丢弃行数等）
    """

    # 确保排序，避免乱序导致未来窗口错乱
    df = df.sort_values([symbol_col, ts_col]).copy()

    if label_cfg.kind == "forward_return":
        return _apply_forward_return(df, label_cfg, symbol_col, ts_col)
    elif label_cfg.kind == "triple_barrier":
        return _apply_triple_barrier(df, label_cfg, symbol_col, ts_col)
    else:
        raise ValueError(f"Unsupported label kind: {label_cfg.kind}")
```

### 3.1 Forward Return 实现（无泄露）

```python
def _apply_forward_return(
    df: pd.DataFrame,
    label_cfg: LabelConfig,
    symbol_col: str,
    ts_col: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    cfg = label_cfg.raw_cfg
    price_col = cfg["price_col"]
    return_mode = cfg.get("return_mode", "log")
    horizon_m = label_cfg.horizon_minutes

    # 假设 df 是 1 分钟 bar， horizon_m=10 => shift -10
    # 如果以后有 5s 数据，可以把 horizon / bar_interval 传进来
    bar_interval_minutes = 1
    shift_n = horizon_m // bar_interval_minutes

    if shift_n <= 0:
        raise ValueError(f"Invalid horizon / bar interval: shift_n={shift_n}")

    # groupby symbol 保证多标的情况正确
    def _group_compute(g: pd.DataFrame) -> pd.Series:
        fwd_price = g[price_col].shift(-shift_n)
        if return_mode == "log":
            ret = np.log(fwd_price / g[price_col])
        else:
            ret = (fwd_price - g[price_col]) / g[price_col]
        return ret

    df["__tmp_label_ret"] = (
        df.groupby(symbol_col, group_keys=False).apply(_group_compute)
    )

    # 由于未来窗口，最后 shift_n 行会是 NaN，需要丢弃或打标 invalid
    min_valid_pct = cfg.get("min_valid_horizon_pct", 0.8)
    # 简化：只要有 fwd_price，就算 valid，这里不做 path truncation

    label_col = cfg.get("alias", "y")
    if label_cfg.task == "regression":
        y = df["__tmp_label_ret"]
        # clip
        clip_cfg = cfg.get("clip")
        if clip_cfg and isinstance(clip_cfg, (list, tuple)) and len(clip_cfg) == 2:
            y = y.clip(clip_cfg[0], clip_cfg[1])
        df[label_col] = y
    else:
        # classification: 根据 thresholds 分成 long / short / flat
        th = cfg["thresholds"]
        long_th = th["long"]
        short_th = th["short"]

        def _to_class(v: float) -> str:
            if pd.isna(v):
                return "invalid"
            if v >= long_th:
                return "long"
            if v <= short_th:
                return "short"
            return "flat"

        df[label_col] = df["__tmp_label_ret"].apply(_to_class)

    # 清理临时列
    df = df.drop(columns=["__tmp_label_ret"])

    # 丢弃末尾 shift_n 行（没有完整 horizon）
    # 或者保留但标记为 invalid；这里选择直接 drop，更干净
    df = df.groupby(symbol_col, group_keys=False).apply(
        lambda g: g.iloc[:-shift_n] if len(g) > shift_n else g.iloc[0:0]
    )

    # label 统计，用于 sanity 和 Brain
    meta = _build_label_meta(df, label_col, label_cfg)

    return df, meta
```

---

### 3.2 Triple Barrier 实现（路径依赖）

这是简化版 triple-barrier，但足够机构级使用（你以后可以升级）。

```python
def _apply_triple_barrier(
    df: pd.DataFrame,
    label_cfg: LabelConfig,
    symbol_col: str,
    ts_col: str,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    cfg = label_cfg.raw_cfg
    price_col = cfg["price_col"]
    vol_col = cfg["vol_col"]
    upper_k = cfg["upper_k"]
    lower_k = cfg["lower_k"]
    label_col = cfg.get("alias", "y")
    horizon_m = label_cfg.horizon_minutes
    bar_interval_minutes = 1
    horizon_bars = horizon_m // bar_interval_minutes

    # 三个事件：上轨触发 / 下轨触发 / 时间到期
    def _label_one_group(g: pd.DataFrame) -> pd.DataFrame:
        g = g.copy().reset_index(drop=True)
        n = len(g)
        labels = ["invalid"] * n

        for i in range(n):
            # 剩余 bars 不够 horizon，标记 invalid 或 break
            if i + 1 >= n:
                break

            entry_price = g.at[i, price_col]
            vol = g.at[i, vol_col]
            if pd.isna(entry_price) or pd.isna(vol) or vol <= 0:
                continue

            up_level = entry_price * (1 + upper_k * vol / entry_price)
            dn_level = entry_price * (1 - lower_k * vol / entry_price)

            end_j = min(n, i + 1 + horizon_bars)
            hit_up = False
            hit_dn = False
            for j in range(i + 1, end_j):
                p = g.at[j, price_col]
                if pd.isna(p):
                    continue
                if p >= up_level:
                    hit_up = True
                    break
                if p <= dn_level:
                    hit_dn = True
                    break

            enc = cfg["encoding"]
            if hit_up and not hit_dn:
                labels[i] = enc.get("long_hit", "long")
            elif hit_dn and not hit_up:
                labels[i] = enc.get("short_hit", "short")
            else:
                labels[i] = enc.get("time_expired", "flat")

        g[label_col] = labels
        return g

    df = df.groupby(symbol_col, group_keys=False).apply(_label_one_group)

    meta = _build_label_meta(df, label_col, label_cfg)
    return df, meta
```

### 3.3 label_meta 统计

```python
def _build_label_meta(
    df: pd.DataFrame,
    label_col: str,
    label_cfg: LabelConfig,
) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "label_id": label_cfg.label_id,
        "kind": label_cfg.kind,
        "task": label_cfg.task,
        "horizon": label_cfg.horizon,
        "label_col": label_col,
        "total_rows": int(len(df)),
        "nan_count": int(df[label_col].isna().sum()),
    }

    if label_cfg.task == "regression":
        y = df[label_col].dropna()
        meta.update(
            {
                "mean": float(y.mean()) if len(y) else None,
                "std": float(y.std()) if len(y) else None,
                "min": float(y.min()) if len(y) else None,
                "max": float(y.max()) if len(y) else None,
            }
        )
    else:
        vc = df[label_col].value_counts(dropna=False).to_dict()
        meta["class_counts"] = {str(k): int(v) for k, v in vc.items()}

    return meta
```

这样：

* label 分布信息可以直接写入 `ExperimentRecord.sanity_report` 或 `label_meta` 字段。
* AI 大脑可以用这些信息判断：这个 label 是否严重失衡 / 是否有问题。

---

## 四、接入 PI（Principal Investigator）：禁止 LLM 写 target SQL

现在来修改你发的 `PrincipalInvestigator`，让它只允许用 `target_id`，不再接受 LLM 自己定义的 target SQL。

### 4.1 修改 PI 构造：引入 LabelRegistry

```python
# pi/principal_investigator.py (修改版片段)

from labels.registry import LabelRegistry
from labels.compute import apply_label

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
        # 新增：Label Registry
        labels_cfg_path = (llm_cfg or {}).get("labels_cfg_path", "config/labels.json")
        self._label_registry = LabelRegistry(labels_cfg_path)
```

### 4.2 在 propose 里使用 LabelRegistry + apply_label

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
        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"特征: {', '.join(catalog)}\n"
            f"预测窗口: {horizon}\n"
            "注意: 只能从 label_registry 里选 target_id, 不要写 SQL。"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。\n"
            "training_plan.target_id 必须是一个已注册的 label id（例如 fwd_ret_10m_reg）。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {})
        tplan = plan.get("training_plan", {})

        # === Label Guard ===
        target_id = tplan.get("target_id")
        if not target_id or not self._label_registry.exists(target_id):
            # fallback 默认标签，也可以直接 raise
            target_id = "fwd_ret_10m_reg"
        label_cfg = self._label_registry.get(target_id)

        # 注意：build_select_sql 只返回“特征”，不再在 SQL 里计算 label
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,  # 这里改为 None，label 在 Python 里计算
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # DatasetSanity 基本检查（之前已经接入）
        # sanity_report = run_dataset_sanity_checks(df, cfg=...)

        # 计算 label（Label Guard 核心）
        df_labeled, label_meta = apply_label(df, label_cfg)

        # 构建 HPO grid（增加 target_id 信息）
        grid = {
            "model_type": [],
            "feature_subset": [
                recipe.get("base_features", []) + [d.get("alias") for d in recipe.get("derived", [])]
            ],
            "task": [tplan.get("task", label_cfg.task)],  # 用 label 的默认 task
            "decision": [tplan.get("decision", {"mode": "quantile", "q_long": 0.7, "q_short": 0.3})],
            "model_params": [],
            "target_id": [label_cfg.label_id],  # 新增：把 label_id 放进 grid，方便日志化
        }
        for cand in tplan.get("model_candidates", []) or []:
            grid["model_type"].append(cand.get("model_type", "xgb"))
            grid["model_params"].append(cand.get("model_params", {}))

        guard = (guard_cfg or {}) | {
            "allow_params": ["model_type", "feature_subset", "task", "decision", "model_params", "target_id"]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        # 把 label 信息也挂在 PIResult 上
        tplan["target_id"] = label_cfg.label_id
        tplan["label_meta"] = label_meta

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df_labeled,
            hpo_grid=grid_sane,
        )
```

> 这样：
>
> * LLM 再也不能写 `target.sql_expr` 之类的东西。
> * 它只能选 `target_id`，并且我们会校验。
> * label 完全由 Python 控制，**不会有“未来泄露”的自由度**。

---

## 五、把 label_meta 接入 DatasetSanity & Brain

你之前的 DatasetSanity 已经在检查特征，我们只需要加一段逻辑：

### 5.1 DatasetSanity 报告里加 label 部分

在 `run_dataset_sanity_checks(df, cfg)` 里：

```python
def run_dataset_sanity_checks(df, cfg) -> DatasetSanityReport:
    # 省略已有代码...
    label_col = cfg.get("label_col")
    label_meta = cfg.get("label_meta")  # 来自 apply_label

    if label_col and label_col in df.columns:
        # 如果是分类，看 class 分布
        if isinstance(label_meta.get("class_counts"), dict):
            total = max(label_meta.get("total_rows", len(df)), 1)
            class_ratios = {
                k: v / total for k, v in label_meta["class_counts"].items()
            }
            # 如果某个类占比 > 0.99，给一个 warn
            for cls, r in class_ratios.items():
                if r > 0.99:
                    risk_flags.append(f"label_extreme_imbalance:{label_col}:{cls}")
                    notes.append(
                        f"Label {label_col} extremely imbalanced: class {cls} ratio={r:.3f}"
                    )
        # 如果是回归，看 std 是否接近 0
        if label_meta.get("task") == "regression":
            std = label_meta.get("std")
            if std is not None and std < 1e-6:
                risk_flags.append(f"label_zero_variance:{label_col}")
                notes.append(
                    f"Label {label_col} has near-zero variance (std={std})"
                )

    # 最后根据 risk_flags 决定 level
    # ...
```

调用时，在 PI 里传：

```python
sanity_report = run_dataset_sanity_checks(
    df_labeled,
    cfg={
        "dataset_id": f"{symbol}_{horizon}_{start_ts}_{end_ts}",
        "label_col": label_cfg.raw_cfg.get("alias", "y"),
        "label_meta": label_meta,
        # ...其它参数
    },
)
```

### 5.2 Brain 侧：看到 label 定义 + 分布

在 Phase 4A 的 `ExperimentRecord` 里加：

```python
artifacts = {
    "feature_recipe": feature_recipe,
    "sandbox_features": feature_recipe.get("sandbox_derived", []) or [],
    "label": {
        "label_id": label_cfg.label_id,
        "kind": label_cfg.kind,
        "task": label_cfg.task,
        "horizon": label_cfg.horizon,
        "meta": label_meta,
    },
}
```

在 Brain 的 context builder 里（类似上次的 sandbox summary），多加一块：

```python
def summarize_label(exp) -> Dict[str, Any]:
    lbl = (exp.artifacts or {}).get("label") or {}
    meta = lbl.get("meta") or {}

    human = f"Label {lbl.get('label_id')} ({lbl.get('kind')}, horizon={lbl.get('horizon')})"

    if lbl.get("task") == "classification":
        cc = meta.get("class_counts", {})
        human += " class distribution: " + ", ".join(
            f"{k}={v}" for k, v in cc.items()
        )
    else:
        human += f" stats: mean={meta.get('mean')}, std={meta.get('std')}"

    return {
        "label": lbl,
        "label_summary_text": human,
    }
```

这样大脑在选策略时可以看到：

* 用的是 `fwd_ret_10m_reg` 还是 `tb_10m_1atr_1atr`
* label 是否极度不平衡 / 是否有问题

---

## 六、小结 & 下一步

这一轮 “继续” 我们完成了：

1. 设计并实现了 **Label Registry（labels.json + LabelConfig + LabelRegistry）**
2. 实现了 **无泄露的标签计算核心**（forward return + triple barrier），全部在 Python 端
3. 改造了 `PrincipalInvestigator.propose`：

   * LLM 只能选 `target_id`，不能写 target SQL
   * `build_select_sql` 只负责特征，不再算 label
   * `apply_label` 计算 label，并返回 `label_meta`
4. 将 label 信息接入 DatasetSanity 和 ExperimentRecord，给 AI 大脑提供可见的 label 质量信息。

接下来如果你说一句 **“继续”**，我会按之前的大路线进入：

* **Step 3：Feature Registry 收敛 / 规范化（把散落在 feature_programs.json 的定义统一起来）**
* 再往后是：**Phase 4B 的“策略选择器 + 模型运行时”集成建议**（但这是需要结合你现有训练脚本/模型保存方式的）。

你现在可以先把这一轮代码交给 Claude Code 落地和跑测试，然后我们再向下推进。
好，那我们直接进入**Step 3：Feature Registry + LLM 特征约束**，把“特征这块”彻底从野生状态收拢到有规矩、可审计的状态。

这一步的目标只有两个字：**收权**——
特征的“生杀大权”从 LLM 手里收回来，交给一个你能看懂、能 diff、能 code review 的 **feature_registry.json**。

---

## 一、我们现在的状态（接上一步）

目前已经完成的底座：

* ✅ **DatasetSanity**：任何 DataFrame 进模型之前，都要过一遍体检。
* ✅ **Label Registry + Label Guard**：

  * 所有 label 必须从 `labels.json` 里来（`target_id`）。
  * 标签计算在 Python 里（forward return + triple barrier），杜绝未来泄露。
  * 标签分布写入 `label_meta`，接进 sanity & Brain。

接下来这一步要做的是：

> **特征也要走同样的“注册表 + 审计”机制**，LLM 只能选 ID，不允许写 SQL / 表达式。

---

## 二、Feature Registry 的 JSON 设计

新建：`config/feature_registry.json`

> 这里只给**最小可用版本**，你后面可以逐步把现有特征整理进去。

```json
{
  "_comment": "Feature Registry - canonical, leak-safe feature definitions for AI Brain and PI.",
  "_version": "1.0",

  "features": {
    "close": {
      "kind": "raw",
      "expr": "close",
      "dtype": "float64",
      "leak_safe": true,
      "description": "Last traded price for the bar.",
      "tags": ["price", "base"],
      "default": true
    },

    "volume": {
      "kind": "raw",
      "expr": "volume",
      "dtype": "float64",
      "leak_safe": true,
      "description": "Traded volume for the bar.",
      "tags": ["volume", "base"],
      "default": true
    },

    "vwap_1m": {
      "kind": "derived_sql",
      "expr": "vwap",            // 若 vwap 已在 ClickHouse 里预先算好，就直接用列名
      "dtype": "float64",
      "deps": ["vwap"],
      "leak_safe": true,
      "description": "VWAP on 1m bars.",
      "tags": ["price", "vwap", "1m"]
    },

    "ret_1m": {
      "kind": "window",
      "expr":
        "log(close / lagInFrame(close, 1)) OVER (PARTITION BY symbol ORDER BY ts)",
      "dtype": "float64",
      "deps": ["close"],
      "leak_safe": true,
      "description": "1-bar (1m) log return.",
      "tags": ["return", "1m"]
    },

    "rsi_14": {
      "kind": "precomputed",
      "expr": "rsi_14",          // 已经由离线 ETL 写进表里的列
      "dtype": "float64",
      "deps": ["rsi_14"],
      "leak_safe": true,
      "description": "14-period RSI on 1m bars.",
      "tags": ["indicator", "rsi", "1m"]
    }
  }
}
```

说明：

* key（如 `"ret_1m"`）就是 **feature_id**。
* `expr` 是 **ClickHouse SQL 片段**（可以是列名，也可以是窗口函数表达式）。
* `kind` 用来区分：

  * `raw`: 直接是底层表的列（`close`, `volume`）
  * `precomputed`: 你离线算好的列（`rsi_14`、`atr_14`）
  * `derived_sql`: 简单表达式
  * `window`: 需要窗口函数
* `leak_safe`：未来如果有需要，你可以标记某些特征为 `false`，禁止 AI 使用。

> ✅ **重要原则**：**Registry 不需要一口气写完**。
> 一开始只收紧 AI 大脑那一小撮常用特征；后面慢慢把整个 feature universe 搬进来。

---

## 三、Feature Registry loader

新建：`features/registry.py`

```python
# features/registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import json


@dataclass
class FeatureConfig:
    feature_id: str
    kind: str           # raw | precomputed | derived_sql | window | sandbox?
    expr: str           # ClickHouse SQL snippet
    dtype: str
    deps: List[str]
    leak_safe: bool
    tags: List[str]
    description: str = ""
    default: bool = False


class FeatureRegistry:
    def __init__(self, cfg_path: str | Path) -> None:
        self._cfg_path = Path(cfg_path)
        self._features: Dict[str, FeatureConfig] = {}
        self._load()

    def _load(self) -> None:
        data = json.loads(self._cfg_path.read_text(encoding="utf-8"))
        feats = data.get("features", {})
        for fid, cfg in feats.items():
            if fid.startswith("_"):
                continue
            self._features[fid] = FeatureConfig(
                feature_id=fid,
                kind=cfg.get("kind", "raw"),
                expr=cfg["expr"],
                dtype=cfg.get("dtype", "float64"),
                deps=list(cfg.get("deps", [])),
                leak_safe=bool(cfg.get("leak_safe", True)),
                tags=list(cfg.get("tags", [])),
                description=cfg.get("description", ""),
                default=bool(cfg.get("default", False)),
            )

    def get(self, feature_id: str) -> FeatureConfig:
        if feature_id not in self._features:
            raise KeyError(f"Unknown feature_id: {feature_id}")
        return self._features[feature_id]

    def exists(self, feature_id: str) -> bool:
        return feature_id in self._features

    def list_ids(self) -> List[str]:
        return sorted(self._features.keys())

    def leak_safe_ids(self) -> List[str]:
        return sorted(
            fid for fid, f in self._features.items() if f.leak_safe
        )

    def filter_by_tags(self, required: Iterable[str]) -> List[str]:
        req = set(required)
        out: List[str] = []
        for fid, f in self._features.items():
            if req.issubset(set(f.tags)):
                out.append(fid)
        return sorted(out)
```

---

## 四、重写 build_select_sql：只接收 feature_id，不接收 SQL

现在来动你这段：

```python
from pi.feature_sql import build_select_sql
```

我们直接重构 `pi/feature_sql.py`，让它从 `FeatureRegistry` 拿 SQL，而不是从 LLM 获得自由表达式。

### 4.1 pi/feature_sql.py（新实现）

```python
# pi/feature_sql.py

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from features.registry import FeatureRegistry


# 全局单例（也可以在 higher-level 传进来）
_feature_registry = FeatureRegistry("config/feature_registry.json")


def _resolve_feature_ids(recipe: Dict[str, Any]) -> List[str]:
    """
    从 LLM 的 feature_recipe 中解析 feature_id 列表，并过滤掉未知 / 不安全的特征。
    期望 recipe 格式大致是：

    {
      "base_features": ["close", "volume", "ret_1m"],
      "derived": [
          {"id": "rsi_14"},
          {"id": "vwap_1m"}
      ]
    }
    """
    base = recipe.get("base_features", []) or []
    derived = recipe.get("derived", []) or []

    ids: List[str] = []

    # base 部分
    for fid in base:
        if not isinstance(fid, str):
            continue
        if not _feature_registry.exists(fid):
            # TODO: 这里可以打日志 warning
            continue
        if not _feature_registry.get(fid).leak_safe:
            # TODO: 日志：被 leak_safe 拦截
            continue
        ids.append(fid)

    # derived 部分
    for item in derived:
        if isinstance(item, str):
            fid = item
        elif isinstance(item, dict):
            fid = item.get("id") or item.get("feature_id") or item.get("alias")
        else:
            continue

        if not fid:
            continue
        if not _feature_registry.exists(fid):
            continue
        if not _feature_registry.get(fid).leak_safe:
            continue
        ids.append(fid)

    # 去重
    seen = set()
    uniq: List[str] = []
    for fid in ids:
        if fid in seen:
            continue
        seen.add(fid)
        uniq.append(fid)
    return uniq


def build_select_sql(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    target: Any | None,       # 已经不用了，保留是为了兼容
    feature_catalog: Iterable[str] | None = None,  # ClickHouse 那边的 columns，作为 guard
) -> str:
    """
    使用 FeatureRegistry 生成 SELECT 查询：
    - 只使用 registry 中允许的 feature_id
    - 不在 SQL 里计算 label（label 在 Python 端由 Label Guard 计算）
    """

    feature_ids = _resolve_feature_ids(recipe)
    if not feature_ids:
        # fallback：至少给 close/volume
        for fid in ("close", "volume"):
            if _feature_registry.exists(fid):
                feature_ids.append(fid)

    select_exprs: List[str] = [
        "ts",
        "symbol",
    ]

    for fid in feature_ids:
        cfg = _feature_registry.get(fid)
        # 简单做法：expr 直接放；复杂情况可以根据 kind 决定是否用窗口。
        select_exprs.append(f"{cfg.expr} AS {fid}")

    select_clause = ",\n       ".join(select_exprs)

    sql = f"""
    SELECT
       {select_clause}
    FROM {table}
    WHERE symbol = %(symbol)s
      AND ts >= %(start_ts)s
      AND ts < %(end_ts)s
    ORDER BY ts
    """

    return sql.strip()
```

> 这样：
>
> * **LLM 再也不能写 SQL**，只能写 `feature_id`。
> * 所有 SQL 表达式都从 `feature_registry.json` 出来，可审计、可 diff。
> * 如果 LLM 提的特征不在 registry 里，直接被静默过滤掉。

---

## 五、接回 PrincipalInvestigator（PI）

我们在上一步已经改造了 `PrincipalInvestigator` 加入 LabelRegistry。

现在要把 `build_select_sql` 的新用法对齐一下：

```python
# pi/principal_investigator.py 关键片段

from pi.feature_sql import build_select_sql
from features.registry import FeatureRegistry
from labels.registry import LabelRegistry
from labels.compute import apply_label

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

        labels_cfg_path = (llm_cfg or {}).get("labels_cfg_path", "config/labels.json")
        self._label_registry = LabelRegistry(labels_cfg_path)

        # 可选：如果你希望在 prompt 里把允许的 feature_id 告诉 LLM
        feats_cfg_path = (llm_cfg or {}).get("features_cfg_path", "config/feature_registry.json")
        self._feature_registry = FeatureRegistry(feats_cfg_path)

    def propose(...):
        catalog = _fetch_catalog(self._client, table)

        # 在 prompt 里给 LLM 一份 "allowed feature ids" 清单
        allowed_feature_ids = self._feature_registry.leak_safe_ids()
        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"底层可用列(仅供参考): {', '.join(catalog)}\n"
            f"允许使用的特征ID列表(必须从中选择): {', '.join(allowed_feature_ids)}\n"
            f"预测窗口: {horizon}\n"
            "注意: 只能从上述 feature_id 中选择 base_features 和 derived 特征，"
            "并且 training_plan.target_id 必须是 label_registry 中的合法 ID。"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。\n"
            "- feature_recipe.base_features: 只填 feature_id 字符串数组\n"
            "- feature_recipe.derived: 只填 {\"id\": feature_id} 数组\n"
            "- 不要写 SQL 表达式或列名\n"
            "- training_plan.target_id 必须为合法 label_id（例如 fwd_ret_10m_reg）。"
        )

        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # Label Guard（上一轮已经设计，这里直接用）
        target_id = tplan.get("target_id")
        if not target_id or not self._label_registry.exists(target_id):
            target_id = "fwd_ret_10m_reg"
        label_cfg = self._label_registry.get(target_id)

        # 生成 SQL（不算 label）
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,
            feature_catalog=catalog,
        )
        df = self._client.query_df(sql)

        # 体检 + label 计算（上一轮的逻辑）
        df_labeled, label_meta = apply_label(df, label_cfg)
        ...
```

> 这一段的关键是：
>
> * prompt 里明确告诉 LLM：**只能用 feature_id，不准写表达式**。
> * 代码里只通过 FeatureRegistry 映射 SQL。
> * 如果 LLM 填了一个不存在的 id，会被 `_resolve_feature_ids()` 静默过滤掉。

---

## 六、和 Sandbox 特征的关系

之前我们设计过 **sandbox 特征机制**（AI 可以在一个受控沙箱里“发明”新特征，跑实验，成功后再晋升为正式特征）。

这里做一个简洁的对齐：

1. **正式特征**：

   * 全部写进 `feature_registry.json` 的 `"features"`。
   * 由 FeatureRegistry 管理。
   * 能在任何生产训练 / HPO 中使用。

2. **Sandbox 特征**：

   * 不直接写进 `feature_registry.json`。
   * 用我们之前的 `sandbox_features.py`：

     * LLM 提出候选表达式。
     * 通过语法/泄露检查。
     * 在 **Python 层** 对 `dataset_df` 做 `df.eval(...)` 或手写计算。
   * 只有当 sandbox 实验效果好，你再人工 review，把这条表达式迁移到 `feature_registry.json`，变成一个正式的 feature_id。

好处：

* Registry 仍然是干净可控的；
* Sandbox 是“实验池”，不会污染生产特征空间；
* AI 大脑的创造力仍然可以释放，但必须通过 Sandbox → Registry 的晋升流程。

---

## 七、下一步建议（你可以直接让 Claude Code 开始干）

这轮 “继续” 我们完成了：

* ✅ 设计并实现 `config/feature_registry.json` 的结构；
* ✅ 实现 `FeatureRegistry` loader；
* ✅ 重写 `build_select_sql`，只接收 feature_id，所有 SQL 来自 Registry；
* ✅ 把 Registry 和新的 `build_select_sql` 接回 `PrincipalInvestigator`，在 prompt 层就把约束写死。

**下一步（你下次再说“继续”的时候我会做）：**

1. **小范围落地 & 回归测试建议**：

   * 如何在不推倒现有系统的前提下，以“单标的 + 单时间段”的方式验证新 Registry 流程是正确的。

2. **用 DatasetSanity + LabelMeta + FeatureRegistry 给 Brain 提供一个“Dataset&Label&Feature Summary”**：

   * 让 AI 大脑每次决策前，都知道：

     * 用了哪些特征（id 列表 + 标签描述）
     * 标签定义和分布
     * 数据质量评级（sanity 的 level + risk_flags）

如果你觉得这一步代码已经够你让 Claude 开始改，那你可以继续丢日志给我；
如果准备好了，下一句就还是那两个字：**“继续”**，我就按上面的顺序往下推进。
