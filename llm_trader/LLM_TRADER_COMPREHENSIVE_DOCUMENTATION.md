# LLM Trader Comprehensive Documentation

## Architecture Overview

The LLM Trader is an autonomous trading system that leverages multiple LLM providers (OpenAI, Google Gemini, DeepSeek, DeepSeek-NVIDIA) to make real-time trading decisions based on market data from ClickHouse and Redis. The system supports multiple versions with progressive enhancements.

### Version Timeline
- **V1.0**: Initial LLM decision-only system
- **V2.0**: Added memory system, time awareness, and adaptive frequency
- **V2.5**: Added data degradation handling and hard gates
- **V2.6**: Compact prompt system (83% token reduction) with two-tier execution

---

## Core Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   Main Trading Loop                         │
│                  (LLMTraderBot.run)                         │
└─────────────────────────────────────────────────────────────┘
                              ↓
        ┌─────────────────────┼─────────────────────┐
        ↓                     ↓                     ↓
   ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
   │  Data Fetch │   │ LLM Provider │   │  Market Context  │
   │ (ClickHouse │   │  (OpenAI,    │   │   (Time, Memory, │
   │    Redis)   │   │  Gemini,     │   │   Higher TF)     │
   └─────────────┘   │  DeepSeek,   │   └──────────────────┘
                     │  DeepSeek-R1)│
                     └──────────────┘
                            ↓
        ┌─────────────────────┼─────────────────────┐
        ↓                     ↓                     ↓
   ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
   │   Decision  │   │   Portfolio  │   │   Memory System  │
   │  (BUY/SELL/ │   │  (Risk Mgmt) │   │  (Learning)      │
   │   HOLD)     │   │              │   │                  │
   └─────────────┘   └──────────────┘   └──────────────────┘
                            ↓
        ┌─────────────────────┼─────────────────────┐
        ↓                     ↓                     ↓
   ┌─────────────┐   ┌──────────────┐   ┌──────────────────┐
   │  Risk Check │   │    Execute   │   │   Log Results    │
   │   (Limits)  │   │    Trade     │   │  (JSON, Memory)  │
   └─────────────┘   └──────────────┘   └──────────────────┘
```

---

## File Structure

```
llm_trader/
├── run_single_bot.py                    # Main entry point (CLI)
├── run_llm_race.py                      # Multi-provider competition mode
├── run_premarket.sh                     # Premarket trading script
├── run_morning_combo.sh                 # Premarket + first hour combo
├── run_all_providers.sh                 # All 4 providers concurrently
├── run_fast_providers.sh                # 3 fast providers only
├── run_first_hour.sh                    # First hour after market open
│
├── llm_trader/                          # Main package
│   ├── llm_trader_bot.py                # Core trading bot (async loop)
│   ├── prompt_builder_compact.py        # V2.6: Compact two-tier prompts
│   ├── prompt_builder_universal.py      # Multi-timeframe compression
│   ├── realtime_aggregator.py           # 1s → 5s/1m/5m/15m aggregation
│   ├── time_manager.py                  # EOD/premarket time management
│   ├── higher_tf_context.py             # 1h/1d background context
│   │
│   ├── data/
│   │   └── market_data_fetcher.py       # ClickHouse + Redis connector
│   │
│   ├── portfolio/
│   │   └── portfolio_manager.py         # Position, P&L, risk limits
│   │
│   ├── providers/                       # LLM provider implementations
│   │   ├── __init__.py                  # BaseLLMProvider abstract class
│   │   ├── openai_provider.py           # OpenAI (GPT-4o, etc)
│   │   ├── gemini_provider.py           # Google Gemini 2.0
│   │   ├── deepseek_provider.py         # DeepSeek V3 (fast)
│   │   └── deepseek_nvidia_provider.py  # DeepSeek-R1 via NVIDIA NIM
│   │
│   └── utils/
│       ├── config.py                    # .env configuration loader
│       ├── time_awareness.py            # V2: Market phase detection
│       ├── memory_system.py             # V2: 3-layer memory system
│       ├── prompt_builder.py            # V2: Decision/feedback/reflection prompts
│       ├── prompt_builder_r1.py         # DeepSeek-R1 optimized prompts
│       ├── trading_helpers.py           # Utility functions
│       ├── llm_logger.py                # LLM call logging
│       └── analyze_llm_logs.py          # Log analysis tools
│
├── scripts/                             # Utility scripts
├── results/                             # Output JSON results
└── logs/                                # Trading session logs
```

---

## Main Entry Point: run_single_bot.py

### Command-Line Options

```bash
usage: run_single_bot.py [-h] --provider {openai,gemini,deepseek,deepseek_nvidia}
                         [--symbols SYMBOLS ...] [--cash CASH]
                         [--interval INTERVAL] [--cycles CYCLES]
                         [--live] [--enable-v2]
                         [--enable-memory] [--enable-time-awareness]
                         [--enable-adaptive-frequency]
                         [--use-aggregator] [--prefill-5s-mins MINS]
                         [--higher-tf-refresh-sec SEC]
                         [--close-at TIME] [--close-before MINUTES]
                         [--forbid-new-positions-before MINUTES]
                         [--min-bars-1m N] [--min-bars-5m N] [--min-bars-15m N]
                         [--disable-degrade] [--wait-for-market]
                         [--compact-prompt] [--reflection-interval MINUTES]
