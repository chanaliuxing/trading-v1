import time

from strategies.qqq_intraday_strategy import QQQIntradayStrategy
from strategies.shared_data import UnderlyingSharedDataPlane


def test_qqq_monolith_publish_shared_underlying_data():
    plane = UnderlyingSharedDataPlane()
    strategy = QQQIntradayStrategy.__new__(QQQIntradayStrategy)
    strategy.params = type("Params", (), {"extras": {"enable_shared_underlying_data_plane": True}})()
    strategy.strategy_id = "qqq_intraday_001"
    strategy._qqq_vwap = 501.25
    strategy._qqew_vwap = None

    original = __import__("strategies.qqq_intraday_strategy", fromlist=["shared_underlying_data_plane"])
    saved = original.shared_underlying_data_plane
    original.shared_underlying_data_plane = lambda: plane
    try:
        strategy._publish_shared_underlying_data(symbol="QQQ", ts_ms=1710000000000, close_px=502.0, vol=1234.0)
    finally:
        original.shared_underlying_data_plane = saved

    snap = plane.latest_snapshot(symbol="QQQ", namespace="qqq")
    feats = plane.latest_features(symbol="QQQ", namespace="qqq")
    assert snap is not None
    assert float(snap.last_price or 0.0) == 502.0
    assert float(snap.vwap or 0.0) == 501.25
    assert float(feats.get("last_price") or 0.0) == 502.0


def test_qqq_recent_rows_prefers_shared_underlying_plane():
    plane = UnderlyingSharedDataPlane()
    now_s = time.time()
    plane.publish_underlying(
        symbol="QQQ",
        namespace="qqq",
        ts_epoch_s=now_s,
        last_price=500.0,
        bar={
            "ts_epoch_s": now_s,
            "open": 499.0,
            "high": 501.0,
            "low": 498.0,
            "close": 500.0,
            "volume": 50.0,
            "vwap": 500.0,
        },
    )
    strategy = QQQIntradayStrategy.__new__(QQQIntradayStrategy)
    strategy.params = type(
        "Params",
        (),
        {"extras": {"enable_shared_underlying_data_plane": True, "shared_underlying_read_mode": "prefer"}},
    )()
    strategy.strategy_id = "qqq_intraday_001"
    strategy._sfs = None

    original = __import__("strategies.qqq_intraday_strategy", fromlist=["shared_underlying_data_plane"])
    saved = original.shared_underlying_data_plane
    original.shared_underlying_data_plane = lambda: plane
    try:
        rows = __import__("asyncio").run(strategy._recent_1s_rows("QQQ", 5))
    finally:
        original.shared_underlying_data_plane = saved

    assert len(rows) == 1
    assert rows[0]["ts"] == int(now_s * 1000)
    assert float(rows[0]["close"] or 0.0) == 500.0


def test_qqq_recent_rows_plane_only_skips_sfs_reader():
    plane = UnderlyingSharedDataPlane()
    now_s = time.time()
    plane.publish_underlying(
        symbol="QQQ",
        namespace="qqq",
        ts_epoch_s=now_s,
        last_price=503.0,
        bar={
            "ts_epoch_s": now_s,
            "open": 502.0,
            "high": 504.0,
            "low": 501.0,
            "close": 503.0,
            "volume": 80.0,
            "vwap": 503.0,
        },
    )
    strategy = QQQIntradayStrategy.__new__(QQQIntradayStrategy)
    strategy.params = type(
        "Params",
        (),
        {"extras": {"enable_shared_underlying_data_plane": True, "shared_underlying_read_mode": "plane_only"}},
    )()
    strategy.strategy_id = "qqq_intraday_001"
    strategy._sfs = None

    original = __import__("strategies.qqq_intraday_strategy", fromlist=["shared_underlying_data_plane"])
    saved = original.shared_underlying_data_plane
    original.shared_underlying_data_plane = lambda: plane
    try:
        rows = __import__("asyncio").run(strategy._recent_1s_rows("QQQ", 5))
    finally:
        original.shared_underlying_data_plane = saved

    assert strategy._uses_sfs_reader() is False
    assert len(rows) == 1
    assert float(rows[0]["close"] or 0.0) == 503.0


