# AI Brain Auto-Research Loop - Complete Example

This document demonstrates the complete end-to-end workflow of the AI Brain auto-research system.

## Overview

The AI Brain research loop consists of 4 steps:

```
┌─────────────────────────────────────────────────────────────┐
│                                                             │
│  1. Run HPO         2. Export Report    3. AI Analysis     │
│     ↓                   ↓                   ↓              │
│  Optuna DB  →  hpo_report.json  →  next_patch.json        │
│     ↑                                       ↓              │
│     └───────────────────────────────────────┘              │
│                4. Apply Patch & Repeat                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Step 1: Run Initial HPO

Start with a broad configuration to explore the space:

```bash
# Run 30 trials with wide search space
python alpha_loop.py \
  --config alpha_config_enhanced.json \
  --provider optuna \
  --gens 1 \
  --out_dir /tmp/hpo_gen0
```

**alpha_config_enhanced.json** (initial):
```json
{
  "label_profile": "intraday_reg_5m",
  "hpo": {
    "symbols": ["TSLA"],
    "start": "2024-01-01 09:30:00",
    "end": "2024-12-31 16:00:00",
    "interval_sec": 60,
    "strategy_path": "strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy"
  },
  "optuna": {
    "trials": 30,
    "study_name": "tsla_autoresearch_gen0",
    "storage": "sqlite:///alpha_optuna_autoresearch.db"
  },
  "optuna_space": {
    "model_type": {
      "type": "categorical",
      "choices": ["xgb", "ridge", "lasso", "ensemble2", "stacking"]
    },
    "max_depth": {"type": "int", "low": 2, "high": 10},
    "learning_rate": {"type": "float", "low": 0.01, "high": 0.2},
    "n_estimators": {"type": "int", "low": 50, "high": 300, "step": 50},
    "fs_method": {
      "type": "categorical",
      "choices": ["none", "lasso", "xgb_importance"]
    },
    "fs_max_features": {"type": "int", "low": 16, "high": 64, "step": 16}
  }
}
```

**Result after 30 trials:**
- Best Sharpe: 0.82
- Median Sharpe: 0.35
- Best IC: 0.142
- Completed trials in database: 30

---

## Step 2: Export HPO Report

Generate AI-readable report from Optuna database:

```bash
python export_hpo_report.py \
  --db alpha_optuna_autoresearch.db \
  --config alpha_config_enhanced.json \
  --output hpo_report_gen0.json \
  --top-k 5 \
  --worst-k 3 \
  --study-name tsla_autoresearch_gen0
