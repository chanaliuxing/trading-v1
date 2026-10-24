好，那现在按计划推进到**“标签契约 / LabelRegistry + 真正机构级标签生成”**这一步，直接给你代码和集成方案，不问你问题。

接下来这轮我们做三件事：

1. 设计一个 **LabelRegistry（标签注册表）JSON 格式**。
2. 写两个模块：

   * `labels/registry.py`：加载标签配置、选择合适的 label。
   * `labels/builder.py`：在 **Python 里安全地计算标签列**（time-bar return + triple-barrier）。
3. 把它 **接到 PrincipalInvestigator.propose() 里**，彻底禁止 LLM 自己写 target SQL。

---

## 一、LabelRegistry JSON 结构（示例）

你可以新建一个：`config/label_registry.json`（路径你可以按你习惯调）：

```json
{
  "_comment": "Label Registry - Whitelist of approved labels for AI Brain",
  "_version": "1.0",
  "labels": {
    "ret_10m_fwd_log": {
      "kind": "time_return",
      "column_name": "y_ret_10m_fwd_log",
      "task": "reg",
      "params": {
        "horizon_bars": 10,
        "price_col": "close",
        "log_return": true
      },
      "required_columns": ["close"],
      "description": "10-bar forward log return of close, per-symbol, sorted by ts."
    },
    "ret_30m_fwd_log": {
      "kind": "time_return",
      "column_name": "y_ret_30m_fwd_log",
      "task": "reg",
      "params": {
        "horizon_bars": 30,
        "price_col": "close",
        "log_return": true
      },
      "required_columns": ["close"],
      "description": "30-bar forward log return of close."
    },
    "triple_10m_002_002_side": {
      "kind": "triple_barrier",
      "column_name": "y_triple_10m_002_002",
      "task": "clf",
      "params": {
        "horizon_bars": 10,
        "price_col": "close",
        "upper_pct": 0.02,
        "lower_pct": -0.02,
        "use_horizon_direction_if_no_touch": true
      },
      "required_columns": ["close"],
      "description": "Triple-barrier label with +2% / -2% barriers and 10-bar time limit, returns {-1, 0, 1}."
    }
  }
}
```

> 说明：
>
> * `kind`: 决定在 Python 里调用哪个打标函数 (`time_return` / `triple_barrier`)
> * `column_name`: 在 df 里生成的 target 列名（避免 LLM 瞎取名）
> * `task`: `"reg"` 或 `"clf"`，直接喂给 HPO / TrainingGate
> * `params`: horizon 等具体参数
> * `required_columns`: 该 label 计算需要哪些原始列（确保 SQL 一定选出来）

---

## 二、模块 1：`labels/registry.py`

新建目录 `labels/`，在里面放 `__init__.py` 和 `registry.py`。

### `labels/registry.py`