```

### Parameter Categories

#### Core Trading Parameters
- `--provider` *(required)*: `openai`, `gemini`, `deepseek`, `deepseek_nvidia`
- `--symbols`: Stock symbols (default: loads from config.yml)
- `--cash`: Initial capital (default: $50,000)
- `--interval`: Trading cycle interval in seconds (default: 60)
- `--cycles`: Maximum cycles to run (default: infinite)
- `--live`: Enable live trading (default: dry-run/paper mode)

#### Data Source Parameters (V2+)
- `--use-aggregator`: Use realtime in-memory aggregator from 1s bars (default: True)
- `--prefill-5s-mins`: Cold start: prefill N minutes of 5s bars from ClickHouse (default: 20)
- `--higher-tf-refresh-sec`: Refresh 1h/1d background snapshots (default: 300s)

#### Time Management Parameters
- `--close-at`: Fixed EOD flatten time in ET, e.g., "15:55"
- `--close-before`: Minutes before 16:00 ET to trigger flatten (e.g., 5 = 15:55)
- `--forbid-new-positions-before`: Minutes before close to forbid opening new positions (default: 5)
- `--wait-for-market`: Wait until market opens (9:30 ET) before starting

#### V2 Feature Flags
- `--enable-v2`: Enable all V2 features (memory, time-awareness, adaptive frequency)
- `--enable-memory`: Enable memory system (V2)
- `--enable-time-awareness`: Enable time awareness (V2)
- `--enable-adaptive-frequency`: Enable adaptive polling frequency (V2)

#### V2.5 Data Degradation Parameters
- `--min-bars-1m`: Minimum 1m bars required (default: 10)
- `--min-bars-5m`: Minimum 5m bars required (default: 3)
- `--min-bars-15m`: Minimum 15m bars required (default: 1)
- `--disable-degrade`: Disable auto-degrade when data incomplete

#### V2.6 Compact Prompt Parameters
- `--compact-prompt`: Use compact prompt system (83% token reduction)
- `--reflection-interval`: Minutes between reflection prompts (default: 5)

### Usage Examples

```bash
# Basic usage - OpenAI with default symbols
python run_single_bot.py --provider openai

# Custom symbols, 20 cycles, 30 second interval
python run_single_bot.py --provider gemini --symbols AAPL TSLA NVDA --cycles 20 --interval 30

# DeepSeek-R1 with $100k capital, 10 cycles
python run_single_bot.py --provider deepseek_nvidia --cash 100000 --cycles 10

# Full V2 features with memory and time awareness
python run_single_bot.py --provider openai --enable-v2 --symbols AAPL MSFT

# Premarket trading with forced close at 9:25 AM ET
python run_single_bot.py --provider deepseek --close-at "09:25" --forbid-new-positions-before 5

