# Phase 4B 实施计划：从 HPO 到 Live Trading 的完整闭环

> **状态**: Phase 4A 已完成（数据结构 + 测试），Phase 4B 待实施
> **目标**: 将 4A 的"纸面架子"接入真实的 HPO → 回测 → Live Orchestrator 流程

---

## 一、当前状态盘点

### ✅ Phase 4A 已完成的基础设施

```
experiments/
├── types.py              # Metrics, ExperimentRecord
├── registry.py           # append_experiment_record(), load_experiment_records()
└── summary.py            # build_experiment_summary_for_llm()

strategies/
└── runtime.py            # StrategyManifest, load_strategy_manifest()

brain/
├── decision_schema.py    # BrainDecision, validate_brain_decision()
├── strategy_selector.py  # StrategySelector (AI 大脑)
└── context_builder.py    # build_brain_prompts() with allowed_run_ids

tests/unit/
├── experiments/          # 38 tests passing
└── brain/                # 35 tests passing
```

**核心能力**:
- ✅ ExperimentRecord 可以写入 JSONL index
- ✅ StrategyManifest 可以存储模型元数据
- ✅ BrainDecision 有完整的验证逻辑
- ✅ 大脑可以从历史实验中选择 run_id

### ⚠️ Phase 4B 缺失的连接点

1. **HPO 结束时不会写 ExperimentRecord/StrategyManifest**
2. **没有模型 loader（不能根据 manifest 真正加载模型）**
3. **Live orchestrator 不知道如何使用 AI 大脑选出的 run_id**

---

## 二、4B 实施路线图（分三步走）

### Step 1: 离线闭环 - HPO → ExperimentRecord
**目标**: HPO 完成后自动生成并写入实验记录
**不涉及**: Live trading, AI 大脑

### Step 2: 模型运行时 - LoadedStrategy
**目标**: 根据 StrategyManifest 加载模型并预测
**不涉及**: Live trading

### Step 3: Live 接入 - Orchestrator + Brain
**目标**: 开盘前 AI 大脑选策略，盘中执行
**涉及**: Paper trading 测试

---

## 三、Step 1 详细实施：HPO → ExperimentRecord

### 3.1 目标

在 HPO/backtest 结束时：
1. 生成唯一的 `run_id`
2. 保存模型到 `models/{symbol}/{run_id}/model.pkl`
3. 写 `StrategyManifest` 到 `models/{symbol}/{run_id}/manifest.json`
4. 写 `ExperimentRecord` 到 `experiments/experiment_index.jsonl`

### 3.2 需要修改的文件

**新增文件**:
```python
# experiments/writer.py
"""
HPO 完成后写入 ExperimentRecord 的统一接口
"""
```

**需要修改的现有文件**:
- `tools/nightly_hpo.py` - 在 HPO 完成处调用 writer
- `hpo_cli_upgraded.py` - 同上
- 或任何你实际使用的 HPO 入口

### 3.3 实现代码

#### 3.3.1 新增 `experiments/writer.py`

