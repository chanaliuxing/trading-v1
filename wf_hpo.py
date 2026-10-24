# -*- coding: utf-8 -*-
"""
wf_hpo.py
---------
Purged walk-forward cross-validation + grid search HPO scaffold
for the BacktestEngine defined in backtest_engine.py.

Enhancements:
- make_folds(): auto rolling-window fold generator (IS/OOS/GAP/STEP days)
- Optional parallel grid evaluation via ProcessPoolExecutor
- Export helpers for CSV/Parquet leaderboards
- Extended objectives: sortino, omega(τ=0), calmar, and custom formula
- Plot exports: OOS equity curves, OOS drawdowns, IS heatmap for top-2 varied params

Author: ChatGPT
"""

from __future__ import annotations
import os, sys, math, json, itertools, statistics, time, ast, hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Iterable, Tuple, Set
from datetime import datetime, timedelta, timezone
from concurrent.futures import ProcessPoolExecutor, as_completed
import multiprocessing

# Fix macOS multiprocessing hanging issue by using 'spawn' instead of 'fork'
# This must be set before creating any ProcessPoolExecutor
if sys.platform == 'darwin':  # macOS
    multiprocessing.set_start_method('spawn', force=True)

# ⭐ Import IC calculator for prediction quality metrics (机构级Spearman IC)
try:
    from libs.features.ic_metrics import compute_ic_oos
    HAS_IC_CALCULATOR = True
except ImportError:
    HAS_IC_CALCULATOR = False
    def compute_ic_oos(*args, **kwargs):
        return (0.0, 0.0)  # (ic, coverage)

import pandas as pd
import gc
import numpy as np

try:
    import matplotlib
    matplotlib.use('Agg')  # Non-interactive backend
    import matplotlib.pyplot as plt
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False

try:
    import yaml  # type: ignore
    HAS_YAML = True
except ImportError:
    yaml = None  # type: ignore
    HAS_YAML = False

# Import engine from current working dir
from backtest_ml_engine import BacktestConfig, BacktestEngine
from metrics_extra import probabilistic_sharpe_ratio, deflated_sharpe, pbo_fraction
from leakage_guard import LeakageGuard, LeakageReport, apply_embargo_to_splits

# Prometheus metrics for leakage detection
try:
    from prometheus_client import Counter, Gauge, Histogram, Info
    leakage_checks_total = Counter('ml_leakage_checks_total', 'Total leakage checks performed', ['status'])
    leakage_issues_total = Counter('ml_leakage_issues_total', 'Total leakage issues detected', ['level', 'check_name'])
    leakage_check_duration = Histogram('ml_leakage_check_duration_seconds', 'Leakage check duration')
    leakage_overlap_ratio = Gauge('ml_leakage_overlap_ratio', 'Train/val overlap ratio', ['symbol'])
    HAS_PROMETHEUS = True
except ImportError:
    HAS_PROMETHEUS = False
    leakage_checks_total = None
    leakage_issues_total = None
    leakage_check_duration = None
    leakage_overlap_ratio = None

from models import (
    XGBModel,
    LSTMModel,
    TransformerBaseline,
    ETSformer,
    iTransformer,
    DecisionScheme,
    ModelAdapter,
)

# Linear / KNN / LightGBM models
from models_linear import (
    LassoModel,
    RidgeModel,
    ElasticNetModel,
    LogisticModel,
    LinearSVMModel,
    KNNModel,
    LightGBMModel,
    RuleFitModel,
    MODEL_POOL,
    get_linear_model_class,
)

# Ensemble model (weighted average)
from models_ensemble import EnsembleModel

from strategy_registry import StrategyRegistry

# Feature selection imports
try:
    from sklearn.linear_model import Lasso as SkLasso
    HAS_SKLASSO = True
except Exception:
    SkLasso = None
    HAS_SKLASSO = False

try:
    import xgboost as xgb
    HAS_XGB_FS = True
except Exception:
    xgb = None
    HAS_XGB_FS = False

# ============================================================================
# Model Space Templates (模型空间模板 - AI大脑可直接引用)
# ============================================================================

MODEL_SPACE_TEMPLATES: Dict[str, Dict[str, Any]] = {
    # 线性回归家族（L1/L2/ElasticNet）
    "linear_reg_basic": {
        "model_type": {
            "type": "categorical",
            "choices": ["lasso", "ridge", "elasticnet"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                {"alpha": 1e-5, "max_iter": 1000},
                {"alpha": 1e-4, "max_iter": 1000},
                {"alpha": 1e-3, "max_iter": 1000},
                {"alpha": 1e-2, "max_iter": 1000},
                {"alpha": 1e-1, "max_iter": 1000},
                {"alpha": 0.01, "l1_ratio": 0.3, "max_iter": 2000},  # ElasticNet
                {"alpha": 0.01, "l1_ratio": 0.5, "max_iter": 2000},
                {"alpha": 0.01, "l1_ratio": 0.7, "max_iter": 2000},
            ]
        },
    },

    # 线性分类家族（用于方向/涨跌信号）
    "linear_clf_basic": {
        "model_type": {
            "type": "categorical",
            "choices": ["logistic", "svm_linear"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                {"C": 1e-3},
                {"C": 1e-2},
                {"C": 1e-1},
                {"C": 1.0},
                {"C": 10.0},
                {"C": 100.0},
            ]
        },
    },

    # KNN（回归/分类通用）
    "knn_basic": {
        "model_type": {
            "type": "categorical",
            "choices": ["knn"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                {"n_neighbors": 3},
                {"n_neighbors": 5},
                {"n_neighbors": 7},
                {"n_neighbors": 11},
                {"n_neighbors": 15},
                {"n_neighbors": 21},
            ]
        },
    },

    # XGBoost 回归的一个小空间（避免炸 M1）
    "xgb_reg_basic": {
        "model_type": {
            "type": "categorical",
            "choices": ["xgb"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                {"n_estimators": 100, "max_depth": 3, "eta": 0.05, "subsample": 0.8, "colsample_bytree": 0.8},
                {"n_estimators": 200, "max_depth": 4, "eta": 0.05, "subsample": 0.8, "colsample_bytree": 0.8},
                {"n_estimators": 200, "max_depth": 5, "eta": 0.1, "subsample": 0.7, "colsample_bytree": 0.7},
                {"n_estimators": 300, "max_depth": 4, "eta": 0.03, "subsample": 0.8, "colsample_bytree": 0.8},
                {"n_estimators": 400, "max_depth": 3, "eta": 0.02, "subsample": 0.9, "colsample_bytree": 0.9},
            ]
        },
    },

    # LightGBM 基础空间
    "lgbm_reg_basic": {
        "model_type": {
            "type": "categorical",
            "choices": ["lgbm"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                {"learning_rate": 0.05, "num_leaves": 31, "n_estimators": 100, "max_depth": -1},
                {"learning_rate": 0.05, "num_leaves": 31, "n_estimators": 200, "max_depth": 6},
                {"learning_rate": 0.1, "num_leaves": 63, "n_estimators": 100, "max_depth": 6},
                {"learning_rate": 0.1, "num_leaves": 63, "n_estimators": 200, "max_depth": 8},
            ]
        },
    },

    # 一个「混合小池」：线性 + XGB + KNN
    "mixed_small": {
        "model_type": {
            "type": "categorical",
            "choices": ["xgb", "lasso", "ridge", "elasticnet", "knn"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                # XGB variants
                {"n_estimators": 100, "max_depth": 3, "eta": 0.05},
                {"n_estimators": 200, "max_depth": 4, "eta": 0.05},
                # Linear variants
                {"alpha": 0.001, "max_iter": 1000},
                {"alpha": 0.01, "max_iter": 1000},
                {"alpha": 0.1, "max_iter": 1000},
                {"alpha": 0.01, "l1_ratio": 0.5, "max_iter": 2000},
                # KNN variants
                {"n_neighbors": 5},
                {"n_neighbors": 11},
                {"n_neighbors": 21},
            ]
        },
    },

    # 树模型组合（XGB + LightGBM）
    "tree_ensemble": {
        "model_type": {
            "type": "categorical",
            "choices": ["xgb", "lgbm"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                # XGB
                {"n_estimators": 200, "max_depth": 4, "eta": 0.05},
                {"n_estimators": 300, "max_depth": 5, "eta": 0.03},
                # LightGBM
                {"learning_rate": 0.05, "num_leaves": 31, "n_estimators": 200},
                {"learning_rate": 0.1, "num_leaves": 63, "n_estimators": 100},
            ]
        },
    },

    # 全家桶（适合探索阶段）
    "full_exploration": {
        "model_type": {
            "type": "categorical",
            "choices": ["xgb", "lgbm", "lasso", "ridge", "elasticnet", "knn", "logistic"],
        },
        "model_params": {
            "type": "categorical",
            "choices": [
                # Tree-based
                {"n_estimators": 100, "max_depth": 3, "eta": 0.05},
                {"n_estimators": 200, "max_depth": 4, "eta": 0.05},
                {"learning_rate": 0.05, "num_leaves": 31, "n_estimators": 200},
                # Linear regression
                {"alpha": 0.001},
                {"alpha": 0.01},
                {"alpha": 0.1},
                {"alpha": 0.01, "l1_ratio": 0.5, "max_iter": 2000},
                # Classification
                {"C": 0.1},
                {"C": 1.0},
                {"C": 10.0},
                # KNN
                {"n_neighbors": 5},
                {"n_neighbors": 15},
            ]
        },
    },
}


