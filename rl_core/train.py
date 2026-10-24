#!/usr/bin/env python3
"""
Train RL position manager (SAC) in a Gym environment using historical features from ClickHouse.

Usage:
  python rl_core/train.py \
    --symbols TSLA,AAPL,NVDA \
    --start 2025-07-01 09:30:00 \
    --end 2025-08-31 16:00:00 \
    --features-table trading.features_1m \
    --timesteps 200000 \
    --out models/rl/sac_position_manager_latest

Notes:
  - Requires: gymnasium, stable-baselines3, clickhouse-driver, pandas, numpy
  - This script is best-effort; if deps missing, it prints guidance and exits.
"""

from __future__ import annotations
import argparse
import os
from pathlib import Path
from typing import List


def _load_ch(symbols: List[str], start: str, end: str, table: str, host: str, port: int, db: str, user: str = "default", password: str = ""):
    import pandas as pd
    from clickhouse_driver import Client
    syms = ",".join([f"'{s}'" for s in symbols])
    sql = f"""
    SELECT ts, symbol, close, volume,
           atr_14, z_price_20, slope_20, vol_spike,
           ret_1, ret_5, spread_bps, spy_ret_1,
           lagInFrame(close) OVER (PARTITION BY symbol ORDER BY ts) AS prev_close
    FROM {table}
    WHERE symbol IN ({syms})
      AND ts >= toDateTime('{start}')
      AND ts <= toDateTime('{end}')
    ORDER BY ts, symbol
    """
    cli = Client(host=host, port=port, database=db, user=user, password=password)
    rows = cli.execute(sql)
    df = pd.DataFrame(rows, columns=[
        "ts", "symbol", "close", "volume",
        "atr_14", "z_price_20", "slope_20", "vol_spike",
        "ret_1", "ret_5", "spread_bps", "spy_ret_1",
        "prev_close"
    ])
    df["price_return"] = (df["close"] / df["prev_close"] - 1.0).fillna(0.0)
    # Simple forward fill then backward fill for technical features, finally fill remaining with 0
    for c in ("atr_14", "z_price_20", "slope_20", "vol_spike", "volume", "prev_close",
              "ret_1", "ret_5", "spread_bps", "spy_ret_1"):
        if c in df.columns:
            df[c] = df[c].ffill().bfill().fillna(0.0)
    # Additional cleanup - replace any infinite values with 0
    df = df.replace([float('inf'), float('-inf')], 0.0)
    return df


