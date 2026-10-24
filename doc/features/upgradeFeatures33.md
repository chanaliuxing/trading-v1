好，我们现在按计划进入 **Sandbox 特征机制**，让 AI 在“安全围栏”里自己发明新特征，并且能被追踪、复用。

接下来这轮我做三件事：

1. 定义一个 **Sandbox 特征 DSL + 校验器模块**（独立文件，便于单测）
2. 把它 **接到 PrincipalInvestigator** 上，让 LLM 产出的 `derived` 特征先过“安检”，合格的才进 SQL
3. 给出一个简单的 **ExperimentRegistry 记录点**，把这次使用到的 sandbox 特征记到实验里（方便以后追踪）

---

## 1️⃣ Sandbox 特征模块：`pi/feature_sandbox.py`

> 目标：允许 LLM 写表达式，但只允许 **安全子集**：
>
> * 只能用给定的列名 + 常数
> * 只能用白名单函数（如 `abs/log/exp/least/greatest/if`）
> * 禁止 `select/join/union/now()` 这类危险东西
> * 禁止引用 label/target 列（防止泄露）

新文件：`pi/feature_sandbox.py`：

```python
# pi/feature_sandbox.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import re


# ─────────────────────────────────────────────────────────────
# Dataclasses
# ─────────────────────────────────────────────────────────────

@dataclass
class SandboxFeature:
    alias: str
    expr: str
    reason_rejected: Optional[str] = None


@dataclass
class SandboxGuardConfig:
    allowed_functions: List[str]
    forbidden_tokens: List[str]
    forbidden_columns: List[str]
    max_expr_length: int = 256


# ─────────────────────────────────────────────────────────────
# Default guard config
# ─────────────────────────────────────────────────────────────

DEFAULT_GUARD = SandboxGuardConfig(
    allowed_functions=[
        "abs",
        "log",
        "exp",
        "sqrt",
        "greatest",
        "least",
        "if",
        "sign",
    ],
    forbidden_tokens=[
        "select",
        "from",
        "where",
        "join",
        "union",
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "truncate",
        "into",
        "table",
        "view",
        "database",
        "system",
        "now",
        "today",
        "yesterday",
        "rand",
        "sleep",
        "dictGet",
        "url",
    ],
    forbidden_columns=[
        "target",
        "label",
        "y",
        "ret_10m_fwd",
        "ret_5m_fwd",
        "ret_30m_fwd",
    ],
    max_expr_length=256,
)


# ─────────────────────────────────────────────────────────────
# Tokenizer / validator
# ─────────────────────────────────────────────────────────────

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUM_RE = re.compile(r"^[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?$")


def _simple_tokenize(expr: str) -> List[str]:
    """
    很粗糙的 tokenizer：
    - 把标识符 / 数字 / 操作符 分开
    - 我们不做完整 SQL 解析，只做安全检查
    """
    # 把各种括号和运算符两边加空格
    spaced = (
        expr.replace("(", " ( ")
        .replace(")", " ) ")
        .replace("+", " + ")
        .replace("-", " - ")
        .replace("*", " * ")
        .replace("/", " / ")
        .replace(",", " , ")
    )
    tokens = [tok for tok in spaced.strip().split() if tok]
    return tokens


def _is_number(tok: str) -> bool:
    return bool(_NUM_RE.match(tok))


def validate_sandbox_expr(
    *,
    expr: str,
    feature_catalog: List[str],
    guard: SandboxGuardConfig = DEFAULT_GUARD,
) -> Tuple[bool, str]:
    """
    对 LLM 提议的 expr 做安全检查。

    返回: (is_ok, reason_or_clean_expr)
    - 如果 is_ok == True: 第二个值是「清洗后 expr」
    - 如果 is_ok == False: 第二个值是「拒绝原因」
    """
    expr_strip = expr.strip()
    if not expr_strip:
        return False, "expression is empty"

    if len(expr_strip) > guard.max_expr_length:
        return False, f"expression too long (>{guard.max_expr_length})"

    low = expr_strip.lower()

    # 1) 禁止关键 token（避免 SQL 注入 / DDL / DML）
    for bad in guard.forbidden_tokens:
        if bad in low:
            return False, f"forbidden token: {bad}"

    tokens = _simple_tokenize(expr_strip)

    # 2) 检查标识符：只能是列名 or 函数名 or 数字 or操作符
    allowed_cols_set = set(feature_catalog)
    forbidden_cols_set = set(guard.forbidden_columns)
    allowed_funcs_set = set(guard.allowed_functions)

    cleaned_tokens: List[str] = []

    for tok in tokens:
        # 数字
        if _is_number(tok):
            cleaned_tokens.append(tok)
            continue

        # 操作符 / 括号 / 逗号
        if tok in {"+", "-", "*", "/", "(", ")", ","}:
            cleaned_tokens.append(tok)
            continue

        # 标识符（列名 or 函数名）
        if _IDENT_RE.match(tok):
            low_tok = tok.lower()

            # 函数调用的处理，我们在这里只是允许函数名出现在 tokens 里
            if low_tok in allowed_funcs_set:
                cleaned_tokens.append(tok)
                continue

            # 列名：必须在 feature_catalog 里
            if tok in allowed_cols_set:
                if tok in forbidden_cols_set:
                    return False, f"forbidden column reference: {tok}"
                cleaned_tokens.append(tok)
                continue

            # 标识符既不是函数也不是已知列 => 拒绝
            return False, f"unknown identifier: {tok}"

        # 其他奇怪的符号 => 拒绝
        return False, f"unsupported token: {tok}"

    # 如果全部 token 合法，就用空格 join 回去（保守一点）
    clean_expr = " ".join(cleaned_tokens)
    return True, clean_expr


def filter_sandbox_features(
    raw_features: List[Dict[str, Any]],
    feature_catalog: List[str],
    guard_cfg_override: Optional[Dict[str, Any]] = None,
) -> Tuple[List[SandboxFeature], List[SandboxFeature]]:
    """
    接收 LLM 给的 "derived" 列表，形式类似：
      [{"alias": "close_to_vwap", "expr": "(close - vwap) / vwap"}, ...]

    返回:
      (accepted, rejected)
    """
    guard = DEFAULT_GUARD
    if guard_cfg_override:
        # 简单的覆盖方式；你也可以换成 dataclass.replace
        guard = SandboxGuardConfig(
            allowed_functions=guard_cfg_override.get("allowed_functions", guard.allowed_functions),
            forbidden_tokens=guard_cfg_override.get("forbidden_tokens", guard.forbidden_tokens),
            forbidden_columns=guard_cfg_override.get("forbidden_columns", guard.forbidden_columns),
            max_expr_length=guard_cfg_override.get("max_expr_length", guard.max_expr_length),
        )

    accepted: List[SandboxFeature] = []
    rejected: List[SandboxFeature] = []

    for item in raw_features or []:
        alias = (item or {}).get("alias")
        expr = (item or {}).get("expr")
        if not alias or not expr:
            rejected.append(
                SandboxFeature(alias=alias or "<missing>", expr=str(expr), reason_rejected="missing alias or expr")
            )
            continue

        ok, res = validate_sandbox_expr(expr=expr, feature_catalog=feature_catalog, guard=guard)
        if ok:
            accepted.append(SandboxFeature(alias=alias, expr=res))
        else:
            rejected.append(SandboxFeature(alias=alias, expr=expr, reason_rejected=res))

    return accepted, rejected
```

👉 这个模块做了几件核心事：

* 提供一个简单 DSL 验证：**只允许**列名 + 数字 + `+ - * / () ,` + 白名单函数
* 所有不认识的标识符一律拒绝
* 所有危险 token 一律拒绝
* 返回 2 组：`accepted` / `rejected`，方便记录到实验/日志里

