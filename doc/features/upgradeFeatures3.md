好，我们按计划继续落地，这一轮做两件核心事：

1. 搭一个 **Feature Registry（特征注册表）**，给 AI 大脑一份“官方可用特征菜单”（但不强制死锁，只是指导）。
2. 给 LLM 提出的 **SQL 派生特征加沙箱校验（Sandbox）**，防止它写奇怪 SQL 或潜在泄露。

> 这一步不会破坏你现有流程，只是：
>
> * 提供一个统一的配置入口（以后你慢慢把特征都搬进去）；
> * 保证 LLM 生成的 `derived` 特征更安全。

---

## 1️⃣ 配置文件：`config/feature_registry.json`（模板）

先给一个**可直接用、可扩展**的模板。你可以先照抄，后面再慢慢往里面加你的高阶特征。

**新文件**：`config/feature_registry.json`

```json
{
  "_comment": "Feature Registry - canonical catalog of features that AI Brain is encouraged to use. This is NOT a hard whitelist yet.",
  "version": "1.0",
  "features": {
    "open": {
      "kind": "native",
      "freq": ["1m"],
      "base_cols": ["open"],
      "tags": ["price", "ohlc", "base"],
      "desc": "Bar open price",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "high": {
      "kind": "native",
      "freq": ["1m"],
      "base_cols": ["high"],
      "tags": ["price", "ohlc", "base"],
      "desc": "Bar high price",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "low": {
      "kind": "native",
      "freq": ["1m"],
      "base_cols": ["low"],
      "tags": ["price", "ohlc", "base"],
      "desc": "Bar low price",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "close": {
      "kind": "native",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["price", "ohlc", "base"],
      "desc": "Bar close price",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "volume": {
      "kind": "native",
      "freq": ["1m"],
      "base_cols": ["volume"],
      "tags": ["volume", "base"],
      "desc": "Bar traded volume",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },

    "ret_1b": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["return", "short_horizon"],
      "desc": "1 bar simple return, computed in Python",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "ret_5b": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["return", "short_horizon"],
      "desc": "5 bar simple return, computed in Python",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "ret_10b": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["return", "medium_horizon"],
      "desc": "10 bar simple return, computed in Python",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },

    "vol_20b": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["volatility", "rolling"],
      "desc": "20 bar rolling std of ret_1b",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },

    "ma_20_close": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["trend", "moving_average"],
      "desc": "20 bar moving average of close",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "ma_50_close": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["trend", "moving_average"],
      "desc": "50 bar moving average of close",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "ma_200_close": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["close"],
      "tags": ["trend", "moving_average"],
      "desc": "200 bar moving average of close",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },

    "vol_ma_20": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["volume"],
      "tags": ["volume", "moving_average"],
      "desc": "20 bar moving average of volume",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "rvol_20": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["volume"],
      "tags": ["volume", "rvol"],
      "desc": "Relative volume: volume / vol_ma_20",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },

    "minute_of_day": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["ts"],
      "tags": ["time", "session"],
      "desc": "Minutes since midnight",
      "leak_safe": true,
      "default_exposed_to_llm": true
    },
    "day_of_week": {
      "kind": "python",
      "freq": ["1m"],
      "base_cols": ["ts"],
      "tags": ["time", "session"],
      "desc": "Day of week, Monday=0",
      "leak_safe": true,
      "default_exposed_to_llm": true
    }
  }
}
```

> 你以后要加更多特征（比如 microstructure、因子特征、行业 Dummy 等），就在 `features` 里加条目即可。

---

## 2️⃣ Registry 代码：`features/registry.py`

这个模块负责：

* 加载 `feature_registry.json`；
* 暴露 `FeatureRegistry` 对象给 PI / AI 大脑；
* 生成给 LLM 看的 “可用特征摘要”。

**新文件**：`features/registry.py`

