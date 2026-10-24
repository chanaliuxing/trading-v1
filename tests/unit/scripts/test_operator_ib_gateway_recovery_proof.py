from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.operator_ib_gateway_recovery_proof as cli
from services.ibkr import ib_gateway_circuit, ib_gateway_recovery_coordinator


ACK = cli.DEFAULT_ACK


class _FakeIBProofOK:
    constructed = 0

    def __init__(self):
        type(self).constructed += 1
        self.connected = False
        self.client_ids: list[int] = []
        self.disconnect_calls = 0

    async def connectAsync(self, host, port, clientId, timeout):
        self.connected = True
        self.client_ids.append(clientId)

    async def reqCurrentTimeAsync(self):
        return datetime.fromisoformat("2026-05-12T10:00:00+00:00")

    def positions(self):
        return []

    async def reqOpenOrdersAsync(self):
        return []

    def openTrades(self):
        return []

    def disconnect(self):
        self.connected = False
        self.disconnect_calls += 1


class _FakeIBProofFailsPositions(_FakeIBProofOK):
    def positions(self):
        raise RuntimeError("positions unavailable")


@pytest.fixture(autouse=True)
def _isolated_gateway_state(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_GATEWAY_CIRCUIT_STATE_PATH", str(tmp_path / "ib_gateway_circuit.json"))
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "operator_cli")
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_PROOF_LEASE_SEC", "60")
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "0")
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None
    _FakeIBProofOK.constructed = 0
    yield
    ib_gateway_circuit._CIRCUIT = None
    ib_gateway_recovery_coordinator._COORDINATOR = None


def _artifact(tmp_path: Path) -> Path:
    return tmp_path / "runtime/recovery/ib_gateway_operator_proof/latest.json"


def _manual_required() -> None:
    ib_gateway_circuit.get_ib_gateway_connection_circuit().mark_manual_required(
        reason="manual_gateway_api_recovery_required",
        source="market:pre_connect_api_probe",
        metadata={"gateway_reason": "gateway_api_half_open"},
    )


def _args(tmp_path: Path, *extra: str):
    return cli.build_parser().parse_args(
        [
            "--source",
            "operator_cli",
            "--host",
            "127.0.0.1",
            "--port",
            "4002",
            "--client-id",
            "98",
            "--timeout-sec",
            "1",
            "--artifact-out",
            str(_artifact(tmp_path)),
            *extra,
        ]
    )


@pytest.mark.asyncio
async def test_operator_proof_cli_dry_run_does_not_construct_ib(tmp_path):
    _manual_required()
    factory = MagicMock(side_effect=AssertionError("dry-run must not construct IB"))

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path), ib_factory=factory)

    assert exit_code == 0
    assert payload["result"] == "DRY_RUN"
    assert payload["live_mode"] is False
    assert payload["proof"]["current_time"] == "not_run"
    assert payload["tcp_port"]["status"] == "not_run"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_operator_proof_cli_requires_live_flag(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")
    factory = MagicMock(side_effect=AssertionError("missing --live must not construct IB"))

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path, "--ack", ACK), ib_factory=factory)

    assert exit_code == 0
    assert payload["result"] == "DRY_RUN"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_operator_proof_cli_requires_ack(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")
    factory = MagicMock(side_effect=AssertionError("missing ack must not construct IB"))

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path, "--live"), ib_factory=factory)

    assert exit_code == 3
    assert payload["result"] == "MISSING_ACK"
    assert payload["tcp_port"]["status"] == "not_run"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_operator_proof_cli_requires_authorized_source(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")
    monkeypatch.setenv("IB_GATEWAY_RECOVERY_AUTHORIZED_PROBE_SOURCES", "another_source")
    factory = MagicMock(side_effect=AssertionError("unauthorized source must not construct IB"))

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path, "--live", "--ack", ACK), ib_factory=factory)

    assert exit_code == 3
    assert payload["result"] == "UNAUTHORIZED"
    assert payload["tcp_port"]["status"] == "not_run"
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_operator_proof_cli_lease_denied_does_not_construct_ib(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")
    circuit = ib_gateway_circuit.get_ib_gateway_connection_circuit()
    circuit.try_acquire_manual_recovery_proof_lease(
        source="other_operator",
        lease_sec=60,
        now_ts=datetime.fromisoformat("2026-05-12T10:00:00+00:00").timestamp(),
    )
    factory = MagicMock(side_effect=AssertionError("lease denied must not construct IB"))

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path, "--live", "--ack", ACK), ib_factory=factory)

    assert exit_code == 4
    assert payload["result"] == "LEASE_DENIED"
    assert payload["lease_acquired"] is False
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_operator_proof_cli_success_requires_time_positions_open_orders(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path, "--live", "--ack", ACK), ib_factory=_FakeIBProofOK)

    assert exit_code == 0
    assert payload["result"] == "SUCCESS"
    assert payload["tcp_port"]["status"] == "skipped_fake_ib_factory"
    assert payload["proof"] == {
        "api_handshake": "ok",
        "current_time": "ok",
        "positions": "ok",
        "open_orders": "ok",
    }
    assert _FakeIBProofOK.constructed == 1