def test_qqq_recent_rows_falls_back_to_global_when_primary_namespace_is_stale(monkeypatch):
    plane = UnderlyingSharedDataPlane()
    plane.publish_underlying(
        symbol="QQQ",
        namespace="qqq",
        ts_epoch_s=100.0,
        last_price=500.0,
        bar={
            "ts_epoch_s": 100.0,
            "open": 499.0,
            "high": 501.0,
            "low": 498.0,
            "close": 500.0,
            "volume": 50.0,
            "vwap": 500.0,
        },
    )
    plane.publish_underlying(
        symbol="QQQ",
        namespace="global",
        ts_epoch_s=198.0,
        last_price=503.0,
        bar={
            "ts_epoch_s": 198.0,
            "open": 502.0,
            "high": 504.0,
            "low": 501.0,
            "close": 503.0,
            "volume": 75.0,
            "vwap": 503.0,
        },
    )

    strategy = QQQIntradayStrategy.__new__(QQQIntradayStrategy)
    strategy.params = type(
        "Params",
        (),
        {
            "extras": {
                "enable_shared_underlying_data_plane": True,
                "shared_underlying_read_mode": "prefer",
                "freshness_max_sec": 5,
            }
        },
    )()
    strategy.strategy_id = "qqq_intraday_001"
    strategy._sfs = None

    import strategies.qqq_intraday_strategy as qmod

    monkeypatch.setattr(qmod, "shared_underlying_data_plane", lambda: plane)
    monkeypatch.setattr(qmod, "ET", __import__("zoneinfo").ZoneInfo("UTC"))

    class _Now:
        @staticmethod
        def now(_tz):
            from datetime import datetime, timezone

            return datetime.fromtimestamp(200.0, tz=timezone.utc)

    monkeypatch.setattr(qmod, "datetime", _Now)

    rows = __import__("asyncio").run(strategy._recent_1s_rows("QQQ", 5))

    assert len(rows) == 1
    assert rows[0]["ts"] == 198000
    assert float(rows[0]["close"] or 0.0) == 503.0


def test_qqq_recent_rows_falls_back_to_sfs_when_plane_empty(monkeypatch):
    strategy = QQQIntradayStrategy.__new__(QQQIntradayStrategy)
    strategy.params = type(
        "Params",
        (),
        {
            "extras": {
                "enable_shared_underlying_data_plane": True,
                "shared_underlying_read_mode": "prefer",
            }
        },
    )()
    strategy.strategy_id = "qqq_intraday_001"
    strategy._sfs = None

    import strategies.qqq_intraday_strategy as qmod

    class _FakeSFSReader:
        async def last_n(self, symbol, n=60):
            assert symbol == "QQQ"
            assert n == 5
            return [
                {"ts": 3000, "close": 503.0, "volume": 30.0},
                {"ts": 2000, "close": 502.0, "volume": 20.0},
                {"ts": 1000, "close": 501.0, "volume": 10.0},
            ]

    monkeypatch.setattr(qmod, "SFS1sReader", _FakeSFSReader)

    rows = __import__("asyncio").run(strategy._recent_1s_rows("QQQ", 5))

    assert [int(r["ts"]) for r in rows] == [3000, 2000, 1000]
    assert float(rows[0]["close"] or 0.0) == 503.0


def test_qqq_recent_rows_force_sfs_bypasses_plane(monkeypatch):
    plane = UnderlyingSharedDataPlane()
    now_s = time.time()
    plane.publish_underlying(
        symbol="QQQ",
        namespace="qqq",
        ts_epoch_s=now_s,
        last_price=600.0,
        bar={
            "ts_epoch_s": now_s,
            "open": 600.0,
            "high": 600.0,
            "low": 600.0,
            "close": 600.0,
            "volume": 1.0,
            "vwap": 600.0,
        },
    )

    strategy = QQQIntradayStrategy.__new__(QQQIntradayStrategy)
    strategy.params = type(
        "Params",
        (),
        {
            "extras": {
                "enable_shared_underlying_data_plane": True,
                "shared_underlying_read_mode": "prefer",
            }
        },
    )()
    strategy.strategy_id = "qqq_intraday_001"
    strategy._sfs = None

    import strategies.qqq_intraday_strategy as qmod

    class _FakeSFSReader:
        async def last_n(self, symbol, n=60):
            assert symbol == "QQQ"
            return [
                {"ts": 3000, "close": 503.0, "volume": 30.0},
                {"ts": 2000, "close": 502.0, "volume": 20.0},
                {"ts": 1000, "close": 501.0, "volume": 10.0},
            ]

    monkeypatch.setattr(qmod, "shared_underlying_data_plane", lambda: plane)
    monkeypatch.setattr(qmod, "SFS1sReader", _FakeSFSReader)

    rows = __import__("asyncio").run(strategy._recent_1s_rows("QQQ", 5, force_sfs=True))

    assert [int(r["ts"]) for r in rows] == [3000, 2000, 1000]
    assert float(rows[0]["close"] or 0.0) == 503.0


