好，我们直接进入 **Sandbox 特征机制**，把 “AI 自己发明特征” 变成一个**有安全边界的正规功能**，而不是现在这种“乱写 SQL 就炸 ClickHouse”的状态。

下面这轮我会：

1. 先定清楚 Sandbox 特征的**设计目标**和约束
2. 给出一个独立模块：`sandbox_features.py`（可直接落地）
3. 教你怎么把它**挂到 PrincipalInvestigator** 里
4. 给出 LLM Prompt 该怎么改

---

## 一、Sandbox 特征的设计目标

目标一句话：

> **允许 LLM 提出新特征表达式，但必须经过机器审查，确认：只用历史列、只用白名单函数、没有注入、没有 label 泄露。通过后自动当成 `derived` 特征参与训练。**

约束：

* **只能用白名单列**：必须在 `feature_catalog`（或者某个 `allowed_columns` 集合）里；
* **只能用白名单函数/操作符**：`+ - * /`, 括号，`abs`, `log`, `exp`, `sqrt`, `greatest`, `least`, `if`, `coalesce` 等；
* **禁止出现关键字/危险符号**：`select`, `from`, `join`, `;`, `--`, `/*`, 子查询等；
* **禁止访问 label / future 列**：比如包含 `'target'`, `'label'`, `'fwd'`, `'future'` 的列名；
* 每次 run **最多允许 N 个 sandbox 特征**，防止一次性造一大堆导致列爆炸；
* 长度限制：单个表达式长度，比如 `< 256` 字符，防止“提示注入型大段 SQL”。

---

## 二、新模块：`sandbox_features.py`

这个模块做三件事：

1. `SandboxFeature` dataclass：表示一个合法的 sandbox 特征；
2. `validate_sandbox_expr`：检查 LLM 给的 expr 是否安全；
3. `build_sandbox_features_from_recipe`：从 `feature_recipe` 里解析/验证 sandbox 特征，返回可用列表 + 错误信息。

你可以新建 `pi/sandbox_features.py`（或顶层 `sandbox_features.py`），代码示例：

```python
# pi/sandbox_features.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Sequence, Tuple

import re


@dataclass
class SandboxFeature:
    alias: str
    expr: str
    description: str | None = None
    source: str = "sandbox"   # 标记来源，方便后面审计
    approved: bool = True     # 通过验证的


class SandboxValidationError(Exception):
    pass


# 允许的函数名（ClickHouse 中常见的一些安全函数）
DEFAULT_ALLOWED_FUNCS = {
    "abs", "log", "log10", "exp", "sqrt",
    "greatest", "least",
    "if", "multiIf",
    "coalesce", "nullIf",
    "toFloat32", "toFloat64",
    "toInt32", "toInt64",
    "round", "floor", "ceil",
    "lagInFrame",  # 如果你允许窗口，但不建议 Sandbox 用太复杂函数
}


# 禁止出现的关键词/符号（简单防注入）
FORBIDDEN_TOKENS = {
    "select", "from", "join", "union", "insert", "update", "delete",
    "drop", "alter", "create", "truncate",
    ";", "--", "/*", "*/",
}

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def validate_sandbox_expr(
    expr: str,
    alias: str,
    allowed_columns: Sequence[str],
    *,
    allowed_funcs: Sequence[str] | None = None,
    forbidden_name_patterns: Sequence[str] | None = None,
    max_length: int = 256,
) -> SandboxFeature:
    """
    验证 LLM 提出的 sandbox 表达式是否安全：
    - 长度限制
    - 无危险关键字
    - 标识符要么是允许的列，要么是允许的函数/常量（如 AND/OR 暂不允许）
    - 不触碰 label/future 列（通过名称 pattern 拦截）
    """

    expr_stripped = expr.strip()
    if not expr_stripped:
        raise SandboxValidationError("empty expression")

    if len(expr_stripped) > max_length:
        raise SandboxValidationError(f"expression too long ({len(expr_stripped)} > {max_length})")

    lowered = expr_stripped.lower()
    for tok in FORBIDDEN_TOKENS:
        if tok in lowered:
            raise SandboxValidationError(f"forbidden token in expression: {tok}")

    # 解析标识符
    allowed_cols_set = set(allowed_columns)
    allowed_funcs_set = set((allowed_funcs or DEFAULT_ALLOWED_FUNCS))

    # 禁止模式，比如包含 "target"/"label"/"fwd"/"future" 的名称
    bad_patterns = list(forbidden_name_patterns or [])
    if not bad_patterns:
        bad_patterns = ["target", "label", "fwd", "future"]

    # 按标识符扫描
    idents = set(IDENT_RE.findall(expr_stripped))
    used_cols: List[str] = []
    used_funcs: List[str] = []

    for ident in idents:
        ident_lower = ident.lower()

        # 禁止名字 pattern
        if any(pat in ident_lower for pat in bad_patterns):
            raise SandboxValidationError(f"identifier {ident} matched forbidden pattern")

        # 是允许的列
        if ident in allowed_cols_set:
            used_cols.append(ident)
            continue

        # 是允许函数
        if ident_lower in allowed_funcs_set:
            used_funcs.append(ident)
            continue

        # 否则，有可能是关键字、常量、运算符等 —— 我们暂时一律禁止，保持保守
        # 如果后面需要支持 AND/OR 等，可以在白名单里加
        raise SandboxValidationError(f"identifier {ident} is neither allowed column nor allowed function")

    if not used_cols:
        raise SandboxValidationError("sandbox expr does not reference any allowed columns")

    return SandboxFeature(
        alias=alias,
        expr=expr_stripped,
        description=None,
        source="sandbox",
        approved=True,
    )


def build_sandbox_features_from_recipe(
    feature_recipe: Dict[str, Any],
    feature_catalog: Sequence[str],
    *,
    max_sandbox_features: int = 5,
    allowed_funcs: Sequence[str] | None = None,
) -> Tuple[List[SandboxFeature], List[Dict[str, Any]]]:
    """
    从 feature_recipe 中解析 sandbox 特征。
    约定：
      feature_recipe["sandbox"] = [
        {"alias": "feat1", "expr": "(close - open) / vwap"},
        ...
      ]
    """
    raw_sandbox = feature_recipe.get("sandbox") or []
    if not isinstance(raw_sandbox, list):
        return [], [{
            "alias": None,
            "expr": None,
            "error": "sandbox field is not a list",
        }]

    sandbox_feats: List[SandboxFeature] = []
    errors: List[Dict[str, Any]] = []

    for idx, item in enumerate(raw_sandbox):
        if len(sandbox_feats) >= max_sandbox_features:
            errors.append({
                "alias": None,
                "expr": None,
                "error": f"too_many_sandbox_features>{max_sandbox_features}",
            })
            break

        if not isinstance(item, dict):
            errors.append({
                "alias": None,
                "expr": str(item),
                "error": f"invalid_sandbox_item_type:{type(item)}",
            })
            continue

        alias = (item.get("alias") or f"sandbox_{idx}").strip()
        expr = (item.get("expr") or "").strip()

        if not alias:
            alias = f"sandbox_{idx}"

        try:
            sf = validate_sandbox_expr(
                expr=expr,
                alias=alias,
                allowed_columns=feature_catalog,
                allowed_funcs=allowed_funcs,
            )
            sandbox_feats.append(sf)
        except SandboxValidationError as e:
            errors.append({
                "alias": alias,
                "expr": expr,
                "error": str(e),
            })

    return sandbox_feats, errors
```

