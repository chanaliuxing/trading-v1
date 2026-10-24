from libs.evolution.golden_runner import run_pure_skill


def test_golden_runner_dummy_math_matches():
    res = run_pure_skill("dummy_math")
    assert res["ok"] is True
    assert res["results"][0]["ok"] is True