```python
# experiments/writer.py

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import joblib

from experiments.types import ExperimentRecord, Metrics
from experiments.registry import append_experiment_record
from strategies.runtime import StrategyManifest


def make_run_id(
    symbol: str,
    horizon: str,
    label_id: str,
    model_type: str,
    timestamp: Optional[str] = None,
) -> str:
    """
    生成唯一的 run_id。
    格式: {symbol}_{YYYYMMDD}_{horizon}_{model_type}_{hash6}
    """
    if timestamp is None:
        timestamp = datetime.utcnow().strftime("%Y%m%d")
    else:
        # 如果传入 ISO 格式，提取日期部分
        timestamp = timestamp[:10].replace("-", "")

    # 用 label_id + 当前时间生成短 hash
    hash_input = f"{label_id}_{datetime.utcnow().isoformat()}"
    short_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:6]

    return f"{symbol}_{timestamp}_{horizon}_{model_type}_{short_hash}"


def register_hpo_result(
    symbol: str,
    freq: str,
    horizon: str,
    label_id: str,
    model_type: str,
    model_obj: Any,
    feature_subset: List[str],
    params: Dict[str, Any],
    metrics: Dict[str, float],
    *,
    models_root: Path = Path("models"),
    data_start_ts: Optional[str] = None,
    data_end_ts: Optional[str] = None,
    tags: Optional[List[str]] = None,
    extra_meta: Optional[Dict[str, Any]] = None,
) -> ExperimentRecord:
    """
    HPO/backtest 完成后，统一注册实验结果。

    核心流程:
    1. 生成 run_id
    2. 保存模型到 models/{symbol}/{run_id}/model.pkl
    3. 写 manifest.json
    4. 写 ExperimentRecord 到 JSONL index
    5. 返回 ExperimentRecord 供后续使用

    参数:
        symbol: 交易品种
        freq: bar 频率 (如 "1m", "5s")
        horizon: 预测时间窗口 (如 "10m")
        label_id: 标签 ID (如 "ret_10m_fwd")
        model_type: 模型类型 (如 "xgb", "lgbm", "ridge")
        model_obj: 训练好的模型对象 (可以被 joblib.dump)
        feature_subset: 特征列表
        params: 模型超参数
        metrics: 回测指标字典 (sharpe, ic, etc)
        models_root: 模型存储根目录
        data_start_ts: 训练数据起始时间
        data_end_ts: 训练数据结束时间
        tags: 可选标签列表
        extra_meta: 可选的额外元数据

    返回:
        ExperimentRecord: 写入 index 的记录
    """
    # 1. 生成 run_id
    run_id = make_run_id(symbol, horizon, label_id, model_type)
    run_ts = datetime.utcnow().isoformat() + "Z"

    # 2. 保存模型
    model_dir = models_root / symbol / run_id
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / "model.pkl"
    joblib.dump(model_obj, model_path)

    # 3. 构造 StrategyManifest
    manifest = StrategyManifest(
        run_id=run_id,
        symbol=symbol,
        freq=freq,
        horizon=horizon,
        label_id=label_id,
        model_type=model_type,
        model_path=str(model_path.absolute()),
        feature_subset=feature_subset,
        params=params,
        train_window_start=data_start_ts,
        train_window_end=data_end_ts,
        metrics=metrics,
    )

    # 4. 写 manifest.json
    manifest_path = model_dir / "manifest.json"
    manifest_dict = {
        "run_id": manifest.run_id,
        "symbol": manifest.symbol,
        "freq": manifest.freq,
        "horizon": manifest.horizon,
        "label_id": manifest.label_id,
        "model_type": manifest.model_type,
        "model_path": manifest.model_path,
        "feature_subset": manifest.feature_subset,
        "params": manifest.params,
        "train_window": {
            "start_ts": manifest.train_window_start,
            "end_ts": manifest.train_window_end,
        },
        "metrics": manifest.metrics,
    }
    with manifest_path.open("w", encoding="utf-8") as f:
        json.dump(manifest_dict, f, ensure_ascii=False, indent=2)

    # 5. 构造 ExperimentRecord
    metrics_obj = Metrics(
        sharpe=metrics.get("sharpe"),
        sortino=metrics.get("sortino"),
        ic=metrics.get("ic"),
        hit_rate=metrics.get("hit_rate"),
        max_drawdown=metrics.get("max_drawdown"),
        pnl=metrics.get("pnl"),
        pnl_annualized=metrics.get("pnl_annualized"),
        trades=metrics.get("trades"),
        avg_trade_pnl=metrics.get("avg_trade_pnl"),
    )

    record = ExperimentRecord(
        run_id=run_id,
        symbol=symbol,
        freq=freq,
        horizon=horizon,
        label_id=label_id,
        model_type=model_type,
        feature_subset=feature_subset,
        params=params,
        metrics=metrics_obj,
        run_ts=run_ts,
        data_start_ts=data_start_ts,
        data_end_ts=data_end_ts,
        manifest_path=str(manifest_path.absolute()),
        tags=tags or [],
    )

    # 6. 写入 JSONL index
    append_experiment_record(record)

    # 7. (可选) 写额外元数据到单独文件
    if extra_meta:
        meta_path = model_dir / "meta.json"
        with meta_path.open("w", encoding="utf-8") as f:
            json.dump(extra_meta, f, ensure_ascii=False, indent=2)

    return record
```

#### 3.3.2 修改 HPO 入口 (示例：`tools/nightly_hpo.py`)

