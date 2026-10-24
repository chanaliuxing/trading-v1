# LLM Auto Trading System V2.0 🧠

## 🎯 From "Decision Assistant" to "Autonomous Trading Agent"

A standalone LLM-powered automated trading system with **time awareness**, **three-layer memory**, and **continuous learning** capabilities.

### V2.0 Major Upgrades
- ✅ **Time Awareness**: Market phase detection + adaptive polling frequency
- ✅ **Three-Layer Memory**: Episodic (trades) + Policy (strategy) + Reflection (learnings)
- ✅ **Three Prompt Types**: Decision, Feedback, Reflection
- ✅ **Self-Learning Loop**: Outcome verification → Strategy adjustment
- ✅ **Token Optimization**: Compressed prompts for cost efficiency

📖 **See [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md) for detailed architecture explanation**

## 🎯 Features

- **Multi-LLM Support**: Run different LLM providers in parallel and compare performance
  - **OpenAI**: `gpt-4o-mini` (fast reasoning)
  - **Gemini**: `gemini-2.0-flash-exp` (fast experimental)
  - **DeepSeek**: `deepseek-chat` (DeepSeek V3 - fast)
  - **DeepSeek-NVIDIA**: `deepseek-r1` (DeepSeek R1 - deep reasoning via NVIDIA NIM)

**Note**: 3 fast models vs 1 reasoning model - see `LLM_MODELS.md` for detailed comparison

- **Real-time Data**: Fetches market features from:
  - ClickHouse `sfs.sfs_5s` - 5-second sliding window features
  - ClickHouse `trading.sfs_features` - Technical indicators (VWAP, ATR, regime, etc.)
  - Redis `sfs:1s:SYMBOL` - 1-second bars from SFS service

- **Risk Management**:
  - $50,000 initial capital (configurable)
  - Max 20% per position
  - Max 95% total exposure
  - Max 10 concurrent positions
  - Rolling capital reinvestment

- **Trading Execution**:
  - IBKR Paper Trading (default)
  - IBKR Live Trading (with `--live` flag)
  - Dry run simulation mode

- **LLM Competition Mode**: Run multiple LLM providers concurrently and see which performs best!

## 📁 Project Structure

```
llm_trader/
├── llm_trader/
│   ├── __init__.py
│   ├── providers/              # LLM provider implementations
│   │   ├── __init__.py
│   │   ├── openai_provider.py
│   │   ├── gemini_provider.py
│   │   ├── deepseek_provider.py
│   │   └── deepseek_nvidia_provider.py
│   ├── data/                   # Market data fetching
│   │   ├── __init__.py
│   │   └── market_data_fetcher.py
│   ├── trading/                # IBKR execution
│   │   ├── __init__.py
│   │   └── ibkr_executor.py
│   ├── portfolio/              # Portfolio & risk management
│   │   ├── __init__.py
│   │   └── portfolio_manager.py
│   ├── utils/                  # Utilities
│   │   ├── __init__.py
│   │   └── config.py
│   └── llm_trader_bot.py       # Main bot logic
├── config/                     # Configuration files
├── logs/                       # Log files
├── results/                    # Trading results and P&L reports
├── run_single_bot.py           # Run single LLM bot
├── run_llm_race.py             # Run LLM competition
├── requirements.txt
└── README.md
```

## 🚀 Quick Start

### 1. Installation

```bash
cd llm_trader
pip install -r requirements.txt
```

### 2. Configuration

Ensure your `.env` file in the parent directory contains:

```bash
# ClickHouse
CH_HOST=localhost
CH_PORT=8123
CH_USER=default
CH_PASSWORD=
CH_DB=trading

# Redis
STATEBUS_REDIS=redis://127.0.0.1:6379/0

# IBKR
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_CLIENT_ID=100

# LLM API Keys
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
NVIDIA_API_KEY=nvapi-...
```

### 3. Run Single Bot

```bash
# Dry run with OpenAI (default)
python run_single_bot.py --provider openai --symbols AAPL TSLA NVDA

# Gemini with custom settings
python run_single_bot.py --provider gemini --symbols AAPL MSFT META --cycles 20 --interval 30

# DeepSeek R1 via NVIDIA
python run_single_bot.py --provider deepseek_nvidia --symbols NVDA AMD --cash 100000

# Live trading (careful!)
python run_single_bot.py --provider openai --symbols AAPL --live
```

### 4. Run LLM Race (Competition Mode)

Run all 4 LLM providers in parallel and compare performance:

```bash
# Default: All providers, 5 symbols, 10 cycles
python run_llm_race.py

# Custom race
python run_llm_race.py \
  --providers openai gemini deepseek \
  --symbols AAPL TSLA NVDA MSFT META GOOGL \
  --cash 100000 \
  --cycles 20 \
  --interval 30
```

Output example:
```
🏁 LLM TRADING RACE 🏁
═════════════════════════════════════════════════════════════════════════════

Race Configuration:
  Providers: openai, gemini, deepseek, deepseek_nvidia
  Symbols: AAPL, TSLA, NVDA, MSFT, META
  Initial Cash: $50,000.00
  Interval: 60s
  Max Cycles: 10
  Mode: DRY RUN (Simulation)

...

🏆 RACE RESULTS 🏆
═════════════════════════════════════════════════════════════════════════════

🥇 GEMINI              | P&L: $  2,450.50 ( 4.90%) | Equity: $ 52,450.50 | Trades:  15 | Cycles:  10
🥈 OPENAI              | P&L: $  1,832.20 ( 3.66%) | Equity: $ 51,832.20 | Trades:  12 | Cycles:  10
🥉 DEEPSEEK_NVIDIA     | P&L: $  1,205.00 ( 2.41%) | Equity: $ 51,205.00 | Trades:   9 | Cycles:  10
4. DEEPSEEK            | P&L: $    -125.50 (-0.25%) | Equity: $ 49,874.50 | Trades:   8 | Cycles:  10
```

