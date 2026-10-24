# AI Brain 自动控制机器学习 - 快速启动指南
详细阅读

> **目标**: 让 AI Brain 持续监控 ML 结果,自动调整参数,直到生成合格的盈利模型

---

## 一、系统架构

```
┌─────────────────┐         ┌──────────────────┐         ┌─────────────────┐
│ Brain Consumer  │ ──监听──> │  Kafka Queue     │ <──提交─ │  ML Scheduler   │
│  (AI 决策器)     │         │ (internal.ml.*)  │         │  (任务执行器)    │
└─────────────────┘         └──────────────────┘         └─────────────────┘
        ↓                                                          ↓
   分析 ML 结果                                                执行 HPO/OOF
        ↓                                                          ↓
   提出优化 action                                            发送完成事件
   (调整窗口/HPO参数)                                              ↓
        ↓                                                    更新 metrics
   写入 ml_review.json ────────────────────────────────────→ 触发下一轮
```

---

## 二、快速启动 (3 步)

### Step 1: 启动 Brain Consumer

```bash
cd .

# 启动 Brain Consumer (在后台运行)
PYTHONPATH=. ./venv/bin/python scripts/ai_trader/control_event_consumer.py \
  > runs/ai_trader/$(date +%Y-%m-%d)/brain_consumer.log 2>&1 &

echo "Brain Consumer started (PID: $!)"
```

**作用**: 监听 Kafka 的 ML 完成事件,分析结果,提出优化建议

### Step 2: 启动 ML Scheduler

```bash
# 启动 ML Scheduler (在后台运行)
PYTHONPATH=. ./venv/bin/python scripts/ai_trader/ml_scheduler.py \
  > runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log 2>&1 &

echo "ML Scheduler started (PID: $!)"
```

**作用**: 从队列读取 Brain 的优化任务,执行 HPO/OOF

### Step 3: 触发初始 ML 任务

```bash
# 触发一个 OOF 评估作为起点 (使用真实数据)
PYTHONPATH=. ./venv/bin/python tools/nightly_oof_flow.py \
  --symbols TSLA \
  --start "2025-09-01 09:30:00" \
  --end "2025-11-06 16:00:00" \
  --window-days 60 \
  --quick \
  --max-iters 3 \
  > runs/ai_trader/$(date +%Y-%m-%d)/oof_initial.log 2>&1 &

echo "Initial OOF triggered (PID: $!)"
```

**作用**: 生成第一个 OOF 结果,触发 Brain 分析并启动优化循环

---

## 三、系统会自动循环

一旦启动,系统会自动循环运行:

```
1. OOF/HPO 完成 → 发送 Kafka 事件
                    ↓
2. Brain Consumer 收到事件 → 分析结果 (IC, AUC, Sharpe)
                    ↓
3. Brain 提出优化 action:
   - reduce_window (缩小窗口)
   - extend_window (扩大窗口)
   - queue_job (运行 HPO)
   - backfill (补充数据)
                    ↓
4. ML Scheduler 执行 action → 生成新结果
                    ↓
5. 回到步骤 1 (持续优化)
```

**终止条件**:
- Brain 判断模型已合格 (Sharpe ≥ 1.0, Profit Factor > 1.0)
- Brain 发出 `pause_ml` action
- 用户手动停止

---

## 四、监控脚本

使用以下监控脚本实时查看进度:

```bash
# 方法 1: 使用自动化监控脚本
./tools/monitor_brain_ml.sh

# 方法 2: 手动查看关键日志
tail -f runs/ai_trader/$(date +%Y-%m-%d)/brain_consumer.log | grep -E "(ml_job_completed|ml_review_saved|queued_action)"
```

监控脚本会显示:
- 进程状态 (Consumer/Scheduler 是否运行)
- 最近 Brain 决策 (从 ml_review.json)
- ML action 历史 (成功/失败/跳过)
- 数据可用性 (多少天历史数据)
- Metrics 趋势 (IC/AUC 是否改善)

---

## 五、关键文件位置

