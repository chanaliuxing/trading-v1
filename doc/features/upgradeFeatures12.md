好，接下来我们把 **“模型池 + HPO 网格”** 这一层锁死，给 AI 大脑戴上“模型安全帽”。

目标：

* LLM 仍然可以提议模型和超参；
* 但 **只能在我们允许的模型类型 + 参数范围里活动**；
* 如果提的太离谱，就自动剪掉 / 回退到安全默认。

---

## 一、`grid_guard.py`：模型 & HPO 网格安全层

下面这份可以直接作为 `grid_guard.py`（或替换你现有的），函数签名保持不变：
`sanitize_and_constrain_grid(grid, guard_cfg) -> (grid_sane, report)`。

```python
# grid_guard.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union


Number = Union[int, float]


@dataclass
class ParamSpec:
    """单个超参的约束定义。"""
    name: str
    ptype: str                      # "int" | "float" | "bool" | "categorical"
    min: Optional[Number] = None
    max: Optional[Number] = None
    choices: Optional[List[Any]] = None
    default: Optional[Any] = None
    required: bool = False


@dataclass
class ModelSpec:
    """模型类型定义（允许的参数及默认值）。"""
    name: str
    params: Dict[str, ParamSpec]


# 1) 定义“白名单模型池”——只允许这里列出来的模型类型
MODEL_SPECS: Dict[str, ModelSpec] = {}


def _register_model(spec: ModelSpec) -> None:
    MODEL_SPECS[spec.name] = spec


def _int_param(name: str, min_: int, max_: int, default: int, required: bool = False) -> ParamSpec:
    return ParamSpec(name=name, ptype="int", min=min_, max=max_, default=default, required=required)


def _float_param(name: str, min_: float, max_: float, default: float, required: bool = False) -> ParamSpec:
    return ParamSpec(name=name, ptype="float", min=min_, max=max_, default=default, required=required)


def _bool_param(name: str, default: bool) -> ParamSpec:
    return ParamSpec(name=name, ptype="bool", default=default)


def _cat_param(name: str, choices: List[Any], default: Any, required: bool = False) -> ParamSpec:
    return ParamSpec(name=name, ptype="categorical", choices=choices, default=default, required=required)


# === 注册几个典型模型（可按你实际 pipeline 扩展） ===

_register_model(
    ModelSpec(
        name="xgb",
        params={
            "n_estimators": _int_param("n_estimators", 50, 1000, 300, required=True),
            "max_depth": _int_param("max_depth", 2, 10, 4),
            "learning_rate": _float_param("learning_rate", 0.005, 0.5, 0.05),
            "subsample": _float_param("subsample", 0.5, 1.0, 0.9),
            "colsample_bytree": _float_param("colsample_bytree", 0.5, 1.0, 0.9),
            "reg_alpha": _float_param("reg_alpha", 0.0, 5.0, 0.0),
            "reg_lambda": _float_param("reg_lambda", 0.0, 10.0, 1.0),
        },
    )
)

_register_model(
    ModelSpec(
        name="lgbm",
        params={
            "n_estimators": _int_param("n_estimators", 50, 2000, 400, required=True),
            "num_leaves": _int_param("num_leaves", 16, 256, 64),
            "max_depth": _int_param("max_depth", -1, 12, -1),
            "learning_rate": _float_param("learning_rate", 0.005, 0.5, 0.03),
            "subsample": _float_param("subsample", 0.5, 1.0, 0.9),
            "colsample_bytree": _float_param("colsample_bytree", 0.5, 1.0, 0.9),
            "reg_alpha": _float_param("reg_alpha", 0.0, 5.0, 0.0),
            "reg_lambda": _float_param("reg_lambda", 0.0, 10.0, 1.0),
        },
    )
)

_register_model(
    ModelSpec(
        name="lasso",
        params={
            "alpha": _float_param("alpha", 1e-6, 10.0, 0.001, required=True),
        },
    )
)

_register_model(
    ModelSpec(
        name="ridge",
        params={
            "alpha": _float_param("alpha", 1e-6, 100.0, 1.0, required=True),
        },
    )
)

_register_model(
    ModelSpec(
        name="logistic",
        params={
            "C": _float_param("C", 1e-4, 100.0, 1.0, required=True),
            "penalty": _cat_param("penalty", ["l2", "l1"], "l2"),
        },
    )
)

_register_model(
    ModelSpec(
        name="linear_svm",
        params={
            "C": _float_param("C", 1e-4, 100.0, 1.0, required=True),
        },
    )
)

_register_model(
    ModelSpec(
        name="knn",
        params={
            "n_neighbors": _int_param("n_neighbors", 2, 64, 8, required=True),
            "weights": _cat_param("weights", ["uniform", "distance"], "distance"),
        },
    )
)

# 你后面可以继续加：rulefit, random_forest, etc.


# === 帮助函数：参数值裁剪 & 校验 ===

def _coerce_numeric(value: Any, ptype: str) -> Optional[Number]:
    if value is None:
        return None
    try:
        if ptype == "int":
            return int(value)
        if ptype == "float":
            return float(value)
    except (TypeError, ValueError):
        return None
    return None


def _sanitize_single_param(
    value: Any,
    spec: ParamSpec,
    warnings: List[str],
    model_name: str,
) -> Any:
    """将单个 param 约束在 spec 定义的范围内，或回退到默认值。"""

    if spec.ptype in ("int", "float"):
        num = _coerce_numeric(value, spec.ptype)
        if num is None:
            if spec.default is not None:
                warnings.append(
                    f"[{model_name}] param '{spec.name}' value {value!r} invalid, "
                    f"fallback to default {spec.default!r}"
                )
                return spec.default
            return None

        if spec.min is not None and num < spec.min:
            warnings.append(
                f"[{model_name}] param '{spec.name}'={num} < min {spec.min}, clipped"
            )
            num = spec.min
        if spec.max is not None and num > spec.max:
            warnings.append(
                f"[{model_name}] param '{spec.name}'={num} > max {spec.max}, clipped"
            )
            num = spec.max
        # int 类型取整
        if spec.ptype == "int":
            num = int(round(num))
        return num

    if spec.ptype == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "y"):
                return True
            if v in ("false", "0", "no", "n"):
                return False
        warnings.append(
            f"[{model_name}] param '{spec.name}' value {value!r} invalid for bool, "
            f"fallback to default {spec.default!r}"
        )
        return spec.default

    if spec.ptype == "categorical":
        choices = spec.choices or []
        if value in choices:
            return value
        warnings.append(
            f"[{model_name}] param '{spec.name}' value {value!r} not in choices {choices}, "
            f"fallback to default {spec.default!r}"
        )
        return spec.default

    # 未知类型，直接用默认或 None
    warnings.append(
        f"[{model_name}] param '{spec.name}' has unknown type {spec.ptype!r}, "
        f"fallback to default {spec.default!r}"
    )
    return spec.default


# === 主函数：HPO 网格安全化 ===

def sanitize_and_constrain_grid(
    grid: Dict[str, Any],
    guard_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    入口签名保持不变：
    - 过滤非法模型类型
    - 为超参做类型转换与 min/max/choices 校验
    - 若 LLM 全部给错，回退到一个安全默认模型
    """
    guard_cfg = guard_cfg or {}

    raw_model_types: List[str] = grid.get("model_type") or []
    raw_params_list: List[Dict[str, Any]] = grid.get("model_params") or []

    # 允许的模型类型可以通过 guard_cfg 覆盖，否则用全部 registry
    allowed_model_types: List[str] = guard_cfg.get("allowed_model_types") or list(MODEL_SPECS.keys())

    warnings: List[str] = []
    sane_model_types: List[str] = []
    sane_params_list: List[Dict[str, Any]] = []

    n = len(raw_model_types)
    for i in range(n):
        mtype = raw_model_types[i]
        params = raw_params_list[i] if i < len(raw_params_list) else {}

        if mtype not in allowed_model_types:
            warnings.append(f"model_type[{i}]={mtype!r} not allowed, skipped")
            continue

        spec = MODEL_SPECS.get(mtype)
        if spec is None:
            warnings.append(f"model_type[{i}]={mtype!r} not in MODEL_SPECS, skipped")
            continue

        sanitized_params: Dict[str, Any] = {}

        # 1) 处理 registry 里定义的参数
        for pname, pspec in spec.params.items():
            raw_val = params.get(pname, pspec.default)
            val = _sanitize_single_param(raw_val, pspec, warnings, mtype)
            if val is None and pspec.required:
                # 必填参数没有合法值 → 用默认，如果连默认都没有，就跳过这个模型
                if pspec.default is None:
                    warnings.append(
                        f"[{mtype}] required param '{pname}' is missing and no default; model skipped"
                    )
                    sanitized_params = None
                    break
                else:
                    val = pspec.default
            sanitized_params[pname] = val

        if sanitized_params is None:
            # 上面标记为 skip
            continue

        # 2) 对于 LLM 乱加的额外参数，直接忽略
        extra_keys = set(params.keys()) - set(spec.params.keys())
        for extra in extra_keys:
            warnings.append(
                f"[{mtype}] extra param '{extra}' ignored (not in registry)"
            )

        sane_model_types.append(mtype)
        sane_params_list.append(sanitized_params)

    # 如果所有候选都被过滤掉，回退到一个安全默认 xgb
    if not sane_model_types:
        fallback_type = "xgb" if "xgb" in MODEL_SPECS else next(iter(MODEL_SPECS.keys()))
        fallback_spec = MODEL_SPECS[fallback_type]
        fallback_params: Dict[str, Any] = {}
        for pname, pspec in fallback_spec.params.items():
            # 默认值必须存在；否则给一个简单保守值
            val = pspec.default
            if val is None:
                if pspec.ptype == "int":
                    val = max(pspec.min or 1, 1)
                elif pspec.ptype == "float":
                    val = pspec.min or 0.01
                elif pspec.ptype == "categorical" and pspec.choices:
                    val = pspec.choices[0]
                elif pspec.ptype == "bool":
                    val = False
            fallback_params[pname] = val

        sane_model_types = [fallback_type]
        sane_params_list = [fallback_params]
        warnings.append(
            f"all model candidates were rejected; fallback to safe default model_type={fallback_type!r}"
        )

    # 3) 任务类型 / 决策规则 轻度校验
    tasks = grid.get("task") or ["reg"]
    if not isinstance(tasks, list):
        tasks = [tasks]

    main_task = tasks[0] if tasks else "reg"
    if main_task not in ("reg", "clf"):
        warnings.append(f"task={main_task!r} invalid, fallback to 'reg'")
        main_task = "reg"

    decision_list = grid.get("decision") or []
    if not isinstance(decision_list, list):
        decision_list = [decision_list]
    decision = decision_list[0] if decision_list else {}

    if not isinstance(decision, dict):
        decision = {}

    mode = decision.get("mode", "quantile")
    if mode not in ("quantile", "threshold"):
        warnings.append(f"decision.mode={mode!r} invalid, fallback to 'quantile'")
        mode = "quantile"

    if mode == "quantile":
        q_long = float(decision.get("q_long", 0.7))
        q_short = float(decision.get("q_short", 0.3))
        # 简单夹逼到 [0.05, 0.95]
        q_long = max(0.05, min(0.95, q_long))
        q_short = max(0.05, min(0.95, q_short))
        if q_long <= q_short:
            warnings.append(
                f"decision quantiles invalid (q_long={q_long}, q_short={q_short}), "
                f"reset to (0.7, 0.3)"
            )
            q_long, q_short = 0.7, 0.3
        sane_decision = {"mode": "quantile", "q_long": q_long, "q_short": q_short}
    else:
        # threshold 模式可后续扩展，目前简单通过
        sane_decision = decision

    # 4) 重建网格
    sane_grid = dict(grid)
    sane_grid["model_type"] = sane_model_types
    sane_grid["model_params"] = sane_params_list
    sane_grid["task"] = [main_task]
    sane_grid["decision"] = [sane_decision]

    report = {
        "warnings": warnings,
        "n_raw_models": len(raw_model_types),
        "n_sane_models": len(sane_model_types),
    }
    return sane_grid, report
```

