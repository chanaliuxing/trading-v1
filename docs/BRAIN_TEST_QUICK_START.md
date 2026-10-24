# Brain IC Integration Test - Quick Start Guide

**日期**: 2025-11-09
**状态**: ✅ Ready to Execute

---

## 🎯 测试目标

验证Brain能自主迭代ML，在≤6个cycles内达到**四维成功标准**:
- ✅ Sharpe ≥ 1.2
- ✅ Profit Factor ≥ 1.2
- ✅ **IC ≥ 0.02** (Information Coefficient - 新增)
- ✅ DD稳定 (ΔMaxDD ≤ 0.03)

---

## ⚡ Quick Start (5分钟启动)

### Step 1: 验证环境

```bash
# 检查ClickHouse schema
docker exec trading_clickhouse clickhouse-client --query "
SELECT name FROM system.tables
WHERE database = 'trading' AND name LIKE 'ml_%'"

# 应该看到: ml_eval_flat, ml_eval_with_delta, ml_model_stats_30d
```

**✅ 已完成**: Schema已部署

### Step 2: 重置环境

```bash
# 清理locks和slots
cd .
PYTHONPATH=. python tools/reset_ml_slot.py --kill
```

**✅ 已完成**: Slots已释放

### Step 3: 启动服务

**Terminal 1 - Scheduler**:
```bash
cd .
ML_SCHEDULER_FORCE_REPLACE=true python scripts/ai_trader/ml_scheduler.py
```

**Terminal 2 - Consumer**:
```bash
cd .
python scripts/ai_trader/control_event_consumer.py
```

**验证**:
```bash
# 检查进程
ps aux | grep -E "(ml_scheduler|control_event_consumer)" | grep -v grep
```

### Step 4: 准备测试数据 (TSLA Toxicity)

创建 OOF eval JSON:
```bash
# 创建目录
mkdir -p runs/ai_trader/$(date +%Y-%m-%d)/oof_eval

# 创建测试数据
cat > runs/ai_trader/$(date +%Y-%m-%d)/oof_eval/TSLA.json << 'EOF'
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
EOF
```

**关键点**:
- `n_bars=70450` > 25k → **Toxicity** (过多数据)
- `fusion.ic=0.0036` ≈ 0 → **预测质量极差**
- `oos_avg_sharpe=0.85` < 1.2 → **未达标**

### Step 5: 触发Cycle 1

```bash
# 如果run_ml_drill.py存在
python tools/run_ml_drill.py --job-id drill-001-tsla-cycle1

# 或者手动触发Brain review (如果drill工具不存在)
# 需要根据实际的trigger机制调整
```

---

## 📊 监控测试进度

### 实时监控脚本

```bash
# 运行监控脚本 (每次查看当前状态)
./tools/brain_test_monitor.sh TSLA

# 或持续监控特定文件
watch -n 5 ./tools/brain_test_monitor.sh TSLA
```

### 关键指标追踪

```bash
# 1. Brain最新决策
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json | jq '{symbol, action, confidence_final}'

# 2. Actions历史
tail -5 runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | jq '.action'

# 3. ClickHouse最新数据
docker exec trading_clickhouse clickhouse-client --query "
SELECT oos_avg_sharpe, oos_avg_ic, is_success
FROM trading.ml_eval_with_delta
WHERE symbol='TSLA'
ORDER BY updated_at DESC LIMIT 1"

# 4. 验证成功标准
python tools/brain_test_validate.py --symbol TSLA
```

---

## ✅ 成功标准检查

### 快速验证命令

```bash
python tools/brain_test_validate.py --symbol TSLA
```

**输出示例** (成功):
```
====================================================================
Brain IC Integration Test - Validation Report
====================================================================

Symbol: TSLA
Trial ID: 1234
Updated: 2025-11-09 12:00:00

--------------------------------------------------------------------
Four-Dimensional Success Gates
--------------------------------------------------------------------
✅ Sharpe ≥ 1.2
   Current: 1.3200
✅ Profit Factor ≥ 1.2
   Current: 1.3800
✅ IC ≥ 0.02 ⭐
   Current: 0.0280
✅ (ΔSharpe ≥ 0.02 OR ΔIC ≥ 0.0)
   ΔSharpe: 0.0450
   ΔIC: 0.0120
✅ ΔMaxDD ≤ 0.03
   Current: 0.0200

--------------------------------------------------------------------
Overall Result
--------------------------------------------------------------------
✅ is_success = 1

🎉 TEST PASSED - Four-dimensional success gates met!

Cycles completed: 4

✅ VALIDATION PASSED
```

