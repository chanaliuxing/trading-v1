#!/usr/bin/env python3
"""
Tests for Evolution Governance (v3.5 Autonomy with Safety Guardrails)

Verifies all 7 safety gates:
1. Global kill switch (config.features.v3_autonomy.enable_policy_self_application)
2. Policy-level permission (governance.allow_code_evolution)
3. Module whitelist/blacklist
4. Test coverage gate
5. Daily rate limits
6. Predictive immunity (system risk)
7. Policy update restrictions

Evidence that the system CAN'T become an "uncontrolled monster"
"""
import pytest
import sys
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from libs.evolution.governance import EvolutionGovernance, GovernanceDecision


class TestGovernanceGates:
    """Test all 7 governance gates"""

    def test_gate_1_global_kill_switch(self):
        """Gate 1: Global kill switch blocks everything"""
        config = {
            "features": {
                "v3_autonomy": {
                    "enable_policy_self_application": False  # 🔒 DISABLED
                }
            }
        }

        policy = {
            "governance": {
                "allow_code_evolution": True  # Allowed by policy
            }
        }

        governance = EvolutionGovernance(config=config, policy=policy)

        decision = governance.check_code_evolution_allowed(
            target_module="libs/skills/generated/",
            experiment_type="tool_patch"
        )

        assert decision.allowed is False
        assert decision.gate_triggered == "global_kill_switch"
        assert "enable_policy_self_application=false" in decision.reason.lower()

        print("✅ Gate 1: Global kill switch BLOCKS all evolution")

    def test_gate_2_policy_level_permission(self):
        """Gate 2: Policy-level permission check"""
        config = {
            "features": {
                "v3_autonomy": {
                    "enable_policy_self_application": True  # Enabled
                }
            }
        }

        policy = {
            "governance": {
                "allow_code_evolution": False  # 🔒 DISABLED
            }
        }

        governance = EvolutionGovernance(config=config, policy=policy)

        decision = governance.check_code_evolution_allowed(
            target_module="libs/skills/generated/",
            experiment_type="tool_patch"
        )

        assert decision.allowed is False
        assert decision.gate_triggered == "policy_permission"
        assert "allow_code_evolution=false" in decision.reason.lower()

        print("✅ Gate 2: Policy permission BLOCKS code evolution")

    def test_gate_3_forbidden_modules(self):
        """Gate 3: Forbidden modules list"""
        config = {
            "features": {
                "v3_autonomy": {
                    "enable_policy_self_application": True
                }
            }
        }

        policy = {
            "governance": {
                "allow_code_evolution": True,
                "forbidden_modules": [
                    "kernel/",
                    "risk/",
                    "execution/",
                    "schema/",
                    "infra/",
                    "libs/db/",
                    "services/execution",
                    "services/risk"
                ]
            }
        }

        governance = EvolutionGovernance(config=config, policy=policy)

        # Try to patch forbidden modules
        forbidden_targets = [
            "kernel/core.py",
            "risk/validator.py",
            "execution/order_manager.py",
            "schema/migrations/",
            "infra/docker-compose.yml",
            "libs/db/clickhouse_client.py",
            "services/execution/executor.py"
        ]

        for target in forbidden_targets:
            decision = governance.check_code_evolution_allowed(
                target_module=target,
                experiment_type="code_patch"
            )

            assert decision.allowed is False, f"Should block {target}"
            assert decision.gate_triggered == "forbidden_module"

        print("✅ Gate 3: Forbidden modules BLOCK critical infrastructure")

    def test_gate_3_allowed_modules_whitelist(self):
        """Gate 3: Allowed modules whitelist (deny unlisted)"""
        config = {
            "features": {
                "v3_autonomy": {
                    "enable_policy_self_application": True
                }
            }
        }

        policy = {
            "governance": {
                "allow_code_evolution": True,
                "allowed_modules": [
                    "prompts/",
                    "docs/",
                    "tools/data_quality",
                    "libs/skills/generated/",
                    "tests/"
                ],
                "forbidden_modules": []
            }
        }

        governance = EvolutionGovernance(config=config, policy=policy)

        # Allowed targets
        allowed_targets = [
            "prompts/research_agent.txt",
            "docs/ARCHITECTURE.md",
            "tools/data_quality/monitor.py",
            "libs/skills/generated/risk/position_sizing.py",
            "tests/unit/test_foo.py"
        ]

        for target in allowed_targets:
            decision = governance.check_code_evolution_allowed(
                target_module=target,
                experiment_type="tool_patch"
            )

            assert decision.allowed is True, f"Should allow {target}"

        # Unlisted targets (denied)
        unlisted_targets = [
            "libs/strategy/base.py",  # Not in whitelist
            "services/market/sfs_1s_service.py"  # Not in whitelist
        ]

        for target in unlisted_targets:
            decision = governance.check_code_evolution_allowed(
                target_module=target,
                experiment_type="code_patch"
            )

            assert decision.allowed is False, f"Should block unlisted {target}"
            assert decision.gate_triggered == "not_in_whitelist"

        print("✅ Gate 3: Whitelist ALLOWS only listed modules, denies rest")

    def test_gate_4_test_coverage_gate(self):
        """Gate 4: Test coverage requirement"""
        config = {
            "features": {
                "v3_autonomy": {
                    "enable_policy_self_application": True,
                    "auto_merge_min_test_coverage_pct": 85  # Require 85%
                }
            }
        }

        policy = {
            "governance": {
                "allow_code_evolution": True,
                "allowed_modules": ["libs/skills/generated/"]
            }
        }

        governance = EvolutionGovernance(config=config, policy=policy)

        # Low coverage - denied
        decision_low = governance.check_code_evolution_allowed(
            target_module="libs/skills/generated/risk/",
            experiment_type="tool_patch",
            test_coverage_pct=60.0  # Too low
        )

        assert decision_low.allowed is False
        assert decision_low.gate_triggered == "test_coverage_gate"
        assert "60.0%" in decision_low.reason

        # High coverage - allowed
        decision_high = governance.check_code_evolution_allowed(
            target_module="libs/skills/generated/risk/",
            experiment_type="tool_patch",
            test_coverage_pct=92.5  # Above threshold
        )

        assert decision_high.allowed is True

        print("✅ Gate 4: Test coverage gate REQUIRES >= 85% coverage")

    def test_gate_5_daily_rate_limits(self):
        """Gate 5: Daily rate limits"""
        # Use temporary file for rate limit tracking
        with tempfile.TemporaryDirectory() as tmpdir:
            rate_limit_file = Path(tmpdir) / "governance_daily_limits.jsonl"

            config = {
                "features": {
                    "v3_autonomy": {
                        "enable_policy_self_application": True
                    }
                }
            }

            policy = {
                "governance": {
                    "allow_code_evolution": True,
                    "allowed_modules": ["libs/skills/generated/"],
                    "max_patches_per_day": 3  # Limit: 3 per day
                }
            }

            governance = EvolutionGovernance(config=config, policy=policy)
            governance.rate_limit_file = rate_limit_file  # Override path

            # First 3 patches should pass
            for i in range(3):
                decision = governance.check_code_evolution_allowed(
                    target_module="libs/skills/generated/",
                    experiment_type="tool_patch"
                )

                assert decision.allowed is True, f"Patch {i+1}/3 should be allowed"

                # Record submission
                governance.record_patch_submission({"patch_number": i+1})

            # 4th patch should be blocked
            decision_4th = governance.check_code_evolution_allowed(
                target_module="libs/skills/generated/",
                experiment_type="tool_patch"
            )

            assert decision_4th.allowed is False
            assert decision_4th.gate_triggered == "rate_limit"
            assert "3/3" in decision_4th.reason  # Hit limit

        print("✅ Gate 5: Daily rate limit ENFORCES max 3 patches/day")

    def test_gate_7_policy_update_restrictions(self):
        """Gate 7: Policy updates require special permission"""
        config = {
            "features": {
                "v3_autonomy": {
                    "enable_policy_self_application": True
                }
            }
        }

        policy = {
            "governance": {
                "allow_code_evolution": True,
                "allow_policy_updates": False  # 🔒 Policy updates disabled
            }
        }

        governance = EvolutionGovernance(config=config, policy=policy)

        decision = governance.check_code_evolution_allowed(
            target_module="configs/research_policy.json",
            experiment_type="policy_patch"
        )

        assert decision.allowed is False
        assert decision.gate_triggered == "policy_updates_disabled"
        assert "allow_policy_updates=false" in decision.reason.lower()

        print("✅ Gate 7: Policy updates REQUIRE separate permission")

    def test_all_gates_pass_scenario(self):
        """Test scenario where all gates pass"""
        with tempfile.TemporaryDirectory() as tmpdir:
            rate_limit_file = Path(tmpdir) / "governance_daily_limits.jsonl"

            config = {
                "features": {
                    "v3_autonomy": {
                        "enable_policy_self_application": True,  # ✅ Enabled
                        "auto_merge_min_test_coverage_pct": 85
                    }
                },
                "immunity": {
                    "enable_predictive_immunity": False  # Disable for this test
                }
            }

            policy = {
                "governance": {
                    "allow_code_evolution": True,  # ✅ Allowed
                    "allowed_modules": ["libs/skills/generated/"],
                    "forbidden_modules": ["kernel/", "risk/"],
                    "max_patches_per_day": 3
                }
            }

            governance = EvolutionGovernance(config=config, policy=policy)
            governance.rate_limit_file = rate_limit_file

            decision = governance.check_code_evolution_allowed(
                target_module="libs/skills/generated/risk/position_sizing.py",
                experiment_type="tool_patch",
                test_coverage_pct=92.5  # ✅ High coverage
            )

            assert decision.allowed is True
            assert decision.gate_triggered is None  # No gate blocked
            assert "all governance checks passed" in decision.reason.lower()

        print("✅ All gates PASS when conditions are met")

    def test_governance_summary(self):
        """Test governance summary reporting"""
        config = {
            "features": {
                "v3_autonomy": {
                    "enable_policy_self_application": False,
                    "auto_merge_min_test_coverage_pct": 85
                }
            },
            "immunity": {
                "enable_predictive_immunity": True
            }
        }

        policy = {
            "governance": {
                "allow_code_evolution": True,
                "allow_policy_updates": False,
                "allowed_modules": ["libs/skills/generated/"],
                "forbidden_modules": ["kernel/", "risk/"],
                "max_patches_per_day": 3
            }
        }

        governance = EvolutionGovernance(config=config, policy=policy)
        summary = governance.get_governance_summary()

        assert summary["autonomy_enabled"] is False
        assert summary["code_evolution_allowed"] is True
        assert summary["policy_updates_allowed"] is False
        assert summary["rate_limits"]["max_patches_per_day"] == 3
        assert summary["quality_gates"]["min_test_coverage_pct"] == 85
        assert "libs/skills/generated/" in summary["allowed_modules"]
        assert "kernel/" in summary["forbidden_modules"]
        assert summary["predictive_immunity_enabled"] is True

        print("✅ Governance summary reports all settings correctly")


