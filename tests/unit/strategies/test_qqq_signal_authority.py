from datetime import datetime

from strategies.qqq_signal_authority import QQQSignalAuthority


def test_qqq_signal_authority_persists_snapshot(tmp_path):
    authority = QQQSignalAuthority(snapshot_root=tmp_path)

    result = authority.publish_entry_snapshot(
        strategy_id="qqq_intraday_001",
        decision_id="decision-1",
        trigger_id="trigger-1",
        trace_id="trace-1",
        ts=datetime(2026, 4, 21, 9, 35),
        direction="LONG",
        exec_symbol="TQQQ",
        signal_state={"atr_eff": 1.2, "v3m_signed": 0.4},
    )

    path = tmp_path / "qqq_signal_snapshots_20260421.jsonl"
    assert path.exists()
    assert result["signal_authority"]["direction"] == "LONG"
    assert result["signal_authority"]["snapshot_kind"] == "entry"
    assert '"atr_eff": 1.2' in path.read_text(encoding="utf-8")


def test_qqq_signal_authority_persists_evaluation_snapshot(tmp_path):
    authority = QQQSignalAuthority(snapshot_root=tmp_path)

    result = authority.publish_evaluation_snapshot(
        strategy_id="qqq_intraday_001",
        decision_id="decision-eval-1",
        trigger_id="",
        trace_id="trace-eval-1",
        ts=datetime(2026, 4, 21, 9, 36),
        allow=False,
        side="SHORT",
        reason="NO_DUAL_SETUP_MATCH",
        exec_symbol="SQQQ",
        signal_state={"mode": "FAST", "gate_reason": "NO_DUAL_SETUP_MATCH"},
    )

    path = tmp_path / "qqq_signal_snapshots_20260421.jsonl"
    text = path.read_text(encoding="utf-8")
    assert result["signal_authority"]["snapshot_kind"] == "evaluation"
    assert result["signal_authority"]["signal_authority"]["allow"] is False
    assert '"gate_reason": "NO_DUAL_SETUP_MATCH"' in text
