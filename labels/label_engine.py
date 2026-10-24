# labels/label_engine.py

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import numpy as np
import pandas as pd


@dataclass
class LabelSpec:
    """
    标签定义规范。

    Attributes:
        name: Label identifier (e.g., "ret_10m", "tb_10m_atr1")
        kind: Label type (forward_return or triple_barrier)
        horizon_min: Forward looking window in minutes
        params: Additional parameters for label computation
    """
    name: str
    kind: Literal["forward_return", "triple_barrier"]
    horizon_min: int
    params: Dict[str, Any]


@dataclass
class LabelSanityConfig:
    """
    标签质量检查配置。

    Attributes:
        max_nan_frac: Maximum allowed fraction of NaN values
        min_variance: Minimum required variance
        check_balance: Whether to check class balance for classification
        min_class_ratio: Minimum ratio for minority class (0.1 = 10%)
    """
    max_nan_frac: float = 0.05
    min_variance: float = 1e-6
    check_balance: bool = False
    min_class_ratio: float = 0.1


class LabelRegistry:
    """
    从 JSON 文件加载标签定义的注册表。

    This class provides centralized management of label definitions,
    similar to the feature registry approach.
    """

    def __init__(self, registry_path: str | Path) -> None:
        """
        Initialize label registry from JSON file.

        Args:
            registry_path: Path to label_registry.json

        Raises:
            FileNotFoundError: If registry file doesn't exist
            ValueError: If registry format is invalid
        """
        p = Path(registry_path)
        if not p.exists():
            raise FileNotFoundError(f"Label registry not found: {p}")

        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)

        self._labels: Dict[str, LabelSpec] = {}

        for name, cfg in data.get("labels", {}).items():
            self._labels[name] = LabelSpec(
                name=name,
                kind=cfg["kind"],
                horizon_min=cfg["horizon_min"],
                params=cfg.get("params", {}),
            )

    def get(self, name: str) -> LabelSpec:
        """
        Get label specification by name.

        Args:
            name: Label identifier

        Returns:
            LabelSpec for the requested label

        Raises:
            KeyError: If label not found in registry
        """
        if name not in self._labels:
            raise KeyError(f"Label {name!r} not found in registry")
        return self._labels[name]

    def list_labels(self) -> List[str]:
        """Return list of all registered label names."""
        return list(self._labels.keys())


