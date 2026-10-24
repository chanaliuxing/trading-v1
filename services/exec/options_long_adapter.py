"""
services/exec/options_long_adapter.py

Execution adapter for intraday long-only options ML strategy.

Responsibilities:
- Read actionable signals from trading.option_intraday_signals.
- Apply risk controls from trading.option_ml_controls:
    * max_trade_risk_pct (e.g., 0.5% of equity)
    * max_daily_dd_pct  (e.g., 1% daily drawdown cap)
- Convert signals into specific option orders:
    * choose contract (DTE, delta, liquidity filters)
    * compute size based on risk budget
- Track per-trade performance into trading.option_intraday_perf.

NOTE:
- This is a skeleton; you need to connect it to your existing broker/IB
  execution stack and PnL/equity tracking.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from datetime import datetime, date
from typing import List, Optional
import asyncio
import os
import logging

from libs.db.ch_pool import get_client
from libs.logger import get_logger
from services.ibkr.ib_connection_manager import get_ib_connection_manager

logger = get_logger(__name__)

# Dedicated loggers setup
def _get_loggers():
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    formatter = logging.Formatter('%(asctime)s %(levelname)s: %(message)s', datefmt='%H:%M:%S')

    t_log = logging.getLogger("option_ml_trader")
    if not t_log.handlers:
        t_log.setLevel(logging.INFO)
        t_log.propagate = False
        fh_t = logging.FileHandler(os.path.join(log_dir, "option_ml_trader.log"), encoding='utf-8')
        fh_t.setFormatter(formatter)
        t_log.addHandler(fh_t)

    s_log = logging.getLogger("option_ml_system")
    if not s_log.handlers:
        s_log.setLevel(logging.INFO)
        s_log.propagate = False
        fh_s = logging.FileHandler(os.path.join(log_dir, "option_ml_system.log"), encoding='utf-8')
        fh_s.setFormatter(formatter)
        s_log.addHandler(fh_s)

    return t_log, s_log

trader_logger, system_logger = _get_loggers()


@dataclass
class RiskConfig:
    max_trade_risk_pct: float
    max_daily_dd_pct: float
    max_positions_per_symbol: int
    max_total_positions: int
    allow_stale_signals: bool


def _load_controls(strategy_name: str = "intraday_options_long") -> RiskConfig:
    client = get_client()
    sql = """
    SELECT
      max_trade_risk_pct,
      max_daily_dd_pct,
      max_positions_per_symbol,
      max_total_positions,
      extra_config
    FROM trading.option_ml_controls
    WHERE strategy_name = %(strategy_name)s
    ORDER BY updated_at DESC
    LIMIT 1
    """
    rows = client.query(sql, parameters={"strategy_name": strategy_name})
    if not rows:
        raise RuntimeError("option_ml_controls row not found")
    row = rows[0]
    extra_config = row.get("extra_config") or "{}"
    allow_stale_signals = False
    try:
        data = json.loads(extra_config)
        allow_stale_signals = bool(data.get("allow_stale_signals", False))
    except json.JSONDecodeError:
        allow_stale_signals = False

    return RiskConfig(
        max_trade_risk_pct=float(row["max_trade_risk_pct"]),
        max_daily_dd_pct=float(row["max_daily_dd_pct"]),
        max_positions_per_symbol=int(row["max_positions_per_symbol"]),
        max_total_positions=int(row["max_total_positions"]),
        allow_stale_signals=allow_stale_signals,
    )


def _get_today_realized_pnl(strategy_name: str) -> float:
    """
    Aggregate today's realized PnL from option_intraday_perf.
    """
    client = get_client()
    today = date.today().isoformat()
    sql = """
    SELECT coalesce(sum(net_pnl), 0.0) AS pnl
    FROM trading.option_intraday_perf
    WHERE exit_ts IS NOT NULL
      AND toDate(exit_ts) = toDate(%(today)s)
    """
    rows = client.query(sql, parameters={"today": today})
    return float(rows[0]["pnl"]) if rows else 0.0


def _get_equity(default_equity: float = 100_000.0) -> float:
    """
    Best-effort equity lookup from IB account service; fall back to default.
    """
    try:
        from services.portfolio.account_service import get_account_service
    except Exception:
        return default_equity

    async def _fetch() -> Optional[float]:
        svc = get_account_service()
        return await svc.available_funds()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            funds = asyncio.run(_fetch())
        except Exception:
            return default_equity
        if funds is None:
            return default_equity
        return float(funds)

    logger.warning("Event loop running; using default equity fallback")
    return default_equity


def _place_combo_order_sync(
    *,
    underlying: str,
    legs: List[dict],
    qty: int,
    limit_price: float,
    order_ref: str,
    side: str,
) -> bool:
    """
    Place a combo order via IB adapter. Returns True if submitted.
    """
    if os.getenv("OPTION_ML_EXECUTION_ENABLED", "0") != "1":
        logger.info("Execution disabled; skipping order placement", symbol=underlying)
        return False
    raise RuntimeError(
        "legacy_option_combo_direct_ib_disabled:"
        "option execution must route through DecisionPipeline/UOM/ExecutionService"
    )

    async def _place() -> bool:
        ib_cfg = {}
        try:
            from libs.config import config as CFG

            ib_cfg = CFG.ib_config
        except Exception:
            ib_cfg = {}

        host = ib_cfg.get("host", "127.0.0.1")
        port = int(ib_cfg.get("port", 4002))
        account = os.getenv("IB_ACCOUNT", "")
        adapter = await get_ib_connection_manager().get_exec_adapter(
            host=host,
            port=port,
            account=account,
        )
        combo_legs = await adapter.build_combo_legs_from_specs(underlying, legs, order_side=side)
        placed = await adapter.place_combo_order(
            underlying=underlying,
            combo_legs=combo_legs,
            order_type="LMT",
            limit_price=limit_price,
            qty=int(qty),
            side=side,
            tif="DAY",
            order_ref=order_ref,
        )
        return placed is not None

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return bool(asyncio.run(_place()))
        except Exception as exc:
            logger.error("Combo order placement failed", symbol=underlying, error=str(exc))
            return False

    logger.warning("Event loop running; skipping order placement", symbol=underlying)
    return False


def _latest_quotes_ts(underlying: str) -> Optional[datetime]:
    client = get_client()
    rows = client.query(
        """
        SELECT max(ts) AS ts
        FROM trading.options_quotes_1m
        WHERE underlying = %(underlying)s
        """,
        parameters={"underlying": underlying},
    )
    return rows[0]["ts"] if rows and rows[0]["ts"] else None


def _fetch_quotes_at_ts(
    underlying: str,
    ts: datetime,
    right: str,
    dte_candidates: List[int],
) -> List[dict]:
    client = get_client()
    dte_filter = ""
    if dte_candidates:
        dte_filter = f"AND dte IN ({','.join(str(d) for d in dte_candidates)})"
    sql = f"""
    SELECT
      expiry,
      strike,
      right,
      dte,
      bid,
      ask,
      mid,
      delta
    FROM trading.options_quotes_1m
    WHERE underlying = %(underlying)s
      AND ts = %(ts)s
      AND right = %(right)s
      {dte_filter}
      AND bid > 0 AND ask > 0 AND mid > 0
    """
    return client.query(sql, parameters={"underlying": underlying, "ts": ts, "right": right})


def _select_by_delta(
    quotes: List[dict],
    delta_target: float,
) -> Optional[dict]:
    if not quotes:
        return None
    best = None
    best_diff = 1e9
    for row in quotes:
        diff = abs(abs(float(row["delta"])) - delta_target)
        if diff < best_diff:
            best = row
            best_diff = diff
    return best


def _select_by_strike(
    quotes: List[dict],
    expiry: Optional[date],
    target_strike: float,
    dte_target: Optional[int] = None,
) -> Optional[dict]:
    if not quotes:
        return None
    best = None
    best_diff = 1e9
    for row in quotes:
        if expiry is not None and row["expiry"] != expiry:
            continue
        if dte_target is not None and int(row["dte"]) != dte_target:
            continue
        diff = abs(float(row["strike"]) - target_strike)
        if diff < best_diff:
            best = row
            best_diff = diff
    return best


def _estimate_max_loss_per_contract(legs: List[dict], net_debit: float) -> float:
    if net_debit >= 0:
        return float(net_debit)
    credit = abs(float(net_debit))
    widths = []
    for right in ("C", "P"):
        buys = [l for l in legs if l["right"] == right and l["side"] == "BUY"]
        sells = [l for l in legs if l["right"] == right and l["side"] == "SELL"]
        if not buys or not sells:
            continue
        strikes_buy = [float(l["strike"]) for l in buys]
        strikes_sell = [float(l["strike"]) for l in sells]
        width = max(abs(b - s) for b in strikes_buy for s in strikes_sell)
        widths.append(width)
    if widths:
        max_loss = max(widths) - credit
        if max_loss > 0:
            return float(max_loss)
    return float(abs(net_debit))


def _resolve_legs_from_spec(underlying: str, legs_spec_json: str) -> Optional[dict]:
    if not legs_spec_json or legs_spec_json == "{}":
        return None
    try:
        spec = json.loads(legs_spec_json)
    except json.JSONDecodeError:
        return None

    legs_template = spec.get("legs_template", [])
    params = spec.get("params", {})
    if not legs_template:
        return None

    ts = _latest_quotes_ts(underlying)
    if not ts:
        return None

    dte_candidates = params.get("dte_candidates", [])
    delta_target = float(params.get("delta_target", 0.35))

    resolved = []
    anchors: dict[str, dict] = {}

    # First pass: resolve anchor legs
    for leg in legs_template:
        if not leg.get("anchor"):
            continue
        right = leg.get("right")
        side = leg.get("side")
        anchor_id = leg.get("anchor_id") or f"{right}_{side}"
        leg_delta = float(leg.get("delta_target", delta_target))
        dte_target = leg.get("dte_target")
        dte_list = [int(dte_target)] if dte_target is not None else dte_candidates
        quotes = _fetch_quotes_at_ts(underlying, ts, right, dte_list)
        anchor_leg = _select_by_delta(quotes, leg_delta)
        if not anchor_leg:
            return None
        anchor = {
            "underlying": underlying,
            "expiry": anchor_leg["expiry"],
            "strike": float(anchor_leg["strike"]),
            "right": right,
            "side": side,
            "qty": int(leg.get("qty", 1)),
            "dte": int(anchor_leg["dte"]),
            "bid": float(anchor_leg["bid"]),
            "ask": float(anchor_leg["ask"]),
        }
        anchors[anchor_id] = anchor
        resolved.append(anchor)

    if not anchors:
        return None

    # Second pass: resolve non-anchor legs
    for leg in legs_template:
        if leg.get("anchor"):
            continue
        right = leg.get("right")
        side = leg.get("side")
        anchor_ref = leg.get("anchor_ref") or next(iter(anchors.keys()))
        anchor = anchors.get(anchor_ref)
        if not anchor:
            return None

        target_strike = float(anchor["strike"])
        if leg.get("strike_offset") is not None:
            target_strike = float(anchor["strike"]) + float(leg["strike_offset"])

        dte_target = None
        if leg.get("dte_target") is not None:
            dte_target = int(leg["dte_target"])
        elif leg.get("dte_offset") is not None:
            dte_target = int(anchor["dte"]) + int(leg["dte_offset"])

        dte_candidates_leg = [dte_target] if dte_target is not None else dte_candidates
        quotes = _fetch_quotes_at_ts(underlying, ts, right, dte_candidates_leg)
        target_expiry = anchor["expiry"] if dte_target is None else None
        leg_row = _select_by_strike(quotes, target_expiry, target_strike, dte_target)
        if not leg_row:
            return None
        resolved.append(
            {
                "underlying": underlying,
                "expiry": leg_row["expiry"],
                "strike": float(leg_row["strike"]),
                "right": right,
                "side": side,
                "qty": int(leg.get("qty", 1)),
                "dte": int(leg_row["dte"]),
                "bid": float(leg_row["bid"]),
                "ask": float(leg_row["ask"]),
            }
        )

    if not resolved:
        return None

    net_debit = 0.0
    for leg in resolved:
        if leg["side"] == "BUY":
            net_debit += leg["ask"] * leg["qty"]
        else:
            net_debit -= leg["bid"] * leg["qty"]

    return {
        "ts": ts,
        "legs": resolved,
        "net_debit": float(net_debit),
    }


def _compute_size(
    equity: float,
    max_trade_risk_pct: float,
    max_loss_per_contract: float,
    contracts_multiplier: int = 100,
) -> int:
    budget = max_trade_risk_pct * equity
    if max_loss_per_contract <= 0:
        return 0
    size = int(budget // (max_loss_per_contract * contracts_multiplier))
    return max(size, 0)


def _insert_trade_perf_stub(
    symbol: str,
    underlying: str,
    side: str,
    contract: dict,
    contracts: int,
    entry_price: float,
    structure_id: str,
    structure_type: str,
    legs_json: str,
    net_debit_entry: float,
    max_loss_defined: float,
    model_version: str,
    p_up: float,
    p_down: float,
    p_move: float,
    horizon_min: int,
    risk: RiskConfig,
) -> None:
    """
    Insert a placeholder row into option_intraday_perf at entry time.
    Exit fields can be updated later by a closing routine.
    """
    client = get_client()
    trade_id = f"{underlying}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

    expiry = contract.get("expiry", date.today())
    strike = float(contract.get("strike", 0.0))
    right = contract.get("right", "C")

    data = [
        (
            trade_id,
            symbol,
            underlying,
            side,
            expiry,
            strike,
            right,
            int(contract.get("dte", 0)),
            contracts,
            float(contract.get("mid_price", entry_price)),
            datetime.now(),
            datetime.now(),  # placeholder exit_ts
            entry_price,
            entry_price,     # placeholder exit_price
            0.0,
            0.0,
            0.0,
            model_version,
            p_up,
            p_down,
            p_move,
            horizon_min,
            risk.max_trade_risk_pct,
            risk.max_daily_dd_pct,
            "OPEN",
            "{}",
            structure_id,
            structure_type,
            legs_json,
            net_debit_entry,
            0.0,
            max_loss_defined,
            0.0,
            0.0,
            datetime.now(),
        )
    ]

    client.insert(
        "trading.option_intraday_perf",
        data,
        column_names=[
            "trade_id",
            "symbol",
            "underlying",
            "side",
            "expiry",
            "strike",
            "right",
            "dte",
            "contracts",
            "notional_per_contract",
            "entry_ts",
            "exit_ts",
            "entry_price",
            "exit_price",
            "gross_pnl",
            "net_pnl",
            "max_drawdown_pct",
            "model_version",
            "p_up_entry",
            "p_down_entry",
            "p_move_entry",
            "horizon_min",
            "max_trade_risk_pct",
            "max_daily_dd_pct",
            "exit_reason",
            "extra_config",
            "structure_id",
            "structure_type",
            "legs_json",
            "net_debit_entry",
            "net_debit_exit",
            "max_loss_defined",
            "fees",
            "slippage_est",
            "created_at",
        ],
    )


def _process_signal_rows(rows: List[dict], risk_cfg: RiskConfig, equity: float) -> None:
    for row in rows:
        action = row["action"]
        if action == "NO_TRADE":
            continue

        underlying = row["symbol"]
        trader_logger.info(f"Processing Signal: {underlying} {action}")
        
        resolved = _resolve_legs_from_spec(underlying, row.get("legs_spec_json", ""))
        if not resolved:
            msg = f"No legs resolved for {underlying}"
            logger.warning(msg)
            system_logger.warning(msg)
            trader_logger.warning(f"  FAILED: Could not structure order legs for {underlying}")
            continue

        net_debit = float(resolved["net_debit"])
        max_loss_per_contract = _estimate_max_loss_per_contract(resolved["legs"], net_debit)
        if max_loss_per_contract <= 0:
            logger.info("Max loss <= 0; skipping trade", symbol=underlying)
            system_logger.warning(f"{underlying}: Calculated max loss is <= 0. Skipping.")
            continue

        size = _compute_size(equity, risk_cfg.max_trade_risk_pct, max_loss_per_contract, contracts_multiplier=100)
        if size <= 0:
            logger.info("Size computed as 0; skipping trade", symbol=underlying)
            trader_logger.info(f"  SKIPPED: Position size 0 (Risk limit too tight or equity too low)")
            continue

        max_loss = max_loss_per_contract * 100.0 * size
        raw_legs = resolved["legs"]
        legs = []
        
        leg_desc = []
        for leg in raw_legs:
            desc = f"{leg['side']} {leg['qty']}x {leg['strike']}{leg['right']} (Exp: {leg['expiry']})"
            leg_desc.append(desc)
            legs.append(
                {
                    "underlying": leg["underlying"],
                    "expiry": leg["expiry"].isoformat() if hasattr(leg["expiry"], "isoformat") else str(leg["expiry"]),
                    "strike": float(leg["strike"]),
                    "right": leg["right"],
                    "side": leg["side"],
                    "qty": int(leg["qty"]),
                    "dte": int(leg["dte"]),
                    "bid": float(leg["bid"]),
                    "ask": float(leg["ask"]),
                }
            )
        
        trader_logger.info(f"  Structure: {', '.join(leg_desc)}")
        trader_logger.info(f"  Risk: ${max_loss:.2f} ({risk_cfg.max_trade_risk_pct*100:.2f}% of Equity). Size: {size} contracts.")

        primary_raw = raw_legs[0]
        contract = {
            "expiry": primary_raw["expiry"],
            "strike": primary_raw["strike"],
            "right": primary_raw["right"],
            "dte": primary_raw["dte"],
            "mid_price": net_debit,
        }

        ts = row.get("ts") or datetime.now()
        order_ref = f"option_ml_{underlying}_{ts.strftime('%Y%m%d_%H%M%S_%f')}"
        limit_price = abs(net_debit)
        if limit_price <= 0:
            logger.info("Limit price <= 0; skipping trade", symbol=underlying)
            continue
        order_side = "BUY" if net_debit >= 0 else "SELL"
        
        trader_logger.info(f"  >> EXECUTING: {order_side} {underlying} Combo @ ${limit_price:.2f}")
        
        placed = _place_combo_order_sync(
            underlying=underlying,
            legs=legs,
            qty=size,
            limit_price=limit_price,
            order_ref=order_ref,
            side=order_side,
        )
        if not placed:
            logger.info("Order not placed; skipping perf insert", symbol=underlying)
            trader_logger.error(f"  EXECUTION FAILED: IBKR did not accept order for {underlying}")
            system_logger.error(f"Order placement failed for {underlying}. Check IB connection.")
            continue
        
        trader_logger.info(f"  SUCCESS: Order submitted. Ref: {order_ref}\n")

        side_value = "CALL" if str(primary_raw.get("right", "C")) == "C" else "PUT"
        _insert_trade_perf_stub(
            symbol=f"{underlying}_OPT",
            underlying=underlying,
            side=side_value,
            contract=contract,
            contracts=size,
            entry_price=net_debit,
            structure_id=row.get("structure_id", ""),
            structure_type=row.get("structure_type", ""),
            legs_json=json.dumps(legs),
            net_debit_entry=net_debit,
            max_loss_defined=max_loss,
            model_version=row["model_version"],
            p_up=row["p_up"],
            p_down=row["p_down"],
            p_move=row["p_move"],
            horizon_min=row["horizon_min"],
            risk=risk_cfg,
        )


def process_signals_rows(rows: List[dict], strategy_name: str = "intraday_options_long") -> None:
    """
    Process a pre-filtered set of signal rows (e.g., latest per symbol).
    """
    risk_cfg = _load_controls(strategy_name)
    equity = _get_equity()

    today_pnl = _get_today_realized_pnl(strategy_name)
    if today_pnl / equity <= -risk_cfg.max_daily_dd_pct:
        logger.info("Daily drawdown limit reached; skipping new trades", pnl=today_pnl, equity=equity)
        return

    if not rows:
        logger.info("No option ML signals to process")
        return

    _process_signal_rows(rows, risk_cfg, equity)


def process_signals_once(strategy_name: str = "intraday_options_long") -> None:
    """
    Single-pass processing:
    - read latest signals,
    - apply risk controls,
    - derive orders and record entries.
    """
    risk_cfg = _load_controls(strategy_name)
    client = get_client()

    equity = _get_equity()

    today_pnl = _get_today_realized_pnl(strategy_name)
    if today_pnl / equity <= -risk_cfg.max_daily_dd_pct:
        logger.info("Daily drawdown limit reached; skipping new trades", pnl=today_pnl, equity=equity)
        return

    if risk_cfg.allow_stale_signals:
        sql = """
        SELECT *
        FROM trading.option_intraday_signals
        WHERE ts >= now('America/New_York') - INTERVAL 7 DAY
        ORDER BY ts DESC
        LIMIT 1 BY symbol
        """
    else:
        sql = """
        SELECT *
        FROM trading.option_intraday_signals
        WHERE ts >= now('America/New_York') - INTERVAL 2 MINUTE
        ORDER BY ts DESC
        """
    rows = client.query(sql)
    if not rows:
        logger.info("No option ML signals to process")
        return

    _process_signal_rows(rows, risk_cfg, equity)


def main() -> None:
    # In production, wire this into your scheduling/execution loop
    process_signals_once()


if __name__ == "__main__":
    main()