---

## 2️⃣ 把 Sandbox 接进 PrincipalInvestigator

现在修改你之前的 `PrincipalInvestigator.propose`，让它：

1. 从 LLM 产出的 `plan` 里拿到 `feature_recipe["derived"]`
2. 调用 `filter_sandbox_features` 做校验
3. 把通过的 derived 特征交给 `build_select_sql` 生成 SQL

> 下面示意的是对你原来的 `PrincipalInvestigator` 的 **增量改造**：

```python
# pi/principal_investigator.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql
from pi.feature_sandbox import filter_sandbox_features, SandboxFeature


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
    sandbox_features_accepted: List[SandboxFeature]
    sandbox_features_rejected: List[SandboxFeature]


def _fetch_catalog(client, table: str) -> List[str]:
    q = "SELECT name FROM system.columns WHERE table = {table:String}"
    df = client.query_df(q, parameters={"table": table})
    return sorted(df["name"].tolist())


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
            f"预测窗口: {horizon}\n\n"
            "请返回 JSON，结构示例：\n"
            "{\n"
            '  "feature_recipe": {\n'
            '    "use_table": "bars_1m",\n'
            '    "base_features": ["close", "vwap", "volume"],\n'
            '    "derived": [\n'
            '      {"alias": "close_to_vwap", "expr": "(close - vwap) / vwap"},\n'
            '      {"alias": "log_volume", "expr": "log(volume + 1)"}\n'
            "    ]\n"
            "  },\n"
            '  "training_plan": {\n'
            '    "task": "reg",\n'
            '    "target": "ret_10m_fwd",\n'
            '    "decision": {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},\n'
            '    "model_candidates": [\n'
            '      {"model_type": "xgb", "model_params": {"max_depth": 4}},\n'
            '      {"model_type": "lasso", "model_params": {}}\n'
            "    ]\n"
            "  }\n"
            "}"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
            " 其中 feature_recipe.derived 里的 expr 必须只使用给定特征名、数字常数、"
            "加减乘除和简单函数（abs/log/exp/sqrt/greatest/least/if）。禁止使用 target/label 列和 SQL 语句。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # ─────────────────────────────────────────────────
        # 1) 处理 derived sandbox 特征
        # ─────────────────────────────────────────────────
        raw_derived = recipe.get("derived", []) or []
        sandbox_accepted, sandbox_rejected = filter_sandbox_features(
            raw_features=raw_derived,
            feature_catalog=catalog,
            guard_cfg_override=(guard_cfg or {}).get("sandbox_guard"),
        )

        # 把“通过的”重写回 recipe，expr 用清洗后的
        recipe["derived"] = [{"alias": f.alias, "expr": f.expr} for f in sandbox_accepted]

        # ─────────────────────────────────────────────────
        # 2) 生成 SQL（由 build_select_sql 实际拼 SELECT）
        #    注意：这里假设 build_select_sql 能理解 recipe["derived"]
        # ─────────────────────────────────────────────────
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

        # ─────────────────────────────────────────────────
        # 3) 生成 HPO grid（原逻辑不变）
        # ─────────────────────────────────────────────────
        grid = {
            "model_type": [],
            "feature_subset": [
                recipe.get("base_features", [])
                + [d.get("alias") for d in recipe.get("derived", [])]
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
            "allow_params": ["model_type", "feature_subset", "task", "decision", "model_params"]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            sandbox_features_accepted=sandbox_accepted,
            sandbox_features_rejected=sandbox_rejected,
        )
```

👉 结果：

* LLM 再也不能随便写 SQL；
* 任何不在 `feature_catalog` 里的东西都会被拒绝；
* 被拒绝的特征，会带 `reason_rejected` 供你在日志/实验里查看；
* 通过的 sandbox 特征统一变成 `SELECT ... <expr> AS <alias>` 进入训练数据。

---

## 3️⃣ 把 Sandbox 特征记入 ExperimentRegistry（示例）

假设你已经有 `ExperimentRegistry`（Claude 那边 4A 已经搞了一部分），我们给一个简单入口：

```python
# experiments/registry_integration.py  示例

from __future__ import annotations

from typing import Any, Dict

from pi.principal_investigator import PIResult
from data.dataset_sanity import DatasetSanityReport


def build_pi_experiment_payload(
    *,
    pi_result: PIResult,
    sanity: DatasetSanityReport,
    extra_meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    把 PIResult + SanityReport + Sandbox 特征，打成一个统一 payload，
    方便 ExperimentRegistry 记录。
    """
    return {
        "feature_recipe": pi_result.feature_recipe,
        "training_plan": pi_result.training_plan,
        "sanity_report": sanity.summary_dict(),
        "sandbox_features_accepted": [
            {
                "alias": f.alias,
                "expr": f.expr,
            }
            for f in pi_result.sandbox_features_accepted
        ],
        "sandbox_features_rejected": [
            {
                "alias": f.alias,
                "expr": f.expr,
                "reason_rejected": f.reason_rejected,
            }
            for f in pi_result.sandbox_features_rejected
        ],
        "meta": extra_meta or {},
    }
```

然后在你 HPO 前面那层 orchestrator 里：

```python
pi_result, sanity = orch.propose_and_load_dataset(...)

payload = build_pi_experiment_payload(
    pi_result=pi_result,
    sanity=sanity,
    extra_meta={"symbol": "TSLA", "horizon": "10m"},
)

run_id = experiment_registry.register_pi_proposal(payload)
# 后面 HPO 的结果再用 run_id 更新 metrics 即可
```

---

## 这一步做到哪？

到目前为止，我们在 **“数据 & 特征 & 标签”这一层** 做了两件非常关键的基础设施升级：

1. **DatasetSanity + ClickHouse 性能优化**

   * 大表体检不会拖垮 HPO
   * 有统一的 `DatasetSanityReport.ok` 作为“硬 gate”

2. **Sandbox 特征机制**

   * AI 不再随便写 SQL，而是在受控 DSL 中“发明”新特征
   * 所有新特征都是「有定义、有记录、可回溯」的
   * 被拒绝的特征也能被你事后复盘（看 LLM 在想什么）

---

### 下一步我建议：

* 要么继续在 **标签层** 做同样的 Contract（比如：统一用 Python 函数计算 `ret_10m_fwd` / Triple-Barrier，而不是让 LLM 直接点 target 列）；
* 要么开始衔接到 **HPO / ExperimentRegistry**：

  * 每次 HPO Trial 都引用 `run_id`，
  * 每个训练结果都知道“是在哪个数据体检、在哪组 Sandbox 特征”下产生的。

你一句“继续”，我就按这个路线接着往下推。
这一步如果你觉得 OK，我们下一轮就把 **标签 Contract + Label 生成模块** 落地掉。
好，我们按之前的路线，把 **“标签 Contract + 生成模块”** 落地完，再把它接回到 **PI → HPO → ExperimentRegistry** 这条链上。

下面这轮我做三件事：

1. 定义 **Label Contract + Registry**（白名单标签定义）
2. 实现 **Label Generator**（用代码算 label，而不是让 LLM 写 SQL）
3. 把 Label 接回 **PrincipalInvestigator / HPO / ExperimentRegistry**

---

## 1️⃣ Label Contract & Registry：`labels/contracts.py`

目标：

* 所有 label **必须从白名单里选**（id → 规则），而不是 LLM 现编 SQL。
* 每个 label 定义清楚：

  * 类型（回归 / 分类）
  * Horizon（多少 bar）
  * 用哪个生成器（forward_return / triple_barrier 等）
  * 需要哪些基础列

**文件：`labels/contracts.py`**