def test_qqq_initialize_plane_only_does_not_create_sfs_reader(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import strategies.qqq_intraday_strategy as qmod

    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = lambda *args, **kwargs: None
    ctx.strategy_id = "qqq_intraday_001"
    ctx.engine = MagicMock()
    ctx.system_health = type("Health", (), {"subscribe": AsyncMock(return_value=None)})()
    ctx.subscribe_tick_data = AsyncMock(return_value={})

    params = type(
        "Params",
        (),
        {
            "extras": {
                "enable_shared_underlying_data_plane": True,
                "shared_underlying_read_mode": "plane_only",
                "state_dir": str(tmp_path),
            }
        },
    )()
    strategy = QQQIntradayStrategy(ctx, params)

    saved_claim = qmod.claim_paths
    saved_restore_runtime = strategy._restore_runtime_state
    saved_restore_atr = strategy._restore_atr_seed_into_pre
    saved_refresh_daily = strategy._refresh_daily_atr_context
    saved_arm_restart = strategy._arm_restart_degrade_window
    qmod.claim_paths = lambda **kwargs: None
    strategy._restore_runtime_state = lambda: None
    strategy._restore_atr_seed_into_pre = lambda: None
    strategy._refresh_daily_atr_context = lambda now: None
    strategy._arm_restart_degrade_window = lambda now, extras: None

    try:
        asyncio.run(strategy.initialize())
    finally:
        qmod.claim_paths = saved_claim
        strategy._restore_runtime_state = saved_restore_runtime
        strategy._restore_atr_seed_into_pre = saved_restore_atr
        strategy._refresh_daily_atr_context = saved_refresh_daily
        strategy._arm_restart_degrade_window = saved_arm_restart

    assert strategy._sfs is None


def test_qqq_initialize_plane_only_requires_shared_plane_enabled(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = lambda *args, **kwargs: None
    ctx.strategy_id = "qqq_intraday_001"
    ctx.engine = MagicMock()
    ctx.system_health = type("Health", (), {"subscribe": AsyncMock(return_value=None)})()
    ctx.subscribe_tick_data = AsyncMock(return_value={})

    params = type(
        "Params",
        (),
        {
            "extras": {
                "enable_shared_underlying_data_plane": False,
                "shared_underlying_read_mode": "plane_only",
                "state_dir": str(tmp_path),
            }
        },
    )()
    strategy = QQQIntradayStrategy(ctx, params)

    try:
        asyncio.run(strategy.initialize())
        assert False, "initialize should fail closed when plane_only is set without shared plane"
    except RuntimeError as exc:
        assert "qqq_shared_underlying_plane_required_for_plane_only_mode" in str(exc)


def test_qqq_initialize_warms_up_before_live_subscription(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import strategies.qqq_intraday_strategy as qmod

    ctx = MagicMock()
    ctx.log = MagicMock()
    ctx.metrics = lambda *args, **kwargs: None
    ctx.strategy_id = "qqq_intraday_001"
    ctx.engine = MagicMock()
    ctx.system_health = type("Health", (), {"subscribe": AsyncMock(return_value=None)})()
    call_order = []

    async def _subscribe_tick_data(_symbols):
        call_order.append("subscribe")
        return {}

    ctx.subscribe_tick_data = _subscribe_tick_data

    params = type(
        "Params",
        (),
        {
            "extras": {
                "enable_shared_underlying_data_plane": True,
                "shared_underlying_read_mode": "prefer",
                "state_dir": str(tmp_path),
            }
        },
    )()
    strategy = QQQIntradayStrategy(ctx, params)

    saved_claim = qmod.claim_paths
    saved_restore_runtime = strategy._restore_runtime_state
    saved_restore_atr = strategy._restore_atr_seed_into_pre
    saved_refresh_daily = strategy._refresh_daily_atr_context
    saved_arm_restart = strategy._arm_restart_degrade_window
    saved_refresh_pos = strategy._refresh_ib_positions_best_effort
    saved_sync_pos = strategy._sync_position_state
    saved_obs = strategy._maybe_write_observability_file
    saved_warmup_entry = strategy._warmup_entry_seed_data
    saved_warmup_5m = strategy._warmup_5m
    qmod.claim_paths = lambda **kwargs: None
    strategy._restore_runtime_state = lambda: None
    strategy._restore_atr_seed_into_pre = lambda: None
    strategy._refresh_daily_atr_context = lambda now: None
    strategy._arm_restart_degrade_window = lambda now, extras: None
    strategy._refresh_ib_positions_best_effort = AsyncMock(return_value=None)
    strategy._sync_position_state = AsyncMock(return_value=None)
    strategy._maybe_write_observability_file = lambda now: None

    async def _warmup_entry():
        call_order.append("warmup_entry")

    async def _warmup_5m(symbol, warmup_n):
        call_order.append(f"warmup_5m:{symbol}")

    strategy._warmup_entry_seed_data = _warmup_entry
    strategy._warmup_5m = _warmup_5m

    try:
        asyncio.run(strategy.initialize())
    finally:
        qmod.claim_paths = saved_claim
        strategy._restore_runtime_state = saved_restore_runtime
        strategy._restore_atr_seed_into_pre = saved_restore_atr
        strategy._refresh_daily_atr_context = saved_refresh_daily
        strategy._arm_restart_degrade_window = saved_arm_restart
        strategy._refresh_ib_positions_best_effort = saved_refresh_pos
        strategy._sync_position_state = saved_sync_pos
        strategy._maybe_write_observability_file = saved_obs
        strategy._warmup_entry_seed_data = saved_warmup_entry
        strategy._warmup_5m = saved_warmup_5m

    assert call_order[:3] == ["warmup_entry", "warmup_5m:TQQQ", "warmup_5m:SQQQ"]
    assert call_order[3] == "subscribe"
