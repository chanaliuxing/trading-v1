import types

from libs.critic.critic_client import CriticClient, CriticConfig


def test_check_plan_with_mocked_sources():
    # Build instance without hitting ClickHouse
    critic = CriticClient.__new__(CriticClient)
    critic.config = CriticConfig(min_sample_size=1, block_sharpe_threshold=-0.5, block_confidence_threshold=0.9)
    critic.client = None

    critic._fetch_stats = types.MethodType(lambda self, regime, strategy: {"sharpe": -0.6, "n": 10}, critic)
    critic._fetch_heuristics = types.MethodType(
        lambda self: [{"advice": "test", "confidence": 0.2, "condition": "x", "metrics": {}}], critic
    )

    out = critic.check_plan({"regime": "r", "strategy": "s"})
    assert out["should_block"] is True
    assert out["approval_score"] == -0.6
    assert out["sample_size"] == 10
