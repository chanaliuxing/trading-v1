#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IBKR Gateway / TWS 紧急清仓脚本（股票 + 期权，含持仓复核）
Author: Wei
Version: 2026-01-07

示例：
  # 关闭某个标的所有仓位（股票 + 期权）
  python tool/sell_all_shares.py --symbol QQQ --mode paper --yes

  # 仅关闭账户所有期权仓位（谨慎使用）
  python tool/sell_all_shares.py --all --options-only --mode paper --yes

功能（默认“快 + 尽量好价”，失败则回退市价）：
  1) 连接 IB Gateway / TWS
  2) 查询目标持仓（按 symbol 过滤或全账户）
  3) 取消相关 open orders（避免冲突/法规拒单）
  4) 期权优先尝试：
     - 先用 vertical spread（BAG）合并平仓（减少腿风险/滑点）
     - 再对剩余期权逐腿平仓
     - 限价 IOC：mid → 更激进价（bid/ask）→ 市价兜底
  5) 再次查询持仓，确认是否已清仓
  6) 记录完整日志到 sell_log.txt

依赖：
    pip install ib_insync
"""

import argparse
from datetime import datetime
from collections import defaultdict
from dataclasses import dataclass
import math
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path
import sys
import os

try:
    from ib_insync import IB, Contract, ComboLeg, LimitOrder, MarketOrder
except ModuleNotFoundError:
    # 允许用户直接运行 `python tool/sell_all_shares.py ...`
    # 若当前解释器缺少 ib_insync，自动切到仓库内 venv/.venv 重新执行一次。
    if os.environ.get("SELL_ALL_SHARES_REEXEC") == "1":
        raise
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [repo_root / "venv" / "bin" / "python", repo_root / ".venv" / "bin" / "python"]
    for py in candidates:
        if py.exists():
            os.environ["SELL_ALL_SHARES_REEXEC"] = "1"
            os.execv(str(py), [str(py), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise
import time
from dotenv import load_dotenv

LOG_FILE = "sell_log.txt"
load_dotenv()

def log(msg: str):
    """追加写入日志"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")
    print(msg)

def _pick_default_client_id() -> int:
    env = os.getenv("IBKR_CLIENT_ID")
    if env:
        try:
            return int(env)
        except Exception:
            pass
    return 9000 + (os.getpid() % 1000)

@dataclass
class PosRow:
    contract: Contract
    con_id: int
    symbol: str
    sec_type: str
    qty: float
    expiry: Optional[str] = None
    right: Optional[str] = None
    strike: Optional[float] = None


def _sec_type(contract: Contract) -> str:
    return str(getattr(contract, "secType", "") or "").upper()


def _safe_conid(contract: Contract) -> int:
    try:
        return int(getattr(contract, "conId", 0) or 0)
    except Exception:
        return 0


def _normalize_contract_for_orders(contract: Contract) -> Contract:
    try:
        sec = _sec_type(contract)
        # Force SMART exchange for common types to avoid direct-routing precautionary blocks
        if sec in {"STK", "OPT", "FOP"}:
            contract.exchange = "SMART"
        
        if not getattr(contract, "exchange", None):
            contract.exchange = "SMART"
        if not getattr(contract, "currency", None):
            contract.currency = "USD"
    except Exception:
        return contract
    return contract


