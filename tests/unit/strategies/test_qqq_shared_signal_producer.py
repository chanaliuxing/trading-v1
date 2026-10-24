from datetime import datetime
from types import SimpleNamespace

from strategies.qqq_shared_signal_producer import (
    QQQSharedEntryCandidate,
    publish_qqq_shared_entry_signal,
)


def test_qqq_shared_entry_candidate_payload_contains_expected_fields() -> None:
    candidate = QQQSharedEntryCandidate(
        mode="FAST_DRIFT",
        v3m_signed=0.42,
        atr_eff=0.31,
        atr_live=0.33,
        atr_seed=0.29,
        atr_cap=0.52,
        vol_surge_ratio=1.6,
        distance_from_vwap_atr=0.8,
        data_age_qqq_sec=0.8,
        data_age_qqew_sec=0.9,
        continuity_180s_ok=True,
        qqq_now=501.2,
        vwap_now=500.8,
        qqew_now=99.1,
        qqew_vwap_now=99.0,
    )

    payload = candidate.to_payload()
    assert payload["publish_stage"] == "shared_entry_candidate"
    assert payload["mode"] == "FAST_DRIFT"
    assert payload["v3m_signed"] == 0.42
    assert payload["qqq_now"] == 501.2
    assert payload["qqew_now"] == 99.1


def test_publish_qqq_shared_entry_signal_uses_stock_signal_artifact_api(monkeypatch) -> None:
    captured = {}

    def _fake_publish_stock_entry_signal(**kwargs):
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            signal_family=kwargs["signal_family"],
            strategy_id=kwargs["strategy_id"],
            direction=kwargs["direction"],
            option_side="CALL",
            trigger_ts=kwargs["trigger_ts"],
        )

    monkeypatch.setattr(
        "strategies.qqq_shared_signal_producer.publish_stock_entry_signal",
        _fake_publish_stock_entry_signal,
    )
    monkeypatch.setattr(
        "strategies.qqq_shared_signal_producer.signal_plane_health",
        lambda: {"state": "healthy_memory", "backend_config": "memory"},
    )

    candidate = QQQSharedEntryCandidate(
        mode="FAST_DRIFT",
        v3m_signed=0.42,
        atr_eff=0.31,
        atr_live=0.33,
        atr_seed=0.29,
        atr_cap=0.52,
        vol_surge_ratio=1.6,
        distance_from_vwap_atr=0.8,
        data_age_qqq_sec=0.8,
        data_age_qqew_sec=0.9,
        continuity_180s_ok=True,
        qqq_now=501.2,
        vwap_now=500.8,
        qqew_now=99.1,
        qqew_vwap_now=99.0,
    )
    now = datetime(2026, 4, 10, 10, 9, 2)

    artifact, health = publish_qqq_shared_entry_signal(
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        trigger_id="t1",
        decision_id="d1",
        trace_id="x1",
        candidate=candidate,
    )

    assert captured["kwargs"]["underlying"] == "QQQ"
    assert captured["kwargs"]["strategy_id"] == "qqq_intraday_001"
    assert captured["kwargs"]["direction"] == "LONG"
    assert captured["kwargs"]["instrument"] == "TQQQ"
    assert captured["kwargs"]["decision_id"] == "d1"
    assert captured["kwargs"]["payload"]["publish_stage"] == "shared_entry_candidate"
    assert getattr(artifact, "signal_family") == "qqq_intraday_entry"
    assert health["state"] == "healthy_memory"

