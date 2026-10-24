# LLM Trader - Quick Start Guide

> **For comprehensive documentation**, see [LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md](./LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md)

## 🚀 Quick Start (5 Minutes)

### 1. Basic Test Run (Dry-Run Mode)

Test a single provider immediately:

```bash
python llm_trader/run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL \
  --cycles 5
```

This will:
- Use DeepSeek R1 (NVIDIA API)
- Trade AAPL only
- Run 5 cycles (about 5 minutes)
- **DRY RUN** mode (no real trades)

### 2. Schedule for Market Open (9:30 AM ET)

Wait until 9:30 AM ET, then run every 3 minutes until 10:30 AM:

```bash
python llm_trader/run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL TSLA NVDA \
  --interval 180 \
  --cycles 20 \
  --close-at "10:30" \
  --wait-for-market
```

**Key parameters:**
- `--interval 180` = Run every 180 seconds (3 minutes)
- `--cycles 20` = 20 cycles (9:30-10:30 = 60 min / 3 min)
- `--close-at "10:30"` = Auto-flatten at 10:30 AM ET
- `--wait-for-market` = Wait until 9:30 AM ET before starting

### 2b. Schedule for Premarket Open (4:00 AM ET) - NEW! ✨

**Run command anytime, bot auto-waits until 4:00 AM ET to start trading:**

```bash
python llm_trader/run_single_bot.py \
  --provider deepseek_nvidia \
  --symbols AAPL TSLA NVDA \
  --interval 600 \
  --cycles 33 \
  --close-at "09:30" \
  --forbid-new-positions-before 5 \
  --wait-for-premarket
```

**What happens:**
```
⏰ WAITING FOR PREMARKET OPEN
Current time (ET): 2025-10-22 23:52:22
Premarket opens:   2025-10-23 04:00:00 ET (4:00)
Time to wait:      4h 7m 38s

Bot will automatically start trading at premarket open...
Press Ctrl+C to cancel.
```

**Key parameters:**
- `--interval 600` = Run every 600 seconds (10 minutes)
- `--cycles 33` = 33 cycles (4:00-9:30 = 330 min / 10 min)
- `--close-at "09:30"` = Auto-flatten at 9:30 AM ET
- `--forbid-new-positions-before 5` = Stop new positions 5 min before 9:30
- `--wait-for-premarket` = **Auto-wait until 4:00 AM ET** (NEW!)

### 3. Live Trading (⚠️ REAL MONEY)

```bash
python llm_trader/run_single_bot.py \
  --provider openai \
  --symbols AAPL \
  --live \
  --close-before 5
```

**WARNING:** `--live` flag enables real trading!

---

## 📋 Common Commands

### Test All Providers

```bash
# Quick test (2 cycles each)
./llm_trader/run_fast_providers.sh

# Full test (10 cycles each)
./llm_trader/run_all_providers.sh
```

### Run Specific Scenarios

```bash
# Premarket (4:00-9:30 AM ET)
./llm_trader/run_premarket.sh

# First hour (9:30-10:30 AM ET)
./llm_trader/run_first_hour.sh

# Morning combo (multiple providers)
./llm_trader/run_morning_combo.sh
```

### Compare LLM Performance

```bash
python llm_trader/run_llm_race.py \
  --providers deepseek_nvidia deepseek openai \
  --symbols AAPL NVDA \
  --duration-minutes 30
```

---

## 🎯 LLM Providers

| Provider | Flag | Model | Speed | Cost | Token Savings |
|----------|------|-------|-------|------|---------------|
| **DeepSeek R1** | `deepseek_nvidia` | deepseek-r1 | Slow (reasoning) | $$ | 83% (compact) |
| **DeepSeek V3** | `deepseek` | deepseek-chat | Fast | $ | 81.5% (compact) |
| **OpenAI** | `openai` | gpt-4o-mini | Fast | $$$ | 73% (compact) |
| **Gemini** | `gemini` | gemini-2.5-flash | Fast | $ | 50% (compact) |

**Compact prompts are enabled by default** (V2.6)

---

## ⚙️ Key Parameters

### Time Controls

```bash
--wait-for-market           # Wait until 9:30 AM ET to start
--wait-for-premarket        # Wait until 4:00 AM ET to start (NEW!)
--close-at "15:55"          # Flatten all positions at specific time
--close-before 5            # Flatten N minutes before 16:00 ET
--forbid-new-positions-before 5  # No new positions N min before close
```

### Trading Controls

```bash
--symbols AAPL TSLA NVDA    # Stocks to trade
--cash 100000.0             # Initial capital ($50k default)
--interval 60               # Seconds between cycles (60s default)
--cycles 100                # Max cycles (unlimited default)
--live                      # Enable real trading (DRY-RUN default)
```

### V2.6 Features (Token Optimization)

```bash
--compact-prompt            # Use compact prompt format (DEFAULT: ON)
--no-compact-prompt         # Disable compact prompts
```

Compact prompts reduce tokens by:
- DeepSeek R1: **83%** (3600 → 600 tokens)
- DeepSeek V3: **81.5%**
- OpenAI: **73%**
- Gemini: **50%**

### V2.0 Features (Advanced)

```bash
--enable-v2                 # Enable ALL V2 features
--enable-memory             # Enable 3-layer memory system
--enable-time-awareness     # Enable market phase awareness
--enable-adaptive-frequency # Enable dynamic polling
```

### Data Controls

