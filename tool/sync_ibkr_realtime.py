#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IBKR 实时数据同步服务
每5分钟从 IBKR 拉取实时账户数据并更新到 ClickHouse

同步内容:
1. 账户总览 (Net Liquidation, Cash, Position Value, Unrealized/Realized PnL, Buying Power)
2. 当前持仓 (总持仓，实时价格，实时盈亏)
3. 今日成交 (已由交易系统自动记录)

运行方式:
  python sync_ibkr_realtime.py          # 运行一次
  python sync_ibkr_realtime.py --loop   # 循环运行（每5分钟）
"""

import os
import sys
import math
import time
import argparse
import subprocess
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from ib_insync import IB, Stock
from libs.db.clickhouse import ClickHouseDB
from services.ibkr.client_id_registry import get_client_id_registry

ET = ZoneInfo("America/New_York")

# =============================================================================
# Configuration
# =============================================================================

IBKR_HOST = '127.0.0.1'
IBKR_PORT = 4002  # Paper trading
CONNECT_TIMEOUT_SECONDS = float(os.getenv("IB_SYNC_CONNECT_TIMEOUT_SEC", "12"))
CLIENT_ID = int(os.getenv("IB_SYNC_CLIENT_ID", "0") or 0)
ALLOW_INDEPENDENT_SESSION = str(os.getenv("IB_SYNC_ALLOW_INDEPENDENT_SESSION", "")).lower() in {
    "1",
    "true",
    "yes",
    "on",
}

CLICKHOUSE_HOST = 'localhost'
CLICKHOUSE_PORT = 8123
CLICKHOUSE_DB = os.getenv('CH_DB', 'trading')

SYNC_INTERVAL_SECONDS = 300  # 5 minutes

# =============================================================================
# ClickHouse Table Schemas
# =============================================================================

# Real-time account equity snapshots
CREATE_EQUITY_REALTIME_TABLE = """
CREATE TABLE IF NOT EXISTS ibkr_account_realtime (
    ts_ny DateTime('America/New_York'),
    date Date,
    net_liquidation Float64,
    total_cash Float64,
    gross_position_value Float64,
    unrealized_pnl Float64,
    realized_pnl Float64,
    buying_power Float64,
    available_funds Float64,
    excess_liquidity Float64,
    init_margin_req Float64,
    maint_margin_req Float64
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, ts_ny)
"""

# Real-time position snapshots (total positions, not incremental)
CREATE_POSITIONS_REALTIME_TABLE = """
CREATE TABLE IF NOT EXISTS ibkr_positions_realtime (
    ts_ny DateTime('America/New_York'),
    date Date,
    symbol String,
    conId Int64,
    position Float64,
    avg_cost Float64,
    market_price Float64,
    market_value Float64,
    unrealized_pnl Float64,
    realized_pnl Float64,
    exchange String
) ENGINE = ReplacingMergeTree()
PARTITION BY toYYYYMM(date)
ORDER BY (date, ts_ny, symbol)
"""

# =============================================================================
# Data Sync Functions
# =============================================================================

def _safe_float(value) -> Optional[float]:
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def _pick_market_price(ticker) -> Optional[float]:
    if ticker is None:
        return None
    for candidate in (
        getattr(ticker, "close", None),
        getattr(ticker, "last", None),
        getattr(ticker, "marketPrice", lambda: None)(),
    ):
        px = _safe_float(candidate)
        if px is not None and px > 0:
            return px
    bid = _safe_float(getattr(ticker, "bid", None))
    ask = _safe_float(getattr(ticker, "ask", None))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None

def _candidate_client_ids() -> list[int]:
    registry = get_client_id_registry()
    candidates = [
        CLIENT_ID,
        int(registry.resolve("account_sync")),
    ]
    out: list[int] = []
    for candidate in candidates:
        try:
            normalized = int(candidate)
        except Exception:
            continue
        if normalized <= 0 or normalized in out:
            continue
        out.append(normalized)
    return out or [888]


def _run_system_process_active() -> bool:
    try:
        result = subprocess.run(
            ["ps", "-axo", "command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return False
    current_pid = os.getpid()
    for line in (result.stdout or "").splitlines():
        if "run_system.py" not in line:
            continue
        if "sync_ibkr_realtime.py" in line:
            continue
        if f" {current_pid} " in f" {line} ":
            continue
        return True
    return False


def connect_ibkr() -> IB:
    """Connect to IBKR with a deterministic fallback client-id ladder."""
    if _run_system_process_active() and not ALLOW_INDEPENDENT_SESSION:
        raise RuntimeError(
            "run_system_active_refusing_independent_ib_session; "
            "move account sync into the shared run_system IB session or set "
            "IB_SYNC_ALLOW_INDEPENDENT_SESSION=true for manual diagnostics only"
        )
    last_error: Exception | None = None
    for client_id in _candidate_client_ids():
        ib = IB()
        try:
            ib.connect(
                IBKR_HOST,
                IBKR_PORT,
                clientId=int(client_id),
                timeout=CONNECT_TIMEOUT_SECONDS,
                readonly=True,
            )
            print(f"✅ IBKR connected (clientId={int(client_id)}, readonly=True)")
            return ib
        except Exception as exc:
            last_error = exc
            try:
                ib.disconnect()
            except Exception:
                pass
            print(f"⚠️  IBKR connect failed (clientId={int(client_id)}): {type(exc).__name__}: {exc}")
    if last_error is not None:
        raise last_error
    raise RuntimeError("ibkr_connect_failed_without_candidate_client_ids")

def connect_clickhouse() -> ClickHouseDB:
    """Connect to ClickHouse"""
    return ClickHouseDB()

def ensure_tables(ch: ClickHouseDB):
    """Create tables if they don't exist"""
    ch.execute(CREATE_EQUITY_REALTIME_TABLE)
    ch.execute(CREATE_POSITIONS_REALTIME_TABLE)
    print("✅ ClickHouse tables ensured")