### 日志文件
```bash
runs/ai_trader/2025-11-07/
  ├── brain_consumer.log       # Brain Consumer 日志
  ├── ml_scheduler.log          # ML Scheduler 日志
  ├── oof_initial.log           # OOF 评估日志
  ├── ml_review.json            # Brain 最新决策 (JSON 格式)
  ├── ml_actions.jsonl          # 所有 action 历史 (每行一个 JSON)
  └── ml_last_metrics.json      # 上一次的 metrics (用于计算 delta)
```

### 配置文件
```bash
config.yml                                    # 主配置 (trading universe, HPO 默认参数)
config/oof_policies.json                      # OOF 策略 (horizon, label_bps, window_days)
config/gating_policies.json                   # Gating 策略 (conf_floor, sharpe 阈值)
scripts/ai_trader/brain/brain.py              # Brain 提示词和逻辑
scripts/ai_trader/brain/policy/validators.py  # Action 验证规则
```

---

## 六、常用监控命令

### 查看进程状态
```bash
ps aux | grep -E "control_event_consumer|ml_scheduler" | grep -v grep
```

### 查看最新 Brain 决策
```bash
jq '.' runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json
```

### 查看 ML action 历史 (最近 5 条)
```bash
tail -5 runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | jq '.'
```

### 查看数据可用性
```bash
docker exec -i trading_clickhouse clickhouse-client --query "
  SELECT
    symbol,
    dateDiff('day', MIN(ts), MAX(ts)) as days_available,
    COUNT(*) as bars
  FROM trading.features_1m_with_labels
  WHERE symbol IN ('TSLA', 'AAPL', 'NVDA')
  GROUP BY symbol
  FORMAT PrettyCompact
"
```

### 实时监控 Brain 输出
```bash
tail -f runs/ai_trader/$(date +%Y-%m-%d)/brain_consumer.log | \
  grep --color=auto -E "(ml_job_completed|ml_review_saved|reduce_window|extend_window|queue_job|validation_failed)"
```

### 查看 HPO 进度
```bash
tail -f runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler.log | \
  grep --color=auto -E "(alpha_loop_starting|alpha_loop_completed|job_dequeued)"
```

---

## 七、验证 Brain 是否生效

### 检查 Brain 能看到可用数据
在 Brain Consumer 日志中搜索 `data_availability`:

```bash
grep "data_availability" runs/ai_trader/$(date +%Y-%m-%d)/brain_consumer.log
```

应该看到类似:
```
"data_availability": {"symbols": {"TSLA": 202}, "max_days_available": 202}
```

### 检查 Brain 建议的窗口大小
查看 ml_review.json 的 `next_actions`:

```bash
jq '.review.next_actions[] | select(.action == "extend_window" or .action == "reduce_window")' \
  runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json
```

应该看到 `window_days` 在 {30, 60, 90, 120, 150, 180} 范围内 (而不是旧的 {21, 30, 45, 60})。

### 检查 Validator 是否通过
查看 ml_actions.jsonl 中是否有 `validation_failed`:

```bash
grep "validation_failed" runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl
```

如果没有输出 = 验证都通过了 ✅

如果有输出,检查原因:
```bash
grep "validation_failed" runs/ai_trader/$(date +%Y-%m-%d)/ml_actions.jsonl | jq '.params.reason'
```

---

## 八、停止系统

```bash
# 停止 Brain Consumer 和 ML Scheduler
pkill -f "control_event_consumer.py|ml_scheduler.py"

# 验证已停止
ps aux | grep -E "control_event_consumer|ml_scheduler" | grep -v grep
```

**注意**: 停止后,正在运行的 HPO/OOF 任务会继续完成,但不会触发新的优化循环。

---

## 九、故障排查

### 问题 1: Brain Consumer 启动失败

**症状**:
```
FileNotFoundError: No such file or directory: 'runs/ai_trader/2025-11-07'
```

**解决**:
```bash
mkdir -p runs/ai_trader/$(date +%Y-%m-%d)/locks
mkdir -p runs/ai_trader/$(date +%Y-%m-%d)/cache
```

---

### 问题 2: Validator 一直拒绝 Brain 的决策

**症状**:
```json
{"action": "validation_failed", "params": {"reason": "fabricated_number_detected"}}
```

**解决**:
1. 检查 `validators.py` 是否更新到最新版本 (支持 k-notation 和 1% 容差)
2. 重启 Brain Consumer 加载新代码:
   ```bash
   pkill -f control_event_consumer.py
   PYTHONPATH=. ./venv/bin/python scripts/ai_trader/control_event_consumer.py \
     > runs/ai_trader/$(date +%Y-%m-%d)/brain_consumer_restart.log 2>&1 &
   ```

