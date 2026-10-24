好，按我们的路线图，现在开始落地 **“特征注册表 + 安全 SQL 视图 + 沙箱特征”**，并且直接接进你现有的 `PrincipalInvestigator` / `build_select_sql` 流程。

这一步的目标是：

* 不再让 LLM 直接“乱写列名和 SQL”
* 让 AI 在一个**受控的 Feature Registry** 里“点餐”
* 同时保留一点“创意空间”：**沙箱特征 sandbox features**，但有严格 SQL 审核

我会给你几块代码，你可以按模块直接落盘：

---

## 一、Feature Registry：给所有特征一个“身份证”

**新文件：`features/registry.py`**

```python
# features/registry.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import json


@dataclass
class FeatureSpec:
    """
    注册表里的一条特征定义。

    - id:      在 AI / 配置 / 日志中使用的稳定 ID（不要随便改）
    - name:    人类可读名字（可选）
    - expr_sql: 用在 ClickHouse SELECT 里的表达式（不含 AS）
    - alias:   SELECT 出来的列名（训练用特征名）
    - source_table: 从哪个表/视图读取（默认使用 orchestrator 传入的 table）
    - dtype:   粗类型: "float", "int", "bool", "category", "string"
    - tags:    用来分组/过滤，比如 ["price", "intraday", "volatile"]
    - description: 解释含义，帮助 AI 选特征
    - is_sandbox:   是否为“沙箱特征”（由 LLM 提出、自动注册的临时特征）
    """
    id: str
    expr_sql: str
    alias: str

    name: Optional[str] = None
    source_table: Optional[str] = None
    dtype: str = "float"
    tags: Optional[List[str]] = None
    description: Optional[str] = None
    is_sandbox: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class FeatureRegistry:
    """
    核心特征注册表管理器。

    - 从 JSON 文件加载静态特征
    - 动态插入/管理“沙箱特征”
    - 提供按 id / alias / tag 查询接口
    """

    def __init__(self, registry_path: Path) -> None:
        self._registry_path = registry_path
        self._features_by_id: Dict[str, FeatureSpec] = {}
        self._features_by_alias: Dict[str, FeatureSpec] = {}

        if registry_path.exists():
            self._load()
        else:
            # 初始为空，允许后面通过 add_feature 动态添加
            pass

    # ---------- load/save ----------

    def _load(self) -> None:
        with self._registry_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        items = raw.get("features", [])
        for item in items:
            spec = FeatureSpec(**item)
            self._register_spec(spec)

    def save(self) -> None:
        """
        将当前注册表写回 JSON。
        建议只在人工审核后更新正式表；
        沙箱特征可以写入单独的文件（比如 sandbox_feature_registry.json）。
        """
        data = {
            "features": [spec.to_dict() for spec in self._features_by_id.values()]
        }
        tmp_path = self._registry_path.with_suffix(".tmp.json")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp_path.replace(self._registry_path)

    # ---------- query ----------

    def _register_spec(self, spec: FeatureSpec) -> None:
        if spec.id in self._features_by_id:
            raise ValueError(f"Duplicate feature id: {spec.id}")
        if spec.alias in self._features_by_alias:
            raise ValueError(f"Duplicate feature alias: {spec.alias}")
        self._features_by_id[spec.id] = spec
        self._features_by_alias[spec.alias] = spec

    def add_feature(self, spec: FeatureSpec) -> None:
        """
        外部调用：新增一条特征（可用于沙箱特征）。
        """
        self._register_spec(spec)

    def get_by_id(self, feature_id: str) -> FeatureSpec:
        return self._features_by_id[feature_id]

    def get_by_alias(self, alias: str) -> FeatureSpec:
        return self._features_by_alias[alias]

    def has_id(self, feature_id: str) -> bool:
        return feature_id in self._features_by_id

    def has_alias(self, alias: str) -> bool:
        return alias in self._features_by_alias

    def list_all(self) -> List[FeatureSpec]:
        return list(self._features_by_id.values())

    def list_static(self) -> List[FeatureSpec]:
        return [f for f in self._features_by_id.values() if not f.is_sandbox]

    def list_sandbox(self) -> List[FeatureSpec]:
        return [f for f in self._features_by_id.values() if f.is_sandbox]

    def list_by_tags(self, required_tags: List[str]) -> List[FeatureSpec]:
        rs = []
        req = set(required_tags)
        for spec in self._features_by_id.values():
            tags = set(spec.tags or [])
            if req.issubset(tags):
                rs.append(spec)
        return rs

    def to_llm_catalog(self) -> List[Dict[str, Any]]:
        """
        提供给 LLM Prompt 使用的“可用特征列表”，
        避免直接暴露底层物理列名。
        """
        items = []
        for spec in self.list_static():
            items.append(
                {
                    "id": spec.id,
                    "alias": spec.alias,
                    "dtype": spec.dtype,
                    "tags": spec.tags or [],
                    "description": spec.description or "",
                }
            )
        return items
```

### 一个简单的 `feature_registry.json` 样例

你可以先放一个最小版本，慢慢扩充：

