from __future__ import annotations

from strategies.stocks_kernel.types import DataReadinessReport, HistoryBundle, MarketSnapshot


def evaluate_data_readiness(
    *,
    snapshot: MarketSnapshot | None,
    history: HistoryBundle | None,
    now_epoch_s: float,
    max_staleness_sec: float,
    min_history_bars: int = 1,
) -> DataReadinessReport:
    if snapshot is None:
        return DataReadinessReport(
            ready=False,
            reason="snapshot_missing",
            freshness_ok=False,
            history_ready=bool(history and len(history.bars_1m) >= min_history_bars),
            snapshot_ready=False,
            payload={"min_history_bars": int(min_history_bars)},
        )
    age_sec = max(0.0, float(now_epoch_s) - float(snapshot.ts_epoch_s))
    freshness_ok = age_sec <= float(max_staleness_sec)
    history_count = len(history.bars_1m) if history is not None else 0
    history_ready = history_count >= int(min_history_bars)
    snapshot_ready = snapshot.last_price is not None or snapshot.minute_close is not None
    ready = freshness_ok and history_ready and snapshot_ready
    reason = "ready"
    if not freshness_ok:
        reason = "snapshot_stale"
    elif not history_ready:
        reason = "history_missing"
    elif not snapshot_ready:
        reason = "snapshot_price_missing"
    return DataReadinessReport(
        ready=ready,
        reason=reason,
        freshness_ok=freshness_ok,
        history_ready=history_ready,
        snapshot_ready=snapshot_ready,
        payload={"age_sec": age_sec, "history_bars": history_count, "min_history_bars": int(min_history_bars)},
    )
