# Brain IC Integration - 端到端验证测试计划

**日期**: 2025-11-09
**目标**: 验证Brain能自主迭代ML达到四维成功标准 (Sharpe≥1.2, PF≥1.2, IC≥0.02, DD稳定)

---

## 🎯 测试目标

验证Brain在IC集成后能够:
1. **正确诊断**: 基于四维指标(Sharpe+PF+IC+MaxDD)诊断模型问题
2. **选择正确路径**:
   - Toxicity (n_bars≥25k, IC≈0) → reduce_window
   - Low IC in range → queue_job (HPO)
   - IC persistent low → feature_build
3. **参数正确传递**: scheduler接收到正确的n_trials, timeout_hours, symbols
4. **达到成功标准**: ≤6个cycles内达到is_success=1
5. **安全保障**: 无extend_window under toxicity, 无fabricated numbers

---

## ✅ Prerequisites 检查

### 1. ClickHouse Schema
```bash
# 验证表和视图已部署
docker exec trading_clickhouse clickhouse-client --query "
SELECT name FROM system.tables
WHERE database = 'trading' AND name LIKE 'ml_%'
ORDER BY name"

# 应该看到:
# ml_eval_flat
# ml_eval_with_delta
# ml_model_stats_30d
```

**✅ 已验证**: 所有表和视图已部署

### 2. 配置检查
```bash
# 验证config.yml中的Brain policies
grep -A 15 "^  ml:" config.yml
```

**✅ 已验证**: 配置正确
```yaml
ml:
  slot: 1
  actions_conf_floor: 0.60
  min_conf_by_action:
    data_quality_check: 0.40
    reduce_window: 0.55
    extend_window: 0.70
    queue_job: 0.65
  confidence_calibration:
    base: 0.60
    history_weight: 0.40
    skip_fail_streak: 2
  memory_source:
    use_clickhouse: false
```

### 3. 清理Locks/Slots
```bash
# 重置ML slots和locks
PYTHONPATH=. python tools/reset_ml_slot.py --kill
```

**✅ 已完成**: Slots已释放，locks已清理

---

## 🧪 测试矩阵

### Symbols
- **TSLA**: Scenario A (Toxicity)
- **AAPL**: Scenario B (No toxicity, optional)

### 测试场景

#### Scenario A: Toxicity Start (TSLA)
**初始条件**:
- `n_bars` ≥ 25,000 (toxic data量)
- `fusion.ic` ≈ 0 (预测质量差)
- `oos_avg_sharpe` < 1.2

**预期路径**:
```
Cycle 1: reduce_window (60 → 30天)
    ↓
Cycle 2: reduce_window (30 → 15天) 或 queue_job
    ↓
Cycle 3-4: queue_job (HPO)
    ↓
Cycle 5-6: feature_build (如IC仍<0.02)
    ↓
is_success=1 (Sharpe≥1.2, PF≥1.2, IC≥0.02)
```

#### Scenario B: No Toxicity (AAPL, Optional)
**初始条件**:
- `n_bars` 在 [1k, 25k] 范围
- `fusion.ic` low but not zero

**预期路径**:
```
Cycle 1-2: queue_job (HPO)
    ↓
Cycle 3-4: feature_build (如需要)
    ↓
is_success=1
```

---

## 📋 测试执行步骤

### Phase 1: 启动服务

#### 1.1 启动Scheduler (新窗口)
```bash
cd .
ML_SCHEDULER_FORCE_REPLACE=true python scripts/ai_trader/ml_scheduler.py
```

**验证**:
```bash
ps aux | grep ml_scheduler | grep -v grep
# 应该看到进程正在运行

# 检查日志
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log
```

#### 1.2 启动Consumer (新窗口)
```bash
cd .
python scripts/ai_trader/control_event_consumer.py
```

**验证**:
```bash
ps aux | grep control_event_consumer | grep -v grep
# 应该看到进程正在运行

# 检查日志
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ai_events.jsonl
```

---

### Phase 2: Cycle 1 - Seed Toxicity OOF