这层做的事情：

* 任何 **不在白名单里的 model_type** → 直接丢掉；
* 超参全部按类型与范围进行 **coerce + clip**；
* LLM 乱加的超参 key → 忽略；
* 如果全被过滤掉 → 自动回退到一个保守的 `xgb` 默认配置；
* `task` 和 `decision` 也做轻度校验（`reg/clf`、quantile 参数范围）。

---

## 二、接 `PrincipalInvestigator`：把“净化后的网格”灌回 TPlan & PIResult

在你给的 `PrincipalInvestigator` 里，我们已经有：

```python
grid = {
    "model_type": [],
    "feature_subset": [recipe.get("base_features", []) + [d.get("alias") for d in recipe.get("derived", [])]],
    "task": [tplan.get("task", "reg")],
    "decision": [tplan.get("decision", {"mode": "quantile", "q_long": 0.7, "q_short": 0.3})],
    "model_params": [],
}
for cand in tplan.get("model_candidates", []) or []:
    grid["model_type"].append(cand.get("model_type", "xgb"))
    grid["model_params"].append(cand.get("model_params", {}))

guard = (guard_cfg or {}) | {"allow_params": ["model_type", "feature_subset", "task", "decision", "model_params"]}
grid_sane, _ = sanitize_and_constrain_grid(grid, guard)
```

