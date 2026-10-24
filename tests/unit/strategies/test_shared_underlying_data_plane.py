from types import SimpleNamespace

from strategies.options_kernel.base import OptionKernel
from strategies.options_kernel.plugin import BaseOptionPlugin
from strategies.shared_data import UnderlyingSharedDataPlane


class _DummyOptionPlugin(BaseOptionPlugin):
    component_name = "dummy_option_plugin"

    def build_delegate(self, ctx, params):
        _ = (ctx, params)
        return None


def test_underlying_shared_data_plane_publish_and_read_contract():
    plane = UnderlyingSharedDataPlane()
    plane.publish_underlying(
        symbol="qqq",
        namespace="shared_alpha",
        ts_epoch_s=100.0,
        last_price=500.0,
        vwap=499.5,
        features={"close": 500.0, "vwap": 499.5, "v3m": 0.12},
        bar={"ts_epoch_s": 100.0, "open": 499.0, "high": 501.0, "low": 498.5, "close": 500.0, "volume": 1000.0},
    )

    snap = plane.latest_snapshot(symbol="QQQ", namespace="shared_alpha")
    feats = plane.latest_features(symbol="QQQ", namespace="shared_alpha")
    bars = plane.recent_bars(symbol="QQQ", namespace="shared_alpha", limit=10)

    assert snap is not None
    assert snap.symbol == "QQQ"
    assert snap.namespace == "shared_alpha"
    assert snap.last_price == 500.0
    assert feats.get("v3m") == 0.12
    assert len(bars) == 1
    assert float(bars[0].get("close") or 0.0) == 500.0


def test_option_kernel_build_plugin_ctx_uses_shared_underlying_fallback_when_enabled(monkeypatch):
    plane = UnderlyingSharedDataPlane()
    plane.publish_underlying(
        symbol="QQQ",
        namespace="qqq_cross_asset",
        ts_epoch_s=200.0,
        last_price=501.0,
        vwap=500.2,
        features={"close": 501.0, "vwap": 500.2, "atr_eff": 1.7},
    )

    from strategies import shared_data as shared_module

    monkeypatch.setattr(shared_module, "shared_underlying_data_plane", lambda: plane)
    # base module imported callable directly; patch there too.
    import strategies.options_kernel.base as option_base
    monkeypatch.setattr(option_base, "shared_underlying_data_plane", lambda: plane)

    kernel = OptionKernel(
        strategy_id="opt_test_001",
        real_ctx=SimpleNamespace(),
        params=SimpleNamespace(
            extras={
                "underlying": "QQQ",
                "enable_shared_underlying_data_plane": True,
                "shared_underlying_namespace": "qqq_cross_asset",
            }
        ),
        plugin=_DummyOptionPlugin(),
    )
    ctx = kernel._build_plugin_ctx("QQQ", {"ts_epoch_s": 200.0})

    assert float(ctx["features"].get("close") or 0.0) == 501.0
    assert float(ctx["features"].get("vwap") or 0.0) == 500.2
    assert float(ctx["features"].get("atr_eff") or 0.0) == 1.7


def test_underlying_shared_data_plane_staleness_guard_blocks_old_snapshot():
    plane = UnderlyingSharedDataPlane()
    plane.publish_underlying(
        symbol="QQQ",
        namespace="freshness_test",
        ts_epoch_s=100.0,
        last_price=500.0,
        features={"close": 500.0},
    )

    stale = plane.latest_snapshot(
        symbol="QQQ",
        namespace="freshness_test",
        max_staleness_sec=10.0,
        now_epoch_s=130.0,
    )
    assert stale is None