```python
# features/registry.py

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Iterable


@dataclass
class FeatureDef:
    name: str
    kind: str                    # "native" | "python" | "sandbox"
    freq: List[str]
    base_cols: List[str]
    tags: List[str]
    desc: str
    leak_safe: bool = True
    default_exposed_to_llm: bool = True


class FeatureRegistry:
    """
    Lightweight feature registry.

    当前用途：
      - 给 LLM 一份“推荐特征列表”（不强制）
      - 以后可以逐步扩展为更严格的 contract
    """

    def __init__(self, features: Dict[str, FeatureDef], version: str = "1.0") -> None:
        self._features = features
        self._version = version

    @classmethod
    def from_json(cls, path: str | Path) -> "FeatureRegistry":
        p = Path(path)
        if not p.exists():
            # 没有文件时，给一个空 registry，避免直接炸
            return cls(features={}, version="0.0")

        raw = json.loads(p.read_text(encoding="utf-8"))
        version = str(raw.get("version", "1.0"))
        feats: Dict[str, FeatureDef] = {}

        for name, f in raw.get("features", {}).items():
            feats[name] = FeatureDef(
                name=name,
                kind=f.get("kind", "native"),
                freq=list(f.get("freq", [])),
                base_cols=list(f.get("base_cols", [])),
                tags=list(f.get("tags", [])),
                desc=f.get("desc", ""),
                leak_safe=bool(f.get("leak_safe", True)),
                default_exposed_to_llm=bool(f.get("default_exposed_to_llm", True)),
            )

        return cls(features=feats, version=version)

    @property
    def version(self) -> str:
        return self._version

    def get(self, name: str) -> Optional[FeatureDef]:
        return self._features.get(name)

    def all_names(self) -> List[str]:
        return sorted(self._features.keys())

    def list_for_freq(
        self,
        freq: str,
        *,
        exposed_only: bool = True,
    ) -> List[FeatureDef]:
        out: List[FeatureDef] = []
        for f in self._features.values():
            if freq not in f.freq:
                continue
            if exposed_only and not f.default_exposed_to_llm:
                continue
            out.append(f)
        # 稍微按 name 排个序，方便阅读
        out.sort(key=lambda x: x.name)
        return out

    def summarize_for_llm(self, freq: str) -> str:
        """
        把给定 freq 下建议的特征，变成一段简短的自然语言摘要，
        用在 system / user prompt 中，指导 LLM 使用。
        """
        feats = self.list_for_freq(freq=freq, exposed_only=True)
        if not feats:
            return "No curated features registered for this frequency yet."

        lines: List[str] = []
        for f in feats:
            tags = ", ".join(f.tags) if f.tags else ""
            tag_str = f" (tags: {tags})" if tags else ""
            lines.append(f"- {f.name}{tag_str}: {f.desc}")
        return "\n".join(lines)


# 默认加载器，可以在其他模块中直接使用
def get_default_registry(config_dir: str | Path = "config") -> FeatureRegistry:
    path = Path(config_dir) / "feature_registry.json"
    return FeatureRegistry.from_json(path)
```

---

## 3️⃣ SQL 沙箱校验：`pi/sandbox_sql.py`

现在给 LLM 提出的 `derived` 特征加一层“防火墙”，防止它写：

* `SELECT ...`
* `;`, `--`, `/* */` 等
* 或者引用不在表中的乱七八糟字段 / 函数。

**新文件**：`pi/sandbox_sql.py`