class LabelEngine:
    """
    统一的标签计算引擎，支持多种标签类型。

    This class provides standardized label calculation without SQL,
    supporting both simple forward returns and path-dependent triple barriers.

    Design principles:
    - Uses registry for label definitions (no arbitrary column access)
    - Computes labels in-memory on DataFrames
    - Provides quality checks before returning
    - Consistent interface for all label types
    """

    def __init__(self, registry: LabelRegistry) -> None:
        """
        Initialize label engine with registry.

        Args:
            registry: LabelRegistry instance with label definitions
        """
        self._registry = registry

    def attach_label(
        self,
        df: pd.DataFrame,
        label_id: str,
        sanity_config: Optional[LabelSanityConfig] = None,
    ) -> pd.DataFrame:
        """
        在 DataFrame 上附加标签列，并做基础 sanity check。

        Args:
            df: Input DataFrame with OHLCV data
            label_id: Label identifier from registry
            sanity_config: Optional quality check configuration

        Returns:
            DataFrame with new label column added

        Raises:
            KeyError: If label_id not in registry
            ValueError: If data quality checks fail

        Expected DataFrame columns:
            - ts: Timestamp (datetime)
            - open, high, low, close: Price data
            - volume: Volume data (optional)
            - Additional columns as needed for computation
        """
        spec = self._registry.get(label_id)

        # Compute label based on kind
        if spec.kind == "forward_return":
            label_series = self._compute_forward_return(df, spec)
        elif spec.kind == "triple_barrier":
            label_series = self._compute_triple_barrier(df, spec)
        else:
            raise ValueError(f"Unknown label kind: {spec.kind}")

        # Add label column
        result = df.copy()
        result[label_id] = label_series

        # Run sanity checks
        if sanity_config:
            self._run_sanity_checks(result[label_id], label_id, sanity_config)

        return result

    def compute_label_only(
        self,
        df: pd.DataFrame,
        label_id: str,
    ) -> pd.Series:
        """
        仅计算标签，不附加到 DataFrame。

        Args:
            df: Input DataFrame with OHLCV data
            label_id: Label identifier from registry

        Returns:
            Series with label values

        Raises:
            KeyError: If label_id not in registry
        """
        spec = self._registry.get(label_id)

        if spec.kind == "forward_return":
            return self._compute_forward_return(df, spec)
        elif spec.kind == "triple_barrier":
            return self._compute_triple_barrier(df, spec)
        else:
            raise ValueError(f"Unknown label kind: {spec.kind}")

    # ---------- Label computation methods ----------

    def _compute_forward_return(
        self,
        df: pd.DataFrame,
        spec: LabelSpec,
    ) -> pd.Series:
        """
        计算简单的前向收益率标签。

        Formula: (close[t+horizon] - close[t]) / close[t]

        Args:
            df: DataFrame with 'close' column
            spec: Label specification with horizon_min

        Returns:
            Series of forward returns (NaN for last horizon_min rows)
        """
        horizon = spec.horizon_min
        close = df["close"].values

        # Shift close prices backward by horizon
        future_close = np.roll(close, -horizon)

        # Compute return
        returns = (future_close - close) / close

        # Set last horizon rows to NaN (no future data)
        returns[-horizon:] = np.nan

        return pd.Series(returns, index=df.index, name=spec.name)

    def _compute_triple_barrier(
        self,
        df: pd.DataFrame,
        spec: LabelSpec,
    ) -> pd.Series:
        """
        计算 triple barrier 标签（路径依赖）。

        Triple Barrier Method:
        - Upper barrier: profit_pct (e.g., +1% or +1 ATR)
        - Lower barrier: loss_pct (e.g., -1% or -1 ATR)
        - Time barrier: horizon_min

        Label values:
        - 1.0: Upper barrier hit first (profit)
        - -1.0: Lower barrier hit first (loss)
        - 0.0: Time barrier hit (neutral)

        Args:
            df: DataFrame with 'close', 'high', 'low' columns
            spec: Label specification with params:
                - profit_pct: Upper barrier percentage (e.g., 0.01 for 1%)
                - loss_pct: Lower barrier percentage (e.g., 0.01 for 1%)
                - use_atr: If True, use ATR instead of fixed percentage
                - atr_window: ATR calculation window (default 14)

        Returns:
            Series of triple barrier labels (-1, 0, 1)
        """
        horizon = spec.horizon_min
        params = spec.params

        profit_pct = params.get("profit_pct", 0.01)
        loss_pct = params.get("loss_pct", 0.01)
        use_atr = params.get("use_atr", False)
        atr_window = params.get("atr_window", 14)

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values

        labels = np.zeros(len(df))

        # Compute ATR if needed
        if use_atr:
            atr = self._compute_atr(df, window=atr_window)
        else:
            atr = None

        # For each bar, look forward up to horizon
        for i in range(len(df) - horizon):
            entry_price = close[i]

            # Determine barriers
            if use_atr and atr is not None:
                if np.isnan(atr[i]):
                    labels[i] = np.nan
                    continue
                upper = entry_price + profit_pct * atr[i]
                lower = entry_price - loss_pct * atr[i]
            else:
                upper = entry_price * (1 + profit_pct)
                lower = entry_price * (1 - loss_pct)

            # Scan forward window
            label = 0.0  # Default: time barrier
            for j in range(i + 1, min(i + horizon + 1, len(df))):
                # Check upper barrier
                if high[j] >= upper:
                    label = 1.0
                    break
                # Check lower barrier
                if low[j] <= lower:
                    label = -1.0
                    break

            labels[i] = label

        # Last horizon rows are NaN (no future data)
        labels[-horizon:] = np.nan

        return pd.Series(labels, index=df.index, name=spec.name)

    def _compute_atr(self, df: pd.DataFrame, window: int = 14) -> np.ndarray:
        """
        Compute Average True Range for triple barrier sizing.

        Args:
            df: DataFrame with 'high', 'low', 'close' columns
            window: ATR calculation window

        Returns:
            Array of ATR values
        """
        high = df["high"].values
        low = df["low"].values
        close = df["close"].values

        # True Range
        tr1 = high - low
        tr2 = np.abs(high - np.roll(close, 1))
        tr3 = np.abs(low - np.roll(close, 1))

        tr = np.maximum(tr1, np.maximum(tr2, tr3))
        tr[0] = tr1[0]  # First bar has no previous close

        # ATR as rolling mean of TR
        atr = pd.Series(tr).rolling(window=window).mean().values

        return atr

    # ---------- Quality checks ----------

    def _run_sanity_checks(
        self,
        label_series: pd.Series,
        label_id: str,
        config: LabelSanityConfig,
    ) -> None:
        """
        Run quality checks on computed labels.

        Args:
            label_series: Computed label values
            label_id: Label identifier for error messages
            config: Sanity check configuration

        Raises:
            ValueError: If quality checks fail
        """
        # Check NaN fraction
        nan_frac = label_series.isna().sum() / len(label_series)
        if nan_frac > config.max_nan_frac:
            raise ValueError(
                f"Label {label_id} has {nan_frac:.2%} NaN values, "
                f"exceeds limit {config.max_nan_frac:.2%}"
            )

        # Check variance (on non-NaN values)
        valid_values = label_series.dropna()
        if len(valid_values) == 0:
            raise ValueError(f"Label {label_id} has no valid values")

        variance = valid_values.var()
        if variance < config.min_variance:
            raise ValueError(
                f"Label {label_id} has variance {variance:.6f}, "
                f"below minimum {config.min_variance:.6f}"
            )

        # Check class balance for classification labels
        if config.check_balance:
            unique_values = valid_values.unique()
            if len(unique_values) <= 1:
                raise ValueError(
                    f"Label {label_id} has only one unique value: {unique_values}"
                )

            # For binary labels (-1, 0, 1), check minority class ratio
            value_counts = valid_values.value_counts()
            min_count = value_counts.min()
            total_count = len(valid_values)
            min_ratio = min_count / total_count

            if min_ratio < config.min_class_ratio:
                raise ValueError(
                    f"Label {label_id} has minority class ratio {min_ratio:.2%}, "
                    f"below minimum {config.min_class_ratio:.2%}"
                )
