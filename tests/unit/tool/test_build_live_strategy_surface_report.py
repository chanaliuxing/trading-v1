from pathlib import Path

from tool.report.build_live_strategy_surface_report import build_live_strategy_surface_report


def test_build_live_strategy_surface_report_classifies_modes_and_risks(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "strategies").mkdir(parents=True)

    (repo_root / "strategies" / "qqq_intraday_strategy.py").write_text(
        "\n".join(
            [
                "from libs.db.redis_client import RedisClient",
                "class QQQIntradayStrategy:",
                "    pass",
                "def demo(ctx):",
                "    ctx.get_price('QQQ')",
                "    ctx.submit_intent({})",
                "    return RedisClient",
            ]
        ),
        encoding="utf-8",
    )
    (repo_root / "strategies" / "stocks_kernel_strategy.py").write_text(
        "\n".join(
            [
                "class StocksKernelStrategy:",
                "    async def on_features(self, symbol, features):",
                "        return None",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repo_root / "strategies" / "qqq_1dte_ma10_reject_v2_strategy.py").write_text(
        "\n".join(
            [
                "from strategies.options_kernel.base import OptionKernelStrategy",
                "class QQQ1DTEMA10RejectV2Strategy(OptionKernelStrategy):",
                "    plugin_cls = object",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config_path = repo_root / "config.yml"
    config_path.write_text(
        """
strategies:
  - id: qqq_intraday_001
    enabled: true
    module: strategies.qqq_intraday_strategy
    class: QQQIntradayStrategy
    params:
      extras:
        symbols: [QQQ, TQQQ, SQQQ]
        enable_shared_underlying_data_plane: true
        shared_underlying_read_mode: plane_only
        legacy_trader_parity_mode: true
        enforce_stock_execution_blockers: false
  - id: qqq_1dte_ma10_reject_0324_v2_001
    enabled: true
    module: strategies.qqq_1dte_ma10_reject_v2_strategy
    class: QQQ1DTEMA10RejectV2Strategy
    params:
      extras:
        underlying: QQQ
        risk_symbol: OPT:QQQ:1DTE
        kernel_mode: pure_plugin
        shared_cap_group_strategy_ids:
          - qqq_0dte_opt_live_001
          - qqq_1dte_ma10_reject_0324_v2_001
  - id: qqq_kernel_shadow_legacy_on_001
    enabled: true
    registry_name: stocks_kernel
    module: strategies.stocks_kernel_strategy
    class: StocksKernelStrategy
    params:
      extras:
        dry_run: true
        shadow_only: true
        shadow_origin_strategy_id: qqq_intraday_001
        signal_symbol: QQQ
        long_exec_symbol: TQQQ
        short_exec_symbol: SQQQ
        enable_shared_underlying_data_plane: true
        shared_underlying_read_mode: plane_only
        policy_id: qqq_intraday_policy_v1
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = build_live_strategy_surface_report(config_path, repo_root=repo_root)

    assert report["summary"]["active_strategy_count"] == 3
    assert report["summary"]["live_strategy_count"] == 2
    assert report["summary"]["shadow_strategy_count"] == 1
    assert report["summary"]["production_eligible_count"] == 0
    assert report["summary"]["canary_only_count"] == 2
    assert report["summary"]["shadow_only_count"] == 1
    assert "qqq_intraday_001" in report["high_risk_strategy_ids"]
    assert "qqq_kernel_shadow_legacy_on_001" not in report["live_strategy_ids"]

    qqq = next(row for row in report["strategies"] if row["strategy_id"] == "qqq_intraday_001")
    assert qqq["mode"] == "live"
    assert qqq["path_type"] == "DIRECT_STRATEGY_EXECUTION_PATH"
    assert qqq["permission_tier"] == "SHADOW_ONLY"
    assert qqq["contract_readiness"]["production_contract_ready"] is False
    assert "stock_execution_blockers_disabled" in qqq["contract_readiness"]["blockers"]
    assert qqq["data_plane_mode"] == "underlying_shared_plane:plane_only"
    assert "legacy_parity_mode_enabled" in qqq["risk_flags"]
    assert "stock_execution_blockers_disabled" in qqq["risk_flags"]
    assert "source_data_signal:direct_ctx_get_price" in qqq["risk_flags"]
    assert "source_data_signal:redis_client" in qqq["risk_flags"]

    option_v2 = next(row for row in report["strategies"] if row["strategy_id"] == "qqq_1dte_ma10_reject_0324_v2_001")
    assert option_v2["execution_path"] == "options_kernel_pure_plugin"
    assert option_v2["path_type"] == "OPTIONS_KERNEL_PURE_PLUGIN"
    assert option_v2["permission_tier"] == "CANARY_ONLY"
    assert option_v2["contract_readiness"]["production_contract_ready"] is False
    assert "production_parity_proof_required" in option_v2["contract_readiness"]["warnings"]
    assert option_v2["data_plane_mode"] == "engine_managed_features"
    assert option_v2["symbols"] == ["QQQ", "OPT:QQQ:1DTE"]
    assert option_v2["strategy_refs"] == ["qqq_0dte_opt_live_001", "qqq_1dte_ma10_reject_0324_v2_001"]

    shadow = next(row for row in report["strategies"] if row["strategy_id"] == "qqq_kernel_shadow_legacy_on_001")
    assert shadow["mode"] == "shadow"
    assert shadow["shadow_origin_strategy_id"] == "qqq_intraday_001"
    assert shadow["execution_path"] == "stocks_kernel_execution_path"
    assert shadow["path_type"] == "STOCKS_KERNEL_EXECUTION_PATH"
    assert shadow["permission_tier"] == "CANARY_ONLY"
    assert shadow["data_plane_mode"] == "underlying_shared_plane:plane_only"


def test_strategy_surface_direct_path_is_shadow_only(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "strategies").mkdir(parents=True)
    (repo_root / "strategies" / "direct.py").write_text(
        "class Direct:\n    async def run(self, ctx):\n        await ctx.submit_intent({})\n",
        encoding="utf-8",
    )
    config_path = repo_root / "config.yml"
    config_path.write_text(
        """
strategies:
  - id: qqq_intraday_001
    enabled: true
    module: strategies.direct
    class: Direct
    params:
      extras:
        symbols: [QQQ]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    row = build_live_strategy_surface_report(config_path, repo_root=repo_root)["strategies"][0]

    assert row["path_type"] == "DIRECT_STRATEGY_EXECUTION_PATH"
    assert row["permission_tier"] == "SHADOW_ONLY"
    assert "direct_strategy_execution_path" in row["contract_readiness"]["blockers"]
    assert row["evidence_flags"]["direct_strategy_execution_path"] is True


def test_strategy_surface_legacy_path_disabled_until_contract_parity(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "strategies").mkdir(parents=True)
    (repo_root / "strategies" / "legacy.py").write_text("class Legacy:\n    pass\n", encoding="utf-8")
    config_path = repo_root / "config.yml"
    config_path.write_text(
        """
strategies:
  - id: legacy_001
    enabled: true
    module: strategies.legacy
    class: Legacy
    params:
      extras:
        symbols: [SPY]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    row = build_live_strategy_surface_report(config_path, repo_root=repo_root)["strategies"][0]

    assert row["path_type"] == "LEGACY_STRATEGY_EXECUTION_PATH"
    assert row["permission_tier"] == "DISABLED_UNTIL_CONTRACT_PARITY"
    assert "legacy_strategy_execution_path" in row["contract_readiness"]["blockers"]


def test_strategy_surface_private_data_path_not_production_eligible(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    (repo_root / "strategies").mkdir(parents=True)
    (repo_root / "strategies" / "private_option.py").write_text("class PrivateOption:\n    pass\n", encoding="utf-8")
    config_path = repo_root / "config.yml"
    config_path.write_text(
        """
strategies:
  - id: private_option_001
    enabled: true
    module: strategies.private_option
    class: PrivateOption
    params:
      extras:
        risk_symbol: OPT:QQQ:1DTE
""".strip()
        + "\n",
        encoding="utf-8",
    )

    row = build_live_strategy_surface_report(config_path, repo_root=repo_root)["strategies"][0]

    assert row["path_type"] == "OPTION_PRIVATE_DATA_PATH"
    assert row["permission_tier"] == "DISABLED_UNTIL_CONTRACT_PARITY"
    assert row["contract_readiness"]["production_contract_ready"] is False


def test_strategy_surface_unknown_path_disabled() -> None:
    report = build_live_strategy_surface_report
    from tool.report import build_live_strategy_surface_report as surface

    readiness, tier = surface._contract_readiness_and_tier(
        path_type="UNKNOWN_PATH",
        data_plane_mode="unknown",
        execution_path="custom_path",
        evidence_flags={
            "legacy_strategy_execution_path": False,
            "direct_strategy_execution_path": False,
            "option_private_data_path": False,
            "stock_execution_blockers_disabled": False,
        },
        risk_flags=[],
        source_scan={},
    )

    assert report
    assert tier == "DISABLED_UNTIL_CONTRACT_PARITY"
    assert "unknown_path_type" in readiness["blockers"]
