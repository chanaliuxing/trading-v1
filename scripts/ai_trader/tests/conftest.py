"""Pytest configuration and shared fixtures"""
import os
import sys
import json
import tempfile
from pathlib import Path
from typing import Generator
import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Temporary directory for test artifacts"""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_health_report(temp_dir: Path) -> Path:
    """Mock system_health.json file"""
    report = {
        "overall_status": "healthy",
        "checks": {
            "clickhouse": {"status": "ok"},
            "redis": {"status": "ok"},
            "ibkr": {"status": "ok"}
        },
        "timestamp": "2025-10-29T10:00:00Z"
    }
    report_path = temp_dir / "system_health.json"
    report_path.write_text(json.dumps(report, indent=2))
    return report_path


@pytest.fixture
def mock_config(temp_dir: Path) -> Path:
    """Mock ai_trader.yaml config"""
    config = {
        "paths": {
            "health_report": str(temp_dir / "system_health.json"),
            "logs_dir": str(temp_dir / "logs"),
            "runs_dir": str(temp_dir / "runs")
        },
        "modes": {
            "paper": True,
            "llm_paper_flag": "--dry-run"
        },
        "clickhouse": {
            "client": "clickhouse-client",
            "dsn": ""
        },
        "policies": {
            "gates": {
                "require_healthy": True,
                "require_data_ok": True,
                "require_exec_ok": True,
                "require_llm_ok": False,
                "llm_deep_check": False,
                "data_checks": {
                    "last_10min_ticks": True,
                    "last_day_1m_bars_ge": 390
                }
            },
            "ab": {
                "default_weights": {"champion": 0.7, "challenger": 0.3},
                "promote_rules": {
                    "sharpe_gain_ratio": 1.05,
                    "mdd_improve_ratio": 0.90,
                    "min_consistency": 0.65
                }
            },
            "rl_deploy": {
                "min_sharpe": 1.0,
                "max_mdd": 0.20
            },
            "hpo_deploy": {
                "min_oos_sharpe": 1.10,
                "max_oos_mdd": 0.10,
                "min_oos_trades": 30
            }
        },
        "commands": {
            "backfill_yesterday": "echo 'mock backfill'",
            "alpha_loop": "echo 'mock alpha_loop'",
            "apply_params": "echo 'mock apply_params'",
            "rl_train": "echo 'mock rl_train'",
            "rl_eval": "echo 'mock rl_eval'",
            "premarket_fallback": "echo 'mock premarket'"
        },
        "brain": {
            "enabled": False,
            "provider": "deepseek_nvidia",
            "model": "deepseek-ai/deepseek-r1"
        },
        "mlflow": {
            "enabled": False,
            "tracking_uri": "file:./mlruns",
            "experiment": "AI Trader Test"
        }
    }
    config_path = temp_dir / "ai_trader.yaml"
    import yaml
    config_path.write_text(yaml.safe_dump(config))
    return config_path


@pytest.fixture
def mock_gen_summary(temp_dir: Path) -> Path:
    """Mock gen_summary.json from HPO"""
    summary = {
        "stage2": {
            "best_params": {
                "rl.enabled": True,
                "market_filter.conf_floor": 0.65,
                "sizing.multiplier": 2.5
            },
            "oos_summary": {
                "oos_sharpe": 1.25,
                "oos_avg_max_drawdown": 0.08,
                "oos_trades": 45
            }
        }
    }
    gen_dir = temp_dir / "alpha_out" / "daily" / "gen_01"
    gen_dir.mkdir(parents=True, exist_ok=True)
    summary_path = gen_dir / "gen_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    return summary_path


@pytest.fixture(autouse=True)
def mock_env_vars(monkeypatch):
    """Mock environment variables for tests"""
    monkeypatch.setenv("NVIDIA_API_KEY", "test-nvidia-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
