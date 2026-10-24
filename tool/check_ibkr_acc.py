#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IBKR 一日复盘（交易 + 财务）v2.0
--------------------------------
功能：
1) 连接 IBKR（Paper: 4002 / Live: 7497）
2) 拉取“今天”的全部成交（按 ET）
3) FIFO 配对生成回合交易（round trips），计算：
   - 平均买价/卖价、数量、持仓时长
   - 已实现盈亏（毛/净）、佣金费用
   - MFE/MAE（入场→出场期间 1m 最高高/最低低）
   - 收益捕获率（实际收益 / MFE 理论收益）
4) 汇总财务指标：净利、胜率、平均盈亏、利润因子、最大回撤（按回合序列）
5) 打印未平仓头寸的实时盈亏
6) 打印未完成订单、全部订单状态分布
"""

import os
import sys
import math
import time
import argparse
import fcntl
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from collections import defaultdict, deque

from typing import List, Dict, Optional, Tuple, Iterable, Any

from ib_insync import IB, util, Contract, Stock, ExecutionFilter

ET = ZoneInfo("America/New_York")

# =============== 数据结构 ===============

@dataclass
class FillRow:
    symbol: str
    conId: int
    side: str          # 'BOT' or 'SLD'
    shares: float
    price: float
    time: datetime
    orderId: int
    sec_type: Optional[str] = None
    right: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    local_symbol: Optional[str] = None
    multiplier: Optional[float] = None
    commission: float = 0.0
    currency: str = "USD"

@dataclass
class RoundTrip:
    symbol: str
    conId: int
    qty: float
    entry_price: float
    exit_price: float
    entry_time: datetime
    exit_time: datetime
    side: str              # 'LONG' or 'SHORT'
    gross_pnl: float
    net_pnl: float
    ret_pct: float
    fees: float
    holding_sec: int
    sec_type: Optional[str] = None
    right: Optional[str] = None
    strike: Optional[float] = None
    expiry: Optional[str] = None
    local_symbol: Optional[str] = None
    multiplier: Optional[float] = None
    mae_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    capture_pct: Optional[float] = None   # 实际收益 / 理论 MFE

# =============== 工具函数 ===============

def _adhoc_ib_connect_allowed() -> bool:
    blocked = os.getenv("IBKR_DISABLE_ADHOC_CONNECT", "").strip().lower() in {"1", "true", "yes", "y"}
    return not blocked


def _acquire_adhoc_ib_connect_guard(cooldown_sec: float) -> Any:
    """Serialize ad-hoc IBKR API sessions and prevent rapid reconnect churn."""
    lock_path = Path(os.getenv("IBKR_ADHOC_LOCK_PATH", "/tmp/trading_ibkr_adhoc_connect.lock"))
    state_path = Path(os.getenv("IBKR_ADHOC_STATE_PATH", "/tmp/trading_ibkr_adhoc_connect.last"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path.open("a+")
    try:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        last_ts = 0.0
        try:
            last_ts = float(state_path.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            last_ts = 0.0
        remaining = float(cooldown_sec or 0.0) - (time.time() - last_ts)
        if remaining > 0:
            raise RuntimeError(f"IBKR ad-hoc connection cooldown active; wait {remaining:.0f}s.")
        return {"lock_fh": lock_fh, "state_path": state_path}
    except Exception:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        lock_fh.close()
        raise


def _release_adhoc_ib_connect_guard(guard: Any) -> None:
    if not guard:
        return
    try:
        Path(guard["state_path"]).write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass
    lock_fh = guard.get("lock_fh")
    try:
        if lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        if lock_fh:
            lock_fh.close()
    except Exception:
        pass


def now_et() -> datetime:
    return datetime.now(ET)

def today_et_dates() -> Tuple[datetime, datetime]:
    """返回今日 ET 时区的起止时间（00:00:00 → 23:59:59）"""
    today = datetime.now(ET).date()
    start = datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=ET)
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start, end

def parse_exec_time(s) -> Optional[datetime]:
    """解析 IBKR ExecutionDetails.time 到带 ET 时区的 datetime。

    兼容输入为 str 或 datetime：
    - 若为 str，尝试若干常见格式；失败则回退 util.parseIBDatetime。
    - 若为 datetime，直接使用。
    最终统一返回 ET 时区 aware 的 datetime。
    """
    if s is None:
        return None

    dt: Optional[datetime] = None
    if isinstance(s, datetime):
        dt = s
    else:
        fmts = ['%Y%m%d  %H:%M:%S', '%Y%m%d %H:%M:%S', '%Y%m%d-%H:%M:%S']
        for fmt in fmts:
            try:
                dt = datetime.strptime(str(s), fmt)
                break
            except (ValueError, TypeError):
                continue
        if dt is None:
            try:
                # ib_insync 的解析器，兼容多种 IB 时间字符串格式
                dt = util.parseIBDatetime(s)
            except Exception:
                return None

    # 统一到 ET 时区
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    else:
        dt = dt.astimezone(ET)
    return dt

def human_money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"

def duration_hms(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}h{m}m{s}s"
    elif m > 0:
        return f"{m}m{s}s"
    else:
        return f"{s}s"

def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "N/A"
    try:
        return human_money(float(x))
    except Exception:
        return "N/A"

def fmt_px(x: Optional[float], width: int = 7) -> str:
    if x is None:
        return f"{'N/A':>{width}}"
    try:
        return f"{float(x):>{width}.2f}"
    except Exception:
        return f"{'N/A':>{width}}"

def fmt_pct(x: Optional[float], width: int = 6) -> str:
    if x is None:
        return f"{'N/A':>{width}}"
    try:
        return f"{float(x):>{width}.2f}%"
    except Exception:
        return f"{'N/A':>{width}}"

def _float_or_none(val: Optional[str]) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except Exception:
        return None

def _normalize_expiry(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    s = str(raw)
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return datetime.strptime(s[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except Exception:
            return None
    return s

def _contract_multiplier(contract: Contract) -> float:
    m = getattr(contract, "multiplier", None)
    try:
        return float(m) if m else 1.0
    except Exception:
        return 1.0

def _normalize_avg_cost_px(
    sec_type: Optional[str],
    avg_cost_raw: float,
    mark_px: Optional[float],
    multiplier: float,
) -> Tuple[float, bool]:
    """Normalize IB avgCost to price-per-unit.

    IB sometimes reports option avgCost in "dollars per contract" (already * multiplier).
    We infer the correct representation by comparing against current mark.
    """
    if sec_type == "OPT" and multiplier and multiplier > 1:
        try:
            return float(avg_cost_raw) / float(multiplier), True
        except Exception:
            return float(avg_cost_raw), False
    return float(avg_cost_raw), False

def format_contract_label(contract: Contract) -> str:
    sec_type = getattr(contract, "secType", "") or ""
    if sec_type == "OPT":
        expiry = _normalize_expiry(getattr(contract, "lastTradeDateOrContractMonth", None))
        right = getattr(contract, "right", "") or ""
        strike = getattr(contract, "strike", None)
        local_symbol = getattr(contract, "localSymbol", "") or ""
        strike_str = f"{float(strike):.1f}" if strike is not None else "?"
        if local_symbol:
            return f"{local_symbol} {right}{strike_str} {expiry or ''}".strip()
        return f"{contract.symbol} {expiry or ''} {right}{strike_str}".strip()
    if sec_type == "BAG":
        return f"{contract.symbol} BAG"
    return contract.symbol

def format_fill_label(fill: FillRow) -> str:
    if fill.sec_type == "OPT":
        strike = f"{float(fill.strike):.1f}" if fill.strike is not None else "?"
        label = f"{fill.symbol} {fill.expiry or ''} {fill.right or ''}{strike}"
        if fill.local_symbol:
            label = f"{fill.local_symbol} {fill.right or ''}{strike} {fill.expiry or ''}".strip()
        return label.strip()
    if fill.sec_type == "BAG":
        return f"{fill.symbol} BAG"
    return fill.symbol

def _order_leg_actions(order_action: str, contract: Contract) -> List[Tuple[int, str]]:
    action = (order_action or "").upper()
    legs: List[Tuple[int, str]] = []
    combo_legs = getattr(contract, "comboLegs", None)
    if combo_legs:
        for leg in combo_legs:
            leg_action = getattr(leg, "action", None)
            conid = getattr(leg, "conId", None)
            if not leg_action or not conid:
                continue
            leg_action = leg_action.upper()
            if action == "SELL":
                leg_action = "SELL" if leg_action == "BUY" else "BUY"
            legs.append((int(conid), leg_action))
        return legs
    conid = getattr(contract, "conId", None)
    if conid and action in {"BUY", "SELL"}:
        legs.append((int(conid), action))
    return legs

def normalize_contract_for_quotes(contract: Contract) -> Contract:
    """Make contracts returned by IB usable for market data requests.

    IB sometimes returns position contracts (especially OPT) with empty exchange.
    reqTickers/reqMktData will then fail with:
      Error 321: "Error validating request.-'bQ' : cause - Please enter exchange"
    """
    try:
        sec_type = getattr(contract, "secType", None)
        exch = getattr(contract, "exchange", None)
        if not exch:
            if sec_type in {"OPT", "STK", "IND", "FUT", "CASH", "BAG"}:
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

def get_default_account(ib: IB, positions: Optional[List[Any]] = None) -> str:
    if positions:
        try:
            acc = getattr(positions[0], "account", None)
            if acc:
                return str(acc)
        except Exception:
            pass
    try:
        accts = ib.managedAccounts()
        if accts:
            return str(accts[0])
    except Exception:
        pass
    return ""

# =============== IB 交互 ===============

def connect_ib(host='127.0.0.1', port=4002, clientId=9901, *, timeout: float = 30.0, readonly: bool = True) -> IB:
    if not _adhoc_ib_connect_allowed():
        raise RuntimeError(
            "IBKR ad-hoc connection blocked by IBKR_DISABLE_ADHOC_CONNECT=true."
        )
    cooldown_sec = float(os.getenv("IBKR_ADHOC_CONNECT_COOLDOWN_SEC", "120") or "120")
    guard = _acquire_adhoc_ib_connect_guard(cooldown_sec)
    ib = IB()
    try:
        ib.connect(host, port, clientId=clientId, timeout=float(timeout), readonly=bool(readonly))
    except Exception:
        _release_adhoc_ib_connect_guard(guard)
        raise

    original_disconnect = ib.disconnect

    def _disconnect_with_guard(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_disconnect(*args, **kwargs)
        finally:
            _release_adhoc_ib_connect_guard(guard)

    ib.disconnect = _disconnect_with_guard  # type: ignore[method-assign]
    return ib

def get_positions_snapshot(ib: IB, fetch_quotes: bool = False) -> List[dict]:
    pos = ib.positions()
    snapshots = []
    if not pos:
        return snapshots

    # Batch snapshot tickers for speed (optional).
    contracts = [normalize_contract_for_quotes(p.contract) for p in pos if getattr(p, "contract", None)]
    ticker_by_conid: Dict[int, Any] = {}
    if fetch_quotes:
        try:
            for i in range(0, len(contracts), 50):
                batch = contracts[i:i + 50]
                tickers = ib.reqTickers(*batch) if batch else []
                for tk in tickers:
                    conid = getattr(getattr(tk, "contract", None), "conId", None)
                    if conid:
                        ticker_by_conid[int(conid)] = tk
        except Exception:
            ticker_by_conid = {}
    for p in pos:
        contract = normalize_contract_for_quotes(p.contract)
        qty = p.position
        avg_cost_raw = float(getattr(p, "avgCost", 0.0) or 0.0)
        if avg_cost_raw < 0:
            avg_cost_raw = abs(avg_cost_raw)

        ticker = None
        if fetch_quotes:
            ticker = ticker_by_conid.get(int(getattr(contract, "conId", 0) or 0))
            if ticker is None:
                try:
                    ticker = ib.reqTickers(contract)[0]
                except Exception:
                    ticker = None

        bid = _valid_px(getattr(ticker, "bid", None) if ticker else None)
        ask = _valid_px(getattr(ticker, "ask", None) if ticker else None)
        last = _valid_px(getattr(ticker, "last", None) if ticker else None)
        mkt = _valid_px(ticker.marketPrice() if ticker else None)
        spread = (ask - bid) if (ask is not None and bid is not None) else None
        mark: Optional[float] = None
        if bid is not None and ask is not None:
            mark = (bid + ask) / 2.0
        elif last is not None:
            mark = last
        elif mkt is not None:
            mark = mkt

        multiplier = _contract_multiplier(contract)
        sec_type = getattr(contract, "secType", None)
        expiry = _normalize_expiry(getattr(contract, "lastTradeDateOrContractMonth", None))
        right = getattr(contract, "right", None)
        strike = getattr(contract, "strike", None)
        local_symbol = getattr(contract, "localSymbol", None)
        avg_cost_px, avg_cost_multiplied = _normalize_avg_cost_px(sec_type, avg_cost_raw, mark, multiplier)

        value: Optional[float] = None
        unreal: Optional[float] = None
        pnl_pct: Optional[float] = None
        mtm_src: Optional[str] = None
        if mark is not None:
            value = abs(qty) * mark * multiplier
            unreal = (mark - avg_cost_px) * qty * multiplier
            pnl_pct = (mark - avg_cost_px) / avg_cost_px * 100 if avg_cost_px else None
            mtm_src = "quotes"

        snapshots.append({
            'symbol': contract.symbol,
            'conId': contract.conId,
            'exchange': contract.exchange,
            'qty': qty,
            'avg_cost': avg_cost_px,
            'avg_cost_raw': avg_cost_raw,
            'avg_cost_multiplied': avg_cost_multiplied,
            'last': mark,
            'bid': bid,
            'ask': ask,
            'spread': spread,
            'value': value,
            'unreal': unreal,
            'pnl_pct': pnl_pct,
            'mtm_src': mtm_src,
            'sec_type': sec_type,
            'right': right,
            'strike': strike,
            'expiry': expiry,
            'local_symbol': local_symbol,
            'multiplier': multiplier,
        })

    # Fallback: if quotes are unavailable (e.g., IB error 10197 competing session),
    # use account-side PnLSingle to populate MTM/value/unrealized.
    missing = [s for s in snapshots if s.get("last") is None]
    if missing:
        account = get_default_account(ib, pos)
        use_fallback = os.getenv("IBKR_USE_PNL_SINGLE", "true").lower() == "true"
        if account and use_fallback:
            try:
                pnl_objs = {int(s["conId"]): ib.reqPnLSingle(account, "", int(s["conId"])) for s in missing}
                ib.sleep(0.8)
                for s in missing:
                    conid = int(s["conId"])
                    pnl = pnl_objs.get(conid)
                    if not pnl:
                        continue
                    try:
                        value = float(getattr(pnl, "value", float("nan")))
                        unreal = float(getattr(pnl, "unrealizedPnL", float("nan")))
                    except Exception:
                        continue
                    if math.isnan(value):
                        continue
                    qty = float(s.get("qty") or 0.0)
                    mult = float(s.get("multiplier") or 1.0)
                    v_abs = abs(value)
                    mark = (v_abs / (abs(qty) * mult)) if qty and mult else None
                    s["last"] = mark
                    s["value"] = v_abs
                    s["unreal"] = None if math.isnan(unreal) else unreal
                    if s["unreal"] is not None and s.get("avg_cost") and qty and mult:
                        denom = abs(qty) * float(s["avg_cost"]) * mult
                        s["pnl_pct"] = (float(s["unreal"]) / denom) * 100 if denom else None
                    s["mtm_src"] = "pnlSingle"
            except Exception:
                pass
            finally:
                for conid in {int(s["conId"]) for s in missing}:
                    try:
                        ib.cancelPnLSingle(account, "", int(conid))
                    except Exception:
                        continue

    return snapshots

def fetch_today_fills_with_commissions(ib: IB) -> List[FillRow]:
    """
    优先用 reqExecutions() 拿当天全部成交，再尽可能从 fills() 关联到 CommissionReport。
    """
    start, end = today_et_dates()
    ef = ExecutionFilter(time=str(start.strftime('%Y%m%d %H:%M:%S')))
    exec_details = ib.reqExecutions(ef)
    ib.sleep(0.2)

    # 建立 (permId, execId, orderId) -> commission 映射（如果拿得到）
    commission_map: Dict[Tuple[int, str, int], Tuple[float, str]] = {}
    try:
        fills = ib.fills()  # 连接后可拿到部分 fills（含 CommissionReport）
        for f in fills:
            cr = f.commissionReport
            if cr:
                key = (f.execution.permId, f.execution.execId, f.execution.orderId)
                commission_map[key] = (cr.commission or 0.0, cr.currency or "USD")
    except Exception:
        pass

    rows: List[FillRow] = []
    for ed in exec_details:
        ex = ed.execution
        t = parse_exec_time(ex.time) or now_et()
        if not (start <= t <= end):
            continue
        commission = 0.0
        ccy = "USD"
        key = (ex.permId, ex.execId, ex.orderId)
        if key in commission_map:
            commission, ccy = commission_map[key]
        contract = ed.contract
        expiry = _normalize_expiry(getattr(contract, "lastTradeDateOrContractMonth", None))
        multiplier = _float_or_none(getattr(contract, "multiplier", None))

        rows.append(FillRow(
            symbol=ed.contract.symbol,
            conId=ed.contract.conId,
            side=ex.side,                     # 'BOT' or 'SLD'
            shares=ex.shares,
            price=ex.price,
            time=t,
            orderId=ex.orderId,
            sec_type=getattr(contract, "secType", None),
            right=getattr(contract, "right", None),
            strike=getattr(contract, "strike", None),
            expiry=expiry,
            local_symbol=getattr(contract, "localSymbol", None),
            multiplier=multiplier,
            commission=commission or 0.0,
            currency=ccy
        ))
    # 按时间排序
    rows.sort(key=lambda r: r.time)
    return rows

# =============== 回合交易（FIFO 配对） ===============

def fifo_round_trips(fills: List[FillRow]) -> Tuple[List[RoundTrip], Dict[Tuple[int, str], List[FillRow]]]:
    """FIFO 配对买卖，支持 LONG/SHORT（适配期权：SELL→BUY 也能正确算 P&L）。

    分组维度： (conId, symbol)。对每个合约按时间顺序处理 fills，维护一个 open lots 队列：
      - 新成交若方向与当前 open lots 相反，则优先平仓并生成 RoundTrip
      - 剩余未平部分继续作为 open lot 挂在队列中
    """
    by_key: Dict[Tuple[int, str], List[FillRow]] = defaultdict(list)
    for f in fills:
        by_key[(int(f.conId), f.symbol)].append(f)

    round_trips: List[RoundTrip] = []
    open_lots: Dict[Tuple[int, str], List[FillRow]] = {}

    def _side_sign(side: str) -> int:
        return 1 if side == "BOT" else -1

    for (con_id, symbol), flist in by_key.items():
        flist = sorted(flist, key=lambda x: x.time)
        lots: deque[FillRow] = deque()

        for f in flist:
            if f.side not in {"BOT", "SLD"}:
                continue
            fill_sign = _side_sign(f.side)
            remaining_qty = float(f.shares or 0.0)
            if remaining_qty <= 0:
                continue

            fill_fee_unit = (float(f.commission or 0.0) / float(f.shares)) if f.shares else 0.0
            remaining_fee = float(f.commission or 0.0)

            # Match against existing opposite lots
            while lots and remaining_qty > 1e-8:
                lot = lots[0]
                lot_sign = _side_sign(lot.side)
                if lot_sign == fill_sign:
                    break

                match_qty = min(float(lot.shares), remaining_qty)
                if match_qty <= 0:
                    break

                lot_fee_unit = (float(lot.commission or 0.0) / float(lot.shares)) if lot.shares else 0.0
                fees = match_qty * (lot_fee_unit + fill_fee_unit)
                remaining_fee = max(0.0, remaining_fee - match_qty * fill_fee_unit)

                mult = float(lot.multiplier or f.multiplier or (100.0 if (lot.sec_type == "OPT" or f.sec_type == "OPT") else 1.0))

                if lot_sign == 1 and fill_sign == -1:
                    # LONG: buy open lot -> sell close fill
                    entry_price = float(lot.price)
                    exit_price = float(f.price)
                    entry_time = lot.time
                    exit_time = f.time
                    side = "LONG"
                    gross = (exit_price - entry_price) * match_qty * mult
                    ret_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price else 0.0
                else:
                    # SHORT: sell open lot -> buy close fill
                    entry_price = float(lot.price)
                    exit_price = float(f.price)
                    entry_time = lot.time
                    exit_time = f.time
                    side = "SHORT"
                    gross = (entry_price - exit_price) * match_qty * mult
                    ret_pct = ((entry_price - exit_price) / entry_price) * 100 if entry_price else 0.0

                net = gross - fees
                hold_sec = int(abs((exit_time - entry_time).total_seconds()))

                rt = RoundTrip(
                    symbol=symbol,
                    conId=con_id,
                    qty=match_qty,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    entry_time=entry_time,
                    exit_time=exit_time,
                    side=side,
                    gross_pnl=gross,
                    net_pnl=net,
                    ret_pct=ret_pct,
                    fees=fees,
                    holding_sec=hold_sec,
                    sec_type=lot.sec_type or f.sec_type,
                    right=lot.right or f.right,
                    strike=lot.strike if lot.strike is not None else f.strike,
                    expiry=lot.expiry or f.expiry,
                    local_symbol=lot.local_symbol or f.local_symbol,
                    multiplier=mult,
                )
                round_trips.append(rt)

                # Reduce lot / fill remaining
                lot.shares -= match_qty
                lot.commission = max(0.0, float(lot.commission or 0.0) - match_qty * lot_fee_unit)
                remaining_qty -= match_qty

                if lot.shares <= 1e-8:
                    lots.popleft()

            # Anything left becomes a new open lot
            if remaining_qty > 1e-8:
                lot_new = FillRow(**vars(f))
                lot_new.shares = remaining_qty
                lot_new.commission = remaining_fee
                lots.append(lot_new)

        if lots:
            open_lots[(con_id, symbol)] = list(lots)

    round_trips.sort(key=lambda x: x.exit_time)
    return round_trips, open_lots

# =============== MFE/MAE 计算（1m K 线） ===============

def fetch_intraday_bars(ib: IB, symbol: str) -> List[Tuple[datetime, float, float]]:
    """
    拉取当天 1 分钟 K 线（含盘前盘后），返回 [(dt, high, low)]
    """
    contract = Stock(symbol, 'SMART', 'USD')
    # 取 '1 D' 的 1m bars（IB 会返回最近一段，包含今天）
    bars = ib.reqHistoricalData(
        contract,
        endDateTime='',
        durationStr='1 D',
        barSizeSetting='1 min',
        whatToShow='TRADES',
        useRTH=False,
        formatDate=1
    )
    out = []
    for b in bars:
        # b.date 为 'YYYYMMDD  HH:MM:SS' 或 datetime，ib_insync 会转 datetime
        dt = b.date if isinstance(b.date, datetime) else util.parseIBDatetime(b.date)
        # 强制设定 ET（历史数据一般返回本地时区或 UTC，保险起见统一本地 ET）
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        out.append((dt.astimezone(ET), float(b.high), float(b.low)))
    return out

def fill_mfe_mae_for_trades(ib: IB, round_trips: List[RoundTrip]) -> None:
    """
    对每个 symbol 聚合拉一次 1m bars，然后为该 symbol 的所有 round trips 计算 MFE/MAE。
    """
    by_sym: Dict[str, List[RoundTrip]] = defaultdict(list)
    for rt in round_trips:
        if rt.sec_type == "OPT":
            continue
        by_sym[rt.symbol].append(rt)

    for symbol, rts in by_sym.items():
        try:
            bars = fetch_intraday_bars(ib, symbol)
        except Exception as e:
            print(f"⚠️  无法拉取 {symbol} 的 1m 数据用于 MFE/MAE: {e}")
            continue

        # 索引便于区间查询
        for rt in rts:
            start_t, end_t = rt.entry_time, rt.exit_time
            highs = [h for (t, h, l) in bars if start_t <= t <= end_t]
            lows  = [l for (t, h, l) in bars if start_t <= t <= end_t]
            if not highs or not lows:
                continue
            max_high = max(highs)
            min_low = min(lows)

            if rt.side == 'LONG':
                rt.mfe_pct = ((max_high - rt.entry_price) / rt.entry_price) * 100.0
                rt.mae_pct = ((rt.entry_price - min_low) / rt.entry_price) * 100.0
                if rt.mfe_pct and rt.mfe_pct > 0:
                    actual_pct = ((rt.exit_price - rt.entry_price) / rt.entry_price) * 100.0
                    rt.capture_pct = max(0.0, min(100.0, (actual_pct / rt.mfe_pct) * 100.0))
            else:
                # 本例策略默认做多；若做空请按相反方向计算
                pass

# =============== 财务汇总与分析 ===============

def equity_curve_and_drawdown(round_trips: List[RoundTrip]) -> Tuple[List[float], float]:
    """
    用回合交易的净利构造逐笔累积权益曲线，返回 (equity_list, max_drawdown)
    """
    eq = []
    c = 0.0
    peak = 0.0
    max_dd = 0.0
    for rt in round_trips:
        c += rt.net_pnl
        eq.append(c)
        peak = max(peak, c)
        dd = (peak - c)
        max_dd = max(max_dd, dd)
    return eq, max_dd

def summarize_round_trips(round_trips: List[RoundTrip]) -> Dict[str, float]:
    wins = [rt for rt in round_trips if rt.net_pnl > 0]
    losses = [rt for rt in round_trips if rt.net_pnl <= 0]
    win_rate = (len(wins) / len(round_trips) * 100) if round_trips else 0.0
    avg_win = sum(rt.net_pnl for rt in wins) / len(wins) if wins else 0.0
    avg_loss = sum(rt.net_pnl for rt in losses) / len(losses) if losses else 0.0
    gross_profit = sum(rt.net_pnl for rt in wins)
    gross_loss = abs(sum(rt.net_pnl for rt in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float('inf')
    _, max_dd = equity_curve_and_drawdown(round_trips)

    return {
        'trades': len(round_trips),
        'win_rate_pct': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'max_drawdown': max_dd,
        'net_realized': sum(rt.net_pnl for rt in round_trips),
        'gross_realized': sum(rt.gross_pnl for rt in round_trips),
        'fees_total': sum(rt.fees for rt in round_trips),
        'median_hold_sec': int(sorted([rt.holding_sec for rt in round_trips])[len(round_trips)//2]) if round_trips else 0
    }

def reason_tags(rt: RoundTrip) -> List[str]:
    """
    为每个回合给出简单“原因标签”，帮助解释盈亏。
    """
    tags = []
    # 费用压力
    if rt.gross_pnl > 0 and rt.fees > 0 and (rt.fees / rt.gross_pnl) > 0.25:
        tags.append("费用偏高侵蚀利润")
    # 买顶/入场后不利
    if rt.mae_pct is not None and rt.mae_pct >= 1.0 and rt.holding_sec <= 600:
        tags.append("入场后快速不利（可能追顶）")
    # 没吃满行情
    if rt.mfe_pct and rt.mfe_pct >= 2.0 and (rt.capture_pct or 0) < 40:
        tags.append("盈利捕获率低（止盈偏早或回吐过多）")
    # 拿太久无效益
    if rt.holding_sec > 3600 and abs(rt.ret_pct) < 0.3:
        tags.append("持仓过久且收益有限")
    return tags

# =============== 订单检查（重复/冲突/幽灵） ===============

def is_open_status(status: str) -> bool:
    s = (status or "").replace(" ", "").upper()
    return s not in {"FILLED", "CANCELLED", "CANCELED", "INACTIVE", "REJECTED", "APICANCELLED"}

def analyze_open_trades(trades: Iterable) -> Dict[str, Any]:
    per_conid_side: Dict[int, Dict[str, List[int]]] = defaultdict(lambda: {"BUY": [], "SELL": []})
    order_to_legs: Dict[int, List[Tuple[int, str]]] = {}
    order_ref_to_ids: Dict[str, List[int]] = defaultdict(list)
    for t in trades:
        order = t.order
        contract = t.contract
        if not order or not contract:
            continue
        status = getattr(getattr(t, "orderStatus", None), "status", "") or ""
        if not is_open_status(status):
            continue
        order_id = int(order.orderId)
        legs = _order_leg_actions(order.action, contract)
        order_to_legs[order_id] = legs
        if getattr(order, "orderRef", None):
            order_ref_to_ids[str(order.orderRef)].append(order_id)
        for conid, side in legs:
            per_conid_side[conid].setdefault(side, [])
            per_conid_side[conid][side].append(order_id)

    duplicates: Dict[int, Dict[str, List[int]]] = {}
    conflicts: Dict[int, Dict[str, List[int]]] = {}
    for conid, sides in per_conid_side.items():
        dup_sides = {s: ids for s, ids in sides.items() if len(ids) > 1}
        if dup_sides:
            duplicates[conid] = dup_sides
        if sides.get("BUY") and sides.get("SELL"):
            conflicts[conid] = {"BUY": sides["BUY"], "SELL": sides["SELL"]}

    return {
        "per_conid_side": per_conid_side,
        "order_to_legs": order_to_legs,
        "duplicates": duplicates,
        "conflicts": conflicts,
        "order_ref_dupes": {k: v for k, v in order_ref_to_ids.items() if len(v) > 1},
    }

def fetch_account_summary_usd(ib: IB) -> Dict[str, float]:
    tags = {
        "NetLiquidation",
        "TotalCashValue",
        "GrossPositionValue",
        "UnrealizedPnL",
        "RealizedPnL",
        "BuyingPower",
        "AvailableFunds",
    }
    out: Dict[str, float] = {}
    try:
        for av in ib.accountSummary():
            if av.currency != "USD":
                continue
            if av.tag in tags:
                try:
                    out[av.tag] = float(av.value)
                except Exception:
                    continue
    except Exception:
        return out
    return out

def trade_age_sec(trade: Any) -> Optional[int]:
    try:
        logs = getattr(trade, "log", None)
        if logs:
            t0 = getattr(logs[0], "time", None)
            if isinstance(t0, datetime):
                dt = t0.astimezone(ET) if t0.tzinfo else t0.replace(tzinfo=ET)
                return int((now_et() - dt).total_seconds())
        t1 = getattr(trade, "time", None)
        if isinstance(t1, datetime):
            dt = t1.astimezone(ET) if t1.tzinfo else t1.replace(tzinfo=ET)
            return int((now_et() - dt).total_seconds())
    except Exception:
        return None
    return None

def pair_option_verticals(opt_positions: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Best-effort pairing of option legs into vertical spreads.

    Groups by (underlying symbol, expiry, right). Pairs opposite-signed quantities
    by closest strike, preferring "normal" vertical direction:
      - Puts: long strike < short strike
      - Calls: long strike > short strike

    Returns:
      spreads: list of paired spreads (qty, strikes, MTM, est max risk)
      residuals: list of remaining unpaired legs
    """
    groups: Dict[Tuple[str, str, str], List[dict]] = defaultdict(list)
    for p in opt_positions:
        expiry = p.get("expiry")
        right = p.get("right")
        strike = p.get("strike")
        qty = float(p.get("qty") or 0.0)
        if not expiry or not right or strike is None or abs(qty) <= 0:
            continue
        key = (str(p.get("symbol") or ""), str(expiry), str(right))
        groups[key].append(p)

    spreads: List[dict] = []
    residuals: List[dict] = []

    for (underlying, expiry, right), legs in groups.items():
        longs = []
        shorts = []
        for p in legs:
            qty = float(p.get("qty") or 0.0)
            strike = float(p.get("strike") or 0.0)
            if qty > 0:
                longs.append({"strike": strike, "qty": qty, "p": p})
            elif qty < 0:
                shorts.append({"strike": strike, "qty": abs(qty), "p": p})

        def best_long_idx(short_strike: float) -> Optional[int]:
            best = None
            best_score = None
            for i, lg in enumerate(longs):
                if lg["qty"] <= 0:
                    continue
                width = abs(short_strike - lg["strike"])
                if width <= 0:
                    continue
                direction_ok = (right == "P" and lg["strike"] < short_strike) or (right == "C" and lg["strike"] > short_strike)
                score = (0 if direction_ok else 1, width)
                if best_score is None or score < best_score:
                    best_score = score
                    best = i
            return best

        for sh in sorted(shorts, key=lambda x: x["strike"]):
            short_strike = float(sh["strike"])
            while sh["qty"] > 0:
                idx = best_long_idx(short_strike)
                if idx is None:
                    break
                lg = longs[idx]
                pair_qty = min(sh["qty"], lg["qty"])
                if pair_qty <= 0:
                    break
                sh["qty"] -= pair_qty
                lg["qty"] -= pair_qty

                short_p = sh["p"]
                long_p = lg["p"]
                mult = float(short_p.get("multiplier") or long_p.get("multiplier") or 100.0)
                short_avg = float(short_p.get("avg_cost") or 0.0)
                long_avg = float(long_p.get("avg_cost") or 0.0)
                short_mark = _valid_px(short_p.get("last"))
                long_mark = _valid_px(long_p.get("last"))
                entry_net = (short_avg * (-pair_qty) + long_avg * (pair_qty)) * mult
                unreal = None
                mark_to_close_per = None
                if short_mark is not None and long_mark is not None:
                    mark_net = (short_mark * (-pair_qty) + long_mark * (pair_qty)) * mult
                    unreal = ((short_mark - short_avg) * (-pair_qty) + (long_mark - long_avg) * (pair_qty)) * mult
                    mark_to_close_per = (-mark_net / (mult * pair_qty)) if pair_qty else None

                width = abs(short_strike - float(lg["strike"]))
                entry_per = entry_net / (mult * pair_qty) if pair_qty else 0.0
                entry_credit_per = (short_avg - long_avg)

                if entry_net < 0:
                    credit = -entry_per
                    max_profit = -entry_net
                    max_loss = width * mult * pair_qty + entry_net
                else:
                    debit = entry_per
                    max_loss = entry_net
                    max_profit = width * mult * pair_qty - entry_net
                    _ = debit

                spreads.append({
                    "underlying": underlying,
                    "expiry": expiry,
                    "right": right,
                    "qty": pair_qty,
                    "short_strike": short_strike,
                    "long_strike": float(lg["strike"]),
                    "width": width,
                    "entry_net": entry_net,
                    "mark_to_close_per": mark_to_close_per,
                    "unreal": unreal,
                    "entry_credit_per": entry_credit_per,
                    "max_profit": max_profit,
                    "max_loss": max_loss,
                })

        # Remaining legs (unpaired)
        for lg in longs:
            if lg["qty"] > 0:
                residuals.append({**lg["p"], "unpaired_qty": lg["qty"]})
        for sh in shorts:
            if sh["qty"] > 0:
                residuals.append({**sh["p"], "unpaired_qty": -sh["qty"]})

    spreads.sort(key=lambda x: (x["underlying"], x["expiry"], x["right"], x["short_strike"], x["long_strike"]))
    return spreads, residuals

