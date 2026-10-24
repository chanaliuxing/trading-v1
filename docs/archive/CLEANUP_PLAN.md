# Repository Cleanup Plan

**Date:** 2025-12-11
**Status:** READY FOR EXECUTION

## Current State

### Issues Identified:
1. ✅ 90 Python scripts in root directory
2. ✅ 2 SQL files in root directory (`fix_bars_1m_ibkr_schema*.sql`)
3. ⚠️ Missing comprehensive .gitignore rules
4. ⚠️ Potential large binary files (*.pth, *.parquet, *.pkl)

---

## .gitignore Updates

### ✅ COMPLETED
Added the following patterns to `.gitignore`:

```gitignore
# Large binary and data files
*.zip
*.tar
*.tar.gz
*.tgz
*.rar
*.7z
*.pth
*.pt
*.h5
*.hdf5
*.parquet
*.feather
*.pkl
*.pickle
*.joblib

# Database files and backups
*.sql
*.backup
*.dump
*.bak.sql
*.sqlite
*.sqlite3-journal
*.db-journal

# Model checkpoints and weights
models/
checkpoints/
*.ckpt
*.safetensors

# Large CSV and data files
data/*.csv
data/*.parquet
data/*.feather
data/raw/
data/processed/
data/cache/
```

---

## File Organization Recommendations

### Root Directory Scripts (90 files)

**Category 1: Core System Entry Points (Keep in root)**
These are legitimate entry points that should stay:
- ✅ `run_system.py` - Main production entry point
- ✅ `wf_hpo.py` - Walk-forward HPO engine
- ✅ `backtest_ml_engine.py` - ML ensemble backtesting

**Category 2: Alpha/HPO Scripts (Move to scripts/ml/ or scripts/hpo/)**
```
alpha_loop.py              → scripts/ml/alpha_loop.py
hpo_cli.py                 → scripts/hpo/hpo_cli.py
hpo_cli_upgraded.py        → scripts/hpo/hpo_cli_upgraded.py
hpo_backends.py            → scripts/hpo/hpo_backends.py
compare_hpo_params.py      → scripts/hpo/compare_params.py
apply_hpo_params.py        → scripts/hpo/apply_params.py
prove_hpo_improvement.py   → scripts/hpo/prove_improvement.py
export_hpo_report.py       → scripts/hpo/export_report.py
```

**Category 3: Backtest Scripts (Move to scripts/backtest/)**
```
run_backtest_batch.py          → scripts/backtest/run_batch.py
run_backtest_batch_relaxed.py  → scripts/backtest/run_batch_relaxed.py
run_backtest_simple.py         → scripts/backtest/run_simple.py
run_backtest_reve.py           → scripts/backtest/run_reve.py
run_backtest_with_viz.py       → scripts/backtest/run_with_viz.py
plot_backtest.py               → scripts/backtest/plot_results.py
debug_backtest.py              → scripts/backtest/debug.py
```

**Category 4: Model Scripts (Move to scripts/ml/)**
```
models_ensemble.py      → scripts/ml/models_ensemble.py
models_linear.py        → scripts/ml/models_linear.py
leakage_guard.py        → scripts/ml/leakage_guard.py
grid_guard.py           → scripts/ml/grid_guard.py
```

**Category 5: Configuration/Setup (Move to scripts/admin/)**
```
apply_config_patch.py      → scripts/admin/apply_config_patch.py
apply_extras_params.py     → scripts/admin/apply_extras_params.py
boot_logging.py            → scripts/admin/boot_logging.py
relax_strategies_for_testing.py → scripts/admin/relax_strategies.py
```

**Category 6: Debug/Development Tools (Move to tools/)**
```
demo_system.py              → tools/demo_system.py
demo.py                     → tools/demo.py
debug_vwap_signals.py       → tools/debug_vwap_signals.py
diagnose_predictions.py     → tools/diagnose_predictions.py
benchmark_speed.py          → tools/benchmark_speed.py
quick_system_validation.py  → tools/quick_validation.py
audit_implementation.py     → tools/audit_implementation.py
```

