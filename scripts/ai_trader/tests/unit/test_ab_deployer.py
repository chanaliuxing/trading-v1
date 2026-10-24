"""Unit tests for AB Deployer"""
import json
from pathlib import Path
import pytest

from scripts.ai_trader.deploy.ab_deployer import ABDeployer


class TestABDeployer:
    """Test ABDeployer class"""

    def test_propose_challenger_wins(self, temp_dir):
        """Test AB proposal when challenger meets criteria"""
        registry_path = temp_dir / "registry.json"
        deployer = ABDeployer(
            registry_path=str(registry_path),
            promote_rules={"sharpe_gain_ratio": 1.05, "mdd_improve_ratio": 0.90},
            default_w={"champion": 0.7, "challenger": 0.3}
        )

        champion = {"sharpe": 1.00, "mdd": 0.10}
        challenger = {"sharpe": 1.10, "mdd": 0.08}  # +10% sharpe, -20% mdd

        result = deployer.propose(champion, challenger)

        assert result["weights"]["champion"] == 0.7
        assert result["weights"]["challenger"] == 0.3
        assert result["reason"] == "auto_ab"

    def test_propose_challenger_fails_sharpe(self, temp_dir):
        """Test AB proposal when challenger fails Sharpe criterion"""
        registry_path = temp_dir / "registry.json"
        deployer = ABDeployer(
            registry_path=str(registry_path),
            promote_rules={"sharpe_gain_ratio": 1.05, "mdd_improve_ratio": 0.90},
            default_w={"champion": 0.7, "challenger": 0.3}
        )

        champion = {"sharpe": 1.00, "mdd": 0.10}
        challenger = {"sharpe": 1.02, "mdd": 0.08}  # Only +2% sharpe (need +5%)

        result = deployer.propose(champion, challenger)

        assert result["weights"]["champion"] == 1.0
        assert result["weights"]["challenger"] == 0.0
        assert result["reason"] == "auto_ab"

    def test_propose_challenger_fails_mdd(self, temp_dir):
        """Test AB proposal when challenger fails drawdown criterion"""
        registry_path = temp_dir / "registry.json"
        deployer = ABDeployer(
            registry_path=str(registry_path),
            promote_rules={"sharpe_gain_ratio": 1.05, "mdd_improve_ratio": 0.90},
            default_w={"champion": 0.7, "challenger": 0.3}
        )

        champion = {"sharpe": 1.00, "mdd": 0.10}
        challenger = {"sharpe": 1.10, "mdd": 0.095}  # Sharpe good, but mdd not 10% better

        result = deployer.propose(champion, challenger)

        assert result["weights"]["champion"] == 1.0
        assert result["weights"]["challenger"] == 0.0

    def test_propose_invalid_data(self, temp_dir):
        """Test AB proposal with invalid metrics"""
        registry_path = temp_dir / "registry.json"
        deployer = ABDeployer(
            registry_path=str(registry_path),
            promote_rules={"sharpe_gain_ratio": 1.05, "mdd_improve_ratio": 0.90},
            default_w={"champion": 0.7, "challenger": 0.3}
        )

        champion = {"sharpe": "invalid", "mdd": 0.10}
        challenger = {"sharpe": 1.10, "mdd": None}

        result = deployer.propose(champion, challenger)

        # Should fail gracefully
        assert result["weights"]["champion"] == 1.0
        assert result["weights"]["challenger"] == 0.0

    def test_save_registry(self, temp_dir):
        """Test saving registry to file"""
        registry_path = temp_dir / "ab_registry.json"
        deployer = ABDeployer(
            registry_path=str(registry_path),
            promote_rules={},
            default_w={}
        )

        registry_data = {
            "champion": {"id": "v1", "sharpe": 1.15, "mdd": 0.09},
            "challenger": {"id": "v2", "sharpe": 1.25, "mdd": 0.08},
            "weights": {"champion": 0.6, "challenger": 0.4},
            "updated_at": "2025-10-29T12:00:00Z"
        }

        deployer.save_registry(registry_data)

        assert registry_path.exists()
        loaded = json.loads(registry_path.read_text())
        assert loaded["champion"]["id"] == "v1"
        assert loaded["weights"]["champion"] == 0.6

    def test_custom_promote_rules(self, temp_dir):
        """Test with custom promotion rules"""
        registry_path = temp_dir / "registry.json"
        deployer = ABDeployer(
            registry_path=str(registry_path),
            promote_rules={"sharpe_gain_ratio": 1.20, "mdd_improve_ratio": 0.80},  # Stricter
            default_w={"champion": 0.8, "challenger": 0.2}
        )

        champion = {"sharpe": 1.00, "mdd": 0.10}
        challenger = {"sharpe": 1.15, "mdd": 0.09}  # +15% sharpe, -10% mdd

        result = deployer.propose(champion, challenger)

        # Should fail: need +20% sharpe AND -20% mdd
        assert result["weights"]["champion"] == 1.0
        assert result["weights"]["challenger"] == 0.0
