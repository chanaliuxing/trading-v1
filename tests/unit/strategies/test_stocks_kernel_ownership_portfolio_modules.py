from types import SimpleNamespace

from strategies.stocks_kernel.modules.ownership import OwnershipPolicy
from strategies.stocks_kernel.modules.portfolio import PortfolioExposurePolicy


def test_ownership_policy_blocks_foreign_strategy_under_strategy_scope():
    policy = OwnershipPolicy(default_scope="strategy")
    lots = (
        SimpleNamespace(symbol="QQQ", strategy_id="beta", lane_id="lane_b", ownership_scope="strategy"),
    )

    decision = policy.allow_entry(strategy_id="alpha", lane_id="lane_a", symbol="QQQ", active_lots=lots)

    assert decision.allowed is False
    assert decision.reason == "foreign_strategy_owns_symbol"


def test_ownership_policy_blocks_foreign_lane_under_lane_scope():
    policy = OwnershipPolicy(default_scope="lane")
    lots = (
        SimpleNamespace(symbol="QQQ", strategy_id="alpha", lane_id="lane_b", ownership_scope="lane"),
    )

    decision = policy.allow_entry(strategy_id="alpha", lane_id="lane_a", symbol="QQQ", active_lots=lots)

    assert decision.allowed is False
    assert decision.reason == "foreign_lane_owns_symbol"


def test_portfolio_exposure_policy_blocks_symbol_limit():
    policy = PortfolioExposurePolicy(symbol_limit=10_000.0)
    positions = (
        SimpleNamespace(symbol="QQQ", qty=10.0, price=500.0),
    )

    decision = policy.evaluate(
        current_positions=positions,
        candidate_symbol="QQQ",
        candidate_qty=20.0,
        candidate_price=300.0,
    )

    assert decision.allowed is False
    assert decision.reason == "symbol_exposure_limit"


def test_ownership_policy_blocks_protection_when_lane_authority_reserved():
    policy = OwnershipPolicy(default_scope="strategy")
    instrument = SimpleNamespace(
        lanes=lambda role=None: (
            SimpleNamespace(
                lane_id="long_lane",
                symbol="TQQQ",
                protection_authority="portfolio",
            ),
        ) if role == "execution" else (),
        lane_by_id=lambda lane_id: SimpleNamespace(
            lane_id=lane_id,
            symbol="TQQQ",
            protection_authority="portfolio",
        ),
    )

    decision = policy.can_build_protection_for_lane(
        strategy_id="alpha",
        instrument=instrument,
        exec_symbol="TQQQ",
        lane_id="long_lane",
    )

    assert decision.allowed is False
    assert decision.reason == "protection_authority_reserved"
    assert decision.payload["authority_field"] == "protection_authority"
    assert decision.payload["requested_strategy_id"] == "alpha"


def test_ownership_policy_emits_standardized_adopt_authority_payload():
    policy = OwnershipPolicy(default_scope="strategy")
    lot = SimpleNamespace(
        strategy_id="beta",
        lane_id="lane_b",
        symbol="TQQQ",
        adopt_authority="strategy",
    )

    decision = policy.can_adopt_reconcile(strategy_id="alpha", lot=lot)

    assert decision.allowed is False
    assert decision.reason == "adopt_authority_denied"
    assert decision.payload["authority_field"] == "adopt_authority"
    assert decision.payload["requested_strategy_id"] == "alpha"
    assert decision.payload["lot_strategy_id"] == "beta"
    assert decision.payload["symbol"] == "TQQQ"


def test_ownership_policy_lane_authority_blocks_foreign_strategy():
    policy = OwnershipPolicy(default_scope="lane")
    lot = SimpleNamespace(
        strategy_id="beta",
        lane_id="lane_b",
        symbol="TQQQ",
        flatten_authority="lane",
    )

    decision = policy.can_flatten(strategy_id="alpha", lot=lot, requested_lane_id="lane_b")

    assert decision.allowed is False
    assert decision.reason == "flatten_authority_denied"
    assert decision.payload["authority"] == "lane"
    assert decision.payload["requested_strategy_id"] == "alpha"
    assert decision.payload["lot_strategy_id"] == "beta"


def test_ownership_policy_lane_authority_blocks_lane_mismatch():
    policy = OwnershipPolicy(default_scope="lane")
    lot = SimpleNamespace(
        strategy_id="alpha",
        lane_id="lane_a",
        symbol="TQQQ",
        protection_authority="lane",
    )

    decision = policy.can_manage_protection(strategy_id="alpha", lot=lot, requested_lane_id="lane_b")

    assert decision.allowed is False
    assert decision.reason == "protection_authority_lane_denied"
    assert decision.payload["requested_lane_id"] == "lane_b"
    assert decision.payload["lot_lane_id"] == "lane_a"