```python
# tools/nightly_hpo.py (在文件末尾添加)

from experiments.writer import register_hpo_result

def run_hpo_for_symbol(symbol: str, ...):
    # ... 现有的 HPO 逻辑 ...

    # HPO 完成后，假设你有:
    # - best_model: 训练好的模型对象
    # - best_params: Dict[str, Any]
    # - backtest_metrics: Dict[str, float]  # {"sharpe": 2.5, "ic": 0.15, ...}
    # - feature_names: List[str]
    # - label_id, horizon, freq, etc.

    # 新增：注册实验结果
    exp_record = register_hpo_result(
        symbol=symbol,
        freq=config["freq"],
        horizon=config["horizon"],
        label_id=config["label_id"],
        model_type=best_params.get("model_type", "xgb"),
        model_obj=best_model,
        feature_subset=feature_names,
        params=best_params,
        metrics=backtest_metrics,
        data_start_ts=train_window_start,
        data_end_ts=train_window_end,
        tags=["nightly_hpo", config.get("strategy_type", "default")],
        extra_meta={
            "hpo_config": config,
            "n_trials": n_trials,
            "best_trial_id": best_trial.number,
        },
    )

    logger.info(
        "Registered experiment",
        run_id=exp_record.run_id,
        sharpe=exp_record.metrics.sharpe,
        manifest_path=exp_record.manifest_path,
    )

    return exp_record
```

### 3.4 验证步骤

#### 3.4.1 编写测试脚本 `tools/test_hpo_writer.py`

```python
# tools/test_hpo_writer.py

import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from experiments.writer import register_hpo_result

# 1. 模拟一个训练好的模型
X_train = pd.DataFrame({"close": [1, 2, 3], "volume": [100, 200, 300]})
y_train = pd.Series([0, 1, 0])
model = RandomForestClassifier(n_estimators=10, random_state=42)
model.fit(X_train, y_train)

# 2. 模拟回测指标
metrics = {
    "sharpe": 2.3,
    "ic": 0.12,
    "hit_rate": 0.58,
    "trades": 150,
}

# 3. 注册实验
record = register_hpo_result(
    symbol="TEST",
    freq="1m",
    horizon="10m",
    label_id="ret_10m_fwd",
    model_type="rf",
    model_obj=model,
    feature_subset=["close", "volume"],
    params={"n_estimators": 10},
    metrics=metrics,
    tags=["test"],
)

print(f"✅ Registered: {record.run_id}")
print(f"📁 Manifest: {record.manifest_path}")
print(f"📊 Sharpe: {record.metrics.sharpe}")

# 4. 验证文件存在
from pathlib import Path
model_path = Path(record.manifest_path).parent / "model.pkl"
assert model_path.exists(), "Model file not saved!"
assert Path(record.manifest_path).exists(), "Manifest not saved!"
print("✅ All files saved successfully!")
```

运行测试:
```bash
python tools/test_hpo_writer.py
```

#### 3.4.2 检查生成的文件

```bash
# 查看 models 目录结构
ls -lR models/TEST/

# 查看 manifest.json
cat models/TEST/TEST_20250318_10m_rf_abc123/manifest.json

# 查看 JSONL index (最后一行应该是刚才的记录)
tail -1 experiments/experiment_index.jsonl | jq .
```

---

## 四、Step 2 详细实施：模型运行时 LoadedStrategy

### 4.1 目标

给定一个 `StrategyManifest`，能够：
1. 加载模型对象
2. 用统一接口调用 `.predict(df)` 预测
3. 支持多种模型类型 (XGBoost, LightGBM, sklearn, etc)

### 4.2 实现代码

#### 4.2.1 扩展 `strategies/runtime.py`

