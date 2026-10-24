# Ledger Integration - Remaining Tasks

## Status
- ✅ Created `ny_time.py` - unified NY timezone utilities
- ✅ Created `ledger_manager.py` - session-based ledger system
- ✅ Modified `run_single_bot.py` - added CLI args and ledger initialization
- ✅ Modified `llm_trader_bot.py.__init__` - accept ledger and session_id
- ⏳ Need to add ledger hooks at key points in `llm_trader_bot.py`

## Ledger Hooks Needed in `llm_trader_bot.py`

### 1. Log LLM Decision (after getting decision from LLM)

**Location**: In `run_trading_cycle()` after LLM returns decision

**Code to add**:
```python
# V2.3: Log decision to ledger
if hasattr(self, 'ledger') and self.session_id:
    self.ledger.log_decision(
        session_id=self.session_id,
        symbol=symbol,
        decision_json={
            "action": action,
            "confidence": confidence,
            "shares": shares,
            "rationale": decision.get("rationale", ""),
        },
        prompt_digest=f"v{self.prompt_version}",
        raw_text=str(decision)[:4000],
    )
```

**Approximate line numbers**: Around 720-860 (after calling LLM, before execute_trade)

### 2. Log Order Creation (when creating order)

**Location**: In `execute_trade()` when creating BUY or SELL order

**Code to add**:
```python
# V2.3: Log order creation
if hasattr(self, 'ledger') and self.session_id:
    import time
    order_id_internal = f"{symbol}-{int(time.time()*1000)}"

    self.ledger.log_order_event(
        session_id=self.session_id,
        order_id_internal=order_id_internal,
        stage="created",
        symbol=symbol,
        side=action,  # "BUY" or "SELL"
        qty=quantity,
        order_type="MKT",
        limit_price=None,
        tif="DAY",
        meta={
            "confidence": decision.confidence if hasattr(decision, 'confidence') else 0.0,
            "dry_run": self.dry_run,
        },
    )
```

**Approximate line numbers**: Around 392-433 (in execute_trade, before placing order)

### 3. Log Fill Event (when order fills)

**Location**: In `execute_trade()` after portfolio.open_position() or portfolio.close_position()

**Code to add**:
```python
# V2.3: Log fill
if hasattr(self, 'ledger') and self.session_id:
    from llm_trader.utils.commission import calculate_commission
    fee = calculate_commission(quantity * current_price)

    self.ledger.log_fill(
        session_id=self.session_id,
        order_id_internal=order_id_internal,
        fill_qty=quantity,
        fill_price=current_price,
        symbol=symbol,
        fee=fee,
        source="IBKR" if not self.dry_run else "SIM",
    )

    # Also log order filled stage
    self.ledger.log_order_event(
        session_id=self.session_id,
        order_id_internal=order_id_internal,
        stage="filled",
        symbol=symbol,
        side=action,
        qty=quantity,
        order_type="MKT",
        limit_price=None,
        tif="DAY",
        meta={"avg_fill_price": current_price},
    )
```

**Approximate line numbers**: Around 403-448 and 489-514 (after portfolio operations succeed)

### 4. Snapshot Position & Equity (every cycle)

**Location**: At the end of `run_trading_cycle()`, before cycle complete log

**Code to add**:
```python
# V2.3: Take snapshots
if hasattr(self, 'ledger') and self.session_id:
    # Position snapshot
    positions = []
    for sym, pos in self.portfolio.positions.items():
        positions.append({
            "symbol": sym,
            "position": pos.quantity,  # Can be negative for short
            "avg_cost": pos.avg_cost,
            "market_price": pos.current_price,
            "unrealized_pnl": pos.unrealized_pnl,
            "realized_pnl_to_date": 0.0,  # Track separately if needed
        })
    self.ledger.snapshot_position(session_id=self.session_id, positions=positions)

    # Equity snapshot
    portfolio_state = self.portfolio.get_position_state()
    self.ledger.snapshot_equity(
        session_id=self.session_id,
        equity=portfolio_state['total_equity'],
        cash=portfolio_state['cash'],
        gross_exposure=abs(self.portfolio.total_market_value),
        net_exposure=self.portfolio.total_market_value,
        drawdown_to_date=0.0,  # Calculate if needed
    )
```

**Approximate line numbers**: Around 940-960 (end of cycle, before "Cycle Complete" log)

### 5. Close Session (when bot stops)

**Location**: In `stop()` method or in the finally block of `run()`

**Code to add**:
```python
# V2.3: Close session
if hasattr(self, 'ledger') and self.session_id:
    self.ledger.close_session(
        session_id=self.session_id,
        status="CLOSED",
        reason="Normal shutdown",
    )
```

**Approximate line numbers**: Around 350 (in stop method) or 1280 (in run method's finally)

## Implementation Strategy

Due to code length, manually add these hooks:

1. Search for the approximate line numbers
2. Add the code snippets at appropriate locations
3. Test with `--enable-ledger` flag

## Testing

```bash
# Test with ledger enabled
python llm_trader/run_single_bot.py \
    --provider deepseek_nvidia \
    --symbols TSLA \
    --interval 60 \
    --cycles 2 \
    --dry-run \
    --enable-ledger \
    --session-name "TEST_STRATEGY_A"

# Verify in ClickHouse
clickhouse-client --query "SELECT * FROM llm.llm_sessions ORDER BY start_ts_ny DESC LIMIT 1"
clickhouse-client --query "SELECT * FROM llm.llm_decisions ORDER BY ts_ny DESC LIMIT 5"
```

## Priority Order

1. ✅ **DONE**: Session creation (in run_single_bot.py)
2. **TODO**: Log decisions (every cycle)
3. **TODO**: Snapshot equity (every cycle)
4. **TODO**: Log orders & fills (when trading)
5. **TODO**: Close session (when stopping)

This way even partial implementation will provide value (session tracking + decision logging).
