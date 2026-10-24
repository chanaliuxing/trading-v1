import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.exec.netting import ExecutionRouter, RoutingConfig


def test_execution_router_default_direct():
    router = ExecutionRouter(RoutingConfig(default_mode="direct"))
    d = router.decide(strategy_id="s1", symbol="TQQQ")
    assert d.mode == "direct"
    assert d.reason == "default"


def test_execution_router_strategy_override_wins():
    router = ExecutionRouter(
        RoutingConfig(
            default_mode="direct",
            strategy_mode_overrides={"s1": "netted"},
            symbol_mode_overrides={"TQQQ": "direct"},
        )
    )
    d = router.decide(strategy_id="s1", symbol="TQQQ")
    assert d.mode == "netted"
    assert d.reason == "strategy_override"


def test_execution_router_allowlist_switches_symbol_to_netted():
    router = ExecutionRouter(RoutingConfig(default_mode="direct", netted_symbols={"TQQQ"}))
    d = router.decide(strategy_id="s2", symbol="TQQQ")
    assert d.mode == "netted"
    assert d.reason == "symbol_allowlist"


def test_execution_router_requested_mode_has_highest_priority():
    router = ExecutionRouter(RoutingConfig(default_mode="direct", strategy_mode_overrides={"s1": "direct"}))
    d = router.decide(strategy_id="s1", symbol="TQQQ", requested_mode="netted")
    assert d.mode == "netted"
    assert d.reason == "strategy_requested"