```python
# strategies/runtime.py (在文件末尾添加)

import joblib
import pandas as pd
import numpy as np
from typing import Any, Protocol

# 可选：如果没装这些库，导入时会失败，可以用 try-except 包裹
try:
    import xgboost as xgb
    HAS_XGB = True
except ImportError:
    HAS_XGB = False

try:
    import lightgbm as lgb
    HAS_LGB = True
except ImportError:
    HAS_LGB = False


class ModelProtocol(Protocol):
    """模型运行时的统一接口"""
    def predict(self, X: pd.DataFrame) -> np.ndarray:
        ...


class LoadedStrategy:
    """
    加载后的策略运行时。
    负责:
    - 管理模型对象
    - 特征列对齐
    - 统一预测接口
    """

    def __init__(self, manifest: StrategyManifest, model: Any):
        self.manifest = manifest
        self.model = model

    def predict(self, df: pd.DataFrame) -> np.ndarray:
        """
        统一的预测接口。

        参数:
            df: 包含所有特征的 DataFrame

        返回:
            np.ndarray: 预测值 (shape: (n_samples,))
        """
        # 1. 特征对齐：按 manifest.feature_subset 取列
        if not all(feat in df.columns for feat in self.manifest.feature_subset):
            missing = [f for f in self.manifest.feature_subset if f not in df.columns]
            raise ValueError(f"Missing features: {missing}")

        X = df[self.manifest.feature_subset].copy()

        # 2. 类型转换（避免警告）
        X = X.astype("float32")

        # 3. 按模型类型分派预测
        model_type = self.manifest.model_type.lower()

        if model_type in ("xgb", "xgboost"):
            if not HAS_XGB:
                raise RuntimeError("XGBoost not installed")
            dmatrix = xgb.DMatrix(X)
            return self.model.predict(dmatrix)

        elif model_type in ("lgb", "lightgbm", "lgbm"):
            if not HAS_LGB:
                raise RuntimeError("LightGBM not installed")
            return self.model.predict(X)

        elif model_type in ("rf", "ridge", "lasso", "lr"):
            # sklearn 系列
            if hasattr(self.model, "predict_proba"):
                # 分类模型，返回正类概率
                proba = self.model.predict_proba(X)
                return proba[:, 1] if proba.shape[1] == 2 else proba[:, 0]
            else:
                # 回归模型
                return self.model.predict(X)

        else:
            # fallback: 直接调用 predict
            if hasattr(self.model, "predict"):
                return self.model.predict(X)
            raise TypeError(f"Unsupported model_type: {model_type}")

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    @property
    def symbol(self) -> str:
        return self.manifest.symbol


def load_strategy(manifest: StrategyManifest) -> LoadedStrategy:
    """
    根据 StrategyManifest 加载模型。

    参数:
        manifest: 策略清单

    返回:
        LoadedStrategy: 可以直接调用 .predict() 的运行时对象
    """
    model_path = Path(manifest.model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"Model file not found: {model_path}")

    model = joblib.load(model_path)
    return LoadedStrategy(manifest=manifest, model=model)


def load_strategy_by_run_id(
    run_id: str,
    models_root: Path = Path("models"),
) -> LoadedStrategy:
    """
    根据 run_id 查找并加载策略。

    假设目录结构: models/{symbol}/{run_id}/manifest.json
    """
    # 遍历查找 (简单实现，生产环境可以用 index 加速)
    for symbol_dir in models_root.iterdir():
        if not symbol_dir.is_dir():
            continue
        run_dir = symbol_dir / run_id
        if run_dir.exists():
            manifest_path = run_dir / "manifest.json"
            if manifest_path.exists():
                manifest = load_strategy_manifest(manifest_path)
                return load_strategy(manifest)

    raise FileNotFoundError(f"Strategy not found for run_id: {run_id}")
```

### 4.3 验证步骤

#### 4.3.1 编写测试脚本 `tools/test_strategy_runtime.py`

```python
# tools/test_strategy_runtime.py

import pandas as pd
from strategies.runtime import load_strategy_by_run_id

# 假设上一步生成了 run_id
run_id = "TEST_20250318_10m_rf_abc123"  # 替换成实际的

# 1. 加载策略
strategy = load_strategy_by_run_id(run_id)
print(f"✅ Loaded: {strategy.run_id}")
print(f"📊 Features: {strategy.manifest.feature_subset}")

# 2. 准备测试数据
test_df = pd.DataFrame({
    "close": [100.5, 101.2, 99.8],
    "volume": [1000, 1500, 900],
    "other_col": [1, 2, 3],  # 不在 feature_subset 中，会被忽略
})

# 3. 预测
predictions = strategy.predict(test_df)
print(f"✅ Predictions: {predictions}")
print(f"📈 Shape: {predictions.shape}")

assert predictions.shape[0] == 3, "Prediction count mismatch!"
print("✅ Runtime test passed!")
```

