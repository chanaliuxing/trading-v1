# Trading System - Quick Start Reference

## 🚀 Fastest Path to Running System

### 1. Verify Infrastructure (30 seconds)
```bash
source venv/bin/activate
python3 scripts/smoke_test.py
```
Expected: 5/5 passed ✅

### 2. Configure Trading Mode (1 minute)
```bash
# Edit .env
TRADING_MODE=shadow
SHADOW_SYMBOLS=AAPL,MSFT,NVDA,GOOGL,TSLA
```

### 3. Initialize Database (30 seconds)
```bash
export PGPASSWORD=postgres
psql -h localhost -U postgres -d test << 'SQL'
-- Set shadow mode
INSERT INTO deployment_stages (stage, started_at, active, capital_pct, symbols)
VALUES ('shadow', NOW(), TRUE, 0, 5) ON CONFLICT DO NOTHING;
SQL
```

### 4. Start Trading System (10 seconds)
```bash
source .env
venv/bin/python3 run_system.py
```

**System Running!** You'll see:
```
✅ PostgreSQL connected
✅ Redis connected
✅ All services started
SYSTEM RUNNING
```

---

## 📊 Monitor Your Trading

### Real-Time Status
```bash
# Check shadow orders
psql -h localhost -U postgres -d test -c "
SELECT symbol, side, qty, limit_px, timestamp
FROM shadow_orders
ORDER BY timestamp DESC
LIMIT 10;
"
```

### Service Health
```bash
# Watch system status (updates every 10s in run_system.py output)
# Services: market_data, feature_calculator, strategy_engine,
#           decision_pipeline, risk_service, execution_adapter
```

---

## 📝 Write Your First Strategy

### Option 1: Python Strategy

**Create: `strategies/my_first_strategy.py`**
```python
from services.strategy.base import StrategyAlgorithm
from models.decision import DecisionEnvelope

class MyStrategy(StrategyAlgorithm):
    async def on_bar(self, symbol: str, bar: dict):
        price = bar["close"]
        ma_20 = bar.get("sma_20")
        
        # Simple logic: buy if price > MA
        if ma_20 and price > ma_20:
            return DecisionEnvelope(
                decision_id=self.generate_decision_id(),
                strategy_id=self.strategy_id,
                symbol=symbol,
                side="BUY",
                target_qty=100,
                # ... other fields
            )
        return None
```

### Option 2: Rule-Based (No Code)

**Create: `config/rules/simple.yml`**
```yaml
rule_id: buy_above_ma
conditions:
  buy:
    all:
      - field: close
        operator: gt
        value: sma_20
actions:
  buy:
    side: BUY
    qty: 100
```

---

## 🎯 Available Services

| Service | Purpose | Status |
|---------|---------|--------|
| **run_system.py** | Main orchestrator | ✅ Working |
| Market Data | IB tick → bars | ✅ Code ready |
| Feature Service | Calc indicators | ✅ Working |
| Strategy Engine | Run strategies | ✅ Code ready |
| Decision Pipeline | Validate decisions | ✅ Code ready |
| Risk Service | Reg-Lite, capital | ✅ Working |
| Execution | Send to IB | ✅ Code ready |

---

## 📈 Trading Modes

### Shadow Mode (Current)
- **Capital**: 0% (no real orders)
- **Purpose**: Test strategy logic
- **Duration**: 2 weeks minimum
```bash
TRADING_MODE=shadow
```

### Paper Mode (Week 3-4)
- **Capital**: IB paper account
- **Purpose**: Validate execution
```bash
TRADING_MODE=paper
IB_PAPER_PORT=4002
```

### Live Mode (Week 5+)
- **Capital**: Real money ⚠️
- **Process**: Canary (5% → 25% → 50% → 100%)
```bash
TRADING_MODE=live
IB_LIVE_PORT=4001
```

---

## 🔧 Common Commands

### Database
```bash
# View all strategies
psql -h localhost -U postgres -d test -c "SELECT * FROM core_strategy;"

# Check capital
psql -h localhost -U postgres -d test -c "
SELECT strategy_id, capital_limit_usd, reserved_usd
FROM core_strategy s
JOIN core_strategy_ledger l USING (strategy_id);
"

# View positions
psql -h localhost -U postgres -d test -c "SELECT * FROM core_position WHERE qty != 0;"
```

### Monitoring
```bash
# System health
curl http://localhost:9090/api/v1/query?query=up

# Shadow orders today
psql -h localhost -U postgres -d test -c "
SELECT COUNT(*) as total_orders, symbol
FROM shadow_orders
WHERE DATE(timestamp) = CURRENT_DATE
GROUP BY symbol;
"
```

### Emergency Stop
```bash
# Kill switch
psql -h localhost -U postgres -d test -c "
UPDATE symbol_config SET enabled = FALSE;
UPDATE core_strategy SET enabled = FALSE;
"

# Stop processes
pkill -f run_system.py
```

---

## 📚 Full Documentation

- **Complete Guide**: `TRADING_SYSTEM_GUIDE.md` (comprehensive)
- **Deployment**: `QUICKSTART_DEPLOY.md` (step-by-step)
- **System Status**: `FINAL_STATUS.md` (current state)
- **Architecture**: `doc/` folder (78 design docs)
- **Hyperparameter Optimization**: [HPO 2.0 Guide](config/HPO_USAGE_GUIDE.md) (for advanced parameter tuning using Bayesian Search)

---

## ✅ Current System Status

**Infrastructure**: ✅ All healthy
- PostgreSQL: Running
- ClickHouse: Running
- Redis: Running
- Kafka: Running

**Code**: ✅ 234/240 tests passing (97.5%)

**Mode**: 🌑 Shadow (safe testing)

**Next Steps**:
1. ✅ System running
2. → Write your strategy
3. → Monitor shadow orders
4. → After 2 weeks → Paper mode
5. → After validation → Live canary

---

**Quick Access:**
```bash
# Start
venv/bin/python3 run_system.py

# Monitor
watch -n 5 "psql -h localhost -U postgres -d test -c 'SELECT * FROM shadow_orders ORDER BY timestamp DESC LIMIT 5'"

# Stop
Ctrl+C (or kill switch above)
```

**Status**: System is running and ready for shadow trading! 🚀