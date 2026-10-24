# AI Brain Auto-Research Loop - Quick Start Guide

## TL;DR

A 4-step closed loop that automatically evolves your HPO configuration:

```bash
# 1. Run HPO (30 trials)
python alpha_loop.py --config config.json --provider optuna --gens 1

# 2. Export report for AI
python export_hpo_report.py --db study.db --config config.json --output report.json

# 3. Feed to AI Brain (ChatGPT/DeepSeek/Claude) → get patch.json

# 4. Apply patch and repeat
python apply_config_patch.py --base config.json --patch patch.json --output config_next.json
```

---

## Files You Need

| File | Purpose | Location |
|------|---------|----------|
| `export_hpo_report.py` | Generate AI-readable HPO report | Root directory |
| `apply_config_patch.py` | Apply AI's config changes | Root directory |
| `prompts/ai_brain_hpo_prompt.md` | System prompt for AI Brain | prompts/ |
| `AI_BRAIN_LOOP_EXAMPLE.md` | Complete walkthrough | Root directory |
| This file | Quick reference | Root directory |

---

## 5-Minute Test

### Step 1: Run Mini HPO (3 trials, ~2 minutes)

```bash
# Create test config
cat > /tmp/test_ai_brain.json <<EOF
{
  "label_profile": "intraday_reg_5m",
  "hpo": {
    "symbols": ["TSLA"],
    "start": "2025-10-20 09:30:00",
    "end": "2025-11-10 16:00:00",
    "interval_sec": 60,
    "clickhouse": {"host": "localhost", "port": 9000, "database": "trading"},
    "features_table": "trading.features_1m",
    "strategy_path": "strategies.enhanced_model_signal_strategy.EnhancedModelSignalStrategy",
    "strategy_base_extras": {
      "symbols": ["TSLA"],
      "bar_sec": 60,
      "ensemble": {"mode": "weighted_avg", "models": [{"name": "ml", "weight": 1.0}]},
      "sizing": {"mode": "fixed", "base_qty": 100}
    },
    "fold_generator": {"start": "2025-10-20 09:30:00", "end": "2025-11-10 16:00:00", "is_days": 15, "oos_days": 3}
  },
  "optuna": {
    "trials": 3,
    "study_name": "ai_brain_test",
    "storage": "sqlite:///alpha_optuna_ai_brain_test.db"
  },
  "optuna_space": {
    "model_type": {"type": "categorical", "choices": ["xgb", "stacking"]},
    "stack_base_models": {"type": "categorical", "choices": ["xgb,ridge", "xgb,lasso"]},
    "max_depth": {"type": "int", "low": 3, "high": 6},
    "learning_rate": {"type": "float", "low": 0.03, "high": 0.1},
    "fs_method": {"type": "categorical", "choices": ["none", "xgb_importance"]}
  }
}
EOF

# Run HPO
python alpha_loop.py --config /tmp/test_ai_brain.json --provider optuna --gens 1 --out_dir /tmp/ai_brain_test
```

### Step 2: Export Report (~5 seconds)

```bash
python export_hpo_report.py \
  --db alpha_optuna_ai_brain_test.db \
  --config /tmp/test_ai_brain.json \
  --output /tmp/hpo_report_test.json \
  --top-k 2 \
  --worst-k 1
```

**Check output:**
```bash
cat /tmp/hpo_report_test.json | jq .
```

You should see:
- `task_meta` (symbol, lookback, frequency)
- `base_config` (current space)
- `last_run_summary` (best/median/worst Sharpe)
- `top_trials` (2 best trials with params)
- `worst_trials` (1 worst trial)

### Step 3: Test AI Brain Prompt (~30 seconds)

**Option A: Using ChatGPT Web**

1. Open ChatGPT
2. Paste contents of `prompts/ai_brain_hpo_prompt.md` as system message
3. Paste contents of `/tmp/hpo_report_test.json` as user message
4. Copy AI's JSON output to `/tmp/patch_test.json`

**Option B: Using Local Ollama**

```bash
# Install Ollama with DeepSeek-R1
ollama pull deepseek-r1:70b

# Call AI Brain
SYSTEM_PROMPT=$(cat prompts/ai_brain_hpo_prompt.md)
REPORT=$(cat /tmp/hpo_report_test.json)

curl -s http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg sys "$SYSTEM_PROMPT" --arg usr "$REPORT" \
    '{model: "deepseek-r1:70b", messages: [{role: "system", content: $sys}, {role: "user", content: $usr}]}')" \
  | jq -r '.choices[0].message.content' \
  > /tmp/patch_test.json
```

