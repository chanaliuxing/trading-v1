"""
ML Model Registry Strategy - 策略层直接使用模型注册表

展示如何在策略中使用统一模型注册表进行在线预测。

适用场景:
1. 在回测中测试不同模型（无需修改wf_hpo.py）
2. 快速切换模型进行A/B测试
3. 策略自主训练和预测（自适应策略）

使用方式:
    config.yml:
    strategies:
      - id: "ml_registry_demo"
        enabled: true
        module: "strategies.ml_model_registry_strategy"
        class: "MLModelRegistryStrategy"
        params:
          symbols: ["AAPL"]
          model_config:
            task: "regression"
            model:
              id: "ridge_cv"
              scaler: "robust"
              params:
                alphas: [0.1, 1.0, 10.0]

Author: Claude AI (Model Registry Integration)
"""

from __future__ import annotations
from typing import Any, Dict, List
import numpy as np
import pandas as pd

from libs.strategy.base import (
    DecisionEnvelope,
    Freshness,
    OrderStyle,
    StrategyAlgorithm,
)
from libs.strategy.registry import register_strategy
from services.strategy.strategy_base import make_decision_id

# >>> REGISTRY-INTEGRATION BEGIN <<<
# 导入模型注册表
try:
    from models.registry import build_model_from_policy
    HAS_MODEL_REGISTRY = True
except ImportError:
    HAS_MODEL_REGISTRY = False
    build_model_from_policy = None
# >>> REGISTRY-INTEGRATION END <<<


