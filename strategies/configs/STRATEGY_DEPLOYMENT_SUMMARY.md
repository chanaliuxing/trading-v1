# Strategy Deployment Summary - V7.2 Multi-Symbol Implementation

**Date:** 2025-10-08
**Status:** ✅ 8 Symbols Configured (Phase 1)
**Framework:** TSLASimplifiedV7_2 (100% JSON-Based)

---

## Implemented Strategies

### Tier 1: Mega-Cap Tech (Lower Volatility)
| Symbol | Config File | Volatility Profile | Key Parameters |
|--------|-------------|-------------------|----------------|
| **AAPL** | `aapl_v7_2.json` | Low-Medium | thr_sigma: 0.0045, k_atr: 1.8 |
| **MSFT** | `msft_v7_2.json` | Low | thr_sigma: 0.0042, k_atr: 1.7 |
| **GOOGL** | `googl_v7_2.json` | Low-Medium | thr_sigma: 0.0044, k_atr: 1.75 |
| **AMZN** | `amzn_v7_2.json` | Low-Medium | thr_sigma: 0.0048, k_atr: 1.85 |

### Tier 2: High Volatility Tech
| Symbol | Config File | Volatility Profile | Key Parameters |
|--------|-------------|-------------------|----------------|
| **NVDA** | `nvda_v7_2.json` | Very High | thr_sigma: 0.0110, k_atr: 2.2 |
| **AMD** | `amd_v7_2.json` | Very High | thr_sigma: 0.0125, k_atr: 2.4 |
| **TSLA** | `tsla_v7_2.json` | High | thr_sigma: 0.009, k_atr: 2.0 |

### Tier 3: Social Media & ETFs
| Symbol | Config File | Volatility Profile | Key Parameters |
|--------|-------------|-------------------|----------------|
| **META** | `meta_v7_2.json` | Medium | thr_sigma: 0.0085, k_atr: 1.95 |
| **QQQ** | `qqq_v7_2.json` | Low (ETF) | thr_sigma: 0.0035, k_atr: 1.6 |

---

## Parameter Calibration Approach

### Risk-Adjusted by Volatility
- **Low Vol (QQQ, MSFT, GOOGL):** Tighter thresholds, higher position sizing
- **Medium Vol (AAPL, AMZN, META):** Balanced parameters
- **High Vol (TSLA, NVDA, AMD):** Wider thresholds, lower position sizing

### Key Adjustments by Symbol

#### A1 (Mean Reversion)
| Symbol | thr_sigma | rvol_lo | rvol_hi | time_exit | Rationale |
|--------|-----------|---------|---------|-----------|-----------|
| QQQ | 0.0035 | 0.85 | 1.80 | 50 min | ETF - tight spreads, lower vol |
| MSFT | 0.0042 | 0.85 | 1.80 | 45 min | Stable mega-cap |
| AAPL | 0.0045 | 0.85 | 1.80 | 45 min | Liquid but moderate moves |
| AMZN | 0.0048 | 0.85 | 1.80 | 44 min | Cloud growth narrative |
| META | 0.0085 | 0.85 | 1.80 | 42 min | Social media sector vol |
| TSLA | 0.0090 | 0.85 | 1.80 | 40 min | EV sector, news-driven |
| NVDA | 0.0110 | 0.85 | 1.80 | 40 min | AI hype, high beta |
| AMD | 0.0125 | 0.85 | 1.80 | 38 min | Highest volatility |

#### A2 (Grid Accumulation)
| Symbol | grid_g | grid_g_floor | oneway_thr | Rationale |
|--------|--------|--------------|------------|-----------|
| QQQ | 0.85 | 0.65 | 3.0 | Smoothest trends |
| MSFT | 0.95 | 0.72 | 2.6 | Gradual moves |
| GOOGL | 0.98 | 0.73 | 2.7 | Similar to MSFT |
| AAPL | 1.00 | 0.75 | 2.5 | Balanced |
| AMZN | 1.05 | 0.76 | 2.4 | Moderate swings |
| META | 1.15 | 0.78 | 2.1 | Faster mean reversion |
| TSLA | 1.20 | 0.80 | 2.2 | Original calibration |
| NVDA | 1.35 | 0.85 | 2.0 | Rapid reversals |
| AMD | 1.45 | 0.90 | 1.9 | Most aggressive |

#### Positioning (Risk Management)
| Symbol | base_unit_pct | dd_soft_pct | dd_hard_pct | Rationale |
|--------|---------------|-------------|-------------|-----------|
| QQQ | 1.50% | 1.2% | 2.0% | Lowest risk (ETF) |
| GOOGL | 1.25% | 1.5% | 2.4% | Stable mega-cap |
| MSFT | 1.30% | 1.4% | 2.3% | Stable mega-cap |
| AAPL | 1.20% | 1.5% | 2.5% | Moderate risk |
| AMZN | 1.15% | 1.6% | 2.6% | Moderate risk |
| META | 1.10% | 1.8% | 2.9% | Higher volatility |
| TSLA | 1.20% | 1.8% | 2.8% | Original calibration |
| NVDA | 1.00% | 2.0% | 3.2% | High risk |
| AMD | 0.90% | 2.2% | 3.5% | Highest risk |

---

## File Structure

