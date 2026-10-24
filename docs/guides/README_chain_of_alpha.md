# Chain-of-Alpha: Iterative Strategy Optimization

Chain-of-Alpha is an automated hyperparameter optimization loop that uses LLM/ML agents to iteratively refine trading strategy parameters based on walk-forward backtest results.

## Overview

The system implements a feedback loop:
1. **Propose** parameter grids using LLM/ML/heuristics
2. **Execute** walk-forward HPO on proposed grids
3. **Analyze** in-sample and out-of-sample results
4. **Refine** grids based on top performers
5. **Iterate** for multiple generations

## Quick Start

### 1. Prerequisites

```bash
# Required
pip install clickhouse-driver numpy pandas pyarrow

# Optional for LLM
pip install openai python-dotenv

# Optional for ML surrogate
pip install scikit-learn

# Optional for Bayesian optimization
pip install optuna

# Optional for plots
pip install matplotlib
```

### 2. Environment Setup

If using LLM provider, create a `.env` file:

```bash
OPENAI_API_KEY=sk-your-api-key-here
```

### 3. Configuration

Create or modify `config/alpha_config_example.json`:

```json
{
  "hpo": {
    "symbols": ["TSLA"],
    "start": "2024-11-01 09:30:00",
    "end": "2024-11-15 16:00:00",
    "strategy_path": "strategies.tsla_v7_2.TSLASimplifiedV7_2",
    "grid": {
      "rsi_period": [10, 14, 20],
      "rsi_oversold": [25, 30, 35],
      "atr_mult_sl": [1.5, 2.0, 2.5]
    },
    "objective": {
      "metric": "sharpe",
      "maximize": true,
      "min_trades": 10,
      "max_mdd": 0.35
    }
  },
  "llm": {
    "model": "gpt-4o-mini"
  }
}
```

### 4. Run Chain-of-Alpha

```bash
# With LLM provider (requires OPENAI_API_KEY)
python alpha_loop.py \
  --config config/alpha_config_example.json \
  --out_dir ./alpha_out \
  --gens 3 \
  --k-top 8 \
  --provider llm

# With ML surrogate (requires scikit-learn)
python alpha_loop.py \
  --config config/alpha_config_example.json \
  --out_dir ./alpha_out \
  --gens 3 \
  --k-top 8 \
  --provider ml

# Hybrid mode (tries LLM first, falls back to ML)
python alpha_loop.py \
  --config config/alpha_config_example.json \
  --out_dir ./alpha_out \
  --gens 3 \
  --k-top 8 \
  --provider hybrid

# Heuristic only (no external dependencies)
python alpha_loop.py \
  --config config/alpha_config_example.json \
  --out_dir ./alpha_out \
  --gens 3 \
  --k-top 8 \
  --provider none

# Optuna Bayesian optimization (requires optuna)
python alpha_loop.py \
  --config config/alpha_config_example.json \
  --out_dir ./alpha_out \
  --gens 1 \
  --provider optuna
```

## Command-Line Arguments

- `--config`: Path to alpha loop configuration JSON (required)
- `--out_dir`: Output directory for generation artifacts (default: `./alpha_out`)
- `--gens`: Number of optimization generations (default: 3)
- `--k-top`: Top K in-sample results to guide refinement for non-Optuna providers (default: 8)
- `--provider`: Grid proposal method - `llm`, `ml`, `hybrid`, `optuna`, or `none` (default: `hybrid`)

## Output Structure

After running, the output directory will contain:

```
alpha_out/
├── gen_01/
│   ├── gen_summary.json          # Full generation results
│   ├── rows.csv                  # All IS/OOS trials
│   ├── leaderboard_is.csv        # IS rankings
│   ├── leaderboard_oos.csv       # OOS rankings
│   ├── oos_summary.json          # OOS aggregate metrics
│   ├── best_params_by_fold.json  # Best params per fold
│   └── champion_params.csv       # Parameter frequency table
├── gen_02/
│   ├── refine.json               # LLM/ML refinement rationale
│   ├── gen_summary.json
│   └── ...
├── gen_03/
│   └── ...
└── index.json                    # Summary across all generations
```

## Providers

### Optuna Provider
Uses Bayesian optimization (TPE or CMA-ES) to efficiently search continuous and discrete parameter spaces.

**Requirements:**
- `pip install optuna`

**Advantages:**
- Efficient exploration of continuous/discrete parameter spaces
- Smart sampling using Bayesian optimization (TPE/CMA-ES)
- Automatically infers search space from grid or uses explicit definition
- No manual grid design needed
- Works well for high-dimensional spaces

**How it works:**
- Each trial runs a full Walk-Forward backtest with a single parameter set
- Optuna learns from past trials to suggest promising parameters
- Optimizes on IS or OOS target (recommended: IS to avoid leakage)
- Supports int, float, and categorical parameters

**Configuration:**
```json
{
  "optuna": {
    "trials": 50,           // Number of optimization trials
    "target": "is",         // Optimize on "is" or "oos" (recommend "is")
    "direction": "maximize", // "maximize" or "minimize"
    "sampler": "tpe",       // "tpe" or "cmaes"
    "widen": 0.10           // Expand grid bounds by ±10% when auto-inferring
  },
  "hpo": {
    "grid": {
      "rsi_period": [10, 14, 20],  // Auto-inferred: int [9, 22]
      "atr_mult": [1.5, 2.0, 2.5]  // Auto-inferred: float [1.35, 2.75]
    },
    // Optional: explicit space definition (overrides auto-inference)
    "optuna_space": {
      "rsi_period": {"type": "int", "low": 8, "high": 25, "step": 1},
      "atr_mult": {"type": "float", "low": 1.0, "high": 3.0, "log": false}
    }
  }
}
```