```python
# labels/registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class LabelSpec:
    """
    一个 label 的完整定义（来自 JSON 注册表）。
    """
    id: str
    kind: str               # "time_return" | "triple_barrier" | ...
    column_name: str        # 在 df 中的列名
    task: str               # "reg" | "clf"
    params: Dict[str, Any]
    required_columns: List[str]
    description: str = ""


class LabelRegistry:
    """
    负责加载 / 查询 LabelSpec。
    """

    def __init__(self, registry: Dict[str, Any]) -> None:
        self._raw = registry
        self._labels: Dict[str, LabelSpec] = {}

        labels = registry.get("labels") or {}
        for lid, cfg in labels.items():
            self._labels[lid] = LabelSpec(
                id=lid,
                kind=cfg["kind"],
                column_name=cfg["column_name"],
                task=cfg.get("task", "reg"),
                params=cfg.get("params", {}) or {},
                required_columns=list(cfg.get("required_columns") or []),
                description=cfg.get("description", ""),
            )

    @classmethod
    def from_file(cls, path: str | Path) -> "LabelRegistry":
        import json

        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    def get(self, label_id: str) -> LabelSpec:
        if label_id not in self._labels:
            raise KeyError(f"Unknown label_id={label_id!r} in LabelRegistry")
        return self._labels[label_id]

    def list_ids(self) -> List[str]:
        return sorted(self._labels.keys())

    def find_by_kind(self, kind: str) -> List[LabelSpec]:
        return [spec for spec in self._labels.values() if spec.kind == kind]

    def raw(self) -> Dict[str, Any]:
        return self._raw


# --- 简单的全局访问器（懒加载） ---

_default_registry: Optional[LabelRegistry] = None


def init_default_registry(path: str | Path) -> None:
    """
    在你的 app 启动时调用一次，用于指定默认 registry JSON 路径。
    """
    global _default_registry
    _default_registry = LabelRegistry.from_file(path)


def get_label_spec(label_id: str) -> LabelSpec:
    """
    获取默认注册表中的某个 LabelSpec。
    """
    if _default_registry is None:
        raise RuntimeError(
            "LabelRegistry is not initialized. "
            "Call labels.registry.init_default_registry(path) at startup."
        )
    return _default_registry.get(label_id)


def list_label_ids() -> List[str]:
    if _default_registry is None:
        raise RuntimeError(
            "LabelRegistry is not initialized. "
            "Call labels.registry.init_default_registry(path) at startup."
        )
    return _default_registry.list_ids()


def resolve_default_label_id(
    *,
    goal: str,
    horizon: str,
) -> str:
    """
    当 LLM 没有或给了错误的 label_id 时，使用一个规则 fallback。
    - 简化版：优先找 time_return，且 horizon 大致匹配，其次任意 time_return。
    - 如果 goal 强调方向/多空，就优先 triple_barrier。
    """
    if _default_registry is None:
        raise RuntimeError(
            "LabelRegistry is not initialized. "
            "Call labels.registry.init_default_registry(path) at startup."
        )

    reg = _default_registry

    # 简单判断是否是“方向/多空”类目标
    goal_lower = goal.lower()
    wants_directional = any(
        kw in goal_lower
        for kw in ["direction", "long/short", "go long", "go short", "多空", "方向"]
    )

    # 简单从 horizon 字符串里提取数字（比如 "10m" -> 10）
    import re

    m = re.match(r"(\d+)", str(horizon))
    horizon_int = int(m.group(1)) if m else None

    # 如果想要方向类标签，先找 triple_barrier
    if wants_directional:
        triple_labels = reg.find_by_kind("triple_barrier")
        if horizon_int is not None:
            for spec in triple_labels:
                h = int(spec.params.get("horizon_bars", -1))
                if h == horizon_int:
                    return spec.id
        if triple_labels:
            return triple_labels[0].id

    # 否则优先 time_return
    time_labels = reg.find_by_kind("time_return")
    if horizon_int is not None:
        for spec in time_labels:
            h = int(spec.params.get("horizon_bars", -1))
            if h == horizon_int:
                return spec.id
    if time_labels:
        return time_labels[0].id

    # 兜底：随便拿一个
    all_ids = reg.list_ids()
    if not all_ids:
        raise RuntimeError("LabelRegistry has no labels defined.")
    return all_ids[0]
```

> 集成方式：在你的主程序启动时（比如 orchestrator / HPO CLI），加一句：

```python
from labels.registry import init_default_registry

init_default_registry("config/label_registry.json")
```

之后任何地方就可以用 `get_label_spec("ret_10m_fwd_log")`。

---

## 三、模块 2：`labels/builder.py`（在 Python 里安全打标签）

新建 `labels/builder.py`：