我们现在只需要两点升级：

1. 接收 `report`，把 warnings 写回 `training_plan`，让 AI 大脑也能看到。
2. 明确告诉下游：`hpo_grid` 已经是“净化后的版本”。

### 2.1 修改 `PrincipalInvestigator.propose`

```python
# pi/principal_investigator.py（只展示修改片段）

from grid_guard import sanitize_and_constrain_grid
...

class PrincipalInvestigator:
    ...

    def propose(... ) -> PIResult:
        ...
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
            feat_reg=self._feat_reg,
        )
        df = self._client.query_df(sql)

        # ==== 1) 原始 HPO 网格构建 ====
        feature_subset = recipe.get("base_features", []) + [
            d.get("alias") for d in (recipe.get("derived") or []) if isinstance(d, dict)
        ]

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
            if not isinstance(cand, dict):
                continue
            grid["model_type"].append(cand.get("model_type", "xgb"))
            grid["model_params"].append(cand.get("model_params", {}))

        # ==== 2) 通过 GridGuard 约束模型池 + 超参 ====
        guard = (guard_cfg or {}) | {
            "allow_params": [
                "model_type",
                "feature_subset",
                "task",
                "decision",
                "model_params",
            ],
            # 你也可以通过 guard_cfg 额外指定 allowed_model_types 等
        }
        grid_sane, grid_report = sanitize_and_constrain_grid(grid, guard)

        # ==== 3) 把结果写回 training_plan，给 AI 大脑 & 下游使用 ====
        tplan["hpo_grid_effective"] = grid_sane
        tplan["hpo_grid_warnings"] = grid_report.get("warnings", [])
        tplan["hpo_grid_model_count"] = grid_report.get("n_sane_models", 0)

        return PIResult(
            feature_recipe=recipe,
            training_plan=tplan,
            sql=sql,
            dataset_df=df,
            hpo_grid=grid_sane,
        )
```