#### 2.1 准备 OOF Eval JSON
创建 `runs/ai_trader/$(date +%Y-%m-%d)/oof_eval/TSLA.json`:

```json
{
  "symbol": "TSLA",
  "model_family": "xgb",
  "oof_params": {
    "is_days": 70,
    "oos_days": 1,
    "window_days": 70,
    "horizon_min": 10
  },
  "metrics": {
    "n_bars": 70450,
    "n_is": 69000,
    "n_oos": 1450,
    "oos_avg_sharpe": 0.85,
    "oos_avg_profit_factor": 1.05,
    "oos_avg_ic": 0.0036,
    "oos_avg_max_drawdown": -0.18,
    "oos_avg_win_rate": 0.52,
    "oos_trades": 145
  },
  "fusion": {
    "ic": 0.0036,
    "sharpe": 0.85,
    "pf": 1.05
  },
  "ts": "2025-11-09T00:00:00Z"
}
```

**关键点**:
- `n_bars=70450` > 25k → Toxicity
- `fusion.ic=0.0036` ≈ 0 → 预测质量极差
- `oos_avg_sharpe=0.85` < 1.2 → 未达标
- `oos_avg_ic=0.0036` < 0.02 → 未达标

#### 2.2 触发Brain Review
```bash
python tools/run_ml_drill.py --job-id drill-001-tsla-cycle1
```

#### 2.3 验证 Cycle 1 输出

**检查 `ml_review.json`**:
```bash
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json | jq '.'
```

**预期内容**:
```json
{
  "symbol": "TSLA",
  "diagnosis": {
    "n_bars": 70450,
    "ic_current": 0.0036,
    "sharpe_current": 0.85,
    "history_delta": {
      "prev_n_bars": null,
      "prev_ic": null,
      "effect_of_last_action": null
    }
  },
  "action": "reduce_window",
  "action_params": {
    "new_window_days": 60
  },
  "action_rationale": "Toxicity detected (n_bars=70450 > 25k, IC≈0). Reducing window to 60 days.",
  "confidence_raw": 0.75,
  "confidence_final": 0.75,
  "numbers_used": ["70450", "60", "0.0036"]
}
```

**检查 `ml_actions.jsonl`**:
```bash
tail -5 runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | jq '.'
```

**预期**:
```json
{
  "ts": "2025-11-09T...",
  "symbol": "TSLA",
  "action": "reduce_window",
  "params": {"new_window_days": 60},
  "status": "ok",
  "confidence": 0.75
}
```

**检查 `hpo_hints.json`** (如果有):
```bash
cat runs/ai_trader/$(date +%Y-%m-%d)/hpo_hints/TSLA.json | jq '.'
```

---

### Phase 3: Cycle 2 - Smaller Window

#### 3.1 更新 OOF Eval
更新 `TSLA.json` with smaller window:
```json
{
  "symbol": "TSLA",
  "model_family": "xgb",
  "oof_params": {
    "is_days": 60,
    "oos_days": 1,
    "window_days": 60,
    "horizon_min": 10
  },
  "metrics": {
    "n_bars": 21940,
    "n_is": 20800,
    "n_oos": 1140,
    "oos_avg_sharpe": 0.88,
    "oos_avg_profit_factor": 1.08,
    "oos_avg_ic": 0.0042,
    "oos_avg_max_drawdown": -0.16,
    "oos_avg_win_rate": 0.53,
    "oos_trades": 118
  },
  "fusion": {
    "ic": 0.0042,
    "sharpe": 0.88,
    "pf": 1.08
  },
  "ts": "2025-11-09T01:00:00Z"
}
```

**变化**:
- `n_bars`: 70450 → 21940 (减少)
- `ic`: 0.0036 → 0.0042 (微小改善，仍<0.02)
- `sharpe`: 0.85 → 0.88 (微小改善，仍<1.2)

#### 3.2 触发 Cycle 2
```bash
python tools/run_ml_drill.py --job-id drill-002-tsla-cycle2
```

#### 3.3 验证 Cycle 2 输出