def sync_account_summary(ib: IB, ch: ClickHouseDB) -> Dict[str, float]:
    """
    Sync account summary to ClickHouse
    Returns account summary dict
    """
    now_ny = datetime.now(ET)
    today = now_ny.date()

    # Get account summary from IBKR
    account_values = ib.accountSummary()

    # Build dict
    summary = {}
    for av in account_values:
        if av.currency == 'USD':
            try:
                summary[av.tag] = float(av.value)
            except (ValueError, TypeError):
                # Skip non-numeric values
                pass

    # Extract key values
    net_liquidation = summary.get('NetLiquidation', 0.0)
    total_cash = summary.get('TotalCashValue', 0.0)
    gross_position_value = summary.get('GrossPositionValue', 0.0)
    unrealized_pnl = summary.get('UnrealizedPnL', 0.0)
    realized_pnl = summary.get('RealizedPnL', 0.0)
    buying_power = summary.get('BuyingPower', 0.0)
    available_funds = summary.get('AvailableFunds', 0.0)
    excess_liquidity = summary.get('ExcessLiquidity', 0.0)
    init_margin_req = summary.get('InitMarginReq', 0.0)
    maint_margin_req = summary.get('MaintMarginReq', 0.0)

    # Insert to ClickHouse
    values = [[
        now_ny,
        today,
        net_liquidation,
        total_cash,
        gross_position_value,
        unrealized_pnl,
        realized_pnl,
        buying_power,
        available_funds,
        excess_liquidity,
        init_margin_req,
        maint_margin_req,
    ]]
    ch.insert(
        "ibkr_account_realtime",
        values,
        column_names=[
            "ts_ny",
            "date",
            "net_liquidation",
            "total_cash",
            "gross_position_value",
            "unrealized_pnl",
            "realized_pnl",
            "buying_power",
            "available_funds",
            "excess_liquidity",
            "init_margin_req",
            "maint_margin_req",
        ],
    )

    print(f"✅ Account synced: NetLiq=${net_liquidation:,.2f}, Cash=${total_cash:,.2f}, "
          f"PosValue=${gross_position_value:,.2f}, UnrealPnL=${unrealized_pnl:+,.2f}")

    return {
        'net_liquidation': net_liquidation,
        'total_cash': total_cash,
        'gross_position_value': gross_position_value,
        'unrealized_pnl': unrealized_pnl,
        'realized_pnl': realized_pnl,
        'buying_power': buying_power
    }