运行测试:
```bash
python tools/test_strategy_runtime.py
```

---

## 五、Step 3 详细实施：Live Orchestrator + AI Brain

### 5.1 目标

**开盘前 (Premarket)**:
- AI 大脑从历史实验中选择 1-3 个 run_id
- Orchestrator 加载对应的 LoadedStrategy

**盘中 (Intraday)**:
- 每次 bar 更新时，调用所有 active strategies 预测
- 聚合信号（加权平均 / 投票）
- 通过 risk gates 后下单

### 5.2 需要修改的文件

**新增文件**:
```python
# scripts/ai_trader/strategy_manager.py
"""
管理 active strategies 的生命周期
"""
```

**需要修改的现有文件**:
- `scripts/ai_trader/brain/brain.py` - 接入 StrategySelector
- `scripts/ai_trader/control_event_consumer.py` - 启动时调用大脑选策略
- 或你实际的 orchestrator 入口

### 5.3 实现代码

#### 5.3.1 新增 `scripts/ai_trader/strategy_manager.py`

```python
# scripts/ai_trader/strategy_manager.py

from __future__ import annotations

from typing import Dict, List, Optional
from dataclasses import dataclass

import pandas as pd
import numpy as np
import structlog

from strategies.runtime import LoadedStrategy, load_strategy_by_run_id
from brain.decision_schema import BrainDecision

logger = structlog.get_logger()


@dataclass
class StrategyWeight:
    """单个策略及其权重"""
    strategy: LoadedStrategy
    weight: float


class StrategyManager:
    """
    管理当前 session 中 active 的策略。

    职责:
    - 根据 BrainDecision 加载/卸载策略
    - 聚合多个策略的预测
    - 提供统一的 .predict() 接口给 orchestrator
    """

    def __init__(self):
        self.active_strategies: Dict[str, List[StrategyWeight]] = {}
        # symbol -> [StrategyWeight, ...]

    def apply_brain_decision(self, decision: BrainDecision, symbol: str) -> None:
        """
        根据 AI 大脑的决策，更新 active strategies。

        参数:
            decision: 大脑输出的决策
            symbol: 交易品种
        """
        if decision.decision == "HOLD" or not decision.chosen_run_id:
            # HOLD: 清空 active strategies (可选)
            # 或者保持当前策略不变，取决于你的策略
            logger.info(
                "Brain decision is HOLD, keeping current strategies",
                symbol=symbol,
                notes=decision.notes,
            )
            return

        # 当前简化版：只支持单个 run_id
        # Phase 4B-advanced 可以支持多个 run_id + 权重
        run_id = decision.chosen_run_id

        try:
            strategy = load_strategy_by_run_id(run_id)
            self.active_strategies[symbol] = [
                StrategyWeight(strategy=strategy, weight=1.0)
            ]
            logger.info(
                "Loaded strategy",
                symbol=symbol,
                run_id=run_id,
                model_type=strategy.manifest.model_type,
                features=len(strategy.manifest.feature_subset),
            )
        except Exception as e:
            logger.error(
                "Failed to load strategy",
                symbol=symbol,
                run_id=run_id,
                error=str(e),
                exc_info=True,
            )
            # 加载失败 → 降级为无策略
            self.active_strategies[symbol] = []

    def predict(self, symbol: str, features: pd.DataFrame) -> Optional[float]:
        """
        对单个 symbol 进行预测。

        参数:
            symbol: 交易品种
            features: 单行 DataFrame，包含所有特征

        返回:
            float: 聚合后的预测分数 (None 表示无策略)
        """
        strategies = self.active_strategies.get(symbol, [])
        if not strategies:
            return None

        # 收集所有策略的预测
        weighted_preds = []
        for sw in strategies:
            try:
                preds = sw.strategy.predict(features)
                score = float(preds[0]) if len(preds) > 0 else 0.0
                weighted_preds.append((score, sw.weight))
            except Exception as e:
                logger.error(
                    "Strategy prediction failed",
                    symbol=symbol,
                    run_id=sw.strategy.run_id,
                    error=str(e),
                    exc_info=True,
                )
                continue

        if not weighted_preds:
            return None

        # 加权平均
        total_weight = sum(w for _, w in weighted_preds)
        if total_weight == 0:
            return None

        aggregated = sum(score * w for score, w in weighted_preds) / total_weight
        return aggregated

    def get_active_run_ids(self, symbol: str) -> List[str]:
        """获取当前 symbol 的 active run_ids"""
        strategies = self.active_strategies.get(symbol, [])
        return [sw.strategy.run_id for sw in strategies]
```