```python
# labels/contracts.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional


LabelType = Literal["regression", "classification"]


@dataclass
class LabelSpec:
    """
    统一的标签定义 Contract：

    - id: 对外暴露的 label_id（供 AI 大脑 / HPO / 回测引用）
    - type: 回归 or 分类
    - generator: 使用哪种算法生成（forward_return / triple_barrier 等）
    - params: 生成器的参数（如 horizon_bars, upper_mult, lower_mult）
    - required_columns: 生成该 label 必须具备的基础列（避免 target 泄露 / 列缺失）
    - description: 人类可读说明，方便你后面做文档 / UI
    """
    id: str
    type: LabelType
    generator: str
    params: Dict[str, Any]
    required_columns: List[str]
    description: str


# ─────────────────────────────────────────────────────────────
# 内置标签白名单
# ─────────────────────────────────────────────────────────────

# 1) 简单前向收益率：ret_10m_fwd
RET_10M_FWD = LabelSpec(
    id="ret_10m_fwd",
    type="regression",
    generator="forward_return",
    params={
        "horizon_bars": 10,
        "price_col": "close",
    },
    required_columns=["close"],
    description="10 bar forward return based on close price, (close[t+10]/close[t] - 1).",
)

# 2) 5 bar 前向收益率：ret_5m_fwd（示例）
RET_5M_FWD = LabelSpec(
    id="ret_5m_fwd",
    type="regression",
    generator="forward_return",
    params={
        "horizon_bars": 5,
        "price_col": "close",
    },
    required_columns=["close"],
    description="5 bar forward return based on close price.",
)

# 3) Triple Barrier：10 bar 内 ±1ATR（示例，先用固定阈值）
TRIPLE_BARRIER_10B_1ATR = LabelSpec(
    id="tb_10b_1atr",
    type="classification",
    generator="triple_barrier",
    params={
        "horizon_bars": 10,
        "upper_mult": 1.0,   # 上轨=close * (1 + 1*atr_perc) 这里只先简化，用百分比/固定值都可以
        "lower_mult": 1.0,   # 下轨=close * (1 - 1*atr_perc)
        "price_col": "close",
        "high_col": "high",
        "low_col": "low",
        # 这里为了简单可以用固定百分比上/下轨；或者你后面接入 ATR 列就把 required_columns 加上 "atr_14"
    },
    required_columns=["close", "high", "low"],
    description=(
        "Triple Barrier: within 10 bars, label=1 if price hits upper barrier first, "
        "label=-1 if hits lower barrier first, else 0."
    ),
)

# 统一注册表
_LABEL_REGISTRY: Dict[str, LabelSpec] = {
    RET_10M_FWD.id: RET_10M_FWD,
    RET_5M_FWD.id: RET_5M_FWD,
    TRIPLE_BARRIER_10B_1ATR.id: TRIPLE_BARRIER_10B_1ATR,
}


def get_label_spec(label_id: str) -> LabelSpec:
    try:
        return _LABEL_REGISTRY[label_id]
    except KeyError:
        raise KeyError(f"Unknown label_id={label_id!r}. Allowed: {list(_LABEL_REGISTRY.keys())}")


def list_label_specs() -> List[LabelSpec]:
    return list(_LABEL_REGISTRY.values())


def label_ids() -> List[str]:
    return list(_LABEL_REGISTRY.keys())
```

这样：

* LLM / AI 大脑 **只被允许输出 label_id 字符串**（比如 `"ret_10m_fwd"`）。
* 真正的 label 公式，只在 **这个模块**里定义，安全、可审计、可版本化。

---

## 2️⃣ Label Generator：`labels/generator.py`

> 目标：
>
> * 给一个 `df`（特征数据）+ 一个 `LabelSpec`，返回带 label 列的新 df + 报告
> * 所有 label 计算都在 Python 里完成，不给 LLM 写 SQL 的机会
> * 至少支持：
>
>   * forward return
>   * triple-barrier（简化实现，但逻辑完整）

**文件：`labels/generator.py`**

```python
# labels/generator.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from labels.contracts import LabelSpec


@dataclass
class LabelReport:
    label_id: str
    col_name: str
    type: str
    num_rows: int
    num_nan: int
    frac_nan: float
    class_counts: Dict[Any, int] | None = None  # 仅分类任务使用

    def summary_dict(self) -> Dict[str, Any]:
        return {
            "label_id": self.label_id,
            "col_name": self.col_name,
            "type": self.type,
            "num_rows": self.num_rows,
            "num_nan": self.num_nan,
            "frac_nan": self.frac_nan,
            "class_counts": self.class_counts,
        }


# ─────────────────────────────────────────────────────────────
# Forward Return 生成器
# ─────────────────────────────────────────────────────────────

def _generate_forward_return(df: pd.DataFrame, spec: LabelSpec, col_name: str) -> pd.Series:
    params = spec.params
    horizon = int(params.get("horizon_bars", 1))
    price_col = params.get("price_col", "close")

    if price_col not in df.columns:
        raise KeyError(f"Required price_col={price_col!r} not in DataFrame columns")

    price = df[price_col].astype(float)
    # forward return: (P_{t+h} / P_t - 1)
    fwd_price = price.shift(-horizon)
    ret = fwd_price / price - 1.0
    return ret.rename(col_name)


# ─────────────────────────────────────────────────────────────
# Triple Barrier 生成器（简化版）
# ─────────────────────────────────────────────────────────────

def _generate_triple_barrier(df: pd.DataFrame, spec: LabelSpec, col_name: str) -> pd.Series:
    params = spec.params
    horizon = int(params.get("horizon_bars", 10))
    price_col = params.get("price_col", "close")
    high_col = params.get("high_col", "high")
    low_col = params.get("low_col", "low")

    for col in [price_col, high_col, low_col]:
        if col not in df.columns:
            raise KeyError(f"Required column {col!r} not in DataFrame for triple_barrier")

    price = df[price_col].astype(float).to_numpy()
    high = df[high_col].astype(float).to_numpy()
    low = df[low_col].astype(float).to_numpy()

    # 暂时用一个固定百分比上下轨（可以改为 ATR*mult）
    # 这里取 mult=1.0，表示 ±1%（你后面可以把 mult 换成 atr-based）
    upper_mult = float(params.get("upper_mult", 1.0)) / 100.0
    lower_mult = float(params.get("lower_mult", 1.0)) / 100.0

    n = len(price)
    labels = np.zeros(n, dtype=float)

    for i in range(n):
        start_price = price[i]
        if np.isnan(start_price):
            labels[i] = np.nan
            continue

        upper = start_price * (1.0 + upper_mult)
        lower = start_price * (1.0 - lower_mult)

        # 向前看 horizon bars
        end_idx = min(n, i + horizon + 1)
        hit = 0  # 1 / -1 / 0

        for j in range(i + 1, end_idx):
            if j >= n:
                break
            if high[j] >= upper:
                hit = 1
                break
            if low[j] <= lower:
                hit = -1
                break

        labels[i] = hit

    return pd.Series(labels, index=df.index, name=col_name)


# ─────────────────────────────────────────────────────────────
# 主入口：生成 label 列并返回 report
# ─────────────────────────────────────────────────────────────

def generate_label_column(
    df: pd.DataFrame,
    spec: LabelSpec,
    col_name: str | None = None,
) -> Tuple[pd.DataFrame, LabelReport]:
    """
    根据 LabelSpec 在 df 上生成一个 label 列。

    返回:
      - 新的 df（包含 label 列）
      - LabelReport（统计信息）
    """
    col_name = col_name or spec.id

    # 1) 校验 required_columns
    missing = [c for c in spec.required_columns if c not in df.columns]
    if missing:
        raise KeyError(f"Label {spec.id!r} missing required columns: {missing}")

    # 2) 调度不同生成器
    if spec.generator == "forward_return":
        label_series = _generate_forward_return(df, spec, col_name)
    elif spec.generator == "triple_barrier":
        label_series = _generate_triple_barrier(df, spec, col_name)
    else:
        raise ValueError(f"Unsupported label generator: {spec.generator!r} for label_id={spec.id!r}")

    # 3) 统计信息
    num_rows = len(label_series)
    num_nan = int(label_series.isna().sum())
    frac_nan = float(num_nan) / num_rows if num_rows > 0 else 0.0

    class_counts = None
    if spec.type == "classification":
        # 注意：排除 NaN
        vc = label_series.dropna().value_counts().to_dict()
        class_counts = {int(k) if isinstance(k, (int, np.integer, float)) and k == int(k) else k: int(v) for k, v in vc.items()}

    report = LabelReport(
        label_id=spec.id,
        col_name=col_name,
        type=spec.type,
        num_rows=num_rows,
        num_nan=num_nan,
        frac_nan=frac_nan,
        class_counts=class_counts,
    )

    df_new = df.copy()
    df_new[col_name] = label_series

    return df_new, report
```

