from services.notifications.factory import build_notification_manager


def test_build_notification_manager_merges_enabled_strategies_into_allowlist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.delenv("TELEGRAM_ALLOWLIST_SYNC_ENABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_STRATEGY_ALLOWLIST", raising=False)

    cfg = {
        "notifications": {
            "enabled": True,
            "telegram": {
                "enabled": True,
                "strategy_allowlist": ["qqq_intraday_001"],
            },
        },
        "strategies": [
            {"id": "qqq_intraday_001", "enabled": True},
            {"id": "qqq_1dte_ma10_reject_0324_v2_001", "enabled": True},
            {"id": "disabled_one", "enabled": False},
        ],
    }

    manager = build_notification_manager(cfg)
    assert manager is not None
    assert "qqq_intraday_001" in manager.strategy_allowlist
    assert "qqq_1dte_ma10_reject_0324_v2_001" in manager.strategy_allowlist
    assert "disabled_one" not in manager.strategy_allowlist
    assert "system_reconciler" in manager.strategy_allowlist


def test_build_notification_manager_does_not_sync_enabled_strategies_when_disabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setenv("TELEGRAM_ALLOWLIST_SYNC_ENABLED", "0")
    monkeypatch.delenv("TELEGRAM_STRATEGY_ALLOWLIST", raising=False)

    cfg = {
        "notifications": {
            "enabled": True,
            "telegram": {
                "enabled": True,
                "strategy_allowlist": ["qqq_intraday_001"],
            },
        },
        "strategies": [
            {"id": "qqq_intraday_001", "enabled": True},
            {"id": "qqq_1dte_ma10_reject_0324_v2_001", "enabled": True},
        ],
    }

    manager = build_notification_manager(cfg)
    assert manager is not None
    assert manager.strategy_allowlist == {"qqq_intraday_001"}