```python
# pi/sandbox_sql.py

from __future__ import annotations

import re
from typing import Dict, List, Tuple


# 允许使用的一小撮函数（可以后续扩展）
ALLOWED_FUNCS = {
    "abs",
    "log",
    "exp",
    "sqrt",
    "least",
    "greatest",
    "if",
    "coalesce",
}

# 简单关键词黑名单，防 SQL 注入 / 子查询
BANNED_KEYWORDS = {
    "select",
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "truncate",
    "union",
    "join",
    "into",
    "from",
}
BANNED_SYMBOLS = {
    ";",
    "--",
    "/*",
    "*/",
}


def is_expr_safe(expr: str, allowed_cols: List[str]) -> bool:
    """
    极简版 SQL 表达式沙箱校验器。
    目标不是“解析一切”，而是：
      - 拦截明显危险 / 超出范围的表达式
      - 确保只引用允许的列名 + 白名单函数
    """
    if not expr or not expr.strip():
        return False

    text = expr.lower()

    # 1. 禁止明显危险关键词 / 符号
    for kw in BANNED_KEYWORDS:
        if kw in text:
            return False
    for sym in BANNED_SYMBOLS:
        if sym in text:
            return False

    # 2. 括号平衡性检查（非常粗糙，但足够早期使用）
    if expr.count("(") != expr.count(")"):
        return False

    # 3. 提取所有“标识符”
    #    例如: close, volume, abs, my_custom_func
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expr)

    allowed_set = set(allowed_cols)
    for ident in identifiers:
        low = ident.lower()
        # 数学函数/控制函数
        if low in ALLOWED_FUNCS:
            continue
        # 内部列名
        if ident in allowed_set:
            continue
        # 数字常量 / 别的东西，不在这个列表里，
        # 但我们用正则已经把数字过滤掉了，所以这里剩下的都是“词”:
        # 如果既不是函数也不是列名，就直接拒绝，避免复杂表达式。
        return False

    return True


def filter_valid_derived(
    recipe: Dict,
    *,
    feature_catalog: List[str],
) -> Tuple[Dict, List[Dict]]:
    """
    过滤掉不安全的 derived 特征，返回：
      - 新的 recipe（只保留安全 derived）
      - 被 drop 的 derived 列表，方便日志记录
    """
    allowed_cols = list(feature_catalog)
    valid: List[Dict] = []
    dropped: List[Dict] = []

    for d in recipe.get("derived", []) or []:
        expr = d.get("expr")
        alias = d.get("alias")
        if not expr or not alias:
            dropped.append(d)
            continue

        if is_expr_safe(expr, allowed_cols):
            valid.append(d)
        else:
            dropped.append(d)

    new_recipe = dict(recipe)
    new_recipe["derived"] = valid
    return new_recipe, dropped
```

> 以后你可以在日志里记录 `dropped`，方便你看到 AI 在乱搞什么。

---

## 4️⃣ 把 Registry + Sandbox 接进 PrincipalInvestigator

现在我们只做**小改动**，不会推翻你上次的 `PrincipalInvestigator` 版本：

* 加载 FeatureRegistry，用它来给 LLM 一段“推荐特征描述”；
* 在用 `recipe` 生成 SQL 前，先跑一遍 `filter_valid_derived`；
* manifest 里多记录一下 registry 版本号。

**修改点**：`pi/principal_investigator.py`（在你上一次版本基础上增量修改）

我只贴有变化的部分，你可以对比合并。

```python
# pi/principal_investigator.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql_safe
from quality.dataset_sanity import run_dataset_sanity
from contracts.labels import attach_label
from experiments.manifest import create_manifest, save_manifest
from features.standard_features import (
    PYTHON_DERIVED_FEATURES,
    add_standard_features,
)
from features.registry import get_default_registry          # NEW
from pi.sandbox_sql import filter_valid_derived             # NEW
```

在 `propose()` 里，替换 / 增加以下内容（关键片段）：

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
        freq: str = "1m",
        default_label_id: str = "ret_10b_fwd",
    ) -> PIResult:
        # === 1) 拿表结构（原始列） ===
        catalog = _fetch_catalog(self._client, table)

        # === NEW: 加载 Feature Registry，并生成给 LLM 的特征摘要 ===
        registry = get_default_registry()
        registry_summary = registry.summarize_for_llm(freq=freq)

        # === 2) 让 LLM 生成 feature_recipe + training_plan ===
        python_feat_str = ", ".join(PYTHON_DERIVED_FEATURES)

        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"原始特征列(来自 ClickHouse 表): {', '.join(catalog)}\n"
            f"可选的标准 Python 衍生特征: {python_feat_str}\n"
            f"预测窗口: {horizon}\n\n"
            "===== Curated Feature Registry (recommended features) =====\n"
            f"{registry_summary}\n"
            "===========================================================\n"
            "在设计 feature_recipe.base_features 时，应优先使用上面登记的特征，"
            "必要时可以额外选用原始列中的特征。\n"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
            "禁止输出任何 SQL 代码或 target 表达式，label 将在 Python 层统一计算。"
            "feature_recipe.base_features 中可以包括两类：\n"
            "1) 原始列名（在原始特征列表中出现）\n"
            "2) 标准 Python 衍生特征名（在标准 Python 特征列表中出现）\n"
            "尽量优先使用 Registry 中的推荐特征。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # === NEW: 对 derived 做 SQL 沙箱过滤 ===
        recipe, dropped_derived = filter_valid_derived(
            recipe,
            feature_catalog=catalog,
        )

        # === 后面的逻辑基本保持不变（label, SQL, sanity, HPO 等） ===
        # ... (省略，你用上一版的实现即可)