---

## 📝 预期测试流程

### Scenario A: TSLA Toxicity

```
Cycle 1: Toxicity detected (n_bars=70450, IC≈0)
         ↓
         Action: reduce_window (70 → 60 days)
         Status: ok
         ↓
Cycle 2: Still toxic (n_bars=21940, IC=0.0042)
         ↓
         Action: queue_job (HPO with n_trials=45)
         Status: ok
         ↓
Cycle 3: HPO running... (1-2 hours)
         ↓
Cycle 4: HPO completed
         Metrics: Sharpe=1.32, PF=1.38, IC=0.028
         ↓
         is_success = 1 ✅

Total Time: 2-4 hours
Total Cycles: 4
Result: PASS ✅
```

---

## 🔍 故障排查

### 问题 1: Brain不触发

**检查**:
```bash
# 1. Consumer是否运行
pgrep -f control_event_consumer

# 2. OOF JSON是否存在
ls runs/ai_trader/$(date +%Y-%m-%d)/oof_eval/

# 3. 检查events日志
tail -20 runs/ai_trader/$(date +%Y-%m-%d)/ai_events.jsonl
```

**解决**: 手动触发或检查trigger机制

### 问题 2: Actions status = validation_failed

**检查**:
```bash
# 查看失败原因
cat runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | grep validation_failed
```

**解决**: 检查validators.py的allowed_numbers或参数范围

### 问题 3: HPO没有启动

**检查**:
```bash
# 1. 检查queue
cat runs/ai_trader/queue/ml_jobs.jsonl | grep TSLA | tail -1

# 2. 检查scheduler日志
grep "alpha_loop" runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log | tail -5
```

**解决**: 验证scheduler收到job，检查params传递

### 问题 4: ClickHouse无数据

**检查**:
```bash
# 1. 检查wf_state表
docker exec trading_clickhouse clickhouse-client --query "
SELECT count() FROM trading.wf_state
WHERE step='trial_end' AND status='done'"

# 2. 检查MV同步
docker exec trading_clickhouse clickhouse-client --query "
SELECT count() FROM trading.ml_eval_flat"
```

**解决**:
- 如果wf_state有数据但ml_eval_flat无数据 → MV只捕获新数据
- 手动插入测试数据验证MV工作

---

## 📚 详细文档

- **完整测试计划**: `docs/BRAIN_IC_INTEGRATION_TEST_PLAN.md`
- **IC集成实现**: `docs/IC_INTEGRATION_COMPLETED.md`
- **IC测试报告**: `docs/IC_INTEGRATION_TEST_REPORT.md`
- **IC集成总结**: `docs/IC_INTEGRATION_FINAL_SUMMARY.md`

---

## 🚀 开始测试

```bash
# 1. 启动Scheduler (Terminal 1)
ML_SCHEDULER_FORCE_REPLACE=true python scripts/ai_trader/ml_scheduler.py

# 2. 启动Consumer (Terminal 2)
python scripts/ai_trader/control_event_consumer.py

# 3. 准备测试数据
# (见上面Step 4)

# 4. 触发测试
python tools/run_ml_drill.py --job-id drill-001-tsla-cycle1

# 5. 监控进度 (Terminal 3)
watch -n 10 ./tools/brain_test_monitor.sh TSLA

# 6. 验证结果
python tools/brain_test_validate.py --symbol TSLA
```

---

## ⏱️ 时间估算

| 阶段 | 估计时间 |
|------|---------|
| 环境准备 | 5分钟 |
| Cycle 1 (reduce_window) | 5分钟 |
| Cycle 2 (queue_job) | 5分钟 |
| HPO运行 | 1-2小时 |
| Cycle 3-4 (review + 可能的feature) | 30分钟 - 2小时 |
| **总计** | **2-4小时** |

---

## ✅ 完成检查清单

- [ ] ClickHouse schema deployed
- [ ] Scheduler running
- [ ] Consumer running
- [ ] OOF JSON created
- [ ] Cycle 1 triggered
- [ ] ml_review.json shows reduce_window
- [ ] Cycle 2 triggered
- [ ] queue_job created
- [ ] HPO completed
- [ ] ClickHouse has is_success=1
- [ ] Validation script passes

---

**准备完成**: 系统已就绪，可立即开始测试！

**下一步**:
1. 启动服务 (Scheduler + Consumer)
2. 创建OOF JSON
3. 触发Cycle 1
4. 监控进度
5. 验证结果

**预期结果**: 4-6个cycles内达到is_success=1 ✅
