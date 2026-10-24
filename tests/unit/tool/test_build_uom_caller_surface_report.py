from pathlib import Path

from tool.report.build_uom_caller_surface_report import build_uom_caller_surface_report, evaluate_uom_caller_gate


def test_build_uom_caller_surface_report_flags_production_direct_call(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    strategy_dir = repo / "strategies"
    tool_dir = repo / "tool"
    strategy_dir.mkdir(parents=True)
    tool_dir.mkdir(parents=True)
    (strategy_dir / "demo.py").write_text("async def f(ctx):\n    await ctx.submit_intent(x)\n", encoding="utf-8")
    (strategy_dir / "bad.py").write_text("from services.exec import ExecutionService\n", encoding="utf-8")
    (tool_dir / "demo_tool.py").write_text("from services.exec import ExecutionService\n", encoding="utf-8")

    report = build_uom_caller_surface_report(repo)

    assert report["summary"]["caller_count"] == 3
    assert report["summary"]["production_pending_count"] == 1
    assert report["summary"]["routed_by_pipeline_count"] == 1
    assert str(strategy_dir / "bad.py") in report["production_pending_files"]
    gate = evaluate_uom_caller_gate(report)
    assert gate["ok"] is False
    assert gate["blocking_files"] == [str(strategy_dir / "bad.py")]
