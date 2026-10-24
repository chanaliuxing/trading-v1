from pathlib import Path

from tool.ops.check_ib_request_governance import build_report


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_ib_request_governance_flags_ungoverned_runtime_call(tmp_path: Path) -> None:
    _write(
        tmp_path / "strategies/bad_strategy.py",
        """
async def run(ib, contract):
    await ib.qualifyContractsAsync(contract)
""",
    )

    report = build_report(repo_root=tmp_path)

    assert report["status"] == "FAIL"
    assert report["violations"][0]["path"] == "strategies/bad_strategy.py"


def test_ib_request_governance_accepts_governed_runtime_call(tmp_path: Path) -> None:
    _write(
        tmp_path / "strategies/good_strategy.py",
        """
async def run(manager, ib, contract):
    decision = manager.allow_market_request(kind='x', key='y', requester='test')
    if not decision.allowed:
        return None
    result = await ib.qualifyContractsAsync(contract)
    manager.record_market_request_result(kind='x', key='y', ok=bool(result))
    return result
""",
    )

    report = build_report(repo_root=tmp_path)

    assert report["status"] == "PASS"
    assert report["governed_callers"][0]["path"] == "strategies/good_strategy.py"


def test_ib_request_governance_accepts_approved_broker_module(tmp_path: Path) -> None:
    _write(
        tmp_path / "services/market/options_data.py",
        """
async def run(ib, contract):
    return ib.reqMktData(contract)
""",
    )

    report = build_report(repo_root=tmp_path)

    assert report["status"] == "PASS"
    assert report["approved_broker_modules"][0]["path"] == "services/market/options_data.py"