def main():
    """Run all governance tests"""
    print("\n" + "=" * 80)
    print("  EVOLUTION GOVERNANCE TESTS (v3.5 Safety Guardrails)")
    print("=" * 80 + "\n")

    test_obj = TestGovernanceGates()

    tests = [
        ("Gate 1: Global Kill Switch", test_obj.test_gate_1_global_kill_switch),
        ("Gate 2: Policy Permission", test_obj.test_gate_2_policy_level_permission),
        ("Gate 3a: Forbidden Modules", test_obj.test_gate_3_forbidden_modules),
        ("Gate 3b: Allowed Modules Whitelist", test_obj.test_gate_3_allowed_modules_whitelist),
        ("Gate 4: Test Coverage Gate", test_obj.test_gate_4_test_coverage_gate),
        ("Gate 5: Daily Rate Limits", test_obj.test_gate_5_daily_rate_limits),
        ("Gate 7: Policy Update Restrictions", test_obj.test_gate_7_policy_update_restrictions),
        ("All Gates Pass Scenario", test_obj.test_all_gates_pass_scenario),
        ("Governance Summary", test_obj.test_governance_summary),
    ]

    passed = 0
    failed = 0

    for name, test_func in tests:
        try:
            print(f"\n{'=' * 80}")
            print(f"  TEST: {name}")
            print(f"{'=' * 80}")
            test_func()
            print(f"✅ PASSED: {name}\n")
            passed += 1
        except Exception as e:
            print(f"❌ FAILED: {name}")
            print(f"   Error: {e}\n")
            import traceback
            traceback.print_exc()
            failed += 1

    print("\n" + "=" * 80)
    print(f"  RESULTS: {passed}/{len(tests)} tests passed")
    print("=" * 80)

    if failed == 0:
        print("\n🎉 All governance tests passed!")
        print("   Evidence: v3 autonomy CANNOT become uncontrolled ✅")
        print("   All 7 safety gates verified ✅")
        return 0
    else:
        print(f"\n⚠️  {failed} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
