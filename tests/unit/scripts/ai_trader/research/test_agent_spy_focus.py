from __future__ import annotations

from types import SimpleNamespace

from scripts.ai_trader.research.agent import ResearchAgent


def test_spy_focus_builds_spy_only_stock_fallback(monkeypatch) -> None:
    monkeypatch.setenv("AI_TRADER_RESEARCH_FOCUS", "spy_scalp")
    agent = ResearchAgent(llm_client=SimpleNamespace(chat=lambda **_: None), pool=None)

    specs = agent._build_strategy_experiment_specs([], cycle_id="2026-04-11")

    assert specs
    assert all(str(s.get("market")) == "stocks" for s in specs)
    first = specs[0]
    assert first["universe"]["symbols"] == ["SPY"]
    assert first["strategy_ref"]["template_id"] == "trend_follow_equity"


def test_build_auto_remediation_jobs_from_readiness_report() -> None:
    agent = ResearchAgent(llm_client=SimpleNamespace(chat=lambda **_: None), pool=None)
    report = {
        "summary": {"total": 1, "ready": 0, "degraded": 0, "blocked": 1},
        "items": [
            {
                "status": "blocked",
                "reason": "no_bars_found",
                "symbol": "SPY",
                "window": {"start": "2026-03-01", "end": "2026-03-31"},
            }
        ],
    }

    jobs = agent._build_auto_remediation_jobs(report)

    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_type"] == "backfill_bars"
    assert job["params"]["symbols"] == ["SPY"]
    assert job["params"]["start_date"] == "2026-03-01"
    assert job["params"]["end_date"] == "2026-03-31"