```

在 `create_manifest(...)` 调用时，可以把 registry 信息和沙箱结果存进去（方便以后调试/审计）：

```python
        manifest = create_manifest(
            symbol=symbol,
            freq=freq,
            label_id=label_id,
            start_ts=start_ts,
            end_ts=end_ts,
            data_source={
                "table": table,
                "use_table": recipe.get("use_table", table),
                "clickhouse": self._ch_cfg,
            },
            feature_recipe=recipe,
            training_plan=tplan,
            sanity_summary=sanity_summary,
            meta={
                "sql": sql,
                "feature_catalog": catalog,
                "python_derived_features": PYTHON_DERIVED_FEATURES,
                "feature_subset_effective": feature_subset,
                "registry_version": registry.version,          # NEW
                "sandbox_dropped_derived": dropped_derived,    # NEW
            },
        )
```

---

## 5️⃣ 这一轮完成后，你已经多了什么能力？

**从 AI 大脑视角：**

* 它不再只看到一堆“原始列名”，而是看到：

  * 一份精心整理的 **Feature Registry 摘要**（“推荐用这些”）；
  * 一份可用的 **Python 标准特征列表**；
* 它在设计 feature_recipe 时，自然会更倾向用这些“机构级基础特征”。

**从数据安全 & 可靠性视角：**

* 任何 `derived.expr` 在进 SQL 前，会被沙箱过滤一遍：

  * 不让它写复杂 SQL / 子查询 / DDL 等；
  * 只能引用表中真实存在的列 + 白名单函数；
* Manifest 里记录了：

  * 实际使用的特征子集；
  * 哪些 derived 被拦截了（以后你可以回溯 AI 大脑的“黑历史”）。

---

## 6️⃣ 下一步规划（我会继续按这个路线走）

下一轮（你再说“继续”）我会：

1. 在 **Feature Registry 的基础上**，给出一个简单的迁移适配器，把你现有的 `feature_programs.json` / `FeatureCalculator` 体系“挂”到 Registry 上（哪怕一开始只挂部分核心特征）。
2. 升级 **AI 大脑执行 Prompt** 的上下文，把我们现在已经造出来的：

   * `dataset_sanity` 摘要
   * `feature_registry` 摘要
   * 过去实验的回测性能（从 manifest 中抽取）
     打包成一个结构化输入，让你的 `v3_xxx` / `v4_experimental` 这些 prompt 真正变成“基于数据质量 & 历史效果做决策的机构级大脑”。

你现在什么都不用干，只要把这些文件按路径放进去，然后再回复我一个字：**“继续”**，我就接着把 Registry ↔ 旧特征系统，以及 AI 大脑 Prompt 升级部分继续落地。

好，我们按路线继续。这一轮我帮你**把特征 Registry 真正“接”到现有流程里**，再加一个**AI 大脑执行上下文构建器**，让大脑在做决策前能看到：

* 当前市场状态
* 风险门槛/风控参数
* 数据集质量（sanity）
* 模型/实验摘要
* 可用特征的“官方菜单”（Feature Registry）

你这一步不用决定做什么，我直接给你**可以落地的代码骨架**，你按路径放进去即可。

---

## 一、特征规划层：FeaturePlanner ↔ Registry ↔ 现有特征

### 1. 新模块：`features/planner.py`

目标：
给定 `feature_recipe` + FeatureRegistry + 当前 DataFrame 可用列，
算出：

* 实际要用的特征列表（喂给模型）
* 哪些特征需要在 Python 里计算
* 哪些特征被丢弃（缺列 / 不安全 / 不存在）

```python
# features/planner.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Iterable