def train_rl_model(
    *,
    symbols: List[str],
    start: str,
    end: str,
    features_table: str = "trading.features_1m",
    timesteps: int = 200_000,
    cost_bps: float = 5.0,
    dd_penalty: float = 0.0,
    turnover_penalty: float = 0.0,
    output_path: str = "models/rl/sac_position_manager_latest.zip",
) -> str:
    """
    Import-safe training entrypoint for tests and other modules.

    Returns the saved model path on success.
    Raises RuntimeError when dependencies/data are unavailable.
    """
    try:
        from stable_baselines3 import SAC  # type: ignore
    except Exception as e:
        raise RuntimeError("stable-baselines3 not installed") from e
    try:
        import gymnasium  # noqa: F401
    except Exception as e:
        raise RuntimeError("gymnasium not installed") from e
    try:
        from rl_core.trading_env import TradingEnv
    except Exception as e:
        raise RuntimeError(f"failed to import TradingEnv: {e}") from e

    host = os.getenv("CH_HOST", "localhost")
    port = int(os.getenv("CH_PORT", 9000))
    db = os.getenv("CH_DB", "trading")
    user = os.getenv("CH_USER", "default")
    password = os.getenv("CH_PASSWORD", "")

    df = _load_ch(symbols, start, end, features_table, host, port, db, user, password)
    if df.empty:
        raise RuntimeError("no training rows loaded from ClickHouse")

    out = Path(output_path)
    model_out = out if out.suffix == ".zip" else out.with_suffix(".zip")
    model_out.parent.mkdir(parents=True, exist_ok=True)

    trained = False
    for sym in symbols:
        sub = df[df.symbol == sym].copy()
        if len(sub) < 100:
            continue
        feat_cols = [c for c in sub.columns if c not in ("ts", "symbol", "price_return")]
        env = TradingEnv(
            sub[[*feat_cols, "price_return"]],
            feature_cols=feat_cols,
            cost_bps=cost_bps,
            dd_penalty=dd_penalty,
            turnover_penalty=turnover_penalty,
        )
        agent = SAC("MlpPolicy", env, verbose=0)
        agent.learn(total_timesteps=max(100, int(timesteps)))
        agent.save(str(model_out.with_suffix("")))
        trained = True
        break

    if not trained or not model_out.exists():
        raise RuntimeError("failed to train/save RL model (insufficient rows or env issue)")
    return str(model_out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="TSLA")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--features-table", default="trading.features_1m")
    ap.add_argument("--timesteps", type=int, default=200_000)
    ap.add_argument("--resume", action="store_true", help="Resume training from existing model if present")
    ap.add_argument("--checkpoint-every", type=int, default=50_000, help="Save checkpoint every N timesteps per symbol")
    ap.add_argument("--out", default="models/rl/sac_position_manager_latest")
    ap.add_argument("--cost-bps", type=float, default=5.0, help="Trading cost in basis points")
    ap.add_argument("--vol-penalty", type=float, default=0.0, help="Volatility penalty weight")
    ap.add_argument("--dd-penalty", type=float, default=0.0, help="Drawdown penalty weight")
    ap.add_argument("--turnover-penalty", type=float, default=0.0, help="Turnover penalty weight")
    args = ap.parse_args()

    # Deps check
    try:
        from stable_baselines3 import SAC  # type: ignore
        from stable_baselines3.common.callbacks import EvalCallback
    except Exception:
        print("✗ stable-baselines3 not installed. pip install 'stable-baselines3[extra]'\nSkipping RL training.")
        return
    try:
        import gymnasium as gym  # type: ignore
    except Exception:
        print("✗ gymnasium not installed. pip install gymnasium\nSkipping RL training.")
        return
    try:
        import pandas as pd  # noqa: F401
    except Exception:
        print("✗ pandas is required. pip install pandas\nSkipping RL training.")
        return

    # ClickHouse env (use native protocol port 9000, not HTTP port 8123)
    host = os.getenv("CH_HOST", "localhost")
    port = int(os.getenv("CH_PORT", 9000))
    db = os.getenv("CH_DB", "trading")
    user = os.getenv("CH_USER", "default")
    password = os.getenv("CH_PASSWORD", "")

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    df = _load_ch(syms, args.start, args.end, args.features_table, host, port, db, user, password)
    # Merge symbols sequentially for a single-asset MVP (one by one)
    # A more advanced version can use VecEnv and multi-asset actions.
    from trading_env import TradingEnv

    model = None
    best_path = Path(args.out).with_suffix(".zip")
    best_path.parent.mkdir(parents=True, exist_ok=True)

    # Train per-symbol and keep the best by eval reward
    best_reward = float("-inf")
    for sym in syms:
        sub = df[df.symbol == sym].copy()
        if len(sub) < 1000:
            print(f"! skip {sym}: not enough rows")
            continue
        feat_cols = [c for c in sub.columns if c not in ("ts", "symbol", "price_return")]
        env = TradingEnv(
            sub[[*feat_cols, "price_return"]],
            feature_cols=feat_cols,
            cost_bps=args.cost_bps,
            vol_penalty=args.vol_penalty,
            dd_penalty=args.dd_penalty,
            turnover_penalty=args.turnover_penalty,
        )
        eval_env = TradingEnv(
            sub[[*feat_cols, "price_return"]],
            feature_cols=feat_cols,
            cost_bps=args.cost_bps,
            vol_penalty=args.vol_penalty,
            dd_penalty=args.dd_penalty,
            turnover_penalty=args.turnover_penalty,
        )
        # Load existing best model if resume requested and file exists
        if args.resume and best_path.exists():
            try:
                agent = SAC.load(str(best_path), env=env, tensorboard_log="runs/rl_tensorboard")
                print(f"✓ resumed model from {best_path}")
            except Exception:
                agent = SAC("MlpPolicy", env, verbose=1, tensorboard_log="runs/rl_tensorboard")
        else:
            agent = SAC("MlpPolicy", env, verbose=1, tensorboard_log="runs/rl_tensorboard")
        cb = EvalCallback(eval_env, best_model_save_path=str(best_path.parent), eval_freq=10_000, deterministic=True, render=False, n_eval_episodes=1)
        # Chunked learning to create periodic checkpoints
        remaining = int(args.timesteps)
        ck_every = max(10_000, int(args.checkpoint_every))
        while remaining > 0:
            step = min(ck_every, remaining)
            agent.learn(total_timesteps=step, reset_num_timesteps=False, callback=cb)
            remaining -= step
            # periodic checkpoint
            try:
                ts_ck = Path(args.out).with_name(Path(args.out).name + f"_{sym}_ck_{remaining}").with_suffix(".zip")
                agent.save(str(ts_ck))
            except Exception:
                pass
        # Eval once
        obs, _ = eval_env.reset()
        total_r = 0.0
        for _ in range(2000):
            action, _ = agent.predict(obs, deterministic=True)
            obs, reward, done, _, _ = eval_env.step(action)
            total_r += float(reward)
            if done:
                break
        if total_r > best_reward:
            best_reward = total_r
            model = agent
            # Save best to out path
            agent.save(str(best_path.with_suffix("")))
            print(f"✓ updated best model ({sym}) reward={total_r:.2f}")

    if model is None:
        print("No model trained (insufficient data or deps missing).")
        return
    print(f"Training finished. Best model saved at {best_path}")
    # MLflow logging (optional)
    try:
        import mlflow
        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "file:./mlruns"))
        mlflow.set_experiment(os.getenv("MLFLOW_EXPERIMENT", "AI Trader RL"))
        with mlflow.start_run(run_name=f"rl_train_{'_'.join(syms)}"):
            mlflow.log_params({
                "symbols": args.symbols,
                "timesteps": int(args.timesteps),
                "cost_bps": float(args.cost_bps),
                "vol_penalty": float(args.vol_penalty),
                "dd_penalty": float(args.dd_penalty),
                "turnover_penalty": float(args.turnover_penalty),
            })
            mlflow.log_metrics({"best_reward": float(best_reward)})
            if best_path.exists():
                mlflow.log_artifact(str(best_path))
    except Exception:
        pass


if __name__ == "__main__":
    main()