> 注意：这里 triple-barrier 先用 **“±固定百分比”** 简版，后面你可以把 `upper_mult/lower_mult` 换成基于 ATR 的门；Contract 层已经留好 `params`。

---

## 3️⃣ 把 Label 接回 PI → HPO → ExperimentRegistry

### 3.1 在 PI 层：禁掉“自由 target SQL”，只允许 label_id

现在给 PrincipalInvestigator 加一个明确的约束：

* LLM 的 `training_plan` 不再写 “`target`: SQL 表达式”，而是写 “`label_id`: 'ret_10m_fwd'”
* 真实的 target 列由 Label Generator 生成
* HPO 只知道：

  * `label_id`（实验可追踪）
  * 训练用的列名，例如 `'target'` 或 `'ret_10m_fwd'`

你可以这样改 `PrincipalInvestigator.propose` 的返回，把 label 相关信息加进去，但 **不在这里生成 label**（PI 只负责：recipe + plan + 数据 + HPO grid）：

```python
# 在 PIResult 里加上 label_id 字段（可选）
@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
    sandbox_features_accepted: List[SandboxFeature]
    sandbox_features_rejected: List[SandboxFeature]
    label_id: str | None = None  # 新增

# propose 里，在 plan 解析后：
label_id = tplan.get("label_id", "ret_10m_fwd")  # 给个默认

return PIResult(
    feature_recipe=recipe,
    training_plan=tplan,
    sql=sql,
    dataset_df=df,
    hpo_grid=grid_sane,
    sandbox_features_accepted=sandbox_accepted,
    sandbox_features_rejected=sandbox_rejected,
    label_id=label_id,
)
```

> 系统提示要同步改一下：
> 让 LLM 在 `training_plan` 里输出 `"label_id": "ret_10m_fwd"`，而不是 `"target": "ret_10m_fwd"`。

---

### 3.2 在 Orchestrator / HPO 前一层：统一算 label

假设你有一个 orchestrator 类似：

```python
# pi_orchestrator.py 之类

from labels.contracts import get_label_spec
from labels.generator import generate_label_column
from data.dataset_sanity import run_dataset_sanity_checks  # 之前我们实现的体检模块


def prepare_dataset_for_hpo(pi_result: PIResult):
    """
    从 PIResult 出发，做:
      1. 数据体检
      2. 生成 label 列
      3. 再做一次带 label 的体检（可选）
      4. 返回最终可用于 HPO 的 df + report
    """
    df = pi_result.dataset_df

    # 1) 对 features 做 DatasetSanity（不含 label）
    sanity_features = run_dataset_sanity_checks(df, target_column=None)

    if not sanity_features.ok:
        raise RuntimeError(f"Dataset feature sanity check failed: {sanity_features.summary_str()}")

    # 2) Label Contract：根据 label_id 生成标签列
    label_id = pi_result.label_id or "ret_10m_fwd"
    spec = get_label_spec(label_id)
    df_with_label, label_report = generate_label_column(df, spec, col_name=label_id)

    # 3) 可选：对 features+label 一起再体检一次（比如检查 label 是否全是 0 / 单边）
    sanity_with_label = run_dataset_sanity_checks(df_with_label, target_column=label_id)

    if not sanity_with_label.ok:
        raise RuntimeError(
            f"Dataset sanity with label failed: {sanity_with_label.summary_str()} "
            f"label_report={label_report.summary_dict()}"
        )

    return df_with_label, sanity_features, label_report, sanity_with_label
```

HPO 那层就可以统一拿：

```python
train_df, sanity_features, label_report, sanity_with_label = prepare_dataset_for_hpo(pi_result)

target_col = pi_result.label_id  # 比如 "ret_10m_fwd"
feature_cols = pi_result.hpo_grid["feature_subset"][0]  # 之前 grid 里的组合

# 把这些信息传给 HPO / ExperimentRegistry
```

---

### 3.3 在 ExperimentRegistry 记录 label 信息

之前我们在 `build_pi_experiment_payload` 里已经写了 Sandbox 部分，现在补上 label：

```python
# experiments/registry_integration.py (扩展)

from labels.contracts import LabelSpec
from labels.generator import LabelReport


def build_pi_experiment_payload(
    *,
    pi_result: PIResult,
    sanity_features: DatasetSanityReport,
    label_spec: LabelSpec,
    label_report: LabelReport,
    sanity_with_label: DatasetSanityReport,
    extra_meta: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    return {
        "feature_recipe": pi_result.feature_recipe,
        "training_plan": pi_result.training_plan,
        "label": {
            "label_id": label_spec.id,
            "type": label_spec.type,
            "generator": label_spec.generator,
            "params": label_spec.params,
            "required_columns": label_spec.required_columns,
            "description": label_spec.description,
            "report": label_report.summary_dict(),
        },
        "sanity_features": sanity_features.summary_dict(),
        "sanity_with_label": sanity_with_label.summary_dict(),
        "sandbox_features_accepted": [
            {"alias": f.alias, "expr": f.expr}
            for f in pi_result.sandbox_features_accepted
        ],
        "sandbox_features_rejected": [
            {"alias": f.alias, "expr": f.expr, "reason_rejected": f.reason_rejected}
            for f in pi_result.sandbox_features_rejected
        ],
        "meta": extra_meta or {},
    }
```

这样：

* 每次 PI+HPO 产生的实验，都在 registry 里留下完整“签名”：

  * 用了哪些特征（base + sandbox）
  * 用了哪个 label_id（`ret_10m_fwd` / `tb_10b_1atr` …）
  * label 的分布情况（class_counts、nan 比例）
  * 数据是否通过 sanity

后面 Phase 4 让 **AI 大脑挑策略** 时，它就可以看到：
“这个 run_id 用的是哪个 label，数据质量如何。”

---

## ✅ 到目前为止三层的进展（针对你刚才的三层结构）

1. **数据 & 特征 & 标签层**

   * ✅ DatasetSanity 模块（含 ClickHouse 优化思路）
   * ✅ Sandbox 特征机制（受控 DSL + 安全检查 + 接入 PI）
   * ✅ Label Contract + Label Generator（forward_return + triple_barrier）
   * ✅ Label 体检 + 与 DatasetSanity 的集成

2. **机器学习 / HPO / 实验治理层**

   * ✅ HPO 之前的输入已经标准化（feature_subset + label_id + sanity）
   * ✅ ExperimentRegistry 可以记录：feature_recipe / sandbox / label / sanity
   * ⏳ HPO 写 ExperimentRecord 的部分你那边已经让 Claude 在做 4A，我这里是兼容的