```json
{
  "_comment": "Central Feature Registry for intraday models",
  "features": [
    {
      "id": "price_close",
      "name": "Close Price",
      "expr_sql": "close",
      "alias": "close",
      "dtype": "float",
      "tags": ["price", "base"],
      "description": "Last traded price for the bar",
      "is_sandbox": false
    },
    {
      "id": "price_vwap",
      "name": "VWAP",
      "expr_sql": "vwap",
      "alias": "vwap",
      "dtype": "float",
      "tags": ["price", "vwap", "base"],
      "description": "Volume-weighted average price for the bar",
      "is_sandbox": false
    },
    {
      "id": "micro_ret_1m",
      "name": "1m return",
      "expr_sql": "close / lagInFrame(close, 1) - 1",
      "alias": "ret_1m",
      "dtype": "float",
      "tags": ["return", "short_horizon"],
      "description": "1-minute logless return",
      "is_sandbox": false
    }
  ]
}
```

> 后面你只要往这里持续加特征，就会越来越接近“机构级特征库”。

---

## 二、沙箱特征：给 AI 一点创作空间，但必须过审

**新文件：`features/sandbox.py`**

目的：

* 接收 PI 里 LLM 提出的 `derived` 特征
* 做**安全检查**（只允许用 registry 中的 alias / whitelist 函数）
* 自动包装成 `FeatureSpec(is_sandbox=True)`

```python
# features/sandbox.py

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from .registry import FeatureRegistry, FeatureSpec

# 允许 LLM 使用的 SQL 函数白名单（可以逐步扩充）
ALLOWED_FUNCS = {
    "abs",
    "log",
    "ln",
    "exp",
    "sqrt",
    "greatest",
    "least",
    "if",
    "multiIf",
    "coalesce",
    "lagInFrame",
    "leadInFrame",
    "avg",
    "sum",
    "min",
    "max",
}

# 简单解析 token 的正则，用来抓出候选列名/函数名
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass
class SandboxValidationResult:
    ok: bool
    reason: str = ""
    used_columns: List[str] = None
    used_funcs: List[str] = None


def validate_sandbox_expr(
    expr_sql: str,
    *,
    allowed_columns: List[str],
) -> SandboxValidationResult:
    """
    极简 SQL 安全检查：
    - 提取所有标识符
    - 识别疑似函数名（后面紧跟 '(' 的）
    - 剩下视为列名，要求都在 allowed_columns 中
    - 函数必须在 ALLOWED_FUNCS 白名单内
    """
    tokens = IDENT_RE.findall(expr_sql)
    used_funcs = set()
    used_cols = set()

    # 用一个简陋方法：根据字符串搜索 func( 来区分函数和列
    expr_lower = expr_sql.lower()
    for tok in tokens:
        tok_l = tok.lower()
        # 是函数？
        if f"{tok_l}(" in expr_lower:
            used_funcs.add(tok_l)
        else:
            used_cols.add(tok)

    # 检查函数
    illegal_funcs = [f for f in used_funcs if f not in {f.lower() for f in ALLOWED_FUNCS}]
    if illegal_funcs:
        return SandboxValidationResult(
            ok=False,
            reason=f"Illegal functions used in sandbox expr: {illegal_funcs}",
            used_columns=sorted(used_cols),
            used_funcs=sorted(used_funcs),
        )

    # 检查列
    allowed_set = set(allowed_columns)
    illegal_cols = [c for c in used_cols if c not in allowed_set]
    if illegal_cols:
        return SandboxValidationResult(
            ok=False,
            reason=f"Unknown columns in sandbox expr: {illegal_cols}, allowed={sorted(allowed_columns)}",
            used_columns=sorted(used_cols),
            used_funcs=sorted(used_funcs),
        )

    return SandboxValidationResult(
        ok=True,
        reason="ok",
        used_columns=sorted(used_cols),
        used_funcs=sorted(used_funcs),
    )


def register_sandbox_features_from_recipe(
    registry: FeatureRegistry,
    recipe: Dict[str, Any],
    *,
    base_table: str,
    allowed_columns: List[str],
    sandbox_prefix: str = "sb_",
) -> List[FeatureSpec]:
    """
    从 PI 提出的 feature_recipe 中提取 derived 特征，
    经校验后注入 registry，返回成功注册的沙箱特征列表。

    约定：recipe["derived"] 是一个 list，每个元素类似：
      {
        "id": "sb_spread_ret",
        "expr_sql": "(close - vwap) / vwap",
        "alias": "spread_ret",
        "description": "..."
      }
    如果没有 id，则自动生成 "sb_<alias>"
    """
    derived = recipe.get("derived") or []
    registered: List[FeatureSpec] = []

    for item in derived:
        expr_sql = item.get("expr_sql")
        alias = item.get("alias")
        if not expr_sql or not alias:
            # 忽略无效定义
            continue

        # 校验
        v = validate_sandbox_expr(expr_sql, allowed_columns=allowed_columns)
        if not v.ok:
            # 忽略非法沙箱特征（也可以选择抛异常，看你调度策略）
            # 这里建议只是 log 警告，继续下一个
            # logger.warning("Sandbox feature rejected: %s", v.reason)
            continue

        fid = item.get("id") or f"{sandbox_prefix}{alias}"
        # 避免覆盖已有正式特征
        if registry.has_id(fid) or registry.has_alias(alias):
            # 也可以在这里自动重命名 alias/fid，视你喜好
            # 暂时直接跳过
            continue

        spec = FeatureSpec(
            id=fid,
            expr_sql=expr_sql,
            alias=alias,
            source_table=item.get("source_table", base_table),
            dtype=item.get("dtype", "float"),
            tags=(item.get("tags") or []) + ["sandbox"],
            description=item.get("description"),
            is_sandbox=True,
        )
        registry.add_feature(spec)
        registered.append(spec)

    return registered
```