# =============== 主流程 ===============

def main():
    util.patchAsyncio()

    parser = argparse.ArgumentParser(description="IBKR daily replay / account audit")
    parser.add_argument("--host", default=os.getenv("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("IBKR_PORT", "4002")))
    parser.add_argument("--client-id", type=int, default=int(os.getenv("IBKR_ADHOC_CLIENT_ID", "9901")))
    parser.add_argument("--connect-timeout", type=float, default=float(os.getenv("IBKR_ADHOC_CONNECT_TIMEOUT", "30")))
    parser.add_argument(
        "--connect-cooldown-sec",
        type=float,
        default=float(os.getenv("IBKR_ADHOC_CONNECT_COOLDOWN_SEC", "120")),
        help="Minimum seconds between ad-hoc IBKR sessions.",
    )
    parser.add_argument("--read-write", action="store_true", help="Disable readonly mode; audit scripts should not need this.")
    parser.add_argument("--quotes", action="store_true", help="Request market quotes via reqTickers (may trigger 10197)")
    parser.add_argument("--bars", action="store_true", help="Request historical bars for MFE/MAE (market data)")
    args = parser.parse_args()

    fetch_quotes = args.quotes or (os.getenv("IBKR_FETCH_QUOTES", "false").lower() == "true")
    fetch_bars = args.bars or (os.getenv("IBKR_FETCH_BARS", "false").lower() == "true")
    print("="*96)
    print(f"IBKR Daily Replay @ {now_et().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print("="*96)

    if not _adhoc_ib_connect_allowed():
        print("\nBLOCKED: IBKR API connection is disabled for this ad-hoc audit script.")
        print("Reason: IBKR_DISABLE_ADHOC_CONNECT=true")
        sys.exit(2)

    # ---- 连接 ----
    ib = IB()
    connect_guard = None
    try:
        connect_guard = _acquire_adhoc_ib_connect_guard(float(args.connect_cooldown_sec))
        readonly = not bool(args.read_write)
        print(
            f"\nConnecting to IBKR ({args.host}:{args.port})...  "
            f"clientId={int(args.client_id)} readonly={readonly} "
            f"Quotes={'ON' if fetch_quotes else 'OFF'} Bars={'ON' if fetch_bars else 'OFF'}"
        )
        ib.connect(
            args.host,
            int(args.port),
            clientId=int(args.client_id),
            timeout=float(args.connect_timeout),
            readonly=readonly,
        )
        print("✅ Connected.\n")
    except Exception as e:
        _release_adhoc_ib_connect_guard(connect_guard)
        print(f"❌ Failed to connect: {e}")
        sys.exit(1)

    md_type = None
    if fetch_quotes:
        # Prefer delayed quotes for audit scripts (avoids IB error 10197 "competing live session").
        try:
            md_type = int(os.getenv("IBKR_MKT_DATA_TYPE", "3"))
            ib.reqMarketDataType(md_type)
        except Exception:
            md_type = None

    # Track IB gateway connectivity + common errors for operator visibility.
    connectivity_lost = {"val": False}
    recent_errors: List[Tuple[int, int, str]] = []

    def _on_ib_error(req_id: int, error_code: int, error_string: str, contract: Any = None) -> None:
        recent_errors.append((int(req_id), int(error_code), str(error_string or "")))
        if int(error_code) == 1100:
            connectivity_lost["val"] = True
        elif int(error_code) == 1102:
            connectivity_lost["val"] = False

    try:
        ib.errorEvent += _on_ib_error
    except Exception:
        pass

    acct_usd = fetch_account_summary_usd(ib)

    # ---- 今日成交（含佣金）----
    fills_all = fetch_today_fills_with_commissions(ib)
    fills_combo = [f for f in fills_all if f.sec_type == "BAG"]
    fills = [f for f in fills_all if f.sec_type != "BAG"]
    rts: List[RoundTrip] = []
    open_lots: Dict[Tuple[int, str], List[FillRow]] = {}
    if fills:
        rts, open_lots = fifo_round_trips(fills)

    total_commission = sum(float(f.commission or 0.0) for f in fills_all)

    summ = summarize_round_trips(rts) if rts else {
        "trades": 0,
        "win_rate_pct": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "profit_factor": 0.0,
        "max_drawdown": 0.0,
        "net_realized": 0.0,
        "gross_realized": 0.0,
        "fees_total": 0.0,
        "median_hold_sec": 0,
    }

    realized_by_conid: Dict[int, float] = defaultdict(float)
    for rt in rts:
        realized_by_conid[int(rt.conId)] += float(rt.net_pnl)

    pos_snaps = get_positions_snapshot(ib, fetch_quotes=fetch_quotes)
    calc_covered = sum(1 for p in pos_snaps if p.get("value") is not None)
    missing_quotes = (len(pos_snaps) - calc_covered) if pos_snaps else 0
    total_val = sum(float(p["value"]) for p in pos_snaps if p.get("value") is not None) if pos_snaps else 0.0
    total_unreal = sum(float(p["unreal"]) for p in pos_snaps if p.get("unreal") is not None) if pos_snaps else 0.0
    opt_positions = [p for p in pos_snaps if p.get("sec_type") == "OPT"]
    stk_positions = [p for p in pos_snaps if p.get("sec_type") != "OPT"]
    opt_contracts = sum(abs(float(p.get("qty") or 0.0)) for p in opt_positions)
    opt_short_contracts = sum(abs(float(p.get("qty") or 0.0)) for p in opt_positions if float(p.get("qty") or 0.0) < 0)
    opt_long_contracts = sum(abs(float(p.get("qty") or 0.0)) for p in opt_positions if float(p.get("qty") or 0.0) > 0)
    opt_value = sum(float(p.get("value") or 0.0) for p in opt_positions if p.get("value") is not None)
    stk_value = sum(float(p.get("value") or 0.0) for p in stk_positions if p.get("value") is not None)
    opt_spreads, opt_unpaired = pair_option_verticals(opt_positions) if opt_positions else ([], [])
    unpaired_opt_qty = sum(abs(float(p.get("unpaired_qty") or 0.0)) for p in opt_unpaired) if opt_unpaired else 0.0

    # ---- OPEN ORDERS snapshot (for alerts + later detail) ----
    ib.reqAllOpenOrders()
    ib.sleep(0.5)
    open_orders = ib.openOrders()
    open_trades = ib.openTrades()
    open_order_ids = {o.orderId for o in open_orders}
    analysis = analyze_open_trades(open_trades)
    ghost_ids = {
        int(t.order.orderId)
        for t in open_trades
        if is_open_status(t.orderStatus.status) and t.order.orderId not in open_order_ids
    }
    open_trade_count = sum(1 for t in open_trades if is_open_status(t.orderStatus.status))
    unfilled_open = sum(
        1
        for t in open_trades
        if is_open_status(t.orderStatus.status)
        and float(getattr(t.orderStatus, "filled", 0.0) or 0.0) <= 0.0
        and float(getattr(t.orderStatus, "remaining", 0.0) or 0.0) > 0.0
    )
    pending_states = {"PendingCancel", "ApiPending", "PendingSubmit", "PreSubmitted"}
    pending_open = sum(1 for t in open_trades if (t.orderStatus.status in pending_states))

    # ---- EXECUTIVE SUMMARY ----
    print("\n" + "="*96)
    print("📌 EXECUTIVE SUMMARY (Boss View)")
    print("="*96)
    net_liq = acct_usd.get("NetLiquidation")
    cash = acct_usd.get("TotalCashValue")
    buying_power = acct_usd.get("BuyingPower")
    avail_funds = acct_usd.get("AvailableFunds")
    ib_realized = acct_usd.get("RealizedPnL")
    ib_unreal = acct_usd.get("UnrealizedPnL")

    print(
        f"NetLiq={human_money(net_liq) if net_liq is not None else 'N/A'}  "
        f"Cash={human_money(cash) if cash is not None else 'N/A'}  "
        f"BuyingPower={human_money(buying_power) if buying_power is not None else 'N/A'}  "
        f"AvailFunds={human_money(avail_funds) if avail_funds is not None else 'N/A'}"
    )
    coverage_str = f"{calc_covered}/{len(pos_snaps)}" if pos_snaps else "0/0"
    if pos_snaps and calc_covered == 0:
        print(f"TodayPnL(Calc)=N/A (market quotes unavailable; coverage {coverage_str})  Fees={human_money(total_commission)}")
    else:
        est_total_pnl = float(summ["net_realized"] or 0.0) + float(total_unreal or 0.0)
        print(
            f"TodayPnL(Calc)=Realized{human_money(float(summ['net_realized'] or 0.0))} + "
            f"Unrealized{human_money(float(total_unreal or 0.0))} = {human_money(est_total_pnl)}  "
            f"Fees={human_money(total_commission)}  MTMCoverage={coverage_str}"
        )
    if ib_realized is not None or ib_unreal is not None:
        print(
            f"IBPnL(Reported)=Realized{human_money(ib_realized) if ib_realized is not None else 'N/A'} + "
            f"Unrealized{human_money(ib_unreal) if ib_unreal is not None else 'N/A'}"
        )
        try:
            if ib_realized is not None and (not pos_snaps or calc_covered == len(pos_snaps)):
                realized_gap = float(summ["net_realized"] or 0.0) - float(ib_realized)
                print(f"PnLGaps: Realized(calc-IB)={human_money(realized_gap)}")
            if ib_unreal is not None and (not pos_snaps or calc_covered == len(pos_snaps)):
                unreal_gap = float(total_unreal or 0.0) - float(ib_unreal)
                print(f"PnLGaps: Unrealized(calc-IB)={human_money(unreal_gap)}")
            if pos_snaps and calc_covered != len(pos_snaps):
                print(f"PnLGaps: skipped (incomplete MTM coverage {coverage_str})")
        except Exception:
            pass
    print(
        f"TradesClosed={int(summ['trades'])}  WinRate={summ['win_rate_pct']:.1f}%  "
        f"ProfitFactor={summ['profit_factor']:.2f}  MaxDD={human_money(summ['max_drawdown'])}"
    )
    print(
        f"OpenPositions={len(pos_snaps)} (OPT={len(opt_positions)} contracts={int(opt_contracts)}  "
        f"STK/Other={len(stk_positions)})  OpenOrders={open_trade_count} (Unfilled={unfilled_open} Pending={pending_open})"
    )
    if pos_snaps:
        src_counts = defaultdict(int)
        for p in pos_snaps:
            src_counts[str(p.get("mtm_src") or "none")] += 1
        print(f"MTMSource: quotes={src_counts.get('quotes', 0)} pnlSingle={src_counts.get('pnlSingle', 0)} none={src_counts.get('none', 0)}")
    if opt_positions:
        print(f"OptionsContracts: Long={int(opt_long_contracts)}  Short={int(opt_short_contracts)}")
        print(f"OptionsSpreadsPaired={len(opt_spreads)}  UnpairedOptionLegsQty={int(unpaired_opt_qty)}")
    if pos_snaps and calc_covered == 0:
        gv = acct_usd.get("GrossPositionValue")
        print(f"MTMPositionValue=N/A (quotes unavailable)  IB_GrossPositionValue={human_money(gv) if gv is not None else 'N/A'}")
    else:
        print(f"MTMPositionValue={human_money(total_val)} (OPT={human_money(opt_value)}  STK/Other={human_money(stk_value)})")
    print(
        f"Alerts: Conflicts(conId)={len(analysis.get('conflicts', {}))}  "
        f"Duplicates(conId+side)={len(analysis.get('duplicates', {}))}  "
        f"DuplicateOrderRef={len(analysis.get('order_ref_dupes', {}))}  "
        f"GhostCandidates={len(ghost_ids)}  UnmatchedLots={len(open_lots)}  "
        f"{'MissingQuotes' if fetch_quotes else 'MissingMTM'}={missing_quotes}"
    )
    if connectivity_lost["val"]:
        print("IBConnectivity=LOST (error 1100)  ⚠️ MTM/quotes may be invalid; wait for Gateway/TWS reconnect (1102).")
    if md_type is not None:
        md_label = {1: "LIVE", 2: "FROZEN", 3: "DELAYED", 4: "DELAYED_FROZEN"}.get(md_type, str(md_type))
        print(f"MarketDataType={md_label} (set via IBKR_MKT_DATA_TYPE)")
    if not fetch_quotes and not fetch_bars:
        print("MarketDataRequests=OFF (no reqTickers / no reqHistoricalData)")
    # Summarize common IB errors (reduce log hunting).
    if recent_errors:
        codes = defaultdict(int)
        for _, code, _ in recent_errors:
            codes[int(code)] += 1
        important = [10197, 321, 1100, 1102]
        parts = [f"{c}x{codes[c]}" for c in important if c in codes]
        if parts:
            print(f"IBErrors: {' '.join(parts)}")
        if 10197 in codes:
            print("MarketData=BLOCKED_BY_COMPETING_LIVE_SESSION (10197) → use delayed quotes (IBKR_MKT_DATA_TYPE=3) or stop the other live market-data session.")

    if pos_snaps:
        print("-"*96)
        print("Top MTM movers (by |Unrealized|):")
        top = sorted(pos_snaps, key=lambda p: abs(float(p.get("unreal") or 0.0)), reverse=True)[:5]
        for p in top:
            label = p["symbol"]
            if p.get("sec_type") == "OPT":
                strike = p.get("strike")
                strike_str = f"{float(strike):.1f}" if strike is not None else "?"
                local = p.get("local_symbol") or ""
                right = p.get("right") or ""
                expiry = p.get("expiry") or ""
                label = (f"{local} {right}{strike_str} {expiry}".strip()
                         if local else f"{p['symbol']} {expiry} {right}{strike_str}".strip())
            print(f"  {label:<28} Qty={p['qty']:+6.0f}  Unreal={fmt_money(p.get('unreal'))}")

    if (
        analysis.get("duplicates")
        or analysis.get("conflicts")
        or analysis.get("order_ref_dupes")
        or ghost_ids
        or open_lots
        or unpaired_opt_qty
        or missing_quotes
    ):
        print("\n" + "="*96)
        print("⚠️  RISK / OPS ALERTS (Action Items)")
        print("="*96)
        if analysis.get("conflicts"):
            print(f"- Opposite-side open orders detected on {len(analysis['conflicts'])} conId(s) (regulatory reject risk).")
        if analysis.get("duplicates"):
            print(f"- Duplicate open orders detected on {len(analysis['duplicates'])} conId(s) (possible double-submit).")
        if analysis.get("order_ref_dupes"):
            print(f"- Duplicate orderRef detected: {len(analysis['order_ref_dupes'])} group(s) (idempotency issue).")
        if ghost_ids:
            print(f"- Ghost candidates: {sorted(ghost_ids)} (open status but not in openOrders snapshot).")
        if open_lots:
            print(f"- Unmatched lots from today's executions: {len(open_lots)} instrument(s) (position not closed today).")
        if unpaired_opt_qty:
            print(f"- Unpaired option legs detected: {int(unpaired_opt_qty)} contract(s) (possible naked exposure).")
        if missing_quotes:
            if fetch_quotes:
                print(f"- Missing market quotes on {missing_quotes} position(s); MTM is incomplete.")
            else:
                print(f"- Missing MTM valuation on {missing_quotes} position(s); IB PnLSingle fallback did not return values.")

    # ---- 头寸快照（未实现 P&L）----
    print("\n" + "="*96)
    print("📍 OPEN POSITIONS (Unrealized P&L)")
    print("="*96)
    if pos_snaps:
        pos_sorted = sorted(pos_snaps, key=lambda x: abs(float(x.get("unreal") or 0.0)), reverse=True)
        opt_sorted = [p for p in pos_sorted if p.get("sec_type") == "OPT"]
        other_sorted = [p for p in pos_sorted if p.get("sec_type") != "OPT"]

        if opt_sorted:
            if opt_spreads:
                print("OPTION SPREAD VIEW (paired)")
                print("-"*96)
                print(f"{'Underlying':<8} {'Expiry':<10} {'R':<1} {'Qty':>3} {'Short':>7} {'Long':>7} "
                      f"{'Width':>6} {'Credit':>8} {'MTM':>12} {'ClosePx':>9} {'MaxLoss':>12}")
                for sp in opt_spreads:
                    mtm_str = fmt_money(float(sp["unreal"])) if sp.get("unreal") is not None else "N/A"
                    close_px_str = f"{float(sp['mark_to_close_per']):>9.2f}" if sp.get("mark_to_close_per") is not None else f"{'N/A':>9}"
                    print(
                        f"{sp['underlying']:<8} {sp['expiry']:<10} {sp['right']:<1} {int(sp['qty']):>3} "
                        f"{sp['short_strike']:>7.1f} {sp['long_strike']:>7.1f} {sp['width']:>6.1f} "
                        f"{float(sp.get('entry_credit_per') or 0.0):>8.2f} "
                        f"{mtm_str:>12} {close_px_str} "
                        f"{human_money(float(sp['max_loss'] or 0.0)):>12}"
                    )
                if opt_unpaired:
                    print("-"*96)
                    print(f"Unpaired option legs: {len(opt_unpaired)} (showing top 5)")
                    for p in opt_unpaired[:5]:
                        strike = p.get("strike")
                        strike_str = f"{float(strike):.1f}" if strike is not None else "?"
                        local = p.get("local_symbol") or ""
                        right = p.get("right") or ""
                        expiry = p.get("expiry") or ""
                        label = (f"{local} {right}{strike_str} {expiry}".strip()
                                 if local else f"{p['symbol']} {expiry} {right}{strike_str}".strip())
                        print(f"  {label:<28} Qty={float(p.get('unpaired_qty') or 0.0):+6.0f}")
                print("-"*96)
            print("OPTIONS")
            print("-"*96)
            for p in opt_sorted:
                spr = f"{float(p['spread']):.4f}" if p.get("spread") is not None else "N/A"
                strike = p.get("strike")
                strike_str = f"{float(strike):.1f}" if strike is not None else "?"
                local = p.get("local_symbol") or ""
                right = p.get("right") or ""
                expiry = p.get("expiry") or ""
                label = (f"{local} {right}{strike_str} {expiry}".strip()
                         if local else f"{p['symbol']} {expiry} {right}{strike_str}".strip())
                realized_today = realized_by_conid.get(int(p["conId"]), 0.0)
                avg_contract = float(p.get("avg_cost_raw") or 0.0)
                print(f"{label:<28} Qty={p['qty']:+6.0f} "
                      f"AvgPx={float(p['avg_cost']):>7.2f} Avg$={avg_contract:>7.2f} "
                      f"MarkPx={fmt_px(p.get('last'))} "
                      f"Bid/Ask={fmt_px(p.get('bid'))}/{fmt_px(p.get('ask'))} "
                      f"Spr={spr:<7} MTM={fmt_money(p.get('unreal')):>11} ({fmt_pct(p.get('pnl_pct'), width=6):>7}) "
                      f"RealizedToday={human_money(realized_today):>10}")

        if other_sorted:
            print("STOCKS / OTHER")
            print("-"*96)
            for p in other_sorted:
                spr = f"{float(p['spread']):.4f}" if p.get("spread") is not None else "N/A"
                label = p['symbol']
                realized_today = realized_by_conid.get(int(p["conId"]), 0.0)
                print(f"{label:<28} Qty={p['qty']:+6.0f} "
                      f"AvgPx={float(p['avg_cost']):>7.2f} MarkPx={fmt_px(p.get('last'))} "
                      f"Bid/Ask={fmt_px(p.get('bid'))}/{fmt_px(p.get('ask'))} "
                      f"Spr={spr:<7} MTM={fmt_money(p.get('unreal')):>11} ({fmt_pct(p.get('pnl_pct'), width=6):>7}) "
                      f"RealizedToday={human_money(realized_today):>10}")
        print("-"*96)
        if pos_snaps and calc_covered == 0:
            print("Total Position Value: N/A (market quotes unavailable)")
            print("Total Unrealized P&L: N/A (market quotes unavailable)")
        else:
            print(f"Total Position Value: {human_money(total_val)}")
            print(f"Total Unrealized P&L: {human_money(total_unreal)}")
    else:
        print("✓ No open positions")

    print("\n" + "="*96)
    print("📈 TODAY EXECUTIONS (with Commissions if available)")
    print("="*96)
    if not fills_all:
        print("✓ No executions today.")
        # 账户概览后直接结束
    else:
        if fills_combo:
            print("\nCOMBO / BAG FILLS (summary, not used for FIFO legs)")
            print(f"   {'Time(ET)':<20} {'Side':<4} {'Qty':>6} {'Price':>10} {'Value':>14} {'Commission':>12}  {'Instrument'}")
            print("   " + "-"*72)
            for f in fills_combo:
                mult = f.multiplier or 1.0
                value = f.price * f.shares * mult * (1 if f.side == 'BOT' else -1)
                emoji = "🟢" if f.side == 'BOT' else "🔴"
                print(f"   {f.time.strftime('%H:%M:%S'):<20} {emoji} {f.side:<4} {f.shares:>6.0f} "
                      f"{f.price:>10.2f} {value:>14,.2f} {human_money(f.commission):>12}  {format_fill_label(f)}")

        # 按 symbol 打印明细
        by_sym: Dict[str, List[FillRow]] = defaultdict(list)
        for f in fills:
            by_sym[f.symbol].append(f)

        for sym, lst in by_sym.items():
            print(f"\n📌 {sym}")
            print(f"   {'Time(ET)':<20} {'Side':<4} {'Qty':>6} {'Price':>10} {'Value':>14} {'Commission':>12}")
            print("   " + "-"*72)
            for f in lst:
                mult = f.multiplier or 1.0
                value = f.price * f.shares * mult * (1 if f.side == 'BOT' else -1)
                emoji = "🟢" if f.side == 'BOT' else "🔴"
                label = format_fill_label(f)
                print(f"   {f.time.strftime('%H:%M:%S'):<20} {emoji} {f.side:<4} {f.shares:>6.0f} "
                      f"{f.price:>10.2f} {value:>14,.2f} {human_money(f.commission):>12}  {label}")

        print("\n" + "-"*96)
        print(f"Fees/Commissions (sum of fills): {human_money(total_commission)}")

        # ---- 生成回合交易 + 计算 MFE/MAE ----
        if rts:
            if fetch_bars:
                fill_mfe_mae_for_trades(ib, rts)
            print("\n" + "="*96)
            print("🧾 ROUND TRIPS (FIFO matched)")
            print("="*96)
            print(f"{'Instrument':<24} {'Side':<5} {'Qty':>6} {'Entry@':>9} {'Exit@':>9} "
                  f"{'P&L(Net)':>12} {'Ret%':>7} {'Hold':>8} {'MFE%':>7} {'MAE%':>7} {'Capture%':>9}  Reason")
            print("-"*96)
            for rt in rts:
                reasons = ", ".join(reason_tags(rt))
                label = rt.symbol
                if rt.sec_type == "OPT":
                    strike = f"{float(rt.strike):.1f}" if rt.strike is not None else "?"
                    label = (f"{rt.local_symbol} {rt.right or ''}{strike} {rt.expiry or ''}".strip()
                             if rt.local_symbol else f"{rt.symbol} {rt.expiry or ''} {rt.right or ''}{strike}".strip())
                mfe_str = f"{rt.mfe_pct:>6.2f}%" if rt.mfe_pct is not None else f"{'N/A':>7}"
                mae_str = f"{rt.mae_pct:>6.2f}%" if rt.mae_pct is not None else f"{'N/A':>7}"
                cap_str = f"{rt.capture_pct:>8.1f}%" if rt.capture_pct is not None else f"{'N/A':>9}"
                print(f"{label:<24} {rt.side:<5} {rt.qty:>6.0f} "
                      f"{rt.entry_price:>9.2f} {rt.exit_price:>9.2f} "
                      f"{human_money(rt.net_pnl):>12} {rt.ret_pct:>6.2f}% "
                      f"{duration_hms(rt.holding_sec):>8} "
                      f"{mfe_str} {mae_str} {cap_str}  {reasons}")

            # 汇总
            print("\n" + "="*96)
            print("📊 DAILY SUMMARY (Closed Trades)")
            print("="*96)
            print(f"Trades: {summ['trades']} | WinRate: {summ['win_rate_pct']:.1f}% "
                  f"| ProfitFactor: {summ['profit_factor']:.2f} | MaxDD: {human_money(summ['max_drawdown'])}")
            print(f"Gross Realized: {human_money(summ['gross_realized'])}  "
                  f"Fees: {human_money(summ['fees_total'])}  "
                  f"Net Realized: {human_money(summ['net_realized'])}")
            print(f"Avg Win: {human_money(summ['avg_win'])} | Avg Loss: {human_money(summ['avg_loss'])} "
                  f"| Median Hold: {duration_hms(summ['median_hold_sec'])}")

        # 未平仓腿（当日层面）
        if open_lots:
            print("\n" + "="*96)
            print("⏳ OPEN LOTS (Unmatched today legs)")
            print("="*96)
            for (con_id, sym), legs in open_lots.items():
                label = sym
                if legs and legs[0].sec_type == "OPT":
                    label = format_fill_label(legs[0])
                print(f"\n{label} (conId={con_id}):")
                for l in legs:
                    emoji = "🟢" if l.side == 'BOT' else "🔴"
                    print(f"  {emoji} {l.side} {l.shares:.0f} @ {l.price:.2f} at {l.time.strftime('%H:%M:%S')} | Fee {human_money(l.commission)}")

    # ---- OPEN ORDERS ----
    print("\n" + "="*96)
    print("📋 OPEN ORDERS")
    print("="*96)
    if open_trades:
        print(
            f"{'ID':>6} {'Instrument':<28} {'Side':<4} {'Qty':>5} {'Type':<7} {'Px':>10} "
            f"{'Status':<13} {'Fill':>9} {'Age':>6} {'Ref':<18} Flags"
        )
        print("-"*96)
        rows = []
        for t in open_trades:
            c = t.contract
            s = t.order
            st = t.orderStatus
            label = format_contract_label(c)
            if getattr(c, "secType", None) == "BAG":
                legs = _order_leg_actions(s.action, c)
                if legs:
                    legs_str = " ".join([f"{side}{conid}" for conid, side in legs])
                    label = f"{label} [{legs_str}]"
            tags = []
            if is_open_status(st.status):
                tags.append("OPEN")
            filled_qty = float(getattr(st, "filled", 0.0) or 0.0)
            remaining_qty = float(getattr(st, "remaining", 0.0) or 0.0)
            if remaining_qty > 0:
                tags.append("UNFILLED")
            if filled_qty > 0 and remaining_qty > 0:
                tags.append("PARTIAL")
            if getattr(t, "fills", None) is not None and len(t.fills) == 0 and is_open_status(st.status):
                tags.append("NO_FILL")
            if st.status in {"PendingCancel", "ApiPending", "PendingSubmit", "PreSubmitted"}:
                tags.append(st.status.upper())
            age = trade_age_sec(t)
            if age is not None and age >= 120 and st.status in pending_states:
                tags.append("STUCK")
            if int(s.orderId) in ghost_ids:
                tags.append("GHOST?")
            legs = analysis["order_to_legs"].get(int(s.orderId), [])
            for conid, side in legs:
                if conid in analysis["duplicates"] and side in analysis["duplicates"][conid]:
                    tags.append("DUPLICATE")
                    break
            for conid, _ in legs:
                if conid in analysis["conflicts"]:
                    tags.append("CONFLICT")
                    break
            order_ref = str(getattr(s, "orderRef", "") or "")
            if order_ref and order_ref in analysis.get("order_ref_dupes", {}):
                tags.append("DUP_REF")
            tag_str = f" [{' '.join(tags)}]" if tags else ""
            px = ""
            if s.orderType == "LMT" and getattr(s, "lmtPrice", None):
                px = f"{float(s.lmtPrice):.2f}"
            elif s.orderType in ("STP", "STP LMT") and getattr(s, "auxPrice", None):
                px = f"stop {float(s.auxPrice):.2f}"
            elif s.orderType == "TRAIL":
                if getattr(s, "trailingPercent", None):
                    px = f"{s.trailingPercent:.2f}%"
                elif getattr(s, "auxPrice", None):
                    px = f"${float(s.auxPrice):.2f}"
            age_str = duration_hms(int(age)) if isinstance(age, int) and age >= 0 else "N/A"
            ref_short = order_ref[:18]
            line = (
                f"{int(s.orderId):>6} {label:<28} {s.action:<4} {float(s.totalQuantity):>5.0f} {s.orderType:<7} {px:>10} "
                f"{st.status:<13} {filled_qty:>4.0f}/{remaining_qty:<4.0f} {age_str:>6} {ref_short:<18}{tag_str}"
            )
            severity = 0
            for tg in tags:
                severity += {
                    "CONFLICT": 100,
                    "GHOST?": 90,
                    "DUPLICATE": 80,
                    "DUP_REF": 70,
                    "STUCK": 60,
                    "PENDINGCANCEL": 50,
                    "PENDINGSUBMIT": 40,
                    "APIPENDING": 40,
                    "PRESUBMITTED": 30,
                    "UNFILLED": 20,
                    "PARTIAL": 10,
                }.get(tg, 0)
            age_rank = int(age) if isinstance(age, int) else -1
            rows.append((severity, age_rank, line))
        for _, _, line in sorted(rows, key=lambda x: (x[0], x[1]), reverse=True):
            print(line)
    else:
        print("✓ No open orders")

    if analysis["duplicates"] or analysis["conflicts"] or analysis.get("order_ref_dupes") or ghost_ids:
        print("\n" + "="*96)
        print("⚠️  ORDER WARNINGS")
        print("="*96)
        if analysis["duplicates"]:
            print("Duplicate orders (same conId+side):")
            for conid, sides in analysis["duplicates"].items():
                for side, ids in sides.items():
                    print(f"  conId={conid} side={side} orders={ids}")
        if analysis["conflicts"]:
            print("Opposite-side open orders (same conId):")
            for conid, sides in analysis["conflicts"].items():
                print(f"  conId={conid} BUY={sides.get('BUY', [])} SELL={sides.get('SELL', [])}")
        if analysis.get("order_ref_dupes"):
            print("Duplicate orderRef (possible idempotency bug):")
            for ref, ids in analysis["order_ref_dupes"].items():
                print(f"  orderRef={ref} orders={ids}")
        if ghost_ids:
            print(f"Ghost candidates (open status but missing from openOrders): {sorted(ghost_ids)}")

    # ---- ALL TRADES 状态分布 ----
    print("\n" + "="*96)
    print("📜 TRADES STATUS SUMMARY")
    print("="*96)
    trades = ib.trades()
    if trades:
        summary = defaultdict(int)
        for t in trades:
            summary[t.orderStatus.status] += 1
        for k, v in sorted(summary.items()):
            icon = {'Filled':'✅','Cancelled':'❌','Submitted':'⏳','PreSubmitted':'⏳','PendingSubmit':'⏳','Inactive':'⚪'}.get(k, '•')
            print(f"  {icon} {k:<15} {v}")
    else:
        print("No trades tracked in session.")

    # ---- 账户汇总 ----
    print("\n" + "="*96)
    print("💼 ACCOUNT SUMMARY")
    print("="*96)
    label_map = {
        'NetLiquidation': 'Net Liquidation',
        'TotalCashValue': 'Total Cash',
        'GrossPositionValue': 'Position Value',
        'UnrealizedPnL': 'Unrealized P&L',
        'RealizedPnL': 'Realized P&L',
        'BuyingPower': 'Buying Power',
        'AvailableFunds': 'Available Funds'
    }
    if acct_usd:
        for tag, label in label_map.items():
            if tag in acct_usd:
                print(f"{label:20s}: {human_money(float(acct_usd[tag]))}")
    else:
        print("⚠️  accountSummary not available")

    try:
        ib.disconnect()
    finally:
        _release_adhoc_ib_connect_guard(connect_guard)
    print("\n" + "="*96)
    print("✅ Daily replay complete.")
    print("="*96)


if __name__ == "__main__":
    main()