#### 5.3.2 修改 `scripts/ai_trader/brain/brain.py`

```python
# scripts/ai_trader/brain/brain.py (在适当位置添加)

from brain.strategy_selector import StrategySelector
from brain.prompt_library import load_prompt_library
from features.registry import FeatureRegistry
from llm.llm_client import LLMClient

class AIBrain:
    def __init__(self, ...):
        # ... 现有初始化 ...

        # 新增：策略选择器
        self.prompt_lib = load_prompt_library("prompts/prompt_library.json")
        self.feature_registry = FeatureRegistry.from_json("feature_programs.json")
        self.selector = StrategySelector(
            llm=LLMClient(...),
            prompt_lib=self.prompt_lib,
            prompt_id="v3_strategy_selector",  # 需要在 prompt_library 中添加
        )

    def select_strategy_for_symbol(
        self,
        symbol: str,
        now_ts: str,
        freq: str,
        horizon: str,
        label_id: str,
        market_snapshot: Dict[str, Any],
        risk_gates: Dict[str, Any],
    ) -> BrainDecision:
        """
        开盘前调用：为单个 symbol 选择策略。

        返回:
            BrainDecision: 包含 chosen_run_id
        """
        # 构造 dataset_sanity (简化版，可以从数据库查)
        dataset_sanity = {
            "status": "ok",
            "bars": 50000,  # 示例
        }

        decision = self.selector.select(
            symbol=symbol,
            now_ts=now_ts,
            freq=freq,
            horizon=horizon,
            label_id=label_id,
            market_snapshot=market_snapshot,
            risk_gates=risk_gates,
            dataset_sanity=dataset_sanity,
            registry=self.feature_registry,
        )

        return decision
```

#### 5.3.3 修改 orchestrator 入口

```python
# scripts/ai_trader/control_event_consumer.py (伪代码)

from scripts.ai_trader.strategy_manager import StrategyManager
from scripts.ai_trader.brain.brain import AIBrain

class Orchestrator:
    def __init__(self, ...):
        # ... 现有初始化 ...
        self.strategy_manager = StrategyManager()
        self.brain = AIBrain(...)

    async def on_market_open(self):
        """
        开盘前：让 AI 大脑为每个 symbol 选择策略
        """
        for symbol in self.config["symbols"]:
            market_snapshot = await self._get_market_snapshot(symbol)
            risk_gates = self.config["risk_gates"]

            decision = self.brain.select_strategy_for_symbol(
                symbol=symbol,
                now_ts=datetime.utcnow().isoformat(),
                freq=self.config["freq"],
                horizon=self.config["horizon"],
                label_id=self.config["label_id"],
                market_snapshot=market_snapshot,
                risk_gates=risk_gates,
            )

            self.strategy_manager.apply_brain_decision(decision, symbol)

            logger.info(
                "Applied brain decision",
                symbol=symbol,
                decision=decision.decision,
                run_id=decision.chosen_run_id,
                active_strategies=self.strategy_manager.get_active_run_ids(symbol),
            )

    async def on_bar(self, symbol: str, bar: Bar, features: pd.Series):
        """
        盘中：使用 active strategies 预测并决策
        """
        # 1. 预测
        df_features = pd.DataFrame([features])
        prediction = self.strategy_manager.predict(symbol, df_features)

        if prediction is None:
            # 无 active 策略，不交易
            return

        # 2. 应用 risk gates
        if not self._check_risk_gates(symbol, bar, prediction):
            logger.debug("Risk gate blocked", symbol=symbol, prediction=prediction)
            return

        # 3. 生成订单
        signal = self._convert_prediction_to_signal(prediction)
        if signal["action"] != "HOLD":
            await self._execute_order(symbol, signal)
```

### 5.4 验证步骤

#### 5.4.1 Dry-run 测试 (不下单)