## 📊 How It Works

### Trading Cycle

1. **Fetch Market Data**: Get latest 5s features, SFS indicators, and 1s bars
2. **LLM Analysis**: Send market data + portfolio state to LLM
3. **Decision**: LLM returns BUY/SELL/HOLD with quantity and confidence
4. **Risk Check**: Validate against position limits
5. **Execute**: Place order via IBKR (or simulate in dry run)
6. **Update**: Update portfolio state and P&L
7. **Wait**: Sleep for interval, then repeat

### LLM Prompt Structure

Each LLM receives:
- **Current State**: Price, portfolio cash, positions, limits
- **Market Features**: 5s OHLCV, volume, returns
- **Technical Indicators**: VWAP, ATR, regime (UP_TREND/DOWN_TREND/RANGE), ORB levels
- **Task**: Return JSON decision with confidence score

### Data Sources

| Source | Table | Data | Frequency |
|--------|-------|------|-----------|
| ClickHouse | `sfs.sfs_5s` | 5s sliding window OHLCV | Every second |
| ClickHouse | `trading.sfs_features` | Technical indicators (VWAP, ATR, regime) | Every 5s |
| Redis | `sfs:1s:SYMBOL` | 1s bars from SFS service | Real-time |

## ⚙️ Configuration Options

### Portfolio Settings

Edit in `llm_trader_bot.py`:

```python
PortfolioManager(
    initial_cash=50000.0,        # Starting capital
    max_position_pct=0.20,       # Max 20% per position
    max_total_exposure=0.95,     # Max 95% total exposure
    max_positions=10,            # Max concurrent positions
)
```

### LLM Models

Edit in `utils/config.py`:

```python
configs = {
    "openai": {"model": "gpt-4o-mini"},        # Or "gpt-5", "gpt-4o"
    "gemini": {"model": "gemini-2.0-flash-exp"},
    "deepseek": {"model": "deepseek-chat"},    # Or "deepseek-v3"
    "deepseek_nvidia": {"model": "deepseek/deepseek-r1"},
}
```

## 🎮 Command Reference

### run_single_bot.py

```bash
python run_single_bot.py \
  --provider {openai|gemini|deepseek|deepseek_nvidia} \
  --symbols AAPL TSLA NVDA \
  --cash 50000 \
  --interval 60 \
  --cycles 10 \
  [--live]
```

### run_llm_race.py

```bash
python run_llm_race.py \
  --providers openai gemini deepseek deepseek_nvidia \
  --symbols AAPL TSLA NVDA MSFT META \
  --cash 50000 \    # Each LLM gets $50k independently
  --interval 60 \
  --cycles 10
```

**Important**: Each LLM bot gets its own independent capital pool. If you run 4 LLMs with `--cash 50000`, the total capital used is 4 × $50,000 = $200,000 (but each bot manages its own $50k independently).

## 📈 Results and Logs

- **Portfolio State**: Saved to `results/{provider}_final_{timestamp}.json`
- **Race Results**: Saved to `results/race_results_{timestamp}.json`
- **Trade History**: Included in portfolio state JSON

## 🔒 Safety Features

- **Dry Run Default**: All trades are simulated unless `--live` is explicitly set
- **Position Limits**: Enforced at portfolio level
- **Confidence Threshold**: Only execute trades with confidence >= 0.6
- **Risk Checks**: Validate cash, buying power, and position size before each trade
- **Error Handling**: Graceful degradation on API failures

## 🛠️ Development

### Adding New LLM Provider

1. Create provider class in `providers/your_provider.py`
2. Inherit from `BaseLLMProvider`
3. Implement `analyze_market()` and `health_check()`
4. Add to provider factory in `llm_trader_bot.py`
5. Add API key to `utils/config.py`

### Testing

```bash
# Test data fetcher
python -c "
import asyncio
from llm_trader.data.market_data_fetcher import MarketDataFetcher
async def test():
    fetcher = MarketDataFetcher()
    await fetcher.connect()
    data = await fetcher.get_comprehensive_market_data('AAPL')
    print(data)
    await fetcher.close()
asyncio.run(test())
"

# Test LLM provider
python -c "
import asyncio
from llm_trader.providers.openai_provider import OpenAIProvider
from llm_trader.utils.config import Config
async def test():
    config = Config()
    provider = OpenAIProvider(config.openai_api_key, 'gpt-4o-mini')
    healthy = await provider.health_check()
    print(f'Health: {healthy}')
asyncio.run(test())
"
```

## 🚨 Important Notes

1. **Not Production Ready**: This is a skeleton/prototype for experimentation
2. **Paper Trading Recommended**: Always test thoroughly before going live
3. **Data Dependency**: Requires SFS (Sliding Feature Service) to be running
4. **IBKR Connection**: Requires IBKR Gateway or TWS running
5. **API Costs**: LLM API calls cost money - monitor usage!

## 📝 TODO / Future Enhancements

- [ ] Add backtesting mode using historical data
- [ ] Implement stop-loss and take-profit orders
- [ ] Add more technical indicators to LLM prompt
- [ ] Support for options trading
- [ ] Web dashboard for monitoring
- [ ] Database logging for trades
- [ ] Performance metrics and Sharpe ratio calculation
- [ ] Email/Slack alerts for significant events

## 📄 License

See parent project license.

## 🙏 Credits

Built on top of existing trading infrastructure using ClickHouse, Redis, and IBKR.