> 这一块是**完全自洽的模块**，你可以直接丢进项目里。后面只要在 PI 那边调用它，把返回的 `SandboxFeature` 转成 `recipe["derived"]` 的一部分即可。

---

## 三、把 Sandbox 挂到 PrincipalInvestigator 里

现在改你的 `PrincipalInvestigator.propose()`，重点在 **解析 LLM 给的 recipe 之后，构造 sandbox 特征并注入 `derived`**。

原始代码关键部分（略简化）：

```python
plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

recipe = plan.get("feature_recipe", {})
tplan = plan.get("training_plan", {})
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
```

现在改成（示例，只展示核心变动）：

```python
from pi.sandbox_features import build_sandbox_features_from_recipe

class PrincipalInvestigator:
    ...

    def propose(...):
        catalog = _fetch_catalog(self._client, table)
        ...
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # 1) 处理 sandbox 特征
        sandbox_feats, sandbox_errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=catalog,
            max_sandbox_features=5,
        )

        # 2) 合并进 derived 列表
        derived = list(recipe.get("derived") or [])
        for sf in sandbox_feats:
            derived.append({
                "alias": sf.alias,
                "expr": sf.expr,
                "source": sf.source,
                "mode": "sandbox",
            })
        recipe["derived"] = derived

        # 你也可以把 sandbox_errors 记到日志里，或挂到 PIResult 里
        if sandbox_errors:
            # 例如：
            # logger.warning("Sandbox feature errors: %s", sandbox_errors)
            pass

        # 3) 用更新后的 recipe 生成 SQL
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=tplan.get("target"),
            feature_catalog=catalog,
        )

        # 4) DatasetSanity + 后续流程（之前我们已经设计过）
        # report = run_clickhouse_dataset_sanity(...)
        # if report.status == "fail": raise RuntimeError(...)

        df = self._client.query_df(sql)

        ...
        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
        )
```

> 注意：这里**完全不需要修改 `build_select_sql`** 的逻辑 —— 它已经是按 `recipe["derived"]` 把 `expr AS alias` 注入 SQL 的（你之前就是这么用的），我们只是往 `derived` 里多塞几条而已。

---

## 四、Prompt 侧：告诉 LLM 怎么用 Sandbox 特征

现在我们要给 AI 一个**清晰、窄化的接口**：

* 它不能乱写 `SELECT ...` 了，只能在 `feature_recipe.sandbox` 里填表达式；
* 表达式只能用我们给出的 `feature_catalog` 里字段；
* 特征最终会被严格验证，不通过就被丢弃。

你可以在 PI 的 `system_prompt` 里加一段 Sandbox 说明，例如：

```python
system_prompt = """
你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。

特征生成规则：

1. 你有一个现有特征白名单 feature_catalog（见 user 提供）。
2. 你可以在 feature_recipe 中使用三个字段：
   - base_features: 从 feature_catalog 中直接选择列名的列表。
   - derived: 使用已有的预定义派生特征（如果你知道的话，可以留空）。
   - sandbox: 你可以在这里提出新的组合特征，但必须遵守以下约束：

[sandbox 特征约束]
- sandbox 是一个列表，每个元素为:
  {"alias": "特征名", "expr": "表达式字符串"}
- 表达式 expr:
  - 只能引用 feature_catalog 中的列名，例如 close, open, high, low, vwap, volume 等。
  - 只能使用安全函数: abs, log, exp, sqrt, greatest, least, if, coalesce, round, floor, ceil 等。
  - 只能使用算术运算符: +, -, *, / 和括号 ()。
  - 严禁出现 select, from, join, union, insert, update, delete, drop 等关键字。
  - 不能引用任何 target/label/fwd/future 等标签列。

系统会对 sandbox 表达式做安全检查：
- 使用了未知列或函数会被拒绝。
- 不符合语法或安全规则的 sandbox 特征会被丢弃。

你的输出必须是 JSON，不要包含 SQL 代码，只能在 sandbox 中写表达式。
"""
```

用户 prompt 里已经有：

```python
user_prompt = (
    f"目标: {goal}\n"
    f"表: {table}\n"
    f"符号: {symbol}\n"
    f"时间: {start_ts}~{end_ts}\n"
    f"特征: {', '.join(catalog)}\n"
    f"预测窗口: {horizon}"
)
```

这就足够让 LLM 知道能用哪些列去构造表达式。

---

## 五、这一轮的“小结 & TODO”

这一轮我们已经把 Sandbox 特征的架构和代码**完整落地到可用程度**：

* ✅ `sandbox_features.py`：安全表达式检查 + 批量构造
* ✅ `PrincipalInvestigator.propose()`：从 `feature_recipe["sandbox"]` 注入到 `derived`
* ✅ Prompt 规范：告诉 LLM 正确使用 sandbox 接口
* ✅ 防御点：

  * 白名单列
  * 白名单函数
  * 关键字/注入拦截
  * 禁止 target/label/fwd/future
  * 每次 run 限制数量 & 长度

你这边的**最小落地动作**：