现在，下游（包括 HPO 跑法、AI 大脑）拿到的 `PIResult.hpo_grid` 已经是安全的：

* 只包含白名单模型；
* 所有参数都在约束之内；
* 还可以通过 `pi_result.training_plan["hpo_grid_warnings"]` 看到 LLM 有哪些“不守规矩”的地方。

---

## 三、把模型信息也接进 BrainContext（AI 大脑的“模型安全报告”）

在你生成 Brain 调用 payload 的地方（大概有个 `build_brain_context` 之类的函数），可以再加一段类似：

```python
# 假设我们有 pi_result: PIResult

model_section = {
    "hpo_grid_effective": pi_result.training_plan.get("hpo_grid_effective"),
    "hpo_grid_warnings": pi_result.training_plan.get("hpo_grid_warnings", []),
    "hpo_model_count": pi_result.training_plan.get("hpo_grid_model_count", 0),
}

# 然后塞进总的 context 里面，比如：
brain_context["model_safety"] = model_section
```

这样 AI 大脑在“决定是否训练 / 选择哪条路线”时可以看到：

* 这一次真正被允许的模型数量（如果只有 1 个，而且是 fallback，可以提示“策略搜索空间极度受限”）；
* LLM 提过哪些“非法”超参/模型；
* 是否需要简化模型池 / 回到 baseline。

---

## 四、现在这一层达到什么状态？

到目前为止，你的自动策略研究流水线在关键三层已经是**可控状态**：

1. **数据层**：

   * `dataset_sanity` 体检所有训练数据（NaN/Inf/分布/相关性等），坏数据直接拒绝。

2. **特征层**：

   * `FeatureRegistry v2` 管住所有标准特征；
   * `SandboxValidator` 把 LLM 的临时特征表达式关进“安全沙箱”；
   * `build_select_sql` 统一从这两处拿表达式，SQL 不再裸露在 LLM 手里。

3. **模型 & HPO 层（本轮完成）**：

   * `ModelSpec + sanitize_and_constrain_grid` 限制模型类型和参数范围；
   * `PrincipalInvestigator` 把“净化后网格 + 警告”写回 `training_plan` 和 `PIResult.hpo_grid`；
   * AI 大脑通过 BrainContext 能看到模型安全报告，做更稳健的决策。

---

如果你再说一次「继续」，我下一步会做：

* **“训练执行层的 Gate”**：

  * 在真正调用训练/HPO 之前，写一个统一的 `TrainingGate`：

    * 检查数据体检报告（严重问题 → 禁止训练）；
    * 标签统计（极端不平衡 → 强制切换 loss / sample_weight 或直接放弃）；
    * 模型安全报告（只有 fallback 模型 → 标记为“实验价值低”）；
  * 并把这些 Gate 的决策结果、原因一并记到日志 + BrainContext，让 AI 大脑学会“什么时候该说 NO”。
好，继续把“训练闸门 TrainingGate”这一层落地，真的把 **“垃圾数据 / 垃圾标签 / 垃圾模型网格” 一刀拦在训练外面**。

下面我直接给你**可以落地的代码 + 集成点**，不问你任何事。

---

## 一、TrainingGate 的职责和输入输出

位置：**PI → dataset_sanity → TrainingGate → HPO/训练**

它要做三件事：

1. 看 **数据体检报告**（dataset_sanity 的 report）
2. 看 **标签质量**（从 df 里算 label 的统计）
3. 看 **模型池健康度**（grid_guard 的 report）

然后给出一个统一的决策：

```python
@dataclass
class TrainingGateResult:
    ok_to_train: bool          # 能不能继续训练
    severity: str              # "ok" | "degraded" | "block"
    reasons: List[str]         # 具体原因
    suggestions: List[str]     # 建议动作（给人 & 给 AI 大脑看）
    tags: List[str]            # 标记（class_imbalance, few_samples, ...）
```