# High-frequency with compact prompts
python run_single_bot.py --provider openai --compact-prompt --interval 15 --reflection-interval 5

# Wait for market open, then trade
python run_single_bot.py --provider gemini --wait-for-market --cycles 50
```

---

## LLM Providers

### Overview

All providers inherit from `BaseLLMProvider` and implement:
1. `async def analyze_market(symbol, market_data, portfolio_state) -> LLMResponse`
2. `async def health_check() -> bool`
3. Optional: `async def analyze_market_with_prompt(symbol, custom_prompt)`
4. Optional: `async def generate_response(prompt) -> str`

### Supported Providers

#### 1. OpenAI Provider
**File**: `llm_trader/providers/openai_provider.py`

- **Models**: gpt-4o-mini (default), gpt-4, gpt-5
- **API Base**: OpenAI official API
- **Timeout**: 20 seconds
- **Temperature**: 0.7 (configurable)
- **Max Tokens**: 2000

**Features**:
- JSON structured output
- V2 prompt support (custom prompts)
- Reflection/feedback generation

**Configuration**:
```python
Config.get_llm_config("openai")
# Returns: {"api_key": "sk-...", "model": "gpt-4o-mini"}
```

#### 2. Google Gemini Provider
**File**: `llm_trader/providers/gemini_provider.py`

- **Models**: gemini-2.0-flash-exp (default)
- **API Base**: Google GenAI API
- **Timeout**: 20 seconds
- **Temperature**: 0.7
- **Max Tokens**: 2000

**Features**:
- Fast inference
- JSON mode support
- Multi-modal capable

#### 3. DeepSeek Provider
**File**: `llm_trader/providers/deepseek_provider.py`

- **Models**: deepseek-chat (V3, default), deepseek-v2, deepseek-reasoning
- **API Base**: DeepSeek official API
- **Timeout**: 20 seconds
- **Temperature**: 0.7
- **Max Tokens**: 2000

**Features**:
- Cost-effective
- Fast reasoning capabilities
- OpenAI-compatible API

#### 4. DeepSeek via NVIDIA NIM Provider
**File**: `llm_trader/providers/deepseek_nvidia_provider.py`

- **Models**: deepseek-ai/deepseek-r1 (default)
- **API Base**: NVIDIA NIM (nvidia.integrate.api.com)
- **Timeout**: 60 seconds (longer for reasoning)
- **Temperature**: 0.7
- **Max Tokens**: 2000

**Features**:
- Advanced reasoning capabilities (R1 model)
- Extended thinking time
- Best for complex analysis
- Slower but more thorough

**Note**: R1 model includes extended thinking which takes 15-30 seconds per decision.

### LLM Response Structure

All providers return `LLMResponse`:
```python
@dataclass
class LLMResponse:
    decision: str              # "BUY", "SELL", "HOLD"
    symbol: str                # Stock symbol
    quantity: Optional[int]    # Number of shares
    confidence: Optional[float] # 0.0 - 1.0
    reasoning: Optional[str]   # Trading rationale
    raw_response: Optional[str] # Raw LLM output (JSON string)