**Expected output format:**
```json
{
  "version": "v1",
  "reasoning": "Stacking with XGB+ridge outperforms single XGB. Narrowing to stacking-only with refined hyperparameters.",
  "confidence": 0.85,
  "changes": {
    "optuna_space.model_type.choices": ["stacking"],
    "optuna_space.max_depth.low": 4,
    "optuna_space.max_depth.high": 5
  }
}
```

### Step 4: Apply Patch (~1 second)

```bash
python apply_config_patch.py \
  --base /tmp/test_ai_brain.json \
  --patch /tmp/patch_test.json \
  --output /tmp/test_ai_brain_gen1.json
```

**Expected output:**
```
[1/3] Loading base config from /tmp/test_ai_brain.json...
[2/3] Loading patch from /tmp/patch_test.json...
[3/3] Applying patch...
[Reasoning] Stacking with XGB+ridge outperforms single XGB. Narrowing to stacking-only with refined hyperparameters.

  ✓ optuna_space.model_type.choices = ['stacking']
  ✓ optuna_space.max_depth.low = 4
  ✓ optuna_space.max_depth.high = 5

✅ Config saved to: /tmp/test_ai_brain_gen1.json
   Applied 3 changes
```

**Verify changes:**
```bash
diff <(jq -S .optuna_space /tmp/test_ai_brain.json) <(jq -S .optuna_space /tmp/test_ai_brain_gen1.json)
```

### Step 5: Run Generation 1

```bash
python alpha_loop.py --config /tmp/test_ai_brain_gen1.json --provider optuna --gens 1 --out_dir /tmp/ai_brain_test_gen1
```

---

## Production Workflow

### Initial Setup (Once)

```bash
# 1. Create base config with WIDE search space
cp alpha_config_enhanced.json alpha_config_autoresearch_base.json

# Edit optuna_space to be broad:
# - model_type: ["xgb", "ridge", "lasso", "ensemble2", "stacking"]
# - max_depth: [2, 10]
# - learning_rate: [0.01, 0.2]
# - fs_method: ["none", "lasso", "xgb_importance"]

# 2. Set high trial count
#   "trials": 50
```

### Daily Research Loop

```bash
#!/bin/bash
# daily_ai_research.sh

DATE=$(date +%Y%m%d)
GEN=0

while [ $GEN -lt 5 ]; do
    echo "🧠 Generation $GEN - $(date)"

    # Step 1: Run HPO
    python alpha_loop.py \
        --config "alpha_config_gen${GEN}.json" \
        --provider optuna \
        --gens 1 \
        --out_dir "/tmp/hpo_${DATE}_gen${GEN}"

    # Step 2: Export report
    python export_hpo_report.py \
        --db alpha_optuna_autoresearch.db \
        --config "alpha_config_gen${GEN}.json" \
        --output "hpo_report_${DATE}_gen${GEN}.json" \
        --top-k 5 \
        --worst-k 3

    # Step 3: Call AI Brain (requires API key or local Ollama)
    SYSTEM_PROMPT=$(cat prompts/ai_brain_hpo_prompt.md)
    REPORT=$(cat "hpo_report_${DATE}_gen${GEN}.json")

    # Using OpenAI API
    curl -s https://api.openai.com/v1/chat/completions \
        -H "Authorization: Bearer $OPENAI_API_KEY" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg sys "$SYSTEM_PROMPT" --arg usr "$REPORT" \
            '{model: "gpt-4", messages: [{role: "system", content: $sys}, {role: "user", content: $usr}]}')" \
        | jq -r '.choices[0].message.content' \
        > "patch_${DATE}_gen$((GEN + 1)).json"

    # Check confidence
    CONFIDENCE=$(jq -r '.confidence' "patch_${DATE}_gen$((GEN + 1)).json")
    if (( $(echo "$CONFIDENCE < 0.6" | bc -l) )); then
        echo "⚠️  Low confidence ($CONFIDENCE). Stopping loop."
        break
    fi

    # Step 4: Apply patch
    python apply_config_patch.py \
        --base "alpha_config_gen${GEN}.json" \
        --patch "patch_${DATE}_gen$((GEN + 1)).json" \
        --output "alpha_config_gen$((GEN + 1)).json"

    GEN=$((GEN + 1))
done

echo "✅ Research loop complete after $GEN generations"
```

Schedule daily:
```bash
crontab -e
# Run at 6 AM daily
0 6 * * * /path/to/daily_ai_research.sh >> /var/log/ai_research.log 2>&1
```