**预期 `ml_review.json`**:
```json
{
  "symbol": "TSLA",
  "diagnosis": {
    "n_bars": 21940,
    "ic_current": 0.0042,
    "sharpe_current": 0.88,
    "history_delta": {
      "prev_n_bars": 70450,
      "prev_ic": 0.0036,
      "delta_n_bars": -48510,
      "delta_ic": 0.0006,
      "effect_of_last_action": "negligible improvement"
    }
  },
  "action": "queue_job",
  "action_params": {
    "symbols": ["TSLA"],
    "n_trials": 45,
    "timeout_hours": 1.5
  },
  "action_rationale": "Window reduced, IC still low (<0.02). Queue HPO to optimize params.",
  "confidence_raw": 0.68,
  "confidence_final": 0.68
}
```

**检查 Queue Entry**:
```bash
cat runs/ai_trader/queue/ml_jobs.jsonl | grep TSLA | tail -1 | jq '.'
```

**预期**:
```json
{
  "job_id": "hpo_tsla_...",
  "symbol": "TSLA",
  "action": "queue_job",
  "params": {
    "symbols": ["TSLA"],
    "n_trials": 45,
    "timeout_hours": 1.5
  },
  "status": "queued",
  "ts": "2025-11-09T..."
}
```

**验证Scheduler接收**:
```bash
# 检查scheduler日志，应该看到:
grep "alpha_loop_starting" runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log | tail -1

# 预期:
# alpha_loop_starting symbol=TSLA samples=45 timeout_hours=1.5 raw_params={"symbols":["TSLA"],"n_trials":45,...}
```

---

### Phase 4: HPO Completion

#### 4.1 等待HPO完成
```bash
# 监控HPO进程
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log | grep alpha_loop

# 应该看到:
# alpha_loop_starting ...
# alpha_loop_completed job_id=... metrics={...}
```

#### 4.2 检查ML Events
```bash
# HPO完成后，scheduler会发布ml.job event
tail -20 runs/ai_trader/$(date +%Y-%m-%d)/ml_events.jsonl | jq '. | select(.event=="ml.job" and .status=="completed")'
```

**预期**:
```json
{
  "event": "ml.job",
  "status": "completed",
  "job_id": "hpo_tsla_...",
  "symbol": "TSLA",
  "metrics": {
    "best_sharpe": 1.28,
    "best_profit_factor": 1.35,
    "oos_avg_ic": 0.025,
    "oos_avg_sharpe": 1.28,
    "oos_avg_profit_factor": 1.35,
    "oos_avg_max_drawdown": -0.11
  },
  "ts": "2025-11-09T..."
}
```

#### 4.3 验证ClickHouse数据
```bash
# 查询最新评估
docker exec trading_clickhouse clickhouse-client --query "
SELECT
    symbol,
    trial_id,
    oos_avg_sharpe,
    oos_avg_ic,
    oos_avg_profit_factor,
    is_success,
    is_big_success,
    updated_at
FROM trading.ml_eval_with_delta
WHERE symbol = 'TSLA'
ORDER BY updated_at DESC
LIMIT 1"
```

**预期**:
```
TSLA  1234  1.28  0.025  1.35  1  0  2025-11-09 ...
```

**关键验证**:
- `oos_avg_sharpe >= 1.2` ✅
- `oos_avg_profit_factor >= 1.2` ✅
- `oos_avg_ic >= 0.02` ✅
- `is_success = 1` ✅

---

### Phase 5: Cycle 3 - Post-HPO Review

#### 5.1 Brain自动Review
HPO完成后，consumer会自动触发Brain review。

#### 5.2 检查Review输出
```bash
# 最新的ml_review.json
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json | jq '.'
```

**预期** (如果is_success=1):
```json
{
  "symbol": "TSLA",
  "diagnosis": {
    "ic_current": 0.025,
    "sharpe_current": 1.28,
    "pf_current": 1.35,
    "history_delta": {
      "prev_ic": 0.0042,
      "delta_ic": 0.0208,
      "effect_of_last_action": "significant improvement - success gates met"
    }
  },
  "action": "monitor",
  "action_rationale": "Success gates met (Sharpe≥1.2, PF≥1.2, IC≥0.02). Continue monitoring.",
  "success_gates_met": true
}
```

