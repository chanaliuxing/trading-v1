"""
RL Trading Environment (Gymnasium-compatible)
- State: selected market features (normalized) + current position pct
- Action: continuous target position pct in [-1, 1] (single asset MVP)
- Reward: dPortfolio - cost_penalty - vol_penalty - drawdown_penalty

Requires: gymnasium (preferred) or gym fallback. Safe to import without it (only used by train.py).
"""

from __future__ import annotations
from typing import Optional, Tuple

import numpy as np

try:
    import gymnasium as gym  # type: ignore
except Exception:  # pragma: no cover
    try:
        import gym  # type: ignore
    except Exception:  # pragma: no cover
        gym = None  # type: ignore


class TradingEnv((gym.Env if gym else object)):
    def __init__(
        self,
        historical_data,
        initial_capital: float = 100_000.0,
        feature_cols: Optional[list[str]] = None,
        cost_bps: float = 5.0,  # round-trip cost in bps per unit turnover
        vol_penalty: float = 0.0,  # weight for realized vol penalty
        dd_penalty: float = 0.0,  # drawdown penalty multiplier (0=disabled)
        turnover_penalty: float = 0.0,  # turnover penalty multiplier (0=disabled)
    ) -> None:
        if gym is None:
            raise RuntimeError("gymnasium/gym is required to use TradingEnv. Please install gymnasium.")
        super().__init__()
        import pandas as pd

        if not isinstance(historical_data, pd.DataFrame):
            raise ValueError("historical_data must be a pandas DataFrame")
        if "price_return" not in historical_data.columns:
            raise ValueError("historical_data must include 'price_return' column")

        self.df = historical_data.reset_index(drop=True)
        self.capital0 = float(initial_capital)
        self.cost_bps = float(cost_bps)
        self.vol_penalty = float(vol_penalty)
        self.dd_penalty = float(dd_penalty)
        self.turnover_penalty = float(turnover_penalty)
        self.pos_pct = 0.0
        self.step_idx = 0
        self.high_water = float(initial_capital)  # Track high water mark for drawdown

        # Features
        if feature_cols is None:
            feature_cols = [c for c in self.df.columns if c not in ("price_return", "ts", "symbol")] 
        self.feature_cols = feature_cols

        # Action and observation spaces
        self.action_space = gym.spaces.Box(low=-1.0, high=1.0, shape=(1,), dtype=np.float32)
        self.observation_space = gym.spaces.Box(
            low=-np.inf, high=np.inf, shape=(len(self.feature_cols) + 1,), dtype=np.float32
        )

    def _obs(self) -> np.ndarray:
        feats = self.df.loc[self.step_idx, self.feature_cols].to_numpy(dtype=float, copy=False)
        return np.concatenate([feats, np.array([self.pos_pct], dtype=float)], axis=0).astype(np.float32)

    def reset(self, *, seed: Optional[int] = None, options: Optional[dict] = None) -> Tuple[np.ndarray, dict]:
        super().reset(seed=seed)
        self.pv = self.capital0
        self.pos_pct = 0.0
        self.step_idx = 0
        self.high_water = self.capital0  # Reset high water mark
        return self._obs(), {}

    def step(self, action: np.ndarray):
        target_pct = float(np.clip(action[0], -1.0, 1.0))
        # Turnover cost (bps per abs change)
        turnover = abs(target_pct - self.pos_pct)
        cost = self.pv * turnover * (self.cost_bps / 10_000.0)

        # Price move
        r = float(self.df.loc[self.step_idx, "price_return"])
        pnl = self.pos_pct * r * self.pv - cost
        self.pv += pnl
        self.pos_pct = target_pct
        self.step_idx += 1

        # Reward: PnL - penalties
        reward = pnl

        # Volatility penalty (optional)
        if self.vol_penalty > 0.0:
            # approximate realized vol using abs return
            reward -= self.vol_penalty * abs(r) * self.pv

        # Drawdown penalty (optional)
        if self.dd_penalty > 0.0:
            if self.pv < self.high_water:
                dd = (self.high_water - self.pv) / self.high_water  # drawdown as fraction
                reward -= self.dd_penalty * dd * self.pv  # penalize in dollar terms
            else:
                self.high_water = self.pv  # update high water mark

        # Turnover penalty (optional) - penalize excessive trading
        if self.turnover_penalty > 0.0:
            reward -= self.turnover_penalty * turnover * self.pv

        terminated = self.step_idx >= len(self.df) - 1 or self.pv <= self.capital0 * 0.5
        truncated = False
        return self._obs(), float(reward), bool(terminated), bool(truncated), {}

