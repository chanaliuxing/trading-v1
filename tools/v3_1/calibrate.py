"""Walk-forward parameter calibrator for V3.1 strategy."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass
class TrialResult:
    params: Dict[str, float]
    expectancy: float
    win_rate: float
    avg_r: float
    trades: int


def load_trade_log(path: Path) -> List[Dict]:
    rows = []
    for line in path.read_text("utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows


def score_trades(trades: List[Dict]) -> Tuple[float, float, float]:
    if not trades:
        return 0.0, 0.0, 0.0
    rs = [t.get("realized_r", 0.0) for t in trades]
    wins = [r for r in rs if r > 0]
    win_rate = len(wins) / len(rs) if rs else 0.0
    avg_r = sum(rs) / len(rs) if rs else 0.0
    expectancy = avg_r * win_rate
    return expectancy, win_rate, avg_r


def walk_forward(trades: List[Dict], param_grid: List[Dict[str, float]]) -> TrialResult:
    best = TrialResult(params={}, expectancy=-1e9, win_rate=0.0, avg_r=0.0, trades=0)
    for params in param_grid:
        expectancy, win_rate, avg_r = score_trades(trades)
        if expectancy > best.expectancy:
            best = TrialResult(params=params, expectancy=expectancy, win_rate=win_rate, avg_r=avg_r, trades=len(trades))
    return best


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-log", required=True, help="JSONL trade log path")
    parser.add_argument("--out", required=True, help="Output params JSON path")
    args = parser.parse_args()

    trades = load_trade_log(Path(args.trade_log))

    # Simple grid placeholder (expand as needed)
    grid = [
        {"breadth_off_a": 0.40, "breadth_ok": 0.55, "hedge_cost_ratio": 0.35},
        {"breadth_off_a": 0.45, "breadth_ok": 0.60, "hedge_cost_ratio": 0.30},
    ]

    best = walk_forward(trades, grid)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(best.params, indent=2), encoding="utf-8")

    print(f"best_expectancy={best.expectancy:.4f} win_rate={best.win_rate:.3f} avg_r={best.avg_r:.3f} trades={best.trades}")


if __name__ == "__main__":
    main()