```

---

## Core Bot Class: LLMTraderBot

**File**: `llm_trader/llm_trader_bot.py`

### Initialization Parameters

```python
LLMTraderBot(
    provider_name: str,              # "openai", "gemini", "deepseek", "deepseek_nvidia"
    symbols: List[str],              # Trading universe
    config: Config,                  # Configuration object
    initial_cash: float = 50_000,    # Starting capital
    dry_run: bool = True,            # Paper trading mode
    market_hint: str = "US",         # Market timezone hint
    
    # V2 features
    enable_memory: bool = False,
    enable_time_awareness: bool = False,
    enable_adaptive_frequency: bool = False,
    
    # Data source configuration
    use_aggregator: bool = True,
    prefill_5s_mins: int = 20,
    higher_tf_refresh_sec: int = 300,
    
    # Time management
    close_at: Optional[str] = None,
    close_before: Optional[int] = None,
    forbid_new_positions_before: int = 5,
    
    # V2.6 compact mode
    use_compact_prompt: bool = False,
    reflection_interval_minutes: int = 5,
)
```

### Main Trading Loop

```python
async def run(
    interval: int = 60,              # Cycle interval (seconds)
    max_cycles: Optional[int] = None, # Max cycles (None = infinite)
    wait_for_market: bool = False,   # Wait until 9:30 ET to start
)
```

**Cycle Flow**:
1. Fetch latest market data (ClickHouse + Redis)
2. For each symbol:
   - Build decision prompt (includes time context, memory if V2)
   - Call LLM provider for decision
   - Execute trade if conditions met
   - Record to memory system
3. Check for reflection triggers (V2)
4. Sleep for `interval` seconds

---

## Data Sources & Configuration

### Configuration File: config.py

**Location**: `.env` in project root

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

# Trading Universe (as comma-separated list or YAML)
TRADING_SYMBOLS=AAPL,TSLA,NVDA,MSFT,META,GOOGL,...
```

### Data Flow

#### From ClickHouse (`sfs.sfs_5s` table)
- **Content**: 5-second OHLCV bars
- **Columns**: symbol, ts, open, high, low, close, vwap, volume, trades, ret_5s
- **Update Freq**: Every 5 seconds
- **Lookback**: 12-60 bars (1-5 minutes)

#### From ClickHouse (`trading.sfs_features` table)
- **Content**: Technical indicators (1m tumble, 5m tumble)
- **Columns**: 
  - vwap_1m_tumble, atr_1m_tumble
  - vwap_5m_tumble, atr_5m_tumble
  - regime (UP_TREND, DOWN_TREND, RANGE)
  - orb5_high, orb5_low (Opening Range Breakout)
  - q80_ewma, k_ewma
  - p_up_15, trend_push_15
- **Update Freq**: Every 5 seconds
- **Lookback**: Latest value

#### From Redis (`sfs:1s:SYMBOL` key)
- **Content**: 1-second bars (from SFS service)
- **Format**: JSON array of bar objects
- **Keys**: ts, open, high, low, close, volume, trades
- **Update Freq**: Every 1 second (real-time)
- **Lookback**: Last 30-60 seconds (controlled by deque)

### MarketDataFetcher

**Location**: `llm_trader/data/market_data_fetcher.py`

**Key Methods**:
```python
async def get_latest_5s_features(symbol, limit=12) -> List[Dict]
async def get_multiple_symbols_data(symbols) -> Dict[symbol -> market_data]
async def get_1s_bars_from_redis(symbol, limit=60) -> List[Dict]
```

**Returned Market Data Structure**:
```python
{
    "symbol": "AAPL",
    "current_price": 175.25,
    "features_5s": [...],           # Latest 5s bars
    "sfs_features": {...},          # Technical indicators
    "bars_1s": [...],               # 1s bars from Redis
    "mtf_snapshot": {...},          # Multi-timeframe aggregates
}
```

---

## V2.0: Memory System

### Components

#### 1. Time Awareness (`utils/time_awareness.py`)

**Purpose**: Provides market-aware context and adaptive scheduling

**Market Phases**:
- `PREMARKET` (4:00 - 9:30 ET)
- `OPEN` (9:30 - 10:30 ET)
- `MIDDAY` (10:30 - 15:00 ET)
- `CLOSE` (15:00 - 16:00 ET)
- `AFTERHOURS` (16:00 - 20:00 ET)
- `CLOSED` (20:00 - 4:00 ET)

**Key Methods**:
```python
get_market_phase() -> MarketPhase
is_market_open() -> bool
minutes_to_open() -> int
minutes_to_close() -> int
get_adaptive_interval(has_positions) -> int  # Seconds between cycles
should_trade() -> bool
```

**Adaptive Interval Strategy**:
| Phase | No Positions | With Positions |
|-------|-------------|-----------------|
| OPEN | 30s | 15s |
| MIDDAY | 60s | 30s |
| CLOSE | 30s | 15s |
| PREMARKET (30min to open) | 60s | 30s |
| PREMARKET (>30min) | 120s | 120s |
| AFTERHOURS | 120s | 60s |
| CLOSED | 300s | 300s |

