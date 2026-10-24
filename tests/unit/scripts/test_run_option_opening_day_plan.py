from scripts.run_option_opening_day_plan import _step_commands


def test_step_commands_preopen_contains_readiness_and_gate():
    steps = _step_commands(phase="preopen", date="2026-04-06", state_start="2026-04-03", max_days=10)
    names = [name for name, _ in steps]
    assert names == ["spy_readiness_check", "active_evidence", "launch_gate"]


def test_step_commands_intraday_contains_cycle():
    steps = _step_commands(phase="intraday", date="2026-04-06", state_start="2026-04-03", max_days=10)
    assert len(steps) == 1
    assert steps[0][0] == "launch_readiness_cycle"


def test_step_commands_eod_contains_batch_and_gate():
    steps = _step_commands(phase="eod", date="2026-04-06", state_start="2026-04-03", max_days=10)
    names = [name for name, _ in steps]
    assert names == ["qqq_state_shadow_batch", "active_evidence", "launch_gate"]