@register_strategy("ml_model_registry")
class MLModelRegistryStrategy(StrategyAlgorithm):
    """
    使用模型注册表的策略示例

    特性:
    - 策略内部训练模型（从历史数据）
    - 在线预测（每个bar）
    - JSON配置切换模型（无需修改代码）
    """

    async def initialize(self) -> None:
        """初始化策略和模型"""
        if not HAS_MODEL_REGISTRY:
            raise ImportError("Model registry not available. Install: models/registry.py")

        cfg: Dict[str, Any] = self.params.extras or {}

        # 策略参数
        self.symbols: List[str] = cfg.get("symbols", ["AAPL"])
        self.lookback: int = cfg.get("lookback", 200)  # 训练窗口
        self.retrain_every: int = cfg.get("retrain_every", 50)  # 重训练间隔
        self.prediction_horizon: int = cfg.get("prediction_horizon", 5)  # 预测未来N个bar

        # 订单参数
        self.max_qty: int = cfg.get("max_qty", 100)
        self.threshold: float = cfg.get("threshold", 0.002)  # 信号阈值（2bp）

        # 模型配置（从JSON读取）
        self.model_config: Dict[str, Any] = cfg.get("model_config", {
            "task": "regression",
            "model": {
                "id": "ridge_cv",
                "scaler": "robust",
                "params": {"alphas": [0.1, 1.0, 10.0]}
            }
        })

        # 初始化模型（延迟到第一次训练）
        self.model = None
        self.bar_count = 0
        self.last_train_bar = -1

        # 订阅数据
        await self.ctx.subscribe_features(self.symbols, bar_sec=60)

        self.ctx.logger.info(
            "MLModelRegistryStrategy initialized",
            symbols=self.symbols,
            model_id=self.model_config.get("model", {}).get("id"),
            lookback=self.lookback,
            retrain_every=self.retrain_every
        )

    def _prepare_features(self, history: pd.DataFrame) -> pd.DataFrame:
        """
        从历史数据生成特征

        Args:
            history: 历史bar数据，至少包含 [ts, symbol, open, high, low, close, volume]

        Returns:
            特征DataFrame (每行一个样本)
        """
        df = history.copy()

        # 基础技术指标
        df['ret_1'] = df.groupby('symbol')['close'].pct_change(1)
        df['ret_5'] = df.groupby('symbol')['close'].pct_change(5)
        df['ret_10'] = df.groupby('symbol')['close'].pct_change(10)

        df['vol_5'] = df.groupby('symbol')['ret_1'].rolling(5).std().reset_index(0, drop=True)
        df['vol_10'] = df.groupby('symbol')['ret_1'].rolling(10).std().reset_index(0, drop=True)

        df['volume_ma_20'] = df.groupby('symbol')['volume'].rolling(20).mean().reset_index(0, drop=True)
        df['volume_ratio'] = df['volume'] / (df['volume_ma_20'] + 1e-8)

        # RSI简化版（14周期）
        delta = df.groupby('symbol')['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / (loss + 1e-8)
        df['rsi_14'] = 100 - (100 / (1 + rs))

        # 选择特征列
        feature_cols = ['ret_1', 'ret_5', 'ret_10', 'vol_5', 'vol_10', 'volume_ratio', 'rsi_14']

        # 填充NaN（前向填充 + 零填充）
        df[feature_cols] = df[feature_cols].ffill().fillna(0.0)

        return df[feature_cols]

    def _prepare_labels(self, history: pd.DataFrame) -> pd.Series:
        """
        生成标签（未来收益率）

        Args:
            history: 历史bar数据

        Returns:
            标签Series
        """
        df = history.copy()
        # 预测未来N个bar的收益率
        df['label'] = df.groupby('symbol')['close'].pct_change(self.prediction_horizon).shift(-self.prediction_horizon)
        return df['label']

    def _train_model(self, history: pd.DataFrame) -> None:
        """
        训练模型

        Args:
            history: 历史数据 (至少 lookback 条)
        """
        # 准备训练数据
        X = self._prepare_features(history)
        y = self._prepare_labels(history)

        # 移除NaN
        mask = ~(y.isna() | X.isna().any(axis=1))
        X_train = X[mask]
        y_train = y[mask]

        if len(X_train) < 20:
            self.ctx.logger.warning(
                "Insufficient training samples",
                available=len(X_train),
                required=20
            )
            return

        # 构建模型
        try:
            pipe, sw_ok = build_model_from_policy(
                self.model_config["task"],
                self.model_config
            )

            # 训练
            pipe.fit(X_train.values, y_train.values)

            # 保存模型
            self.model = pipe
            self.last_train_bar = self.bar_count

            # 训练集性能
            y_pred_train = pipe.predict(X_train.values)
            mse = np.mean((y_train.values - y_pred_train) ** 2)

            self.ctx.logger.info(
                "Model trained",
                bar_count=self.bar_count,
                train_samples=len(X_train),
                train_mse=mse,
                model_id=self.model_config.get("model", {}).get("id")
            )

        except Exception as e:
            self.ctx.logger.error(
                "Model training failed",
                error=str(e),
                bar_count=self.bar_count
            )

    async def on_features(self, symbol: str, features: Dict[str, Any]) -> None:
        """
        每个bar触发

        Args:
            symbol: 标的代码
            features: 当前bar特征
        """
        self.bar_count += 1

        # 获取历史数据
        try:
            history = await self.ctx.history(symbol, resolution="1m", limit=self.lookback + self.prediction_horizon)
        except Exception as e:
            self.ctx.logger.error("Failed to fetch history", symbol=symbol, error=str(e))
            return

        if history is None or len(history) < self.lookback:
            self.ctx.logger.debug(
                "Insufficient history",
                symbol=symbol,
                available=len(history) if history is not None else 0,
                required=self.lookback
            )
            return

        # 定期重训练
        should_train = (
            self.model is None or
            (self.bar_count - self.last_train_bar) >= self.retrain_every
        )

        if should_train:
            self._train_model(history)

        # 如果模型未训练好，跳过预测
        if self.model is None:
            return

        # 预测
        try:
            # 准备当前bar特征
            current_features = self._prepare_features(history.tail(1))

            if current_features.isna().any().any():
                self.ctx.logger.warning(
                    "Current features contain NaN",
                    symbol=symbol,
                    bar_count=self.bar_count
                )
                return

            # 预测未来收益率
            pred_return = self.model.predict(current_features.values)[0]

            # 生成信号
            if pred_return > self.threshold:
                target_qty = self.max_qty
            elif pred_return < -self.threshold:
                target_qty = -self.max_qty
            else:
                target_qty = 0.0

            # 提交意图
            if abs(target_qty) > 0:
                envelope = DecisionEnvelope(
                    decision_id=make_decision_id(),
                    strategy_id=self.ctx.strategy_id,
                    symbol=symbol,
                    ts_bar_end_us=self.ctx.now_us(),
                    target_qty=target_qty,
                    order_style=OrderStyle(kind="MKT"),
                    freshness=Freshness(ttl_ms=60000, dlb_ms=500),
                )

                await self.ctx.submit_intent(envelope)

                self.ctx.logger.info(
                    "ML signal generated",
                    symbol=symbol,
                    pred_return=pred_return,
                    target_qty=target_qty,
                    bar_count=self.bar_count,
                    model_id=self.model_config.get("model", {}).get("id")
                )

        except Exception as e:
            self.ctx.logger.error(
                "Prediction failed",
                symbol=symbol,
                error=str(e),
                bar_count=self.bar_count
            )