```python
# labels/builder.py

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from .registry import LabelSpec


def _compute_time_return_per_symbol(
    df: pd.DataFrame,
    spec: LabelSpec,
    symbol_col: str,
    time_col: str,
) -> pd.Series:
    """
    对每个 symbol 内部，按 time_col 升序，计算 horizon_bars 后的 log-return / simple-return。
    """
    params = spec.params
    h = int(params.get("horizon_bars", 1))
    if h <= 0:
        raise ValueError(f"Invalid horizon_bars={h} for label {spec.id}")

    price_col = params.get("price_col", "close")
    use_log = bool(params.get("log_return", True))

    if price_col not in df.columns:
        raise KeyError(f"Label {spec.id} requires price_col={price_col!r}, but df has no such column")

    # 分组计算，避免跨 symbol 滚动
    labels_parts = []
    for sym, g in df.sort_values(by=[symbol_col, time_col]).groupby(symbol_col, sort=False):
        p = g[price_col].astype(float).to_numpy()
        n = len(p)
        y = np.full(shape=n, fill_value=np.nan, dtype="float64")

        if n > h:
            fwd = np.empty_like(p)
            fwd[:-h] = p[h:]
            fwd[-h:] = np.nan

            valid = ~np.isnan(fwd)
            if use_log:
                y[valid] = np.log(fwd[valid]) - np.log(p[valid])
            else:
                y[valid] = (fwd[valid] - p[valid]) / p[valid]

        s = pd.Series(y, index=g.index)
        labels_parts.append(s)

    out = pd.concat(labels_parts).reindex(df.index)
    return out


def _compute_triple_barrier_per_symbol(
    df: pd.DataFrame,
    spec: LabelSpec,
    symbol_col: str,
    time_col: str,
) -> pd.Series:
    """
    简化版 triple-barrier：
    - 以 close 为价格路径（如果你有 high/low，可以再升级）。
    - 对每一行 i，向前看 horizon_bars 步：
      * 如果价格先触达 upper_pct → label=+1
      * 如果先触达 lower_pct → label=-1
      * 否则在 horizon 结束时看终点相对起点方向 → label=+1/-1/0
    - 最后 horizon_bars 行因为未来不足，被标记为 NaN。
    """
    params = spec.params
    h = int(params.get("horizon_bars", 1))
    if h <= 0:
        raise ValueError(f"Invalid horizon_bars={h} for label {spec.id}")

    price_col = params.get("price_col", "close")
    if price_col not in df.columns:
        raise KeyError(f"Label {spec.id} requires price_col={price_col!r}, but df has no such column")

    upper_pct = float(params.get("upper_pct", 0.02))
    lower_pct = float(params.get("lower_pct", -0.02))
    use_horizon_dir = bool(params.get("use_horizon_direction_if_no_touch", True))

    labels_parts = []
    for sym, g in df.sort_values(by=[symbol_col, time_col]).groupby(symbol_col, sort=False):
        p = g[price_col].astype(float).to_numpy()
        n = len(p)
        y = np.full(shape=n, fill_value=np.nan, dtype="float64")

        for i in range(n):
            if i + 1 >= n:
                # 最后一行肯定不足 horizon，直接 NaN
                y[i] = np.nan
                continue

            entry = p[i]
            up_level = entry * (1.0 + upper_pct)
            down_level = entry * (1.0 + lower_pct)

            last_idx = min(n - 1, i + h)
            label_val: float = 0.0
            hit = False

            # 在 (i+1 .. last_idx) 之间找 first touch
            for j in range(i + 1, last_idx + 1):
                price_j = p[j]
                if price_j >= up_level:
                    label_val = 1.0
                    hit = True
                    break
                if price_j <= down_level:
                    label_val = -1.0
                    hit = True
                    break

            if not hit:
                # 没有触发上下轨，看 horizon 末尾方向
                if use_horizon_dir and last_idx > i:
                    ret = (p[last_idx] - entry) / entry
                    if ret > 0:
                        label_val = 1.0
                    elif ret < 0:
                        label_val = -1.0
                    else:
                        label_val = 0.0
                else:
                    label_val = 0.0

            y[i] = label_val

        # 最后 h 个位置未来不足，统一 NaN（避免“半截路径”）
        if h < n:
            y[-h:] = np.nan

        s = pd.Series(y, index=g.index)
        labels_parts.append(s)

    out = pd.concat(labels_parts).reindex(df.index)
    return out


def add_label_column(
    df: pd.DataFrame,
    spec: LabelSpec,
    *,
    symbol_col: str = "symbol",
    time_col: str = "ts",
    inplace: bool = True,
) -> pd.DataFrame:
    """
    根据 LabelSpec 在 df 上计算 label 列，并返回带标签的 df。

    - 会检查 required_columns 是否存在；
    - 会根据 spec.kind 决定调用哪种计算逻辑；
    - 如果 inplace=False，则返回一个拷贝。
    """
    for col in spec.required_columns:
        if col not in df.columns:
            raise KeyError(
                f"Label {spec.id} requires column={col!r}, but df has columns={list(df.columns)[:10]}..."
            )

    if symbol_col not in df.columns:
        raise KeyError(f"Label {spec.id} requires symbol_col={symbol_col!r} in df")

    if time_col not in df.columns:
        raise KeyError(f"Label {spec.id} requires time_col={time_col!r} in df")

    if not inplace:
        df = df.copy()

    if spec.kind == "time_return":
        y = _compute_time_return_per_symbol(df, spec, symbol_col=symbol_col, time_col=time_col)
    elif spec.kind == "triple_barrier":
        y = _compute_triple_barrier_per_symbol(df, spec, symbol_col=symbol_col, time_col=time_col)
    else:
        raise ValueError(f"Unsupported label kind={spec.kind!r} for label {spec.id}")

    df[spec.column_name] = y

    # 根据 task 决定数据类型
    if spec.task == "clf":
        # -1/0/1 用 int8 即可
        df[spec.column_name] = df[spec.column_name].astype("float64")  # 保留 NaN，再由模型处理
    else:
        df[spec.column_name] = df[spec.column_name].astype("float64")

    return df
```

> 说明：
>
> * **多 symbol 时**，每个 symbol 单独按时间排序后打标签，不会串行。
> * triple-barrier 这里用的是 `close` 路径，已经是路径依赖（不是简单的 t+h）。后面如果你有 `high/low` 列，可以升级逻辑。
> * 最后 `horizon_bars` 行标签统一设为 NaN → 正常情况 label_nan_ratio 很低，不会触发 TrainingGate 的严重告警。

---

## 四、把 LabelRegistry & Builder 接入 `PrincipalInvestigator.propose()`

现在更新你之前的 `PrincipalInvestigator`。核心目标：

1. **禁止 LLM 直接给 SQL target 表达式**。
2. 只允许 LLM/系统选择 **label_id**（或我们自动选择）。
3. PI 负责：

   * 从 LabelRegistry 拿 label spec
   * 强制把 `required_columns` 加进 feature recipe
   * 用 `build_select_sql` 拉干净数据（没有 label）
   * 用 `add_label_column` 在 Python 里打标签
   * 把 `training_plan["target"]` 更新为 **真正的列名**

下面是带注释的新版（你可以直接覆盖原文件的类定义部分）：

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