3. **AI 大脑 & Prompt 安全层**

   * ✅ PI + LLM 这一侧已经被限制为：

     * 只能在 **sandbox DSL** 内造新特征
     * 只能选择 **白名单 label_id**
   * ⏳ 下一步会在 Brain Prompt 里加入：

     * 只能从 `allowed_run_ids` 里选策略
     * 只能在 `label_ids` 白名单内选标签

---

如果你觉得这一轮 OK，
**下一步我建议：**

* 要么：
  把 `DatasetSanityReport` + `LabelReport` 的结构稍微再标准化一点（方便以后 JSONL 存档 & 前端展示）。
* 要么：
  直接往 **HPO / ExperimentRegistry** 这一段推进，把完整的 `run_id` 生命周期写出来（从 PI proposal → HPO → 回测 → 写 ExperimentRecord → 给 Brain 用）。

你继续一句“继续”，我就顺着这条链，把 **HPO 记录 ExperimentRecord 的代码骨架** 帮你写出来。
好，那我就顺着我们说好的路线，把 **PI → HPO → ExperimentRegistry** 这一条链从“能跑”升级为“有记录、有签名、能给 AI 大脑用”。

这次主要做三件事：

1. 定义标准的 **ExperimentRecord 类型**（实验记录是什么样子）
2. 实现一个简单但够用的 **JSONL 实验登记簿 ExperimentRegistry**
3. 给出一个 **从 PIResult → HPO → 写入 Registry** 的完整骨架函数，后面你只要把现有 HPO 结果接上即可

> 下面所有代码都是“加法”，不会推翻你现有的 HPO，只是包一层。

---

## 1️⃣ 实验类型定义：`experiments/types.py`

这个文件只负责定义结构，不碰 IO。

```python
# experiments/types.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class SplitMetrics:
    """
    训练/验证/测试某一条 split 的指标集合。
    这里不规定具体有哪些指标，统一用字典：
      - 比如 {"sharpe": 1.3, "sortino": 1.8, "rmse": 0.012}
    """
    name: str  # "train" / "valid" / "test" / "cv"
    metrics: Dict[str, float]


@dataclass
class HPOSummary:
    """
    HPO/模型选择总结。
    """
    n_trials: int
    best_model_type: str
    best_params: Dict[str, Any]
    best_objective: float
    direction: str  # "minimize" / "maximize"
    split_metrics: List[SplitMetrics]


@dataclass
class DatasetSignature:
    """
    数据“签名”：让你以后能快速知道这个实验用的是哪段数据。
    """
    table: str
    symbol: str
    start_ts: str
    end_ts: str
    bar_interval: str  # e.g. "1m", "5s"
    row_count: int
    feature_cols: List[str]
    target_col: str
    label_id: str


@dataclass
class SanityBundle:
    """
    把 DatasetSanityReport & LabelReport 的摘要打包。
    注意：这里用 dict，避免强绑定具体实现。
    """
    features_sanity: Dict[str, Any]
    label_sanity: Dict[str, Any]


@dataclass
class ExperimentRecord:
    """
    一次完整的 PI → HPO → 回测 实验记录。
    这是后面 AI 大脑挑策略时看到的“单元”。
    """
    run_id: str
    created_at: str  # ISO8601
    created_by: str  # "pi_orchestrator" / "manual" / "auto_loop" 等
    experiment_type: str  # "pi_hpo" / "pi_backtest" / 未来扩展

    dataset: DatasetSignature
    label: Dict[str, Any]      # 来自 LabelSpec + LabelReport
    sanity: SanityBundle       # DatasetSanityReport + LabelReport 抽象

    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]

    sandbox_features_accepted: List[Dict[str, Any]]
    sandbox_features_rejected: List[Dict[str, Any]]

    hpo: Optional[HPOSummary] = None
    backtest: Optional[Dict[str, Any]] = None  # 以后你接 BacktestEngine

    meta: Dict[str, Any] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()
```

> 这个结构的好处：
>
> * AI 大脑以后只需要知道 `run_id`，就能拿到完整上下文
> * 你在前端 / Notebook 里也能方便地加载、查询、画图

---

## 2️⃣ JSONL 实验登记簿：`experiments/registry.py`

简单但靠谱的实现：所有实验 append 到一个 `experiments.jsonl` 里，每行一个 JSON，方便追溯和 grep。

```python
# experiments/registry.py

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from experiments.types import ExperimentRecord


class ExperimentRegistry:
    """
    极简 JSONL 实验登记簿。

    - 每条 ExperimentRecord 作为一行 JSON 写入 experiments.jsonl
    - 后续可以扩展 index / 按 run_id 查找
    """

    def __init__(self, root_dir: str | Path, file_name: str = "experiments.jsonl") -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.file_path = self.root_dir / file_name

    # ─────────────────────────────────────────────────────────────
    # 写入
    # ─────────────────────────────────────────────────────────────
    def append(self, record: ExperimentRecord) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with self.file_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    # ─────────────────────────────────────────────────────────────
    # 读取
    # ─────────────────────────────────────────────────────────────
    def iter_raw(self) -> Iterable[dict]:
        if not self.file_path.exists():
            return []
        with self.file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    # 这里可以加日志：某一行坏了
                    continue

    def load_all(self, predicate: Optional[Callable[[dict], bool]] = None) -> List[dict]:
        """
        暂时先返回 dict，后续如果需要再反序列化为 ExperimentRecord。
        predicate 可以用来按 symbol / label_id / run_id 过滤。
        """
        preds = predicate or (lambda _: True)
        return [rec for rec in self.iter_raw() if preds(rec)]

    def find_by_run_id(self, run_id: str) -> Optional[dict]:
        for rec in self.iter_raw():
            if rec.get("run_id") == run_id:
                return rec
        return None
```

> 后面 Phase 4 里，AI 大脑要用的就是这个 registry：
>
> * 从中找到 “Sharpe > 1 且 sanity OK 的 run_id 列表”
> * 然后在 Prompt 里告诉 LLM：**只能在这些 run_id 里选**

---

## 3️⃣ 从 PI → HPO → Registry：统一的执行骨架

现在我们把前面已经做好的几块拼起来：

* `PIResult`（PI 的输出）
* `prepare_dataset_for_hpo`（体检 + label 生成）
* `ExperimentRegistry`（登记簿）
* 你现有的 HPO runner（比如 `run_hpo(df, feature_cols, target_col, hpo_grid)`）

### 3.1 HPO runner 约定一个统一返回结构

先约定一下 HPO 函数的输出（你现有 wf_hpo 完全可以 wrap 一层）：

```python
# 假设你写一个 adapter，让现有 HPO 变成这个签名：
def run_hpo_with_grid(
    df: pd.DataFrame,
    feature_cols: list[str],
    target_col: str,
    hpo_grid: dict,
) -> dict:
    """
    统一的 HPO 输出结构示例：

    return {
        "n_trials": 32,
        "best_model_type": "xgb",
        "best_params": {...},
        "best_objective": 1.35,
        "direction": "maximize",
        "split_metrics": {
            "train": {"sharpe": 1.5, "sortino": 2.0},
            "valid": {"sharpe": 1.2, "sortino": 1.6},
        },
    }
    """
    ...
```

> 你只需要在现有 HPO 结束的地方拼出这个 dict，就能直接和下面的骨架对接。

---

### 3.2 统一 orchestrator：`pi_hpo_orchestrator.py`