1. 把 `sandbox_features.py` 加入项目；
2. 在 `PrincipalInvestigator.propose()` 里按上面方式调用 `build_sandbox_features_from_recipe`；
3. 把 PI 的 system_prompt 替换/扩展为包含 Sandbox 规范的版本；
4. 跑一条你熟悉的 symbol（比如 TSLA）做一次 PI 调用，看看：

   * LLM 有没有按要求写 `feature_recipe["sandbox"]`
   * `build_select_sql` 生成的 SQL 是否正常
   * DatasetSanity + HPO 是否能跑通

你如果已经把这些粘进去并能跑，我们下一步就可以开始处理 **Label Contract / Triple Barrier / AI 大脑用 DatasetSanity 报告做决策** 那一块。
你可以随时说一声“继续”，我就按前面的大蓝图推进到下一步。

好，接下来我按我们的大蓝图，正式落地 **「Label Contract（标签契约）」**，把：

* **标签的定义**
* **标签的计算方式**
* **AI 大脑能选哪些标签、不能乱写 SQL**

全部收紧到一个 **可配置 + 可验证 + 无泄露** 的体系里。

这一轮我会给你 **可以直接落地的代码**（你可以按需改路径），包括：

1. `label_registry`：标签注册表（JSON + Python loader）
2. `label_generator`：统一的标签计算（前向收益 & Triple Barrier）
3. `PrincipalInvestigator` 如何改，用 `label_id` 而不是随意的 `target SQL`
4. LLM Prompt 怎么改，让 AI 只在安全集合里挑标签

---

## 一、设计目标：Label Contract 要解决什么问题？

现在的痛点你很清楚：

* 有时候 LLM 会直接写 `target` 的 SQL 表达式，容易产生：

  * **未来信息泄露**（引用未来列或聚合）
  * **与回测标签不一致**（live/backtest 标准不统一）
  * **维护困难**（你都不知道这个 run 用了什么定义）

新的 Label Contract 目标：

1. 所有标签都有一个 **ID**（比如：`ret_10m_close`, `tb_10m_u1.0_d0.5`）；
2. LLM **只能选择 label_id**，不能写 SQL；
3. 标签的具体规则全部写在 **`label_registry.json` + Python 实现** 里；
4. 标签计算逻辑**统一**，可以同时用于：

   * 训练集构造（当前我们做的）
   * 回测 / live 的信号评估

---

## 二、标签注册表：`label_registry.json` + `label_registry.py`

### 1. 示例：`label_registry.json`

先给一个你可以直接放到项目里的 JSON 示例（比如放 `config/label_registry.json`）：

```json
{
  "_comment": "Label Registry - Whitelisted label definitions for AI Brain / HPO / Backtest",
  "_version": "1.0",
  "labels": {
    "ret_10m_close": {
      "type": "regression",
      "mode": "forward_return",
      "horizon": { "unit": "minute", "value": 10 },
      "price_col": "close",
      "transform": "log_return"
    },
    "cls_updown_10m_0.2pct": {
      "type": "classification",
      "mode": "forward_return_sign",
      "horizon": { "unit": "minute", "value": 10 },
      "price_col": "close",
      "threshold": 0.002,
      "pos_label": 1,
      "neg_label": -1
    },
    "tb_10m_u1.0_d0.5": {
      "type": "classification",
      "mode": "triple_barrier",
      "horizon": { "unit": "minute", "value": 10 },
      "price_col": "close",
      "high_col": "high",
      "low_col": "low",
      "up_mult": 1.01,
      "down_mult": 0.995,
      "pos_label": 1,
      "neg_label": -1
    }
  }
}
```

> 你可以先只保留一两个 label，渐进式扩展。关键是：**ID 稳定 + 语义清晰**。

---

### 2. Python：`labels/registry.py`

新建模块：`labels/registry.py`（路径你可以放在 `ai_trader/labels/registry.py` 之类的地方）：

```python
# labels/registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Literal, Optional

import json


LabelMode = Literal["forward_return", "forward_return_sign", "triple_barrier"]
LabelType = Literal["regression", "classification"]
HorizonUnit = Literal["bar", "second", "minute"]
ReturnTransform = Literal["raw_return", "log_return", "pct_return"]


@dataclass
class Horizon:
    unit: HorizonUnit
    value: int


@dataclass
class LabelSpec:
    id: str
    type: LabelType
    mode: LabelMode
    horizon: Horizon

    # Common fields
    price_col: str = "close"
    transform: Optional[ReturnTransform] = None

    # For forward_return_sign
    threshold: Optional[float] = None
    pos_label: Optional[int | str] = None
    neg_label: Optional[int | str] = None

    # For triple_barrier
    high_col: Optional[str] = None
    low_col: Optional[str] = None
    up_mult: Optional[float] = None
    down_mult: Optional[float] = None


class LabelRegistry:
    """
    Small helper around a dict[str, LabelSpec].
    """

    def __init__(self, labels: Mapping[str, LabelSpec]) -> None:
        self._labels: Dict[str, LabelSpec] = dict(labels)

    def get(self, label_id: str) -> LabelSpec:
        try:
            return self._labels[label_id]
        except KeyError:
            raise KeyError(f"Unknown label_id: {label_id!r}. Check label_registry.json")

    def list_ids(self) -> list[str]:
        return sorted(self._labels.keys())

    @classmethod
    def from_dict(cls, cfg: Mapping[str, Any]) -> "LabelRegistry":
        raw_labels = cfg.get("labels") or {}
        out: Dict[str, LabelSpec] = {}

        for lid, spec in raw_labels.items():
            h_cfg = spec.get("horizon") or {}
            horizon = Horizon(
                unit=h_cfg.get("unit", "bar"),
                value=int(h_cfg.get("value", 1)),
            )

            out[lid] = LabelSpec(
                id=lid,
                type=spec["type"],
                mode=spec["mode"],
                horizon=horizon,
                price_col=spec.get("price_col", "close"),
                transform=spec.get("transform"),
                threshold=spec.get("threshold"),
                pos_label=spec.get("pos_label"),
                neg_label=spec.get("neg_label"),
                high_col=spec.get("high_col"),
                low_col=spec.get("low_col"),
                up_mult=spec.get("up_mult"),
                down_mult=spec.get("down_mult"),
            )

        return cls(out)

    @classmethod
    def from_file(cls, path: str | Path) -> "LabelRegistry":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)
```