#### 2. Three-Layer Memory System (`utils/memory_system.py`)

##### Layer 1: Episodic Memory
Stores specific trade instances:
```python
@dataclass
class EpisodicMemory:
    timestamp: str
    symbol: str
    action: str              # BUY, SELL, HOLD
    quantity: int
    price: float
    confidence: float
    reasoning: str
    market_phase: str
    realized_pnl: Optional[float]
    hold_duration_minutes: Optional[int]
    outcome_verified: bool
    market_features: Dict[str, Any]
```

**Example Memory Entry**:
```
[2025-10-22T14:30:00] BUY AAPL x100 @$175.25 (MIDDAY) → Pending
[2025-10-22T14:45:00] SELL AAPL x100 @$175.50 (MIDDAY) → P&L=$25.00
```

##### Layer 2: Policy Memory
Stores learned strategic preferences:
```python
@dataclass
class PolicyMemory:
    preferred_position_size_pct: float
    confidence_threshold: float
    max_hold_duration_minutes: int
    preferred_symbols: List[str]
    avoid_symbols: List[str]
    win_rate: float
    avg_pnl_per_trade: float
    learning_rate: float
    exploration_rate: float
```

##### Layer 3: Reflection Memory
Stores periodic self-assessment:
```python
@dataclass
class ReflectionMemory:
    timestamp: str
    period: str             # "midday", "end_of_day", "weekly"
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    insights: List[str]
```

**Reflection Triggers**:
- Midday: 12:00 PM ET
- End of Day: 16:05 PM ET (after market close)

#### 3. Prompt Builder (`utils/prompt_builder.py`)

Three types of prompts:

**Decision Prompt**:
- Current market features
- Time context (phase, minutes to close, etc.)
- Recent memory episodes (last 20)
- Portfolio state
- Policy memory

**Feedback Prompt**:
- Trade outcome analysis
- Realized P&L
- Hold duration
- Expected vs actual reasoning

**Reflection Prompt**:
- Statistics (win rate, total P&L, etc.)
- Top/bottom performers
- Market phase analysis
- Planned improvements

---

## V2.5: Data Degradation Handling

### Purpose
Prevents trading when market data is incomplete or stale.

### Parameters
```python
--min-bars-1m N      # Default: 10 (need 10 minutes of 1m data)
--min-bars-5m N      # Default: 3 (need 15 minutes of 5m data)
--min-bars-15m N     # Default: 1 (need 15 minutes of 15m data)
--disable-degrade    # Disable degradation (trade anyway)
```

### Hard Gates
When insufficient data:
- **Hard Gate 1**: Insufficient 1m data → Return HOLD
- **Hard Gate 2**: Insufficient 5m data → Return HOLD
- **Hard Gate 3**: Insufficient 15m data → Return HOLD (fallback to 5m)

### Typical Cold Start Sequence
```
4:00 AM: Bot starts
4:00-4:10: Data accumulating, returns HOLD (insufficient bars)
4:10+: Data complete, normal trading begins
```

---

## V2.6: Compact Prompt System (83% Token Reduction)

### Two-Tier Architecture

**Tier 1: High-Frequency Execution** (every 15-30 seconds)
- Compact market snapshot (~1.2-1.8k tokens)
- Execute/decision schema (minimal JSON)
- Fast response expected

**Tier 2: Low-Frequency Reflection** (every 5-10 minutes)
- Full market context and memory review (~3-4k tokens)
- Strategy reflection schema
- Deep analysis permitted

### Unified JSON Schemas

**Execution Schema**:
```python
{
    "a": "B|S|H",           # Action (Buy/Sell/Hold)
    "q": 0-10000,           # Quantity
    "conf": 0.0-1.0,        # Confidence
    "tp": 50,               # Take profit (bps, optional)
    "sl": 100,              # Stop loss (bps, optional)
    "tw_s": 300             # Time window (seconds, optional)
}
```

