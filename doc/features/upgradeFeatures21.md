好，我们把上一轮的事儿真正「接上电」：

> 目标：
>
> * 把 **ClickHouse 预体检 + DatasetSanity + Label 体检**
> * 变成一份 **结构化 JSON 报告 + 简短自然语言摘要**，
> * 接入到 **ExperimentRecord + AI 大脑上下文** 里，让大脑在决策前“先看体检报告”。

我分三块来落地，你可以直接丢给 Claude Code 改：

---

## 一、定义统一的 SanityReport（让所有体检结果有一个家）

新增 / 扩展：`dataset_sanity/report.py`

```python
# dataset_sanity/report.py

from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional

from .core import SanityIssue
from .clickhouse_stats import TableStats


@dataclass
class SanityReport:
    # 聚合后的整体等级：ok / warn / error
    level: str

    # 一句话 summary，给人和 LLM 看
    summary: str

    # 标签健康状况：ok / warn / error
    label_health: str
    label_notes: List[str] = field(default_factory=list)

    # 特征整体状况，用于 AI 大脑判断是否「谨慎」
    feature_health: str
    feature_notes: List[str] = field(default_factory=list)

    # 风险标签，给大脑用来触发不同行为
    # 例如：["few_rows", "label_imbalance", "many_feature_nulls"]
    risk_flags: List[str] = field(default_factory=list)

    # 汇总的一些关键统计（行数、特征数等）
    stats: Dict[str, Any] = field(default_factory=dict)

    # 原始 issue 列表（方便 debug / 日志）
    raw_issues: List[Dict[str, Any]] = field(default_factory=list)


def build_sanity_report(
    *,
    issues: List[SanityIssue],
    ch_issues: List[Dict[str, Any]],
    table_stats: Optional[TableStats],
    label_col: str = "y",
) -> SanityReport:
    """
    把 DatasetSanity（含 label）+ ClickHouse 预体检结果
    汇总成一个 SanityReport。
    """
    # 1) 合并所有 issues
    all_issues: List[Dict[str, Any]] = []

    for i in issues:
        all_issues.append(
            {
                "severity": i.severity,
                "code": i.code,
                "message": i.message,
                "column": i.column,
                "extra": i.extra,
            }
        )
    all_issues.extend(ch_issues or [])

    # 2) 计算整体 level
    severities = {i["severity"] for i in all_issues}
    if "error" in severities:
        level = "error"
    elif "warn" in severities:
        level = "warn"
    else:
        level = "ok"

    # 3) 抽取 label / feature 相关的 issue
    label_notes: List[str] = []
    feature_notes: List[str] = []
    risk_flags: List[str] = []

    for i in all_issues:
        code = i.get("code", "")
        msg = i.get("message", "")
        col = i.get("column")

        # label 相关
        if col == label_col or code.startswith("label_"):
            label_notes.append(msg)
            if code in {
                "label_too_few_non_null",
                "label_all_nan",
                "label_zero_variance",
                "label_too_many_nan",
                "label_class_imbalance",
            }:
                risk_flags.append(code)

        # 特征/整体数据相关
        else:
            feature_notes.append(msg)
            if code in {
                "ch_zero_rows",
                "ch_too_few_rows",
                "ch_high_null_ratio",
                "ch_near_zero_var",
                "feature_all_nan",
                "feature_zero_variance",
            }:
                risk_flags.append(code)

    # 去重 risk_flags
    risk_flags = sorted(set(risk_flags))

    # 4) label_health / feature_health
    def _health_from_codes(codes: List[str]) -> str:
        if any("error" in c or c in {"label_all_nan", "label_too_few_non_null"} for c in codes):
            return "error"
        if any("warn" in c or "imbalance" in c for c in codes):
            return "warn"
        return "ok"

    label_health = _health_from_codes(risk_flags) if label_notes else "ok"
    feature_health = _health_from_codes(risk_flags) if feature_notes else "ok"

    # 5) stats 汇总
    stats: Dict[str, Any] = {}
    if table_stats is not None:
        stats["n_rows"] = table_stats.n_rows
        stats["n_cols"] = len(table_stats.cols)

        # 简单统计几个典型列的 null_ratio 情况
        high_null_cols = sorted(
            [
                (name, cs.null_ratio)
                for name, cs in table_stats.cols.items()
                if cs.null_ratio > 0.5
            ],
            key=lambda x: x[1],
            reverse=True,
        )
        if high_null_cols:
            stats["high_null_cols"] = [
                {"name": name, "null_ratio": ratio}
                for name, ratio in high_null_cols[:10]
            ]

    # 6) 一句话 summary
    parts = []
    if "n_rows" in stats:
        parts.append(f"rows={stats['n_rows']}")
    if "n_cols" in stats:
        parts.append(f"features={stats['n_cols']}")

    parts.append(f"label={label_health}")
    parts.append(f"features_health={feature_health}")
    if risk_flags:
        parts.append("risks=" + ",".join(risk_flags))
    else:
        parts.append("risks=none")

    summary = " | ".join(parts)

    return SanityReport(
        level=level,
        summary=summary,
        label_health=label_health,
        label_notes=label_notes,
        feature_health=feature_health,
        feature_notes=feature_notes,
        risk_flags=risk_flags,
        stats=stats,
        raw_issues=all_issues,
    )


def sanity_report_to_dict(report: SanityReport) -> Dict[str, Any]:
    """
    方便直接放进 ExperimentRecord / JSONL 的 helper。
    """
    return asdict(report)
```