---

## 三、标签计算：`labels/generator.py`

这个模块统一负责：“给我一个 DataFrame + LabelSpec + bar 粒度 ➜ 返回一个标签 Series”。

### 1. 解析 horizon 为「多少根 K 线」

```python
# labels/generator.py

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .registry import Horizon, LabelSpec


def _resolve_horizon_to_bars(h: Horizon, bar_seconds: int) -> int:
    """
    把 Horizon (bar/second/minute) 统一转换成 horizon_bars（多少根 K 线）。
    """
    if h.unit == "bar":
        return max(1, int(h.value))
    elif h.unit == "second":
        return max(1, int(round(h.value / bar_seconds)))
    elif h.unit == "minute":
        return max(1, int(round(h.value * 60 / bar_seconds)))
    else:
        raise ValueError(f"Unsupported horizon unit: {h.unit}")
```

### 2. 前向收益标签（regression）

```python
def compute_forward_return(
    df: pd.DataFrame,
    spec: LabelSpec,
    *,
    bar_seconds: int,
) -> pd.Series:
    """
    spec.mode == 'forward_return'
    Label(t) = return from t -> t + horizon (future)
    """
    price = df[spec.price_col].astype(float)
    horizon_bars = _resolve_horizon_to_bars(spec.horizon, bar_seconds)

    # future price
    future_price = price.shift(-horizon_bars)

    if spec.transform in (None, "raw_return"):
        # raw difference
        label = future_price - price
    elif spec.transform == "pct_return":
        label = (future_price / price) - 1.0
    elif spec.transform == "log_return":
        label = np.log(future_price / price)
    else:
        raise ValueError(f"Unsupported transform: {spec.transform}")

    # 最后 horizon_bars 行会是 NaN（因为未来数据不存在）
    return label
```

### 3. 前向收益符号标签（classification）

```python
def compute_forward_return_sign(
    df: pd.DataFrame,
    spec: LabelSpec,
    *,
    bar_seconds: int,
) -> pd.Series:
    """
    spec.mode == 'forward_return_sign'
    先算 forward_return，再按阈值签名。
    """
    base_ret = compute_forward_return(df, spec, bar_seconds=bar_seconds)

    thr = spec.threshold or 0.0
    pos_label = spec.pos_label if spec.pos_label is not None else 1
    neg_label = spec.neg_label if spec.neg_label is not None else -1

    # 简单版本：> threshold 为 pos，< -threshold 为 neg，中间可设为 0 或 NaN。
    labels = pd.Series(index=base_ret.index, dtype="float64")

    labels[base_ret > thr] = pos_label
    labels[base_ret < -thr] = neg_label
    labels[base_ret.between(-thr, thr, inclusive="both")] = 0

    return labels
```

### 4. Triple Barrier 标签（路径依赖）

**简单版实现**（O(N * horizon_bars)，先用着，后面再优化 / 写 ClickHouse 版）：

```python
def compute_triple_barrier(
    df: pd.DataFrame,
    spec: LabelSpec,
    *,
    bar_seconds: int,
) -> pd.Series:
    """
    spec.mode == 'triple_barrier'
    标签规则（三栅栏法）：
      - 设起始价格 S = close[t]
      - 上轨 U = S * up_mult
      - 下轨 D = S * down_mult
      - 在未来 horizon_bars 根 K 线中：
          - 若 high >= U 且先于 low <= D 触发 -> pos_label
          - 若 low <= D 且先于 high >= U 触发 -> neg_label
          - 若都未触发 -> 0
    """
    if spec.up_mult is None or spec.down_mult is None:
        raise ValueError("Triple barrier requires up_mult and down_mult")

    price = df[spec.price_col].astype(float).to_numpy()
    high = df[spec.high_col or "high"].astype(float).to_numpy()
    low = df[spec.low_col or "low"].astype(float).to_numpy()

    horizon_bars = _resolve_horizon_to_bars(spec.horizon, bar_seconds)
    n = len(df)

    pos_label = spec.pos_label if spec.pos_label is not None else 1
    neg_label = spec.neg_label if spec.neg_label is not None else -1

    out = np.zeros(n, dtype=float)  # 默认 0（无触发）

    for i in range(n):
        s = price[i]
        u = s * spec.up_mult
        d = s * spec.down_mult

        # 在未来 horizon_bars 内搜索首次触发
        end = min(n, i + horizon_bars + 1)
        hit = 0.0

        for j in range(i + 1, end):
            if high[j] >= u:
                hit = pos_label
                break
            if low[j] <= d:
                hit = neg_label
                break

        out[i] = hit

    # 最后 horizon_bars 行其实信息不完整，可以视作 NaN
    out[n - horizon_bars :] = np.nan
    return pd.Series(out, index=df.index, name=f"label_{spec.id}")
```

### 5. 总入口：`add_label_column`

```python
def add_label_column(
    df: pd.DataFrame,
    spec: LabelSpec,
    *,
    bar_seconds: int,
    col_name: str = "y",
) -> pd.DataFrame:
    """
    对 DataFrame 添加一列标签，返回新的 df（不修改原 df）。
    """
    if spec.mode == "forward_return":
        s = compute_forward_return(df, spec, bar_seconds=bar_seconds)
    elif spec.mode == "forward_return_sign":
        s = compute_forward_return_sign(df, spec, bar_seconds=bar_seconds)
    elif spec.mode == "triple_barrier":
        s = compute_triple_barrier(df, spec, bar_seconds=bar_seconds)
    else:
        raise ValueError(f"Unsupported label mode: {spec.mode}")

    out = df.copy()
    out[col_name] = s
    return out
```

---

## 四、把 Label Contract 挂到 PI：PrincipalInvestigator 改造

### 1. 初始化时加载 LabelRegistry

在你的 `PrincipalInvestigator` 里加一点东西（只展示新增部分）：

```python
from pathlib import Path
from labels.registry import LabelRegistry
from labels.generator import add_label_column

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        label_cfg: Optional[Dict[str, Any]] = None,
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

        # Label registry + bar 粒度配置
        label_cfg = label_cfg or {}
        reg_path = label_cfg.get("registry_path")
        self._label_registry: Optional[LabelRegistry] = None
        if reg_path:
            self._label_registry = LabelRegistry.from_file(Path(reg_path))

        # 例如：1 分钟 bar
        self._bar_seconds: int = int(label_cfg.get("bar_seconds", 60))
```

