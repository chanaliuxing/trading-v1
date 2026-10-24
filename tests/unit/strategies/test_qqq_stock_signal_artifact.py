from datetime import datetime

import strategies.qqq_stock_signal_artifact as artifact_mod
from strategies.qqq_stock_signal_artifact import (
    clear_stock_entry_signal_artifacts,
    get_latest_stock_entry_signal,
    publish_stock_entry_signal,
    signal_plane_health,
)


def test_publish_and_get_latest_stock_entry_signal() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 9, 2)
    publish_stock_entry_signal(
        underlying="qqq",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
        trigger_id="t1",
        decision_id="d1",
        trace_id="x1",
    )
    artifact = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_001",
    )
    assert artifact is not None
    assert artifact.direction == "LONG"
    assert artifact.option_side == "CALL"
    assert artifact.entry_allowed is True
    assert artifact.trigger_id == "t1"


def test_get_latest_stock_entry_signal_filters_strategy_id() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 10, 9, 2)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="SHORT",
        instrument="SQQQ",
        entry_allowed=True,
    )
    artifact = get_latest_stock_entry_signal(
        underlying="QQQ",
        source_strategy_id="another_strategy",
    )
    assert artifact is None


def test_latest_without_strategy_id_picks_latest_trigger_ts() -> None:
    clear_stock_entry_signal_artifacts()
    t0 = datetime(2026, 4, 8, 10, 0, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=t0,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_002",
        trigger_ts=t0.replace(minute=1),
        direction="SHORT",
        instrument="SQQQ",
        entry_allowed=True,
    )
    artifact = get_latest_stock_entry_signal(underlying="QQQ")
    assert artifact is not None
    assert artifact.strategy_id == "qqq_intraday_002"
    assert artifact.option_side == "PUT"


def test_artifact_payload_strips_order_ownership_fields() -> None:
    clear_stock_entry_signal_artifacts()
    now = datetime(2026, 4, 8, 11, 0, 0)
    artifact = publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
        payload={
            "v3m_signed": 0.7,
            "order_ref": "optk:bad",
            "position_lot_id": "lot:bad",
            "target_qty": 2,
        },
    )
    p = dict(artifact.payload or {})
    assert p.get("v3m_signed") == 0.7
    assert "order_ref" not in p
    assert "position_lot_id" not in p
    assert "target_qty" not in p


def test_signal_artifact_redis_backend_round_trip(monkeypatch) -> None:
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "redis")

    class _FakeRedis:
        def __init__(self) -> None:
            self.hashes = {}
            self.expire_calls = []

        def hset(self, name: str, key: str, value: str) -> int:
            self.hashes.setdefault(name, {})[key] = value
            return 1

        def hget(self, name: str, key: str):
            return self.hashes.get(name, {}).get(key)

        def hgetall(self, name: str):
            return dict(self.hashes.get(name, {}))

        def expire(self, name: str, sec: int) -> bool:
            self.expire_calls.append((name, sec))
            return True

        def health_check(self) -> bool:
            return True

    fake = _FakeRedis()
    monkeypatch.setattr(artifact_mod, "_redis_client", lambda: fake)
    now = datetime(2026, 4, 9, 10, 10, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    row = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_001",
    )
    assert row is not None
    assert row.direction == "LONG"
    assert row.option_side == "CALL"
    assert len(fake.expire_calls) >= 1


def test_signal_artifact_auto_backend_fallback_to_memory(monkeypatch) -> None:
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "auto")
    monkeypatch.setattr(artifact_mod, "_redis_client", lambda: None)
    now = datetime(2026, 4, 9, 10, 11, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="SHORT",
        instrument="SQQQ",
        entry_allowed=True,
    )
    row = get_latest_stock_entry_signal(underlying="QQQ")
    assert row is not None
    assert row.direction == "SHORT"
    health = signal_plane_health()
    assert health["backend_config"] == "auto"
    assert health["fallback_memory_enabled"] is True


def test_clear_stock_signal_artifacts_remote_delete(monkeypatch) -> None:
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "redis")

    class _FakeRedis:
        def __init__(self) -> None:
            self.hashes = {}
            self.deleted = []

        def hset(self, name: str, key: str, value: str) -> int:
            self.hashes.setdefault(name, {})[key] = value
            return 1

        def hget(self, name: str, key: str):
            return self.hashes.get(name, {}).get(key)

        def hgetall(self, name: str):
            return dict(self.hashes.get(name, {}))

        def expire(self, name: str, sec: int) -> bool:
            return True

        def delete(self, key: str) -> int:
            self.deleted.append(key)
            self.hashes.pop(key, None)
            return 1

        def hdel(self, name: str, key: str) -> int:
            if name in self.hashes and key in self.hashes[name]:
                self.hashes[name].pop(key, None)
                return 1
            return 0

        def health_check(self) -> bool:
            return True

    fake = _FakeRedis()
    monkeypatch.setattr(artifact_mod, "_redis_client", lambda: fake)
    now = datetime(2026, 4, 9, 10, 15, 0)
    publish_stock_entry_signal(
        underlying="QQQ",
        strategy_id="qqq_intraday_001",
        trigger_ts=now,
        direction="LONG",
        instrument="TQQQ",
        entry_allowed=True,
    )
    clear_stock_entry_signal_artifacts(clear_remote=True, underlying="QQQ")
    row = get_latest_stock_entry_signal(
        underlying="QQQ",
        signal_family="qqq_intraday_entry",
        source_strategy_id="qqq_intraday_001",
    )
    assert row is None
    assert len(fake.deleted) >= 1


def test_redis_client_retry_after_failure_window(monkeypatch) -> None:
    clear_stock_entry_signal_artifacts()
    monkeypatch.setenv("QQQ_STOCK_SIGNAL_BACKEND", "redis")
    monkeypatch.setattr(artifact_mod, "_REDIS_CLIENT", None)
    monkeypatch.setattr(artifact_mod, "_REDIS_DISABLED", False)
    monkeypatch.setattr(artifact_mod, "_REDIS_RETRY_AT_MONO", 0.0)
    monkeypatch.setattr(artifact_mod, "_REDIS_ERROR_STREAK", 0)

    class _FakeRedis:
        def hset(self, name: str, key: str, value: str) -> int:
            return 1

        def expire(self, name: str, sec: int) -> bool:
            return True

        def hget(self, name: str, key: str):
            return None

        def hgetall(self, name: str):
            return {}

        def health_check(self) -> bool:
            return True

    state = {"n": 0}

    def _builder():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("redis down")
        return _FakeRedis()

    monkeypatch.setattr(artifact_mod, "_build_redis_client", _builder)
    c1 = artifact_mod._redis_client()
    assert c1 is None
    monkeypatch.setattr(artifact_mod, "_REDIS_RETRY_AT_MONO", 0.0)
    c2 = artifact_mod._redis_client()
    assert c2 is not None
