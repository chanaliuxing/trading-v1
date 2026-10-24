"""
Example Strategy: Mean Reversion on 5s bars
Based on doc/59.md template

Simple mean reversion strategy that:
- Monitors z-score on 5-second bars
- Enters when z-score < -2 or > 2
- Sizes positions using ATR
- Uses quality scoring to filter bad data
"""
from services.strategy.strategy_base import (
    StrategyAlgorithm,
    StrategyParams,
    DecisionEnvelope,
    OrderStyle,
    Freshness,
    make_decision_id,
    atr_position_size
)


class MeanRevert5s(StrategyAlgorithm):
    """
    Mean reversion strategy on 5-second bars

    Entry signals:
    - Long when z-score < -2.0 (oversold)
    - Short when z-score > 2.0 (overbought)

    Exit signals:
    - Close when z-score returns to [-0.5, 0.5]

    Position sizing:
    - ATR-based risk sizing
    - Max per-symbol notional limit
    """

    async def initialize(self) -> None:
        """Initialize strategy"""
        symbols = self.params.extras.get('symbols', ['AAPL'])

        # Subscribe to 5-second features
        await self.ctx.subscribe_features(symbols, bar_sec=5)

        # Set cadence
        await self.ctx.set_cadence(self.params.cadence_s)

        self.log.info(
            f"MeanRevert5s initialized",
            symbols=symbols,
            cadence_s=self.params.cadence_s
        )

        # Strategy state
        self.state['last_signal'] = {}
        self.state['signal_count'] = 0

    async def on_features(self, symbol: str, features: dict) -> None:
        """Core strategy logic"""
        # Quality gate
        qscore = features.get('qscore', 0)
        if qscore < self.params.qscore_threshold:
            self.log.debug(f"Skipping {symbol}: low quality score {qscore}")
            return

        # Extract features
        close = features.get('close')
        zscore = features.get('zscore_20')  # 20-bar z-score
        atr = features.get('atr_14')  # 14-bar ATR
        mid = features.get('mid_last') or close

        if not all([close, zscore is not None, atr]):
            self.log.debug(f"Skipping {symbol}: missing features")
            return

        # Get current position
        position = await self.ctx.position(symbol)
        current_qty = position.get('qty', 0.0)

        # Entry signals
        target_qty = 0.0

        if zscore < -2.0 and current_qty <= 0:
            # Oversold → Long
            target_qty = self._calculate_size(mid, atr, 'LONG')
            signal_type = 'LONG_ENTRY'

        elif zscore > 2.0 and current_qty >= 0:
            # Overbought → Short
            target_qty = -self._calculate_size(mid, atr, 'SHORT')
            signal_type = 'SHORT_ENTRY'

        # Exit signals
        elif abs(zscore) < 0.5 and current_qty != 0:
            # Mean reversion → Close
            target_qty = 0.0
            signal_type = 'EXIT'

        else:
            # No signal
            return

        # Create decision envelope
        envelope = DecisionEnvelope(
            decision_id=make_decision_id(),
            strategy_id=self.ctx.strategy_id,
            symbol=symbol,
            ts_bar_end_us=self.ctx.now_us(),
            target_qty=target_qty,
            order_style=OrderStyle(
                kind='LMT',
                limit_px=mid,  # Use mid for limit price
                tif='DAY'
            ),
            freshness=Freshness(
                ttl_ms=self.params.ttl_ms,
                dlb_ms=500
            ),
            extras={
                'ref_px': mid,
                'edge_bp': abs(zscore) * 10,  # Rough edge estimate
                'signal_type': signal_type,
                'zscore': zscore,
                'atr': atr
            }
        )

        # Submit intent
        await self.ctx.submit_intent(envelope)

        # Update state
        self.state['last_signal'][symbol] = signal_type
        self.state['signal_count'] += 1

        self.log.info(
            f"Signal: {signal_type}",
            symbol=symbol,
            zscore=f"{zscore:.2f}",
            target_qty=target_qty,
            current_qty=current_qty
        )

    def _calculate_size(
        self,
        price: float,
        atr: float,
        direction: str
    ) -> float:
        """
        Calculate position size using ATR-based risk

        Args:
            price: Current price
            atr: Average True Range
            direction: 'LONG' or 'SHORT'

        Returns:
            Position size in shares
        """
        # Use ATR-based sizing
        risk_usd = self.params.risk_per_trade_usd
        atr_mult = 2.0  # Stop at 2x ATR

        shares = atr_position_size(risk_usd, atr, atr_mult, price)

        # Apply notional limits
        max_notional = self.params.per_symbol_max_usd
        max_shares = max_notional / price if price > 0 else 0

        shares = min(shares, max_shares)

        # Round to whole shares
        return round(shares)

    async def on_order_update(self, update: dict) -> None:
        """Handle order updates"""
        self.log.info(
            f"Order update",
            order_id=update.get('order_id'),
            state=update.get('state'),
            symbol=update.get('symbol')
        )

    async def on_position_update(self, position: dict) -> None:
        """Handle position updates"""
        self.log.info(
            f"Position update",
            symbol=position.get('symbol'),
            qty=position.get('qty'),
            pnl=position.get('unrealized_pnl_usd', 0.0)
        )