> 你以后在 config 里可以写：
> `label_cfg = {"registry_path": "config/label_registry.json", "bar_seconds": 60}`

### 2. `propose()`：从 `label_id` 生成标签，而不是让 LLM 写 SQL

原来你是这样：

```python
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
```

现在改成（注意：**不再让 LLM 给 target SQL**，它只给 `label_id`）：

```python
    def propose(...):
        catalog = _fetch_catalog(self._client, table)
        ...
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # 先处理 sandbox 特征（我们上一轮已经设计过）
        sandbox_feats, sandbox_errors = build_sandbox_features_from_recipe(
            feature_recipe=recipe,
            feature_catalog=catalog,
            max_sandbox_features=5,
        )
        derived = list(recipe.get("derived") or [])
        for sf in sandbox_feats:
            derived.append({
                "alias": sf.alias,
                "expr": sf.expr,
                "source": sf.source,
                "mode": "sandbox",
            })
        recipe["derived"] = derived

        # === Label Contract: 只接受 label_id，而不是随意的 target SQL ===
        label_id = tplan.get("label_id")
        if not label_id:
            # 给一个默认值，或者直接报错
            raise ValueError("training_plan.label_id is required (must reference label_registry)")

        if not self._label_registry:
            raise RuntimeError("LabelRegistry is not configured but label_id was provided")

        label_spec = self._label_registry.get(label_id)

        # 只生成特征的 SQL，不直接算标签
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,  # <--- 不让 LLM 控制 target SQL 了
            feature_catalog=catalog,
        )

        df = self._client.query_df(sql)

        # 在 Python 里按统一规则计算标签
        df = add_label_column(
            df,
            spec=label_spec,
            bar_seconds=self._bar_seconds,
            col_name=tplan.get("target_col", "y"),
        )

        # 后面 DatasetSanity + HPO grid 保持原样
        ...
        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
        )
```

> 这样一来：
>
> * LLM **不能再乱写 target 表达式**；
> * 所有 label 的定义都由 `label_registry` + `label_generator` 控制；
> * 训练集 / 回测 / 实盘 可以共享一套标签定义。

---

## 五、Prompt 更新：让 AI 只选 label_id

现在要告诉 PI 的 LLM：

* 你不能写 SQL 了；
* 你只能在 `label_id` 字段里选一个、且必须来自给定列表。

你可以改 `system_prompt` 里关于 training_plan 的部分，比如：

```text
[Label Contract]

- 你不能直接写 SQL 表达式来定义 target。
- 系统有一个标签注册表 label_registry，其中定义了若干标签 ID。
- 你只能在 training_plan 中设置 "label_id"，其值必须是允许列表中的一个。

示例：
  "training_plan": {
    "task": "regression",
    "label_id": "ret_10m_close",
    ...
  }

允许的 label_id 例子（实际列表会在 user prompt 中给出）：
  - "ret_10m_close": 10 分钟前向 log return
  - "cls_updown_10m_0.2pct": 基于 10 分钟收益率的三分类标签
  - "tb_10m_u1.0_d0.5": Triple Barrier 三栅栏标签
```

然后在 `user_prompt` 里加一段 label 列表（从 registry 里读出来）：

```python
allowed_labels = ", ".join(self._label_registry.list_ids()) if self._label_registry else ""
user_prompt = (
    f"目标: {goal}\n"
    f"表: {table}\n"
    f"符号: {symbol}\n"
    f"时间: {start_ts}~{end_ts}\n"
    f"特征: {', '.join(catalog)}\n"
    f"预测窗口: {horizon}\n"
    f"可用标签ID: {allowed_labels}\n"
)
```

这样 AI 大脑在输出 JSON 时就会乖乖写：

```json
"training_plan": {
  "task": "regression",
  "label_id": "ret_10m_close",
  ...
}
```

---

## 六、这一轮的落地 Checklist

你这轮实际要做的事情整理一下：

1. **新建文件**

   * `config/label_registry.json`（先加 1~3 个 label 就好）
   * `labels/registry.py`
   * `labels/generator.py`

2. **改造 `PrincipalInvestigator`**

   * `__init__` 里接收 `label_cfg` 并加载 `LabelRegistry`
   * `propose()`：

     * 不再使用 `tplan["target"]`，改用 `tplan["label_id"]`
     * 用 `LabelRegistry.get(label_id)` 拿到 `LabelSpec`
     * `build_select_sql(... target=None ...)`
     * `df = add_label_column(df, label_spec, bar_seconds=...)`

3. **更新 LLM Prompt**

   * system_prompt：加入 Label Contract 规则
   * user_prompt：附带 `可用标签ID: ...` 列表

4. **做一次小测试**

   * 手动写一个简化的 `training_plan`（不用 LLM），比如：
     `{"task": "regression", "label_id": "ret_10m_close", ...}`
   * 调用 `PrincipalInvestigator.propose()`
   * 看 `dataset_df` 是否多了一列 `y`，且最后几行是 NaN（因为没有未来）。

---

如果你把这一轮的代码粘进去并能正常跑，**你的标签问题基本就从“野生状态”升级为“工厂标准模式”了**：

* LLM 不再掌控 target SQL，防止泄露；
* 标签定义统一可追溯，可以复用到任何 pipeline / 回测；
* 后面我们要做的，只是：

  * 在 DatasetSanity 中**继续检查标签分布（是否极度不平衡）**
  * 让 AI 大脑在看到 “label 分布有问题” 时自动拒绝／减权

下一步，如果你愿意继续，我会在现有 DatasetSanity 的基础上，把 **“标签分布 + 特征-标签相关性”** 那一层也加进去，完成 **“数据体检看标签”** 部分，然后再往上连到 AI 大脑的决策眼睛。
好，我们从刚才的 **Label Contract** 接着往下走，把「数据体检 + 标签体检 + ClickHouse 预检查」这一块真正变成能上生产的东西。

这一轮我做三件事：