@pytest.mark.asyncio
async def test_operator_proof_cli_failure_does_not_clear_manual_required(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")

    exit_code, payload = await cli.run_operator_proof(
        _args(tmp_path, "--live", "--ack", ACK),
        ib_factory=_FakeIBProofFailsPositions,
    )

    assert exit_code == 2
    assert payload["result"] == "FAILED"
    assert payload["proof"]["positions"] == "failed"
    assert ib_gateway_circuit.get_ib_gateway_connection_circuit().snapshot().manual_required is True


@pytest.mark.asyncio
async def test_operator_proof_cli_success_clears_manual_required(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path, "--live", "--ack", ACK), ib_factory=_FakeIBProofOK)

    assert exit_code == 0
    assert payload["manual_required_cleared"] is True
    assert ib_gateway_circuit.get_ib_gateway_connection_circuit().snapshot().manual_required is False


@pytest.mark.asyncio
async def test_operator_proof_cli_can_verify_when_manual_required_already_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")

    exit_code, payload = await cli.run_operator_proof(
        _args(tmp_path, "--live", "--ack", ACK, "--allow-when-not-manual-required"),
        ib_factory=_FakeIBProofOK,
    )

    assert exit_code == 0
    assert payload["result"] == "SUCCESS"
    assert payload["manual_required_cleared"] is False
    assert payload["proof"]["api_handshake"] == "ok"


@pytest.mark.asyncio
async def test_operator_proof_cli_writes_latest_artifact(tmp_path, monkeypatch):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")
    artifact = _artifact(tmp_path)

    exit_code, payload = await cli.run_operator_proof(_args(tmp_path, "--live", "--ack", ACK), ib_factory=_FakeIBProofOK)

    assert exit_code == 0
    written = json.loads(artifact.read_text(encoding="utf-8"))
    assert written["result"] == "SUCCESS"
    assert written["artifact_path"] == str(artifact)
    assert payload["artifact_path"] == str(artifact)


def test_operator_proof_cli_outputs_json(tmp_path, monkeypatch, capsys):
    _manual_required()
    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")

    exit_code = cli.main(
        [
            "--live",
            "--ack",
            ACK,
            "--source",
            "operator_cli",
            "--artifact-out",
            str(_artifact(tmp_path)),
        ],
        ib_factory=_FakeIBProofOK,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["result"] == "SUCCESS"


def test_operator_proof_dry_run_script_no_live(monkeypatch):
    env = dict(os.environ)
    env["TRADING_TEST_ALLOW_LIVE_IBKR"] = "0"
    result = subprocess.run(
        ["bash", "scripts/check_ib_operator_proof_dry_run.sh"],
        cwd=cli.REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    combined = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, combined
    assert "operator proof dry-run smoke: DRY_RUN no-live ok" in result.stdout
    assert "Connected to IBKR" not in combined
    assert "Connecting to 127.0.0.1" not in combined


@pytest.mark.asyncio
async def test_operator_proof_cli_exit_codes(tmp_path, monkeypatch):
    _manual_required()
    missing_env_code, missing_env = await cli.run_operator_proof(
        _args(tmp_path, "--live", "--ack", ACK),
        ib_factory=MagicMock(side_effect=AssertionError("env block must not construct IB")),
    )
    assert missing_env_code == 3
    assert missing_env["result"] == "LIVE_OPT_IN_MISSING"

    monkeypatch.setenv("TRADING_TEST_ALLOW_LIVE_IBKR", "1")
    fail_code, fail_payload = await cli.run_operator_proof(
        _args(tmp_path, "--live", "--ack", ACK),
        ib_factory=_FakeIBProofFailsPositions,
    )
    assert fail_code == 2
    assert fail_payload["result"] == "FAILED"