> 这样：
>
> * PI 可以继续输出 `recipe["derived"]`；
> * 我们用 `register_sandbox_features_from_recipe` 过滤 + 注入；
> * 训练时，沙箱特征和正式特征一样使用，只是打了 `is_sandbox=True` 标记；
> * 将来你觉得某个沙箱特征好用，可以手动拷贝到 `feature_registry.json` 变成“正式员工”。

---

## 三、升级 `build_select_sql`：由 FeatureRegistry 驱动的安全 SQL

现在我们替换/升级你现有的 `pi/feature_sql.py`，让它：

* 根据 **feature IDs 或 alias** 从 Registry 找 SQL 表达式；
* 自动拼 SELECT；
* 过滤到 `symbol` & `time_range`；
* 确保 label/基础列也在。

**新版本示意：`pi/feature_sql.py`**

```python
# pi/feature_sql.py

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from features.registry import FeatureRegistry, FeatureSpec


def _resolve_feature_specs(
    registry: FeatureRegistry,
    *,
    recipe: Dict[str, Any],
    feature_catalog_aliases: List[str],
) -> List[FeatureSpec]:
    """
    把 PI 的 feature_recipe 转换成 FeatureSpec 列表。

    支持两种输入方式（兼容你现有结构）：
    1) recipe["feature_ids"] = ["price_close", "price_vwap", ...]
    2) recipe["base_features"] = ["close", "vwap", ...]  # 直接给 alias/原列名

    feature_catalog_aliases: 当前底层表中可用的物理列/alias，用来兜底检查。
    """

    specs: List[FeatureSpec] = []

    # 优先使用 feature_ids
    ids = recipe.get("feature_ids") or []
    for fid in ids:
        if registry.has_id(fid):
            specs.append(registry.get_by_id(fid))

    # 再看 base_features（通常是原始列名或 alias）
    base_feats = recipe.get("base_features") or []
    for alias in base_feats:
        # 如果已经通过 id 添加过，就跳过
        if any(f.alias == alias for f in specs):
            continue
        if registry.has_alias(alias):
            specs.append(registry.get_by_alias(alias))
        else:
            # 如果 registry 没有，但底层表有同名列，可以视为“直通列”
            # 注意：这属于“半开放特征”，建议逐步转为 registry 管理
            if alias in feature_catalog_aliases:
                specs.append(
                    FeatureSpec(
                        id=f"raw_{alias}",
                        expr_sql=alias,
                        alias=alias,
                        dtype="float",
                        tags=["raw_passthrough"],
                        description=f"Raw passthrough column {alias}",
                        is_sandbox=False,
                    )
                )
            else:
                # 直接忽略未知列，避免 SQL 报错
                # logger.warning("Unknown base feature alias %s, skip", alias)
                continue

    return specs


def build_select_sql(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    target: Optional[Dict[str, Any]],
    feature_catalog: List[str],
    registry: FeatureRegistry,
    time_col: str = "ts",
    symbol_col: str = "symbol",
) -> str:
    """
    基于 FeatureRegistry + feature_recipe 构建 SELECT SQL。

    - table:       ClickHouse 源表/视图名
    - symbol:      训练标的
    - start_ts/end_ts:  时间范围（ISO 字符串）
    - recipe:      PI 返回的 feature_recipe（已经过沙箱注册）
    - target:      PI 返回的 training_plan["target"] 定义（这里只用来决定 label 列是否需要 join/compute）
    - feature_catalog:  当前表存在的物理列名列表（来自 _fetch_catalog）
    - registry:    中央 FeatureRegistry 实例
    """

    # 1) 解析特征列表
    feature_specs = _resolve_feature_specs(
        registry,
        recipe=recipe,
        feature_catalog_aliases=feature_catalog,
    )

    # 2) 基础列：时间 + symbol 必须带上
    select_exprs: List[str] = [
        f"{time_col} AS {time_col}",
        f"{symbol_col} AS {symbol_col}",
    ]

    # 3) 特征列
    for spec in feature_specs:
        # 注意：这里使用 expr_sql AS alias
        select_exprs.append(f"{spec.expr_sql} AS {spec.alias}")

    # 4) target/label 列（如果需要在 SQL 层直接构造）
    # 建议大部分 label 在 Python 层 compute，避免 label 泄露；
    # 但你也可以在这里支持一些 safe 的 forward return 计算（严格用 lag / time window）
    if target:
        # 例如: {"mode": "horizon_return", "horizon_bars": 10, "col": "close", "alias": "ret_10"}
        mode = target.get("mode")
        if mode == "precomputed":
            # 表中已有 label 列
            label_col = target.get("col")
            alias = target.get("alias", label_col)
            if label_col in feature_catalog:
                select_exprs.append(f"{label_col} AS {alias}")

        # 其他复杂 label 建议留给 LabelEngine 统一做

    # 5) WHERE 过滤
    where_clause = (
        f"{symbol_col} = '{symbol}' "
        f"AND {time_col} >= toDateTime('{start_ts}') "
        f"AND {time_col} < toDateTime('{end_ts}')"
    )

    # 6) 拼完整 SQL
    select_sql = ",\n    ".join(select_exprs)
    sql = f"""
SELECT
    {select_sql}
FROM {table}
WHERE {where_clause}
ORDER BY {time_col} ASC
"""
    return sql.strip()
```