def sync_positions(ib: IB, ch: ClickHouseDB) -> List[Dict]:
    """
    Sync current positions to ClickHouse
    Returns list of position dicts
    """
    now_ny = datetime.now(ET)
    today = now_ny.date()

    # Get all positions from IBKR
    positions = ib.positions()

    if not positions:
        print("⚠️  No positions to sync")
        return []

    # Batch snapshot tickers once to avoid per-position reqMktData churn.
    contracts = [pos.contract for pos in positions if getattr(pos, "contract", None) is not None]
    ticker_by_conid: Dict[int, object] = {}
    try:
        tickers = ib.reqTickers(*contracts) if contracts else []
        for tk in tickers:
            conid = getattr(getattr(tk, "contract", None), "conId", None)
            if conid:
                ticker_by_conid[int(conid)] = tk
    except Exception:
        ticker_by_conid = {}

    # Only request PnLSingle for rows where avgCost is missing/non-positive.
    pnl_by_conid: Dict[int, Dict[str, float]] = {}
    account = ""
    try:
        account = str(next((getattr(p, "account", "") for p in positions if getattr(p, "account", "")), "") or "")
        if not account:
            managed = ib.managedAccounts() or []
            account = str(managed[0]) if managed else ""
    except Exception:
        account = ""
    pnl_objs: Dict[int, object] = {}
    try:
        if account:
            missing_avg = [
                int(getattr(pos.contract, "conId", 0) or 0)
                for pos in positions
                if _safe_float(getattr(pos, "avgCost", 0.0)) in (None, 0.0)
                and int(getattr(getattr(pos, "contract", None), "conId", 0) or 0) > 0
            ]
            for conid in sorted(set(missing_avg)):
                pnl_objs[conid] = ib.reqPnLSingle(account, "", conid)
            if pnl_objs:
                ib.sleep(0.8)
                for conid, pnl in pnl_objs.items():
                    unreal = _safe_float(getattr(pnl, "unrealizedPnL", None))
                    value = _safe_float(getattr(pnl, "value", None))
                    if unreal is None and value is None:
                        continue
                    row: Dict[str, float] = {}
                    if unreal is not None:
                        row["unrealized_pnl"] = float(unreal)
                    if value is not None:
                        row["market_value"] = float(value)
                    pnl_by_conid[int(conid)] = row
    except Exception:
        pnl_by_conid = {}
    finally:
        for conid in list(pnl_objs.keys()):
            try:
                ib.cancelPnLSingle(account, "", int(conid))
            except Exception:
                pass

    # Prepare data
    position_data = []
    total_value = 0.0
    total_unrealized = 0.0

    for pos in positions:
        contract = pos.contract
        qty = pos.position
        avg_cost = abs(float(_safe_float(getattr(pos, "avgCost", 0.0)) or 0.0))
        conid = int(getattr(contract, "conId", 0) or 0)
        ticker = ticker_by_conid.get(conid)
        market_price = _pick_market_price(ticker)
        pnl_row = pnl_by_conid.get(conid, {})

        # Prefer IB-provided unrealized/value when avgCost is missing/invalid.
        unrealized_pnl = _safe_float(pnl_row.get("unrealized_pnl"))
        market_value = _safe_float(pnl_row.get("market_value"))

        # If avgCost is zero, infer from price and unrealized PnL to keep PnL math sane.
        if (avg_cost <= 0) and (market_price is not None) and (unrealized_pnl is not None) and abs(float(qty)) > 1e-12:
            inferred_avg = float(market_price) - (float(unrealized_pnl) / float(qty))
            if inferred_avg > 0:
                avg_cost = float(inferred_avg)

        if market_price is None or market_price <= 0:
            market_price = avg_cost if avg_cost > 0 else 0.0
        if market_value is None:
            market_value = abs(float(qty)) * float(market_price)
        # Keep market_value semantics stable (gross notional, always non-negative).
        market_value = abs(float(market_value))
        if unrealized_pnl is None:
            if avg_cost > 0 and market_price > 0:
                unrealized_pnl = (float(market_price) - float(avg_cost)) * float(qty)
            else:
                unrealized_pnl = 0.0

        position_data.append({
            'symbol': contract.symbol,
            'conId': contract.conId,
            'qty': qty,
            'avg_cost': avg_cost,
            'market_price': market_price,
            'market_value': market_value,
            'unrealized_pnl': unrealized_pnl,
            'realized_pnl': 0.0,  # TODO: Get from somewhere if available
            'exchange': contract.exchange or 'SMART'
        })

        total_value += float(market_value)
        total_unrealized += float(unrealized_pnl)

    # Insert to ClickHouse
    if position_data:
        values = [
            [
                now_ny,
                today,
                p['symbol'],
                p['conId'],
                p['qty'],
                p['avg_cost'],
                p['market_price'],
                p['market_value'],
                p['unrealized_pnl'],
                p['realized_pnl'],
                p['exchange']
            ]
            for p in position_data
        ]
        ch.insert(
            "ibkr_positions_realtime",
            values,
            column_names=[
                "ts_ny",
                "date",
                "symbol",
                "conId",
                "position",
                "avg_cost",
                "market_price",
                "market_value",
                "unrealized_pnl",
                "realized_pnl",
                "exchange",
            ],
        )

        print(f"✅ Positions synced: {len(position_data)} symbols, "
              f"TotalValue=${total_value:,.2f}, UnrealPnL=${total_unrealized:+,.2f}")

        for p in position_data:
            print(f"   {p['symbol']:<6} {p['qty']:>6.0f}股 @ ${p['avg_cost']:>8.2f}平均, "
                  f"当前${p['market_price']:>8.2f}, 浮盈浮亏${p['unrealized_pnl']:>+10.2f}")

    return position_data