from features.registry import FeatureRegistry
from features.standard_features import PYTHON_DERIVED_FEATURES


@dataclass
class FeatureMaterializationPlan:
    """
    描述这次训练/实验真正要用到的特征物化计划。
    """
    base_columns: List[str]        # 需要在 df 中存在的原始列（来自 ClickHouse）
    python_features: List[str]     # 需要通过 add_standard_features 计算的标准特征
    final_feature_list: List[str]  # 最终喂给模型的特征列表（顺序固定）
    dropped_features: List[str]    # 请求中被丢弃的特征名（记录日志用）


def plan_from_recipe(
    recipe: Dict[str, Any],
    registry: FeatureRegistry,
    *,
    available_columns: Iterable[str],
) -> FeatureMaterializationPlan:
    """
    根据 LLM 给出的 feature_recipe + Registry + 实际可用列，
    决定最终用哪些特征。

    - 优先信任 Registry（知道哪些是 python 特征 / 原始列）
    - 其次 fallback 到：
        * 在 PYTHON_DERIVED_FEATURES 里 → python 特征
        * 在 available_columns 里 → 原始列
        * 其他 → 丢弃并记录
    """
    available_set = set(available_columns)
    requested = list(recipe.get("base_features") or [])

    base_cols: List[str] = []
    python_feats: List[str] = []
    dropped: List[str] = []

    def _safe_add(unique_list: List[str], name: str) -> None:
        if name not in unique_list:
            unique_list.append(name)

    for name in requested:
        fdef = registry.get(name)
        if fdef is not None:
            # 1) Registry 里有定义
            if fdef.kind == "python":
                # python 型特征，必须在标准 python 特征列表里存在
                if name in PYTHON_DERIVED_FEATURES:
                    _safe_add(python_feats, name)
                    # 确保它依赖的 base_cols 在 df 中存在
                    missing_base = [c for c in fdef.base_cols if c not in available_set]
                    if missing_base:
                        dropped.append(name)
                        # 缺基础列，暂时放弃该特征
                        python_feats.remove(name)
                    else:
                        for c in fdef.base_cols:
                            _safe_add(base_cols, c)
                else:
                    # Registry 标记为 python，但我们目前还没有对应实现
                    dropped.append(name)
            else:
                # native 型特征，要求 df 中有同名列
                if name in available_set:
                    _safe_add(base_cols, name)
                else:
                    dropped.append(name)
        else:
            # 2) Registry 没有定义，fallback 策略
            if name in PYTHON_DERIVED_FEATURES:
                _safe_add(python_feats, name)
            elif name in available_set:
                _safe_add(base_cols, name)
            else:
                dropped.append(name)

    # SQL 里已经算出来的 derived 特征（expr AS alias）
    derived_aliases = [
        d.get("alias")
        for d in recipe.get("derived", []) or []
        if d.get("alias")
    ]

    # 最终特征列表 = 原始列 + python 特征 + derived 列
    final_features: List[str] = []
    for n in base_cols:
        _safe_add(final_features, n)
    for n in python_feats:
        _safe_add(final_features, n)
    for n in derived_aliases:
        _safe_add(final_features, n)

    return FeatureMaterializationPlan(
        base_columns=base_cols,
        python_features=python_feats,
        final_feature_list=final_features,
        dropped_features=dropped,
    )
```

> 说明：
> 目前这个 planner 在 **SQL 之后**使用：
>
> * SQL 仍然可以简单地把表里所有常用列查出来（或你原来的逻辑）；
> * planner 只负责：在 df 中决定“真正用哪些列 + 哪些 python 特征要算”。

---

### 2. 把 Planner 接到 `PrincipalInvestigator`

我们在上一次版本的 `PrincipalInvestigator.propose` 里已经：

* 用 Registry 生成 LLM 的特征摘要；
* 用 `filter_valid_derived` 做 SQL 沙箱。

现在再加一小段，把 planner 接上去，统一决定：

* 要算哪些 python 特征
* 最终特征子集

**关键修改点**（在你当前的 `PrincipalInvestigator` 基础上补充）：

```python
# pi/principal_investigator.py