```python
# pi_hpo_orchestrator.py

from __future__ import annotations

import uuid
from typing import Any, Dict, Tuple

import pandas as pd

from experiments.registry import ExperimentRegistry
from experiments.types import (
    DatasetSignature,
    ExperimentRecord,
    HPOSummary,
    SanityBundle,
    SplitMetrics,
)
from labels.contracts import get_label_spec
from labels.generator import generate_label_column, LabelReport
from data.dataset_sanity import DatasetSanityReport, run_dataset_sanity_checks

from pi.principal_investigator import PIResult  # 你前面的类
# from hpo.runner import run_hpo_with_grid  # 你现有 HPO 的适配函数


def _build_dataset_signature(
    pi_result: PIResult,
    df_with_label: pd.DataFrame,
    label_id: str,
    bar_interval: str,
) -> DatasetSignature:
    feature_subset = pi_result.hpo_grid.get("feature_subset", [[]])[0] or []
    return DatasetSignature(
        table=pi_result.feature_recipe.get("use_table", "unknown"),
        symbol=pi_result.training_plan.get("symbol", "UNKNOWN"),
        start_ts=pi_result.training_plan.get("start_ts", ""),  # 如果有的话，也可以从外面传入
        end_ts=pi_result.training_plan.get("end_ts", ""),
        bar_interval=bar_interval,
        row_count=int(len(df_with_label)),
        feature_cols=list(feature_subset),
        target_col=label_id,
        label_id=label_id,
    )


def _build_hpo_summary(hpo_result: Dict[str, Any]) -> HPOSummary:
    split_metrics = []
    for name, metrics_dict in (hpo_result.get("split_metrics") or {}).items():
        split_metrics.append(SplitMetrics(name=name, metrics=metrics_dict))

    return HPOSummary(
        n_trials=int(hpo_result.get("n_trials", 0)),
        best_model_type=str(hpo_result.get("best_model_type", "")),
        best_params=hpo_result.get("best_params", {}),
        best_objective=float(hpo_result.get("best_objective", 0.0)),
        direction=str(hpo_result.get("direction", "maximize")),
        split_metrics=split_metrics,
    )


def run_pi_hpo_and_register(
    *,
    pi_result: PIResult,
    registry: ExperimentRegistry,
    bar_interval: str,
    created_by: str = "pi_orchestrator",
    meta: Dict[str, Any] | None = None,
    hpo_runner: Any = None,  # 你现有的 run_hpo_with_grid 函数
) -> Tuple[ExperimentRecord, Dict[str, Any]]:
    """
    串起来整个链路：

      PIResult → DatasetSanity → Label → HPO → ExperimentRegistry

    返回:
      - ExperimentRecord（已写入 registry）
      - hpo_result（原样返回，方便你后面直接用）
    """
    if hpo_runner is None:
        raise ValueError("hpo_runner must be provided (e.g., run_hpo_with_grid)")

    df = pi_result.dataset_df

    # 1) 对特征做 DatasetSanity（不含 label）
    sanity_features: DatasetSanityReport = run_dataset_sanity_checks(df, target_column=None)
    if not sanity_features.ok:
        raise RuntimeError(f"Feature sanity check failed: {sanity_features.summary_str()}")

    # 2) 生成 label
    label_id = pi_result.label_id or "ret_10m_fwd"
    label_spec = get_label_spec(label_id)
    df_with_label, label_report = generate_label_column(df, label_spec, col_name=label_id)

    # 3) 再对 features+label 做一次体检（检查 label 分布、共线性等）
    sanity_with_label: DatasetSanityReport = run_dataset_sanity_checks(df_with_label, target_column=label_id)
    if not sanity_with_label.ok:
        raise RuntimeError(
            "Dataset sanity with label failed: "
            f"{sanity_with_label.summary_str()} "
            f"label_report={label_report.summary_dict()}"
        )

    # 4) 调 HPO（用你现有的 HPO runner）
    feature_subset = pi_result.hpo_grid.get("feature_subset", [[]])[0] or []
    hpo_result = hpo_runner(
        df_with_label,
        feature_cols=feature_subset,
        target_col=label_id,
        hpo_grid=pi_result.hpo_grid,
    )

    # 5) 组装 ExperimentRecord
    run_id = str(uuid.uuid4())

    dataset_sig = _build_dataset_signature(
        pi_result=pi_result,
        df_with_label=df_with_label,
        label_id=label_id,
        bar_interval=bar_interval,
    )

    sanity_bundle = SanityBundle(
        features_sanity=sanity_features.summary_dict(),
        label_sanity={
            "label_report": label_report.summary_dict(),
            "with_label_sanity": sanity_with_label.summary_dict(),
        },
    )

    hpo_summary = _build_hpo_summary(hpo_result)

    record = ExperimentRecord(
        run_id=run_id,
        created_at=ExperimentRecord.now_iso(),
        created_by=created_by,
        experiment_type="pi_hpo",
        dataset=dataset_sig,
        label={
            "label_id": label_spec.id,
            "type": label_spec.type,
            "generator": label_spec.generator,
            "params": label_spec.params,
            "required_columns": label_spec.required_columns,
            "description": label_spec.description,
            "report": label_report.summary_dict(),
        },
        sanity=sanity_bundle,
        feature_recipe=pi_result.feature_recipe,
        training_plan=pi_result.training_plan,
        sandbox_features_accepted=[
            {"alias": f.alias, "expr": f.expr}
            for f in pi_result.sandbox_features_accepted
        ],
        sandbox_features_rejected=[
            {"alias": f.alias, "expr": f.expr, "reason_rejected": f.reason_rejected}
            for f in pi_result.sandbox_features_rejected
        ],
        hpo=hpo_summary,
        backtest=None,  # 以后你接 BacktestEngine 再填
        meta=meta or {},
    )

    # 6) 写入 Registry
    registry.append(record)

    return record, hpo_result
```

> 这段是关键：
>
> * **PIResult** 进来 → 自动体检 → 自动算 label → 调你的 HPO → 自动写 ExperimentRecord
> * AI 大脑以后只要拿着 `run_id`，就能知道：
>
>   * 用的是什么 label & 特征
>   * 数据质量是否合格
>   * HPO 结果如何（Sharpe/Sortino 等）

---

## 4️⃣ 现在三层的状态再盘一下

你刚刚问的三层，我按现在的落地程度总结一下：

### ① 数据 & 特征 & 标签层

* ✅ DatasetSanity：

  * NaN/Inf/零方差/极端偏度/共线性（可在 ClickHouse 下沉部分统计）
* ✅ Sandbox 特征：

  * 安全 DSL + 白名单函数 + 列名检查 + 接入 PI
* ✅ Label Contract + Generator：

  * forward_return + triple_barrier（简版）
  * 所有 label 必须从白名单 `label_id` 选择
* ✅ Label + Sanity 与 ExperimentRecord 集成：

  * 每个实验的 label 分布/质量都被记录下来

### ② 机器学习 / HPO / 实验治理层

* ✅ HPO 输入结构统一：

  * `df_with_label` + `feature_subset` + `label_id` + `hpo_grid`
* ✅ ExperimentRecord：

  * 数据签名 / label / sanity / recipe / training_plan / sandbox / HPO 结果
* ✅ JSONL Registry：

  * 所有 run 统一落入一个“流水账”
* ⏳ 你这边要做的只是：

  * 在现有 HPO 的最后返回匹配 `hpo_result` 的 dict
  * 把 `run_pi_hpo_and_register` 用在你当前 PI → HPO 的调用入口处

### ③ AI 大脑 & Prompt 安全层（预备）

* ✅ 限制 LLM：

  * 只能选 sandbox DSL 里的特征表达式
  * 只能选 `label_id` 白名单