---

## 二、新文件：`training_gate.py`

你可以新建一个模块，例如 `ai_guard/training_gate.py`（目录随你）：

```python
# training_gate.py

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


@dataclass
class TrainingGateResult:
    ok_to_train: bool               # 是否允许进入训练 / HPO
    severity: str                   # "ok" | "degraded" | "block"
    reasons: List[str]
    suggestions: List[str]
    tags: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok_to_train": self.ok_to_train,
            "severity": self.severity,
            "reasons": self.reasons,
            "suggestions": self.suggestions,
            "tags": self.tags,
        }


def _safe_get(d: Dict[str, Any], key: str, default: Any = None) -> Any:
    v = d.get(key, default)
    return default if v is None else v


def evaluate_dataset_health(
    df: pd.DataFrame,
    dataset_report: Dict[str, Any],
    label_col: str,
    config: Optional[Dict[str, Any]] = None,
) -> TrainingGateResult:
    """
    统一检查数据“体检报告”和 DataFrame 基本健康度。
    config 里可以覆盖一些阈值。
    """
    cfg = {
        "min_rows": 5_000,                # 太少样本直接 block
        "min_features": 5,                # 特征太少，价值极低
        "max_nan_ratio_per_col": 0.3,     # 某个特征 NaN 比例超过 30% → 严重告警
        "max_total_nan_ratio": 0.2,       # 全表 NaN 比例超过 20% → 严重告警
        "max_zero_var_fraction": 0.2,     # 零方差特征占比 > 20% → 警告
    }
    if config:
        cfg.update(config)

    reasons: List[str] = []
    suggestions: List[str] = []
    tags: List[str] = []

    n_rows, n_cols = df.shape

    # 1) 样本数检查
    if n_rows < cfg["min_rows"]:
        reasons.append(
            f"row_count={n_rows} < min_rows={cfg['min_rows']}，样本数量过少，训练无统计意义。"
        )
        suggestions.append("增加时间范围或合并多个交易日，重新抽样构建训练集。")
        tags.append("too_few_rows")
        return TrainingGateResult(
            ok_to_train=False,
            severity="block",
            reasons=reasons,
            suggestions=suggestions,
            tags=tags,
        )

    # 2) 特征数检查
    if n_cols < cfg["min_features"]:
        reasons.append(
            f"feature_count={n_cols} < min_features={cfg['min_features']}，特征过少。"
        )
        suggestions.append("检查特征注册表 / feature_recipe，是否被裁剪过多。")
        tags.append("too_few_features")
        # 这里不一定 block，先标记为 degraded
        severity = "degraded"
    else:
        severity = "ok"

    # 3) 基于 dataset_report 的 NaN / 零方差 等检查
    # 假设 dataset_report 中包含这些字段；如果没有就动态算
    col_nan_ratio = _safe_get(dataset_report, "nan_ratio_per_col")
    total_nan_ratio = _safe_get(dataset_report, "nan_ratio_all")

    if col_nan_ratio is None or total_nan_ratio is None:
        # fallback: 即时计算
        nan_per_col = df.isna().mean()
        col_nan_ratio = nan_per_col.to_dict()
        total_nan_ratio = float(df.isna().values.mean())

    # 全表 NaN 比例
    if total_nan_ratio > cfg["max_total_nan_ratio"]:
        reasons.append(
            f"总体缺失比例 total_nan_ratio={total_nan_ratio:.3f} > {cfg['max_total_nan_ratio']}。"
        )
        suggestions.append("检查 ETL / 特征计算链路，是否某一步输出了大量空值。")
        tags.append("high_nan_ratio")
        severity = "block"

    # 单列 NaN 比例
    bad_nan_cols = [
        col
        for col, r in col_nan_ratio.items()
        if isinstance(r, (int, float)) and r > cfg["max_nan_ratio_per_col"]
    ]
    if bad_nan_cols:
        reasons.append(
            f"以下特征缺失比例严重 (> {cfg['max_nan_ratio_per_col']}): {bad_nan_cols}。"
        )
        suggestions.append("考虑直接从训练集中移除这些字段，或在 FeatureRegistry 中标记为 'nullable' 并加填充逻辑。")
        tags.append("nan_heavy_features")
        # 不一定 block，但至少 degraded
        if severity != "block":
            severity = "degraded"

    # 4) 零方差特征
    zero_var_cols = _safe_get(dataset_report, "zero_variance_cols")
    if zero_var_cols is None:
        numeric_cols = df.select_dtypes(include=[np.number])
        variances = numeric_cols.var()
        zero_var_cols = variances[variances == 0.0].index.tolist()

    if zero_var_cols:
        frac = len(zero_var_cols) / max(1, n_cols)
        if frac > cfg["max_zero_var_fraction"]:
            reasons.append(
                f"零方差特征数量为 {len(zero_var_cols)}，占全部特征的 {frac:.2%}。"
            )
            suggestions.append("更新 FeatureRegistry，直接禁用这些几乎不变的字段。")
            tags.append("many_zero_variance_features")
            if severity == "ok":
                severity = "degraded"

    # 5) label 列是否存在大量 NaN
    if label_col not in df.columns:
        reasons.append(f"label 列 {label_col!r} 不在 dataset 中。")
        suggestions.append("检查 build_select_sql 的 target 定义与 LabelRegistry 配置。")
        tags.append("missing_label_column")
        return TrainingGateResult(
            ok_to_train=False,
            severity="block",
            reasons=reasons,
            suggestions=suggestions,
            tags=tags,
        )

    label_nan_ratio = float(df[label_col].isna().mean())
    if label_nan_ratio > 0.01:
        reasons.append(f"标签列 {label_col} 缺失比例为 {label_nan_ratio:.3%}。")
        suggestions.append("检查 label 生成逻辑，确保 shift/backfill 没有丢失过多样本。")
        tags.append("label_nan")
        if label_nan_ratio > 0.05:
            severity = "block"
        elif severity == "ok":
            severity = "degraded"

    ok_to_train = severity != "block"
    return TrainingGateResult(
        ok_to_train=ok_to_train,
        severity=severity,
        reasons=reasons,
        suggestions=suggestions,
        tags=tags,
    )


def evaluate_label_distribution(
    df: pd.DataFrame,
    label_col: str,
    task: str = "reg",
    config: Optional[Dict[str, Any]] = None,
) -> TrainingGateResult:
    """
    专门针对 label 做二次检查：
    - 回归：方差是否接近 0，是否极端长尾
    - 分类：类不平衡严重
    """
    cfg = {
        "min_label_std": 1e-4,             # label 标准差太小 → 无法学习
        "max_abs_label": 10.0,             # label 绝对值过大可能是爆炸
        "min_pos_fraction": 0.05,          # 分类：最少类比例
        "min_class_count": 100,            # 分类：每类至少多少样本
    }
    if config:
        cfg.update(config)

    reasons: List[str] = []
    suggestions: List[str] = []
    tags: List[str] = []

    if label_col not in df.columns:
        return TrainingGateResult(
            ok_to_train=False,
            severity="block",
            reasons=[f"label 列 {label_col!r} 不存在。"],
            suggestions=["检查 LabelRegistry 与 SQL 构造逻辑。"],
            tags=["missing_label_column"],
        )

    y = df[label_col].dropna()

    if y.empty:
        return TrainingGateResult(
            ok_to_train=False,
            severity="block",
            reasons=[f"label 列 {label_col!r} 全部为空或被过滤掉。"],
            suggestions=["检查 label 生成步骤的过滤条件。"],
            tags=["empty_label_series"],
        )

    if task == "reg":
        std = float(y.std())
        max_abs = float(np.abs(y).max())

        if std < cfg["min_label_std"]:
            reasons.append(
                f"label 标准差 std={std:.6f} 过小，几乎没有可学习信号。"
            )
            suggestions.append("可能是窗口太短/太长，或价格序列处理问题（如未去除停牌时段）。")
            tags.append("label_low_variance")
            return TrainingGateResult(
                ok_to_train=False,
                severity="block",
                reasons=reasons,
                suggestions=suggestions,
                tags=tags,
            )

        if max_abs > cfg["max_abs_label"]:
            reasons.append(
                f"label 绝对值最大值 max_abs={max_abs:.3f}，超过阈值 {cfg['max_abs_label']}。"
            )
            suggestions.append("检查是否误用 raw price 差值而不是百分比/对数收益。")
            tags.append("label_extreme_values")
            # 这里暂时 degraded，让人/AI 决定要不要剪尾或重定义 label
            return TrainingGateResult(
                ok_to_train=True,
                severity="degraded",
                reasons=reasons,
                suggestions=suggestions,
                tags=tags,
            )

        # 正常回归情况
        return TrainingGateResult(
            ok_to_train=True,
            severity="ok",
            reasons=[],
            suggestions=[],
            tags=[],
        )

    else:  # 分类
        # 简单假设二分类 {0,1} 或 {-1, 1}，你可以按自己标签规范调整
        value_counts = y.value_counts()
        total = int(value_counts.sum())
        if total == 0:
            return TrainingGateResult(
                ok_to_train=False,
                severity="block",
                reasons=["label 列没有有效样本。"],
                suggestions=["检查标签生成与过滤逻辑。"],
                tags=["empty_label_series"],
            )

        # 找到最小类
        min_class_count = int(value_counts.min())
        min_class_frac = min_class_count / total

        reasons_local: List[str] = []
        suggestions_local: List[str] = []
        tags_local: List[str] = []

        severity = "ok"

        if min_class_count < cfg["min_class_count"]:
            reasons_local.append(
                f"最少类别样本数为 {min_class_count} (< {cfg['min_class_count']})。"
            )
            suggestions_local.append("考虑延长样本区间或改变打标规则。")
            tags_local.append("class_imbalance_few_samples")
            severity = "degraded"

        if min_class_frac < cfg["min_pos_fraction"]:
            reasons_local.append(
                f"最少类别占比为 {min_class_frac:.3%} (< {cfg['min_pos_fraction']:.0%})，类别极度不平衡。"
            )
            suggestions_local.append("考虑使用 class_weight / focal loss / resampling。")
            tags_local.append("class_imbalance_ratio")
            if severity == "ok":
                severity = "degraded"

        ok_to_train = severity != "block"

        return TrainingGateResult(
            ok_to_train=ok_to_train,
            severity=severity,
            reasons=reasons_local,
            suggestions=suggestions_local,
            tags=tags_local,
        )


def evaluate_model_grid_health(
    hpo_grid_report: Dict[str, Any],
) -> TrainingGateResult:
    """
    审查模型网格健康度：
    - 没有任何模型候选 → block
    - 全部是 fallback 模型 → degraded
    """
    reasons: List[str] = []
    suggestions: List[str] = []
    tags: List[str] = []

    n_raw = int(hpo_grid_report.get("n_raw_models", 0) or 0)
    n_sane = int(hpo_grid_report.get("n_sane_models", 0) or 0)
    warnings = hpo_grid_report.get("warnings", []) or []

    severity = "ok"

    if n_raw == 0:
        reasons.append("LLM 未提供任何模型候选 (n_raw_models=0)。")
        suggestions.append("检查 training_plan 生成逻辑，至少提供 1 个模型类型。")
        tags.append("no_model_candidates")
        severity = "block"
    elif n_sane == 0:
        reasons.append("所有模型候选都被 GridGuard 拒绝 (n_sane_models=0)。")
        suggestions.append("LLM 提供的模型类型/参数全部越界，请收紧 prompt 或更新模型白名单。")
        tags.append("all_models_rejected")
        severity = "block"
    elif n_sane == 1 and n_raw > 1:
        reasons.append("多数模型候选被拒绝，只剩 1 个有效模型。")
        suggestions.append("考虑人工检查 warnings，并调整 prompt / ModelSpec 范围。")
        tags.append("limited_model_diversity")
        severity = "degraded"

    if warnings:
        reasons.append("GridGuard 产生的警告: " + " | ".join(warnings))
        tags.append("grid_warnings_present")
        if severity == "ok":
            severity = "degraded"

    ok_to_train = severity != "block"

    return TrainingGateResult(
        ok_to_train=ok_to_train,
        severity=severity,
        reasons=reasons,
        suggestions=suggestions,
        tags=tags,
    )


def run_training_gate(
    *,
    df: pd.DataFrame,
    dataset_report: Dict[str, Any],
    label_col: str,
    task: str,
    hpo_grid_report: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> TrainingGateResult:
    """
    统一入口：综合数据 + 标签 + 模型三方面的 gate 结果。
    """
    cfg = config or {}

    # 1) 数据健康
    ds_result = evaluate_dataset_health(
        df=df,
        dataset_report=dataset_report,
        label_col=label_col,
        config=cfg.get("dataset"),
    )

    # 2) 标签分布
    label_result = evaluate_label_distribution(
        df=df,
        label_col=label_col,
        task=task,
        config=cfg.get("label"),
    )

    # 3) 模型网格
    grid_result = evaluate_model_grid_health(hpo_grid_report=hpo_grid_report)

    reasons: List[str] = []
    suggestions: List[str] = []
    tags: List[str] = []

    for r in (ds_result, label_result, grid_result):
        reasons.extend(r.reasons)
        suggestions.extend(r.suggestions)
        tags.extend(r.tags)

    # 综合 severity
    severities = {ds_result.severity, label_result.severity, grid_result.severity}
    if "block" in severities:
        severity = "block"
    elif "degraded" in severities:
        severity = "degraded"
    else:
        severity = "ok"

    ok_to_train = severity != "block"

    return TrainingGateResult(
        ok_to_train=ok_to_train,
        severity=severity,
        reasons=reasons,
        suggestions=suggestions,
        tags=list(sorted(set(tags))),
    )
```

