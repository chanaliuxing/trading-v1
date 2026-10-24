from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from services.option_replay.core.types import ContractQuote
from services.option_replay.research_adapter import DEFAULT_REPLAY_STRATEGY_MAP, OptionReplayResearchAdapter


def _quote(ts: datetime, *, spot: float, bid: float, ask: float, dte: int = 1) -> ContractQuote:
    mid = (bid + ask) / 2.0
    return ContractQuote(
        ts=ts,
        expiry="2026-03-25",
        right="C",
        strike=round(spot),
        dte=dte,
        bid=bid,
        ask=ask,
        mid=mid,
        iv=0.24,
        delta=0.52,
        gamma=0.01,
        theta=-0.02,
        vega=0.12,
        open_interest=500,
    )


def _build_bars() -> pd.DataFrame:
    ts = pd.date_range("2026-03-24 09:30:00", periods=240, freq="1min")
    close = []
    for i in range(len(ts)):
        if i < 15:
            close.append(500.0 + 0.02 * i)
        elif i == 15:
            close.append(501.35)
        elif i == 16:
            close.append(501.60)
        elif i < 25:
            close.append(501.80 + 0.03 * (i - 16))
        else:
            close.append(501.0 + 0.01 * (i - 25))
    close = pd.Series(close, dtype=float)
    return pd.DataFrame(
        {
            "ts": ts,
            "symbol": ["QQQ"] * len(ts),
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 0.18,
            "low": close - 0.16,
            "close": close,
            "vwap": close - 0.05,
            "volume": [1500.0] * len(ts),
        }
    )


def _build_option_cache(bars: pd.DataFrame) -> dict[tuple[datetime, str], list[ContractQuote]]:
    cache: dict[tuple[datetime, str], list[ContractQuote]] = {}
    for i, row in bars.iterrows():
        ts = pd.Timestamp(row["ts"]).to_pydatetime().replace(second=0, microsecond=0)
        spot = float(row["close"])
        bid = 4.80 + 0.01 * min(i, 20)
        ask = bid + 0.40
        cache[(ts, "C")] = [_quote(ts, spot=spot, bid=bid, ask=ask)]
    return cache


def test_validate_resolves_definition_fallback_strategy():
    adapter = OptionReplayResearchAdapter()
    spec = {
        "strategy_ref": {"definition_id": "QQQ_INTRADAY_OPTION_PARITY_V1"},
        "universe": {"underlying": "QQQ"},
    }

    validation = adapter.validate(spec)

    assert validation["ok"] is True
    assert validation["definition_id"] == "QQQ_INTRADAY_OPTION_PARITY_V1"
    assert validation["replay_strategy_id"] == DEFAULT_REPLAY_STRATEGY_MAP["QQQ_INTRADAY_OPTION_PARITY_V1"]
    assert validation["replay_strategy_source"] == "adapter_fallback"


def test_run_produces_standardized_result_and_artifacts(tmp_path: Path):
    adapter = OptionReplayResearchAdapter(output_root=tmp_path)
    bars = _build_bars()
    spec = {
        "experiment_id": "options-single-leg-smoke",
        "strategy_ref": {"definition_id": "QQQ_INTRADAY_OPTION_PARITY_V1"},
        "universe": {"underlying": "QQQ"},
        "inputs": {
            "bars": bars,
            "option_cache": _build_option_cache(bars),
        },
        "acceptance": {
            "min_trades": 1,
            "max_drawdown_pct": 0.50,
        },
        "robustness": {
            "wf_pass": True,
        },
        "sizing": {
            "risk_per_trade_bps": 15,
        },
    }

    result = adapter.run(spec)

    assert result["status"] in {"completed", "degraded"}
    assert result["market"] == "options"
    assert result["definition_id"] == "QQQ_INTRADAY_OPTION_PARITY_V1"
    assert result["replay_strategy_id"] == DEFAULT_REPLAY_STRATEGY_MAP["QQQ_INTRADAY_OPTION_PARITY_V1"]
    assert result["quality_flags"]["validation_ok"] is True
    assert result["quality_flags"]["has_fills"] is True
    assert result["quality_flags"]["enough_trades"] is True
    assert result["summary_metrics"]["trades"] >= 1
    assert result["execution_reality"]["fill_ratio"] > 0.0
    assert result["execution_reality"]["modeled_slippage_bps"] > 0.0
    assert result["execution_reality"]["avg_fill_latency_sec"] is not None
    assert result["execution_reality"]["latency_bucket"] in {"sub_1s", "sub_2s", "sub_5s", "sub_10s", "10s_plus", "unknown"}
    assert result["execution_reality"]["order_count"] >= 1
    assert result["artifacts"]["run_dir"].endswith("options-single-leg-smoke")
    assert result["lineage"]["family_id"]
    assert result["robustness"]["wf_pass"] is True
    assert result["sizing"]["risk_per_trade_bps"] == 15

    artifacts = result["artifacts"]
    for key in ("result_json", "summary_json", "trades_csv", "equity_curve_csv", "orders_csv", "fills_csv"):
        assert Path(artifacts[key]).exists(), key


def test_run_persists_blocked_result_for_invalid_spec(tmp_path: Path):
    adapter = OptionReplayResearchAdapter(output_root=tmp_path)
    result = adapter.run(
        {
            "experiment_id": "options-single-leg-blocked",
            "strategy_ref": {"definition_id": "QQQ_INTRADAY_OPTION_PARITY_V1"},
            "universe": {"underlying": "QQQ"},
            "acceptance": {
                "min_trades": 1,
                "max_drawdown_pct": 0.50,
            },
            "inputs": {},
        }
    )

    assert result["status"] == "failed"
    assert result["quality_flags"]["validation_ok"] is True
    assert result["quality_flags"]["has_fills"] is False
    assert result["execution_reality"]["fill_ratio"] == 0.0
    assert result["execution_reality"]["modeled_slippage_bps"] == 0.0
    assert result["execution_reality"]["latency_bucket"] == "unknown"
    assert Path(result["artifacts"]["result_json"]).exists()
    assert Path(result["artifacts"]["summary_json"]).exists()