* ⏳ 下一步：

  * 把 `ExperimentRegistry` 接入 AI 大脑的 `context_builder`：

    * 给它一个 `allowed_run_ids` 列表（例如 Sharpe > 1 & sanity.ok == True）
    * 在 Prompt 里写死：**只能在 allowed_run_ids 中选策略**

---

接下来我建议的**下一步**：

> 用一小段“假数据 + 假 HPO runner”在本地快速走通：
> `PIResult → run_pi_hpo_and_register → experiments.jsonl`
> 确认整个链条不会报错、结构清晰、JSON 可读。

你如果愿意，我们可以继续往下，把 **“Brain context builder：从 ExperimentRegistry 里筛选策略 + 构造 Prompt 约束”** 这一段也落地出来，让 AI 大脑真正只能选已有 run_id，而不是凭空想象策略。
好，我们先把 **PI → HPO → ExperimentRegistry** 这条链彻底打磨顺一点，把刚才方案里几个“暗坑”修正掉，再给你一个可以直接落地的版本。

---

## 一步：先把 `PIResult` 补齐，避免隐藏字段

你现在的 `PIResult` 是这样的（你贴过）：

```python
@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
```

但我在 orchestrator 里用到了：

* `pi_result.label_id`
* `pi_result.sandbox_features_accepted`
* `pi_result.sandbox_features_rejected`

为了不到处乱传参数、又保持向后兼容，我们**只在这里给 `PIResult` 做最小扩展**：

```python
# pi/principal_investigator.py

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import pandas as pd

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # ⬇️ 新增：但都有默认值，不会破坏已有代码
    label_id: str = "ret_10m_fwd"
    # 这里直接存 dict，避免再引入新 dataclass
    sandbox_features_accepted: List[Dict[str, Any]] = field(default_factory=list)
    sandbox_features_rejected: List[Dict[str, Any]] = field(default_factory=list)
```

> **好处：**
>
> * 以后 PI 想换 label 时，只要在 `PI.propose()` 里给 `PIResult(label_id="xxx")` 即可。
> * sandbox 特征那一层：你可以在 PI 内部对 LLM 生成的特征做“审核”，把通过和拒绝的都放进这两个 list。orchestrator 原样带入 ExperimentRecord。

---

## 二步：修正 orchestrator 签名，避免到处乱猜 start_ts/end_ts

之前版本里我在 `_build_dataset_signature` 里从 `training_plan` 里尝试 `get("start_ts")`，但这其实是**猜字段**，不稳。

更稳妥的做法是：**调用 orchestrator 的人显式传入 `start_ts` & `end_ts`**（你本来就有这两个参数）。

### 2.1 更新 `_build_dataset_signature`

```python
# pi_hpo_orchestrator.py

from experiments.types import DatasetSignature

def _build_dataset_signature(
    pi_result: "PIResult",
    df_with_label: pd.DataFrame,
    label_id: str,
    bar_interval: str,
    start_ts: str,
    end_ts: str,
) -> DatasetSignature:
    feature_subset = pi_result.hpo_grid.get("feature_subset", [[]])[0] or []
    return DatasetSignature(
        table=pi_result.feature_recipe.get("use_table", "unknown"),
        symbol=pi_result.training_plan.get("symbol", "UNKNOWN"),
        start_ts=start_ts,
        end_ts=end_ts,
        bar_interval=bar_interval,
        row_count=int(len(df_with_label)),
        feature_cols=list(feature_subset),
        target_col=label_id,
        label_id=label_id,
    )
```

---

### 2.2 更新 `run_pi_hpo_and_register` 签名 + sandbox 处理

这里给一个**修正后的完整版本**（替换掉你当前的同名函数即可）：

```python
# pi_hpo_orchestrator.py

from __future__ import annotations

import uuid
from typing import Any, Dict, Tuple, Callable

import pandas as pd

from experiments.registry import ExperimentRegistry
from experiments.types import (
    DatasetSignature,
    ExperimentRecord,
    HPOSummary,
    SanityBundle,
    SplitMetrics,
)
from labels.contracts import get_label_spec
from labels.generator import generate_label_column, LabelReport
from data.dataset_sanity import DatasetSanityReport, run_dataset_sanity_checks

from pi.principal_investigator import PIResult  # 上面扩展后的版本


def _build_dataset_signature(
    pi_result: PIResult,
    df_with_label: pd.DataFrame,
    label_id: str,
    bar_interval: str,
    start_ts: str,
    end_ts: str,
) -> DatasetSignature:
    feature_subset = pi_result.hpo_grid.get("feature_subset", [[]])[0] or []
    return DatasetSignature(
        table=pi_result.feature_recipe.get("use_table", "unknown"),
        symbol=pi_result.training_plan.get("symbol", "UNKNOWN"),
        start_ts=start_ts,
        end_ts=end_ts,
        bar_interval=bar_interval,
        row_count=int(len(df_with_label)),
        feature_cols=list(feature_subset),
        target_col=label_id,
        label_id=label_id,
    )


def _build_hpo_summary(hpo_result: Dict[str, Any]) -> HPOSummary:
    split_metrics = []
    for name, metrics_dict in (hpo_result.get("split_metrics") or {}).items():
        split_metrics.append(SplitMetrics(name=name, metrics=metrics_dict))

    return HPOSummary(
        n_trials=int(hpo_result.get("n_trials", 0)),
        best_model_type=str(hpo_result.get("best_model_type", "")),
        best_params=hpo_result.get("best_params", {}),
        best_objective=float(hpo_result.get("best_objective", 0.0)),
        direction=str(hpo_result.get("direction", "maximize")),
        split_metrics=split_metrics,
    )


def run_pi_hpo_and_register(
    *,
    pi_result: PIResult,
    registry: ExperimentRegistry,
    bar_interval: str,
    start_ts: str,
    end_ts: str,
    created_by: str = "pi_orchestrator",
    meta: Dict[str, Any] | None = None,
    hpo_runner: Callable[[pd.DataFrame, list[str], str, dict], Dict[str, Any]] | None = None,
) -> Tuple[ExperimentRecord, Dict[str, Any]]:
    """
    串起来整个链路：

      PIResult → DatasetSanity → Label → HPO → ExperimentRegistry

    参数:
      - pi_result: 来自 PrincipalInvestigator.propose()
      - registry: ExperimentRegistry 实例
      - bar_interval: "1s"/"5s"/"1m" 等，用于 DatasetSignature
      - start_ts, end_ts: 本次训练/回测使用的数据时间范围
      - hpo_runner: 你的 HPO 适配函数, 签名: (df, feature_cols, target_col, hpo_grid) -> dict

    返回:
      - ExperimentRecord（已写入 registry）
      - hpo_result（原样返回，方便你后面直接用）
    """
    if hpo_runner is None:
        raise ValueError("hpo_runner must be provided (e.g., run_hpo_with_grid)")

    df = pi_result.dataset_df

    # 1) 对特征做 DatasetSanity（不含 label）
    sanity_features: DatasetSanityReport = run_dataset_sanity_checks(df, target_column=None)
    if not sanity_features.ok:
        raise RuntimeError(f"Feature sanity check failed: {sanity_features.summary_str()}")

    # 2) 生成 label（优先用 PIResult 里带的 label_id）
    label_id = getattr(pi_result, "label_id", "ret_10m_fwd")
    label_spec = get_label_spec(label_id)
    df_with_label, label_report = generate_label_column(df, label_spec, col_name=label_id)

    # 3) 再对 features+label 做一次体检（检查 label 分布、共线性等）
    sanity_with_label: DatasetSanityReport = run_dataset_sanity_checks(df_with_label, target_column=label_id)
    if not sanity_with_label.ok:
        raise RuntimeError(
            "Dataset sanity with label failed: "
            f"{sanity_with_label.summary_str()} "
            f"label_report={label_report.summary_dict()}"
        )

    # 4) 调 HPO（用你现有的 HPO runner）
    feature_subset = pi_result.hpo_grid.get("feature_subset", [[]])[0] or []
    hpo_result = hpo_runner(
        df_with_label,
        feature_cols=feature_subset,
        target_col=label_id,
        hpo_grid=pi_result.hpo_grid,
    )

    # 5) 组装 ExperimentRecord
    run_id = str(uuid.uuid4())

    dataset_sig = _build_dataset_signature(
        pi_result=pi_result,
        df_with_label=df_with_label,
        label_id=label_id,
        bar_interval=bar_interval,
        start_ts=start_ts,
        end_ts=end_ts,
    )

    sanity_bundle = SanityBundle(
        features_sanity=sanity_features.summary_dict(),
        label_sanity={
            "label_report": label_report.summary_dict(),
            "with_label_sanity": sanity_with_label.summary_dict(),
        },
    )

    hpo_summary = _build_hpo_summary(hpo_result)

    record = ExperimentRecord(
        run_id=run_id,
        created_at=ExperimentRecord.now_iso(),
        created_by=created_by,
        experiment_type="pi_hpo",
        dataset=dataset_sig,
        label={
            "label_id": label_spec.id,
            "type": label_spec.type,
            "generator": label_spec.generator,
            "params": label_spec.params,
            "required_columns": label_spec.required_columns,
            "description": label_spec.description,
            "report": label_report.summary_dict(),
        },
        sanity=sanity_bundle,
        feature_recipe=pi_result.feature_recipe,
        training_plan=pi_result.training_plan,
        # ⬇️ 这里不做进一步转换，原样记录
        sandbox_features_accepted=pi_result.sandbox_features_accepted or [],
        sandbox_features_rejected=pi_result.sandbox_features_rejected or [],
        hpo=hpo_summary,
        backtest=None,  # 以后接 BacktestEngine 再填
        meta=meta or {},
    )

    # 6) 写入 Registry
    registry.append(record)

    return record, hpo_result
```