---

### Phase 6: Feature Ladder (如需要)

如果HPO后IC仍<0.02:

#### 6.1 预期Action
```json
{
  "action": "feature_build",
  "action_params": {
    "candidates": ["momentum_cross", "volatility_regime"]
  }
}
```

#### 6.2 验证Feature Build
```bash
# 检查feature build状态
cat runs/ai_trader/$(date +%Y-%m-%d)/features_build/status.json
```

#### 6.3 验证Feature Eval
```bash
# 检查feature eval报告
ls runs/ai_trader/$(date +%Y-%m-%d)/feature_eval/
cat runs/ai_trader/$(date +%Y-%m-%d)/feature_eval/report_*.json
```

#### 6.4 验证Feature Apply
```bash
cat runs/ai_trader/$(date +%Y-%m-%d)/feature_apply/apply.json
```

---

## 🎯 成功标准验证

### Pass Criteria
在≤6个cycles内，ClickHouse中显示:

```sql
SELECT
    symbol,
    oos_avg_sharpe,
    oos_avg_profit_factor,
    oos_avg_ic,
    delta_sharpe,
    delta_ic,
    delta_max_dd,
    is_success,
    is_big_success
FROM trading.ml_eval_with_delta
WHERE symbol = 'TSLA'
ORDER BY updated_at DESC
LIMIT 1;
```

**Pass Gates**:
- ✅ `oos_avg_sharpe >= 1.2`
- ✅ `oos_avg_profit_factor >= 1.2`
- ✅ `oos_avg_ic >= 0.02`
- ✅ `(delta_sharpe >= 0.02 OR delta_ic >= 0.0)`
- ✅ `delta_max_dd <= 0.03`
- ✅ `is_success = 1`

**Stretch (Big Success)**:
- ⭐ `(delta_sharpe >= 0.10 OR delta_ic >= 0.01)`
- ⭐ `oos_avg_sharpe >= 1.5`
- ⭐ `oos_avg_profit_factor >= 1.3`
- ⭐ `oos_avg_ic >= 0.03`
- ⭐ `is_big_success = 1`

---

## 🔍 监控和调试

### 实时监控命令

```bash
# 监控Brain review
watch -n 5 "cat runs/ai_trader/\$(date +%Y-%m-%d)/ml_review.json | jq '{symbol, action, confidence_final, success_gates_met}'"

# 监控Actions
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | jq '.'

# 监控Scheduler
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log | grep -E "alpha_loop|job_status"

# 监控Events
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_events.jsonl | jq '. | select(.event=="ml.job")'

# ClickHouse实时查询
watch -n 10 "docker exec trading_clickhouse clickhouse-client --query '
SELECT symbol, oos_avg_sharpe, oos_avg_ic, is_success
FROM trading.ml_eval_with_delta
WHERE symbol=\"TSLA\"
ORDER BY updated_at DESC LIMIT 3'"
```

### 关键日志位置

```
runs/ai_trader/YYYY-MM-DD/
├── ml_review.json           # Brain最新诊断和决策
├── ml_actions.jsonl         # 所有actions历史
├── ml_events.jsonl          # ML job events
├── ml_scheduler.log         # Scheduler日志
├── ai_events.jsonl          # Consumer events
├── oof_eval/
│   └── TSLA.json           # OOF评估结果
├── hpo_hints/
│   └── TSLA.json           # HPO hints
├── features_build/
│   └── status.json         # Feature build状态
├── feature_eval/
│   └── report_*.json       # Feature eval结果
└── queue/
    └── ml_jobs.jsonl       # Job queue
```

---

## 🚨 Edge Cases 测试

### Test 1: Validator Anti-Hallucination
**场景**: Brain提出 n_trials=50, timeout_hours=1.23

**验证**:
```bash
# 检查ml_actions.jsonl
grep "n_trials.*50" runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | jq '.'
```

**预期**: `status: "ok"`, 无 `validation_failed`