**Category 7: Mock/Test Utilities (Move to tests/utils/)**
```
generate_mock_backtest.py   → tests/utils/generate_mock_backtest.py
mock_data_injector.py       → tests/utils/mock_data_injector.py
minimal_hpo_test.py         → tests/utils/minimal_hpo_test.py
hot_reload_test_params.py   → tests/utils/hot_reload_test_params.py
```

**Category 8: Deprecated/Unused (Archive or delete)**
Review and potentially remove:
```
premarket_calibrator.py     # Check if used
portfolio_overlay.py        # Check if used
llm_client.py              # Likely superseded by libs/llm/
metrics_extra.py           # Check if used
paper_engine.py            # Check if used
```

### SQL Files in Root
```
fix_bars_1m_ibkr_schema.sql       → migrations/fix_bars_1m_ibkr_schema.sql
fix_bars_1m_ibkr_schema_final.sql → migrations/fix_bars_1m_ibkr_schema_final.sql
```

---

## Cleanup Script

Create `scripts/admin/cleanup_repo.sh`:

```bash
#!/bin/bash
set -e

echo "Repository Cleanup Script"
echo "========================="

# Create target directories
mkdir -p scripts/ml
mkdir -p scripts/hpo
mkdir -p scripts/backtest
mkdir -p scripts/admin
mkdir -p tools
mkdir -p migrations
mkdir -p tests/utils

# Move Alpha/HPO scripts
mv alpha_loop.py scripts/ml/ 2>/dev/null || true
mv hpo_cli.py scripts/hpo/ 2>/dev/null || true
mv hpo_cli_upgraded.py scripts/hpo/ 2>/dev/null || true
mv hpo_backends.py scripts/hpo/ 2>/dev/null || true
mv compare_hpo_params.py scripts/hpo/compare_params.py 2>/dev/null || true
mv apply_hpo_params.py scripts/hpo/apply_params.py 2>/dev/null || true
mv prove_hpo_improvement.py scripts/hpo/prove_improvement.py 2>/dev/null || true
mv export_hpo_report.py scripts/hpo/export_report.py 2>/dev/null || true

# Move backtest scripts
mv run_backtest_batch.py scripts/backtest/run_batch.py 2>/dev/null || true
mv run_backtest_batch_relaxed.py scripts/backtest/run_batch_relaxed.py 2>/dev/null || true
mv run_backtest_simple.py scripts/backtest/run_simple.py 2>/dev/null || true
mv run_backtest_reve.py scripts/backtest/run_reve.py 2>/dev/null || true
mv run_backtest_with_viz.py scripts/backtest/run_with_viz.py 2>/dev/null || true
mv plot_backtest.py scripts/backtest/plot_results.py 2>/dev/null || true
mv debug_backtest.py scripts/backtest/debug.py 2>/dev/null || true

# Move model scripts
mv models_ensemble.py scripts/ml/ 2>/dev/null || true
mv models_linear.py scripts/ml/ 2>/dev/null || true
mv leakage_guard.py scripts/ml/ 2>/dev/null || true
mv grid_guard.py scripts/ml/ 2>/dev/null || true

# Move config/admin scripts
mv apply_config_patch.py scripts/admin/ 2>/dev/null || true
mv apply_extras_params.py scripts/admin/ 2>/dev/null || true
mv boot_logging.py scripts/admin/ 2>/dev/null || true
mv relax_strategies_for_testing.py scripts/admin/relax_strategies.py 2>/dev/null || true

# Move debug/dev tools
mv demo_system.py tools/ 2>/dev/null || true
mv demo.py tools/ 2>/dev/null || true
mv debug_vwap_signals.py tools/ 2>/dev/null || true
mv diagnose_predictions.py tools/ 2>/dev/null || true
mv benchmark_speed.py tools/ 2>/dev/null || true
mv quick_system_validation.py tools/quick_validation.py 2>/dev/null || true
mv audit_implementation.py tools/ 2>/dev/null || true

# Move test utilities
mv generate_mock_backtest.py tests/utils/ 2>/dev/null || true
mv mock_data_injector.py tests/utils/ 2>/dev/null || true
mv minimal_hpo_test.py tests/utils/ 2>/dev/null || true
mv hot_reload_test_params.py tests/utils/ 2>/dev/null || true

# Move SQL files
mv fix_bars_1m_ibkr_schema.sql migrations/ 2>/dev/null || true
mv fix_bars_1m_ibkr_schema_final.sql migrations/ 2>/dev/null || true

echo ""
echo "Cleanup complete!"
echo ""
echo "Next steps:"
echo "1. Review moved files"
echo "2. Update any import paths if needed"
echo "3. Run: git status"
echo "4. Commit changes: git add -A && git commit -m 'chore: reorganize repository structure'"
```