> 和你原来的 `build_select_sql` 接口保持兼容（多了一个 `registry` 参数），
> 但内部完全转成 **FeatureRegistry 驱动**。

---

## 四、把 FeatureRegistry 接进 PrincipalInvestigator

现在我们回到你给的文件：

```python
class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(self, ch_cfg: Dict[str, Any], llm_cfg: Optional[Dict[str, Any]] = None) -> None:
        ...
```

我们在里面加上：

* 加载 FeatureRegistry
* 在 prompt 中给大脑看的是 **feature_catalog_for_llm**（有描述）
* 拿到 LLM 的 `plan` 后：

  * 先用 `register_sandbox_features_from_recipe` 注入沙箱特征
  * 再调用新的 `build_select_sql(..., registry=self._feature_registry)`

**修改版：**

```python
# pi/principal_investigator.py (基于你之前贴的代码改造)

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from pathlib import Path

import clickhouse_connect
import pandas as pd

from grid_guard import sanitize_and_constrain_grid
from llm.llm_client import LLMClient
from pi.feature_sql import build_select_sql
from features.registry import FeatureRegistry
from features.sandbox import register_sandbox_features_from_recipe


@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]


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
        feature_registry_path: Optional[str] = None,
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

        # ★ 中央 FeatureRegistry
        registry_path = feature_registry_path or "config/feature_registry.json"
        self._feature_registry = FeatureRegistry(Path(registry_path))

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
        label_id: Optional[str] = None,  # 预留给后面 LabelContract，用于 prompt
    ) -> PIResult:
        # 1) 底层 ClickHouse 物理列 catalog
        catalog = _fetch_catalog(self._client, table)

        # 2) 给 LLM 的特征列表：用 registry 里的“逻辑特征”而不是 raw 列
        feature_catalog_for_llm = self._feature_registry.to_llm_catalog()

        user_prompt = (
            f"目标: {goal}\n"
            f"标的: {symbol}\n"
            f"时间区间: {start_ts} ~ {end_ts}\n"
            f"预测窗口: {horizon}\n"
            f"可用特征列表(通过 id 或 alias 选择):\n"
            f"{feature_catalog_for_llm}\n"
        )
        if label_id:
            user_prompt += f"\n预定义标签: {label_id}\n"

        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。\n"
            "- feature_recipe 中优先使用 feature_ids 字段来引用特征注册表中的特征 id。\n"
            "- 如需新建派生特征，请在 derived 字段中提供 expr_sql 和 alias，我们会自动进行安全审核。\n"
            "- 不要直接写任意 SQL 访问未知列，只能使用给出的特征/列。"
        )
        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # 3) 把 derived 沙箱特征注入 registry（有严格 SQL 校验）
        register_sandbox_features_from_recipe(
            self._feature_registry,
            recipe,
            base_table=table,
            allowed_columns=catalog,  # 物理表中真实存在的列
        )

        # 4) 基于 FeatureRegistry 构建 SELECT SQL
        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=tplan.get("target"),
            feature_catalog=catalog,
            registry=self._feature_registry,
        )
        df = self._client.query_df(sql)

        # 5) HPO grid 生成逻辑保持不变（可以逐步升级）
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
        )
```

> 至此，PI → SQL 这一段已经变成：
> **LLM 只能对 FeatureRegistry 下指令 + 少量受审的沙箱特征**。
> 原来的“随便拼列名写 SQL”通道被关闭。

---

## 五、这一轮完成了什么？还有哪些 TODO？

这一轮我们已经**按计划**完成了：

1. ✅ 建立 `FeatureRegistry`：

   * 中央 JSON 驱动的特征库
   * 支持静态特征 + 沙箱特征
   * 对 LLM 暴露的是干净的特征目录（id/alias/description/tags）

2. ✅ 建立 `Sandbox` 安全层：

   * 对 LLM 提出的派生特征 expr_sql 做白名单函数 + 列校验
   * 合法的自动注册为 `is_sandbox=True`
   * 非法的被静默丢弃（或按你的需求改成 hard fail）

3. ✅ 重写 `build_select_sql`：

   * 用 FeatureRegistry 驱动 SELECT
   * 基础列 + 特征列 + 可选 label 列
   * 保留对原始 `base_features` 的兼容（通过 raw_passthrough）

