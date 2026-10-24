# LLM Trader - Complete Documentation Summary

## Overview

This document serves as an index to comprehensive documentation for the LLM Trader system - a multi-provider autonomous trading platform that uses LLMs (OpenAI, Google Gemini, DeepSeek) to make real-time trading decisions.

---

## Available Documentation

### 1. Main Documentation: `LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md`
The primary reference document (1000+ lines) covering:

#### Architecture & Design
- Core architecture diagram and data flow
- Complete file structure breakdown
- Version timeline (V1.0 → V2.6)

#### Entry Points & CLI
- `run_single_bot.py` - All command-line options explained
  - Core trading parameters
  - Data source configuration
  - Time management parameters
  - V2.0 feature flags
  - V2.5 degradation parameters
  - V2.6 compact prompt parameters
- Usage examples for every scenario
- 7 major shell scripts documented

#### LLM Providers
- Provider overview (4 implementations)
- OpenAI (gpt-4o-mini, gpt-4, gpt-5)
- Google Gemini (2.0-flash-exp)
- DeepSeek (V3, fast)
- DeepSeek-NVIDIA (R1, reasoning model)
- LLM response structure (standardized)

#### Core Components
- `LLMTraderBot` class (main trading loop)
- `MarketDataFetcher` (ClickHouse + Redis)
- `PortfolioManager` (positions & risk)
- `RealtimeAggregator` (1s → multi-timeframe)
- `TimeManager` (EOD/premarket scheduling)
- `HigherTFContextLoader` (background analysis)

#### Version Features
- **V2.0**: Memory system (3-layer), time awareness, adaptive frequency
  - Episodic memory (specific trades)
  - Policy memory (learned preferences)
  - Reflection memory (self-assessment)
  - Reflection triggers (midday 12:00, EOD 16:05)

- **V2.5**: Data degradation handling
  - Hard gates for insufficient data
  - Min bars requirements (1m, 5m, 15m)
  - Cold start sequence (4:00-4:10 HOLD period)

- **V2.6**: Compact prompt system (83% token reduction)
  - Two-tier architecture (high-frequency + reflection)
  - Execution schema (minimal JSON)
  - Reflection schema (strategy analysis)
  - Data compression (CSV encoding, basis points)

#### Configuration & Data Sources
- .env configuration variables
- ClickHouse tables (sfs.sfs_5s, trading.sfs_features)
- Redis keys (sfs:1s:SYMBOL)
- Market data structure
- Technical indicators available

#### Time Management
- Market phases (PREMARKET, OPEN, MIDDAY, CLOSE, AFTERHOURS, CLOSED)
- Adaptive interval strategy
- EOD flattening control
- Premarket position closing

#### Output & Results
- Results JSON format
- Trade history structure
- Performance statistics
- Race results format (multi-provider)

#### Practical Usage
- Common usage patterns
- Testing workflows
- Monitoring & debugging
- Performance characteristics
- Token consumption tables
- Memory usage estimates

---

## Quick Reference

### Command Examples

```bash
# Basic start
python run_single_bot.py --provider openai

# Premarket trading (4:00-9:25 AM ET)
python run_single_bot.py --provider deepseek --close-at "09:25" --forbid-new-positions-before 5

# Full V2 features
python run_single_bot.py --provider openai --enable-v2 --symbols AAPL TSLA

# High-frequency compact mode
python run_single_bot.py --provider openai --compact-prompt --interval 15

# Provider race (all 4 concurrent)
python run_llm_race.py

# Shell script for premarket
./run_premarket.sh

# Shell script for morning combo (premarket + opening hour)
./run_morning_combo.sh
```

### Key File Locations

| Component | Location |
|-----------|----------|
| Main bot | `llm_trader/llm_trader_bot.py` |
| CLI entry | `run_single_bot.py` |
| Providers | `llm_trader/providers/*.py` |
| Market data | `llm_trader/data/market_data_fetcher.py` |
| Portfolio | `llm_trader/portfolio/portfolio_manager.py` |
| Time awareness | `llm_trader/utils/time_awareness.py` |
| Memory system | `llm_trader/utils/memory_system.py` |
| Prompt builder | `llm_trader/utils/prompt_builder.py` |
| Realtime agg | `llm_trader/realtime_aggregator.py` |
| Configuration | `llm_trader/utils/config.py` |

### Configuration Variables (`.env`)

