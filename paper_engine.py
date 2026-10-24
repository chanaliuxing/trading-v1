# paper_engine.py
"""
Shadow/Paper Trading Engine

Usage:
  python paper_engine.py --config config.json --feed csv --feed-path data/live_feed.csv

Features:
- Uses same SimAdapter/PortfolioOverlay as backtest
- Circuit breakers (drawdown halt)
- Alerting via webhooks
- Optional ClickHouse persistence
- Shadow trading only (no real broker execution)
"""
from __future__ import annotations
import os, json, argparse, asyncio, signal, time
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Dict, AsyncGenerator, Optional

import pandas as pd

import boot_logging  # noqa: F401

from sim_adapter import SimAdapter
from portfolio_overlay import PortfolioOverlay, OverlayConfig
from obs.audit import log_event

try:
    from opentelemetry import trace
except Exception:  # pragma: no cover
    trace = None  # type: ignore

_TRACER = trace.get_tracer("trader.paper") if trace else None


# --------- Alerts ---------
class AlertManager:
    def __init__(self, webhooks: list[str] | None = None, enabled: bool = True):
        self.webhooks = webhooks or []
        self.enabled = enabled

    def send(self, title: str, text: str):
        msg = f"[ALERT] {title}: {text}"
        print(msg, flush=True)
        if not self.enabled or not self.webhooks:
            return
        try:
            import urllib.request, json as _json
            for url in self.webhooks:
                req = urllib.request.Request(
                    url,
                    data=_json.dumps({"text": msg}).encode("utf-8"),
                    headers={"Content-Type":"application/json"}
                )
                urllib.request.urlopen(req, timeout=2)
        except Exception:
            pass


# --------- ClickHouse sink (optional) ---------
def _ch_client_or_none(cfg: Dict[str, Any]):
    sink = cfg.get("sink_clickhouse", {})
    if not sink or not sink.get("enabled"):
        return None
    try:
        import clickhouse_connect
        return clickhouse_connect.get_client(
            host=sink.get("host","localhost"),
            port=sink.get("port",8123),
            username=sink.get("username","default"),
            password=sink.get("password",""),
            database=sink.get("database","default"),
        )
    except Exception as e:
        print(f"[WARN] ClickHouse client not available: {e}")
        return None


def _ch_ensure_live_tables(client):
    if client is None:
        return
    try:
        client.command("""
        CREATE TABLE IF NOT EXISTS live_runs (
          run_id String,
          mode LowCardinality(String),
          ts DateTime DEFAULT now(),
          nav Float64,
          peak_nav Float64,
          dd Float64,
          halted UInt8,
          notes String
        ) ENGINE=MergeTree ORDER BY (run_id, ts)
        """)
        client.command("""
        CREATE TABLE IF NOT EXISTS live_orders (
          run_id String,
          ts_ms UInt64,
          symbol String,
          side LowCardinality(String),
          qty Int32,
          ref_px Float64,
          fill_px Float64,
          fee Float64
        ) ENGINE=MergeTree ORDER BY (run_id, ts_ms)
        """)
        client.command("""
        CREATE TABLE IF NOT EXISTS live_nav (
          run_id String,
          ts_ms UInt64,
          nav Float64
        ) ENGINE=MergeTree ORDER BY (run_id, ts_ms)
        """)
    except Exception as e:
        print(f"[WARN] Failed to create ClickHouse tables: {e}")


# --------- Data Feeds ---------
async def csv_feed(path: str) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Read CSV or Parquet file and yield features row by row.
    Required columns: ts (ms), symbol, open, high, low, close, volume
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)

    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)

    df = df.sort_values("ts")
    for _, row in df.iterrows():
        yield row.to_dict()