---

### 问题 3: ML Scheduler 没有执行 action

**症状**: ml_actions.jsonl 有 action,但 ml_scheduler.log 没有 `job_dequeued`

**解决**:
1. 检查 Scheduler 是否运行:
   ```bash
   ps aux | grep ml_scheduler.py | grep -v grep
   ```

2. 检查队列是否有任务:
   ```bash
   # 查看 Kafka 队列
   docker exec -it trading_kafka kafka-console-consumer --bootstrap-server localhost:9092 \
     --topic internal.ml.queue --from-beginning --max-messages 10
   ```

3. 重启 Scheduler:
   ```bash
   pkill -f ml_scheduler.py
   PYTHONPATH=. ./venv/bin/python scripts/ai_trader/ml_scheduler.py \
     > runs/ai_trader/$(date +%Y-%m-%d)/ml_scheduler_restart.log 2>&1 &
   ```

---

### 问题 4: Brain 一直建议 `do_nothing`

**症状**: ml_review.json 的 next_actions 一直是 `do_nothing`

**可能原因**:
1. **Success cooldown 激活**: Brain 刚成功执行了一个 action,进入冷却期
2. **Metrics 没有改善**: IC/AUC 一直很差,Brain 判断需要等待
3. **缺少必要数据**: `missing_fields` 不为空

**解决**:
```bash
# 检查 cooldown 状态
ls -lh runs/ai_trader/$(date +%Y-%m-%d)/locks/success_cooldown_*.lock

# 如果存在,删除 lock (强制解除冷却)
rm runs/ai_trader/$(date +%Y-%m-%d)/locks/success_cooldown_*.lock

# 检查 missing_fields
jq '.review.missing_fields' runs/ai_trader/$(date +%Y-%m-%d)/ml_review.json
```

---

## 十、性能优化建议

### 1. 使用真实 HPO (而不是 quick 模式)

Quick 模式 (max-iters=2~3) 只用于测试,实际优化应该:

```bash
PYTHONPATH=. ./venv/bin/python tools/nightly_oof_flow.py \
  --symbols TSLA \
  --start "2025-09-01 09:30:00" \
  --end "2025-11-06 16:00:00" \
  --window-days 60 \
  --max-iters 10 \
  > runs/ai_trader/$(date +%Y-%m-%d)/oof_real.log 2>&1 &
```

### 2. 并行多 symbol

修改 config.yml 的 `universe`:

```yaml
universe:
  - TSLA
  - AAPL
  - NVDA
```

Brain 会为每个 symbol 独立优化。

### 3. 调整 Brain 的 timeout

如果 Brain 经常超时 (timeout > 300s),在 `brain.py` 增加:

```python
ProviderConfig(name="deepseek_nvidia", priority=1, timeout=600, max_retries=1)
```

---

## 十一、预期结果

系统正常运行时,你应该看到:

### Day 1-2: 探索阶段
- Brain 尝试不同窗口大小 (30/60/90 天)
- Brain 运行多次 HPO (n_trials=24~50)
- IC/AUC 缓慢提升

### Day 3-5: 优化阶段
- Brain 找到最优窗口 (例如 60 天)
- Brain 微调 HPO 参数 (timeout_hours, n_trials)
- Sharpe 接近 1.0

### Day 5+: 稳定阶段
- Brain 建议 `pause_ml` 或 `do_nothing`
- 模型通过 gating (Sharpe ≥ 1.0, PF > 1.0)
- 可以部署到 Paper Trading

---

## 十二、相关文档

- [Brain 策略详解](./AI_BRAIN_CAPABILITY_ASSESSMENT.md)
- [Validator 规则](../scripts/ai_trader/brain/policy/validators.py)
- [ML 优化流程](./BRAIN_VALIDATION_PLAN.md)
- [SFS 数据系统](./SFS_QUICK_REFERENCE.md)

---

**最后更新**: 2025-11-07
**维护者**: Trading Team
**问题反馈**: 检查 `runs/ai_trader/{date}/brain_consumer.log` 中的错误信息