这一个文件，就变成 **“训练前总闸门”**。

---

## 三、在训练入口接上 TrainingGate

你现在的流程大概是这样：

* `PrincipalInvestigator.propose()` → 得到 `PIResult(dataset_df, training_plan, hpo_grid, ...)`
* 之前我们已经有 `dataset_sanity.run_dataset_sanity_checks(df)` 返回 `dataset_report`
* HPO 入口（例如 `wf_hpo.py` 或某个 orchestrator）拿着 `PIResult` 就直接开跑 HPO 了

现在，我们在 HPO 入口加一段：

### 3.1 示例：在 HPO 入口文件里使用

假设有类似这样的代码（伪代码）：

```python
# wf_hpo.py (示意)

from training_gate import run_training_gate
from dataset_sanity import run_dataset_sanity_checks
from pi.principal_investigator import PrincipalInvestigator

...

pi = PrincipalInvestigator(ch_cfg=..., llm_cfg=...)
pi_result = pi.propose(
    table=...,
    symbol=...,
    start_ts=...,
    end_ts=...,
    goal=...,
    horizon=...,
)

df = pi_result.dataset_df
tplan = pi_result.training_plan
hpo_grid = pi_result.hpo_grid

# 1) 体检
dataset_report, dataset_tags = run_dataset_sanity_checks(df)

# 2) 模型网格报告：从 training_plan 或 grid_guard 返回结果中取
hpo_grid_report = {
    "n_raw_models": tplan.get("hpo_grid_model_count_raw", tplan.get("hpo_grid_model_count", 0)),
    "n_sane_models": tplan.get("hpo_grid_model_count", 0),
    "warnings": tplan.get("hpo_grid_warnings", []),
}

label_col = tplan.get("target", "label")    # 视你 LabelRegistry 约定而定
task = tplan.get("task", "reg")

# 3) 训练总闸门
gate_result = run_training_gate(
    df=df,
    dataset_report=dataset_report,
    label_col=label_col,
    task=task,
    hpo_grid_report=hpo_grid_report,
)

if not gate_result.ok_to_train:
    logger.warning(
        "Training blocked by TrainingGate: severity=%s reasons=%s",
        gate_result.severity,
        gate_result.reasons,
    )
    # 这里可以：
    # - 记录到 DB / outbox_event
    # - 把 gate_result.to_dict() 喂给 AI 大脑，让它知道“这批数据不能用”
    return

# 4) 正常进入 HPO / 训练
run_hpo(
    df=df,
    grid=hpo_grid,
    task=task,
    ...
)
```