# NEW: 引入 LabelRegistry & Builder
from labels.registry import get_label_spec, resolve_default_label_id
from labels.builder import add_label_column


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
        symbol_col: str = "symbol",   # NEW: 显式传入列名，默认 symbol/ts
        time_col: str = "ts",
    ) -> PIResult:
        catalog = _fetch_catalog(self._client, table)

        # --- 构造 LLM Prompt（此处略，只是原样保留） ---
        user_prompt = (
            f"目标: {goal}\n"
            f"表: {table}\n"
            f"符号: {symbol}\n"
            f"时间: {start_ts}~{end_ts}\n"
            f"特征: {', '.join(catalog)}\n"
            f"预测窗口: {horizon}\n"
            "说明: 你必须只在已知特征上构造特征, target 只能从系统提供的 label_id 列表中选择。\n"
            "请输出 JSON: {\"feature_recipe\": {..}, \"training_plan\": {..}}。"
        )
        system_prompt = (
            "你是量化平台的首席科学家。必须返回 JSON，包含 feature_recipe 和 training_plan。"
            "training_plan 不允许直接写 SQL 表达式作为 target，只能指定 label_id 或从给定列表中选择。"
        )

        plan = self._llm.generate(system_prompt=system_prompt, user_prompt=user_prompt)

        recipe = plan.get("feature_recipe", {}) or {}
        tplan = plan.get("training_plan", {}) or {}

        # --- 1) 解析并锁定 label_id（契约化），完全禁止 LLM 写 SQL target 表达式 ---

        # LLM 可能返回 label_id 或 target (老字段名)，我们优先使用 label_id
        label_id = tplan.get("label_id") or tplan.get("target")

        if not label_id:
            # 没给的话，我们基于 goal + horizon 自动选择一个默认 label
            label_id = resolve_default_label_id(goal=goal, horizon=horizon)

        # 如果给了 label_id 但 registry 里不存在，会 raise，让上游看到明确错误
        label_spec = get_label_spec(label_id)

        # 把 label 元数据写回 training_plan，方便后续组件和日志
        tplan["label_id"] = label_id
        tplan["target"] = label_spec.column_name
        tplan.setdefault("task", label_spec.task)
        tplan.setdefault("symbol", symbol)

        # --- 2) 确保 label 所需列也出现在 recipe 中（避免 SQL 漏列） ---

        base_features = recipe.get("base_features") or []
        base_features = list(base_features)  # 确保是 list

        for col in label_spec.required_columns:
            if col not in base_features and col in catalog:
                base_features.append(col)

        recipe["base_features"] = base_features

        # --- 3) 构造 SQL（注意：这里不再把 label 塞进 SQL，用 Python 打标签） ---

        sql = build_select_sql(
            table=recipe.get("use_table", table),
            symbol=tplan.get("symbol", symbol),
            start_ts=start_ts,
            end_ts=end_ts,
            recipe=recipe,
            target=None,                 # CHANGED: target 设为 None，由 Python 负责打标签
            feature_catalog=catalog,
        )

        df = self._client.query_df(sql)

        # --- 4) 在 Python 中根据 LabelSpec 打标签（防止 label 泄露 & LLM 注入） ---

        df = add_label_column(
            df,
            label_spec=label_spec,
            symbol_col=symbol_col,
            time_col=time_col,
            inplace=True,
        )

        # --- 5) 构造 HPO 搜索网格（与原实现保持一致） ---

        grid: Dict[str, Any] = {
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
        grid_sane, grid_report = sanitize_and_constrain_grid(grid, guard)

        # 把 grid 报告写回 training_plan，方便 TrainingGate 使用
        tplan["hpo_grid_model_count_raw"] = int(grid_report.get("n_raw_models", len(grid["model_type"])))
        tplan["hpo_grid_model_count"] = int(grid_report.get("n_sane_models", len(grid_sane.get("model_type", []))))
        tplan["hpo_grid_warnings"] = list(grid_report.get("warnings", []))

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
        )
