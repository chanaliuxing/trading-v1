#!/usr/bin/env bash
set -euo pipefail

# Sequentially run EnhancedModelSignalStrategy HPO with exactly one model per run.
# Models: xgb, lstm, transformer, etsformer, itransformer
# Usage:
#   scripts/run_enhanced_model_sweep.sh [CONFIG_JSON] [OUT_DIR]
# Example:
#   scripts/run_enhanced_model_sweep.sh config/alpha_config_enhanced_debug.json alpha_out/enhanced_sweep

CONFIG=${1:-config/alpha_config_enhanced_debug.json}
OUT_ROOT=${2:-alpha_out/enhanced_sweep}
MODELS=(xgb lstm transformer etsformer itransformer)

mkdir -p "$OUT_ROOT"

patch_config() {
  local base_cfg="$1"; local model="$2"; local out_cfg="$3"
  python - "$base_cfg" "$model" "$out_cfg" << 'PY'
import json, sys
cfg_path, model, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(cfg_path, 'r', encoding='utf-8') as f:
    cfg = json.load(f)

# Ensure HPO keys exist
hpo = cfg.get('hpo') or {}
cfg['hpo'] = hpo
optuna = cfg.get('optuna') or {}
cfg['optuna'] = optuna

# Force single trial and in-memory storage for speed
optuna['trials'] = 1
optuna['storage'] = 'sqlite:///:memory:'

# Force single worker
par = hpo.get('parallel') or {}
par['workers'] = 1
hpo['parallel'] = par

# Restrict model_type to a single choice
space = hpo.get('optuna_space') or {}
mt = space.get('model_type') or {}
mt['type'] = 'categorical'
mt['choices'] = [model]
space['model_type'] = mt

# Filter model_params choices by model family
choices = (space.get('model_params') or {}).get('choices') or []
xgb_keys = {"eta","learning_rate","max_depth","min_child_weight","subsample","colsample_bytree","gamma","lambda","alpha","reg_lambda","reg_alpha","n_estimators","num_boost_round","early_stopping_rounds"}
seq_keys = {"lookback","hidden","hidden_dim","num_layers","dropout","pooling","bidirectional","use_attention","layer_norm","batch_size","epochs","patience","lr","weight_decay","device","d_model","nhead","dim_feedforward","alpha_init"}

def family(d):
    ks = set(d.keys())
    if ks & xgb_keys: return 'xgb'
    if ks & seq_keys: return 'seq'
    return 'other'

if choices:
    if model in ('xgb','xgboost'):
        choices = [c for c in choices if family(c) == 'xgb'] or [choices[0]]
    else:
        choices = [c for c in choices if family(c) == 'seq'] or [choices[0]]
    space['model_params'] = {'type':'categorical','choices':choices}

hpo['optuna_space'] = space

with open(out_path, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print(out_path)
PY
}

summarize_one() {
  local out_dir="$1"; local model="$2"
  python - "$out_dir" "$model" << 'PY'
import json, sys, os
out_dir, model = sys.argv[1], sys.argv[2]
g = os.path.join(out_dir, 'gen_01')
sum_path = os.path.join(g, 'oos_summary.json')
best_path = os.path.join(g, 'best_params_by_fold.json')
oos = {}
try:
    oos = json.load(open(sum_path,'r'))
except Exception:
    pass
best_model = model
try:
    arr = json.load(open(best_path,'r'))
    if isinstance(arr, list) and arr:
        p = arr[0].get('params') or {}
        best_model = p.get('model_type', model)
except Exception:
    pass
print(json.dumps({
    'model_run': model,
    'best_model_type': best_model,
    'oos': {
        'sharpe': float(oos.get('oos_avg_sharpe', 0.0)),
        'total_return': float(oos.get('oos_avg_total_return', 0.0)),
        'max_drawdown': float(oos.get('oos_avg_max_drawdown', 0.0)),
        'trades': int(oos.get('oos_trades', 0))
    },
    'paths': {'summary': sum_path, 'best_params': best_path}
}, ensure_ascii=False))
PY
}

echo "Running Enhanced model sweep sequentially…"
for m in "${MODELS[@]}"; do
  TMP_CFG="$OUT_ROOT/tmp_${m}.json"
  OUT_DIR="$OUT_ROOT/${m}"
  mkdir -p "$OUT_DIR"
  echo "\n==> Preparing config for model: $m"
  patch_config "$CONFIG" "$m" "$TMP_CFG" >/dev/null
  echo "==> Running: $m"
  PYTHONUNBUFFERED=1 DISABLE_EVENT_BUS=1 PYTHONPATH=$PWD \
    venv/bin/python -X faulthandler alpha_loop.py \
      --config "$TMP_CFG" --gens 1 --provider optuna --out_dir "$OUT_DIR"
  echo "==> Result:"
  summarize_one "$OUT_DIR" "$m"
done

echo "\nDone. See per-model outputs under: $OUT_ROOT/<model>/gen_01/"