> 到这里，我们就有了一个统一的 `SanityReport` 对象，既能给人看（`summary`、`label_notes`），也能给 AI 看（`risk_flags`、`level`）。

---

## 二、把 SanityReport 串进 PI / HPO / ExperimentRecord

### 1. 扩展 PIResult：带上 Sanity 信息

原来你的 `PIResult` 是这样的：

```python
@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
```

建议改成（增加体检相关）：

```python
from typing import List, Dict, Any, Optional
from dataset_sanity.core import SanityIssue
from dataset_sanity.clickhouse_stats import TableStats
from dataset_sanity.report import SanityReport

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # 新增：体检信息
    ch_issues: List[Dict[str, Any]]
    dataset_issues: List[SanityIssue]
    table_stats: Optional[TableStats]
    sanity_report: SanityReport
```

然后在 `PrincipalInvestigator.propose()` 里：

```python
from dataset_sanity.report import build_sanity_report

class PrincipalInvestigator:
    ...

    def propose(...)-> PIResult:
        ...
        # 1) ClickHouse 预体检
        table_stats = collect_basic_stats(...)
        ch_issues = quick_table_sanity_from_stats(table_stats)

        fatal_ch = [i for i in ch_issues if i["severity"] == "error"]
        if fatal_ch:
            raise ValueError(f"ClickHouse sanity failed: {fatal_ch}")

        # 2) 构造特征 SQL + df
        sql = build_select_sql(...)
        df = self._client.query_df(sql)

        # 3) 添加标签列 + DatasetSanity
        df = add_label_column(...)
        sanity_cfg = SanityConfig(label_col=tplan.get("target_col", "y"))
        dataset_issues = run_dataset_sanity_checks(df, sanity_cfg)
        fatal_ds = [i for i in dataset_issues if i.severity == "error"]
        if fatal_ds:
            raise ValueError(f"DatasetSanity failed: {fatal_ds}")

        # 4) 构建统一 SanityReport
        sanity_report = build_sanity_report(
            issues=dataset_issues,
            ch_issues=ch_issues,
            table_stats=table_stats,
            label_col=sanity_cfg.label_col,
        )

        # 5) 正常返回
        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            ch_issues=ch_issues,
            dataset_issues=dataset_issues,
            table_stats=table_stats,
            sanity_report=sanity_report,
        )
```

这样，**HPO / 训练端** 拿到的 `PIResult` 里，就天然带上：

* `pi_result.sanity_report.level`（总等级）
* `pi_result.sanity_report.risk_flags`（风险标签）
* `pi_result.sanity_report.summary`（一句话）

---

### 2. 接入 ExperimentRecord（Phase 4A 用）

假设 Claude 那边已经有类似的 `ExperimentRecord`（示意）：

```python
@dataclass
class ExperimentRecord:
    run_id: str
    created_at: datetime
    symbol: str
    config: Dict[str, Any]
    metrics: Dict[str, float]
    artifacts: Dict[str, Any]
    tags: Dict[str, str]
    # 这里我们加一项
    sanity_report: Optional[Dict[str, Any]] = None
```

HPO 主流程里通常有类似代码：

```python
pi_result = pi.propose(...)
metrics = train_and_eval(pi_result.dataset_df, pi_result.hpo_grid, ...)

record = ExperimentRecord(
    run_id=run_id,
    created_at=datetime.utcnow(),
    symbol=symbol,
    config={...},
    metrics=metrics,
    artifacts={...},
    tags={...},
    # 新增：
    sanity_report=sanity_report_to_dict(pi_result.sanity_report),
)

experiment_registry.save(record)
```

> 这样，每一条 Run / 实验记录，都绑定了一份「体检报告」。
> Phase 4A 的 **Experiment Index / Summarizer** 就能直接消费这块 JSON，不需要再去重算。

---

## 三、把 Sanity 信息喂给 AI 大脑（Brain Context + Prompt 指南）

### 1. 上下文压缩：给大脑一个简洁的 `sanity` 块