from features.planner import plan_from_recipe   # NEW
```

在 `df = self._client.query_df(sql)` 之后，补：

```python
        # === 3) 根据 Registry + 实际列，生成特征物化计划 ===
        materialization_plan = plan_from_recipe(
            recipe=recipe,
            registry=registry,
            available_columns=df.columns.tolist(),
        )

        # 记录一下被丢掉的特征，方便以后追踪
        dropped_features = materialization_plan.dropped_features or []

        # === 4) 物化标准 Python 特征 ===
        if materialization_plan.python_features:
            df = add_standard_features(
                df,
                feats=materialization_plan.python_features,
            )

        # === 5) 附加 label（仍然在 Python 层，防泄露） ===
        label_id = tplan.get("label_id", default_label_id)
        df = attach_label(
            df,
            label_id=label_id,
            horizon=horizon,
        )

        # 用我们计划好的最终特征列表 + label 列构建训练用 df
        feature_subset = materialization_plan.final_feature_list
        label_col = label_id  # attach_label 里生成的列名约定为 label_id

        # 保证特征集合里没有 label 本身
        feature_subset = [c for c in feature_subset if c != label_col]

        dataset_cols = feature_subset + [label_col]
        df_train = df[dataset_cols].copy()

        # === 6) 体检 (Dataset Sanity) ===
        sanity_summary = run_dataset_sanity(df_train)

        # === 7) 构建 HPO grid（feature_subset 用 planner 的结果） ===
        grid = {
            "model_type": [],
            "feature_subset": [feature_subset],
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

        # === 8) 创建并保存 manifest（加上 registry / planner 信息） ===
        manifest = create_manifest(
            symbol=symbol,
            freq=freq,
            label_id=label_id,
            start_ts=start_ts,
            end_ts=end_ts,
            data_source={
                "table": table,
                "use_table": recipe.get("use_table", table),
                "clickhouse": self._ch_cfg,
            },
            feature_recipe=recipe,
            training_plan=tplan,
            sanity_summary=sanity_summary,
            meta={
                "sql": sql,
                "feature_catalog": catalog,
                "python_derived_features": materialization_plan.python_features,
                "feature_subset_effective": feature_subset,
                "registry_version": registry.version,
                "sandbox_dropped_derived": dropped_derived,
                "planner_dropped_features": dropped_features,
            },
        )
        save_manifest(manifest)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df_train,
            hpo_grid=grid_sane,
            sanity_summary=sanity_summary,
            manifest=manifest,
        )
```

> 上面我顺带把 `PIResult` 里加了 `sanity_summary` 和 `manifest` 字段（你需要更新一下 `@dataclass PIResult` 定义，把这两个字段加上）。

---

## 二、AI 大脑执行上下文：Prompt Library + Context Builder

接下来是这一轮的第二部分：
给“执行大脑”（你贴的 `v3_*`、`v4_experimental` 那个 prompt 库）一个**统一入口**和**上下文构造器**，以后只要给它一个 `execution_prompt_id`，就能自动拿到：

* 对应的执行风格 prompt（比如 conservative / fast_market_following）
* 加上结构化的环境信息（市场、风险门槛、数据质量、模型表现）

### 1. Prompt 库加载器：`brain/prompt_library.py`

```python
# brain/prompt_library.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict
import json


@dataclass
class PromptLibrary:
    raw: Dict[str, Any]
    prompts: Dict[str, str]   # prompt_id -> prompt text
    meta: Dict[str, Any]      # prompt_id -> meta dict（如果有）


    def get(self, prompt_id: str) -> str:
        """
        返回对应 prompt 文本；如果不存在，抛异常，避免静默 fallback。
        """
        try:
            return self.prompts[prompt_id]
        except KeyError:
            raise KeyError(f"Prompt id '{prompt_id}' not found in library")


    def get_meta(self, prompt_id: str) -> Dict[str, Any]:
        meta = self.meta.get(prompt_id)
        return meta or {}