> 真实项目里，你的 `dataset_sanity` / `PIResult` / `training_plan` 字段命名会有出入，你只要按上面的逻辑对齐字段即可。

---

## 四、把 GateResult 接入 AI 大脑的上下文

下一步是让 **AI 大脑“知道”这次训练为什么会被拒绝 / 降级**，从而在策略迁移/多头脑协作时做出更聪明的决策。

比如你有一个构建 Brain payload 的函数（伪）：

```python
def build_brain_context(..., pi_result: PIResult, gate_result: TrainingGateResult, dataset_report: Dict[str, Any]) -> Dict[str, Any]:
    ctx = {}
    ...
    ctx["data_health"] = {
        "dataset_tags": dataset_report.get("tags", []),
        "training_gate": gate_result.to_dict(),
    }
    ...
    return ctx
```

然后在 LLM prompt 里加一小段说明：

* 如果 `training_gate.ok_to_train == False`：

  > 当前数据集被 TrainingGate 拒绝，原因已经给出。你不能继续要求训练，而是需要输出“拒绝训练 + 建议”。

* 如果 `severity == "degraded"`：

  > 当前数据集标记为“质量受损”，你可以继续训练，但在结论中必须明确标记风险，并 **禁止把这种结果作为“强信号策略”上线**。

这部分是 AI 大脑 prompt 的小修改，你现在的 prompts JSON 里已经有一堆战术版本，我们之后可以专门写一个“小段落”，统一插入。

---

## 五、现在系统已经具备的“硬防线”

总结一下：做到这里，你的 AI-自动研究流水线已经有了三道真正“机构级”的硬防线：

1. **数据体检层（DatasetSanity + TrainingGate dataset 部分）**

   * 拦截：样本数过少、NaN 爆炸、特征全常数、label 列缺失等。

2. **标签防泄露 & 质量层（LabelRegistry + TrainingGate label 部分）**

   * 拦截：label 方差接近 0、极端爆炸值、分类严重不平衡等。

3. **模型池约束层（GridGuard + TrainingGate model 部分）**

   * 拦截：LLM 随便乱提模型、乱给超参、所有候选被拒绝等。

> 这三道防线的最大价值是：
> **即便有一天你把大脑换成别的 LLM、Prompt 完全改写，只要这三层不动，系统仍然是“不会训练垃圾模型”的。**

---

如果你再说一次「继续」，我会往下做：

* **把 LabelRegistry（标签契约）做成和 FeatureRegistry 对称的 JSON + 解析层**，
* 并给你一套标准的 **time-bar / event-bar / triple-barrier** 标签定义模板，
  让 AI 大脑在“用什么标签”和“怎么匹配策略/回测逻辑”这块也变成契约化，而不是口头约定。