### Test 2: Slot/Lock Resilience
**场景**: 模拟stuck lock

```bash
# 创建stuck lock
mkdir -p runs/ai_trader/$(date +%Y-%m-%d)/locks
echo "stuck" > runs/ai_trader/$(date +%Y-%m-%d)/locks/ml_slot.lock

# 重置
PYTHONPATH=. python tools/reset_ml_slot.py --kill

# 重启scheduler
ML_SCHEDULER_FORCE_REPLACE=true python scripts/ai_trader/ml_scheduler.py &

# 验证: 1-2分钟内新job应该运行
```

### Test 3: Memory Calibration
**验证confidence调整**:

```bash
# 检查audit中的confidence_final
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json | jq '{confidence_raw, confidence_final, history_success_rate, fail_streak}'
```

**预期**:
- `fail_streak >= 2` → `confidence_final` 降低或skip
- `history_success_rate高` → `confidence_final` 提升

---

## 📊 成功指标汇总

### System Capability Checks

| 能力 | 验证方法 | Pass标准 |
|------|---------|---------|
| **正确诊断** | ml_review.json包含n_bars, IC, history_delta | ✅ diagnosis准确引用数字 |
| **选择正确路径** | ml_actions.jsonl显示reduce_window → queue_job → feature | ✅ 逻辑正确 |
| **参数传递** | scheduler log显示正确的symbols, n_trials, timeout_hours | ✅ 无default-all-symbols |
| **达到成功** | ClickHouse is_success=1 within ≤6 cycles | ✅ 在6个cycles内达标 |
| **安全保障** | 无extend_window under toxicity, 无fabricated numbers | ✅ 无违反 |

---

## 📝 测试执行Checklist

### 开始前
- [ ] ClickHouse schema deployed (ml_eval_*, MVs)
- [ ] Config.yml verified (policies.ml.*)
- [ ] Locks/slots cleaned
- [ ] Scheduler + Consumer running

### Cycle 1
- [ ] Toxicity OOF JSON created (n_bars>25k, IC≈0)
- [ ] Drill triggered
- [ ] ml_review.json shows reduce_window
- [ ] ml_actions.jsonl status=ok
- [ ] hpo_hints.json created

### Cycle 2
- [ ] Smaller window OOF JSON (n_bars~22k)
- [ ] Drill triggered
- [ ] ml_review.json shows history_delta
- [ ] Action is queue_job
- [ ] Queue entry has nested params
- [ ] Scheduler log shows alpha_loop_starting with correct params

### Cycle 3+
- [ ] HPO completes
- [ ] ml_events.jsonl has completed event with metrics
- [ ] ClickHouse has new data in ml_eval_flat
- [ ] ml_eval_with_delta shows is_success or next action
- [ ] Iterate until is_success=1

### Final Validation
- [ ] ClickHouse query shows is_success=1
- [ ] All success gates met (Sharpe, PF, IC, Delta)
- [ ] ≤6 cycles total
- [ ] No validator errors in ml_actions.jsonl
- [ ] Audit trails complete

---

## 🎉 预期结果

**成功场景** (≤6 cycles):
```
Cycle 1: Toxicity detected → reduce_window
Cycle 2: Still low IC → queue_job (HPO)
Cycle 3: HPO running...
Cycle 4: HPO completed → is_success=1 ✅

ClickHouse:
oos_avg_sharpe: 1.32 ✅ (≥1.2)
oos_avg_pf: 1.38 ✅ (≥1.2)
oos_avg_ic: 0.028 ✅ (≥0.02)
is_success: 1 ✅
```

**Stretch目标** (Big Success):
```
oos_avg_sharpe: 1.58 ✅ (≥1.5)
oos_avg_pf: 1.42 ✅ (≥1.3)
oos_avg_ic: 0.035 ✅ (≥0.03)
delta_sharpe: 0.12 ✅ (≥0.10)
is_big_success: 1 ✅
```

---

**准备完成**: 系统已就绪，可开始执行测试！
**估计时间**: 4-8小时 (取决于HPO速度)
**建议**: 在tmux/screen中运行，以便长时间观察