---

## Key Concepts

### What AI Brain Analyzes

1. **Model Architecture Patterns**
   - Stacking vs Ensemble2 vs Single models
   - Best base model combinations (XGB+Ridge vs XGB+Lasso)

2. **Hyperparameter Sweet Spots**
   - max_depth: Shallow (3-5) vs Deep (8-10)
   - learning_rate: Fast (>0.1) vs Slow (<0.05)
   - Feature count: 16-32 (aggressive) vs 48-64 (conservative)

3. **Failure Modes**
   - Overfitting: Deep trees + no feature selection
   - Underfitting: Too simple models + high learning rate

4. **Stability Signals**
   - IC stability > 0.8: Good generalization
   - IC stability < 0.5: Noisy data or overfitting

### What AI Brain Suggests

**Narrow successful regions:**
```json
"optuna_space.max_depth.low": 4,
"optuna_space.max_depth.high": 5
```

**Expand under-explored areas:**
```json
"optuna.trials": 100
```

**Enable/disable features:**
```json
"feature_selection.enabled": true
```

**Change model focus:**
```json
"optuna_space.model_type.choices": ["stacking"]
```

---

## Troubleshooting

### Error: AI returns non-JSON text

**Symptom:** Response starts with "Based on the analysis..."

**Fix:** Add to prompt:
```
IMPORTANT: Your response must ONLY be valid JSON. No explanatory text before or after.
```

### Error: Patch violates hard_param_bounds

**Symptom:** `ValueError: max_depth.high=15 exceeds hard_param_bounds.max=12`

**Fix:** AI Brain ignored bounds. Regenerate with clearer prompt or manually fix patch.

### Warning: Performance plateau

**Symptom:** No improvement after 2-3 generations

**Solutions:**
- Check AI confidence (should drop below 0.6)
- Increase trial count: `"optuna.trials": 100`
- Expand search: Add new model types or hyperparameters

### Error: Empty changes dict

**Symptom:** AI returns `{"changes": {}}`

**Meaning:** AI found no clear pattern. Either:
- Too few trials (increase to 30+)
- All trials performed similarly (data issue?)
- Search space already optimal

---

## Advanced Usage

### Multi-Symbol Research

Run separate loops for each symbol:

```bash
for symbol in TSLA AAPL NVDA; do
    sed "s/TSLA/$symbol/g" base_config.json > config_${symbol}.json
    python alpha_loop.py --config config_${symbol}.json ...
done
```

### Custom Reasoning Logic

Edit `prompts/ai_brain_hpo_prompt.md` to add domain knowledge:

```markdown
## Special Rules for High-Frequency Trading

If `data_frequency` is < 5s:
- Prefer shallow trees (max_depth ≤ 4) for speed
- Disable feature selection (adds latency)
- Prioritize n_estimators < 100
```

### Track Research History

```bash
# Save all configs
mkdir -p research_history/$DATE
cp alpha_config_gen*.json research_history/$DATE/
cp hpo_report_*.json research_history/$DATE/
cp patch_*.json research_history/$DATE/

# Compare best Sharpe across dates
grep "best_sharpe" research_history/*/hpo_report_*.json | sort
```

---

## FAQ

**Q: How many trials for first generation?**
A: 30-50 trials to get diverse patterns. AI needs both good and bad examples.

**Q: How many generations before convergence?**
A: Usually 3-5. Stop when confidence < 0.6 or improvement < 5%.

**Q: Can I use with other HPO tools (Grid Search, Random)?**
A: Yes, as long as you can export a similar JSON report with top/worst trials.

**Q: What if AI suggests invalid config?**
A: Use `--dry-run` flag to preview changes before applying. Add validation to `apply_config_patch.py`.

**Q: Cost of using GPT-4 API?**
A: ~$0.03-0.10 per generation (depends on report size). Use cheaper models like GPT-3.5 or local Ollama.

---

## Next Steps

1. **Run 5-minute test above** to verify setup
2. **Read full example** in `AI_BRAIN_LOOP_EXAMPLE.md`
3. **Customize system prompt** for your trading style
4. **Schedule daily runs** with cron

---

**Related Documents:**
- `AI_BRAIN_LOOP_EXAMPLE.md` - Complete walkthrough with sample outputs
- `export_hpo_report.py` - Report generator script
- `apply_config_patch.py` - Patch applier script
- `prompts/ai_brain_hpo_prompt.md` - AI Brain system prompt

**Version:** 1.0
**Date:** 2025-01-15