```

> 至此，**标签这一块的“自由 SQL + 潜在泄露”问题已经被彻底切断**：
>
> * LLM 只能选 `label_id`，不能自己写 `close[t+10] - close[t]` 之类的 SQL；
> * 所有标签计算统一走 `LabelRegistry + add_label_column`；
> * 你可以在 `label_registry.json` 里慢慢扩展更多复杂标签（如基于 VWAP、基于 volume 的 event-bar 等），AI 大脑只是在 label 空间里做“选择”，而不是“写代码”。

---

## 五、与 DatasetSanity / TrainingGate 的衔接

现在，你的训练入口（HPO/Backtest orchestrator）可以按下面顺序跑：

1. `pi_result = PI.propose(...)`

   * 得到已经 **带 label 列** 的 `dataset_df` + `training_plan` + `hpo_grid`。

2. `dataset_report, _ = run_dataset_sanity_checks(pi_result.dataset_df)`

3. 准备 `hpo_grid_report`：

   ```python
   tplan = pi_result.training_plan
   hpo_grid_report = {
       "n_raw_models": tplan.get("hpo_grid_model_count_raw", 0),
       "n_sane_models": tplan.get("hpo_grid_model_count", 0),
       "warnings": tplan.get("hpo_grid_warnings", []),
   }
   ```

4. `gate_result = run_training_gate(...)`，其中：

   ```python
   label_col = tplan["target"]      # 现在就是 label_spec.column_name
   task = tplan["task"]
   gate_result = run_training_gate(
       df=pi_result.dataset_df,
       dataset_report=dataset_report,
       label_col=label_col,
       task=task,
       hpo_grid_report=hpo_grid_report,
   )
   ```

5. 如果 `gate_result.ok_to_train` 为 False → 直接 stop，并把原因写日志 / DB / 发给 AI 大脑。

---

到这里，我们已经完成了大计划中的**第二大块升级**：

> **Feature → Label → Model** 这条链路已经被“契约化 + 体检 + 闸门化”，
> AI 大脑不再有“瞎写 SQL / 瞎打标签”的自由，只在一个安全的、你掌控的空间里玩。

如果你再说一次「继续」，下一步我会：

* **整理 FeatureRegistry 的落地实现**（和 LabelRegistry 对称），
* 把你现在分散在 `feature_programs.json` / `feature_sql` 里的东西，收敛成一个统一的 **特征契约层**，
* 并且给 AI 大脑一个“特征空间地图”，让它既“不会乱来”，又“还能玩组合和衍生特征”。
好，继续往下落地 **FeatureRegistry（特征契约层）**，这一步做完，你的：

* 特征空间会被“收编到一个白名单里”，
* LLM 只能在你允许的特征里面玩组合，不再乱造 SQL，
* `build_select_sql` 变成一个**安全编译器**，而不是随便拼 SQL 的函数。

我直接给你**可落地的 JSON 结构 + Python 模块 + 新版 `build_select_sql`**。你按文件替换/新增即可，不用做额外设计。

---

## 一、Feature Registry 的 JSON 结构

新建：`config/feature_registry.json`（名字/路径你可以自己改，只要后面初始化时一致即可）

> 下面是一个**结构模板**，具体有哪些特征、窗口、表达式，你可以慢慢填；现在的目标是把**架子搭好**。

```json
{
  "_comment": "Feature Registry - Whitelist of approved features for AI Brain",
  "_version": "1.0",
  "columns": {
    "ts": {
      "role": "time",
      "dtype": "DateTime",
      "timeframe": "1m",
      "description": "Event timestamp, primary sort key."
    },
    "symbol": {
      "role": "symbol",
      "dtype": "String",
      "timeframe": "1m",
      "description": "Symbol identifier."
    },
    "open": {
      "role": "price",
      "dtype": "Float64",
      "timeframe": "1m",
      "description": "Bar open price."
    },
    "high": {
      "role": "price",
      "dtype": "Float64",
      "timeframe": "1m",
      "description": "Bar high price."
    },
    "low": {
      "role": "price",
      "dtype": "Float64",
      "timeframe": "1m",
      "description": "Bar low price."
    },
    "close": {
      "role": "price",
      "dtype": "Float64",
      "timeframe": "1m",
      "description": "Bar close price."
    },
    "volume": {
      "role": "volume",
      "dtype": "Float64",
      "timeframe": "1m",
      "description": "Bar volume."
    },
    "vwap": {
      "role": "price",
      "dtype": "Float64",
      "timeframe": "1m",
      "description": "Bar VWAP if available."
    }
  },
  "derived": {
    "ret_1": {
      "output_name": "ret_1",
      "sql_expr": "log(close) - log(lagInFrame(close, 1))",
      "required_columns": ["close"],
      "timeframe": "1m",
      "task_hint": "reg",
      "leak_safe": true,
      "description": "1-bar log return of close, per-symbol rolling."
    },
    "ret_5": {
      "output_name": "ret_5",
      "sql_expr": "log(close) - log(lagInFrame(close, 5))",
      "required_columns": ["close"],
      "timeframe": "1m",
      "task_hint": "reg",
      "leak_safe": true,
      "description": "5-bar log return."
    },
    "hl_spread": {
      "output_name": "hl_spread",
      "sql_expr": "(high - low) / nullIf(close, 0)",
      "required_columns": ["high", "low", "close"],
      "timeframe": "1m",
      "task_hint": "reg",
      "leak_safe": true,
      "description": "Intrabar range normalized by close."
    },
    "rv_20": {
      "output_name": "rv_20",
      "sql_expr": "sqrt(avgOver(partition by symbol order by ts rows between 19 preceding and current row)(pow(ret_1, 2)))",
      "required_columns": ["ret_1"],
      "timeframe": "1m",
      "task_hint": "reg",
      "leak_safe": true,
      "description": "Rolling 20-bar realized volatility based on ret_1."
    }
  },
  "sandbox": {
    "_comment": "Optional sandbox features that PI can propose but must be flagged for review.",
    "enabled": true,
    "max_features": 3
  }
}
```

要点：

* `columns`：所有允许直接从表里 SELECT 的**原始字段**（含 ts/symbol），给 AI 大脑一张“眼睛能看到什么”的白名单地图。
* `derived`：你认可的衍生特征，带 `sql_expr`，**只用历史窗口，不允许未来信息**（`leak_safe: true`），以后你可以慢慢增加。
* `sandbox`：留了口子，如果你将来要做“沙箱特征”，在这里控制数量和开关。

---

## 二、FeatureRegistry 模块

新建目录：`features/`，两个文件：

* `features/__init__.py`
* `features/registry.py`

### `features/registry.py`

```python
# features/registry.py

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ColumnSpec:
    name: str
    role: str
    dtype: str
    timeframe: str
    description: str = ""