# --------- Paper Engine ---------
class PaperEngine:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.run_id = cfg.get("run_id") or f"paper_{int(time.time())}"

        # Create SimAdapter with PortfolioOverlay
        overlay_cfg = OverlayConfig(**cfg.get("overlay", {})) if cfg.get("overlay") else OverlayConfig()
        self.sim = SimAdapter(
            commission_per_share=cfg.get("commission_per_share",0.005),
            commission_min=cfg.get("commission_min",1.0),
            slippage_bps=cfg.get("slippage_bps",1.0),
            fill_ref=cfg.get("fill_ref","open"),
            overlay=PortfolioOverlay(overlay_cfg)
        )

        self.alerts = AlertManager(
            cfg.get("alerts", {}).get("webhooks", []),
            cfg.get("alerts", {}).get("enabled", True)
        )
        self.dd_halt = float(cfg.get("halt", {}).get("max_drawdown", 0.35))
        self.min_nav = float(cfg.get("halt", {}).get("min_nav", 1000.0))

        self.client = _ch_client_or_none(cfg)
        _ch_ensure_live_tables(self.client)

    async def run(self, feed_kind: str, feed_path: str):
        """Run paper trading loop."""
        # Setup data feed
        if feed_kind.lower() in ("csv","parquet"):
            agen = csv_feed(feed_path)
        else:
            raise ValueError(f"unsupported feed: {feed_kind}")

        # Setup signal handlers
        stop_flag = [False]
        loop = asyncio.get_running_loop()

        def set_stop():
            stop_flag[0] = True

        for s in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(s, set_stop)
            except Exception:
                pass

        # Main loop
        async for feat in agen:
            if stop_flag[0]:
                break

            span_cm = _TRACER.start_as_current_span(
                "bar",
                attributes={
                    "symbol": str(feat.get("symbol")),
                    "ts": str(feat.get("ts"))
                },
            ) if _TRACER else nullcontext()

            with span_cm:
                # 1) Feed to SimAdapter for MTM
                await self.sim.on_market_data(feat)

                # 2) Strategy callback would go here
                # For paper engine without strategy, we just monitor

                # 3) Circuit breaker & logging
                nav = self.sim.snapshot()["equity"]
                peak = self.sim.overlay.state.peak_nav
                dd = 0.0 if peak <= 0 else (peak - nav)/peak
                halted = dd >= self.dd_halt or nav <= self.min_nav or self.sim.overlay.state.halted

                if halted:
                    log_event(
                        "halt_triggered",
                        component="PaperEngine",
                        run_id=self.run_id,
                        nav=float(nav),
                        peak=float(peak),
                        drawdown=float(dd),
                    )
                    self.alerts.send("HALT", f"run={self.run_id} dd={dd:.3f} nav={nav:.2f}")
                    self._insert_live_runs(nav, peak, dd, 1, notes="halt")
                    break

                # Regular persistence
                self._insert_live_runs(nav, peak, dd, 0, notes="")
                self._flush_fills_to_ch()

        # Cleanup
        self._flush_fills_to_ch(final=True)
        print(f"Paper engine stopped. Final NAV: {self.sim.snapshot()['equity']:.2f}")

    def _insert_live_runs(self, nav: float, peak_nav: float, dd: float, halted: int, notes: str):
        if self.client is None:
            return
        try:
            row = [[self.run_id, "paper", float(nav), float(peak_nav), float(dd), int(halted), notes or ""]]
            self.client.insert("live_runs", row, column_names=[
                "run_id","mode","nav","peak_nav","dd","halted","notes"
            ])
        except Exception as e:
            print(f"[WARN] Failed to insert live_runs: {e}")

    def _flush_fills_to_ch(self, final: bool=False):
        if self.client is None:
            return
        try:
            batch = []
            ledger = self.sim._closed_trades if hasattr(self.sim, '_closed_trades') else []
            for ev in ledger:
                ts = int(ev.get("exit_ts") or int(time.time()*1000))
                fill_px = float(ev.get("exit_px", 0.0))
                pnl = float(ev.get("pnl", 0.0))
                sym = str(ev.get("symbol",""))
                batch.append([self.run_id, ts, sym, "CLOSE", 0, fill_px, fill_px, pnl])

            if batch:
                self.client.insert("live_orders", batch, column_names=[
                    "run_id","ts_ms","symbol","side","qty","ref_px","fill_px","fee"
                ])
        except Exception as e:
            print(f"[WARN] Failed to flush fills: {e}")


# --------- CLI ---------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Config JSON file")
    ap.add_argument("--feed", choices=["csv","parquet"], required=True, help="Feed type")
    ap.add_argument("--feed-path", required=True, help="Path to feed data")
    args = ap.parse_args()

    cfg = json.load(open(args.config,"r",encoding="utf-8"))
    pe = PaperEngine(cfg)
    asyncio.run(pe.run(args.feed, args.feed_path))


if __name__ == "__main__":
    main()