```

**Output: hpo_report_gen0.json**

```json
{
  "version": "v1",
  "task_meta": {
    "symbol": "TSLA",
    "lookback_days": 365,
    "data_frequency": "60s",
    "created_at": "2025-01-15T14:23:45"
  },
  "base_config": {
    "space": {
      "grid": {
        "model_type": ["xgb", "ridge", "lasso", "ensemble2", "stacking"],
        "max_depth": [2, 3, 4, 5, 6, 7, 8, 9, 10],
        "learning_rate": [0.01, 0.05, 0.1, 0.15, 0.2],
        "fs_method": ["none", "lasso", "xgb_importance"]
      }
    },
    "label": "intraday_reg_5m",
    "loss": "sharpe_loss",
    "stacking": {
      "base_models": ["xgb", "ridge", "lasso"],
      "meta_model": "ridge",
      "n_folds": 3
    },
    "feature_selection": {
      "enabled": false,
      "method": "none"
    }
  },
  "hard_param_bounds": {
    "max_depth": {"min": 2, "max": 12},
    "learning_rate": {"min": 0.005, "max": 0.5},
    "stack_kfold": {"min": 2, "max": 6},
    "alpha": {"min": 0.0001, "max": 1.0},
    "n_estimators": {"min": 10, "max": 500}
  },
  "last_run_summary": {
    "n_trials": 30,
    "best_sharpe": 0.82,
    "median_sharpe": 0.35,
    "worst_sharpe": -0.15,
    "best_ic": 0.142,
    "ic_stability": 0.68,
    "avg_trades_per_day": 2.8,
    "max_drawdown": 0.092,
    "comments": ""
  },
  "top_trials": [
    {
      "trial_id": 23,
      "number": 23,
      "metrics": {
        "sharpe": 0.82,
        "ic": 0.142,
        "max_drawdown": 0.045,
        "n_trades": 112,
        "win_rate": 0.56
      },
      "params": {
        "model_type": "stacking",
        "stack_base_models": "xgb,ridge,lasso",
        "stack_meta_model": "ridge",
        "stack_kfold": 3,
        "max_depth": 4,
        "learning_rate": 0.05,
        "n_estimators": 150,
        "fs_method": "xgb_importance",
        "fs_max_features": 48
      }
    },
    {
      "trial_id": 18,
      "number": 18,
      "metrics": {
        "sharpe": 0.78,
        "ic": 0.138,
        "max_drawdown": 0.052,
        "n_trades": 98,
        "win_rate": 0.55
      },
      "params": {
        "model_type": "stacking",
        "stack_base_models": "xgb,ridge",
        "stack_kfold": 4,
        "max_depth": 5,
        "learning_rate": 0.05,
        "n_estimators": 150,
        "fs_method": "xgb_importance",
        "fs_max_features": 32
      }
    },
    {
      "trial_id": 27,
      "number": 27,
      "metrics": {
        "sharpe": 0.71,
        "ic": 0.129,
        "max_drawdown": 0.058,
        "n_trades": 105,
        "win_rate": 0.53
      },
      "params": {
        "model_type": "ensemble2",
        "base1_model_type": "xgb",
        "base2_model_type": "ridge",
        "w1": 0.6,
        "w2": 0.4,
        "max_depth": 4,
        "learning_rate": 0.05,
        "n_estimators": 200,
        "fs_method": "lasso",
        "fs_max_features": 48
      }
    },
    {
      "trial_id": 12,
      "number": 12,
      "metrics": {
        "sharpe": 0.65,
        "ic": 0.118,
        "max_drawdown": 0.068,
        "n_trades": 89,
        "win_rate": 0.52
      },
      "params": {
        "model_type": "xgb",
        "max_depth": 3,
        "learning_rate": 0.05,
        "n_estimators": 150,
        "fs_method": "xgb_importance",
        "fs_max_features": 32
      }
    },
    {
      "trial_id": 8,
      "number": 8,
      "metrics": {
        "sharpe": 0.58,
        "ic": 0.102,
        "max_drawdown": 0.075,
        "n_trades": 72,
        "win_rate": 0.51
      },
      "params": {
        "model_type": "ridge",
        "alpha": 0.01,
        "fs_method": "lasso",
        "fs_max_features": 32
      }
    }
  ],
  "worst_trials": [
    {
      "trial_id": 5,
      "number": 5,
      "metrics": {
        "sharpe": -0.15,
        "ic": -0.032,
        "max_drawdown": 0.152,
        "n_trades": 45,
        "win_rate": 0.42
      },
      "params": {
        "model_type": "xgb",
        "max_depth": 10,
        "learning_rate": 0.2,
        "n_estimators": 50,
        "fs_method": "none"
      }
    },
    {
      "trial_id": 14,
      "number": 14,
      "metrics": {
        "sharpe": 0.08,
        "ic": 0.018,
        "max_drawdown": 0.118,
        "n_trades": 38,
        "win_rate": 0.47
      },
      "params": {
        "model_type": "lasso",
        "alpha": 0.0001,
        "fs_method": "none"
      }
    },
    {
      "trial_id": 22,
      "number": 22,
      "metrics": {
        "sharpe": 0.12,
        "ic": 0.025,
        "max_drawdown": 0.095,
        "n_trades": 52,
        "win_rate": 0.48
      },
      "params": {
        "model_type": "xgb",
        "max_depth": 8,
        "learning_rate": 0.15,
        "n_estimators": 100,
        "fs_method": "none"
      }
    }
  ]
}
```

---

## Step 3: AI Brain Analysis

Feed the report to AI Brain (ChatGPT / DeepSeek / Claude) with the system prompt:

**System Message:**
```
[Content of prompts/ai_brain_hpo_prompt.md]
```

**User Message:**
```json
[Content of hpo_report_gen0.json]
```

**AI Brain Response:**

```json
{
  "version": "v1",
  "reasoning": "Stacking with 3+ base models dominates (Sharpe 0.82 vs 0.58 for single XGB). Top trials use depth 4-5, learning_rate 0.05, and XGB feature selection (32-48 features). Deep trees (8-10) without FS fail. Focusing on stacking with refined hyperparameters.",
  "confidence": 0.89,
  "changes": {
    "optuna_space.model_type.choices": ["stacking", "ensemble2"],
    "optuna_space.stack_base_models.choices": ["xgb,ridge,lasso", "xgb,ridge,elasticnet", "xgb,lasso,elasticnet"],
    "optuna_space.stack_kfold.low": 3,
    "optuna_space.stack_kfold.high": 5,
    "optuna_space.max_depth.low": 3,
    "optuna_space.max_depth.high": 5,
    "optuna_space.learning_rate.low": 0.03,
    "optuna_space.learning_rate.high": 0.08,
    "optuna_space.n_estimators.low": 100,
    "optuna_space.n_estimators.high": 200,
    "feature_selection.enabled": true,
    "feature_selection.method": "xgb_importance",
    "optuna_space.fs_max_features.low": 32,
    "optuna_space.fs_max_features.high": 48,
    "optuna.trials": 50
  }
}
```

Save this as `next_config_patch_gen1.json`.

---

## Step 4: Apply Patch and Iterate

Apply the AI Brain's suggested changes:

```bash
python apply_config_patch.py \
  --base alpha_config_enhanced.json \
  --patch next_config_patch_gen1.json \
  --output alpha_config_enhanced_gen1.json