@dataclass
class DerivedFeatureSpec:
    id: str
    output_name: str
    sql_expr: str
    required_columns: List[str]
    timeframe: str
    task_hint: str = "reg"
    leak_safe: bool = True
    description: str = ""


class FeatureRegistry:
    """
    管理可用特征（原始列 + 派生列）的白名单。
    """

    def __init__(self, raw: Dict[str, Any]) -> None:
        self._raw = raw
        self._columns: Dict[str, ColumnSpec] = {}
        self._derived: Dict[str, DerivedFeatureSpec] = {}

        for name, cfg in (raw.get("columns") or {}).items():
            self._columns[name] = ColumnSpec(
                name=name,
                role=cfg.get("role", "feature"),
                dtype=cfg.get("dtype", "Float64"),
                timeframe=cfg.get("timeframe", ""),
                description=cfg.get("description", ""),
            )

        for fid, cfg in (raw.get("derived") or {}).items():
            self._derived[fid] = DerivedFeatureSpec(
                id=fid,
                output_name=cfg["output_name"],
                sql_expr=cfg["sql_expr"],
                required_columns=list(cfg.get("required_columns") or []),
                timeframe=cfg.get("timeframe", ""),
                task_hint=cfg.get("task_hint", "reg"),
                leak_safe=bool(cfg.get("leak_safe", True)),
                description=cfg.get("description", ""),
            )

        sandbox_cfg = raw.get("sandbox") or {}
        self._sandbox_enabled: bool = bool(sandbox_cfg.get("enabled", False))
        self._sandbox_max_features: int = int(sandbox_cfg.get("max_features", 0))

    @classmethod
    def from_file(cls, path: str | Path) -> "FeatureRegistry":
        import json

        p = Path(path)
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    # ---- Columns ----
    def column_exists(self, name: str) -> bool:
        return name in self._columns

    def get_column(self, name: str) -> ColumnSpec:
        if name not in self._columns:
            raise KeyError(f"Unknown base column={name!r} in FeatureRegistry")
        return self._columns[name]

    def list_columns(self) -> List[str]:
        return sorted(self._columns.keys())

    # ---- Derived ----
    def derived_exists(self, feature_id: str) -> bool:
        return feature_id in self._derived

    def get_derived(self, feature_id: str) -> DerivedFeatureSpec:
        if feature_id not in self._derived:
            raise KeyError(f"Unknown derived feature id={feature_id!r} in FeatureRegistry")
        return self._derived[feature_id]

    def list_derived_ids(self) -> List[str]:
        return sorted(self._derived.keys())

    # ---- Sandbox ----
    def sandbox_enabled(self) -> bool:
        return self._sandbox_enabled

    def sandbox_max_features(self) -> int:
        return self._sandbox_max_features

    def raw(self) -> Dict[str, Any]:
        return self._raw


# --- 全局默认 FeatureRegistry（懒加载） ---

_default_registry: Optional[FeatureRegistry] = None


def init_default_feature_registry(path: str | Path) -> None:
    """
    在应用启动时调用一次，加载 feature_registry.json。
    """
    global _default_registry
    _default_registry = FeatureRegistry.from_file(path)


def get_registry() -> FeatureRegistry:
    if _default_registry is None:
        raise RuntimeError(
            "FeatureRegistry is not initialized. "
            "Call features.registry.init_default_feature_registry(path) at startup."
        )
    return _default_registry


def sanitize_base_features(
    requested: List[str],
    feature_catalog: List[str],
) -> List[str]:
    """
    防御性筛选：只保留出现在 ClickHouse 表 schema 和 FeatureRegistry 中的 base features。
    """
    reg = get_registry()
    catalog_set = set(feature_catalog)

    safe: List[str] = []
    for name in requested:
        if name not in catalog_set:
            # 表里都没有，直接丢弃
            continue
        if not reg.column_exists(name):
            # registry 没登记，说明没审过，也丢弃
            continue
        safe.append(name)

    # 去重，保持顺序
    seen = set()
    result: List[str] = []
    for name in safe:
        if name not in seen:
            seen.add(name)
            result.append(name)
    return result