4. ✅ 升级 `PrincipalInvestigator`：

   * 初始化时加载 FeatureRegistry
   * Prompt 中对 AI 展示的是**逻辑特征目录**
   * 收到 recipe 后自动注入沙箱特征，并用 Registry 构建 SQL

---

下一步（你再说一句“继续”，我就直接开做）我会：

* 把 **LabelContract + BacktestEngine-compatible LabelSpec** 串起来：

  * 统一标签的定义和 ID（比如 `ret_10m_hl_triple_barrier`）
  * 确保所有 label 都通过同一个 `LabelRegistry + LabelEngine` 计算
  * 让 AI 只能选 `label_id`，而不能自己发明 target 公式

这会把“标签”和你现有回测/实盘完全打通，彻底消掉 data corruption / label 泄露的风险。
好，那我们按路线图进入 **Step 2：LabelContract + LabelEngine**，把「标签」这块从现在的“随缘 + 容易泄露”变成**有契约、有注册表、有计算引擎**的标准组件。

这一轮我会做三件事：

1. 定义 **LabelRegistry**（像 FeatureRegistry 一样给标签一个“身份证”）
2. 定义 **LabelEngine**（统一在 Python 里算 label，防止 SQL label 泄露）
3. 把 **PI（PrincipalInvestigator）→ LabelRegistry → LabelEngine** 串起来

---

## 一、LabelRegistry：给每个标签一个“身份证”

> 目标：
>
> * 所有可用标签都有一个固定的 `id`
> * 明确 mode（`fwd_return` / `triple_barrier` 等）、参数、输出列名
> * AI 大脑只能选 `label_id`，禁止自己乱写 SQL 做 target

### 1. 新文件：`labels/registry.py`

```python
# labels/registry.py

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional
import json


@dataclass
class LabelSpec:
    """
    统一的标签定义（Label Contract）.

    - id:          稳定 ID，PI / Brain / Backtest 都通过这个引用
    - name:        人类可读的名字
    - mode:        计算模式，如:
                     - "fwd_return": 简单前瞻收益
                     - "triple_barrier": 三重栅栏
    - alias:       在 DataFrame 里的列名，例如 "y", "ret_10m", "tb_label"
    - target_type: "regression" / "classification"
    - params:      计算所需的参数，完全 mode-specific
    - description: 文档 / 提示给大脑使用
    """
    id: str
    mode: str
    alias: str
    target_type: str  # "regression" | "classification"
    params: Dict[str, Any]

    name: Optional[str] = None
    description: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class LabelRegistry:
    """
    中央标签注册表:
    - 从 JSON 加载 LabelSpec
    - 提供 by_id 查询
    - 提供给 LLM 的简化 catalog (id + 描述)
    """

    def __init__(self, registry_path: Path) -> None:
        self._registry_path = registry_path
        self._labels_by_id: Dict[str, LabelSpec] = {}

        if registry_path.exists():
            self._load()
        else:
            # 可以后面再写入
            pass

    def _load(self) -> None:
        with self._registry_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        items = raw.get("labels", [])
        for item in items:
            spec = LabelSpec(**item)
            if spec.id in self._labels_by_id:
                raise ValueError(f"Duplicate label id: {spec.id}")
            self._labels_by_id[spec.id] = spec

    def save(self) -> None:
        data = {
            "labels": [spec.to_dict() for spec in self._labels_by_id.values()]
        }
        tmp = self._registry_path.with_suffix(".tmp.json")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._registry_path)

    # ---- 查询 API ----

    def has_id(self, label_id: str) -> bool:
        return label_id in self._labels_by_id

    def get(self, label_id: str) -> LabelSpec:
        return self._labels_by_id[label_id]

    def list_all(self) -> List[LabelSpec]:
        return list(self._labels_by_id.values())

    def to_llm_catalog(self) -> List[Dict[str, Any]]:
        """
        提供给 LLM 使用的标签清单（不要暴露 params 细节，以免幻觉）
        """
        items = []
        for spec in self.list_all():
            items.append(
                {
                    "id": spec.id,
                    "name": spec.name or spec.id,
                    "mode": spec.mode,
                    "alias": spec.alias,
                    "target_type": spec.target_type,
                    "description": spec.description or "",
                }
            )
        return items
```

### 2. 一个合理的 `label_registry.json` 样例

你可以先放在 `config/label_registry.json`，后续慢慢扩展：

```json
{
  "_comment": "Central Label Registry for intraday ML / Backtest",
  "labels": [
    {
      "id": "ret_10m_simple",
      "name": "10-minute simple return",
      "mode": "fwd_return",
      "alias": "y_ret_10m",
      "target_type": "regression",
      "params": {
        "price_col": "close",
        "horizon_bars": 10,
        "kind": "simple",       // "simple" or "log"
        "clip_min": -0.3,
        "clip_max": 0.3
      },
      "description": "Simple forward return over 10 bars based on close price."
    },
    {
      "id": "tb_10m_2_1_class",
      "name": "Triple Barrier 10m (2% up, 1% down)",
      "mode": "triple_barrier",
      "alias": "y_tb_10m",
      "target_type": "classification",
      "params": {
        "price_col": "close",
        "max_horizon_bars": 10,
        "up_th": 0.02,
        "down_th": 0.01,
        "label_scheme": "long_1_0_-1",   // up=1, down=-1, timeout=0
        "allow_equal_touch": true
      },
      "description": "Path-dependent triple barrier label with 10-bar timeout."
    }
  ]
}
```