def apply_model_space_template(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    如果 config 里写了 model_space_template，
    且没有手动提供 optuna_space，就自动填充 optuna_space。
    这样老配置完全不受影响。

    Args:
        cfg: HPO configuration dict

    Returns:
        Updated cfg with optuna_space populated from template (if applicable)
    """
    # 如果已经有 optuna_space，不覆盖
    hpo_section = cfg.get("hpo", {})
    if "optuna_space" in hpo_section:
        return cfg

    # 检查是否指定了模板
    tpl_name = hpo_section.get("model_space_template")
    if not tpl_name:
        return cfg

    # 查找模板
    tpl = MODEL_SPACE_TEMPLATES.get(tpl_name)
    if not tpl:
        print(f"[HPO] WARNING: unknown model_space_template={tpl_name}, ignoring.")
        return cfg

    # 浅拷贝一份，防止后续修改污染全局模板
    if "hpo" not in cfg:
        cfg["hpo"] = {}
    cfg["hpo"]["optuna_space"] = {k: dict(v) for k, v in tpl.items()}

    print(f"[HPO] Using model_space_template='{tpl_name}' to populate optuna_space")
    print(f"[HPO]   - model_type choices: {tpl.get('model_type', {}).get('choices', [])}")
    print(f"[HPO]   - model_params variants: {len(tpl.get('model_params', {}).get('choices', []))}")

    return cfg


def load_label_profile(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    从 label_profiles.yml 中加载指定的标签/损失配置，塞到 cfg["label_cfg"] 里。
    如果没有 label_profile 字段，则保持现有逻辑不变（向后兼容）。

    Args:
        cfg: HPO configuration dict with optional "label_profile" field

    Returns:
        Updated cfg with label_cfg populated from profile (if applicable)

    Usage in config:
        {
          "label_profile": "intraday_reg_5m",
          "label_profiles_path": "label_profiles.yml"  # optional, defaults to label_profiles.yml
        }
    """
    # 检查是否指定了 label_profile
    profile_name = cfg.get("label_profile")
    if not profile_name:
        return cfg

    # 检查 YAML 是否可用
    if not HAS_YAML or yaml is None:
        raise RuntimeError(
            "PyYAML not installed, cannot load label profiles. "
            "Install with: pip install pyyaml"
        )

    # 读取 YAML 文件
    label_path = cfg.get("label_profiles_path", "label_profiles.yml")
    if not os.path.exists(label_path):
        raise FileNotFoundError(
            f"Label profiles file not found: {label_path}\n"
            f"Current directory: {os.getcwd()}"
        )

    with open(label_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    profiles = data.get("profiles") or {}
    if profile_name not in profiles:
        available = list(profiles.keys())
        raise KeyError(
            f"label_profile='{profile_name}' not found in {label_path}\n"
            f"Available profiles: {available}"
        )

    # 加载配置
    label_cfg = profiles[profile_name]
    cfg["label_cfg"] = label_cfg

    # 自动填充 task 和 objective（如果配置里没有）
    hpo_section = cfg.get("hpo", {})
    if "model_task" not in hpo_section and "task" in label_cfg:
        hpo_section["model_task"] = label_cfg["task"]
        cfg["hpo"] = hpo_section

    if "objective" not in hpo_section and "objective" in label_cfg:
        hpo_section["objective"] = dict(label_cfg["objective"])
        cfg["hpo"] = hpo_section

    # 日志输出
    print(f"[Label] Using label_profile='{profile_name}'")
    print(f"[Label]   - task: {label_cfg.get('task', 'N/A')}")
    print(f"[Label]   - horizon: {label_cfg.get('horizon_bars', 'N/A')} bars")
    transform = label_cfg.get("base", {}).get("transform", "N/A")
    print(f"[Label]   - transform: {transform}")
    obj_metric = label_cfg.get("objective", {}).get("metric", "N/A")
    print(f"[Label]   - objective: {obj_metric}")

    return cfg


# ============================================================================
# Weighted Ensemble Model (轻量级实现，直接在 wf_hpo 中定义)
# ============================================================================

@dataclass
class WeightedEnsembleMetadata:
    name: str
    task: str
    num_models: int
    model_names: list

class WeightedEnsembleModel:
    """
    简单加权集成：
    - 持有多个 base model
    - predict = 加权平均各个 base model 的预测
    - to_signal：优先用第一个子模型的 to_signal / decision_scheme
    """

    def __init__(
        self,
        models,
        weights=None,
        task: str = "reg",
        name: str = "ensemble",
        decision_scheme=None,
    ):
        if not models:
            raise ValueError("WeightedEnsembleModel requires at least one base model")
        self.models = list(models)
        self.task = task
        self.name = name
        self.decision_scheme = decision_scheme

        # lookback = 子模型里最大的 lookback
        self.lookback = max(getattr(m, "lookback", 1) or 1 for m in self.models)

        if weights is None:
            weights = [1.0] * len(self.models)
        w = np.asarray(weights, dtype="float64")
        if not np.isfinite(w).all() or w.sum() <= 0:
            w = np.ones_like(w)
        self.weights = w / w.sum()

    # 兼容现有调用：虽然我们一般在外面 fit 子模型，但为了安全还是实现一下
    def fit(self, X, y):
        for m in self.models:
            if hasattr(m, "fit"):
                m.fit(X, y)
        return self

    def predict(self, X):
        preds = []
        for m in self.models:
            p = m.predict(X)
            p = np.asarray(p).reshape(-1)
            preds.append(p)
        preds_stack = np.vstack(preds)  # [n_models, n_samples]
        return np.dot(self.weights, preds_stack)  # [n_samples]

    def to_signal(self, preds):
        # 优先用第一个子模型的 to_signal（它已经和 DecisionScheme 对齐）
        # Note: ModelAdapter.to_signal returns single array (signals), not tuple
        first = self.models[0]
        if hasattr(first, "to_signal"):
            return first.to_signal(preds)

        # 退化兜底：按回归预测未来收益处理
        preds = np.asarray(preds)
        signals = np.where(preds > 0, 1, np.where(preds < 0, -1, 0))
        return signals

    def metadata(self):
        return WeightedEnsembleMetadata(
            name=self.name,
            task=self.task,
            num_models=len(self.models),
            model_names=[getattr(m, "name", type(m).__name__) for m in self.models],
        )

# Feature DSL
from libs.features.feature_dsl import FeatureProgramRunner

# Feature Engineering v2（机构级零拷贝特征注入）
try:
    from libs.features.features_injector import prepare_features_for_training
    HAS_FEATURES_INJECTOR = True
except ImportError:
    HAS_FEATURES_INJECTOR = False
    prepare_features_for_training = None

# >>> FT-INTEGRATION BEGIN <<<
# Featuretools DFS（自动特征工程）
try:
    from libs.features.featuretools_adapter import FeaturetoolsAdapter
    HAS_FEATURETOOLS = True
except ImportError:
    HAS_FEATURETOOLS = False
    FeaturetoolsAdapter = None
# >>> FT-INTEGRATION END <<<

# >>> REGISTRY-INTEGRATION BEGIN <<<
# 机构级统一模型注册表 (Lasso/Ridge/ElasticNet/LogReg/RF/LightGBM/CatBoost/etc.)
try:
    from models.registry import build_model_from_policy, get_supported_models, _SklearnLinearAdapter
    from models.optuna_spaces import suggest_model_params, suggest_scaler
    HAS_MODEL_REGISTRY = True
except ImportError:
    HAS_MODEL_REGISTRY = False
    build_model_from_policy = None
    get_supported_models = None
# >>> REGISTRY-INTEGRATION END <<<

# >>> MODEL-POOL-INTEGRATION BEGIN <<<
# 机构级模型池 + 二层集成 (Layered Ensemble: Lasso/Ridge/XGB/LGBM/KNN/SVM)
try:
    from models.model_pool import build_model_from_cfg as build_model_pool, LayeredEnsemble
    HAS_MODEL_POOL = True
except ImportError:
    HAS_MODEL_POOL = False
    build_model_pool = None
    LayeredEnsemble = None
# >>> MODEL-POOL-INTEGRATION END <<<

# ------------------ Utilities ------------------

def _dt(s: str) -> datetime:
    # Accept "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DDTHH:MM:SS"
    s2 = s.replace("T", " ")
    if len(s2) == 10:
        s2 += " 00:00:00"
    return datetime.fromisoformat(s2)

def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _product_dict(grid: Dict[str, Iterable[Any]]) -> List[Dict[str, Any]]:
    keys = list(grid.keys())
    vals = [list(v) for v in grid.values()]
    combos = []
    for tup in itertools.product(*vals):
        combos.append({k: v for k, v in zip(keys, tup)})
    return combos

def _jsonify(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(obj)

def _ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

# ------------------ Resume Capability Helpers ------------------

def _hash_params(params: Dict[str, Any]) -> str:
    """Generate a consistent hash for parameter dict to detect duplicates."""
    # Sort keys for consistency
    params_str = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return hashlib.md5(params_str.encode()).hexdigest()

def _save_trial_result(out_dir: str, gen: int, fold_id: int, params: Dict[str, Any],
                       results: Dict[str, Any], phase: str = "IS") -> None:
    """Append a completed trial to the JSONL log (atomic operation)."""
    trial_data = {
        "gen": gen,
        "fold_id": fold_id,
        "params_hash": _hash_params(params),
        "params": params,
        "results": results,
        "phase": phase,
        "ts": datetime.now().isoformat()
    }

    # Atomic append to JSONL
    trials_file = os.path.join(out_dir, "completed_trials.jsonl")
    with open(trials_file, "a") as f:
        f.write(json.dumps(trial_data, ensure_ascii=False) + "\n")
        f.flush()  # Force write to disk

def _load_resume_state(out_dir: str) -> Optional[Dict[str, Any]]:
    """Load previously completed trials from JSONL file."""
    trials_file = os.path.join(out_dir, "completed_trials.jsonl")

    if not os.path.exists(trials_file):
        return None

    completed_trials = []
    with open(trials_file, "r") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                trial_data = json.loads(line)
                completed_trials.append(trial_data)
            except json.JSONDecodeError as e:
                print(f"[Resume WARNING] Corrupted line {line_num} in {trials_file}: {e}")
                continue

    if not completed_trials:
        return None

    return {"trials": completed_trials}

def _update_resume_state(out_dir: str, gen: int, fold_id: int, total_trials: int) -> None:
    """Update resume state file with current progress (atomic write)."""
    state = {
        "last_completed_gen": gen,
        "last_completed_fold": fold_id,
        "total_trials_completed": total_trials,
        "last_update": datetime.now().isoformat()
    }

    # Atomic write: write to temp then rename
    state_file = os.path.join(out_dir, "resume_state.json")
    temp_file = state_file + ".tmp"
    with open(temp_file, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(temp_file, state_file)  # Atomic on POSIX

# ------------------ Fold Generator ------------------

def make_folds(start: str, end: str, *, is_days: float, oos_days: float, gap_days: float = 0, step_days: float | None = None, embargo_days: float = 0) -> List[Dict[str, Any]]:
    """
    Build rolling time windows:
      [IS start, IS end] --(gap)--> [OOS start, OOS end]
    Windows slide forward by step_days (default = oos_days).

    If embargo_days > 0, applies leakage_guard.apply_embargo_to_splits() to ensure
    OOS periods are pushed forward to prevent label leakage.

    Note: All day parameters accept floats (e.g., 0.1 days = 2.4 hours).
    """
    start_dt = _dt(start)
    end_dt = _dt(end)
    if step_days is None or step_days <= 0:
        step_days = oos_days
    folds = []
    fid = 1
    cursor = start_dt

    # First generate all splits
    splits_raw = []
    max_iterations = 10000  # Safety: prevent infinite loop
    iteration = 0
    while True:
        iteration += 1
        if iteration > max_iterations:
            raise ValueError(f"make_folds exceeded {max_iterations} iterations. Check step_days > 0 and fold configuration.")

        is_start = cursor
        is_end = is_start + timedelta(days=is_days)
        oos_start = is_end + timedelta(days=gap_days)
        oos_end = oos_start + timedelta(days=oos_days)
        if oos_end > end_dt:
            break
        splits_raw.append((is_start, is_end, oos_start, oos_end))
        cursor = cursor + timedelta(days=step_days)

        # Extra safety: if step_days is 0, break immediately
        if step_days == 0:
            raise ValueError("step_days must be > 0 to avoid infinite loop")

    # Apply embargo if specified
    if embargo_days > 0:
        embargo_delta = pd.Timedelta(days=embargo_days)
        splits_embargoed = apply_embargo_to_splits(splits_raw, embargo_delta)
    else:
        splits_embargoed = splits_raw

    # Convert to fold dicts
    for fid, (is_start, is_end, oos_start, oos_end) in enumerate(splits_embargoed, start=1):
        # Validate: skip if embargo pushed OOS beyond end_dt
        if oos_end > end_dt:
            continue
        folds.append({
            "fold_id": fid,
            "is_start": _fmt_dt(is_start),
            "is_end": _fmt_dt(is_end),
            "oos_start": _fmt_dt(oos_start),
            "oos_end": _fmt_dt(oos_end),
            "purge_days": gap_days,
            "embargo_days": embargo_days
        })

    return folds

# ------------------ Data Models ------------------

@dataclass
class Fold:
    fold_id: int
    is_start: str
    is_end: str
    oos_start: str
    oos_end: str
    purge_days: int = 0
    embargo_days: int = 0

@dataclass
class Objective:
    metric: str = "sharpe"      # "sharpe" | "total_return" | "profit_factor" | "win_rate" | "sortino" | "omega" | "calmar"
    maximize: bool = True
    min_trades: int = 0         # filter candidates
    max_mdd: Optional[float] = None  # e.g. 0.25 (25%)
    min_pnl: Optional[float] = None  # e.g. -5000 (minimum acceptable PnL)
    threshold: float = 0.0      # for omega (τ), default 0
    formula: Optional[str] = None  # e.g. "sharpe - 2.0*max_drawdown"

# ------------------ Engine helper (process-safe) ------------------

def _run_engine_external(
    bt_cfg_dict: Dict[str, Any],
    start: str,
    end: str,
    strategy_params: Dict[str, Any],
    df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    import gc
    cfg = BacktestConfig(**bt_cfg_dict)
    cfg.start = start
    cfg.end = end
    cfg.strategy_params = strategy_params
    eng = BacktestEngine(cfg)
    result = eng.run(df=df)

    # MEMORY FIX: Explicit cleanup of backtest engine
    # Clear internal state before deleting
    if hasattr(eng, 'strategy') and eng.strategy:
        eng.strategy = None
    if hasattr(eng, 'broker') and eng.broker:
        eng.broker = None
    if hasattr(eng, 'orders'):
        eng.orders = []
    if hasattr(eng, 'trades'):
        eng.trades = []
    del eng, cfg
    gc.collect()

    return result

# ------------------ Derived metrics from equity ------------------

def _equity_to_returns(equity_curve: List[Tuple[int, float]]) -> List[float]:
    rets: List[float] = []
    if not equity_curve or len(equity_curve) < 2:
        return rets
    eq = [float(e) for _, e in equity_curve]
    for i in range(1, len(eq)):
        prev = eq[i-1] if eq[i-1] != 0 else 1e-9
        rets.append((eq[i]-eq[i-1]) / prev)
    return rets

def _sortino(returns: List[float]) -> float:
    if not returns:
        return 0.0
    neg = [r for r in returns if r < 0]
    down_stdev = (sum(r*r for r in neg)/len(neg))**0.5 if neg else 1e-9
    mean_r = sum(returns)/len(returns)
    return mean_r / down_stdev

def _omega(returns: List[float], tau: float = 0.0) -> float:
    if not returns:
        return 0.0
    gains = sum(max(r - tau, 0.0) for r in returns)
    losses = sum(max(tau - r, 0.0) for r in returns)
    return gains / (losses if losses > 0 else 1e-9)

def _calmar(total_return: float, max_drawdown: float) -> float:
    if max_drawdown <= 0:
        return float('inf')
    return total_return / max_drawdown

def _merge_metrics_with_extras(res: Dict[str, Any], objective: Objective) -> Dict[str, Any]:
    row = dict(res.get("metrics", {}))
    eq = res.get("equity_curve", [])
    rets = _equity_to_returns(eq)
    row["sortino"] = float(_sortino(rets))
    row["omega"] = float(_omega(rets, objective.threshold if objective else 0.0))
    row["calmar"] = float(_calmar(row.get("total_return", 0.0), row.get("max_drawdown", 0.0)))
    # Intraday-view helpers: approximate daily PnL, trades/day, and intraday MDD
    try:
        # Daily PnL from daily_stats if available
        daily_stats = res.get("daily_stats") or {}
        if isinstance(daily_stats, dict):
            pnl_vals = []
            for v in daily_stats.values():
                try:
                    if isinstance(v, dict) and "pnl" in v:
                        pnl_vals.append(float(v["pnl"]))
                except Exception:
                    continue
            if pnl_vals:
                row["intraday_pnl_mean"] = float(sum(pnl_vals) / len(pnl_vals))
                row["intraday_pnl_median"] = float(sorted(pnl_vals)[len(pnl_vals) // 2])
        # Trades per day if trade_stats present
        trade_stats = res.get("trade_stats") or {}
        if isinstance(trade_stats, dict):
            days = float(trade_stats.get("days", 0.0) or 0.0)
            if days > 0:
                row["trades_per_day"] = float((trade_stats.get("n_trades", 0.0) or 0.0) / days)
        # Intraday max drawdown from equity curve (per fold)
        if eq:
            peak = -1e18
            max_dd = 0.0
            for _, e in eq:
                e = float(e)
                peak = max(peak, e)
                if peak > 0:
                    dd = (peak - e) / peak
                    if dd > max_dd:
                        max_dd = dd
            row["intraday_max_drawdown"] = float(max_dd)
    except Exception:
        # Best-effort; do not break core metrics
        pass
    return row

# ------------------ Safe formula eval ------------------

_ALLOWED_NODES = {
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Num, ast.Load, ast.Name, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Mod, ast.Constant, ast.FloorDiv
}

def _safe_eval_formula(expr: str, context: Dict[str, float]) -> float:
    tree = ast.parse(expr, mode='eval')
    for node in ast.walk(tree):
        if type(node) not in _ALLOWED_NODES:
            raise ValueError(f"Unsafe or unsupported expression node: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id not in context:
            raise ValueError(f"Unknown name in formula: {node.id}")
    return float(eval(compile(tree, "<formula>", "eval"), {"__builtins__": {}}, context))

# ==============================================================================
# Model Building & Feature Selection Helpers (机构级模型池 + 自动特征选择)
# ==============================================================================

def _build_base_model(
    *,
    task: str,
    model_type: str,
    params: dict,
    decision_scheme,
) -> "ModelAdapter":
    """
    构建单个基础模型（XGB / 线性 / 深度），供单模型和 Ensemble 共用。

    注意：这里不做 Ensemble，只是"单个模型"的工厂。
    """
    mt = (model_type or "").lower()
    raw_hparams = params or {}

    # XGB 允许的 key
    xgb_keys = (
        "max_depth",
        "learning_rate",
        "n_estimators",
        "subsample",
        "colsample_bytree",
        "min_child_weight",
        "gamma",
        "reg_alpha",
        "reg_lambda",
        "max_delta_step",
        "eta",
        "objective",
        "eval_metric",
        "tree_method",
    )

    # 深度模型 key
    seq_keys = (
        "hidden_size",
        "num_layers",
        "dropout",
        "nhead",
        "dim_feedforward",
        "num_encoder_layers",
        "lookback",
        "hidden",
        "hidden_dim",
        "pooling",
        "bidirectional",
        "use_attention",
        "layer_norm",
        "batch_size",
        "epochs",
        "patience",
        "lr",
        "weight_decay",
        "device",
        "d_model",
        "alpha_init",
    )

    # 线性/经典模型 key
    linear_keys = (
        "alpha",
        "l1_ratio",
        "tol",
        "max_iter",
        "C",
        "penalty",
        "solver",
        "n_neighbors",
        "weights",
        "num_leaves",
        "min_child_samples",
        "tree_size",
        "sample_fract",
        "max_rules",
        "memory_par",
    )

    # 1) 按模型族过滤 hparams
    if mt in {"xgb", "xgboost"}:
        hparams = {k: v for k, v in raw_hparams.items() if k in xgb_keys}
    elif mt in MODEL_POOL:
        # 线性 / 经典模型直接吃全部 raw_hparams（alpha / C / l1_ratio / n_neighbors 等）
        hparams = {k: v for k, v in raw_hparams.items() if k in linear_keys}
    else:
        # 深度模型只取 seq_keys
        hparams = {k: v for k, v in raw_hparams.items() if k in seq_keys}

    # 2) 实例化
    if mt in {"xgb", "xgboost"}:
        model: ModelAdapter = XGBModel(
            task=task,
            hparams=hparams,
            decision_scheme=decision_scheme,
        )

    elif mt in MODEL_POOL:
        if get_linear_model_class is None:
            raise RuntimeError(
                f"model_type={model_type!r} is linear-family but models_linear not available"
            )
        LinearCls = get_linear_model_class(mt)
        model = LinearCls(
            task=task,
            hparams=hparams,
            decision_scheme=decision_scheme,
        )

    elif mt in ("lstm", "transformer_baseline", "etsformer", "itransformer", "i-transformer"):
        if mt == "lstm":
            deep_model_cls = LSTMModel
        elif mt in ("transformer_baseline", "transformerbaseline"):
            deep_model_cls = TransformerBaseline
        elif mt == "etsformer":
            deep_model_cls = ETSformer
        else:  # itransformer / i-transformer
            deep_model_cls = iTransformer

        model = deep_model_cls(
            task=task,
            hparams=hparams,
            decision_scheme=decision_scheme,
        )
    else:
        available = ["xgb", "lstm", "transformer_baseline", "etsformer", "itransformer"] + list(MODEL_POOL.keys())
        raise ValueError(f"Unknown model_type={model_type!r}. Available: {available}")

    return model


class Ensemble2Model:
    """
    简单 2 模型加权 Ensemble，接口尽量与现有 ModelAdapter 保持一致：
    - fit(X, y)
    - predict(X)
    - to_signal(preds)
    """

    def __init__(
        self,
        *,
        task: str,
        base1_type: str,
        base2_type: str,
        params: dict,
        weight1: float,
        weight2: float,
        decision_scheme,
    ):
        self.task = task
        self.base1_type = base1_type
        self.base2_type = base2_type
        self.params = params or {}
        self.w1 = float(weight1)
        self.w2 = float(weight2)
        self.decision_scheme = decision_scheme

        self._m1: Optional["ModelAdapter"] = None
        self._m2: Optional["ModelAdapter"] = None

    def fit(self, X, y):
        # 使用同一套 params，但每个模型只吃自己允许的 hparams
        self._m1 = _build_base_model(
            task=self.task,
            model_type=self.base1_type,
            params=self.params,
            decision_scheme=self.decision_scheme,
        )
        self._m2 = _build_base_model(
            task=self.task,
            model_type=self.base2_type,
            params=self.params,
            decision_scheme=self.decision_scheme,
        )
        self._m1.fit(X, y)
        self._m2.fit(X, y)
        return self

    def predict(self, X):
        if self._m1 is None or self._m2 is None:
            raise RuntimeError("Ensemble2Model not fitted")

        p1 = np.asarray(self._m1.predict(X), dtype=float).ravel()
        p2 = np.asarray(self._m2.predict(X), dtype=float).ravel()

        w1 = self.w1
        w2 = self.w2
        denom = w1 + w2 if (w1 + w2) != 0 else 1.0

        preds = (w1 * p1 + w2 * p2) / denom
        return preds

    def to_signal(self, preds: np.ndarray) -> np.ndarray:
        # 优先用统一的 decision_scheme
        if hasattr(self.decision_scheme, "to_signal"):
            return self.decision_scheme.to_signal(preds)

        preds = np.asarray(preds, dtype=float).ravel()
        mu = preds.mean()
        std = preds.std() or 1.0
        z = (preds - mu) / std
        return np.tanh(z)


# ============================================================================
# StackingModel: 多模型堆叠 (Meta-Learner)
# ============================================================================

class StackingModel:
    """
    多模型 Stacking（机构级模型组合器）：
    - base_models: 多个基础模型（XGB / Lasso / Ridge / ElasticNet / KNN / Logistic / LinearSVM ...）
    - meta_model: 顶层聚合模型（通常是 Lasso / Ridge / Logistic / XGB）

    训练流程：
      1. 对每个 base model 做 K 折 OOF (Out-of-Fold) 预测，得到 Z_train (n_samples, n_base_models)
      2. 用 Z_train 训练 meta model（学习"在什么特征空间下应该更信任哪个模型"）
      3. 每个 base model 再在全量 X_train 上训练一份最终版本

    推理流程：
      - 每个 base model 对 X 预测 → 拼成 Z (n_samples, n_base_models)
      - meta model 对 Z 预测 → 最终预测

    优势：
      - 多模型视角：线性 vs 非线性、稀疏 vs 树
      - Meta-model 学会模型选择：自动在不同样本上分配权重
      - 完全 JSON 驱动：AI 大脑通过配置控制 base_models / meta_model / k-fold
    """

    def __init__(
        self,
        *,
        task: str,
        base_model_types: List[str],
        meta_model_type: str,
        params: dict,
        n_folds: int,
        decision_scheme,
        logger=None,
    ):
        self.task = task
        self.base_model_types = [m.lower() for m in base_model_types]
        self.meta_model_type = (meta_model_type or "").lower()
        self.params = params or {}
        self.n_folds = max(2, int(n_folds))
        self.decision_scheme = decision_scheme
        self.logger = logger

        self._base_models: List["ModelAdapter"] = []
        self._meta_model: Optional["ModelAdapter"] = None

    def fit(self, X, y):
        """
        训练 Stacking 模型：
        1. K-fold OOF 生成 meta features
        2. 训练 meta model
        3. 全量训练所有 base models
        """
        import pandas as pd

        # 保留 DataFrame 格式（如果是），因为某些模型（如 XGB）需要 columns 信息
        X_is_df = isinstance(X, pd.DataFrame)
        if X_is_df:
            X_df = X.copy()
            feature_cols = X_df.columns.tolist()
            X_values = X_df.values.astype(float)
        else:
            X_values = np.asarray(X, dtype=float)
            feature_cols = [f"f{i}" for i in range(X_values.shape[1])]
            X_df = pd.DataFrame(X_values, columns=feature_cols)

        # 保存 feature columns 供 predict() 使用
        self._feature_cols = feature_cols

        y = np.asarray(y, dtype=float)

        n_samples = X_df.shape[0]
        n_base = len(self.base_model_types)
        if n_base == 0:
            raise ValueError("StackingModel: empty base_model_types")

        print(
            f"[Stacking] Training with {n_base} base models "
            f"({', '.join(self.base_model_types)}), "
            f"meta={self.meta_model_type}, n_folds={self.n_folds}"
        )

        # 简单 K 折索引（手写以减少依赖）
        indices = np.arange(n_samples, dtype=int)
        np.random.seed(42)  # 固定随机种子保证可复现
        np.random.shuffle(indices)

        fold_sizes = np.full(self.n_folds, n_samples // self.n_folds, dtype=int)
        fold_sizes[: n_samples % self.n_folds] += 1

        folds = []
        start = 0
        for fs in fold_sizes:
            stop = start + fs
            folds.append(indices[start:stop])
            start = stop

        # OOF 矩阵：每列是一个 base model 的 OOF 预测
        Z_train = np.zeros((n_samples, n_base), dtype=float)

        # ========== Phase 1: 生成 OOF 预测 ==========
        for j, mt in enumerate(self.base_model_types):
            print(f"[Stacking] Base model {j+1}/{n_base}: {mt} - generating OOF predictions")

            oof_pred = np.zeros(n_samples, dtype=float)

            for k, val_idx in enumerate(folds):
                # 训练集 = 除了当前 fold 的所有样本
                train_idx = np.setdiff1d(indices, val_idx, assume_unique=False)

                # 使用 DataFrame（保留 columns 信息）
                X_tr = X_df.iloc[train_idx]
                y_tr = y[train_idx]
                X_val = X_df.iloc[val_idx]

                # 构建并训练 base model
                m = _build_base_model(
                    task=self.task,
                    model_type=mt,
                    params=self.params,
                    decision_scheme=self.decision_scheme,
                )
                m.fit(X_tr, y_tr)

                # OOF 预测
                p_val = np.asarray(m.predict(X_val), dtype=float).ravel()
                oof_pred[val_idx] = p_val

                print(
                    f"[Stacking]   Fold {k+1}/{self.n_folds}: "
                    f"train={len(train_idx)}, val={len(val_idx)}"
                )

            Z_train[:, j] = oof_pred
            print(f"[Stacking] Base model {mt}: OOF complete, pred_range=[{oof_pred.min():.4f}, {oof_pred.max():.4f}]")

        # ========== Phase 2: 训练 Meta Model ==========
        print(f"[Stacking] Training meta model: {self.meta_model_type}")

        # Meta model 的输入是 OOF 预测（numpy array），转为 DataFrame
        import pandas as pd
        Z_train_df = pd.DataFrame(Z_train, columns=[f"base_{i}" for i in range(n_base)])

        self._meta_model = _build_base_model(
            task=self.task,
            model_type=self.meta_model_type,
            params=self.params,
            decision_scheme=self.decision_scheme,
        )
        self._meta_model.fit(Z_train_df, y)
        print(f"[Stacking] Meta model training complete")

        # ========== Phase 3: 全量训练 Base Models ==========
        print(f"[Stacking] Training final base models on full dataset")
        self._base_models = []
        for j, mt in enumerate(self.base_model_types):
            print(f"[Stacking] Final training {j+1}/{n_base}: {mt}")
            m_full = _build_base_model(
                task=self.task,
                model_type=mt,
                params=self.params,
                decision_scheme=self.decision_scheme,
            )
            # 使用完整的 X_df
            m_full.fit(X_df, y)
            self._base_models.append(m_full)

        print(f"[Stacking] Training complete: {n_base} base + 1 meta model")
        return self

    def predict(self, X):
        """
        Stacking 预测：
        1. 每个 base model 预测
        2. 拼接成 meta features
        3. Meta model 最终预测
        """
        if self._meta_model is None or not self._base_models:
            raise RuntimeError("StackingModel not fitted")

        import pandas as pd

        # 确保 X 是 DataFrame
        if isinstance(X, pd.DataFrame):
            X_df = X
        else:
            X_values = np.asarray(X, dtype=float)
            # 尝试使用训练时的 feature names（如果有）
            if hasattr(self, '_feature_cols'):
                X_df = pd.DataFrame(X_values, columns=self._feature_cols)
            else:
                X_df = pd.DataFrame(X_values, columns=[f"f{i}" for i in range(X_values.shape[1])])

        n_samples = X_df.shape[0]
        n_base = len(self._base_models)

        # 构建 meta features
        Z = np.zeros((n_samples, n_base), dtype=float)

        for j, m in enumerate(self._base_models):
            p = np.asarray(m.predict(X_df), dtype=float).ravel()
            Z[:, j] = p

        # Meta model 预测 (Z 转为 DataFrame)
        Z_df = pd.DataFrame(Z, columns=[f"base_{i}" for i in range(n_base)])
        preds = self._meta_model.predict(Z_df)
        return preds

    def to_signal(self, preds: np.ndarray) -> np.ndarray:
        """
        转换为交易信号（标准化到 [-1, 1]）
        """
        if hasattr(self.decision_scheme, "to_signal"):
            return self.decision_scheme.to_signal(preds)

        # Fallback: tanh 标准化
        preds = np.asarray(preds, dtype=float).ravel()
        mu = preds.mean()
        std = preds.std() or 1.0
        z = (preds - mu) / std
        return np.tanh(z)


def apply_feature_selection(
    X_train,
    y_train,
    X_valid,
    feature_names: List[str],
    fs_conf: dict,
    logger=None,
) -> Tuple[np.ndarray, np.ndarray, List[str], dict]:
    """
    在单个 fold 上做特征选择：
    - method: none / lasso / xgb_importance
    - max_features: 限制最多保留多少个特征
    返回:
      X_train_fs, X_valid_fs, new_feature_names, meta
    """
    fs_conf = fs_conf or {}
    method = (fs_conf.get("method") or "none").lower()
    enabled = fs_conf.get("enabled", False)
    max_features = int(fs_conf.get("max_features", 0))

    if (not enabled) or method in ("none", "", "off"):
        return np.asarray(X_train), np.asarray(X_valid), feature_names, {"selected_idx": None}

    X_train = np.asarray(X_train, dtype=float)
    X_valid = np.asarray(X_valid, dtype=float)
    n_cols = X_train.shape[1]
    idx_all = np.arange(n_cols, dtype=int)

    if n_cols == 0:
        return X_train, X_valid, feature_names, {"selected_idx": None}

    selected_idx = idx_all

    # ========== Lasso ==========
    if method == "lasso":
        if not HAS_SKLASSO:
            raise RuntimeError("fs_method=lasso but scikit-learn not installed")
        alpha = float(fs_conf.get("alpha", 0.001))
        max_iter = int(fs_conf.get("max_iter", 2000))

        model = SkLasso(alpha=alpha, max_iter=max_iter)
        model.fit(X_train, y_train)
        coefs = np.abs(model.coef_)
        order = np.argsort(-coefs)  # 从大到小

        if max_features > 0:
            order = order[:max_features]

        # 过滤掉系数为 0 的特征
        order = [i for i in order if coefs[i] > 0.0]
        if order:
            selected_idx = np.array(sorted(order), dtype=int)
        else:
            selected_idx = idx_all  # 兜底：全保留

        if logger:
            logger.info(
                f"[FS] lasso selected {len(selected_idx)}/{n_cols} features "
                f"(alpha={alpha}, max_features={max_features})"
            )

    # ========== XGB importance ==========
    elif method == "xgb_importance":
        if not HAS_XGB_FS:
            raise RuntimeError("fs_method=xgb_importance but xgboost not installed")

        task = (fs_conf.get("task") or "reg").lower()
        max_depth = int(fs_conf.get("max_depth", 4))
        n_estimators = int(fs_conf.get("n_estimators", 80))
        learning_rate = float(fs_conf.get("learning_rate", 0.1))

        if task == "clf":
            booster = xgb.XGBClassifier(
                max_depth=max_depth,
                n_estimators=n_estimators,
                learning_rate=learning_rate,
                subsample=0.8,
                colsample_bytree=0.8,
                n_jobs=-1,
                tree_method="hist",
            )
        else:
            booster = xgb.XGBRegressor(
                max_depth=max_depth,
                n_estimators=n_estimators,
                learning_rate=learning_rate,
                subsample=0.8,
                colsample_bytree=0.8,
                n_jobs=-1,
                tree_method="hist",
            )

        booster.fit(X_train, y_train)
        importances = booster.feature_importances_

        order = np.argsort(-importances)
        if max_features > 0:
            order = order[:max_features]

        order = [i for i in order if importances[i] > 0.0]
        if order:
            selected_idx = np.array(sorted(order), dtype=int)
        else:
            selected_idx = idx_all

        if logger:
            logger.info(
                f"[FS] xgb_importance selected {len(selected_idx)}/{n_cols} "
                f"features (max_features={max_features})"
            )

    else:
        # 未知 method，直接原样返回
        return X_train, X_valid, feature_names, {"selected_idx": None}

    X_train_fs = X_train[:, selected_idx]
    X_valid_fs = X_valid[:, selected_idx]
    new_feature_names = [feature_names[i] for i in selected_idx]

    return X_train_fs, X_valid_fs, new_feature_names, {"selected_idx": selected_idx.tolist()}


# ------------------ Core Runner ------------------

class WalkForwardHPO:
    def __init__(self, cfg: Dict[str, Any]) -> None:
        """
        cfg expects keys:
          symbols, timeframe, features_table,
          clickhouse (optional): {host, port, user, password, database}
          strategy_path, strategy_base_extras (dict),
          broker: {initial_cash, commission_per_share, commission_min, slippage_bps, fill_ref},
          grid: {param: [values...]},
          folds: [Fold-like dicts], OR
          fold_generator: {start, end, is_days, oos_days, gap_days, step_days}
          objective: {metric, maximize, min_trades, max_mdd, threshold, formula}
          parallel: {workers: int}
        """
        # Early breadcrumb for diagnosing silent crashes
        try:
            import os as _os
            import psutil as _psutil  # type: ignore
            _proc = _psutil.Process(_os.getpid())
            _rss = _proc.memory_info().rss / 1024 / 1024
            self._init_rss_mib = float(_rss)
            print(
                f"[WalkForwardHPO:init] pid={_os.getpid()} rss={_rss:.1f} MB start",
                flush=True,
            )
        except Exception:
            pass

        self.cfg = cfg
        self.symbols = cfg["symbols"]
        self.timeframe = cfg.get("timeframe", "1m")
        self.features_table = cfg.get("features_table")
        self.clickhouse = cfg.get("clickhouse", {})

        # Strategy path is now REQUIRED (no hardcoded default)
        strategy_path = cfg.get("strategy_path")
        if not strategy_path:
            available = StrategyRegistry.list_strategies()
            raise ValueError(
                "配置缺少 'strategy_path' 字段。\n"
                f"可用策略 ({len(available)} 个):\n" +
                "\n".join(f"  - {s}" for s in available) +
                "\n\n请在配置文件的 'hpo' 部分添加 'strategy_path' 字段。"
            )
        self.strategy_path = strategy_path
        self.strategy_base_extras = cfg.get("strategy_base_extras", {}) or {}

        try:
            print(
                f"[WalkForwardHPO:init] strategy_path={self.strategy_path} "
                f"extras_keys={sorted(list((self.strategy_base_extras or {}).keys()))}",
                flush=True,
            )
        except Exception:
            pass

        # Initialize strategy adapter
        try:
            print("[WalkForwardHPO:init] acquiring strategy adapter...", flush=True)
            self.adapter = StrategyRegistry.get_adapter(self.strategy_path)
            print(f"[WalkForwardHPO] 已加载策略适配器: {self.adapter.__class__.__name__}", flush=True)
            print(f"[WalkForwardHPO] 策略: {self.strategy_path}", flush=True)
            print(f"[WalkForwardHPO] 必需列: {self.adapter.get_required_columns()}", flush=True)
            print(f"[WalkForwardHPO] 支持 ML: {self.adapter.supports_ml_signals()}", flush=True)
        except Exception:
            # Re-raise after logging
            import traceback as _tb
            print("[WalkForwardHPO:init] adapter acquisition failed", flush=True)
            _tb.print_exc()
            raise

        dataset_df = cfg.get("dataset_df")
        if dataset_df is not None:
            df = pd.DataFrame(dataset_df)
            if "ts" not in df.columns or "symbol" not in df.columns:
                raise ValueError("dataset_df must contain 'ts' and 'symbol' columns")
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            self.dataset_df = df.sort_values(["ts", "symbol"]).reset_index(drop=True)
        else:
            self.dataset_df = None
        try:
            print(
                f"[WalkForwardHPO:init] dataset_df={'yes' if self.dataset_df is not None else 'no'}",
                flush=True,
            )
        except Exception:
            pass

        b = cfg.get("broker", {}) or {}
        self.broker = {
            "initial_cash": b.get("initial_cash", 100_000.0),
            "commission_per_share": b.get("commission_per_share", 0.005),
            "commission_min": b.get("commission_min", 1.0),
            "slippage_bps": b.get("slippage_bps", 1.0),
            "fill_ref": b.get("fill_ref", "open"),
        }

        self.grid = cfg["grid"]
        self.out_dir = cfg.get("out_dir", ".")  # For resume capability

        # >>> FT-INTEGRATION BEGIN <<<
        # Load feature_tools.json for Featuretools + XGB products + filters
        ft_config_path = cfg.get("feature_tools_path", "feature_tools.json")
        try:
            import os
            if os.path.exists(ft_config_path):
                with open(ft_config_path) as f:
                    self.feature_tools_config = json.load(f)
                print(f"[WalkForwardHPO] Loaded feature_tools.json from {ft_config_path}")
            else:
                self.feature_tools_config = {}
                print(f"[WalkForwardHPO] WARNING: {ft_config_path} not found, using empty config")
        except Exception as e:
            print(f"[WalkForwardHPO] Failed to load feature_tools.json: {e}")
            self.feature_tools_config = {}
        # >>> FT-INTEGRATION END <<<

        if "folds" in cfg and cfg["folds"]:
            self.folds = [Fold(**f) for f in cfg["folds"]]
        elif "fold_generator" in cfg:
            gen = cfg["fold_generator"]
            folds = make_folds(
                gen["start"], gen["end"],
                is_days=float(gen["is_days"]),
                oos_days=float(gen["oos_days"]),
                gap_days=float(gen.get("gap_days", 0)),
                step_days=float(gen.get("step_days", gen["oos_days"])),
                embargo_days=float(gen.get("embargo_days", 0))
            )
            self.folds = [Fold(**f) for f in folds]
        else:
            raise ValueError("Must provide 'folds' or 'fold_generator'.")
        try:
            print(f"[WalkForwardHPO:init] folds={len(self.folds)} ready", flush=True)
        except Exception:
            pass

        obj = cfg.get("objective", {}) or {}
        self.objective = Objective(**obj)

        par = cfg.get("parallel", {}) or {}
        raw_workers = int(par.get("workers", 1))

        # ✅ OOM hotfix: FORCE single worker to prevent multi-process memory explosion
        # Read hard cap from config.yml policies.hpo_safe_baseline.workers (default: 1)
        try:
            from libs.config import config as _cfg
            safe_baseline = _cfg.get("policies", {}).get("hpo_safe_baseline", {})
            max_workers = safe_baseline.get("workers", 1)
        except Exception:
            max_workers = 1

        self.workers = min(raw_workers, max_workers)
        if self.workers != raw_workers:
            print(f"[OOM Guard] workers clamped: {raw_workers} → {self.workers} (max_workers={max_workers})")

        # Build base bt_cfg once (process-safe dict)
        # Build base BacktestConfig dict; ensure strategy_params has nested 'extras'
        self._bt_cfg_dict = asdict(BacktestConfig(
            symbols=self.symbols,
            start="1970-01-01 00:00:00",
            end="1970-01-02 00:00:00",
            timeframe=self.timeframe,
            features_table=self.features_table,
            clickhouse=self.clickhouse,
            initial_cash=self.broker["initial_cash"],
            commission_per_share=self.broker["commission_per_share"],
            commission_min=self.broker["commission_min"],
            slippage_bps=self.broker["slippage_bps"],
            fill_ref=self.broker["fill_ref"],
            strategy_path=self.strategy_path,
            strategy_params={"extras": self.strategy_base_extras}
        ))
        try:
            print("[WalkForwardHPO:init] backtest config prepared", flush=True)
        except Exception:
            pass

        try:
            import os as _os
            import psutil as _psutil  # type: ignore
            _proc = _psutil.Process(_os.getpid())
            _rss2 = _proc.memory_info().rss / 1024 / 1024
            print(
                f"[WalkForwardHPO:init] completed rss={_rss2:.1f} MB (+{_rss2 - float(getattr(self, '_init_rss_mib', _rss2)):.1f} MB)",
                flush=True,
            )
        except Exception:
            pass

    # ----- objective scoring -----
    def _score(self, row: Dict[str, Any]) -> Optional[float]:
        # constraints
        if self.objective.min_trades and row.get("trades", 0) < self.objective.min_trades:
            return None
        if self.objective.max_mdd is not None and row.get("max_drawdown", 0.0) > self.objective.max_mdd:
            return None

        # formula first
        if self.objective.formula:
            ctx = {
                "sharpe": float(row.get("sharpe", 0.0)),
                "total_return": float(row.get("total_return", 0.0)),
                "max_drawdown": float(row.get("max_drawdown", 0.0)),
                "win_rate": float(row.get("win_rate", 0.0)),
                "profit_factor": float(row.get("profit_factor", 0.0)),
                "trades": float(row.get("trades", 0.0)),
                "sortino": float(row.get("sortino", 0.0)),
                "omega": float(row.get("omega", 0.0)),
                "calmar": float(row.get("calmar", 0.0)),
                "psr": float(row.get("psr", 0.0)),
                "dsr": float(row.get("dsr", 0.0)),
                "pbo": float(row.get("pbo", 0.0)),
            }
            val = _safe_eval_formula(self.objective.formula, ctx)
            return val if self.objective.maximize else -val

        # otherwise use chosen metric
        key = self.objective.metric
        val = float(row.get(key, 0.0))
        return val if self.objective.maximize else -val

    # ----- dataset helpers -----
    def _load_dataset_for_fold(self, start: str, end: str) -> pd.DataFrame:
        """
        Load dataset for a fold using strategy adapter.

        If dataset_df is provided in config, slice it.
        Otherwise, use adapter.load_data() to fetch from ClickHouse.
        """
        if self.dataset_df is not None:
            # Use pre-loaded dataset
            start_dt = pd.to_datetime(_dt(start), utc=True)
            end_dt = pd.to_datetime(_dt(end), utc=True)
            mask = (self.dataset_df["ts"] >= start_dt) & (self.dataset_df["ts"] < end_dt)
            if not mask.any():
                return self.dataset_df.iloc[0:0]
            return self.dataset_df.loc[mask]

        # Use adapter to load data from ClickHouse
        print(f"[WalkForwardHPO] 使用适配器加载数据: {start} ~ {end}")
        df = self.adapter.load_data(
            symbols=self.symbols,
            start=start,
            end=end,
            clickhouse_config=self.clickhouse,
            features_table=self.features_table,
            timeframe=self.timeframe
        )

        # Validate data
        self.adapter.validate_data(df)
        print(f"[WalkForwardHPO] 数据加载成功: {len(df)} 行, {len(df.columns)} 列")

        return df

    def _slice_dataset(self, start: str, end: str) -> Optional[pd.DataFrame]:
        """Legacy method for backward compatibility. Delegates to _load_dataset_for_fold()."""
        return self._load_dataset_for_fold(start, end)

    def _prepare_model_data(
        self,
        fold: Fold,
        params: Dict[str, Any],
        df_is: Optional[pd.DataFrame] = None,
    ) -> Tuple[Optional[pd.DataFrame], Optional[pd.DataFrame], Optional[Dict[str, Any]]]:
        # BUGFIX: Don't return early if dataset_df is None - adapter mode loads data via _slice_dataset
        # if self.dataset_df is None:
        #     return None, None, None

        model_type = params.get("model_type")
        if not model_type or str(model_type).lower() in ("none", "baseline"):
            return None, None, None

        df_is = df_is if df_is is not None else self._slice_dataset(fold.is_start, fold.is_end)
        if df_is is None or df_is.empty:
            return None, None, None

        target_col = params.get("target_col") or self.cfg.get("model_target_col", "y")
        if target_col not in df_is.columns:
            # 优先使用 label_cfg (如果存在)
            label_cfg = self.cfg.get("label_cfg")
            if label_cfg:
                # 使用 Label Engine + Sanity Checks (with backward compatibility)
                from labels.label_integration import (
                    build_labels_with_sanity,
                    summarize_label_sanity_for_logging,
                )
                df_is = df_is.copy()
                # Ensure sorting by ts then symbol
                if not pd.api.types.is_datetime64_any_dtype(df_is["ts"]):
                    df_is["ts"] = pd.to_datetime(df_is["ts"], utc=True, errors="coerce")
                df_is = df_is.sort_values(["ts", "symbol"]).reset_index(drop=True)

                # Build labels with quality validation
                df_is, label_sanity = build_labels_with_sanity(
                    df_is,
                    label_cfg,
                    target_col,
                    enable_sanity=True,
                )

                # Log label quality
                if label_sanity:
                    summary = summarize_label_sanity_for_logging(label_sanity)
                    print(f"[WalkForwardHPO] Label Quality Check:\n{summary}", flush=True)

                    # Warning if quality issues detected
                    if not label_sanity.is_ok:
                        print(f"⚠️  [WalkForwardHPO] Label quality issues detected:", flush=True)
                        for issue_type, details in (label_sanity.issues or {}).items():
                            print(f"  - {issue_type}: {details}", flush=True)
                        # Note: We continue training despite issues (configurable in future)
            else:
                # Fallback: derive target from future returns on 'close'
                # horizon in bars; default 5
                horizon = int(self.cfg.get("model_horizon_bars", 5) or 5)
                if "close" not in df_is.columns:
                    raise ValueError(f"Target column '{target_col}' not found and no 'close' to derive synthetic labels")
                df_is = df_is.copy()
                # Ensure sorting by ts then symbol
                if not pd.api.types.is_datetime64_any_dtype(df_is["ts"]):
                    df_is["ts"] = pd.to_datetime(df_is["ts"], utc=True, errors="coerce")
                df_is = df_is.sort_values(["ts", "symbol"]).reset_index(drop=True)
                # Future return per symbol
                def _future_ret(close_series):
                    c = close_series.astype(float)
                    fr = (c.shift(-horizon) / c) - 1.0
                    return fr
                df_is[target_col] = df_is.groupby("symbol", dropna=False, sort=False)["close"].transform(_future_ret)
                # For classification task, map to {0,1}
                if (params.get("task") or self.cfg.get("model_task", "reg")).lower() == "clf":
                    df_is[target_col] = (df_is[target_col] > 0.0).astype(float)

        feature_cols = params.get("feature_subset") or []
        if not feature_cols:
            exclude = {target_col, "ts", "symbol", "feature_cutoff_ts"}
            feature_cols = [c for c in df_is.columns if c not in exclude]
        feature_cols = list(dict.fromkeys(feature_cols))

        scheme = DecisionScheme.from_dict(params.get("decision"))
        task = params.get("task") or self.cfg.get("model_task", "reg")
        raw_hparams = params.get("model_params") or {}

        # ------------------------------
        # Helper: Split hyperparameters by model type
        # ------------------------------
        def _split_hparams(mt_lower: str, raw_hparams: Dict[str, Any]) -> Dict[str, Any]:
            """Filter hyperparameters by model type and sanitize obvious incompatibilities.

            - Ensures we only pass keys relevant for the chosen model family.
            - For XGBoost, clamps max_depth to a valid non-negative value to avoid
              crashes when raw_hparams came from a different model family (e.g. LGBM).
            """
            xgb_keys = {
                "objective", "eval_metric", "tree_method", "verbosity",
                "eta", "learning_rate", "max_depth", "min_child_weight",
                "subsample", "colsample_bytree", "gamma", "lambda", "alpha",
                "reg_lambda", "reg_alpha",
                "n_estimators", "num_boost_round", "early_stopping_rounds",
            }
            seq_keys = {
                "lookback", "hidden", "hidden_dim", "num_layers", "dropout",
                "pooling", "bidirectional", "use_attention", "layer_norm",
                "batch_size", "epochs", "patience", "lr", "weight_decay",
                "device", "d_model", "nhead", "dim_feedforward", "alpha_init",
            }
            linear_keys = {
                "alpha", "l1_ratio", "tol", "max_iter", "C", "penalty", "solver",
            }
            knn_keys = {"n_neighbors", "weights"}
            lgbm_keys = {
                "learning_rate", "num_leaves", "max_depth", "min_child_samples",
                "subsample", "colsample_bytree", "n_estimators",
                "objective", "metric", "n_jobs", "verbose",
            }
            rulefit_keys = {"tree_size", "sample_fract", "max_rules", "memory_par"}

            if mt_lower in {"xgb", "xgboost"}:
                hp = {k: v for k, v in raw_hparams.items() if k in xgb_keys}
                # Safety: some legacy configs reuse model_params across families (e.g. LGBM),
                # which may carry max_depth = -1 (valid for LGBM, invalid for XGBoost).
                md = hp.get("max_depth")
                try:
                    if isinstance(md, (int, float)) and md < 0:
                        # XGBoost expects max_depth >= 0; use a sane default depth.
                        hp["max_depth"] = 6
                except Exception:
                    pass
                return hp
            elif mt_lower in {"lstm", "transformer", "transformer_baseline", "transformerbaseline",
                              "etsformer", "itransformer", "i-transformer"}:
                return {k: v for k, v in raw_hparams.items() if k in seq_keys}
            elif mt_lower in {"lasso", "ridge", "elasticnet", "elastic_net", "logreg",
                              "logistic", "svm_linear"}:
                return {k: v for k, v in raw_hparams.items() if k in linear_keys}
            elif mt_lower in {"knn"}:
                return {k: v for k, v in raw_hparams.items() if k in knn_keys}
            elif mt_lower in {"lgbm", "lightgbm"}:
                return {k: v for k, v in raw_hparams.items() if k in lgbm_keys}
            elif mt_lower in {"rulefit", "rule_fit"}:
                return {k: v for k, v in raw_hparams.items() if k in rulefit_keys}
            else:
                # Fallback: use all params (for registry/pool models)
                return dict(raw_hparams)

        # ------------------------------
        # Helper: Build single model by type
        # ------------------------------
        def _build_single_model(mt_lower: str, task_: str, hparams_: Dict[str, Any],
                               scheme_: DecisionScheme) -> ModelAdapter:
            """
            统一的单模型构造函数，支持:
            - XGB (tree-based)
            - LSTM/Transformer/ETSformer/iTransformer (sequence models)
            - Lasso/Ridge/ElasticNet/Logistic/SVM/KNN/LightGBM/RuleFit (linear/sklearn models via MODEL_POOL)
            """
            # Tree-based models
            if mt_lower in {"xgb", "xgboost"}:
                return XGBModel(task=task_, hparams=hparams_, decision_scheme=scheme_)

            # Sequence models
            elif mt_lower == "lstm":
                return LSTMModel(task=task_, hparams=hparams_, decision_scheme=scheme_)
            elif mt_lower in {"transformer", "transformer_baseline", "transformerbaseline"}:
                return TransformerBaseline(task=task_, hparams=hparams_, decision_scheme=scheme_)
            elif mt_lower == "etsformer":
                return ETSformer(task=task_, hparams=hparams_, decision_scheme=scheme_)
            elif mt_lower in {"itransformer", "i-transformer"}:
                return iTransformer(task=task_, hparams=hparams_, decision_scheme=scheme_)

            # Linear/sklearn models (via MODEL_POOL)
            elif mt_lower in MODEL_POOL:
                ModelCls = get_linear_model_class(mt_lower)
                return ModelCls(task=task_, hparams=hparams_, decision_scheme=scheme_)

            else:
                available_models = ["xgb", "lstm", "transformer", "etsformer", "itransformer"] + list(MODEL_POOL.keys())
                raise ValueError(
                    f"Unsupported single model type: {mt_lower}\n"
                    f"Available models: {available_models}"
                )

        # ------------------------------
        # 4. Model Selection + Training + Prediction (支持 stacking / ensemble2 / ensemble / 单模型)
        # ------------------------------
        mt = str(model_type).lower()

        # >>> STACKING BRANCH (Multi-model meta-learner) <<<
        if mt == "stacking":
            # 从配置和 HPO 参数中读取 stacking 配置
            stack_conf = dict(self.cfg.get("stacking", {}))

            # 基础模型列表
            base_models_cfg = stack_conf.get("base_models") or []
            # Meta 模型类型
            meta_model_cfg = stack_conf.get("meta_model") or "ridge"
            # K-fold 数量
            n_folds_cfg = int(stack_conf.get("n_folds", 3))

            # 允许 HPO 在 params 中覆盖配置
            if "stack_base_models" in params:
                base_models_param = params["stack_base_models"]
                if isinstance(base_models_param, str):
                    # 允许逗号分隔字符串: "xgb,ridge,lasso"
                    base_models_cfg = [s.strip() for s in base_models_param.split(",") if s.strip()]
                elif isinstance(base_models_param, (list, tuple)):
                    base_models_cfg = list(base_models_param)

            if "stack_meta_model" in params:
                meta_model_cfg = params["stack_meta_model"]

            if "stack_kfold" in params:
                n_folds_cfg = int(params["stack_kfold"])

            if not base_models_cfg:
                raise ValueError("model_type=stacking but no base_models specified in config or params")

            model = StackingModel(
                task=task,
                base_model_types=base_models_cfg,
                meta_model_type=meta_model_cfg,
                params=raw_hparams,
                n_folds=n_folds_cfg,
                decision_scheme=scheme,
                logger=None,
            )
            print(
                f"[WalkForwardHPO] Using Stacking: "
                f"base=[{', '.join(base_models_cfg)}], "
                f"meta={meta_model_cfg}, "
                f"k-fold={n_folds_cfg}"
            )

        # >>> ENSEMBLE2 BRANCH (2-model weighted ensemble) <<<
        elif mt == "ensemble2":
            base1_type = raw_hparams.get("base1_model_type", "xgb")
            base2_type = raw_hparams.get("base2_model_type", "lasso")
            w1 = float(raw_hparams.get("w1", 0.5))
            w2 = float(raw_hparams.get("w2", 0.5))

            model = Ensemble2Model(
                task=task,
                base1_type=base1_type,
                base2_type=base2_type,
                params=raw_hparams,
                weight1=w1,
                weight2=w2,
                decision_scheme=scheme,
            )
            print(f"[WalkForwardHPO] Using Ensemble2: {base1_type} (w={w1:.2f}) + {base2_type} (w={w2:.2f})")

        # >>> ENSEMBLE BRANCH (N-model weighted ensemble) <<<
        elif mt == "ensemble":
            # 加权平均 Ensemble: 支持任意组合 (XGB + Lasso + Ridge + LightGBM + ...)
            ensemble_spec = params.get("ensemble")
            if not ensemble_spec:
                # Fallback to global ensemble_default in hpo section
                ensemble_spec = self.cfg.get("hpo", {}).get("ensemble_default") or self.cfg.get("ensemble_default")
            if not ensemble_spec or not ensemble_spec.get("models"):
                raise ValueError("ensemble model_type 需要 'ensemble' 配置，包含 'models' 列表")

            sub_models = []
            sub_weights = []
            print(f"[WalkForwardHPO] Building Ensemble with {len(ensemble_spec['models'])} sub-models...")

            for i, sub_spec in enumerate(ensemble_spec["models"]):
                sub_type = sub_spec.get("type", "xgb")
                sub_weight = sub_spec.get("weight", 1.0)
                sub_task = sub_spec.get("task", task)
                sub_raw_hparams = sub_spec.get("hparams", {})

                # Filter hyperparameters for this sub-model
                sub_hparams = _split_hparams(sub_type, sub_raw_hparams)

                # Build sub-model using unified helper
                sub_model = _build_single_model(sub_type, sub_task, sub_hparams, scheme)
                sub_models.append(sub_model)
                sub_weights.append(sub_weight)

                print(f"  Sub-model {i+1}/{len(ensemble_spec['models'])}: {sub_type} (weight={sub_weight:.3f})")

            # Create WeightedEnsembleModel
            model = WeightedEnsembleModel(
                models=sub_models,
                weights=sub_weights,
                task=task,
                name="ensemble",
                decision_scheme=scheme
            )
            print(f"[WalkForwardHPO] WeightedEnsembleModel created with {len(sub_models)} sub-models")

        # >>> SINGLE MODEL BRANCH <<<
        else:
            # 单模型分支：使用统一的 _build_single_model()
            hparams = _split_hparams(mt, raw_hparams)
            model = _build_single_model(mt, task, hparams, scheme)
            print(f"[WalkForwardHPO] Using single model: {mt}")

        df_is_sorted = df_is.sort_values("ts").reset_index(drop=True)

        # ========== PHASE 1: Feature Engineering ==========

        # >>> FT-INTEGRATION BEGIN <<<
        # 🔥 Featuretools DFS (Deep Feature Synthesis) - AI-controlled automatic feature engineering
        # Pipeline modes: "xgb_only", "dfs_only", "xgb_then_dfs", "dfs_then_xgb"
        ft_config = self.feature_tools_config.get("featuretools", {})
        pipeline_mode = self.feature_tools_config.get("pipeline", {}).get("mode", "xgb_only")
        use_ft = ft_config.get("enabled", False) and HAS_FEATURETOOLS and pipeline_mode in ("dfs_only", "dfs_then_xgb", "xgb_then_dfs")

        ft_features_is = None
        ft_adapter = None

        if use_ft:
            print(f"[WalkForwardHPO] Featuretools DFS enabled, pipeline_mode={pipeline_mode}")

            # Prepare base columns for DFS (exclude target and metadata)
            base_cols = [c for c in df_is_sorted.columns if c not in {target_col, "ts", "symbol", "feature_cutoff_ts"}]

            try:
                # Initialize adapter
                ft_adapter = FeaturetoolsAdapter(ft_config)

                # Fit on IS data
                print(f"[WalkForwardHPO] DFS fitting on IS data ({len(df_is_sorted)} rows, {len(base_cols)} base features)...")
                ft_adapter.fit_on_is(df_is_sorted, base_cols)
                print(f"[WalkForwardHPO] DFS generated {len(ft_adapter.feature_defs_)} feature definitions")

                # Transform IS
                ft_features_is = ft_adapter.transform_any(df_is_sorted)
                print(f"[WalkForwardHPO] DFS transform IS: {ft_features_is.shape}")

            except Exception as e:
                print(f"[WalkForwardHPO] WARNING: Featuretools DFS failed: {e}")
                import traceback
                traceback.print_exc()
                use_ft = False
                ft_features_is = None
                ft_adapter = None

        # >>> FT-INTEGRATION END <<<

        # 🔥 NEW: Institutional-grade feature pipeline available (v2)
        # To enable: set "use_features_v2": true in config
        # Requires: feature_programs_v2.json with schema 2.0
        # Benefits: zero-copy, robust scaler, correlation pruning, IC tracking
        use_v2 = params.get("use_features_v2") or self.cfg.get("use_features_v2", False)

        if use_v2 and HAS_FEATURES_INJECTOR:
            # ========== V2: 机构级零拷贝特征流程 ==========
            print("[WalkForwardHPO] Using Feature Engineering v2 (zero-copy institutional pipeline)")

            # Load feature programs v2
            programs_path = self.cfg.get("feature_programs_v2_path", "feature_programs_v2.json")
            import json
            with open(programs_path) as f:
                programs_v2 = json.load(f)

            program_id = params.get("feature_program_id") or self.cfg.get("feature_program_id", "alpha_stack_v1")

            # 准备训练掩码（IS数据全部用于训练）
            train_mask = pd.Series([True] * len(df_is_sorted), index=df_is_sorted.index)

            # 零拷贝特征准备：构建→填充→标准化→剪枝
            feature_cols, scaler = prepare_features_for_training(
                df=df_is_sorted,
                programs=programs_v2,
                program_id=program_id,
                fit_mask=train_mask,
                importance_map=None  # 首轮无importance，二轮可用
            )

            print(f"[WalkForwardHPO] V2 pipeline generated {len(feature_cols)} features (post-pruning)")

            # 提取特征和标签
            features_is = df_is_sorted[feature_cols]
            target_is = df_is_sorted[target_col]
            mask = target_is.notna()

            # 特征已标准化，无需再处理
            scaler_meta = {"mode": "v2_robust", "program_id": program_id, "n_features": len(feature_cols)}
            dsl_features = feature_cols
            dsl_meta = {"program_id": program_id, "status": "v2_applied", "pipeline": "institutional"}

        else:
            # ========== V1: 传统DSL特征流程（向后兼容） ==========
            # Apply feature program from JSON configuration
            feature_program_id = params.get("feature_program_id") or self.cfg.get("feature_program_id", "comprehensive_v1")

            # Initialize feature runner (singleton pattern)
            if not hasattr(self, "feature_runner"):
                feature_programs_path = self.cfg.get("feature_programs_path", "feature_programs.json")
                self.feature_runner = FeatureProgramRunner(feature_programs_path)

            if feature_program_id and feature_program_id not in ("", "none"):
                print(f"[WalkForwardHPO] Applying feature program '{feature_program_id}' to IS data")
                df_is_sorted, dsl_features, dsl_meta = self.feature_runner.apply(
                    df_is_sorted,
                    feature_program_id,
                    verbose=False
                )
                print(f"[WalkForwardHPO] DSL created {len(dsl_features)} features: {dsl_features}")
            else:
                dsl_features = []
                dsl_meta = {"program_id": "none", "status": "skipped"}

            # Update feature_cols to include derived features (if feature_subset not manually specified)
            if not params.get("feature_subset"):
                exclude = {target_col, "ts", "symbol", "feature_cutoff_ts"}
                feature_cols = [c for c in df_is_sorted.columns if c not in exclude]

            # Prepare features once; avoid unnecessary deep copies
            features_is = df_is_sorted[feature_cols]
            features_is = features_is.fillna(method="ffill").fillna(method="bfill").fillna(0.0)
            target_is = df_is_sorted[target_col]
            mask = target_is.notna()
            if mask.sum() == 0:
                raise ValueError("No training samples with non-null target")

            # ========== PHASE 1: Feature Standardization ==========
            # Only use IS data to fit scaler (avoid data leakage)
            scale_mode = params.get("model_scale_mode") or self.cfg.get("model_scale_mode", "robust")
            scaler_meta = {"mode": scale_mode}

            if scale_mode in ("zscore", "robust"):
                train_feats = features_is.loc[mask].astype(float)

                if scale_mode == "zscore":
                    mean = train_feats.mean(axis=0)
                    std = train_feats.std(axis=0).replace(0.0, 1.0)
                    features_is = (features_is - mean) / std
                    scaler_meta["mean"] = mean.to_dict()
                    scaler_meta["std"] = std.to_dict()
                else:  # robust
                    median = train_feats.median(axis=0)
                    iqr = (train_feats.quantile(0.75) - train_feats.quantile(0.25)).replace(0.0, 1.0)
                    features_is = (features_is - median) / iqr
                    scaler_meta["median"] = median.to_dict()
                    scaler_meta["iqr"] = iqr.to_dict()

                # Clip extreme values to [-5, 5] to reduce outlier impact
                features_is = features_is.clip(-5.0, 5.0)

        # ========== 统一：模型训练（v1和v2都走这里） ==========

        # ========== PHASE 2: Feature Selection (Lasso / XGB importance) ==========
        # 🔥 NEW: Automatic feature selection - AI可通过JSON控制特征数量和方法
        fs_meta = {"selected_idx": None, "feature_names": feature_cols.copy()}
        selected_feature_cols = feature_cols.copy()

        # 合并配置：全局 cfg + HPO params
        # fs_method and fs_max_features are in params (not raw_hparams/model_params)
        fs_conf = dict(self.cfg.get("feature_selection", {}))
        fs_method = params.get("fs_method")
        if fs_method is not None:
            fs_conf["method"] = fs_method
            fs_conf["enabled"] = fs_method not in ("none", "", "off")
        if "fs_max_features" in params:
            fs_conf["max_features"] = int(params["fs_max_features"])
        fs_conf.setdefault("task", task)  # 传递任务类型给XGB FS

        # 应用特征选择（仅使用IS数据，避免泄露）
        if fs_conf.get("enabled", False) and fs_conf.get("method", "none").lower() not in ("none", "off", ""):
            print(f"[WalkForwardHPO] Feature Selection: method={fs_conf.get('method')}, max_features={fs_conf.get('max_features', 'auto')}")

            # 将DataFrame转为numpy array（保留valid rows only）
            X_train_fs = features_is.loc[mask].values.astype(float)
            y_train_fs = target_is.loc[mask].values.astype(float)
            X_dummy_valid = features_is.iloc[:min(100, len(features_is))].values.astype(float)  # dummy valid set for FS

            # 运行特征选择
            X_train_fs_selected, X_dummy_selected, selected_feature_cols, fs_meta = apply_feature_selection(
                X_train=X_train_fs,
                y_train=y_train_fs,
                X_valid=X_dummy_valid,
                feature_names=feature_cols,
                fs_conf=fs_conf,
                logger=None,
            )

            if fs_meta.get("selected_idx") is not None:
                selected_idx = fs_meta["selected_idx"]
                print(f"[WalkForwardHPO] Feature Selection: {len(selected_idx)}/{len(feature_cols)} features selected")

                # 应用选择的特征到features_is
                features_is = features_is.iloc[:, selected_idx]
                feature_cols = selected_feature_cols  # 更新feature_cols以便后续OOS使用

                # 保存到meta用于后续分析
                fs_meta["n_selected"] = len(selected_idx)
                fs_meta["n_total"] = len(selected_feature_cols) + len(selected_idx)  # 原始总数
            else:
                print(f"[WalkForwardHPO] Feature Selection: No features selected, using all {len(feature_cols)} features")
        else:
            print(f"[WalkForwardHPO] Feature Selection: disabled")

        # ----  LeakageGuard检测: 在训练前检查IS/OOS数据切分的合法性 ----
        lg_cfg = self.cfg.get("leakage_guard", {})
        if lg_cfg.get("enabled", True):  # 默认启用
            guard = LeakageGuard(
                time_col=lg_cfg.get("time_col", "ts"),
                symbol_col=lg_cfg.get("symbol_col", "symbol"),
                label_col=target_col,  # 使用当前的target列
                label_horizon_bars=lg_cfg.get("label_horizon_bars", self.cfg.get("model_horizon_bars", 5)),
                max_overlap_ratio=lg_cfg.get("max_overlap_ratio", 0.0),
                strict_time_order=lg_cfg.get("strict_time_order", True),
            )

            # 准备IS和OOS数据的索引（基于整个数据集df_is_sorted）
            train_idx = df_is_sorted.index[mask].tolist()  # IS中有效样本的索引

            # 获取OOS数据来做检查
            df_oos_temp = self._slice_dataset(fold.oos_start, fold.oos_end)
            if df_oos_temp is not None and not df_oos_temp.empty:
                # 合并IS和OOS数据进行整体检查
                df_combined = pd.concat([df_is_sorted, df_oos_temp], ignore_index=True)
                # IS索引是前len(df_is_sorted)行，OOS索引是后len(df_oos_temp)行
                is_len = len(df_is_sorted)
                combined_train_idx = list(range(is_len))
                combined_val_idx = list(range(is_len, is_len + len(df_oos_temp)))

                # 运行LeakageGuard检查 (记录时长)
                import time
                check_start = time.time()
                leakage_report = guard.run_all(df_combined, train_idx=combined_train_idx, val_idx=combined_val_idx)
                check_duration = time.time() - check_start

                # Prometheus打点
                if HAS_PROMETHEUS:
                    leakage_checks_total.labels(status="ok" if leakage_report.ok else "failed").inc()
                    leakage_check_duration.observe(check_duration)

                    # 记录每个issue
                    for issue in leakage_report.issues:
                        leakage_issues_total.labels(level=issue.level, check_name=issue.check).inc()

                        # 记录重叠比例 (overlap_keys检查)
                        if issue.check == "overlap_keys" and "overlap_ratio" in issue.stats:
                            for sym in df_combined["symbol"].unique():
                                leakage_overlap_ratio.labels(symbol=str(sym)).set(issue.stats["overlap_ratio"])

                # 打印检查结果
                print(f"[LeakageGuard] Leakage check: ok={leakage_report.ok}, issues={len(leakage_report.issues)}, duration={check_duration:.3f}s")
                for issue in leakage_report.issues:
                    level_symbol = "✅" if issue.level == "INFO" else ("⚠️" if issue.level == "WARN" else "❌")
                    print(f"[LeakageGuard] {level_symbol} [{issue.level}] {issue.check}: {issue.message}")

                # 如果检测到ERROR级别问题且配置为fail_on_error，则直接终止该trial
                if not leakage_report.ok and lg_cfg.get("fail_on_error", True):
                    print(f"[LeakageGuard] ❌ CRITICAL: Leakage detected, skipping this trial!")
                    # 返回一个失败的元数据，让该trial得分很低
                    failed_meta = {
                        "leakage_report": leakage_report.to_dict(),
                        "status": "failed_leakage_check",
                        "error": "Data leakage detected by LeakageGuard"
                    }
                    return None, None, failed_meta
            else:
                print(f"[LeakageGuard] INFO: No OOS data available, skipping leakage check")
        # ---- LeakageGuard检测结束 ----

        model.fit(features_is.loc[mask], target_is.loc[mask])

        preds_is = model.predict(features_is)
        sig_is = model.to_signal(preds_is)
        if model.task == "clf":
            conf_is = np.clip(np.abs(preds_is - 0.5) * 2.0, 0.0, 1.0)
        else:
            # BUGFIX: For regression, normalize confidence by prediction std to get 0-1 scale
            # Old: conf_is = np.clip(np.abs(preds_is), 0.0, 1.0)  # Wrong - predictions are tiny returns
            pred_std = np.std(preds_is) if len(preds_is) > 0 else 1e-9
            pred_std = max(pred_std, 1e-9)  # Avoid division by zero
            conf_is = np.clip(np.abs(preds_is) / (3 * pred_std), 0.0, 1.0)  # 3-sigma normalization

        df_is_aug = df_is_sorted.copy()
        df_is_aug["ml_pred"] = preds_is
        df_is_aug["ml_signal"] = sig_is
        df_is_aug["ml_conf"] = conf_is

        df_oos_aug: Optional[pd.DataFrame] = None
        df_oos = self._slice_dataset(fold.oos_start, fold.oos_end)
        if df_oos is not None and not df_oos.empty:
            df_oos_sorted = df_oos.sort_values("ts").reset_index(drop=True)

            # >>> FT-INTEGRATION BEGIN <<<
            # Apply Featuretools DFS transform to OOS (using IS-fitted definitions)
            ft_features_oos = None
            if use_ft and ft_adapter is not None:
                try:
                    ft_features_oos = ft_adapter.transform_any(df_oos_sorted)
                    print(f"[WalkForwardHPO] DFS transform OOS: {ft_features_oos.shape}")
                except Exception as e:
                    print(f"[WalkForwardHPO] WARNING: DFS OOS transform failed: {e}")
                    ft_features_oos = None
            # >>> FT-INTEGRATION END <<<

            # ========== PHASE 1: Apply SAME DSL feature program to OOS ==========
            # CRITICAL: Use DSL (not hardcoded) to ensure IS/OOS consistency
            if feature_program_id and feature_program_id not in ("", "none"):
                print(f"[WalkForwardHPO] Applying feature program '{feature_program_id}' to OOS data")
                df_oos_sorted, oos_new_feats, _ = self.feature_runner.apply(
                    df_oos_sorted,
                    feature_program_id,
                    verbose=False
                )
                if oos_new_feats:
                    print(f"[WalkForwardHPO] DSL created {len(oos_new_feats)} OOS features: {oos_new_feats}")
            else:
                print(f"[WalkForwardHPO] No feature program, using raw features for OOS")

            # Check if all feature_cols exist in df_oos_sorted
            missing_cols = [c for c in feature_cols if c not in df_oos_sorted.columns]
            if missing_cols:
                print(f"[WalkForwardHPO] ERROR: OOS missing columns: {missing_cols}")
                print(f"[WalkForwardHPO] Available columns: {list(df_oos_sorted.columns)}")
                raise ValueError(f"OOS data missing required feature columns: {missing_cols}")

            features_oos = df_oos_sorted[feature_cols]
            features_oos = features_oos.fillna(method="ffill").fillna(method="bfill").fillna(0.0)

            # ========== PHASE 1: Apply same scaler to OOS (no data leakage) ==========
            if scale_mode in ("zscore", "robust"):
                if scale_mode == "zscore":
                    mean = pd.Series(scaler_meta["mean"])
                    std = pd.Series(scaler_meta["std"]).replace(0.0, 1.0)
                    features_oos = (features_oos - mean) / std
                else:  # robust
                    median = pd.Series(scaler_meta["median"])
                    iqr = pd.Series(scaler_meta["iqr"]).replace(0.0, 1.0)
                    features_oos = (features_oos - median) / iqr

                features_oos = features_oos.clip(-5.0, 5.0)
            # Align sequence models by concatenating IS tail for warm-start predictions
            lookback = int(getattr(model, "lookback", 1) or 1)
            if lookback > 1:
                tail = features_is.tail(max(0, lookback - 1))
                combined = pd.concat([tail, features_oos], axis=0)
                preds_combined = model.predict(combined)
                preds_oos = preds_combined[-len(features_oos):]
            else:
                preds_oos = model.predict(features_oos)
            sig_oos = model.to_signal(preds_oos)
            if model.task == "clf":
                conf_oos = np.clip(np.abs(preds_oos - 0.5) * 2.0, 0.0, 1.0)
            else:
                # BUGFIX: For regression, normalize confidence using IS prediction std (same scale as IS)
                pred_std = np.std(preds_is) if len(preds_is) > 0 else 1e-9
                pred_std = max(pred_std, 1e-9)
                conf_oos = np.clip(np.abs(preds_oos) / (3 * pred_std), 0.0, 1.0)
            df_oos_aug = df_oos_sorted.copy()
            df_oos_aug["ml_pred"] = preds_oos
            df_oos_aug["ml_signal"] = sig_oos
            df_oos_aug["ml_conf"] = conf_oos

        meta = asdict(model.metadata()) if hasattr(model, "metadata") else {}
        meta["feature_cols"] = feature_cols
        meta["target_col"] = target_col
        meta["scaler"] = scaler_meta  # PHASE 1: Save scaler info for production use

        # ========= DIAGNOSTICS: labels, features, predictions =========
        diagnostics: Dict[str, Any] = {}

        # 1) Label distribution (IS, training mask)
        try:
            y_train = target_is.loc[mask].astype(float)
            if len(y_train) > 0:
                ys = y_train

                def _q(p: float) -> float:
                    return float(ys.quantile(p))

                label_stats: Dict[str, Any] = {
                    "n": int(ys.shape[0]),
                    "mean": float(ys.mean()),
                    "std": float(ys.std()),
                    "min": float(ys.min()),
                    "p01": _q(0.01),
                    "p05": _q(0.05),
                    "p25": _q(0.25),
                    "p50": _q(0.50),
                    "p75": _q(0.75),
                    "p95": _q(0.95),
                    "p99": _q(0.99),
                    "max": float(ys.max()),
                    "pos_frac": float((ys > 0).mean()),
                    "neg_frac": float((ys < 0).mean()),
                    "zero_frac": float((ys == 0).mean()),
                }
                diagnostics["label_stats"] = label_stats

                # 2) Feature IC (Spearman) against target
                feature_ic: Dict[str, Any] = {}
                for col in feature_cols:
                    try:
                        s = features_is.loc[mask, col].astype(float)
                        # Require at least a few distinct values to avoid spurious NaNs
                        if s.nunique(dropna=True) < 3:
                            continue
                        ic_val = s.corr(ys, method="spearman")
                        if pd.notna(ic_val):
                            feature_ic[col] = float(ic_val)
                    except Exception:
                        continue
                diagnostics["feature_ic_spearman"] = feature_ic
        except Exception:
            # Diagnostics must never break training
            pass

        # 3) Prediction / signal / confidence stats (IS)
        try:
            if preds_is is not None and len(preds_is) > 0:
                arr_pred = np.asarray(preds_is, dtype=float)

                def _basic_stats(arr: np.ndarray) -> Dict[str, Any]:
                    if arr.size == 0:
                        return {}
                    return {
                        "mean": float(arr.mean()),
                        "std": float(arr.std()),
                        "min": float(arr.min()),
                        "p10": float(np.quantile(arr, 0.10)),
                        "p50": float(np.quantile(arr, 0.50)),
                        "p90": float(np.quantile(arr, 0.90)),
                        "max": float(arr.max()),
                    }

                diagnostics["pred_stats"] = _basic_stats(arr_pred)

            if sig_is is not None and len(sig_is) > 0:
                arr_sig = np.asarray(sig_is, dtype=float)
                diagnostics["signal_stats"] = {
                    "n": int(arr_sig.size),
                    "mean": float(arr_sig.mean()),
                    "pos_frac": float((arr_sig > 0).mean()),
                    "neg_frac": float((arr_sig < 0).mean()),
                    "zero_frac": float((arr_sig == 0).mean()),
                    "avg_abs": float(np.abs(arr_sig).mean()),
                }

            if conf_is is not None and len(conf_is) > 0:
                arr_conf = np.asarray(conf_is, dtype=float)
                diagnostics["conf_stats"] = {
                    "mean": float(arr_conf.mean()),
                    "p25": float(np.quantile(arr_conf, 0.25)),
                    "p50": float(np.quantile(arr_conf, 0.50)),
                    "p75": float(np.quantile(arr_conf, 0.75)),
                    "frac_ge_0_2": float((arr_conf >= 0.2).mean()),
                    "frac_ge_0_5": float((arr_conf >= 0.5).mean()),
                }
        except Exception:
            # Do not let diagnostics affect training or backtests
            pass

        if diagnostics:
            meta["diagnostics"] = diagnostics

        # Proactively free large temporaries before returning
        try:
            del features_is, target_is, preds_is, sig_is
        except Exception:
            pass
        try:
            del features_oos, preds_oos
        except Exception:
            pass
        gc.collect()

        return df_is_aug, df_oos_aug, meta

    # ----- IS Grid (serial/parallel) -----
    def _grid_eval(
        self,
        fold: Fold,
        grid_list: List[Dict[str, Any]],
        df_slice: Optional[pd.DataFrame] = None
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []

        # Load resume state and filter already-completed trials
        resume_data = _load_resume_state(self.out_dir)
        original_count = len(grid_list)

        if resume_data:
            # Build set of completed param hashes for this fold
            completed_hashes: Set[str] = {
                trial["params_hash"]
                for trial in resume_data["trials"]
                if trial["fold_id"] == fold.fold_id
            }

            # Filter grid_list to skip completed trials
            grid_list = [
                params for params in grid_list
                if _hash_params(params) not in completed_hashes
            ]

            # Load completed rows for this fold
            completed_rows = [
                trial for trial in resume_data["trials"]
                if trial["fold_id"] == fold.fold_id
            ]

            # Convert completed trials back to row format
            for trial in completed_rows:
                row = {
                    "fold_id": trial["fold_id"],
                    "phase": trial.get("phase", "IS"),
                    "start": fold.is_start,
                    "end": fold.is_end,
                    "params": _jsonify(trial["params"]),
                    **trial["results"]
                }
                rows.append(row)

            print(f"[Resume] Loaded {len(completed_rows)} completed trials for fold {fold.fold_id}")
            print(f"[Resume] Skipping {len(completed_hashes)} already-tested params")
            print(f"[Resume] Running {len(grid_list)}/{original_count} remaining trials")
        else:
            print(f"[Resume] No previous results found - running all {original_count} trials")

        # If all trials already completed, return early
        if not grid_list:
            print(f"[Resume] All trials for fold {fold.fold_id} already completed!")
            return rows

        if self.workers > 1:
            fut2meta = {}
            print(f"[Progress] Submitting {len(grid_list)} trials to {self.workers} workers...")
            with ProcessPoolExecutor(max_workers=self.workers) as ex:
                for idx, params in enumerate(grid_list):
                    # Use adapter to parse parameters
                    strategy_params = self.adapter.parse_params(
                        grid_params=params,
                        base_extras=self.strategy_base_extras
                    )

                    # Only prepare ML data if strategy supports ML signals
                    if self.adapter.supports_ml_signals():
                        df_aug, _, meta = self._prepare_model_data(fold, params, df_slice)
                        data_for_run = df_aug if df_aug is not None else df_slice
                    else:
                        data_for_run = df_slice
                        meta = None

                    fut = ex.submit(_run_engine_external, self._bt_cfg_dict, fold.is_start, fold.is_end, strategy_params, data_for_run)
                    fut2meta[fut] = (params, meta)
                    print(f"[Progress] Submitted trial {idx+1}/{len(grid_list)}")

                print(f"[Progress] All {len(grid_list)} trials submitted, waiting for results...")
                completed = 0
                for fut in as_completed(list(fut2meta.keys())):
                    params, meta = fut2meta[fut]
                    print(f"[Progress] Trial {completed+1}/{len(grid_list)} completed, getting result...")
                    try:
                        res = fut.result(timeout=600)  # 10 minute timeout per trial
                        completed += 1
                        print(f"[Progress] Trial {completed}/{len(grid_list)} result retrieved successfully")
                    except TimeoutError:
                        print(f"[ERROR] Trial {completed+1}/{len(grid_list)} TIMEOUT after 600s")
                        res = {"error": "timeout"}
                    except Exception as e:
                        print(f"[ERROR] Trial {completed+1}/{len(grid_list)} FAILED: {type(e).__name__}: {e}")
                        res = {"error": str(e)}
                    met = _merge_metrics_with_extras(res, self.objective)
                    row = {
                        "fold_id": fold.fold_id,
                        "phase": "IS",
                        "start": fold.is_start,
                        "end": fold.is_end,
                        "params": _jsonify(params),
                        **met
                    }
                    if meta:
                        row["model_meta"] = json.dumps(meta, ensure_ascii=False)
                    rows.append(row)

                    # Save trial result to resume JSONL (atomic append)
                    _save_trial_result(
                        out_dir=self.out_dir,
                        gen=1,  # Generation number (can be passed as parameter later)
                        fold_id=fold.fold_id,
                        params=params,
                        results=met,
                        phase="IS"
                    )
                    print(f"[Progress] Trial {completed}/{len(grid_list)} saved to resume log")
        else:
            import psutil, os, gc
            proc = psutil.Process(os.getpid())

            for trial_idx, params in enumerate(grid_list):
                mem_trial_start = proc.memory_info().rss / 1024 / 1024
                if trial_idx % 5 == 0:  # Log every 5 trials to reduce noise
                    print(f"[MEM-TRIAL] Trial {trial_idx+1}/{len(grid_list)}: {mem_trial_start:.1f} MB", flush=True)

                # Use adapter to parse parameters
                strategy_params = self.adapter.parse_params(
                    grid_params=params,
                    base_extras=self.strategy_base_extras
                )

                # Only prepare ML data if strategy supports ML signals
                try:
                    if self.adapter.supports_ml_signals():
                        df_aug, _, meta = self._prepare_model_data(fold, params, df_slice)
                        mem_after_ml = proc.memory_info().rss / 1024 / 1024
                        if trial_idx % 5 == 0:
                            print(f"[MEM-TRIAL] After ML prep: {mem_after_ml:.1f} MB (+{mem_after_ml - mem_trial_start:.1f} MB)", flush=True)
                        data_for_run = df_aug if df_aug is not None else df_slice
                    else:
                        data_for_run = df_slice
                        meta = None

                    res = _run_engine_external(self._bt_cfg_dict, fold.is_start, fold.is_end, strategy_params, df=data_for_run)
                    mem_after_backtest = proc.memory_info().rss / 1024 / 1024
                    if trial_idx % 5 == 0:
                        print(f"[MEM-TRIAL] After backtest: {mem_after_backtest:.1f} MB", flush=True)

                    met = _merge_metrics_with_extras(res, self.objective)

                except Exception as e:
                    # Robust handling of data/label issues (e.g., no samples, RandomSampler errors)
                    msg = str(e)
                    print(f"[ERROR] Trial {trial_idx+1}/{len(grid_list)} FAILED: {type(e).__name__}: {msg}", flush=True)
                    met = {
                        "total_return": 0.0,
                        "sharpe": 0.0,
                        "max_drawdown": 0.0,
                        "win_rate": 0.0,
                        "profit_factor": 0.0,
                        "trades": 0,
                    }
                    if "No training samples" in msg or "num_samples" in msg or "should be a positive integer" in msg:
                        met["error"] = "no_samples"
                    else:
                        met["error"] = msg
                    mem_after_cleanup = proc.memory_info().rss / 1024 / 1024
                else:
                    # Explicit cleanup every trial
                    del res, data_for_run
                    # Only delete meta if it was actually created
                    if self.adapter.supports_ml_signals() and 'meta' in locals():
                        del meta
                    if self.adapter.supports_ml_signals() and 'df_aug' in locals():
                        del df_aug
                    gc.collect()

                    mem_after_cleanup = proc.memory_info().rss / 1024 / 1024
                    if trial_idx % 5 == 0:
                        print(f"[MEM-TRIAL] After cleanup: {mem_after_cleanup:.1f} MB (freed: {mem_after_backtest - mem_after_cleanup:.1f} MB)", flush=True)
                row = {
                    "fold_id": fold.fold_id,
                    "phase": "IS",
                    "start": fold.is_start,
                    "end": fold.is_end,
                    "params": _jsonify(params),
                    **met
                }
                if 'meta' in locals() and meta:
                    row["model_meta"] = json.dumps(meta, ensure_ascii=False)
                rows.append(row)

                # Save trial result to resume JSONL
                _save_trial_result(
                    out_dir=self.out_dir,
                    gen=1,
                    fold_id=fold.fold_id,
                    params=params,
                    results=met,
                    phase="IS"
                )
        return rows

    def _eval_oos(self, fold: Fold, best_params: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        # Use adapter to parse parameters
        strategy_params = self.adapter.parse_params(
            grid_params=best_params,
            base_extras=self.strategy_base_extras
        )

        # MEMORY FIX: Calculate IC on the fly and don't hold the dataframe
        ic_value: Optional[float] = None
        
        # Only prepare ML data if strategy supports ML signals
        if self.adapter.supports_ml_signals():
            _, df_oos, meta = self._prepare_model_data(fold, best_params)
            if df_oos is None:
                df_oos = self._slice_dataset(fold.oos_start, fold.oos_end)
            else:
                # ⭐ Save predictions and labels for IC calculation
                target_col = best_params.get("target_col") or self.cfg.get("model_target_col", "y")
                if "ml_pred" in df_oos.columns and target_col in df_oos.columns:
                    df_oos_for_ic = df_oos[["ml_pred", target_col]].copy()
                    # 准备横截面IC计算所需的列（ts, symbol可选）
                    for col in ["ts", "symbol"]:
                        if col in df_oos.columns:
                            df_oos_for_ic[col] = df_oos[col]

                    df_oos_for_ic = df_oos_for_ic.rename(columns={
                        "ml_pred": "pred",
                        target_col: "future_ret"
                    })
                    if HAS_IC_CALCULATOR:
                        try:
                            # 使用机构级Spearman IC（横截面模式）
                            ic_val, ic_cov = compute_ic_oos(
                                df_oos=df_oos_for_ic,
                                ret_col="future_ret",
                                pred_col="pred",
                                mode="cross_sectional",  # 多标的横截面
                                ts_col="ts",
                                symbol_col="symbol",
                                min_n=3
                            )
                            ic_value = float(ic_val) if ic_val is not None else None
                            ic_coverage = float(ic_cov)
                        except Exception:
                            ic_value = None
                            ic_coverage = 0.0
        else:
            df_oos = self._slice_dataset(fold.oos_start, fold.oos_end)
            meta = None

        res = _run_engine_external(self._bt_cfg_dict, fold.oos_start, fold.oos_end, strategy_params, df=df_oos)
        met = _merge_metrics_with_extras(res, self.objective)

        # Add IC & coverage to metrics if calculated
        if ic_value is not None:
            met["ic"] = ic_value
            met["ic_coverage"] = ic_coverage
        else:
            met["ic_coverage"] = 0.0

        row = {
            "fold_id": fold.fold_id,
            "phase": "OOS",
            "start": fold.oos_start,
            "end": fold.oos_end,
            "params": _jsonify(best_params),
            **met
        }
        if meta:
            row["model_meta"] = json.dumps(meta, ensure_ascii=False)

        # MEMORY FIX: Only return equity curve, not the dataframe
        oos_payload = {
            "fold_id": fold.fold_id,
            "equity_curve": res.get("equity_curve", [])
        }
        return row, oos_payload

    # ----- main loop -----
    def _run_core(self, trial=None) -> Dict[str, Any]:
        grid_list = _product_dict(self.grid)
        all_rows: List[Dict[str, Any]] = []
        oos_rows: List[Dict[str, Any]] = []
        oos_eqs: List[Dict[str, Any]] = []
        best_params_by_fold: List[Dict[str, Any]] = []

        # Check for resume capability
        resume_data = _load_resume_state(self.out_dir)
        if resume_data:
            total_completed = len(resume_data["trials"])
            folds_completed = len(set(t["fold_id"] for t in resume_data["trials"]))
            print(f"╔══════════════════════════════════════════════════════════════════════╗")
            print(f"║ RESUME DETECTED                                                      ║")
            print(f"╠══════════════════════════════════════════════════════════════════════╣")
            print(f"║ Found {total_completed} completed trials across {folds_completed} fold(s)")
            print(f"║ Resume file: {self.out_dir}/completed_trials.jsonl")
            print(f"║ Will skip already-completed trials and continue from where left off ║")
            print(f"╚══════════════════════════════════════════════════════════════════════╝")
        else:
            print(f"[Resume] No previous results found in {self.out_dir} - starting fresh")

        import psutil, os
        proc = psutil.Process(os.getpid())

        for fold_idx, fold in enumerate(self.folds):
            mem_fold_start = proc.memory_info().rss / 1024 / 1024
            print(f"[MEM-FOLD] Fold {fold_idx+1}/{len(self.folds)} START: {mem_fold_start:.1f} MB", flush=True)

            df_is = self._slice_dataset(fold.is_start, fold.is_end)
            mem_after_slice = proc.memory_info().rss / 1024 / 1024
            print(f"[MEM-FOLD] After _slice_dataset: {mem_after_slice:.1f} MB (+{mem_after_slice - mem_fold_start:.1f} MB), df shape: {df_is.shape if df_is is not None else 'None'}", flush=True)

            # 1) grid on IS
            is_rows = self._grid_eval(fold, grid_list, df_slice=df_is)
            mem_after_grid = proc.memory_info().rss / 1024 / 1024
            print(f"[MEM-FOLD] After _grid_eval: {mem_after_grid:.1f} MB (+{mem_after_grid - mem_after_slice:.1f} MB), rows: {len(is_rows)}", flush=True)

            all_rows.extend(is_rows)

            # 2) select best
            scored = []
            for r in is_rows:
                sc = self._score(r)
                if sc is not None:
                    scored.append((sc, r))
            if not scored:
                # fallback to raw best by selected metric
                key = self.objective.metric
                scored = [((r.get(key, 0.0) or -1e9), r) for r in is_rows]
            scored.sort(key=lambda x: x[0], reverse=True)
            top_row = scored[0][1]
            best_params = json.loads(top_row["params"])
            best_params_by_fold.append({"fold_id": fold.fold_id, "params": best_params, "is_metrics": {k: top_row[k] for k in top_row if k not in ("phase","params","fold_id","start","end")}})

            # 3) OOS eval
            oos_row, oos_payload = self._eval_oos(fold, best_params)
            all_rows.append(oos_row)
            oos_rows.append(oos_row)
            oos_eqs.append(oos_payload)

            # Update resume state after completing this fold
            _update_resume_state(
                out_dir=self.out_dir,
                gen=1,
                fold_id=fold.fold_id,
                total_trials=len(all_rows)
            )
            print(f"[Resume] State updated: completed fold {fold.fold_id}, {len(all_rows)} total trials")

            if trial is not None:
                cur_score = float(oos_row.get("sharpe", 0.0) - oos_row.get("max_drawdown", 0.0))
                trial.report(cur_score, step=fold.fold_id - 1)
                if trial.should_prune():
                    import optuna
                    raise optuna.TrialPruned()

        # 4) aggregate OOS
        oos_df = pd.DataFrame(oos_rows)
        if not oos_df.empty:
            oos_avg_sharpe = float(oos_df["sharpe"].mean() if "sharpe" in oos_df else 0.0)
            oos_trades = int(oos_df["trades"].sum() if "trades" in oos_df else 0)

            # Calculate PSR/DSR/PBO
            # n = average trades across folds (for PSR/DSR)
            n_samples = max(1, int(oos_df["trades"].mean() if "trades" in oos_df else 1))
            # trials = unique parameter combinations tested
            trials = len({r.get("params", "{}") for r in all_rows})
            # OOS Sharpe ratios for PBO
            oos_fold_sharpes = oos_df["sharpe"].tolist() if "sharpe" in oos_df else []

            oos_psr = probabilistic_sharpe_ratio(oos_avg_sharpe, n_samples, ref=0.0)
            oos_dsr = deflated_sharpe(oos_avg_sharpe, n_samples, trials=max(2, trials))
            oos_pbo = pbo_fraction(oos_fold_sharpes)

            # MEMORY FIX: Calculate IC from the 'ic' column in oos_df
            if "ic" in oos_df.columns:
                oos_avg_ic = float(oos_df["ic"].mean())
            else:
                oos_avg_ic = 0.0

            # IC Coverage（有IC的fold数 / 总fold数）
            if "ic_coverage" in oos_df.columns:
                oos_ic_coverage = float(oos_df["ic_coverage"].mean())
            else:
                oos_ic_coverage = 0.0

            summary = {
                "oos_avg_total_return": float(oos_df["total_return"].mean() if "total_return" in oos_df else 0.0),
                "oos_avg_sharpe": oos_avg_sharpe,
                "oos_avg_ic": oos_avg_ic,
                "ic_coverage": oos_ic_coverage,
                "oos_avg_max_drawdown": float(oos_df["max_drawdown"].mean() if "max_drawdown" in oos_df else 0.0),
                "oos_avg_win_rate": float(oos_df["win_rate"].mean() if "win_rate" in oos_df else 0.0),
                "oos_avg_profit_factor": float(oos_df["profit_factor"].mean() if "profit_factor" in oos_df else 0.0),
                "oos_avg_sortino": float(oos_df["sortino"].mean() if "sortino" in oos_df else 0.0),
                "oos_avg_omega": float(oos_df["omega"].mean() if "omega" in oos_df else 0.0),
                "oos_avg_calmar": float(oos_df["calmar"].mean() if "calmar" in oos_df else 0.0),
                "oos_trades": oos_trades,
                "oos_psr": oos_psr,
                "oos_dsr": oos_dsr,
                "oos_pbo": oos_pbo,
                "folds": int(oos_df["fold_id"].nunique())
            }
        else:
            summary = {}

        return {
            "rows": all_rows,
            "oos_summary": summary,
            "best_params_by_fold": best_params_by_fold,
            "oos_equity_curves": oos_eqs
        }

    def run(self, trial=None) -> Dict[str, Any]:
        return self._run_core(trial=trial)

    def run_with_dataframe(self, df: pd.DataFrame, trial=None) -> Dict[str, Any]:
        if df is None:
            raise ValueError("DataFrame is required")

        # Data is now assumed to be pre-cleaned in the calling loop (e.g., alpha_loop.py)
        # This method just sets the dataframe and runs the core logic.
        prev_dataset = self.dataset_df
        try:
            self.dataset_df = df
            return self._run_core(trial=trial)
        finally:
            self.dataset_df = prev_dataset

    # ------------------ Exports ------------------

    @staticmethod
    def export_tables(result: Dict[str, Any], out_dir: str) -> Dict[str, str]:
        """
        Save rows & summaries to CSV/Parquet; return paths.
        """
        _ensure_dir(out_dir)
        rows = pd.DataFrame(result.get("rows", []))
        paths = {}
        if not rows.empty:
            p_csv = os.path.join(out_dir, "rows.csv")
            rows.to_csv(p_csv, index=False)
            paths["rows_csv"] = p_csv
            try:
                p_par = os.path.join(out_dir, "rows.parquet")
                rows.to_parquet(p_par, index=False)
                paths["rows_parquet"] = p_par
            except Exception:
                pass

            # Leaderboards
            if "phase" in rows:
                is_df = rows[rows["phase"] == "IS"].copy()
                oos_df = rows[rows["phase"] == "OOS"].copy()
            else:
                is_df = pd.DataFrame(); oos_df = pd.DataFrame()

            def _save_lb(df: pd.DataFrame, fname: str) -> Optional[str]:
                if df.empty:
                    return None
                p = os.path.join(out_dir, fname)
                df.sort_values(["fold_id", "sharpe"], ascending=[True, False]).to_csv(p, index=False)
                return p

            p_is = _save_lb(is_df, "leaderboard_is.csv")
            p_oos = _save_lb(oos_df, "leaderboard_oos.csv")
            if p_is: paths["leaderboard_is_csv"] = p_is
            if p_oos: paths["leaderboard_oos_csv"] = p_oos

        # Summaries
        p_sum = os.path.join(out_dir, "oos_summary.json")
        with open(p_sum, "w", encoding="utf-8") as f:
            json.dump(result.get("oos_summary", {}), f, indent=2, ensure_ascii=False)
        paths["oos_summary_json"] = p_sum

        p_best = os.path.join(out_dir, "best_params_by_fold.json")
        with open(p_best, "w", encoding="utf-8") as f:
            json.dump(result.get("best_params_by_fold", []), f, indent=2, ensure_ascii=False)
        paths["best_params_by_fold_json"] = p_best

        # Champion statistics: count of param choices among best
        champs = pd.DataFrame(result.get("best_params_by_fold", []))
        if not champs.empty and "params" in champs.columns:
            param_dicts = champs["params"].apply(lambda d: d)
            exp = pd.json_normalize(param_dicts)
            exp.insert(0, "fold_id", champs["fold_id"])
            p_champ = os.path.join(out_dir, "champion_params.csv")
            exp.to_csv(p_champ, index=False)
            paths["champion_params_csv"] = p_champ

        return paths

    # ------------------ Plot Exports ------------------

    @staticmethod
    def export_plots(result: Dict[str, Any], out_dir: str) -> Dict[str, str]:
        """
        Generate PNG charts:
          - oos_equity.png : normalized equity per fold
          - oos_drawdown.png : drawdown per fold
          - is_heatmap_<p1>_vs_<p2>.png : IS leaderboard heatmap for top-2 varied params
        Notes:
          * Uses matplotlib; one chart per figure; no explicit colors.
        """
        if not HAS_MATPLOTLIB:
            return {}
            
        _ensure_dir(out_dir)
        out: Dict[str, str] = {}

        # 1) OOS Equity
        oos_eqs = result.get("oos_equity_curves", [])
        if oos_eqs:
            plt.figure()
            for payload in oos_eqs:
                curve = payload.get("equity_curve", [])
                if not curve:
                    continue
                xs = [ts for ts, _ in curve]
                eq = [float(e) for _, e in curve]
                if not eq:
                    continue
                base = eq[0] if eq[0] != 0 else 1e-9
                norm = [e / base for e in eq]
                plt.plot(xs, norm, label=f"Fold {payload.get('fold_id')}")
            plt.xlabel("Timestamp (ms)")
            plt.ylabel("Normalized Equity")
            plt.title("OOS Equity (per fold)")
            plt.legend()
            p = os.path.join(out_dir, "oos_equity.png")
            plt.savefig(p, dpi=144, bbox_inches="tight")
            plt.close()
            out["oos_equity_png"] = p

        # 2) OOS Drawdown
        if oos_eqs:
            plt.figure()
            for payload in oos_eqs:
                curve = payload.get("equity_curve", [])
                if not curve:
                    continue
                eq = [float(e) for _, e in curve]
                if not eq:
                    continue
                peak = -1e18
                dd_series = []
                for e in eq:
                    peak = max(peak, e)
                    dd = 0.0 if peak <= 0 else (peak - e) / peak
                    dd_series.append(dd)
                xs = [ts for ts, _ in curve]
                plt.plot(xs, dd_series, label=f"Fold {payload.get('fold_id')}")
            plt.xlabel("Timestamp (ms)")
            plt.ylabel("Drawdown")
            plt.title("OOS Drawdown (per fold)")
            plt.legend()
            p = os.path.join(out_dir, "oos_drawdown.png")
            plt.savefig(p, dpi=144, bbox_inches="tight")
            plt.close()
            out["oos_drawdown_png"] = p

        # 3) IS Heatmap (top-2 varied params)
        rows = pd.DataFrame(result.get("rows", []))
        if not rows.empty and "phase" in rows and "params" in rows.columns:
            is_rows = rows[rows["phase"] == "IS"].copy()
            if not is_rows.empty:
                # Expand params JSON
                try:
                    is_rows["_pdict"] = is_rows["params"].apply(json.loads)
                except Exception:
                    is_rows["_pdict"] = [{} for _ in range(len(is_rows))]
                pexp = pd.json_normalize(is_rows["_pdict"])
                if not pexp.empty:
                    # Find two most-varied params
                    var_counts = pexp.nunique().sort_values(ascending=False)
                    varied = [c for c, n in var_counts.items() if n > 1]
                    if len(varied) >= 2:
                        p1, p2 = varied[0], varied[1]
                        mat = is_rows.copy()
                        mat[p1] = pexp[p1]
                        mat[p2] = pexp[p2]
                        pivot = mat.pivot_table(index=p1, columns=p2, values="sharpe", aggfunc="mean")
                        if pivot.shape[0] >= 2 and pivot.shape[1] >= 2:
                            plt.figure()
                            plt.imshow(pivot.values, aspect="auto", origin="lower")
                            plt.xticks(range(len(pivot.columns)), [str(c) for c in pivot.columns], rotation=45)
                            plt.yticks(range(len(pivot.index)), [str(i) for i in pivot.index])
                            plt.xlabel(str(p2))
                            plt.ylabel(str(p1))
                            plt.title(f"IS Heatmap (Sharpe): {p1} vs {p2}")
                            p = os.path.join(out_dir, f"is_heatmap_{p1}_vs_{p2}.png")
                            plt.savefig(p, dpi=144, bbox_inches="tight")
                            plt.close()
                            out["is_heatmap_png"] = p

        return out


# ------------------ IC/IR Calculation Helper ------------------

def _calculate_ic_ir_coverage(result: dict) -> dict:
    """
    Calculate Model Layer metrics (IC/IR/coverage) from backtest result.

    Requires result['trades_df'] to contain columns:
    - 'ml_pred': ML model prediction (continuous)
    - 'forward_return': Actual forward return
    - 'ts': Timestamp (optional, for daily IC calculation)

    Returns:
        {
            "ic": float,       # Information Coefficient (Spearman correlation)
            "ir": float,       # Information Ratio (mean_IC / std_IC)
            "coverage": float  # Fraction of valid predictions
        }
    """
    try:
        import pandas as pd
        import numpy as np
        from scipy.stats import spearmanr

        trades_df = result.get("trades_df")
        if trades_df is None or len(trades_df) == 0:
            return {"ic": 0.0, "ir": 0.0, "coverage": 0.0}

        # Check required columns
        required_cols = ["ml_pred", "forward_return"]
        if not all(col in trades_df.columns for col in required_cols):
            return {"ic": 0.0, "ir": 0.0, "coverage": 0.0}

        # Filter valid rows
        valid_rows = trades_df[required_cols].dropna()
        if len(valid_rows) < 10:
            coverage = len(valid_rows) / len(trades_df) if len(trades_df) > 0 else 0.0
            return {"ic": 0.0, "ir": 0.0, "coverage": coverage}

        # Calculate overall IC (Spearman correlation)
        ic, _ = spearmanr(valid_rows["ml_pred"], valid_rows["forward_return"])

        # Calculate IR (Information Ratio)
        # Method: Calculate daily IC, then IR = mean(daily_IC) / std(daily_IC)
        ir = 0.0
        if "ts" in trades_df.columns:
            try:
                trades_df["date"] = pd.to_datetime(trades_df["ts"]).dt.date
                daily_ic = []
                for date, group in valid_rows.groupby(trades_df.loc[valid_rows.index, "date"]):
                    if len(group) >= 5:
                        ic_day, _ = spearmanr(group["ml_pred"], group["forward_return"])
                        if not np.isnan(ic_day):
                            daily_ic.append(ic_day)

                if len(daily_ic) >= 2:
                    mean_ic = np.mean(daily_ic)
                    std_ic = np.std(daily_ic)
                    ir = mean_ic / std_ic if std_ic > 0 else 0.0
            except Exception:
                pass  # Fallback to IR=0

        # Calculate coverage
        coverage = len(valid_rows) / len(trades_df)

        return {
            "ic": float(ic) if not np.isnan(ic) else 0.0,
            "ir": float(ir) if not np.isnan(ir) else 0.0,
            "coverage": float(coverage)
        }

    except Exception as e:
        # Gracefully handle any errors
        return {"ic": 0.0, "ir": 0.0, "coverage": 0.0}


# ------------------ Optuna Adapter Function ------------------

def evaluate_trial(config: dict, params: dict, fold: dict | None) -> dict:
    """
    Adapter function for Optuna-based HPO (hpo_cli_upgraded.py).

    This function is called by the upgraded CLI for each trial.
    It runs a backtest for a single parameter combination and fold,
    and returns performance metrics.

    Args:
        config: Full HPO configuration dict (from JSON config file)
        params: Parameter dict for this trial (e.g., {"A1_thr_sigma": 0.008, "A2_grid_g": 1.0})
        fold: Fold dict with OOS window (e.g., {"fold_id": 1, "oos_start": ..., "oos_end": ...})
              If None, will use the entire period in config.

    Returns:
        Dict with metrics: {
            "sharpe": float,
            "mdd": float (max_drawdown),
            "pnl": float (total_return),
            "trades": int,
            "sortino": float,
            "omega": float,
            "calmar": float,
            "win_rate": float,
            "profit_factor": float
        }
    """
    # Build strategy_params using the registered StrategyAdapter to ensure
    # correct nested structure {"extras": {...}} and dotted-key merges.
    strategy_base_extras = config.get("strategy_base_extras", {}) or {}
    try:
        from strategy_registry import StrategyRegistry  # adapter registry
        adapter = StrategyRegistry.get_adapter(config["strategy_path"])  # may raise if not registered
        strategy_params = adapter.parse_params(grid_params=params, base_extras=strategy_base_extras)
    except Exception:
        # Fallback: shallow merge into extras
        def _merge_dot(dst: dict, dotted_key: str, val):
            cur = dst
            parts = dotted_key.split(".")
            for p in parts[:-1]:
                if p not in cur or not isinstance(cur[p], dict):
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = val

        extras = dict(strategy_base_extras)
        for k, v in (params or {}).items():
            if isinstance(k, str) and "." in k:
                _merge_dot(extras, k, v)
            else:
                extras[k] = v
        strategy_params = {"extras": extras}

    # Ensure symbols present in extras for strategies that require it
    try:
        ex = strategy_params.get("extras", {})
        if not ex.get("symbols"):
            ex_syms = config.get("symbols") or []
            if ex_syms:
                ex["symbols"] = ex_syms
                strategy_params["extras"] = ex
    except Exception:
        pass

    # Determine time window (use OOS window from fold if provided)
    if fold is not None:
        start = fold["oos_start"]
        end = fold["oos_end"]
    else:
        # Fallback to config's time range (if any)
        start = config.get("start", "2025-01-01 09:30:00")
        end = config.get("end", "2025-12-31 16:00:00")

    # Build BacktestConfig
    broker_cfg = config.get("broker", {}) or {}
    bt_cfg_dict = {
        "symbols": config["symbols"],
        "start": start,
        "end": end,
        "timeframe": config.get("timeframe", "1m"),
        "features_table": config.get("features_table"),
        "clickhouse": config.get("clickhouse", {}),
        "initial_cash": broker_cfg.get("initial_cash", 100_000.0),
        "commission_per_share": broker_cfg.get("commission_per_share", 0.005),
        "commission_min": broker_cfg.get("commission_min", 1.0),
        "slippage_bps": broker_cfg.get("slippage_bps", 1.0),
        "fill_ref": broker_cfg.get("fill_ref", "open"),
        "strategy_path": config["strategy_path"],  # Required, no default
        "strategy_params": strategy_params,
    }

    # Handle dataset_df if provided
    df = None
    if "dataset_df" in config and config["dataset_df"] is not None:
        df = pd.DataFrame(config["dataset_df"])
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
        df = df.sort_values(["ts", "symbol"]).reset_index(drop=True)

    # Run backtest
    import sys
    mem_before_bt = sys.getsizeof(result := _run_engine_external(bt_cfg_dict, start, end, strategy_params, df=df)) / 1024 / 1024
    import structlog
    logger = structlog.get_logger()
    logger.info("evaluate_trial_memory", phase="after_backtest", result_size_mb=f"{mem_before_bt:.1f}")

    # Extract base metrics
    metrics = result.get("metrics", {})

    # Compute extended metrics
    obj_cfg = config.get("objective", {}) or {}
    objective = Objective(**obj_cfg)
    extended = _merge_metrics_with_extras(result, objective)

    # Calculate Model Layer metrics (IC/IR/coverage)
    model_layer = _calculate_ic_ir_coverage(result)

    # Memory bounding: Drop heavy result keys to limit memory growth
    trades_count = len(result.get("trades", []))
    equity_count = len(result.get("equity_curve", []))
    try:
        result.pop("trades", None)
        result.pop("equity_curve", None)
        result.pop("final_snapshot", None)
    except Exception:
        pass
    del result
    import gc as _gc
    collected = _gc.collect()
    logger.info("evaluate_trial_memory", phase="after_cleanup", trades_dropped=trades_count,
                equity_points_dropped=equity_count, gc_collected=collected)

    # Build return dict with all metrics
    return {
        # L3 Metrics (Strategy Layer)
        "sharpe": extended.get("sharpe", 0.0),
        "mdd": extended.get("max_drawdown", 0.0),  # Use 'mdd' as key for max_drawdown
        "pnl": extended.get("total_return", 0.0),  # Use 'pnl' as key for total_return
        "trades": extended.get("trades", 0),
        "sortino": extended.get("sortino", 0.0),
        "omega": extended.get("omega", 0.0),
        "calmar": extended.get("calmar", 0.0),
        "win_rate": extended.get("win_rate", 0.0),
        "profit_factor": extended.get("profit_factor", 0.0),

        # L1 Metrics (Model Layer) - NEW
        "ic": model_layer.get("ic", 0.0),
        "ir": model_layer.get("ir", 0.0),
        "coverage": model_layer.get("coverage", 0.0),
    }