```

在 app 启动时（比如 orchestrator / ai_brain runner）加两行初始化：

```python
from features.registry import init_default_feature_registry
from labels.registry import init_default_registry

init_default_feature_registry("config/feature_registry.json")
init_default_registry("config/label_registry.json")
```

---

## 三、升级版 build_select_sql：把特征编译成安全 SQL

我们现在重写 `pi/feature_sql.py`，让它：

1. **强制 base_features 经过 FeatureRegistry + schema 校验**；
2. **derived 只能引用 registry 里的 `derived.id`**，不会随便执行 LLM 写的 SQL；
3. 保留一个“非常窄”的兼容口：如果你未来要试 `sandbox`，可以少量允许 inline_expr，但必须走 guard。

> 下面是一个**完整版本**，你可以直接拿去覆盖 `pi/feature_sql.py`（如果你已有别的函数，可以手动 merge，但这个版本是自洽的）。

```python
# pi/feature_sql.py

from __future__ import annotations

from typing import Any, Dict, List, Optional

from features.registry import (
    get_registry,
    sanitize_base_features,
    DerivedFeatureSpec,
)


def _quote_str(value: str) -> str:
    """
    简单的 ClickHouse 字符串转义。
    """
    s = str(value)
    s = s.replace("\\", "\\\\").replace("'", "''")
    return f"'{s}'"


def _build_derived_select_list(
    recipe_derived: List[Dict[str, Any]] | None,
) -> List[str]:
    """
    把 feature_recipe['derived'] 编译为 SELECT 子句列表。
    每个元素形如: "<sql_expr> AS <alias>"
    """
    if not recipe_derived:
        return []

    reg = get_registry()
    select_parts: List[str] = []

    sandbox_inline_used = 0
    sandbox_allowed = reg.sandbox_max_features() if reg.sandbox_enabled() else 0

    for item in recipe_derived:
        # 推荐用法：通过 id 引用 registry 中的特征
        feature_id = item.get("id")
        alias = item.get("alias")
        inline_expr = item.get("expr")  # 兼容旧格式 / sandbox

        if feature_id:
            spec: DerivedFeatureSpec = reg.get_derived(feature_id)
            expr = spec.sql_expr
            out_name = alias or spec.output_name

            # 如果 registry 标注 leak_safe=False，可以在这里加更严格限制
            if not spec.leak_safe:
                raise ValueError(
                    f"Derived feature id={feature_id!r} is not marked leak_safe; "
                    "don't use it in automated PI runs."
                )

            select_parts.append(f"{expr} AS {out_name}")
            continue

        # 走到这里说明没有 id，只提供了 expr → 只允许作为极少数 sandbox
        if inline_expr:
            if not reg.sandbox_enabled():
                # 全局禁用 sandbox，直接拒绝
                raise ValueError(
                    "Inline SQL expr for derived features is disabled by FeatureRegistry. "
                    "Please register this feature in feature_registry.json as a derived.id."
                )
            if sandbox_inline_used >= sandbox_allowed:
                raise ValueError(
                    f"Inline SQL expr quota exceeded: allowed={sandbox_allowed}, "
                    "please register more features in FeatureRegistry instead of using sandbox."
                )

            # 简单静态防御：禁止明显的未来函数/lead/负窗口关键字
            bad_keywords = ["lead(", "leadInFrame(", "toRelativeSecondNum("]
            lowered = inline_expr.lower()
            if any(bad in lowered for bad in bad_keywords):
                raise ValueError(
                    f"Inline expr appears to use forward-looking functions: {inline_expr!r}"
                )

            out_name = alias or "sandbox_feat_" + str(sandbox_inline_used + 1)
            select_parts.append(f"({inline_expr}) AS {out_name}")
            sandbox_inline_used += 1
            continue

        # 如果既没有 id 也没有 expr，则跳过
        continue

    return select_parts