**Reflection Schema**:
```python
{
    "style": "scalp|intra|avoid",
    "size_curve": "conservative|balanced|aggressive",
    "hold_s_range": [60, 300],      # 1-5 minute hold
    "open_filters": ["morning_volatility", "breakout"],
    "close_rules": ["hit_tp", "time_exit"],
    "confidence": 0.75
}
```

### Data Compression Techniques

**Sparse CSV Encoding** (for high-frequency bars):
```
ts,o,h,l,c,v
1729513845000,175.12,175.20,175.11,175.18,2200
1729513846000,175.18,175.25,175.15,175.22,1800
```
- **Token Reduction**: ~53% vs JSON per bar
- **Applied to**: 1s bars, 5s bars

**Basis Points Representation**:
- Instead of: `"price": 175.25, "base": 175.00` (5 tokens)
- Use: `"dbps": 25` (1 token = 25 basis points)

**Volume Normalization**:
- `to_volk(volume)` → volume in thousands
- Instead of: `"volume": 2234500` (large number)
- Use: `"v_k": 2234` (volume in thousands)

### Usage
```bash
python run_single_bot.py --provider openai --compact-prompt --interval 15
```

---

## Time Management

### Time Manager (`time_manager.py`)

**Purpose**: Handle EOD flattening and premarket close requirements

**Parameters**:
```python
close_at: Optional[str]              # Fixed time "HH:MM" in ET
close_before_min: Optional[int]      # Minutes before 16:00 ET
forbid_new_positions_before_min: int # Minutes before close to forbid new entries
```

**Session Phases**:
- `OFF` (20:00 - 4:00 ET)
- `PRE` (4:00 - 9:30 ET)
- `OPEN` (9:30 AM ET, first minute)
- `RTH` (9:30 - 16:00 ET, regular trading hours)
- `CLOSE` (16:00 - 16:15 ET)
- `POST` (16:00 - 20:00 ET)

**Methods**:
```python
should_flatten_now(now_utc) -> bool    # Should liquidate all positions?
allow_new_position(now_utc) -> bool    # Can open new position?
```

**Example**: Premarket trading
```bash
python run_single_bot.py --provider deepseek \
  --close-at "09:25" \
  --forbid-new-positions-before 5
```
- Closes all positions at 9:25 AM ET
- Forbids new opens after 9:20 AM ET

---

## Realtime Aggregator

**File**: `llm_trader/realtime_aggregator.py`

**Purpose**: In-memory aggregation of 1s bars to 5s, 1m, 5m, 15m, 30m, 1h bars

**Key Features**:
- Incremental aggregation (no re-computation)
- Rolling window calculation for technical indicators
- Fast access to multi-timeframe data

**Indicators Computed**:
- OHLCV aggregates
- EMA (Exponential Moving Average)
- RSI (Relative Strength Index)
- Z-score (volatility measure)
- ATR (Average True Range)
- VWAP (Volume Weighted Average Price)

**Methods**:
```python
add_1s_bar(symbol, bar) -> None
get_bars(symbol, timeframe) -> Deque[Dict]
get_features(symbol, timeframe) -> Dict  # OHLCV + indicators
```

---

## Portfolio Management

**File**: `llm_trader/portfolio/portfolio_manager.py`

### Risk Limits
```python
max_position_pct: float = 0.20      # Max 20% per position
max_total_exposure: float = 0.95    # Max 95% total leverage
max_positions: int = 10             # Max 10 concurrent positions
```

### Position Tracking
```python
@dataclass
class Position:
    symbol: str
    quantity: int
    avg_cost: float
    current_price: float
    entry_time: datetime
    peak_pnl_pct: float            # For trailing stops
    
    # Properties
    market_value: float             # quantity × current_price
    unrealized_pnl: float
    unrealized_pnl_pct: float
    hold_duration_minutes: int
    drawdown_from_peak: float
```

### Key Methods
```python
open_position(symbol, quantity, price) -> bool
close_position(symbol, quantity, price) -> bool
update_prices(symbol, price) -> None
get_position_state() -> Dict
get_total_pnl() -> float
```

---

## Shell Scripts