def _valid_px(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
        if math.isnan(v) or v <= 0:
            return None
        return v
    except Exception:
        return None


def _round_down(px: float, tick: float = 0.01) -> float:
    if tick <= 0:
        return float(px)
    return math.floor(px / tick) * tick


def _round_up(px: float, tick: float = 0.01) -> float:
    if tick <= 0:
        return float(px)
    return math.ceil(px / tick) * tick


def _is_done_status(status: str) -> bool:
    return str(status or "") in {"Filled", "Cancelled", "Inactive"}


def _extract_trade_conids(trade: Any) -> List[int]:
    c = getattr(trade, "contract", None)
    if not c:
        return []
    sec_type = _sec_type(c)
    if sec_type == "BAG":
        out = []
        legs = getattr(c, "comboLegs", None) or []
        for leg in legs:
            try:
                conid = int(getattr(leg, "conId", 0) or 0)
            except Exception:
                conid = 0
            if conid:
                out.append(conid)
        return out
    conid = _safe_conid(c)
    return [conid] if conid else []


def _format_pos_label(p: PosRow) -> str:
    if p.sec_type not in {"OPT", "FOP"}:
        return f"{p.symbol} ({p.sec_type})"
    strike = f"{float(p.strike):.1f}" if p.strike is not None else "?"
    expiry = str(p.expiry or "")
    right = str(p.right or "")
    local = getattr(p.contract, "localSymbol", None) or ""
    if local:
        return f"[{p.sec_type}] {local} {right}{strike} {expiry}".strip()
    return f"[{p.sec_type}] {p.symbol} {expiry} {right}{strike}".strip()


def _fetch_option_quotes(ib: IB, contracts: List[Contract]) -> Dict[int, Dict[str, Optional[float]]]:
    quotes: Dict[int, Dict[str, Optional[float]]] = {}
    if not contracts:
        return quotes
    try:
        for i in range(0, len(contracts), 50):
            batch = contracts[i:i + 50]
            tickers = ib.reqTickers(*batch) if batch else []
            for tk in tickers:
                c = getattr(tk, "contract", None)
                conid = _safe_conid(c) if c else 0
                if not conid:
                    continue
                bid = _valid_px(getattr(tk, "bid", None))
                ask = _valid_px(getattr(tk, "ask", None))
                last = _valid_px(getattr(tk, "last", None))
                quotes[conid] = {"bid": bid, "ask": ask, "last": last}
    except Exception:
        return quotes
    return quotes


def _wait_trade_done(ib: IB, trade: Any, timeout_sec: float) -> None:
    t0 = time.time()
    while True:
        if trade is None:
            return
        try:
            if trade.isDone():
                return
        except Exception:
            return
        if time.time() - t0 >= timeout_sec:
            return
        ib.sleep(0.2)


def _place_ioc_limit(
    ib: IB,
    contract: Contract,
    action: str,
    qty: float,
    limit_px: float,
    wait_sec: float,
) -> Any:
    order = LimitOrder(action, qty, float(limit_px))
    order.tif = "IOC"
    trade = ib.placeOrder(contract, order)
    _wait_trade_done(ib, trade, timeout_sec=wait_sec)
    return trade


def _place_market(ib: IB, contract: Contract, action: str, qty: float, wait_sec: float) -> Any:
    order = MarketOrder(action, qty)
    trade = ib.placeOrder(contract, order)
    _wait_trade_done(ib, trade, timeout_sec=wait_sec)
    return trade


def _filled_remaining(trade: Any, total_qty: float) -> Tuple[float, float]:
    filled = 0.0
    remaining = float(total_qty)
    try:
        st = getattr(trade, "orderStatus", None)
        if st:
            filled = float(getattr(st, "filled", filled) or filled)
            remaining = float(getattr(st, "remaining", remaining) or remaining)
    except Exception:
        return 0.0, float(total_qty)
    return filled, remaining


def _smart_close_single_option(
    ib: IB,
    pos: PosRow,
    quote: Optional[Dict[str, Optional[float]]],
    ioc_wait_sec: float,
    market_wait_sec: float,
    tick: float = 0.01,
) -> None:
    qty = int(round(abs(float(pos.qty))))
    if qty <= 0:
        return
    action = "SELL" if pos.qty > 0 else "BUY"

    bid = _valid_px((quote or {}).get("bid"))
    ask = _valid_px((quote or {}).get("ask"))
    mid = (bid + ask) / 2.0 if (bid is not None and ask is not None) else None
    if mid is None:
        mid = _valid_px((quote or {}).get("last"))

    remaining = qty
    if mid is not None and bid is not None and ask is not None:
        # Attempt 1: midpoint (best-effort price improvement)
        px_mid = float(round(mid, 2))
        if px_mid > 0:
            trade = _place_ioc_limit(ib, pos.contract, action, remaining, px_mid, wait_sec=ioc_wait_sec)
            _, remaining = _filled_remaining(trade, remaining)
            try:
                remaining = int(round(float(remaining)))
            except Exception:
                remaining = int(qty)

    if remaining > 1e-8 and bid is not None and ask is not None:
        # Attempt 2: cross to NBBO side to fill quickly
        if action == "SELL":
            px = _round_down(float(bid), tick=tick)
        else:
            px = _round_up(float(ask), tick=tick)
        if px > 0:
            trade = _place_ioc_limit(ib, pos.contract, action, remaining, px, wait_sec=ioc_wait_sec)
            _, remaining = _filled_remaining(trade, remaining)
            try:
                remaining = int(round(float(remaining)))
            except Exception:
                remaining = int(qty)

    if remaining > 1e-8:
        # Final fallback: market order
        _place_market(ib, pos.contract, action, int(round(float(remaining))), wait_sec=market_wait_sec)


def _pair_verticals(option_positions: List[PosRow]) -> Tuple[List[dict], List[PosRow]]:
    """Best-effort pairing of opposite-sign option legs into vertical spreads."""
    groups: Dict[Tuple[str, str, str], List[PosRow]] = defaultdict(list)
    for p in option_positions:
        if p.sec_type not in {"OPT", "FOP"}:
            continue
        if not p.expiry or not p.right or p.strike is None:
            continue
        if abs(float(p.qty)) <= 1e-8:
            continue
        groups[(p.symbol, str(p.expiry), str(p.right))].append(p)

    spreads: List[dict] = []
    residuals: List[PosRow] = []

    for (sym, expiry, right), legs in groups.items():
        longs: List[dict] = []
        shorts: List[dict] = []
        for p in legs:
            strike = float(p.strike or 0.0)
            qty = float(p.qty)
            if qty > 0:
                longs.append({"p": p, "strike": strike, "qty": qty})
            elif qty < 0:
                shorts.append({"p": p, "strike": strike, "qty": abs(qty)})

        def best_long_idx(short_strike: float) -> Optional[int]:
            best = None
            best_width = None
            for i, lg in enumerate(longs):
                if lg["qty"] <= 0:
                    continue
                width = abs(short_strike - float(lg["strike"]))
                if width <= 0:
                    continue
                if best_width is None or width < best_width:
                    best_width = width
                    best = i
            return best

        for sh in sorted(shorts, key=lambda x: x["strike"]):
            while sh["qty"] > 1e-8:
                idx = best_long_idx(float(sh["strike"]))
                if idx is None:
                    break
                lg = longs[idx]
                pair_qty = min(float(sh["qty"]), float(lg["qty"]))
                if pair_qty <= 0:
                    break
                sh["qty"] -= pair_qty
                lg["qty"] -= pair_qty
                spreads.append(
                    {
                        "symbol": sym,
                        "expiry": expiry,
                        "right": right,
                        "qty": pair_qty,
                        "long": lg["p"],
                        "short": sh["p"],
                        "long_strike": float(lg["strike"]),
                        "short_strike": float(sh["strike"]),
                    }
                )

        for lg in longs:
            if lg["qty"] > 1e-8:
                p = lg["p"]
                residuals.append(PosRow(**{**p.__dict__, "qty": float(lg["qty"])}))
        for sh in shorts:
            if sh["qty"] > 1e-8:
                p = sh["p"]
                residuals.append(PosRow(**{**p.__dict__, "qty": -float(sh["qty"])}))

    # Also include any option positions that didn't have a valid grouping key.
    grouped_ids = {id(p) for legs in groups.values() for p in legs}
    for p in option_positions:
        if p.sec_type in {"OPT", "FOP"} and id(p) not in grouped_ids:
            residuals.append(p)

    return spreads, residuals


def _build_vertical_bag(sp: dict) -> Optional[Tuple[Contract, str, int]]:
    """Build a BAG contract + close side for a paired vertical spread."""
    long_p: PosRow = sp["long"]
    short_p: PosRow = sp["short"]
    qty = int(round(float(sp["qty"])))
    right = str(sp["right"] or "").upper()
    if qty <= 0 or right not in {"P", "C"}:
        return None

    long_strike = float(sp["long_strike"])
    short_strike = float(sp["short_strike"])

    # Determine which conId is higher/lower strike.
    if long_strike >= short_strike:
        high_conid = long_p.con_id
        low_conid = short_p.con_id
        high_strike = long_strike
        low_strike = short_strike
        long_vs_short = "LONG_HIGH"
    else:
        high_conid = short_p.con_id
        low_conid = long_p.con_id
        high_strike = short_strike
        low_strike = long_strike
        long_vs_short = "LONG_LOW"

    # Canonical debit vertical definition:
    # - Puts: BUY higher strike, SELL lower strike  (bear put debit)
    # - Calls: BUY lower strike, SELL higher strike (bull call debit)
    if right == "P":
        buy_leg_conid = high_conid
        sell_leg_conid = low_conid
        # If positions are credit (short high, long low) => close by BUY combo.
        close_side = "BUY" if long_vs_short == "LONG_LOW" else "SELL"
    else:
        buy_leg_conid = low_conid
        sell_leg_conid = high_conid
        # Calls: long low/short high is debit => close by SELL combo.
        close_side = "SELL" if long_vs_short == "LONG_LOW" else "BUY"

    combo = Contract()
    combo.symbol = str(sp["symbol"])
    combo.secType = "BAG"
    combo.currency = "USD"
    combo.exchange = "SMART"

    leg_buy = ComboLeg()
    leg_buy.conId = int(buy_leg_conid)
    leg_buy.ratio = 1
    leg_buy.action = "BUY"
    leg_buy.exchange = "SMART"

    leg_sell = ComboLeg()
    leg_sell.conId = int(sell_leg_conid)
    leg_sell.ratio = 1
    leg_sell.action = "SELL"
    leg_sell.exchange = "SMART"

    combo.comboLegs = [leg_buy, leg_sell]
    _ = high_strike, low_strike  # keep for potential debugging
    return combo, close_side, qty


def _smart_close_vertical_bag(
    ib: IB,
    combo: Contract,
    close_side: str,
    qty: int,
    buy_leg_conid: int,
    sell_leg_conid: int,
    quotes_by_conid: Dict[int, Dict[str, Optional[float]]],
    ioc_wait_sec: float,
    market_wait_sec: float,
    tick: float = 0.01,
) -> None:
    if qty <= 0:
        return
    # Compute synthetic combo prices from leg quotes.
    q_buy = quotes_by_conid.get(int(buy_leg_conid), {})
    q_sell = quotes_by_conid.get(int(sell_leg_conid), {})

    buy_bid = _valid_px(q_buy.get("bid"))
    buy_ask = _valid_px(q_buy.get("ask"))
    sell_bid = _valid_px(q_sell.get("bid"))
    sell_ask = _valid_px(q_sell.get("ask"))
    buy_mid = (buy_bid + buy_ask) / 2.0 if (buy_bid is not None and buy_ask is not None) else _valid_px(q_buy.get("last"))
    sell_mid = (sell_bid + sell_ask) / 2.0 if (sell_bid is not None and sell_ask is not None) else _valid_px(q_sell.get("last"))

    remaining = int(qty)

    def _combo_mid() -> Optional[float]:
        if buy_mid is None or sell_mid is None:
            return None
        v = float(buy_mid) - float(sell_mid)
        return v if v > 0 else None

    def _combo_aggr() -> Optional[float]:
        if buy_bid is None or buy_ask is None or sell_bid is None or sell_ask is None:
            return None
        if close_side.upper() == "BUY":
            v = float(buy_ask) - float(sell_bid)
            return v if v > 0 else None
        v = float(buy_bid) - float(sell_ask)
        return v if v > 0 else None

    mid_px = _combo_mid()
    if mid_px is not None:
        px = float(round(mid_px, 2))
        if px > 0:
            trade = _place_ioc_limit(ib, combo, close_side, int(remaining), px, wait_sec=ioc_wait_sec)
            _, remaining = _filled_remaining(trade, remaining)

    if remaining > 1e-8:
        aggr_px = _combo_aggr()
        if aggr_px is not None:
            if close_side.upper() == "BUY":
                px = _round_up(float(aggr_px), tick=tick)
            else:
                px = _round_down(float(aggr_px), tick=tick)
            if px > 0:
                trade = _place_ioc_limit(ib, combo, close_side, int(remaining), px, wait_sec=ioc_wait_sec)
                _, remaining = _filled_remaining(trade, remaining)

    if remaining > 1e-8:
        _place_market(ib, combo, close_side, int(round(float(remaining))), wait_sec=market_wait_sec)


def main(
    *,
    symbol: Optional[str],
    mode: str,
    host: str,
    port: int,
    client_id: int,
    close_stocks: bool,
    close_options: bool,
    options_only: bool,
    stocks_only: bool,
    option_close: str,
    use_combo: bool,
    ioc_wait_sec: float,
    market_wait_sec: float,
    cancel_timeout_sec: float,
    global_cancel: bool,
    dry_run: bool,
    yes: bool,
    close_all: bool,
) -> None:
    ib = IB()
    client_id = int(client_id)

    # Attach a temporary error listener to detect "client id already in use" (Error 326).
    connect_errors: List[int] = []

    def _on_error(req_id: int, error_code: int, error_string: str, contract: Any = None) -> None:
        _ = req_id, error_string, contract
        try:
            connect_errors.append(int(error_code))
        except Exception:
            return

    try:
        ib.errorEvent += _on_error
    except Exception:
        pass

    last_exc: Optional[Exception] = None
    for attempt in range(10):
        connect_errors.clear()
        log(f"🔌 Connecting to IB ({mode}, {host}:{port}, clientId={client_id}) ...")
        try:
            ib.connect(host, port, clientId=client_id)
            last_exc = None
            break
        except Exception as e:
            last_exc = e
            if 326 in connect_errors:
                log(f"⚠️ clientId {client_id} already in use (Error 326). Retrying with a new clientId...")
                try:
                    ib.disconnect()
                except Exception:
                    pass
                ib = IB()
                try:
                    ib.errorEvent += _on_error
                except Exception:
                    pass
                client_id += 1
                continue
            raise
    if last_exc is not None:
        raise last_exc

    ib.reqPositions()
    ib.sleep(1.5)
    positions = ib.positions() or []

    log(f"📡 Found {len(positions)} raw positions in account.")

    # Pre-qualify contracts to ensure expiry/strike/right are populated
    if positions:
        log("📡 Qualifying contracts (best-effort)...")
        all_contracts = [p.contract for p in positions]
        ib.qualifyContracts(*all_contracts)

    if not positions:
        if close_all:
            log("⚠️ 当前账户无任何持仓，将继续执行 --all 挂单清理。")
        else:
            log("⚠️ 当前账户无任何持仓。")
            ib.disconnect()
            return

    sym_u = symbol.upper() if symbol else None
    targets: List[PosRow] = []
    for pos in positions:
        c = getattr(pos, "contract", None)
        if not c:
            continue
        c = _normalize_contract_for_orders(c)
        sec_type = _sec_type(c)
        qty = float(getattr(pos, "position", 0.0) or 0.0)
        if abs(qty) <= 1e-8:
            continue
        if sym_u and str(getattr(c, "symbol", "") or "").upper() != sym_u:
            continue
        if not close_all and not sym_u:
            continue

        is_opt = sec_type in {"OPT", "FOP"}
        is_stk = sec_type == "STK"

        if options_only:
            if not is_opt:
                continue
        elif stocks_only:
            if not is_stk:
                continue
        else:
            if is_stk and not close_stocks:
                continue
            if is_opt and not close_options:
                continue
            if not is_stk and not is_opt:
                continue

        targets.append(
            PosRow(
                contract=c,
                con_id=_safe_conid(c),
                symbol=str(getattr(c, "symbol", "") or ""),
                sec_type=sec_type,
                qty=qty,
                expiry=(str(getattr(c, "lastTradeDateOrContractMonth", "") or "") or None) if is_opt else None,
                right=(str(getattr(c, "right", "") or "") or None) if is_opt else None,
                strike=(float(getattr(c, "strike")) if getattr(c, "strike", None) is not None else None) if is_opt else None,
            )
        )

    if targets:
        # Safety summary + confirm
        log("📌 Target positions to close:")
        for p in targets:
            log(f"  - {_format_pos_label(p)} qty={p.qty:+g} conId={p.con_id}")
    else:
        if sym_u:
            log(f"⚠️ 没有匹配 {sym_u} 的可平仓仓位（处理 STK/OPT/FOP）。")
        elif not close_all:
            log("⚠️ 未指定 --symbol 且未指定 --all，出于安全默认不执行。")
            ib.disconnect()
            return
        else:
            log("ℹ️ --all 模式下当前无持仓，将仅执行挂单清理（含 PreSubmitted）。")

    if not yes and not dry_run:
        ans = input("⚠️ Confirm flatten NOW? Type YES to continue: ").strip().upper()
        if ans != "YES":
            log("⛔ Aborted by user.")
            ib.disconnect()
            return

    target_conids = {p.con_id for p in targets if p.con_id}

    # Step 1: cancel relevant open orders first (prevents conflicts/regulatory rejects).
    auto_global_cancel = bool(close_all and not global_cancel)
    if global_cancel or auto_global_cancel:
        if auto_global_cancel:
            log("🧹 --all 模式自动执行全局撤单: ib.reqGlobalCancel()")
        else:
            log("🧹 Global cancel requested: ib.reqGlobalCancel()")
        if not dry_run:
            try:
                ib.reqGlobalCancel()
                ib.sleep(0.5)
            except Exception as e:
                log(f"⚠️ Global cancel failed: {e}")
    if (not global_cancel) or close_all:
        log("🧹 Cancelling open orders for targeted conId(s)...")
        if not dry_run:
            try:
                ib.reqAllOpenOrders()
                ib.sleep(0.8)
            except Exception:
                pass
            open_trades = ib.openTrades() or []
            cancelled = 0
            for tr in open_trades:
                if close_all:
                    status = str(getattr(getattr(tr, "orderStatus", None), "status", "") or "")
                    if _is_done_status(status):
                        continue
                    try:
                        ib.cancelOrder(tr.order)
                        cancelled += 1
                    except Exception:
                        continue
                    continue

                conids = _extract_trade_conids(tr)
                if conids and any(cid in target_conids for cid in conids):
                    try:
                        ib.cancelOrder(tr.order)
                        cancelled += 1
                    except Exception:
                        continue
            log(f"🧹 Cancel sent for {cancelled} open trade(s).")

            # Wait for cancellations to settle (best-effort).
            still_open: List[Any] = []
            t0 = time.time()
            while time.time() - t0 < float(cancel_timeout_sec):
                open_trades = ib.openTrades() or []
                if close_all:
                    still_open = [
                        tr
                        for tr in open_trades
                        if not _is_done_status(str(getattr(getattr(tr, "orderStatus", None), "status", "") or ""))
                    ]
                else:
                    still_open = [
                        tr
                        for tr in open_trades
                        if any(cid in target_conids for cid in _extract_trade_conids(tr))
                        and not _is_done_status(str(getattr(getattr(tr, "orderStatus", None), "status", "") or ""))
                    ]
                if not still_open:
                    break
                ib.sleep(0.3)
                try:
                    ib.reqAllOpenOrders()
                    ib.sleep(0.2)
                except Exception:
                    pass
            if close_all and still_open:
                try:
                    ib.reqAllOpenOrders()
                    ib.sleep(0.8)
                    open_trades = ib.openTrades() or []
                    still_open = [
                        tr
                        for tr in open_trades
                        if not _is_done_status(str(getattr(getattr(tr, "orderStatus", None), "status", "") or ""))
                    ]
                except Exception:
                    pass
            if still_open:
                log(f"⚠️ Some orders still not done after {cancel_timeout_sec}s; close orders may be rejected by IB.")

    # Step 2: close OPTIONS (prefer combo), then STOCKS.
    opt_positions = [p for p in targets if p.sec_type == "OPT"]
    stk_positions = [p for p in targets if p.sec_type == "STK"]

    quotes_by_conid: Dict[int, Dict[str, Optional[float]]] = {}
    if option_close == "smart" and opt_positions and not dry_run:
        log("📡 Fetching option quotes (best-effort) for smart close...")
        quotes_by_conid = _fetch_option_quotes(ib, [p.contract for p in opt_positions])

    if opt_positions:
        log(f"🧯 Closing option positions: {len(opt_positions)} contract(s)")

        remaining_opts = opt_positions
        if use_combo:
            spreads, residuals = _pair_verticals(opt_positions)
            if spreads:
                log(f"🔗 Detected vertical spreads: {len(spreads)} (will try BAG close first)")
            for sp in spreads:
                bag_info = _build_vertical_bag(sp)
                if not bag_info:
                    continue
                combo, close_side, qty = bag_info
                # Identify buy/sell leg conId as defined in combo.comboLegs.
                buy_leg_conid = int(combo.comboLegs[0].conId)
                sell_leg_conid = int(combo.comboLegs[1].conId)
                long_p: PosRow = sp["long"]
                short_p: PosRow = sp["short"]
                label = (
                    f"{sp['symbol']} {sp['expiry']} {sp['right']} "
                    f"long={sp['long_strike']:.1f} short={sp['short_strike']:.1f} x{qty} close={close_side}"
                )
                log(f"  - BAG {label}")
                if dry_run:
                    continue
                if option_close == "market":
                    _place_market(ib, combo, close_side, qty, wait_sec=market_wait_sec)
                else:
                    _smart_close_vertical_bag(
                        ib,
                        combo,
                        close_side,
                        qty,
                        buy_leg_conid,
                        sell_leg_conid,
                        quotes_by_conid,
                        ioc_wait_sec=ioc_wait_sec,
                        market_wait_sec=market_wait_sec,
                    )
                ib.sleep(0.1)

            remaining_opts = residuals

        if remaining_opts:
            log(f"🧾 Closing remaining option legs: {len(remaining_opts)}")
            for p in remaining_opts:
                label = _format_pos_label(p)
                log(f"  - {label} qty={p.qty:+g}")
                if dry_run:
                    continue
                if option_close == "market":
                    action = "SELL" if p.qty > 0 else "BUY"
                    _place_market(ib, p.contract, action, int(round(abs(float(p.qty)))), wait_sec=market_wait_sec)
                else:
                    _smart_close_single_option(
                        ib,
                        p,
                        quotes_by_conid.get(int(p.con_id)),
                        ioc_wait_sec=ioc_wait_sec,
                        market_wait_sec=market_wait_sec,
                    )
                ib.sleep(0.1)

    if stk_positions and close_stocks and not options_only:
        log(f"🧯 Closing stock positions: {len(stk_positions)}")
        for p in stk_positions:
            action = "SELL" if p.qty > 0 else "BUY"
            qty = abs(float(p.qty))
            label = _format_pos_label(p)
            log(f"  - {label} qty={p.qty:+g} action={action}")
            if dry_run:
                continue
            _place_market(ib, p.contract, action, qty, wait_sec=market_wait_sec)
            ib.sleep(0.1)

    # Final verification (+ emergency market sweep for leftovers)
    ib.sleep(1.0)
    ib.reqPositions()
    ib.sleep(1.0)
    updated = ib.positions() or []

    def _match_target(pos: Any) -> bool:
        c = getattr(pos, "contract", None)
        if not c:
            return False
        if sym_u and str(getattr(c, "symbol", "") or "").upper() != sym_u:
            return False
        st = _sec_type(c)
        if options_only:
            return st in {"OPT", "FOP"}
        if stocks_only:
            return st == "STK"
        return st in {"STK", "OPT", "FOP"}

    leftovers = [p for p in updated if abs(float(getattr(p, "position", 0.0) or 0.0)) > 1e-8 and _match_target(p)]

    log("📊 After flatten, remaining positions (target scope):")
    if not leftovers:
        log("  ✅ No remaining target positions. Flatten complete.")
    else:
        for pos in leftovers:
            c = getattr(pos, "contract", None)
            label = str(getattr(c, "localSymbol", None) or getattr(c, "symbol", "") or "")
            log(f"  - {label} ({_sec_type(c)}) qty={getattr(pos, 'position', 0.0)} conId={_safe_conid(c)}")
        if not dry_run:
            log("🧯 Final sweep: sending MARKET orders for remaining positions...")
            for pos in leftovers:
                c = getattr(pos, "contract", None)
                if not c:
                    continue
                c = _normalize_contract_for_orders(c)
                sec = _sec_type(c)
                q = float(getattr(pos, "position", 0.0) or 0.0)
                if abs(q) <= 1e-8:
                    continue
                action = "SELL" if q > 0 else "BUY"
                qty = int(round(abs(q))) if sec in {"OPT", "FOP"} else abs(q)
                log(f"  - MARKET {action} {qty} {str(getattr(c, 'localSymbol', None) or getattr(c, 'symbol', '') or '')}")
                try:
                    _place_market(ib, c, action, qty, wait_sec=market_wait_sec)
                except Exception:
                    continue
                ib.sleep(0.1)

            ib.sleep(1.0)
            ib.reqPositions()
            ib.sleep(1.0)
            updated2 = ib.positions() or []
            leftovers2 = [p for p in updated2 if abs(float(getattr(p, "position", 0.0) or 0.0)) > 1e-8 and _match_target(p)]
            if leftovers2:
                log("⚠️ Still remaining after final sweep (likely outside trading hours / illiquid / rejected):")
                for pos in leftovers2:
                    c = getattr(pos, "contract", None)
                    label = str(getattr(c, "localSymbol", None) or getattr(c, "symbol", "") or "")
                    log(f"  - {label} ({_sec_type(c)}) qty={getattr(pos, 'position', 0.0)} conId={_safe_conid(c)}")
            else:
                log("  ✅ Final sweep cleared remaining positions.")
        else:
            log("⚠️ Some positions remain (dry-run).")

    ib.disconnect()
    log("🔚 Done.\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=None, help="标的代码，例如 AAPL/QQQ（将平仓该 symbol 的 STK/OPT/FOP）")
    parser.add_argument("--all", action="store_true", help="平仓账户所有 STK/OPT/FOP（谨慎使用）")
    parser.add_argument("--mode", default=None, choices=["paper", "live"], help="交易模式 (paper 或 live)")
    parser.add_argument("--host", default=os.getenv("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=0, help="IB Gateway/TWS 端口；默认按 mode 自动选择 (paper=4002, live=4001)")
    parser.add_argument("--client-id", type=int, default=None, help="IB API clientId（默认自动选择，避免冲突）")

    parser.add_argument("--options-only", action="store_true", help="仅平仓期权（OPT/FOP）")
    parser.add_argument("--stocks-only", action="store_true", help="仅平仓股票（STK）")
    parser.add_argument("--no-options", action="store_true", help="不平仓期权（仅对默认模式有效）")
    parser.add_argument("--no-stocks", action="store_true", help="不平仓股票（仅对默认模式有效）")

    parser.add_argument("--option-close", choices=["smart", "market"], default="smart", help="期权平仓方式：smart=IOC限价→市价兜底；market=直接市价")
    parser.add_argument("--no-combo", action="store_true", help="不尝试用 BAG 合并平仓 vertical spreads")
    parser.add_argument("--ioc-wait-sec", type=float, default=1.2, help="IOC 订单等待成交/取消的秒数")
    parser.add_argument("--market-wait-sec", type=float, default=6.0, help="市价单等待成交的秒数（超时仍会继续执行后续）")
    parser.add_argument("--cancel-timeout-sec", type=float, default=6.0, help="等待撤单完成的最大秒数")
    parser.add_argument("--global-cancel", action="store_true", help="先执行 reqGlobalCancel()（取消全账户所有 open orders）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不下单")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    args = parser.parse_args()

    mode = args.mode or os.getenv("TRADING_MODE", "paper")
    port = int(args.port) if int(args.port) > 0 else (4002 if mode == "paper" else 4001)
    client_id = int(args.client_id) if args.client_id is not None else _pick_default_client_id()

    if args.options_only and args.stocks_only:
        raise SystemExit("Invalid args: --options-only and --stocks-only are mutually exclusive.")
    if not args.all and not args.symbol:
        log("⚠️ Safety: must specify --symbol or --all.")
        raise SystemExit(2)

    main(
        symbol=args.symbol,
        mode=mode,
        host=args.host,
        port=port,
        client_id=client_id,
        close_stocks=not args.no_stocks,
        close_options=not args.no_options,
        options_only=bool(args.options_only),
        stocks_only=bool(args.stocks_only),
        option_close=str(args.option_close),
        use_combo=not bool(args.no_combo),
        ioc_wait_sec=float(args.ioc_wait_sec),
        market_wait_sec=float(args.market_wait_sec),
        cancel_timeout_sec=float(args.cancel_timeout_sec),
        global_cancel=bool(args.global_cancel),
        dry_run=bool(args.dry_run),
        # --all 属于紧急一键全平，默认跳过二次确认
        yes=bool(args.yes or args.all),
        close_all=bool(args.all),
    )