def build_select_sql(
    *,
    table: str,
    symbol: str,
    start_ts: str,
    end_ts: str,
    recipe: Dict[str, Any],
    target: Optional[str],
    feature_catalog: List[str],
    symbol_col: str = "symbol",
    time_col: str = "ts",
) -> str:
    """
    根据 feature_recipe 和 FeatureRegistry 构造安全的 ClickHouse SELECT 语句。

    约束：
    - base_features 只能在 (表 schema ∩ FeatureRegistry.columns) 中取；
    - derived 只能优先使用 FeatureRegistry.derived[id]；
    - target 不再放入 SQL（推荐），而是在 Python 打标签；如果你确实要从表里取已有 target 列，
      必须是 feature_catalog+FeatureRegistry 里登记过的列。
    """

    reg = get_registry()

    # ---- 1. Base features: 白名单过滤 ----
    requested_base = list(recipe.get("base_features") or [])
    base_features = sanitize_base_features(requested_base, feature_catalog)

    # symbol/time 列强制保留
    must_have = [symbol_col, time_col]
    for col in must_have:
        if col in feature_catalog and col not in base_features:
            base_features.append(col)

    if not base_features:
        raise ValueError(
            "No valid base_features after sanitization. "
            "Check feature_registry.json and PI feature_recipe."
        )

    # 构造 base SELECT 列，如 "close", "volume"
    select_cols: List[str] = []
    for col in base_features:
        # 可以选择根据 ColumnSpec 的 dtype 做一些 CAST，这里先保持简单
        select_cols.append(col)

    # ---- 2. Derived features: 通过 registry 编译 SQL 表达式 ----
    derived_recipe = recipe.get("derived") or []
    derived_select = _build_derived_select_list(derived_recipe)

    # ---- 3. Target 列（可选 & 不推荐） ----
    #   - 现在我们推荐 target=None（由 Python 打标签）。
    #   - 如果外部确实传了 target，并且它是一个实际列名：
    if target:
        if target in feature_catalog and reg.column_exists(target):
            if target not in base_features:
                select_cols.append(target)
        else:
            # 不符合白名单，直接忽略，防止 LLM 注入 "close[t+10]" 之类
            pass

    # ---- 4. 拼接 SELECT 列表 ----
    select_parts: List[str] = []
    for col in select_cols:
        select_parts.append(col)  # 可根据需要加 alias: f"{col} AS {col}"
    select_parts.extend(derived_select)

    select_clause = ",\n       ".join(select_parts)

    # ---- 5. WHERE 条件 + ORDER ----
    # 简单字符串拼接，但做了转义
    symbol_literal = _quote_str(symbol)
    start_literal = _quote_str(start_ts)
    end_literal = _quote_str(end_ts)

    sql = f"""
SELECT
       {select_clause}
FROM {table}
WHERE {symbol_col} = {symbol_literal}
  AND {time_col} >= parseDateTimeBestEffort({start_literal})
  AND {time_col} <  parseDateTimeBestEffort({end_literal})
ORDER BY {time_col} ASC
"""

    return sql.strip()
```

> 你之前的 `PrincipalInvestigator.propose()` 调用是：

```python
sql = build_select_sql(
    table=recipe.get("use_table", table),
    symbol=tplan.get("symbol", symbol),
    start_ts=start_ts,
    end_ts=end_ts,
    recipe=recipe,
    target=None,                  # 已经改成 None
    feature_catalog=catalog,
)
```

这个签名和上面的实现是对得上的。

---

## 四、现在这条链路长成什么样了？

**从 AI 大脑到训练数据完整路径：**

1. AI 大脑 / PI 只能在你定义的 `feature_registry.json` / `label_registry.json` 里**选**：

   * `feature_recipe.base_features`: 只能填你表 schema & registry 里存在的列。
   * `feature_recipe.derived`: 只能填 `{"id": "ret_1", "alias": "ret_1"}` 之类的安全 ID。
   * `training_plan.label_id`: 只能是你 LabelRegistry 里定义的标签。

2. `PrincipalInvestigator.propose()`：

   * 用 `FeatureRegistry` 过滤 base_features，强制加上 `symbol`/`ts`。
   * 用 `build_select_sql()` 构造 SQL（**不含 target 表达式**）。
   * 从 ClickHouse 取出干净的特征 DataFrame。
   * 用 `LabelRegistry + add_label_column()` 在 **Python 里打标签**（time_return 或 triple-barrier）。

3. 得到的 `dataset_df` + `training_plan` 进入：

   * `run_dataset_sanity_checks(df)`（体检）
   * `run_training_gate(...)`（闸门），检查：

     * label 的 NaN 比例、分布；
     * 特征共线性、常数列比例；
     * HPO grid 是否合理；

4. **只有通过了闸门的 dataset** 才能进入 HPO / 训练 / 回测；否则直接 FAIL，并把原因报告喂给 AI 大脑和日志。

到这里：

* **标签泄露问题**：已经从根上切断（target 不再出现在 SQL 里，全部在 Python 统一打）。
* **特征乱造问题**：被 FeatureRegistry 白名单 + sandbox 配额牢牢拦住。
* **AI 乱用工具问题**：现在它能用的“眼睛”和“工具”都被清晰约束在 registry + sanity/gate 体系里。

---

如果你对上面没有异议，下一步（你再说一次“继续”）我会：

* 设计一个 **FeatureRecipe 契约**（给 AI 大脑看的 schema & 使用说明），
* 把 Prompt 里的“feature space 描述”写成一个稳定的 system prompt，让 LLM 知道：

  * 有哪些 **base_features**、有哪些 **derived ids** 可以用；
  * 不允许自己写 SQL，只能填 ID；
  * 如何根据 `goal/horizon` 组合特征；
* 再给你一个“AI 大脑选特征”的测试计划，让你可以跑几次 PI → Sanity → Gate → HPO，检查全链路现在是不是稳定了。