```python
# tools/test_orchestrator_dry_run.py

import pandas as pd
from datetime import datetime
from scripts.ai_trader.strategy_manager import StrategyManager
from brain.decision_schema import BrainDecision

# 1. 初始化
manager = StrategyManager()

# 2. 模拟大脑决策
decision = BrainDecision(
    decision="ENTER",
    chosen_run_id="TEST_20250318_10m_rf_abc123",
    side="LONG",
    size_frac=0.5,
    notes="Test brain decision",
)

# 3. 应用决策
manager.apply_brain_decision(decision, symbol="TEST")

# 4. 模拟预测
test_features = pd.DataFrame({
    "close": [100.5],
    "volume": [1000],
})

prediction = manager.predict("TEST", test_features)
print(f"✅ Prediction: {prediction}")
print(f"📊 Active strategies: {manager.get_active_run_ids('TEST')}")
```

#### 5.4.2 Paper Trading 测试

```bash
# 修改 config.yml，启用 paper trading
trading:
  mode: paper  # or "simulation"
  symbols: ["NVDA"]  # 先测一个
  max_position_size: 100  # 小仓位

# 启动
python scripts/ai_trader/control_event_consumer.py --config config.yml
```

**监控指标**:
- 策略加载成功率 (Prometheus: `strategies_loaded_total`)
- 预测调用延迟 (P99 < 100ms)
- 大脑选策略准确率 (手动 review 日志)

---

## 六、推荐实施顺序总结

### Week 1: Step 1 (离线闭环)
- [ ] 实现 `experiments/writer.py`
- [ ] 修改 1-2 个 HPO 入口调用 `register_hpo_result()`
- [ ] 编写 `tools/test_hpo_writer.py` 验证
- [ ] 跑一次真实 HPO，检查生成的文件

### Week 2: Step 2 (模型运行时)
- [ ] 扩展 `strategies/runtime.py` 添加 `LoadedStrategy`
- [ ] 编写 `tools/test_strategy_runtime.py` 验证
- [ ] 用历史 bars 测试预测延迟 (确保 < 100ms)

### Week 3: Step 3 (Live 接入)
- [ ] 实现 `scripts/ai_trader/strategy_manager.py`
- [ ] 修改 orchestrator 接入 `StrategyManager`
- [ ] 在 prompt_library 中添加策略选择 prompt
- [ ] Dry-run 测试 (不下单)
- [ ] Paper trading 测试 (1-2 天)
- [ ] 小仓位实盘测试 (1 周)

---

## 七、风险控制与回滚

### 风险点
1. **模型加载失败** → 自动降级为 HOLD
2. **预测延迟过高** → 设置 timeout (200ms)
3. **大脑选错策略** → 人工 review 前 N 天的选择日志
4. **实盘亏损** → 设置 max daily loss (-2%)，触发后停止交易

### 回滚计划
- 保留旧的"规则策略"作为 fallback
- 增加 `--disable-ai-brain` flag，快速切回规则模式
- 每天盘后对比 AI 选择 vs 规则策略的 PnL

---

## 八、附录：配置示例

### `prompts/prompt_library.json` (新增 prompt)

```json
{
  "v3_strategy_selector": "You are an AI trading brain. Given historical experiment performance and current market conditions, select the best strategy (run_id) for today's trading session. Consider: 1) Recent Sharpe ratio, 2) IC stability, 3) Market regime match, 4) Drawdown tolerance. Output a JSON decision.",

  "_meta_v3_strategy_selector": {
    "version": "1.0",
    "created": "2025-03-18",
    "usage": "Premarket strategy selection"
  }
}
```

### `config.yml` (新增配置)

```yaml
brain:
  enabled: true
  prompt_id: "v3_strategy_selector"
  selection_time: "09:15"  # EST, 开盘前 15 分钟

  # 策略选择约束
  constraints:
    min_sharpe: 1.5
    min_trades: 50
    lookback_days: 30  # 只考虑最近 30 天的实验

strategies:
  models_root: "models"
  max_active_per_symbol: 1  # Phase 4B 简化版：每个 symbol 最多 1 个策略

  # 预测超时
  prediction_timeout_ms: 200
```

---

**文档版本**: v1.0
**最后更新**: 2025-03-18
**作者**: AI Trading Team
**状态**: 🟡 Draft (待 review)