```

**Output:**
```
[1/3] Loading base config from alpha_config_enhanced.json...
[2/3] Loading patch from next_config_patch_gen1.json...
[3/3] Applying patch...
[Reasoning] Stacking with 3+ base models dominates (Sharpe 0.82 vs 0.58 for single XGB). Top trials use depth 4-5, learning_rate 0.05, and XGB feature selection (32-48 features). Deep trees (8-10) without FS fail. Focusing on stacking with refined hyperparameters.

  ✓ optuna_space.model_type.choices = ['stacking', 'ensemble2']
  ✓ optuna_space.stack_base_models.choices = ['xgb,ridge,lasso', 'xgb,ridge,elasticnet', 'xgb,lasso,elasticnet']
  ✓ optuna_space.stack_kfold.low = 3
  ✓ optuna_space.stack_kfold.high = 5
  ✓ optuna_space.max_depth.low = 3
  ✓ optuna_space.max_depth.high = 5
  ✓ optuna_space.learning_rate.low = 0.03
  ✓ optuna_space.learning_rate.high = 0.08
  ✓ optuna_space.n_estimators.low = 100
  ✓ optuna_space.n_estimators.high = 200
  ✓ feature_selection.enabled = True
  ✓ feature_selection.method = xgb_importance
  ✓ optuna_space.fs_max_features.low = 32
  ✓ optuna_space.fs_max_features.high = 48
  ✓ optuna.trials = 50

✅ Config saved to: alpha_config_enhanced_gen1.json
   Applied 13 changes

📝 AI Brain Reasoning:
Stacking with 3+ base models dominates (Sharpe 0.82 vs 0.58 for single XGB). Top trials use depth 4-5, learning_rate 0.05, and XGB feature selection (32-48 features). Deep trees (8-10) without FS fail. Focusing on stacking with refined hyperparameters.
```

Now run Generation 1 with refined configuration:

```bash
python alpha_loop.py \
  --config alpha_config_enhanced_gen1.json \
  --provider optuna \
  --gens 1 \
  --out_dir /tmp/hpo_gen1