### run_premarket.sh
**Time**: 4:00 AM - 9:25 AM ET (5.5 hours)
**Provider**: DeepSeek V3 (fast)
**Frequency**: Every 2 minutes
**Cycles**: 165

**Features**:
- Premarket trading execution
- Automatic flatten at 9:25 AM ET
- Forbid new positions after 9:20 AM ET

```bash
./run_premarket.sh
```

### run_morning_combo.sh
**Time**: 4:00 AM - 10:25 AM ET (6.5 hours)
**Providers**: 
  - 4:00-9:25 AM: DeepSeek V3
  - 9:30-10:25 AM: Gemini 2.0

**Capital**: $50k × 2 providers = $100k total

```bash
./run_morning_combo.sh
```

### run_all_providers.sh
**Providers**: OpenAI, DeepSeek, Gemini, DeepSeek-NVIDIA (R1)
**Frequency**: 60-180s intervals (R1 slower)
**Cycles**: 10 per provider

**Note**: R1 runs at 180s intervals due to reasoning model slowness

```bash
./run_all_providers.sh
```

### run_fast_providers.sh
**Providers**: OpenAI (60s), DeepSeek (65s), Gemini (70s)
**Skips**: DeepSeek-NVIDIA (R1) - too slow for concurrent testing
**Cycles**: 10 per provider

```bash
./run_fast_providers.sh
```

---

## Output Files

### Results Directory Structure
```
results/
├── openai_final_20251022_150230.json
├── gemini_final_20251022_150240.json
├── deepseek_final_20251022_150245.json
├── deepseek_nvidia_final_20251022_151530.json
└── race_results_20251022_152000.json
```

### Single Bot JSON Format
```json
{
  "metadata": {
    "provider": "openai",
    "model": "gpt-4o-mini",
    "timestamp": "2025-10-22T15:02:30Z",
    "total_cycles": 10,
    "runtime_seconds": 625.5,
    "initial_cash": 50000.0
  },
  "portfolio": {
    "cash": 48750.50,
    "total_equity": 52450.50,
    "total_pnl": 2450.50,
    "total_pnl_pct": 4.90,
    "positions": {
      "AAPL": {"quantity": 50, "avg_cost": 175.25, "current_price": 176.00},
      "TSLA": {"quantity": 20, "avg_cost": 285.50, "current_price": 290.00}
    }
  },
  "trade_history": [
    {
      "timestamp": "2025-10-22T09:30:15Z",
      "symbol": "AAPL",
      "action": "BUY",
      "quantity": 50,
      "price": 175.25,
      "reason": "Strong ORB5 breakout"
    },
    {
      "timestamp": "2025-10-22T09:45:30Z",
      "symbol": "AAPL",
      "action": "SELL",
      "quantity": 50,
      "price": 175.80,
      "realized_pnl": 27.50,
      "reason": "Take profit at +30bps"
    }
  ],
  "statistics": {
    "total_trades": 15,
    "winning_trades": 10,
    "losing_trades": 5,
    "win_rate": 0.667,
    "avg_pnl_per_trade": 163.37,
    "largest_win": 250.00,
    "largest_loss": -75.50,
    "max_drawdown_pct": -2.15
  }
}
```

### Race Results Format
```json
{
  "timestamp": "2025-10-22T15:20:00Z",
  "race_duration_seconds": 1200,
  "total_capital": 200000,
  "results": [
    {
      "rank": 1,
      "provider": "gemini",
      "initial_cash": 50000,
      "final_equity": 52450.50,
      "pnl": 2450.50,
      "pnl_pct": 4.90,
      "trades": 15,
      "win_rate": 0.667,
      "model": "gemini-2.0-flash-exp"
    },
    {
      "rank": 2,
      "provider": "openai",
      "initial_cash": 50000,
      "final_equity": 51832.20,
      "pnl": 1832.20,
      "pnl_pct": 3.66,
      "trades": 12,
      "win_rate": 0.583,
      "model": "gpt-4o-mini"
    }
  ]
}
```

---

## Configuration via Environment Variables