> 以后 BacktestEngine 也可以直接用同一个 `label_id`，避免“训练一种 label、回测另一种 label”的乱象。

---

## 二、LabelEngine：统一在 Python 里算标签（防泄露）

> 目标：
>
> * 所有 label 都在 DataFrame 层计算，不再让 LLM 或 SQL 直接写 target
> * 支持基础的前瞻收益 + 三重栅栏
> * 输出 label 列 + 基本统计（方便 sanity check 和 Brain 使用）

### 1. 新文件：`labels/engine.py`

```python
# labels/engine.py

from __future__ import annotations

from typing import Dict, Any, Tuple
import numpy as np
import pandas as pd

from .registry import LabelSpec


def _compute_fwd_return(
    df: pd.DataFrame,
    *,
    price_col: str,
    horizon_bars: int,
    kind: str = "simple",
    clip_min: float | None = None,
    clip_max: float | None = None,
) -> pd.Series:
    """
    简单前瞻收益: price[t+h] / price[t] - 1 或 log(price[t+h]/price[t]).
    注意: 最后 horizon_bars 行没有未来数据，会是 NaN（可在外层处理）
    """
    px = df[price_col].astype(float)
    fwd = px.shift(-horizon_bars)

    if kind == "log":
        ret = np.log(fwd / px)
    else:
        # 默认 simple return
        ret = fwd / px - 1.0

    if clip_min is not None or clip_max is not None:
        ret = ret.clip(lower=clip_min, upper=clip_max)

    return ret


def _compute_triple_barrier(
    df: pd.DataFrame,
    *,
    price_col: str,
    max_horizon_bars: int,
    up_th: float,
    down_th: float,
    allow_equal_touch: bool = True,
    label_scheme: str = "long_1_0_-1",
) -> pd.Series:
    """
    三重栅栏标签（简化实现）：

    对于每一行 i:
      - entry_price = close[i]
      - up_barrier = entry_price * (1 + up_th)
      - down_barrier = entry_price * (1 - down_th)
      - 在 (i, i+max_horizon] 区间中逐步检查价格是否触及任一 barrier
      - 先触及 up → "赢", 先触及 down → "亏", 都没触及直到 timeout → "超时"

    返回一个整数 label:
      - label_scheme="long_1_0_-1": up=1, down=-1, timeout=0
    """
    px = df[price_col].astype(float).values
    n = len(px)
    labels = np.full(n, np.nan, dtype=float)

    # 转换不同方案可以在这里扩展，这里先只实现 long_1_0_-1
    up_label, timeout_label, down_label = 1.0, 0.0, -1.0

    # 简单 for 循环版本（O(N * horizon)），先跑通，性能瓶颈以后可以优化 / 用 numba
    for i in range(n):
        entry = px[i]
        if not np.isfinite(entry):
            continue

        up_barrier = entry * (1.0 + up_th)
        down_barrier = entry * (1.0 - down_th)

        hit = None  # "up" / "down" / None
        last_idx = min(n - 1, i + max_horizon_bars)

        for j in range(i + 1, last_idx + 1):
            p = px[j]
            if not np.isfinite(p):
                continue

            if allow_equal_touch:
                if p >= up_barrier:
                    hit = "up"
                    break
                if p <= down_barrier:
                    hit = "down"
                    break
            else:
                if p > up_barrier:
                    hit = "up"
                    break
                if p < down_barrier:
                    hit = "down"
                    break

        if hit == "up":
            labels[i] = up_label
        elif hit == "down":
            labels[i] = down_label
        else:
            labels[i] = timeout_label

    return pd.Series(labels, index=df.index)


def compute_label_series(df: pd.DataFrame, spec: LabelSpec) -> pd.Series:
    """
    顶层入口：根据 LabelSpec 计算一列 label（不做填充/清洗，只算出来）
    """
    mode = spec.mode

    if mode == "fwd_return":
        p = spec.params
        return _compute_fwd_return(
            df,
            price_col=p.get("price_col", "close"),
            horizon_bars=int(p.get("horizon_bars", 10)),
            kind=p.get("kind", "simple"),
            clip_min=p.get("clip_min"),
            clip_max=p.get("clip_max"),
        )

    elif mode == "triple_barrier":
        p = spec.params
        return _compute_triple_barrier(
            df,
            price_col=p.get("price_col", "close"),
            max_horizon_bars=int(p.get("max_horizon_bars", 10)),
            up_th=float(p.get("up_th", 0.02)),
            down_th=float(p.get("down_th", 0.01)),
            allow_equal_touch=bool(p.get("allow_equal_touch", True)),
            label_scheme=p.get("label_scheme", "long_1_0_-1"),
        )

    else:
        raise ValueError(f"Unknown label mode: {mode}")


def attach_label_and_stats(
    df: pd.DataFrame,
    spec: LabelSpec,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    给 DataFrame 附加一列 label，并返回简单统计结果:
    - valid_count / nan_count
    - regression: mean/std/quantiles
    - classification: 各类占比
    """
    y = compute_label_series(df, spec)
    alias = spec.alias

    # 将 label 填回 df（保持不就地修改，返回新对象）
    df_with_label = df.copy()
    df_with_label[alias] = y

    stats: Dict[str, Any] = {
        "label_id": spec.id,
        "alias": alias,
        "target_type": spec.target_type,
        "total": int(len(df_with_label)),
        "valid": int(y.notna().sum()),
        "nan": int(y.isna().sum()),
    }

    # 进一步统计
    if spec.target_type == "regression":
        valid = y.dropna()
        if not valid.empty:
            stats.update(
                {
                    "mean": float(valid.mean()),
                    "std": float(valid.std()),
                    "p01": float(valid.quantile(0.01)),
                    "p05": float(valid.quantile(0.05)),
                    "p50": float(valid.quantile(0.5)),
                    "p95": float(valid.quantile(0.95)),
                    "p99": float(valid.quantile(0.99)),
                }
            )
    elif spec.target_type == "classification":
        counts = y.value_counts(dropna=True)
        total_valid = counts.sum()
        dist = {}
        for cls, cnt in counts.items():
            dist[str(cls)] = {
                "count": int(cnt),
                "ratio": float(cnt / total_valid) if total_valid > 0 else 0.0,
            }
        stats["class_dist"] = dist

    return df_with_label, stats
```