```
strategies/
├── configs/
│   ├── tsla_v7_2.json     ✅ Original
│   ├── aapl_v7_2.json     ✅ NEW
│   ├── nvda_v7_2.json     ✅ NEW
│   ├── msft_v7_2.json     ✅ NEW
│   ├── amd_v7_2.json      ✅ NEW
│   ├── meta_v7_2.json     ✅ NEW
│   ├── amzn_v7_2.json     ✅ NEW
│   ├── googl_v7_2.json    ✅ NEW
│   └── qqq_v7_2.json      ✅ NEW
│
├── LKG Files (Last Known Good):
│   ├── tsla_v7_2_lkg.json   ✅
│   ├── aapl_v7_2_lkg.json   ✅
│   ├── nvda_v7_2_lkg.json   ✅
│   ├── msft_v7_2_lkg.json   ✅
│   ├── amd_v7_2_lkg.json    ✅
│   ├── meta_v7_2_lkg.json   ✅
│   ├── amzn_v7_2_lkg.json   ✅
│   ├── googl_v7_2_lkg.json  ✅
│   └── qqq_v7_2_lkg.json    ✅
```

---

## Remaining Universe Symbols (Not Yet Configured)

### Lower Priority (Need Analysis)
| Symbol | Type | Notes |
|--------|------|-------|
| **SPY** | Market Index ETF | Reference only - used for market regime |
| **VIX** | Volatility Index | Reference only - used for pause conditions |
| **PLMR** | Small Cap | Palomar Holdings - low liquidity |
| **ELF** | Small Cap | e.l.f. Beauty - low liquidity |
| **ORCL** | Mega Cap | Oracle - slower mover |
| **INTC** | Large Cap | Intel - semiconductor (similar to AMD) |
| **TQQQ** | 3x Leveraged ETF | Ultra-high risk - needs special config |
| **SQQQ** | 3x Inverse ETF | Bearish instrument - incompatible |
| **VOO** | S&P 500 ETF | Low volatility - similar to SPY |

### Recommendation
- **Don't Trade:** SPY, VIX (reference only), SQQQ (bearish)
- **Special Config Needed:** TQQQ (3x leverage - requires different risk params)
- **Can Use Existing Configs:**
  - ORCL → copy MSFT config
  - INTC → copy AMD config (lower the params slightly)
  - VOO → copy QQQ config
  - ELF, PLMR → copy META config (increase pause thresholds for liquidity)

---

## Strategy Activation (Next Steps)

### Option 1: Test Individual Symbols
Start with one symbol at a time to validate configs:

```yaml
# config.yml
strategies:
  - id: "aapl_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    capital_limit_usd: 50000
    params:
      extras:
        config_file: "strategies/configs/aapl_v7_2.json"
```

### Option 2: Deploy All at Once
Deploy all 9 symbols with proportional capital allocation:

```yaml
strategies:
  - id: "tsla_v7_2_001"
    enabled: true
    capital_limit_usd: 60000  # 15% (highest vol)

  - id: "aapl_v7_2_001"
    enabled: true
    capital_limit_usd: 50000  # 12.5%

  - id: "nvda_v7_2_001"
    enabled: true
    capital_limit_usd: 45000  # 11.25% (high vol)

  # ... etc
```

### Capital Allocation Recommendation
Total capital: $400,000

| Symbol | Allocation | Capital | Rationale |
|--------|-----------|---------|-----------|
| QQQ | 15% | $60,000 | Lowest risk |
| MSFT | 13% | $52,000 | Very stable |
| AAPL | 13% | $52,000 | Liquid, moderate vol |
| GOOGL | 12% | $48,000 | Stable |
| AMZN | 12% | $48,000 | Moderate vol |
| TSLA | 11% | $44,000 | Higher vol |
| META | 10% | $40,000 | Medium-high vol |
| NVDA | 9% | $36,000 | High vol |
| AMD | 5% | $20,000 | Highest vol, lowest allocation |

---

## Validation Checklist

Before going live:

- [✅] All config files created with `use_config_values: true`
- [✅] All LKG files created with initial parameters
- [✅] Parameters calibrated based on volatility profiles
- [ ] Backtest each symbol with paper trading
- [ ] Verify data pipeline for all symbols (universe list)
- [ ] Monitor first week in shadow mode
- [ ] Adjust parameters based on live behavior

---

## Key Differences from TSLA

### More Conservative (QQQ, MSFT, GOOGL)
- Lower `thr_sigma` (tighter entry)
- Lower `k_atr` (smaller stops)
- Higher `base_unit_pct` (larger positions due to lower risk)
- Longer `time_exit_min` (let winners run)

### More Aggressive (NVDA, AMD)
- Higher `thr_sigma` (wider entry)
- Higher `k_atr` (wider stops)
- Lower `base_unit_pct` (smaller positions due to higher risk)
- Shorter `time_exit_min` (take profits faster)

---

## Success Metrics

Track per-symbol:
- Win rate (target: >55%)
- Average win/loss ratio (target: >1.5)
- Max drawdown (should stay within `dd_hard_pct`)
- Number of triggers per day (target: ~4)
- Sharpe ratio (target: >2.0)

---

## Notes

1. All configs use the **same strategy logic** (`TSLASimplifiedV7_2`)
2. Only **parameters differ** - this is the power of JSON-based config!
3. Set `premarket_calibration.use_config_values = true` to use these values
4. Set `false` if you want auto-calibration from historical data
5. Each symbol has its own LKG file for safety

**Status:** Ready for paper trading validation! 🚀