---

## Pre-Cleanup Checklist

Before running cleanup:

1. ✅ Ensure .gitignore is updated
2. ⏭️ Create backup: `tar -czf trading_backup_$(date +%Y%m%d).tar.gz .`
3. ⏭️ Check for uncommitted changes: `git status`
4. ⏭️ Create cleanup branch: `git checkout -b cleanup/repo-organization`
5. ⏭️ Review which scripts are actively used
6. ⏭️ Test key entry points still work

---

## Post-Cleanup Tasks

After moving files:

1. ⏭️ Update imports in moved files (if needed)
2. ⏭️ Update documentation/README references
3. ⏭️ Update CI/CD scripts (if any)
4. ⏭️ Test critical paths:
   - `python run_system.py --help`
   - `python wf_hpo.py --help`
   - `python backtest_ml_engine.py --help`
5. ⏭️ Run tests: `pytest tests/`
6. ⏭️ Commit and push

---

## Large File Scan

To find large files that should be excluded:

```bash
# Find files larger than 10MB
find . -type f -size +10M ! -path "./.git/*" ! -path "./venv/*" -exec ls -lh {} \; | awk '{print $9, $5}'

# Find all .pth/.pkl/.parquet files
find . -type f \( -name "*.pth" -o -name "*.pkl" -o -name "*.parquet" \) ! -path "./.git/*" ! -path "./venv/*"

# Find all .sql/.backup files
find . -type f \( -name "*.sql" -o -name "*.backup" \) ! -path "./.git/*" ! -path "./venv/*"
```

---

## Expected Git Status After Cleanup

```
On branch cleanup/repo-organization
Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
        modified:   .gitignore
        renamed:    alpha_loop.py -> scripts/ml/alpha_loop.py
        renamed:    hpo_cli.py -> scripts/hpo/hpo_cli.py
        ... (50+ renames)
```

---

## Safe Execution Plan

### Phase 1: Preparation (✅ DONE)
- ✅ Update .gitignore
- ✅ Document cleanup plan
- ⏭️ Create backup

### Phase 2: Execution (READY)
- ⏭️ Create cleanup branch
- ⏭️ Run cleanup script
- ⏭️ Verify git status

### Phase 3: Validation (PENDING)
- ⏭️ Test import paths
- ⏭️ Run test suite
- ⏭️ Verify entry points work

### Phase 4: Finalization (PENDING)
- ⏭️ Commit changes
- ⏭️ Create PR/merge
- ⏭️ Update documentation

---

## Risk Mitigation

**Low Risk:**
- Moving scripts preserves git history
- .gitignore changes don't affect existing files
- Can easily revert with `git checkout`

**Medium Risk:**
- Import paths may need updating
- Scripts that reference root-level files may break

**Mitigation:**
1. Work on feature branch
2. Create backup before starting
3. Test incrementally
4. Keep list of moved files for rollback

---

## Summary

**Current State:**
- 90 Python files in root
- 2 SQL files in root
- Incomplete .gitignore

**Target State:**
- 3-5 core entry points in root
- Scripts organized by purpose
- Comprehensive .gitignore
- Clean git status

**Estimated Time:** 30-60 minutes
**Risk Level:** Low (with backup)
**Reversibility:** High (git history preserved)

---

**Status:** ✅ Plan ready for execution
**Next Action:** Create backup and run cleanup script