1. 让 DatasetSanity **认识标签列**，检查标签分布（不平衡、空值、常数等）。
2. 设计并实现一个 **ClickHouse 端的快速体检层**（不拉全量数据也能先看个大概）。
3. 把这套东西 **串回 PrincipalInvestigator / HPO 流程**，形成“先体检，后训练”的闭环。

> 你这轮可以先把代码丢给 Claude Code 落地，我这边负责设计和给出完整模块。

---

## 一、扩展 DatasetSanity：让它认识标签列

假设你现在已经有一个 `dataset_sanity/core.py`（前面我们多轮“继续”应该已经有类似模块了）。
下面我直接给出「**标签感知版** DatasetSanity」的核心补丁：

### 1. 增强配置：`SanityConfig` 增加 label 相关阈值

```python
# dataset_sanity/core.py

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class SanityIssue:
    severity: str  # "error" | "warn" | "info"
    code: str
    message: str
    column: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SanityConfig:
    # 原有的字段略…

    # === 标签相关阈值 ===
    label_col: str = "y"

    # 标签必须至少有多少非空样本
    min_label_non_null: int = 500

    # 分类任务：最大类别不平衡比（max_count / min_count）
    max_class_imbalance_ratio: float = 50.0

    # 回归任务：允许 0 方差的标签吗（基本不允许）
    allow_zero_var_label: bool = False

    # 标签列允许的 NaN 比例
    max_label_nan_ratio: float = 0.05

    # 是否对 label 做简单的 sanity check
    enable_label_checks: bool = True
```

### 2. 标签检查函数：`check_label_column`

```python
def check_label_column(df: pd.DataFrame, cfg: SanityConfig) -> List[SanityIssue]:
    issues: List[SanityIssue] = []

    if not cfg.enable_label_checks:
        return issues

    if cfg.label_col not in df.columns:
        issues.append(
            SanityIssue(
                severity="error",
                code="label_missing",
                message=f"Label column {cfg.label_col!r} is missing in dataset_df.",
                column=cfg.label_col,
            )
        )
        return issues

    s = df[cfg.label_col]
    n = len(s)
    n_nan = int(s.isna().sum())
    nan_ratio = n_nan / max(1, n)

    issues.append(
        SanityIssue(
            severity="info",
            code="label_basic_stats",
            message=f"Label column {cfg.label_col!r}: n={n}, nan={n_nan} ({nan_ratio:.2%}).",
            column=cfg.label_col,
            extra={"n": n, "nan": n_nan, "nan_ratio": nan_ratio},
        )
    )

    # 1) 非空样本数量
    n_non_null = n - n_nan
    if n_non_null < cfg.min_label_non_null:
        issues.append(
            SanityIssue(
                severity="error",
                code="label_too_few_non_null",
                message=(
                    f"Label {cfg.label_col!r} non-null samples {n_non_null} "
                    f"< required {cfg.min_label_non_null}."
                ),
                column=cfg.label_col,
                extra={"required": cfg.min_label_non_null, "actual": n_non_null},
            )
        )

    # 2) NaN 比例
    if nan_ratio > cfg.max_label_nan_ratio:
        issues.append(
            SanityIssue(
                severity="error",
                code="label_too_many_nan",
                message=(
                    f"Label {cfg.label_col!r} NaN ratio {nan_ratio:.2%} "
                    f"> allowed {cfg.max_label_nan_ratio:.2%}."
                ),
                column=cfg.label_col,
                extra={"max_nan_ratio": cfg.max_label_nan_ratio, "nan_ratio": nan_ratio},
            )
        )

    # 3) 分类/回归自动推断
    # 简单约定：如果唯一值个数 <= 10，且都是整数/有限有限值，视作分类；否则视为回归
    non_null_values = s.dropna()
    uniq_vals = non_null_values.unique()
    n_uniq = len(uniq_vals)

    if n_uniq == 0:
        issues.append(
            SanityIssue(
                severity="error",
                code="label_all_nan",
                message=f"Label {cfg.label_col!r} has no non-null values.",
                column=cfg.label_col,
            )
        )
        return issues

    # 零方差检查（无论回归/分类都值得看）
    if np.all(non_null_values == non_null_values.iloc[0]):
        severity = "error" if not cfg.allow_zero_var_label else "warn"
        issues.append(
            SanityIssue(
                severity=severity,
                code="label_zero_variance",
                message=(
                    f"Label {cfg.label_col!r} is constant (zero variance). "
                    "Model will not learn anything."
                ),
                column=cfg.label_col,
            )
        )

    # 简单判断是否是“离散标签”
    is_discrete = (n_uniq <= 10) and np.all(
        [float(v).is_integer() for v in non_null_values.unique() if pd.notna(v)]
    )

    if is_discrete:
        # 分类任务：检查类别分布
        value_counts = non_null_values.value_counts()
        max_count = int(value_counts.max())
        min_count = int(value_counts.min())
        imbalance = max_count / max(1, min_count)

        issues.append(
            SanityIssue(
                severity="info",
                code="label_class_distribution",
                message=(
                    f"Label {cfg.label_col!r} looks like classification. "
                    f"classes={n_uniq}, imbalance_ratio={imbalance:.2f}."
                ),
                column=cfg.label_col,
                extra={
                    "num_classes": n_uniq,
                    "imbalance_ratio": imbalance,
                    "value_counts": value_counts.to_dict(),
                },
            )
        )

        if imbalance > cfg.max_class_imbalance_ratio:
            issues.append(
                SanityIssue(
                    severity="warn",
                    code="label_class_imbalance",
                    message=(
                        f"Label {cfg.label_col!r} is highly imbalanced: "
                        f"imbalance_ratio={imbalance:.2f} > allowed {cfg.max_class_imbalance_ratio:.2f}."
                    ),
                    column=cfg.label_col,
                )
            )
    else:
        # 回归任务：简单打一下分布 info
        issues.append(
            SanityIssue(
                severity="info",
                code="label_reg_distribution",
                message=(
                    f"Label {cfg.label_col!r} looks like regression: "
                    f"n_uniq={n_uniq}, mean={float(non_null_values.mean()):.4f}, "
                    f"std={float(non_null_values.std()):.4f}."
                ),
                column=cfg.label_col,
            )
        )

    return issues
```

### 3. 在 `run_dataset_sanity_checks` 里挂上 label 检查