### LLM Provider
Uses OpenAI API to analyze top performers and propose refined grids.

**Requirements:**
- `pip install openai python-dotenv`
- `.env` file with `OPENAI_API_KEY`

**Advantages:**
- Contextual understanding of strategy behavior
- Can make non-obvious parameter connections
- Generates human-readable rationale

### ML Provider
Uses RandomForest surrogate model to predict promising parameter regions.

**Requirements:**
- `pip install scikit-learn`

**Advantages:**
- Fast and deterministic
- No API costs
- Good for numeric parameter spaces

### Hybrid Provider
Tries LLM first, falls back to ML if LLM unavailable.

**Advantages:**
- Best of both worlds
- Graceful degradation

### None (Heuristic)
Uses median-centered expansion of top performers.

**Advantages:**
- No external dependencies
- Always available
- Simple and transparent

## Configuration Details

### HPO Section
Standard walk-forward HPO configuration (same as `wf_hpo.py`).

See `config/hpo_config_example.json` for full schema.

### LLM Section
```json
{
  "llm": {
    "model": "gpt-4o-mini"  // OpenAI model name
  }
}
```

### ML Section
```json
{
  "ml": {
    "n_points": 16  // Number of candidate points to generate
  }
}
```

### Heuristic Section
```json
{
  "heuristic": {
    "widen": 0.2  // Expansion scale (0.2 = ±20% around median)
  }
}
```

### Optuna Section
```json
{
  "optuna": {
    "trials": 50,            // Number of Bayesian optimization trials
    "target": "is",          // Optimize on "is" (in-sample) or "oos" (out-of-sample)
    "direction": "maximize", // "maximize" or "minimize"
    "sampler": "tpe",        // "tpe" (Tree-structured Parzen Estimator) or "cmaes"
    "widen": 0.10            // Auto-inference: expand grid bounds by ±10%
  }
}
```

## Example Workflow

### Generation 1
- Uses initial grid from config
- Runs full walk-forward HPO
- Identifies top 8 IS performers

### Generation 2
- LLM analyzes top 8 results
- Proposes refined grid focusing on promising regions
- Example: If top performers had `rsi_period=14`, new grid might be `[12, 14, 16]`
- Runs HPO on refined grid

### Generation 3
- Analyzes Gen 2 results
- Further narrows parameter space
- Runs final HPO

### Analysis
- Compare `oos_summary.json` across generations
- Check if OOS performance improved
- Review `refine.json` for LLM reasoning
- Examine `champion_params.csv` for parameter convergence

## Advanced Usage

### Custom Objective Functions

Use custom formulas in the objective config:

```json
{
  "objective": {
    "formula": "0.7*sharpe + 0.3*sortino - 2.0*max_drawdown",
    "maximize": true,
    "min_trades": 10,
    "max_mdd": 0.35
  }
}
```

### Multi-Symbol Optimization

```json
{
  "hpo": {
    "symbols": ["TSLA", "AAPL", "NVDA"],
    ...
  }
}
```

### Extended Metrics

Available metrics: `sharpe`, `sortino`, `omega`, `calmar`, `total_return`, `max_drawdown`, `profit_factor`, `win_rate`

## Troubleshooting

### LLM Provider Not Working
- Check `.env` file exists with valid `OPENAI_API_KEY`
- Verify `openai` package installed: `pip install openai`
- Check network connectivity
- Review logs in `refine.json` for error messages

### ML Provider Not Working
- Install scikit-learn: `pip install scikit-learn`
- Ensure sufficient data (at least 10 IS trials)

### Heuristic Fallback
If both LLM and ML fail, the system automatically falls back to heuristic median-centered expansion.

### No Improvement Across Generations
- Increase `--gens` for more iterations
- Adjust `--k-top` to use more/fewer top results
- Expand initial grid to cover wider parameter space
- Check if objective constraints are too restrictive

## Integration with HPO CLI

Chain-of-Alpha internally uses `wf_hpo.WalkForwardHPO`. You can also run individual generations manually:

```bash
# Extract a generation's config
python -c "import json; cfg=json.load(open('alpha_out/gen_02/gen_summary.json')); json.dump(cfg['hpo_cfg'], open('manual_hpo.json','w'), indent=2)"

# Run HPO manually
python hpo_cli.py --config manual_hpo.json --out_dir ./manual_out --plots --print-summary
```

## Next Steps

1. **Tune Initial Grid**: Start with a broad grid covering diverse parameter regions
2. **Set Appropriate Constraints**: Use `min_trades` and `max_mdd` to filter unrealistic strategies
3. **Run Multiple Generations**: Typically 3-5 generations show meaningful convergence
4. **Validate OOS Results**: Focus on `oos_summary.json` - IS performance can overfit
5. **Compare Providers**: Try different providers and compare OOS results
6. **Deploy Best Strategy**: Use parameters from best OOS fold for live trading

## References

- Walk-Forward HPO: See `doc/backtest/IMPLEMENTATION_2.md`
- Backtest Engine: See `doc/backtest/IMPLEMENTATION_1.md`
- Strategy API: See example strategies in `strategies/` folder