def load_prompt_library(path: str | Path) -> PromptLibrary:
    """
    加载你贴的那种 prompt JSON:

    {
      "_comment": "...",
      "_usage": "...",
      "v3_stable": "....",
      "v3_fast_market_following": "....",
      ...
      "prompts": {
        "v3_fast_market_following_tactical_20251102": "....",
        ...
      },
      "meta": {
        "v3_fast_market_following_tactical_20251102": {...},
        ...
      }
    }
    """
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))

    prompts: Dict[str, str] = {}

    # 1) 顶层的 prompt（v3_stable / conservative / aggressive_scalper 等）
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if k in ("prompts", "meta"):
            continue
        if isinstance(v, str):
            prompts[k] = v

    # 2) 嵌套在 "prompts" 下的演进版本
    nested = raw.get("prompts") or {}
    for k, v in nested.items():
        if isinstance(v, str):
            prompts[k] = v

    meta_raw = raw.get("meta") or {}

    return PromptLibrary(
        raw=raw,
        prompts=prompts,
        meta=meta_raw,
    )
```

---

### 2. AI 大脑上下文构造器：`brain/context_builder.py`

这个模块负责**把所有你想让大脑看到的“眼睛信息”拼成一条 user_prompt**。

设计思想：

* **system_prompt**：纯粹描述“你是谁 + 你该怎么输出（JSON only + schema）”，直接用 `PromptLibrary.get(prompt_id)`，再加一两句说明后面会给它结构化上下文。
* **user_prompt**：按区块给它喂信息：

  * `[Market State]` 当前标的的价格 / 波动 / 重要因子
  * `[Risk Gates]` 今天的风险门槛（比如 max position, RVOL 阈值等）
  * `[Dataset Quality]` 来自我们刚才的 `sanity_summary`
  * `[Experiment / Model Summary]` 来自 manifest（如 Sharpe / IC / 最近回测结果）
  * `[Feature Registry Snapshot]` 当前 freq 下推荐使用的特征简表

**新文件**：`brain/context_builder.py`

```python
# brain/context_builder.py

from __future__ import annotations

from typing import Any, Dict, Optional

from brain.prompt_library import PromptLibrary
from features.registry import FeatureRegistry


def _format_dict_block(title: str, data: Optional[Dict[str, Any]]) -> str:
    if not data:
        return f"[{title}]\n- n/a\n"
    lines = [f"[{title}]"]
    for k, v in data.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) + "\n"


def _format_list_block(title: str, data: Optional[list]) -> str:
    if not data:
        return f"[{title}]\n- n/a\n"
    lines = [f"[{title}]"]
    for item in data:
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def build_brain_prompts(
    *,
    prompt_lib: PromptLibrary,
    prompt_id: str,
    symbol: str,
    now_ts: str,
    freq: str,
    horizon: str,
    market_snapshot: Dict[str, Any],
    risk_gates: Dict[str, Any],
    dataset_sanity: Optional[Dict[str, Any]] = None,
    experiment_summary: Optional[Dict[str, Any]] = None,
    registry: Optional[FeatureRegistry] = None,
) -> Dict[str, str]:
    """
    构造给执行大脑的 system_prompt / user_prompt（两段字符串）。

    - prompt_id: 比如 "v3_fast_market_following_tactical_20251117"
    - market_snapshot: 一般由上游整理好，比如:
        {
          "last_price": 612.5,
          "change_pct": -0.8,
          "rvol_20": 2.3,
          "session": "RTH",
          ...
        }
    - risk_gates: 今天启用的所有硬门槛 & 软参数
    - dataset_sanity: 来自训练数据集的主要质量结论（status / warnings / etc）
    - experiment_summary: 来自 manifest / 回测，包含模型性能指标等
    """
    base_prompt = prompt_lib.get(prompt_id)

    # ===== system_prompt =====
    system_prompt = (
        base_prompt
        + "\n\n"
        "You will receive structured context in the user message, with sections:\n"
        "[Market State], [Risk Gates], [Dataset Quality], [Experiment Summary], [Feature Registry].\n"
        "Use this information to make a risk-aware intraday decision.\n"
        "Always obey risk gates. Always output JSON only, following the agreed schema."
    )

    # ===== user_prompt: 拼接各个部分 =====
    parts = []

    # 0) 基本描述
    header = (
        f"Symbol: {symbol}\n"
        f"Now (exchange or NY time): {now_ts}\n"
        f"Primary bar frequency: {freq}\n"
        f"Decision horizon: {horizon}\n\n"
    )
    parts.append(header)

    # 1) 市场状态
    parts.append(_format_dict_block("Market State", market_snapshot))

    # 2) 风险门槛
    parts.append(_format_dict_block("Risk Gates", risk_gates))

    # 3) 数据集质量
    parts.append(_format_dict_block("Dataset Quality", dataset_sanity))

    # 4) 历史实验 / 模型表现
    parts.append(_format_dict_block("Experiment Summary", experiment_summary))

    # 5) Feature Registry 概览
    if registry is not None:
        registry_text = registry.summarize_for_llm(freq=freq)
        parts.append("[Feature Registry]\n" + registry_text + "\n")
    else:
        parts.append("[Feature Registry]\n- n/a (no registry loaded)\n")

    user_prompt = "\n".join(parts).strip()

    return {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
    }
