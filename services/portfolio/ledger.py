"""
PnL Ledger
Maintains position, cost, and PnL accounting using WAC (Weighted Average Cost) method
Based on doc/75.md, doc/63.md, doc/70.md specifications

Supports:
- Long and short positions
- Exactly-once fill processing with idempotency
- Cost averaging for additions
- Realized PnL calculation for reductions
- Position reversals (crossing zero)
"""
import asyncio
from dataclasses import dataclass
from typing import Dict, Tuple, Optional
from collections import defaultdict
import hashlib
import time

from libs.db.ch_pool import threadsafe_insert
from libs.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Lot:
    """
    Position lot state

    pos: Net position quantity (positive=long, negative=short)
    avg: Average cost (signed, negative for short positions)
    cash: Cash balance
    """
    pos: int = 0
    avg: float = 0.0
    cash: float = 0.0


@dataclass
class FillResult:
    """Fill processing result"""
    pos_after: int
    avg_after: float
    realized_pnl: float
    cash_after: float
    fill_uid: str
    ver: int


class PnLLedger:
    """
    Position and PnL accounting ledger

    Uses WAC (Weighted Average Cost) method for cost basis.
    Maintains in-memory state per (strategy_id, symbol).
    """

    def __init__(self, ch_client: any, pg_client: any = None):
        """
        Initialize ledger

        Args:
            ch_client: ClickHouse client for writing results
            pg_client: Optional PostgreSQL client for checkpoints
        """
        self.ch = ch_client
        self.pg = pg_client
        self.state: Dict[Tuple[str, str], Lot] = defaultdict(Lot)
        self._processed_fills: set = set()  # Fill UID deduplication

        logger.info("PnLLedger initialized")

    def generate_fill_uid(self, fill: Dict) -> str:
        """
        Generate idempotency key for fill

        Priority:
        1. ib_exec_id (if available)
        2. SHA1 hash of fill attributes

        Args:
            fill: Fill event dict

        Returns:
            20-character fill UID
        """
        # Use broker execution ID if available
        if fill.get("ib_exec_id"):
            return str(fill["ib_exec_id"])[:20]

        # Generate deterministic hash
        base = (
            f"{fill.get('account_code', 'default')}|"
            f"{fill['order_id']}|"
            f"{int(fill['ts'].timestamp() * 1000)}|"
            f"{fill['side']}|"
            f"{abs(int(fill['qty']))}|"
            f"{round(float(fill['price']), 5)}"
        )

        uid = hashlib.sha1(base.encode()).hexdigest()[:20]
        return uid

    def step_fill(
        self,
        state: Lot,
        side: int,
        qty: int,
        px: float,
        fee: float
    ) -> Tuple[int, float, float, float]:
        """
        Process fill and update position/cost/PnL

        Args:
            state: Current lot state (modified in-place)
            side: +1 for BUY, -1 for SELL
            qty: Fill quantity (absolute value)
            px: Fill price
            fee: Commission/fee

        Returns:
            (pos_after, avg_after, realized_pnl, cash_after)
        """
        pos0, avg0, cash0 = state.pos, state.avg, state.cash
        qty = abs(qty) * side

        realized = 0.0

        # Case A: Same direction (add to position or open from zero)
        if pos0 == 0 or (pos0 > 0 and qty > 0) or (pos0 < 0 and qty < 0):
            # WAC: (old_notional + new_notional) / new_qty
            notional_old = abs(pos0) * abs(avg0)
            notional_new = abs(qty) * abs(px)
            pos1 = pos0 + qty

            if pos1 != 0:
                avg1 = (notional_old + notional_new) / abs(pos1)
                avg1 = avg1 * (1 if pos1 > 0 else -1)  # Apply sign
            else:
                avg1 = 0.0
        else:
            # Case B: Opposite direction (reduce or reverse)
            close_qty = min(abs(pos0), abs(qty))
            close_qty_signed = close_qty * (1 if pos0 > 0 else -1)

            # Calculate realized PnL on closed portion
            if pos0 > 0:  # Closing long
                realized += (px - abs(avg0)) * abs(close_qty)
            else:  # Closing short
                realized += (abs(avg0) - px) * abs(close_qty)

            pos1 = pos0 + qty

            if pos1 == 0:
                # Fully closed
                avg1 = 0.0
            elif (pos0 > 0 and pos1 > 0) or (pos0 < 0 and pos1 < 0):
                # Partial close, same direction remains
                avg1 = avg0
            else:
                # Reversal: new position from remaining fill quantity
                residual_qty = abs(pos1)
                avg1 = px * (1 if pos1 > 0 else -1)

        # Update cash: buy reduces cash, sell increases cash
        cash1 = cash0 - side * abs(qty) * px - fee
        realized -= fee  # Fee reduces realized PnL

        # Update state in-place
        state.pos = pos1
        state.avg = avg1
        state.cash = cash1

        return pos1, avg1, realized, cash1

    async def process_fill(self, fill: Dict) -> Optional[FillResult]:
        """
        Process fill event with idempotency

        Args:
            fill: Fill event with fields:
                - strategy_id, symbol, side, qty, price, fee
                - ts, order_id, decision_id, param_hash
                - Optional: ib_exec_id, account_code

        Returns:
            FillResult or None if duplicate
        """
        # Generate fill UID
        fill_uid = self.generate_fill_uid(fill)

        # Check for duplicate
        if fill_uid in self._processed_fills:
            logger.debug("Duplicate fill skipped", fill_uid=fill_uid)
            return None

        # Mark as processed
        self._processed_fills.add(fill_uid)

        # Get state
        key = (fill["strategy_id"], fill["symbol"])
        lot = self.state[key]

        # Parse side
        side_str = fill["side"]
        if isinstance(side_str, str):
            side = +1 if side_str.upper() in ("BUY", "BOT") else -1
        else:
            side = +1 if side_str > 0 else -1

        # Process fill
        pos1, avg1, rpl, cash1 = self.step_fill(
            lot,
            side=side,
            qty=int(fill["qty"]),
            px=float(fill["price"]),
            fee=float(fill.get("fee", 0.0))
        )

        # Generate version
        ver = int(time.time() * 1000)

        logger.debug(
            "Fill processed",
            strategy_id=fill["strategy_id"],
            symbol=fill["symbol"],
            side=side_str,
            qty=fill["qty"],
            price=fill["price"],
            pos_after=pos1,
            avg_after=round(avg1, 4),
            realized=round(rpl, 2)
        )

        return FillResult(
            pos_after=pos1,
            avg_after=avg1,
            realized_pnl=rpl,
            cash_after=cash1,
            fill_uid=fill_uid,
            ver=ver
        )

    async def write_pnl_fill(self, fill: Dict, result: FillResult) -> None:
        """
        Write fill result to pnl_fills table

        Args:
            fill: Original fill event
            result: Processing result
        """
        side = +1 if fill["side"].upper() in ("BUY", "BOT") else -1

        row = {
            "ts": fill["ts"],
            "strategy_id": fill["strategy_id"],
            "symbol": fill["symbol"],
            "side": side,
            "qty": int(fill["qty"]),
            "price": float(fill["price"]),
            "fee": float(fill.get("fee", 0.0)),
            "pos_qty_after": result.pos_after,
            "avg_cost_after": result.avg_after,
            "realized_pnl": result.realized_pnl,
            "cash_after": result.cash_after,
            "order_id": fill["order_id"],
            "decision_id": fill.get("decision_id", ""),
            "param_hash": fill.get("param_hash", ""),
            "fill_uid": result.fill_uid,
            "ver": result.ver
        }

        # Insert to ClickHouse
        columns = [
            "ts", "strategy_id", "symbol", "side", "qty", "price", "fee",
            "pos_qty_after", "avg_cost_after", "realized_pnl", "cash_after",
            "order_id", "decision_id", "param_hash", "fill_uid", "ver"
        ]

        values = [[row[c] for c in columns]]

        await asyncio.to_thread(
            threadsafe_insert,
            self.ch,
            "pnl_fills",
            values,
            columns,
        )

        logger.debug("PnL fill written", fill_uid=result.fill_uid)

    async def snapshot_positions(self, ts_end: any) -> None:
        """
        Write position snapshots to pnl_pos_5s

        Args:
            ts_end: 5-second window end timestamp
        """
        rows = []

        for (strategy_id, symbol), lot in self.state.items():
            if lot.pos == 0:
                continue

            # Get mark price (placeholder - should fetch from bars_5s)
            mark_px = abs(lot.avg) * 1.001  # Temporary stub

            # Calculate UPL
            upl = lot.pos * (mark_px - lot.avg) if lot.pos > 0 else lot.pos * (lot.avg - mark_px)
            mtm = lot.pos * mark_px

            row = {
                "ts_end": ts_end,
                "strategy_id": strategy_id,
                "symbol": symbol,
                "pos_qty": lot.pos,
                "avg_cost": lot.avg,
                "mark_px": mark_px,
                "mtm": mtm,
                "upl": upl,
                "ver": int(time.time() * 1000)
            }

            rows.append(row)

        if not rows:
            return

        columns = [
            "ts_end", "strategy_id", "symbol", "pos_qty",
            "avg_cost", "mark_px", "mtm", "upl", "ver"
        ]

        values = [[r[c] for c in columns] for r in rows]

        await asyncio.to_thread(
            threadsafe_insert,
            self.ch,
            "pnl_pos_5s",
            values,
            columns,
        )

        logger.debug("Position snapshots written", count=len(rows))

    def get_position(self, strategy_id: str, symbol: str) -> Lot:
        """
        Get current position state

        Args:
            strategy_id: Strategy ID
            symbol: Symbol

        Returns:
            Lot state
        """
        return self.state.get((strategy_id, symbol), Lot())

    async def save_checkpoint(self) -> None:
        """Save checkpoint for recovery (optional)"""
        if not self.pg:
            return

        # Save state to pnl_ckpt table
        for (strategy_id, symbol), lot in self.state.items():
            sql = """
                INSERT INTO pnl_ckpt (strategy_id, symbol, ts_ckpt, pos, avg, cash, ver)
                VALUES (%s, %s, now(), %s, %s, %s, %s)
                ON CONFLICT (strategy_id, symbol, ts_ckpt) DO UPDATE
                SET pos = EXCLUDED.pos, avg = EXCLUDED.avg, cash = EXCLUDED.cash, ver = EXCLUDED.ver
            """

            with self.pg.get_cursor() as cur:
                cur.execute(sql, (
                    strategy_id, symbol,
                    lot.pos, lot.avg, lot.cash,
                    int(time.time() * 1000)
                ))

        logger.info("Checkpoint saved", state_count=len(self.state))