假设你之前有类似函数：

```python
def run_dataset_sanity_checks(df: pd.DataFrame, cfg: SanityConfig) -> list[SanityIssue]:
    issues: list[SanityIssue] = []

    # 之前已有：空值、无限值、零方差特征、重复列、时间顺序等…

    # 最后加上标签检查
    issues.extend(check_label_column(df, cfg))

    return issues
```

> 这样，每次 PI / HPO 拿到 `dataset_df` 后，都可以得到一份包含 **标签健康状况** 的报告：
>
> * 样本是否足够
> * 是否全是 NaN / 常数
> * 分类是否极度不平衡

后面我们会把这些 issue 提炼成 **自然语言摘要 + JSON 结构** 交给 AI 大脑，让它学会拒绝垃圾数据。

---

## 二、ClickHouse 侧的「轻量体检」层

目的：
**在拉全量 DataFrame 之前**，先在 ClickHouse 上做一轮轻量检查：

* 有多少行
* 基本统计（min/max/mean/std/null ratio）
  -（可选）标签相关统计（如果 label 基于 close 之类，暂时只看特征）

### 1. 新模块：`dataset_sanity/clickhouse_stats.py`

```python
# dataset_sanity/clickhouse_stats.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

import clickhouse_connect
import pandas as pd


@dataclass
class ColumnStats:
    name: str
    nulls: int
    null_ratio: float
    uniq: int
    min: float | None
    max: float | None
    mean: float | None
    std: float | None


@dataclass
class TableStats:
    n_rows: int
    cols: Dict[str, ColumnStats]


def build_basic_stats_query(
    table: str,
    *,
    symbol: Optional[str],
    ts_col: str,
    start_ts: str,
    end_ts: str,
    numeric_cols: Iterable[str],
) -> str:
    """
    在 ClickHouse 端生成一条聚合查询，统计基础信息。
    只针对基础列（不含派生表达式），以减少复杂度。
    """
    where_clauses = [f"{ts_col} >= toDateTime64('{start_ts}', 3)", f"{ts_col} < toDateTime64('{end_ts}', 3)"]
    if symbol is not None:
        where_clauses.append(f"symbol = '{symbol}'")

    where_sql = " AND ".join(where_clauses)

    parts = ["COUNT() AS n_rows"]
    for col in numeric_cols:
        safe = col  # TODO: 如果有特殊字符可加反引号
        parts.extend(
            [
                f"countIf(isNull({safe})) AS `{col}__nulls`",
                f"uniqExact({safe}) AS `{col}__uniq`",
                f"min({safe}) AS `{col}__min`",
                f"max({safe}) AS `{col}__max`",
                f"avg({safe}) AS `{col}__mean`",
                f"stddevPop({safe}) AS `{col}__std`",
            ]
        )

    select_sql = ",\n  ".join(parts)

    q = f"""
    SELECT
      {select_sql}
    FROM {table}
    WHERE {where_sql}
    """
    return q.strip()
```

### 2. 运行并解析结果：`collect_basic_stats`

```python
def collect_basic_stats(
    client: clickhouse_connect.driver.Client,
    table: str,
    *,
    symbol: Optional[str],
    ts_col: str,
    start_ts: str,
    end_ts: str,
    numeric_cols: Iterable[str],
) -> TableStats:
    """
    在 ClickHouse 端执行基础体检查询。
    """
    q = build_basic_stats_query(
        table=table,
        symbol=symbol,
        ts_col=ts_col,
        start_ts=start_ts,
        end_ts=end_ts,
        numeric_cols=numeric_cols,
    )
    df = client.query_df(q)
    if df.empty:
        return TableStats(n_rows=0, cols={})

    row = df.iloc[0]
    n_rows = int(row["n_rows"])

    cols_stats: Dict[str, ColumnStats] = {}
    for col in numeric_cols:
        nulls = int(row.get(f"{col}__nulls", 0))
        uniq = int(row.get(f"{col}__uniq", 0))
        col_min = row.get(f"{col}__min")
        col_max = row.get(f"{col}__max")
        mean = row.get(f"{col}__mean")
        std = row.get(f"{col}__std")

        null_ratio = nulls / max(1, n_rows)

        cols_stats[col] = ColumnStats(
            name=col,
            nulls=nulls,
            null_ratio=null_ratio,
            uniq=uniq,
            min=float(col_min) if col_min is not None else None,
            max=float(col_max) if col_max is not None else None,
            mean=float(mean) if mean is not None else None,
            std=float(std) if std is not None else None,
        )

    return TableStats(n_rows=n_rows, cols=cols_stats)
```

### 3. 基于 TableStats 做一个「预体检」

```python
def quick_table_sanity_from_stats(
    stats: TableStats,
    *,
    min_rows: int = 500,
    max_null_ratio: float = 0.5,
    min_std_threshold: float = 1e-9,
) -> List[Dict[str, Any]]:
    """
    输出简单的 issue 列表（用 dict，便于和你现有 SanityIssue 结构拼接）。
    """
    issues: List[Dict[str, Any]] = []

    if stats.n_rows == 0:
        issues.append(
            {
                "severity": "error",
                "code": "ch_zero_rows",
                "message": "ClickHouse stats: filtered dataset has zero rows.",
            }
        )
        return issues

    if stats.n_rows < min_rows:
        issues.append(
            {
                "severity": "warn",
                "code": "ch_too_few_rows",
                "message": f"ClickHouse stats: n_rows={stats.n_rows} < min_rows={min_rows}.",
                "extra": {"n_rows": stats.n_rows, "min_rows": min_rows},
            }
        )

    for col, cs in stats.cols.items():
        if cs.null_ratio > max_null_ratio:
            issues.append(
                {
                    "severity": "warn",
                    "code": "ch_high_null_ratio",
                    "message": (
                        f"Column {col!r} null_ratio={cs.null_ratio:.2%} > "
                        f"allowed {max_null_ratio:.2%} (ClickHouse stats)."
                    ),
                    "column": col,
                    "extra": {"null_ratio": cs.null_ratio},
                }
            )

        # 零方差 / 非变化列
        if cs.std is not None and cs.std < min_std_threshold:
            issues.append(
                {
                    "severity": "warn",
                    "code": "ch_near_zero_var",
                    "message": (
                        f"Column {col!r} std={cs.std:.3e} (≈0), "
                        "may be near-constant (ClickHouse stats)."
                    ),
                    "column": col,
                    "extra": {"std": cs.std},
                }
            )

    return issues
```