```

> 你上游只要准备好几块信息（`market_snapshot` / `risk_gates` / `dataset_sanity` / `experiment_summary`），
> 再加一个 `execution_prompt_id`，就可以直接调用：

```python
from brain.prompt_library import load_prompt_library
from brain.context_builder import build_brain_prompts
from features.registry import get_default_registry

prompt_lib = load_prompt_library("config/prompt_library.json")
registry = get_default_registry("config")

ctx = build_brain_prompts(
    prompt_lib=prompt_lib,
    prompt_id="v3_fast_market_following_tactical_20251117",
    symbol="NVDA",
    now_ts="2025-11-17T09:35:15-05:00",
    freq="1m",
    horizon="10m",
    market_snapshot=market_snapshot_dict,
    risk_gates=risk_gates_dict,
    dataset_sanity=sanity_summary_dict,      # 可以直接用 PIResult.sanity_summary
    experiment_summary=experiment_summary_dict,
    registry=registry,
)

llm_result = llm_client.generate(
    system_prompt=ctx["system_prompt"],
    user_prompt=ctx["user_prompt"],
)
```

---

## 三、这一轮完成后，你的系统多了什么“硬升级”？

1. **特征路径更清晰：**

   * LLM 的 `feature_recipe` 不再直接决定“模型看到的是什么”，而是先经过：

     * Registry（哪些是推荐特征，哪些是 python 特征）
     * Planner（数据里到底有哪些列、哪些要算、哪些被丢弃）
   * 有任何不合法 / 不存在的特征，都会进入 `planner_dropped_features`，写到 manifest 里，方便你回溯。

2. **AI 大脑的“眼睛”升级：**

   * 不再只拿到一堆数字就开始瞎操作；
   * 每次调用执行 LLM 时，都可以统一塞给它：

     * 市场状态
     * 风控门槛
     * 这套模型训练数据的质量概况
     * 这套模型过去的回测表现
     * 可用特征注册表摘要

3. **未来扩展空间：**

   * 你可以很容易在 `experiment_summary` 里加：

     * 最近 N 天 out-of-sample Sharpe / MDD / hit rate
     * 当前策略属于哪个 regime（trend / mean-revert / choppy）
   * 也可以在 `risk_gates` 里统一控制“今天是否允许开新仓”、“今天最大杠杆”等。

---

下一步（你再说一次“继续”时），我会：

* 在 `experiment_summary` 这块设计一个**标准 schema** + 一个从 manifest 里抽取摘要的工具模块（比如 `experiments/summary.py`），自动把 IC / Sharpe / MDD / 交易次数等信息压缩成几条给 LLM 的自然语言 + 数值；
* 给 **HPO / Backtest 流水线** 加一个统一的“结果回写到 manifest + experiment index”的步骤，让 AI 大脑能读到“历史实验表现”，真正做到“基于过去表现选择模型 / 策略”。

你这一步只需要把上面的文件按路径放进去就行。准备好之后，可以再回一句：**“继续”**，我就把“实验摘要 + 历史表现索引 + 大脑自动选模型”的那层再落地。
