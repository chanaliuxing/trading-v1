import json
from datetime import datetime

from libs.strategy import StrategyContext, StrategyParams
from strategies.qqq_intraday_strategy import ET, QQQIntradayStrategy


class _DummyLogger:
    def info(self, *args, **kwargs):
        return None

    def warning(self, *args, **kwargs):
        return None

    def error(self, *args, **kwargs):
        return None

    def debug(self, *args, **kwargs):
        return None


class _DummyEngine:
    pass


def _make_strategy():
    ctx = StrategyContext(
        engine=_DummyEngine(),
        strategy_id="qqq_intraday_test",
        logger=_DummyLogger(),
        metrics=lambda *args, **kwargs: None,
    )
    params = StrategyParams()
    return QQQIntradayStrategy(ctx, params)


def test_atr_seed_persist_and_restore(tmp_path):
    seed_path = tmp_path / "qqq_intraday_atr_seed.json"

    strat = _make_strategy()
    strat._atr_seed_path = seed_path
    strat._persist_atr_seed(
        day="2026-02-09",
        atr_pre={"TQQQ": 0.35, "SQQQ": 0.30},
        reason="test",
    )
    assert seed_path.exists()

    strat2 = _make_strategy()
    strat2._atr_seed_path = seed_path
    strat2._restore_atr_seed_into_pre()

    assert strat2._atr_pre["TQQQ"] == 0.35
    assert strat2._atr_pre["SQQQ"] == 0.30


def test_atr_seed_manual_json_supported(tmp_path):
    seed_path = tmp_path / "manual_atr_seed.json"
    seed_path.write_text(
        json.dumps(
            {
                "day": "2026-02-08",
                "TQQQ": 0.33,
                "SQQQ": 0.28,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    strat = _make_strategy()
    strat._atr_seed_path = seed_path
    strat._restore_atr_seed_into_pre()

    assert strat._atr_pre["TQQQ"] == 0.33
    assert strat._atr_pre["SQQQ"] == 0.28


def test_maybe_persist_daily_atr_seed_uses_pre_when_live_missing(tmp_path):
    seed_path = tmp_path / "eod_atr_seed.json"
    strat = _make_strategy()
    strat._atr_seed_path = seed_path
    strat._atr_pre["TQQQ"] = 0.34
    strat._atr_pre["SQQQ"] = 0.29
    strat._atr_5m = lambda symbol, period: None  # type: ignore[method-assign]

    now = datetime(2026, 2, 9, 15, 59, 30, tzinfo=ET)
    strat._maybe_persist_daily_atr_seed(now)

    assert seed_path.exists()
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    latest = raw.get("latest", {})
    assert latest.get("day") == "2026-02-09"
    assert latest.get("atr_pre", {}).get("TQQQ") == 0.34
    assert latest.get("atr_pre", {}).get("SQQQ") == 0.29