def sync_once():
    """Run sync once"""
    print("="*80)
    print(f"IBKR Real-time Sync @ {datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("="*80)

    # Connect
    ib = None
    ch = None

    try:
        print(f"\n🔌 Connecting to IBKR ({IBKR_HOST}:{IBKR_PORT})...")
        ib = connect_ibkr()

        print(f"\n🔌 Connecting to ClickHouse ({CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}/{CLICKHOUSE_DB})...")
        ch = connect_clickhouse()
        print("✅ ClickHouse connected")

        # Ensure tables exist
        ensure_tables(ch)

        # Sync account summary
        print("\n📊 Syncing account summary...")
        account = sync_account_summary(ib, ch)

        # Sync positions
        print("\n📦 Syncing positions...")
        positions = sync_positions(ib, ch)

        # Summary
        print("\n" + "="*80)
        print("✅ Sync completed successfully")
        print(f"   Account: NetLiq=${account.get('net_liquidation', 0):,.2f}, "
              f"Cash=${account.get('total_cash', 0):,.2f}")
        print(f"   Positions: {len(positions)} symbols")
        print("="*80)

        return True

    except Exception as e:
        print(f"\n❌ Sync failed: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        if ib:
            ib.disconnect()
            print("\n🔌 IBKR disconnected")

def sync_loop():
    """Run sync in loop every 5 minutes"""
    print("="*80)
    print("🔄 IBKR Real-time Sync Service - Loop Mode")
    print(f"   Sync interval: {SYNC_INTERVAL_SECONDS}s ({SYNC_INTERVAL_SECONDS/60:.1f} minutes)")
    print("   Press Ctrl+C to stop")
    print("="*80)

    iteration = 0

    while True:
        try:
            iteration += 1
            print(f"\n🔄 Iteration #{iteration}")

            success = sync_once()

            if success:
                print(f"\n⏰ Next sync in {SYNC_INTERVAL_SECONDS}s "
                      f"({datetime.now(ET) + timedelta(seconds=SYNC_INTERVAL_SECONDS):%H:%M:%S})...")
            else:
                print(f"\n⏰ Retry in {SYNC_INTERVAL_SECONDS}s...")

            time.sleep(SYNC_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\n\n⛔ Stopped by user")
            break
        except Exception as e:
            print(f"\n❌ Loop error: {e}")
            import traceback
            traceback.print_exc()
            print(f"\n⏰ Retry in {SYNC_INTERVAL_SECONDS}s...")
            time.sleep(SYNC_INTERVAL_SECONDS)

# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='IBKR Real-time Data Sync to ClickHouse'
    )
    parser.add_argument(
        '--loop',
        action='store_true',
        help='Run in loop mode (sync every 5 minutes)'
    )
    parser.add_argument(
        '--interval',
        type=int,
        default=300,
        help='Sync interval in seconds (default: 300 = 5 minutes)'
    )

    args = parser.parse_args()

    global SYNC_INTERVAL_SECONDS
    SYNC_INTERVAL_SECONDS = args.interval

    if args.loop:
        from datetime import timedelta
        sync_loop()
    else:
        sync_once()

if __name__ == "__main__":
    main()