### Essential Variables
```bash
# Data Sources
CH_HOST=localhost
CH_PORT=8123
CH_USER=default
CH_PASSWORD=
CH_DB=trading

STATEBUS_REDIS=redis://127.0.0.1:6379/0

# IBKR Integration
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002
IB_CLIENT_ID=100

# LLM API Keys
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
NVIDIA_API_KEY=nvapi-...

# Optional
LLM_MODEL=gpt-4o-mini    # Override default model
TRADING_SYMBOLS=...      # Override universe
```

---

## Common Usage Patterns

### Pattern 1: Test Before Trading
```bash
# 1. Test setup
python -c "from llm_trader.utils.config import Config; Config().trading_symbols"

# 2. Dry run with 5 cycles
python run_single_bot.py --provider openai --cycles 5

# 3. Review results
cat results/openai_final_*.json | jq .portfolio
```

### Pattern 2: Premarket Session
```bash
# Run 4:00 AM - 9:25 AM ET
./run_premarket.sh

# Monitor progress
tail -f logs/premarket_deepseek_*.log

# Check results
cat results/deepseek_final_*.json | jq .statistics
```

### Pattern 3: Morning Combo (High Frequency)
```bash
# Run premarket (slow) + opening hour (fast)
./run_morning_combo.sh

# In another terminal, monitor both
watch 'ps aux | grep run_single_bot'
```

### Pattern 4: Provider Comparison
```bash
# Run all 4 providers concurrently
./run_all_providers.sh

# Compare results
for f in results/*_final_*.json; do
  echo "=== $(basename $f) ==="
  jq '.statistics | {trades, win_rate, total_pnl}' $f
done
```

### Pattern 5: High-Frequency with Compact Mode
```bash
# Ultra-compact, 15-second cycles
python run_single_bot.py \
  --provider openai \
  --compact-prompt \
  --interval 15 \
  --reflection-interval 5 \
  --cycles 200
```

---

## Monitoring & Debugging

### Real-Time Monitoring
```bash
# Watch trading cycles in progress
tail -f logs/openai_*.log | grep -E "Decision:|Portfolio:|P&L:"

# Extract decision statistics
grep "LLM Decision" logs/*.log | wc -l

# Find errors
grep -i "error\|exception" logs/*.log
```

### Log Analysis
```bash
python llm_trader/utils/analyze_llm_logs.py logs/openai_*.log
```

### Token Usage Tracking
```bash
python compare_token_usage.py
```

### Data Validation
```bash
# Check ClickHouse data
python llm_trader/check_clickhouse_tables.py

# Check Redis data
redis-cli LINDEX "sfs:1s:AAPL" 0
```

---

## Performance Characteristics

### Typical Execution Times

| Provider | Decision Time | Per Cycle Time |
|----------|---------------|-----------------|
| OpenAI (gpt-4o-mini) | 0.5-2.0s | 5-10s |
| Gemini (2.0-flash-exp) | 0.5-2.0s | 5-10s |
| DeepSeek (V3) | 1-3s | 10-15s |
| DeepSeek-NVIDIA (R1) | 15-30s | 20-40s |

### Token Consumption

| Prompt Type | Tokens (V1) | Tokens (V2.6) | Reduction |
|-------------|-----------|---------------|-----------|
| Standard Decision | 2,000 | 300 | 85% |
| With Memory | 3,500 | 1,200 | 66% |
| With Reflection | 4,500 | 2,000 | 56% |

### Memory Usage
- Single bot instance: ~200-300 MB
- With V2 memory system: +100-150 MB
- 4 concurrent bots: ~1.2-1.8 GB

---

## Future Enhancements (Planned)

1. **Live Trade Execution** via IBKR API
2. **Multi-Market Support** (HK, EU, Crypto)
3. **Advanced Strategy Composition** (combining multiple LLMs)
4. **Risk Controls** (max drawdown limits, volatility-based sizing)
5. **Real-time P&L Tracking Dashboard**
6. **Extended Market Context** (macroeconomic data, news)
7. **Model Fine-tuning** (custom LLM training on historical data)

