from __future__ import annotations

from pathlib import Path

import yaml

from scripts.ai_trader.learning.pipeline import LearningPipeline


def _write_runtime_cfg(path: Path, strategies: list[dict]) -> None:
    data = {"strategies": strategies}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_learning_pipeline_uses_configured_targets(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    _write_runtime_cfg(
        cfg_path,
        strategies=[
            {"id": "qqq_intraday_001", "enabled": True, "params": {"extras": {"symbols": ["QQQ", "TQQQ"]}}},
            {"id": "enhanced_ml_001", "enabled": False, "params": {"extras": {"symbols": ["TSLA"]}}},
        ],
    )
    control_cfg = {"policies": {"learning": {"default_targets": ["enhanced_ml_001"]}}}
    lp = LearningPipeline(control_cfg=control_cfg, runtime_config_path=str(cfg_path))

    assert lp.resolve_targets() == ["enhanced_ml_001"]
    assert lp.primary_target() == "enhanced_ml_001"
    assert lp.collect_symbols() == ["TSLA"]


def test_learning_pipeline_falls_back_to_enabled_targets(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    _write_runtime_cfg(
        cfg_path,
        strategies=[
            {"id": "qqq_intraday_001", "enabled": True, "params": {"extras": {"symbols": ["QQQ", "QQEW"]}}},
            {"id": "bull_put_spread_qqq", "enabled": False, "params": {"extras": {"symbol": "QQQ"}}},
        ],
    )
    lp = LearningPipeline(control_cfg={}, runtime_config_path=str(cfg_path))

    assert lp.resolve_targets() == ["qqq_intraday_001"]
    assert lp.primary_target() == "qqq_intraday_001"
    assert lp.collect_symbols() == ["QQQ", "QQEW"]


def test_learning_pipeline_fallback_strategy_from_apply_params(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    _write_runtime_cfg(
        cfg_path,
        strategies=[
            {"id": "enhanced_ml_001", "enabled": False, "params": {"extras": {"symbols": ["TSLA", "NVDA"]}}},
        ],
    )
    control_cfg = {
        "commands": {
            "apply_params": "venv/bin/python apply_extras_params.py --config-yaml config.yml --strategy-id enhanced_ml_001"
        }
    }
    lp = LearningPipeline(control_cfg=control_cfg, runtime_config_path=str(cfg_path))

    assert lp.resolve_targets() == ["enhanced_ml_001"]
    assert lp.primary_target() == "enhanced_ml_001"


def test_learning_pipeline_apply_command_override(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    _write_runtime_cfg(
        cfg_path,
        strategies=[
            {
                "id": "qqq_intraday_001",
                "enabled": True,
                "learning_profile": "qqq_intraday",
                "learning": {
                    "apply_params_cmd": "venv/bin/python custom_apply.py --strategy-id qqq_intraday_001"
                },
            }
        ],
    )
    control_cfg = {
        "commands": {
            "apply_params": "venv/bin/python apply_extras_params.py --config-yaml config.yml --strategy-id enhanced_ml_001"
        }
    }
    lp = LearningPipeline(control_cfg=control_cfg, runtime_config_path=str(cfg_path))

    cmd = lp.apply_command_for("qqq_intraday_001")
    assert "custom_apply.py" in cmd


def test_learning_pipeline_observability_fields(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.yml"
    _write_runtime_cfg(
        cfg_path,
        strategies=[
            {
                "id": "qqq_intraday_001",
                "enabled": True,
                "learning_profile": "qqq_intraday",
            }
        ],
    )
    control_cfg = {"policies": {"learning": {"pipeline_version": "v2"}}}
    lp = LearningPipeline(control_cfg=control_cfg, runtime_config_path=str(cfg_path))

    assert lp.profile_for("qqq_intraday_001") == "qqq_intraday"
    assert lp.adapter_name_for("qqq_intraday_001") == "generic"
    assert lp.pipeline_version() == "v2"