> 这样，所有 label 都是 **纯 Python 运算**，明确使用 `shift(-h)` 或 path-dependent 循环，
> 完全不再依赖 LLM 拼 target SQL，自然就不存在 label 泄露/瞎写的问题。

---

## 三、把 LabelRegistry + LabelEngine 接进 PI（PrincipalInvestigator）

现在我们升级你的 `PrincipalInvestigator`：

### 1. 扩展 `PIResult`，增加 label 信息（保持兼容）

之前的 dataclass 是：

```python
@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]
```

我们在 **后面追加可选字段**（不破坏原有调用）：

```python
# pi/principal_investigator.py

from dataclasses import dataclass
from typing import Optional

@dataclass
class PIResult:
    feature_recipe: Dict[str, Any]
    training_plan: Dict[str, Any]
    sql: str
    dataset_df: pd.DataFrame
    hpo_grid: Dict[str, Any]

    # 新增，可选
    label_id: Optional[str] = None
    label_spec: Optional[Dict[str, Any]] = None
    label_stats: Optional[Dict[str, Any]] = None
```

> 老代码继续用位置参数构造前 5 个字段不会出错；
> 新代码可以利用后面三个字段做日志 / 可视化 / Brain 输入。

### 2. 在 `PrincipalInvestigator.__init__` 中加载 LabelRegistry

基于你之前的 PI 代码，修改如下（重点部分标注为 `★`）：

```python
# pi/principal_investigator.py (节选)

from pathlib import Path
from labels.registry import LabelRegistry
from labels.engine import attach_label_and_stats

class PrincipalInvestigator:
    """LLM-driven feature + training plan generator."""

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        llm_cfg: Optional[Dict[str, Any]] = None,
        feature_registry_path: Optional[str] = None,
        label_registry_path: Optional[str] = None,   # ★ 新增
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

        # FeatureRegistry 保持之前版本
        registry_path = feature_registry_path or "config/feature_registry.json"
        self._feature_registry = FeatureRegistry(Path(registry_path))

        # ★ LabelRegistry
        label_path = label_registry_path or "config/label_registry.json"
        self._label_registry = LabelRegistry(Path(label_path))
```

### 3. 在 `propose` 里接入 label（先选 id，再算 label）