```

**Result after 50 trials:**
- Best Sharpe: 1.15 (↑ from 0.82)
- Median Sharpe: 0.78 (↑ from 0.35)
- Best IC: 0.168 (↑ from 0.142)

Continue the loop:

```bash
# Export Gen1 report
python export_hpo_report.py \
  --db alpha_optuna_autoresearch.db \
  --config alpha_config_enhanced_gen1.json \
  --output hpo_report_gen1.json

# Feed to AI Brain → get next_config_patch_gen2.json

# Apply and run Gen2
python apply_config_patch.py \
  --base alpha_config_enhanced_gen1.json \
  --patch next_config_patch_gen2.json \
  --output alpha_config_enhanced_gen2.json

python alpha_loop.py \
  --config alpha_config_enhanced_gen2.json \
  --provider optuna \
  --gens 1 \
  --out_dir /tmp/hpo_gen2
```

---

## Full Automation Script

Create a bash script to automate the entire loop:

**`run_ai_brain_loop.sh`:**

```bash
#!/bin/bash
# AI Brain Auto-Research Loop
# Usage: ./run_ai_brain_loop.sh <start_config> <max_generations> <llm_api_endpoint>

set -e

START_CONFIG="$1"
MAX_GENS="${2:-5}"
LLM_API="${3:-http://localhost:11434/v1/chat/completions}"  # Ollama default

echo "🧠 AI Brain Auto-Research Loop"
echo "   Start config: $START_CONFIG"
echo "   Max generations: $MAX_GENS"
echo "   LLM API: $LLM_API"

SYSTEM_PROMPT=$(cat prompts/ai_brain_hpo_prompt.md)
DB_PATH="alpha_optuna_autoresearch.db"

for gen in $(seq 0 $((MAX_GENS - 1))); do
    echo ""
    echo "════════════════════════════════════════════════"
    echo "  Generation $gen"
    echo "════════════════════════════════════════════════"

    # Step 1: Run HPO
    echo "[1/4] Running HPO..."
    if [ $gen -eq 0 ]; then
        CONFIG="$START_CONFIG"
    else
        CONFIG="alpha_config_enhanced_gen${gen}.json"
    fi

    python alpha_loop.py \
        --config "$CONFIG" \
        --provider optuna \
        --gens 1 \
        --out_dir "/tmp/hpo_gen${gen}"

    # Step 2: Export report
    echo "[2/4] Exporting HPO report..."
    python export_hpo_report.py \
        --db "$DB_PATH" \
        --config "$CONFIG" \
        --output "hpo_report_gen${gen}.json" \
        --top-k 5 \
        --worst-k 3

    # Step 3: Call AI Brain
    echo "[3/4] Calling AI Brain..."
    REPORT_JSON=$(cat "hpo_report_gen${gen}.json")

    curl -s "$LLM_API" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg sys "$SYSTEM_PROMPT" --arg usr "$REPORT_JSON" \
            '{model: "deepseek-r1:70b", messages: [{role: "system", content: $sys}, {role: "user", content: $usr}]}')" \
        | jq -r '.choices[0].message.content' \
        > "next_config_patch_gen$((gen + 1)).json"

    # Step 4: Apply patch
    echo "[4/4] Applying config patch..."
    python apply_config_patch.py \
        --base "$CONFIG" \
        --patch "next_config_patch_gen$((gen + 1)).json" \
        --output "alpha_config_enhanced_gen$((gen + 1)).json"

    echo "✅ Generation $gen complete"
done

echo ""
echo "🎉 All $MAX_GENS generations complete!"
echo "   Final config: alpha_config_enhanced_gen${MAX_GENS}.json"
```

**Usage:**

```bash
chmod +x run_ai_brain_loop.sh