你不希望把整份 raw_issues 丢给 LLM，那会撑爆上下文，也没意义。

所以我们在 orchestrator 里做一层压缩：

```python
# brain/context_builder.py

from typing import Any, Dict
from dataset_sanity.report import SanityReport

def sanity_for_brain(sanity_report: Dict[str, Any]) -> Dict[str, Any]:
    """
    从 ExperimentRecord.sanity_report 中抽取最关键的信息给 LLM。
    """
    level = sanity_report.get("level", "unknown")
    label_health = sanity_report.get("label_health", "unknown")
    feature_health = sanity_report.get("feature_health", "unknown")
    risk_flags = sanity_report.get("risk_flags", [])
    summary = sanity_report.get("summary", "")

    # 只选前几条 note，防止太长
    label_notes = (sanity_report.get("label_notes") or [])[:5]
    feature_notes = (sanity_report.get("feature_notes") or [])[:5]

    return {
        "level": level,
        "label_health": label_health,
        "feature_health": feature_health,
        "risk_flags": risk_flags,
        "summary": summary,
        "label_notes": label_notes,
        "feature_notes": feature_notes,
    }
```

在构建 Brain 的上下文时：

```python
def build_brain_context(exp: ExperimentRecord, ...) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "run_id": exp.run_id,
        "symbol": exp.symbol,
        "metrics": exp.metrics,
        # 其它的 config / artifacts 略…
    }

    if exp.sanity_report:
        ctx["sanity"] = sanity_for_brain(exp.sanity_report)
    else:
        ctx["sanity"] = {"level": "unknown", "risk_flags": []}

    return ctx
```

AI 大脑看到的 JSON 大概是：

```json
{
  "run_id": "NVDA_2025_0115_ret10m_v3",
  "symbol": "NVDA",
  "metrics": { "sharpe": 1.8, "ic": 0.02 },
  "sanity": {
    "level": "warn",
    "label_health": "warn",
    "feature_health": "ok",
    "risk_flags": ["label_class_imbalance"],
    "summary": "rows=120000 | features=32 | label=warn | features_health=ok | risks=label_class_imbalance",
    "label_notes": [
      "Label 'y' looks like classification. classes=2, imbalance_ratio=120.00 > allowed 50.00."
    ],
    "feature_notes": []
  }
}
```

### 2. Prompt 侧规则：让大脑“看体检报告再说话”

在控制 AI 研究大脑（不是交易执行大脑）的 system prompt 里，加一段非常硬的规则（示例）：

```text
You are the research orchestration brain for a quantitative trading system.

You receive a JSON context that includes:
- experiment metrics
- a `sanity` block describing dataset quality (`level`, `risk_flags`, etc.)

You MUST obey the following rules:

1. If `sanity.level == "error"`:
   - You MUST NOT approve using this run as a candidate strategy.
   - Your decision MUST set `action` to "reject" and include a clear `reason`
     mentioning the critical data issue.

2. If `sanity.level == "warn"` or `"label_health" == "warn"`:
   - You may consider this run, but you MUST lower its priority
     compared to clean runs.
   - You MUST mention the `sanity.risk_flags` in your `reasoning`.

3. If `label_health` is `"error"` or `risk_flags` contains `"label_all_nan"` or
   `"label_too_few_non_null"`:
   - Treat this run as invalid. Do not use it to form ensembles or production strategies.

You must output JSON ONLY following the BrainDecision schema.
```

> 这样：
>
> * 数据体检层负责「发现问题」；
> * ExperimentRecord 负责「记账」；
> * ContextBuilder 负责「翻译成简短 JSON」；
> * Prompt 负责「让 AI 大脑根据体检结果约束自己」。

---

## 接下来可以干啥？

这轮我们做完了：

* ✅ SanityReport dataclass + builder
* ✅ 把体检结果塞进 PIResult
* ✅ 在 ExperimentRecord 里挂上 `sanity_report`
* ✅ 给 AI 大脑准备了一个 `sanity` 块和 Prompt 规则

**下一步**，如果你继续要推进，我会：

1. 针对 **sandbox 特征机制**，设计一整套：

   * LLM 提议新特征 SQL → 语法/安全检查 → 暂时注册为 `sandbox_XXX`
   * 训练后如果带来显著收益 → 自动写入 feature_registry → 升级为「正式特征」。

2. 同时补一个小模块：

   * 从 `SanityReport + metrics` 里自动生成「研究日志摘要」（给人类看），比如：

     > “NVDA_2025_0115：Sharpe 1.8，IC 0.02；标签轻微不平衡；建议保留为备选策略。”

你可以先让 Claude 按这一轮的代码修改落地，我再帮你把 **sandbox 特征** 这一块的设计全写出来。
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