> 注意：这里我只做了 **非常轻** 的检查：
>
> * 行数
> * Null 比例
> * 方差近 0
>   更重的相关性 / label 相关检查，我们仍然放在 Python 的采样层。

---

## 三、把 ClickHouse 预体检 + DatasetSanity + Label 合起来（串到 PI）

现在我们来改造 `PrincipalInvestigator.propose` 的一部分流程，让它变成：

1. **先**用 ClickHouse 做一轮 TableStats（不取全量数据）；
2. 如果 n_rows 够、没有致命错误，再跑真正的 `SELECT` 拿 DataFrame；
3. 在 Python 层跑 `run_dataset_sanity_checks`（包含特征 + 标签检查）；
4. 若有严重错误（severity="error"），直接抛异常，HPO 不再浪费算力；
5. 把 sanity_report 记录到 HPO / 实验追踪中（这部分可在 Phase 4A 里用）。

### 1. 在 PI 里调用 ClickHouse 预检查

伪代码式补丁（只展示关键逻辑）：

```python
from dataset_sanity.clickhouse_stats import collect_basic_stats, quick_table_sanity_from_stats
from dataset_sanity.core import SanityConfig, run_dataset_sanity_checks, SanityIssue

class PrincipalInvestigator:
    ...

    def propose(...):
        catalog = _fetch_catalog(self._client, table)
        ...
        # === Label Contract & Sandbox 特征 已在上一轮补丁里实现 ===
        label_id = tplan.get("label_id")
        ...
        label_spec = self._label_registry.get(label_id)

        # 1) ClickHouse 预体检（只看基础列）
        # 这里我们可以简单地认为 numeric_cols = recipe["base_features"] + label 所需的列
        base_features = recipe.get("base_features", [])
        label_base_cols = {label_spec.price_col}
        if label_spec.high_col:
            label_base_cols.add(label_spec.high_col)
        if label_spec.low_col:
            label_base_cols.add(label_spec.low_col)

        numeric_cols = sorted(set(base_features) | label_base_cols)

        table_stats = collect_basic_stats(
            self._client,
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            ts_col=tplan.get("ts_col", "ts"),
            start_ts=start_ts,
            end_ts=end_ts,
            numeric_cols=numeric_cols,
        )
        ch_issues = quick_table_sanity_from_stats(table_stats)

        # 如果 ClickHouse 预检查已经发现致命错误，直接失败
        fatal_ch_issues = [i for i in ch_issues if i["severity"] == "error"]
        if fatal_ch_issues:
            # 你可以自定义一个异常类型，或者直接 ValueError
            raise ValueError(
                f"ClickHouse sanity check failed: {fatal_ch_issues}"
            )

        # 2) 真正构造特征 SQL（无标签）
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

        # 3) 在 Python 层添加标签列
        df = add_label_column(
            df,
            spec=label_spec,
            bar_seconds=self._bar_seconds,
            col_name=tplan.get("target_col", "y"),
        )

        # 4) DatasetSanity（含标签检查）
        sanity_cfg = SanityConfig(label_col=tplan.get("target_col", "y"))
        issues = run_dataset_sanity_checks(df, sanity_cfg)

        fatal_issues = [i for i in issues if i.severity == "error"]
        if fatal_issues:
            raise ValueError(f"DatasetSanity failed with fatal issues: {fatal_issues}")

        # TODO: 在 Phase 4A 里把 issues + ch_issues 存到 ExperimentRecord.sanity_report 里

        # 5) 正常返回
        ...
        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
        )
```

> 至此，训练前的流程就是：
>
> * 「ClickHouse 快速体检」 ➜ 大问题提前发现
> * 「Python DatasetSanity + 标签体检」 ➜ 切实过滤坏数据 / 坏标签
> * 才把 `dataset_df` 交给 HPO / 模型训练。

---

## 四、采样策略（补一句）

为了防止 DataFrame 级别的 DatasetSanity 太慢，我们可以给原有的 `run_dataset_sanity_checks` 增加一个简单采样参数，比如：

```python
@dataclass
class SanityConfig:
    ...
    max_rows_full_scan: int = 200_000   # 超过就只采样
    sample_rows: int = 50_000           # 用于复杂检查（相关性等）
```

然后在 `run_dataset_sanity_checks` 的最开始：

```python
def run_dataset_sanity_checks(df: pd.DataFrame, cfg: SanityConfig) -> List[SanityIssue]:
    issues: List[SanityIssue] = []

    n = len(df)
    if n > cfg.max_rows_full_scan:
        # 仅采样一部分做重检查
        df_sample = df.sample(n=min(cfg.sample_rows, n), random_state=42)
    else:
        df_sample = df

    # 后续复杂检查（相关性、分布）都用 df_sample
    # 简单检查（行数、是否有 NaN/inf）可以对 full df 做
    ...
```

这样：

* ClickHouse 层负责 **整体规模 & 基本统计**；
* Python 层负责对一个合理规模的 sample 做深度检查；
* 性能不会炸，也不会因为数据太大而无法体检。

---

## 五、接下来可以做什么？

这一轮我们把：

* 标签契约（Label Contract）
* 标签体检（Label-aware DatasetSanity）
* ClickHouse 预体检（性能 + 防守）

全部串进了 **PI ➜ 数据集构造前置流程**。

**下一步**（如果你继续说“继续”😂），我会：

1. 把这些 `issues` + `table_stats` 变成一份

   * 简短、结构化的 JSON（给 Phase 4A 的 ExperimentRecord 用）
   * 简短的自然语言摘要（给 AI 大脑 prompt 用，告诉它：这次数据质量如何，它应该更保守还是更激进）。

2. 再往上连到 **Brain 的决策 schema**：

   * 如果 Sanity 报告里有严重 warning，例如「label 极度不平衡」，AI 大脑在 JSON 决策里必须写出 `reason`/`risk_flags`，甚至可以自动降权或拒绝执行。

你现在可以先让 Claude 把这轮代码落地，然后我再继续往“**让大脑看体检报告之后再决策**”这层走。