我们升级 `propose` 方法（只贴关键变化）：

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
        label_id: Optional[str] = None,  # 已经加过，这里真正用起来
    ) -> PIResult:
        # ---------- 1) 获取底层列名 + 给大脑的特征/标签清单 ----------

        catalog = _fetch_catalog(self._client, table)
        feature_catalog_for_llm = self._feature_registry.to_llm_catalog()
        label_catalog_for_llm = self._label_registry.to_llm_catalog()

        user_prompt = (
            f"目标: {goal}\n"
            f"标的: {symbol}\n"
            f"时间区间: {start_ts} ~ {end_ts}\n"
            f"预测窗口: {horizon}\n"
            f"可用特征列表(通过 id 或 alias 选择):\n"
            f"{feature_catalog_for_llm}\n"
            f"\n可用标签列表(通过 label_id 选择):\n"
            f"{label_catalog_for_llm}\n"
        )
        if label_id:
            user_prompt += f"\n推荐优先使用标签: {label_id}\n"

        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。\n"
            "- feature_recipe 中优先使用 feature_ids 字段来引用特征注册表中的特征 id。\n"
            "- 如需新建派生特征，请在 derived 字段中提供 expr_sql 和 alias，我们会自动进行安全审核。\n"
            "- 请选择一个 label_id 作为训练标签，必须来自提供的标签列表，不能自定义 target SQL。"
        )

        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)
        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # ---------- 2) 处理 label_id 选择逻辑 ----------

        # 允许调用方强制指定 label_id，否则由 LLM 提建议
        chosen_label_id = label_id or tplan.get("label_id")
        label_spec = None
        if chosen_label_id and self._label_registry.has_id(chosen_label_id):
            label_spec = self._label_registry.get(chosen_label_id)
        else:
            # 没选或选的不存在 → 先不算 label，后续可以在外层报错/降级
            chosen_label_id = None

        # ---------- 3) 派生沙箱特征注入 registry（和上一轮一样） ----------

        register_sandbox_features_from_recipe(
            self._feature_registry,
            recipe,
            base_table=table,
            allowed_columns=catalog,
        )

        # ---------- 4) 基于 FeatureRegistry 构建 SQL 并拉数据 ----------

        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=tplan.get("target"),   # 此处暂不让 LLM 构造 SQL target
            feature_catalog=catalog,
            registry=self._feature_registry,
        )
        df = self._client.query_df(sql)

        # ---------- 5) 如果有合法 label_id，则在 Python 层计算标签 ----------

        label_stats_dict: Dict[str, Any] = {}
        if label_spec is not None and not df.empty:
            df, label_stats_dict = attach_label_and_stats(df, label_spec)

            # 同步写回 training_plan 的 target 定义，明确 alias & 类型
            tplan["target"] = {
                "mode": "precomputed",
                "col": label_spec.alias,
                "alias": label_spec.alias,
                "target_type": label_spec.target_type,
                "label_id": label_spec.id,
            }

        # ---------- 6) 按原有逻辑生成 HPO grid（后续可以再增强） ----------

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
            "allow_params": [
                "model_type",
                "feature_subset",
                "task",
                "decision",
                "model_params",
            ]
        }
        grid_sane, _ = sanitize_and_constrain_grid(grid, guard)

        # ---------- 7) 返回 PIResult，带上 label 信息 ----------

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
            label_id=chosen_label_id,
            label_spec=label_spec.to_dict() if label_spec else None,
            label_stats=label_stats_dict or None,
        )
```

> 至此，**AI 大脑对“标签”的权限被硬控在 LabelRegistry + LabelEngine 之下**：
>
> * 它只能在 `label_registry.json` 定义的几种标签里选一个 `label_id`
> * 真正的标签计算全部在 Python 里完成，严格基于价格列和窗口参数
> * 不存在“乱写 SQL target”、“label 泄露”这一类坑

---

## 四、这一步落地后，你的系统在「标签 + 数据安全」上的变化

现在的状态 vs 升级后的状态对比：

### 之前

* LLM 可能在 plan 里写：

  * `"target": {"expr": "lead(close, 10) / close - 1", ...}`
  * 或者干脆拼一个 JOIN/窗口函数的 SQL
* 训练引擎直接用 LLM 提供的 target 定义去拼 SQL
* 你调试困难：

  * 有时候 target 算错
  * 有时候 label 泄露未来信息
  * 有时候根本不知道当前这一轮训练用的是什么标签

### 现在

* 所有标签行为都通过 `label_registry.json` 明确声明：
  `ret_10m_simple`, `tb_10m_2_1_class`, … 每个都写清楚：

  * `mode`
  * `price_col`
  * `horizon_bars` 或 `max_horizon_bars`
  * `up_th` / `down_th`
* AI 大脑只能**选 label_id**，不能写公式：

  * 不会再随便创造 “奇怪 target”
  * Backtest / Live 可以用 *同一个 label_id* 做验证
* LabelEngine 在 Python 层统一计算：

  * 你能在 Notebook 里单独调试 `attach_label_and_stats(df, spec)`
  * `label_stats` 报告清楚：

    * 回归：均值/方差/分位数（能看出是不是全 0 或超极端）
    * 分类：正/负/超时各类比例（能看到严重不平衡的 label）

---

## 五、下一步（预告）

现在我们已经：

1. ✅ DatasetSanity（上一轮）
2. ✅ FeatureRegistry + SandboxFeatures（上一轮）
3. ✅ LabelRegistry + LabelEngine（这一轮）
4. ✅ 把 Feature + Label 全部接入 PI 流程

**下一步（我会在你再说一句“继续”的时候直接开做）：**

* 升级 AI 大脑的「眼睛」和「工具」：

  * 用 `dataset_sanity` + `label_stats` + `feature_registry` + `label_registry` 构造一个**标准的“BrainContext”**：

    * 告诉大脑：

      * 哪些特征有问题（缺失、共线、极端值）
      * 当前标签分布（是不是极度不平衡 / 太噪）
      * 这组数据的时间段、标的、特征组
  * 让大脑在看到劣质数据时能够：

    * 主动拒绝训练
    * 或者自动调低风险/只做探索试验，而不是直接推实盘策略

同时，我会把这些能力绑定到一个 **`BrainTooling` 层**（类似工具箱）里，让：

* Brain 调用“体检工具”
* Brain 调用“特征建议工具”
* Brain 调用“标签列表工具”

形成你说的那种：**眼睛看到更多、更全的信息，工具更强、更皮实** 的形态。

你只要再回一句：**“继续”**，我就按这个方向开始给你设计 & 写出 `BrainContext` + `BrainTools` 的落地代码。