# Run 5 generations with local Ollama
./run_ai_brain_loop.sh alpha_config_enhanced.json 5

# Or with OpenAI API
./run_ai_brain_loop.sh alpha_config_enhanced.json 5 https://api.openai.com/v1/chat/completions
```

---

## Expected Progression

**Generation 0 (Exploration):**
- Broad search space
- Best Sharpe: 0.82
- Identify: Stacking > single models

**Generation 1 (Refinement):**
- Narrow to stacking + ensemble2
- Constrain hyperparameters to successful ranges
- Best Sharpe: 1.15 (↑40%)

**Generation 2 (Optimization):**
- Fine-tune base model combinations
- Optimize k-fold and feature selection
- Best Sharpe: 1.32 (↑15%)

**Generation 3 (Exploitation):**
- Micro-tuning around best region
- Best Sharpe: 1.38 (↑5%)

**Generation 4 (Convergence):**
- Minimal improvement
- Best Sharpe: 1.40 (↑2%)
- AI Brain suggests stopping (confidence < 0.6)

---

## Monitoring and Diagnostics

### Check AI Brain Suggestions

```bash
# View all patches
ls -lh next_config_patch_gen*.json

# Compare successive patches
diff <(jq -S . next_config_patch_gen1.json) <(jq -S . next_config_patch_gen2.json)

# View AI reasoning history
jq -r '.reasoning' next_config_patch_gen*.json
```

### Track Performance Across Generations

```python
import sqlite3
import pandas as pd

conn = sqlite3.connect("alpha_optuna_autoresearch.db")
df = pd.read_sql_query("""
    SELECT
        s.study_name,
        t.number,
        t.value as sharpe
    FROM trials t
    JOIN studies s ON t.study_id = s.study_id
    WHERE t.state = 'COMPLETE'
    ORDER BY s.study_id, t.number
""", conn)

# Plot progression
import matplotlib.pyplot as plt
df.groupby("study_name")["sharpe"].max().plot(kind="bar")
plt.title("Best Sharpe per Generation")
plt.ylabel("Sharpe Ratio")
plt.show()
```

---

## Troubleshooting

### AI Brain Returns Invalid JSON

**Symptom:** `json.JSONDecodeError` when parsing AI response

**Fix:**
```bash
# Validate AI output
jq . next_config_patch_gen1.json

# If invalid, manually fix or regenerate with stricter prompt
```

### Config Patch Violates Bounds

**Symptom:** `ValueError: max_depth.high=15 exceeds hard_param_bounds.max_depth.max=12`

**Fix:** Add validation to `apply_config_patch.py`:
```python
def validate_bounds(changes, bounds):
    for path, value in changes.items():
        if "max_depth" in path and "high" in path:
            if value > bounds["max_depth"]["max"]:
                raise ValueError(f"Violation: {path}={value} > {bounds['max_depth']['max']}")
```

### Performance Plateaus

**Symptom:** No improvement after 2-3 generations

**Solution:**
- Increase `optuna.trials` in patch
- Introduce new model types (e.g., LightGBM)
- Check data quality (IC stability < 0.5 suggests noisy labels)

---

## Next Steps

1. **Run the example**: Test on your TSLA data
2. **Tune System Prompt**: Adjust reasoning style for your use case
3. **Add Model Types**: Extend to LightGBM, RuleFit, etc.
4. **Multi-Symbol**: Run loop for each symbol, compare
5. **Production Deploy**: Schedule daily auto-research runs

---

**Files Created:**
- `export_hpo_report.py` - HPO report generator
- `apply_config_patch.py` - Config patch applier
- `prompts/ai_brain_hpo_prompt.md` - System prompt for AI Brain
- `run_ai_brain_loop.sh` - Full automation script (optional)

**Documentation:**
- This file: `AI_BRAIN_LOOP_EXAMPLE.md`

---

**Author:** AI Research Team
**Date:** 2025-01-15
**Version:** 1.0