```bash
--use-aggregator            # Use 1s bar aggregator (DEFAULT: ON)
--prefill-5s-mins 20        # Prefill N minutes of 5s bars
--min-bars-1m 10            # Min 1m bars required for trading
--min-bars-5m 3             # Min 5m bars required for trading
--disable-auto-degrade      # Disable data degradation handling
```

---

## 📊 Output & Results

### Log Files

Logs are created in `llm_trader/logs/`:

```
logs/
├── deepseek_nvidia_20251022_153653.log              # Main log
├── deepseek_nvidia_20251022_153653_api_detailed.txt # API calls
└── trading_system.log                                # System log
```

### Results Files

Results saved in `results/`:

```
results/
└── deepseek_nvidia_final_20251022_153653.json       # Portfolio state
```

**Result format:**
```json
{
  "total_equity": 50500.0,
  "total_pnl": 500.0,
  "pnl_pct": 1.0,
  "cash": 48000.0,
  "positions": {
    "AAPL": {
      "quantity": 10,
      "avg_cost": 250.0,
      "current_price": 255.0,
      "unrealized_pnl": 50.0
    }
  },
  "trades": [...]
}
```

---

## 🔍 Monitoring

### Check Running Bots

```bash
ps aux | grep run_single_bot.py
```

### Stop All Bots

```bash
pkill -f run_single_bot.py
# or
pkill -f run_system.py
```

### View Live Logs

```bash
tail -f llm_trader/logs/deepseek_nvidia_*.log
```

### Check Last API Call

```bash
tail -100 llm_trader/logs/deepseek_nvidia_*_api_detailed.txt
```

---

## 🐛 Troubleshooting

### "No realtime data from Redis"

**Cause:** SFS-1s service not running or no market data

**Solution:**
```bash
# Check if SFS-1s is running
docker ps | grep sfs

# Check Redis data
redis-cli LINDEX "sfs:1s:AAPL" 0
```

### "Confidence always 0.00"

**Cause:** Bot parsing compact format incorrectly (FIXED in latest version)

**Solution:** Update to latest code with compact prompt fixes

### "API quota exceeded" (Gemini)

**Cause:** Gemini free tier has strict rate limits

**Solution:** Use paid API key or switch providers

### "ModuleNotFoundError"

**Cause:** Running from wrong directory or venv not activated

**Solution:**
```bash
cd .
source venv/bin/activate
python llm_trader/run_single_bot.py ...
```

---

## 📚 Advanced Topics

### Environment Variables

Required in `.env`:

```bash
# LLM API Keys
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
NVIDIA_API_KEY=nvapi-...

# Trading
TRADING_SYMBOLS=AAPL,TSLA,NVDA,MSFT,GOOGL,META,AMZN,AMD

# Database
CLICKHOUSE_HOST=localhost
CLICKHOUSE_PORT=9000
REDIS_HOST=localhost
REDIS_PORT=6379

# IBKR (for live trading)
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=1
```

### Market Phases

The bot recognizes these market phases:

| Phase | Time (ET) | Allow New Positions | Description |
|-------|-----------|---------------------|-------------|
| OFF | Before 4:00 AM | ❌ No | Market closed |
| PRE | 4:00-9:30 AM | ✅ Yes | Pre-market |
| OPEN | 9:30 AM | ✅ Yes | Market open |
| RTH | 9:30-16:00 | ✅ Yes | Regular hours |
| CLOSE | 16:00-16:15 | ❌ No | Close auction |
| POST | 16:15-20:00 | ⚠️ Configurable | After hours |

### Memory System (V2.0)

When `--enable-memory` is used:

- **Episodic Memory**: Recent market events (last N cycles)
- **Policy Memory**: What worked/didn't work for each symbol
- **Reflection**: LLM self-analysis every N minutes

Example:
```bash
python llm_trader/run_single_bot.py \
  --provider openai \
  --symbols AAPL \
  --enable-memory \
  --enable-time-awareness
```

### Adaptive Frequency (V2.0)

When `--enable-adaptive-frequency` is used:

- **High volatility**: Poll more frequently
- **Low volatility**: Poll less frequently
- **Pre-market**: Slower polling
- **RTH**: Faster polling

---

## 🎓 Learning Path

### Beginner

1. ✅ Run basic dry-run test (5 cycles)
2. ✅ Check logs and results files
3. ✅ Try different providers
4. ✅ Understand market phases

### Intermediate

1. ✅ Use `--wait-for-market` for scheduling
2. ✅ Set up custom time windows with `--close-at`
3. ✅ Run comparison tests with multiple providers
4. ✅ Monitor compact prompt token savings

### Advanced

1. ✅ Enable V2.0 features (memory, time-awareness)
2. ✅ Configure adaptive frequency
3. ✅ Set up data degradation handling
4. ✅ Run live trading with IBKR

---

## 📖 Full Documentation

For complete details, see:

- **[LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md](./LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md)** - Full technical documentation (30 KB, 1057 lines)
- **[DOCUMENTATION_SUMMARY.md](./DOCUMENTATION_SUMMARY.md)** - Quick reference index
- **[LLM_Trader_QUICKSTART.md](./LLM_Trader_QUICKSTART.md)** - System architecture overview

---

## 🆘 Support

**Issues:**
- Check logs in `llm_trader/logs/`
- Verify API keys in `.env`
- Ensure Docker services running (ClickHouse, Redis)
- Check market hours and data availability

**Performance:**
- Compact prompts save 50-83% tokens
- DeepSeek V3 is fastest LLM
- R1 is slowest but most analytical
- Use `--interval 180` or higher for R1

---

**Last Updated:** 2025-10-22
**Version:** V2.6 (Compact Prompt System)
**Maintained By:** Trading Team
