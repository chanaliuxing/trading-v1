from __future__ import annotations

import tool.sync_ibkr_realtime as sync_mod


class _FakeIB:
    attempts: list[dict[str, object]] = []

    def connect(self, host, port, clientId, timeout=None, readonly=False):
        self.attempts.append(
            {
                "host": host,
                "port": port,
                "clientId": clientId,
                "timeout": timeout,
                "readonly": readonly,
            }
        )
        if len(self.attempts) == 1:
            raise TimeoutError("first client id timed out")

    def disconnect(self):
        return None


def test_connect_ibkr_retries_candidate_client_ids(monkeypatch):
    _FakeIB.attempts = []
    monkeypatch.setattr(sync_mod, "IB", _FakeIB)
    monkeypatch.setattr(sync_mod, "CLIENT_ID", 506)
    monkeypatch.setattr(sync_mod, "CONNECT_TIMEOUT_SECONDS", 12.0)
    monkeypatch.setattr(sync_mod, "_run_system_process_active", lambda: False)

    class _FakeRegistry:
        def resolve(self, role: str) -> int:
            mapping = {"account_sync": 507, "master": 609}
            return mapping[role]

    monkeypatch.setattr(sync_mod, "get_client_id_registry", lambda: _FakeRegistry())

    ib = sync_mod.connect_ibkr()

    assert isinstance(ib, _FakeIB)
    assert [attempt["clientId"] for attempt in _FakeIB.attempts] == [506, 507]
    assert all(attempt["readonly"] is True for attempt in _FakeIB.attempts)
    assert all(attempt["timeout"] == 12.0 for attempt in _FakeIB.attempts)


def test_connect_ibkr_refuses_independent_session_when_run_system_is_active(monkeypatch):
    _FakeIB.attempts = []
    monkeypatch.setattr(sync_mod, "IB", _FakeIB)
    monkeypatch.setattr(sync_mod, "ALLOW_INDEPENDENT_SESSION", False)
    monkeypatch.setattr(sync_mod, "_run_system_process_active", lambda: True)

    try:
        sync_mod.connect_ibkr()
    except RuntimeError as exc:
        assert "run_system_active_refusing_independent_ib_session" in str(exc)
    else:
        raise AssertionError("expected connect_ibkr to refuse an independent live IB session")

    assert _FakeIB.attempts == []