```bash
# Data connections
CH_HOST=localhost
CH_PORT=8123
CH_DB=trading
STATEBUS_REDIS=redis://127.0.0.1:6379/0

# IBKR
IB_HOST=127.0.0.1
IB_PAPER_PORT=4002

# LLM API Keys
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=AIza...
DEEPSEEK_API_KEY=sk-...
NVIDIA_API_KEY=nvapi-...
```

---

## Version Comparison Matrix

| Feature | V1.0 | V2.0 | V2.5 | V2.6 |
|---------|------|------|------|------|
| LLM Decision | ✓ | ✓ | ✓ | ✓ |
| Time Awareness | - | ✓ | ✓ | ✓ |
| Memory System | - | ✓ | ✓ | ✓ |
| Adaptive Frequency | - | ✓ | ✓ | ✓ |
| Data Degradation | - | - | ✓ | ✓ |
| Compact Prompts | - | - | - | ✓ |
| Token Reduction | - | - | - | 83% |

---

## Architecture at a Glance

```
Input Sources                Processing                 Output
───────────────────────────────────────────────────────────────────

ClickHouse (sfs_5s)    ──┐
ClickHouse (features)  ──┤
Redis (1s bars)        ──┤
                          ├─→ Market Data ─→ Time Awareness ─┐
                          │    Aggregator    & Memory System  │
                          │    & Features                      │
Current Portfolio ─────────┘                                  │
                                                              ↓
                        LLM Provider Analysis ←─────────────┐
                        (GPT/Gemini/DeepSeek)   Prompt Builder
                                  ↓
                        Decision (BUY/SELL/HOLD)
                                  ↓
                        Risk Checks ←─ Portfolio Manager
                        (Limits, Gates)
                                  ↓
                        Execute Trade
                                  ↓
Output: JSON Results, Logs, Updated Portfolio, Memory Updates
```

---

## Learning Path

### Getting Started
1. Read this DOCUMENTATION_SUMMARY.md
2. Review LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md sections:
   - Architecture Overview
   - Main Entry Point
   - Quick examples

### Understanding Components
3. Explore file structure in comprehensive docs
4. Study each provider implementation
5. Understand market data flow

### Running the System
6. Check Configuration section for .env setup
7. Run first test with 5 cycles (Pattern 1 in docs)
8. Review output JSON structure
9. Progress to premarket/combo scripts

### Advanced Features
10. Enable V2 features (memory, time awareness)
11. Experiment with V2.6 compact prompts
12. Try multi-provider race mode
13. Analyze token consumption & performance

---

## Related Documentation Files

Other documentation in the project:

- `LLM_Trader_QUICKSTART.md` - Quick start guide
- `llmDoc/LLM_Trader_ARCHITECTURE_V2.md` - V2.0 architecture details
- `llmDoc/V2_INTEGRATION_COMPLETE.md` - Integration status
- Various test guides and implementation summaries in `llmDoc/` directory

---

## Support Files

Utility scripts for analysis and testing:

- `show_full_prompt.py` - Display full prompt being sent to LLM
- `compare_token_usage.py` - Analyze token efficiency across providers
- `check_clickhouse_tables.py` - Verify data availability
- `llm_trader/utils/analyze_llm_logs.py` - Parse and analyze logs

---

## Key Statistics

### Code Size
- Main documentation: 1,057 lines
- Core bot class: ~500 lines
- Total providers: ~400 lines (4 implementations)
- Memory system: ~280 lines
- Time awareness: ~290 lines

### Performance Specs
- Fastest provider: Gemini/OpenAI (0.5-2s per decision)
- Slowest provider: DeepSeek-R1 (15-30s per decision)
- Token reduction (V2.6): 83% compared to V1
- Memory overhead (V2): +100-150 MB per instance

### Market Phases
- 6 distinct phases recognized
- Adaptive intervals: 15s (busy) - 300s (closed)
- Reflection triggers: 2 per day

---

## Version History

- **V1.0** (Original): Basic LLM decision loop
- **V2.0** (Current): Added memory, time awareness, reflections
- **V2.5** (Current): Data degradation handling, hard gates
- **V2.6** (Current): Compact prompt system with 83% token reduction

---

## Getting Help

1. Check comprehensive documentation for specific topics
2. Review usage examples for your use case
3. Check log files in `logs/` directory
4. Use monitoring commands in documentation
5. Verify configuration in `.env` file

---

## Document Maintenance

Last updated: 2025-10-22
Covers all features through V2.6 (compact prompt system)
All command examples tested and verified

---

For detailed information on any topic, see `LLM_TRADER_COMPREHENSIVE_DOCUMENTATION.md`
