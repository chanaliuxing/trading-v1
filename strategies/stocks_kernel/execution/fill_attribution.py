from __future__ import annotations

from strategies.stocks_kernel.types import FillAttributionResult, KernelFillReport

from .lot_ledger import PositionLotLedger
from .order_identity import parse_order_ref


class FillAttributionEngine:
    """
    Resolve broker fills to a single position lot.

    Resolution priority is strict:
    1. perm_id
    2. broker_order_id
    3. order_ref -> lot_id
    4. order_ref -> strategy_id + symbol, but only if exactly one open lot matches

    Any ambiguous match returns quarantine_required=True and must not mutate state.
    """

    def __init__(self, ledger: PositionLotLedger) -> None:
        self._ledger = ledger

    def attribute_fill(self, fill: KernelFillReport) -> FillAttributionResult:
        symbol = str(fill.symbol or "").strip().upper()
        if not symbol:
            return FillAttributionResult(
                position_lot_id=None,
                strategy_id=None,
                reason="fill_missing_symbol",
                quarantine_required=True,
            )

        if fill.perm_id is not None:
            matched = self._ledger.find_lots(symbol=symbol, perm_id=fill.perm_id)
            exact = self._single_match(matched)
            if exact is not None:
                return exact
            if len(matched) > 1:
                return self._ambiguous("fill_perm_id_ambiguous")

        broker_order_id = str(fill.broker_order_id or "").strip() or None
        if broker_order_id:
            matched = self._ledger.find_lots(symbol=symbol, broker_order_id=broker_order_id)
            exact = self._single_match(matched)
            if exact is not None:
                return exact
            if len(matched) > 1:
                return self._ambiguous("fill_broker_order_id_ambiguous")

        parsed = parse_order_ref(fill.order_ref)
        if parsed is not None:
            if parsed.lot_id:
                lot = self._ledger.get_lot(parsed.lot_id)
                if lot is None:
                    return self._ambiguous("fill_order_ref_unknown_lot")
                if lot.symbol != symbol:
                    return self._ambiguous("fill_order_ref_symbol_mismatch")
                if lot.strategy_id != parsed.strategy_id:
                    return self._ambiguous("fill_order_ref_strategy_mismatch")
                return FillAttributionResult(
                    position_lot_id=lot.position_lot_id,
                    strategy_id=lot.strategy_id,
                    reason="matched_by_order_ref_lot_id",
                )
            matched = self._ledger.find_lots(strategy_id=parsed.strategy_id, symbol=symbol)
            exact = self._single_match(matched)
            if exact is not None:
                return FillAttributionResult(
                    position_lot_id=exact.position_lot_id,
                    strategy_id=exact.strategy_id,
                    reason="matched_by_order_ref_strategy_symbol",
                )
            if len(matched) > 1:
                return self._ambiguous("fill_order_ref_strategy_symbol_ambiguous")

        return self._ambiguous("fill_unattributed")

    def _single_match(self, lots):
        if len(lots) != 1:
            return None
        lot = lots[0]
        return FillAttributionResult(
            position_lot_id=lot.position_lot_id,
            strategy_id=lot.strategy_id,
            reason="matched_by_broker_identity",
        )

    def _ambiguous(self, reason: str) -> FillAttributionResult:
        return FillAttributionResult(
            position_lot_id=None,
            strategy_id=None,
            reason=reason,
            quarantine_required=True,
        )