> 这样：
>
> * start/end 完全由你外部控制，不依赖 training_plan 内容。
> * sandbox 特征作为“原始 dict 列表”写入记录，方便你以后随时变更结构。
> * label_id 优先从 PIResult 拿，不用再从别的地方猜。

---

## 三步：一个“假 HPO runner” + “烟雾测试”怎么写

你现在真正的 HPO 流程比较复杂，我不动它。
我只给一个**适配器模板**，你可以把它包在现有 HPO 外面：

```python
# hpo/adapter.py

from __future__ import annotations

from typing import Any, Dict, List
import pandas as pd

# 这里引入你现有的 wf_hpo / run_hpo 之类
# from hpo.wf_hpo import main_hpo_entry

def run_hpo_with_grid(
    df: pd.DataFrame,
    feature_cols: List[str],
    target_col: str,
    hpo_grid: Dict[str, Any],
) -> Dict[str, Any]:
    """
    统一的 HPO 输出结构示例：

    return {
        "n_trials": 32,
        "best_model_type": "xgb",
        "best_params": {...},
        "best_objective": 1.35,
        "direction": "maximize",
        "split_metrics": {
            "train": {"sharpe": 1.5, "sortino": 2.0},
            "valid": {"sharpe": 1.2, "sortino": 1.6},
        },
    }
    """
    # 1) 在这里你调用现有的 HPO 逻辑
    # best_model, best_score, metrics_by_split = main_hpo_entry(...)

    # 2) 下面是一个假数据模板，帮助你对齐结构：
    fake = {
        "n_trials": 8,
        "best_model_type": "xgb",
        "best_params": {"max_depth": 4, "learning_rate": 0.05},
        "best_objective": 1.23,
        "direction": "maximize",
        "split_metrics": {
            "train": {"sharpe": 1.5, "sortino": 2.1},
            "valid": {"sharpe": 1.1, "sortino": 1.6},
        },
    }
    return fake
```

然后你可以写一个非常简单的“烟雾测试”脚本，先不用 ClickHouse / LLM，直接构造一个假的 `PIResult` 和假 DataFrame：

```python
# debug_pi_hpo_flow.py

from __future__ import annotations

import numpy as np
import pandas as pd

from experiments.registry import ExperimentRegistry
from pi.principal_investigator import PIResult
from pi_hpo_orchestrator import run_pi_hpo_and_register
from hpo.adapter import run_hpo_with_grid


def make_fake_pi_result() -> PIResult:
    n = 200
    df = pd.DataFrame(
        {
            "ts": pd.date_range("2025-01-01 09:30:00", periods=n, freq="1min"),
            "close": np.linspace(100, 110, n) + np.random.randn(n),
            "volume": np.random.randint(10_000, 50_000, size=n),
        }
    )

    feature_recipe = {
        "use_table": "bars_1m",
        "base_features": ["close", "volume"],
        "derived": [],
    }
    training_plan = {
        "symbol": "NVDA",
        "task": "reg",
        "target": "ret_10m_fwd",
        "decision": {"mode": "quantile", "q_long": 0.7, "q_short": 0.3},
    }
    hpo_grid = {
        "model_type": ["xgb"],
        "feature_subset": [["close", "volume"]],
        "task": ["reg"],
        "decision": [training_plan["decision"]],
        "model_params": [{"max_depth": 4}],
    }

    return PIResult(
        feature_recipe=feature_recipe,
        training_plan=training_plan,
        sql="-- fake sql",
        dataset_df=df,
        hpo_grid=hpo_grid,
        label_id="ret_10m_fwd",
        sandbox_features_accepted=[],
        sandbox_features_rejected=[],
    )


if __name__ == "__main__":
    pi_result = make_fake_pi_result()
    registry = ExperimentRegistry(root_dir="./experiments_dev")

    record, hpo_result = run_pi_hpo_and_register(
        pi_result=pi_result,
        registry=registry,
        bar_interval="1m",
        start_ts="2025-01-01 09:30:00",
        end_ts="2025-01-01 12:30:00",
        hpo_runner=run_hpo_with_grid,
        created_by="debug_script",
        meta={"note": "smoke test"},
    )

    print("Saved run_id:", record.run_id)
    print("HPO result:", hpo_result)
    print("Experiment file:", registry.file_path)
```

> 你可以先把 `labels.*` / `DatasetSanity` 那几行暂时 stub 掉（比如返回 OK），只要能看到 **experiments_dev/experiments.jsonl** 里多了一行 JSON，就说明：
>
> ✅ PIResult → orchestrator → registry 这条链已经“通电”。
> 之后再逐步把真正的 DatasetSanity / LabelSpec / 真 HPO 接回去。

---

## 接下来可以做什么（下一步预告）

等这条链你本地验证 OK 之后，下一步就可以做：

1. **ExperimentRegistry → AI 大脑 context builder**

   * 从 jsonl 里筛选：`sanity.ok == True` 且 `hpo.best_objective` 达到你设定门槛（Sharpe/Sortino 等）
   * 得到一个候选 `run_id` 列表，比如 5–10 个
   * 在 Prompt 里写死：

     > “你只能在 allowed_run_ids 里选择策略，并给出权重，不允许自己发明新策略。”

2. **BrainDecision 的 JSON Schema**

   * `{"run_id": "...", "allocation": {...}, "notes": "..."}`
   * 用 pydantic / JSON schema 强制 LLM 输出完全合法的结构。

你先把上面这条 **PI → HPO → Registry + 烟雾测试** 跑通，有任何报错，我们再一起按 error message 精修。只要 experiments.jsonl 能稳定地累积记录，AI 大脑那一层就有“可用的记忆”了。
