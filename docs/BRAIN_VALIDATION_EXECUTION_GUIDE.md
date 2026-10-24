# Brain 验证执行指南

**目标**: 在3天内收集≥10个Brain影响的job，验证Brain是否比基线提升≥10%

**关键指标**:
- Brain成功率 vs 基线成功率
- IC/AUC提升
- 非回归拦截率 ≤5%
- 统计显著性 (N≥10)

---

## 📋 准备工作（已完成 ✅）

### 1. 配置已更新

**config.yml 变更**:
```yaml
policies:
  ml:
    actions_conf_floor: 0.55  # ✅ 降低阈值（从0.6→0.55），让Brain更主动
  apply:
    # ✅ 非回归保护（验证期严格模式）
    non_regression_tolerance:
      profit_factor_max_drop: 0.0
      sharpe_max_drop: 0.0
      total_return_max_drop: 0.0
```

**位置**: `./config.yml:335` 和 `:480-485`

### 2. 工具已就绪

- ✅ **daily_brain_report.py** - 每日收敛报告
- ✅ **brain_ab_validation.py** - AB对照验证脚本
- ✅ **validate_brain_effectiveness.py** - Brain效果验证（已有）

---

## 🚀 立即执行（今天）

### Step 1: 重启Consumer（应用新配置）

```bash
# 1. 停止旧的consumer
pkill -f control_event_consumer.py

# 2. 确认进程已停止
ps aux | grep control_event_consumer

# 3. 启动新的consumer
cd .
PYTHONPATH=. nohup python scripts/ai_trader/control_event_consumer.py > \
  logs/control_event_consumer_$(date +%F_%H%M%S).log 2>&1 &

# 4. 确认启动成功
tail -f logs/control_event_consumer_*.log
# 看到 "consumer started" 或类似日志后按 Ctrl+C 退出
```

**验证配置生效**:
```bash
# 检查日志中是否有新的conf_floor值
grep "conf_floor" logs/control_event_consumer_*.log | tail -5
```

### Step 2: 触发Day 1测试（3轮）

**选项A: 手动触发单轮**（推荐，便于观察）
```bash
# 上午轮（9:30-12:00）
python tools/brain_ab_validation.py --day 1 --round morning

# 等待2小时后，触发下午轮
python tools/brain_ab_validation.py --day 1 --round afternoon

# 等待4小时后，触发夜间轮
python tools/brain_ab_validation.py --day 1 --round night
```

**选项B: 自动全天3轮**（无需人工干预）
```bash
# 一次性触发全天3轮（会自动等待）
nohup python tools/brain_ab_validation.py --day 1 --all-rounds > \
  logs/brain_ab_day1_$(date +%F).log 2>&1 &

# 查看进度
tail -f logs/brain_ab_day1_*.log
```

### Step 3: 每小时检查进度

```bash
# 查看当天的Brain报告
python tools/daily_brain_report.py

# 或指定日期
python tools/daily_brain_report.py --date 2025-11-07
```

**关键指标**:
- Brain-influenced jobs: 目标 ≥3（每轮1个）
- Success rate lift: 目标 >10%
- Non-regression blocks: 目标 ≤5%

---

## 📅 3天验证计划

### Day 1（今天 - 2025-11-07）

**目标**: 小窗口baseline + Brain调整

**测试配置**:
- Brain路径: `window_days=7` → Brain预期建议 `extend_window(30-45)`
- 基线路径: `window_days=30` (固定)

**执行**:
```bash
# 上午 (推荐: 10:00 AM)
python tools/brain_ab_validation.py --day 1 --round morning

# 下午 (推荐: 2:00 PM)
python tools/brain_ab_validation.py --day 1 --round afternoon

# 夜间 (推荐: 9:00 PM)
python tools/brain_ab_validation.py --day 1 --round night
```

**预期结果**:
- Brain路径: 成功率 ~80%（Brain会建议扩展窗口）
- 基线路径: 成功率 ~70%（30天窗口）
- 累计Brain样本: 3个

**检查点**:
```bash
# 晚上10点前运行日报
python tools/daily_brain_report.py --date 2025-11-07
```

---

### Day 2（明天 - 2025-11-08）

**目标**: 中窗口 + Brain微调

**测试配置**:
- Brain路径: `window_days=30` → Brain可能维持或微调
- 基线路径: `window_days=30` (固定)

**执行**:
```bash
python tools/brain_ab_validation.py --day 2 --round morning
python tools/brain_ab_validation.py --day 2 --round afternoon
python tools/brain_ab_validation.py --day 2 --round night
```

**预期结果**:
- Brain路径: 成功率 ~85%（微调优化）
- 基线路径: 成功率 ~70%
- 累计Brain样本: 6个

**检查点**:
```bash
# 对比两天数据
python tools/daily_brain_report.py --date 2025-11-08
python tools/validate_brain_effectiveness.py --days 2
```

**决策点**:
- 如果Brain成功率 >80%: ✅ 继续，可降低`conf_floor`至0.50
- 如果50-80%: ⚠️ 继续观察，保持0.55
- 如果<50%: ❌ 提高`conf_floor`至0.70，重新评估

---

### Day 3（后天 - 2025-11-09）

**目标**: 大窗口 + Brain纠正

**测试配置**:
- Brain路径: `window_days=60` → Brain预期建议 `reduce_window(30-45)`
- 基线路径: `window_days=30` (固定)

**执行**:
```bash
python tools/brain_ab_validation.py --day 3 --round morning
python tools/brain_ab_validation.py --day 3 --round afternoon
python tools/brain_ab_validation.py --day 3 --round night
```

**预期结果**:
- Brain路径: 成功率 ~80%（Brain会纠正大窗口问题）
- 基线路径: 成功率 ~70%
- 累计Brain样本: 9个

**最终验证**:
```bash
# 生成最终报告
python tools/validate_brain_effectiveness.py --days 3 \
  --output reports/brain_final_$(date +%F).json

# 查看汇总
cat reports/brain_final_*.json | jq .
```

**通过标准**:
- [x] Brain样本 N≥10
- [x] Brain成功率 > 基线+10% (例: 85% vs 70%)
- [x] 非回归拦截率 ≤5%
- [x] 无循环建议（同一job不被反复调整）

---

## 📊 监控与调试

### 实时监控

**查看Consumer日志**:
```bash
tail -f logs/control_event_consumer_*.log | grep -E "(ml_action|confidence|brain)"
```

**查看ML事件**:
```bash
# 查看今天的事件
cat runs/ai_trader/$(date +%F)/ml_events.jsonl | jq .

# 统计成功率
cat runs/ai_trader/$(date +%F)/ml_events.jsonl | \
  jq -s '[.[] | select(.status=="completed")] | length'
```

**查看Brain动作**:
```bash
# 查看执行的动作
cat runs/ai_trader/$(date +%F)/ml_actions.jsonl | \
  jq 'select(.status=="ok")'

# 统计动作类型
cat runs/ai_trader/$(date +%F)/ml_actions.jsonl | \
  jq -r .action | sort | uniq -c
```

### 调试命令

**检查非回归保护**:
```bash
# 查看被拦截的尝试
cat runs/ai_trader/$(date +%F)/ml_events.jsonl | \
  jq 'select(.reason | contains("regression"))'
```

**检查Brain建议被跳过的原因**:
```bash
# 查看低置信度跳过
grep "ml_action_skipped_low_confidence" logs/control_event_consumer_*.log | tail -10
```

**手动触发单个job（测试）**:
```bash
# 小窗口（Brain预期建议扩展）
python tools/nightly_oof_flow.py \
  --symbols TSLA,AAPL \
  --start "2025-10-31 09:30:00" \
  --end "2025-11-06 16:00:00" \
  --window-days 7 \
  --quick --max-iters 2

# 大窗口（Brain预期建议缩短）
python tools/nightly_oof_flow.py \
  --symbols TSLA \
  --start "2025-09-01 09:30:00" \
  --end "2025-11-06 16:00:00" \
  --window-days 60 \
  --quick --max-iters 2
```

---

## 🚨 应急处理

### Brain表现异常（成功率<50%）

**1. 提高置信度阈值**:
```yaml
# config.yml
policies:
  ml:
    actions_conf_floor: 0.70  # 从0.55提高到0.70
```

**2. 重启consumer**:
```bash
pkill -f control_event_consumer.py
PYTHONPATH=. nohup python scripts/ai_trader/control_event_consumer.py > \
  logs/control_event_consumer_emergency_$(date +%F_%H%M%S).log 2>&1 &
```

**3. 检查Brain逻辑**:
```bash
# 查看Brain的诊断和建议
grep -A 5 "brain_review" logs/control_event_consumer_*.log | tail -50
```

### 非回归拦截率过高（>20%）

**原因**: Brain建议的参数反而导致性能下降

**处理**:
1. 检查被拦截的案例:
   ```bash
   cat runs/ai_trader/$(date +%F)/ml_events.jsonl | \
     jq 'select(.reason | contains("regression"))' | head -3
   ```

2. 如果是误拦截（baseline本身质量差），可放宽容忍度:
   ```yaml
   # config.yml
   policies:
     apply:
       non_regression_tolerance:
         profit_factor_max_drop: 0.05  # 允许5%下降
         sharpe_max_drop: 0.05
         total_return_max_drop: 0.05
   ```

### Job频繁失败（整体成功率<60%）

**可能原因**: 数据质量问题，而非Brain问题

**排查**:
```bash
# 1. 检查IC=nan情况（应该已修复）
cat runs/ai_trader/$(date +%F)/ml_events.jsonl | \
  jq 'select(.metrics.fusion_ic == null or (.metrics.fusion_ic | tostring | contains("nan")))'

# 2. 检查数据清洗效果
grep "Dropped.*rows" logs/nightly_oof_*.log | tail -10

# 3. 手动运行数据清洗
python tools/recompute_labels_1m.py \
  --symbols TSLA,AAPL \
  --start "2025-10-01" \
  --end "2025-11-06" \
  --max-abs-ret 0.15  # 尝试更严格的过滤
```

---

## ✅ 验证通过标准

### Level 1授权标准

**必须满足ALL以下条件**:

1. **样本充足** ✅
   - Brain影响的job ≥10个
   - 验证周期 ≥3天

2. **效果显著** ✅
   - Brain成功率 > 基线+10%
   - 例: Brain 85% vs 基线 70% = +15% ✅

3. **无误报** ✅
   - 非回归拦截率 ≤5%
   - 无循环建议（同一job不被反复调整）

4. **稳定性** ✅
   - 无consumer崩溃
   - 无数据损坏
   - 日志正常

### 通过后的行动

**如果验证通过**:

1. **记录结果**:
   ```bash
   # 生成最终报告
   python tools/validate_brain_effectiveness.py --days 3 \
     --output reports/brain_level1_passed_$(date +%F).json

   # 备份配置
   cp config.yml config_brain_level1_$(date +%F).yml.bak
   ```

2. **授权Brain Level 1**:
   ```yaml
   # config.yml
   policies:
     ml:
       actions_conf_floor: 0.50  # 进一步降低，让Brain更主动
       brain_level: 1             # 标记为Level 1
       brain_authorized_actions:  # 明确授权
         - extend_window
         - reduce_window
         - queue_job
         - backfill
   ```

3. **启用持续监控**:
   ```bash
   # 添加cron job（每日报告）
   echo "0 22 * * * cd . && python tools/daily_brain_report.py" | crontab -

   # 或使用launchd (macOS)
   # 见附录A
   ```

4. **开始Level 2准备**:
   - 设计阈值调优方案
   - 准备A/B测试框架
   - 建立Sharpe/MDD监控

**如果验证未通过**:

1. **保持Level 0**（仅记录，不执行）:
   ```yaml
   # config.yml
   policies:
     ml:
       actions_conf_floor: 0.95  # 实际上禁用
   ```

2. **分析失败原因**:
   ```bash
   python tools/validate_brain_effectiveness.py --days 3 --debug
   ```

3. **修复Brain逻辑** (如需要):
   - 审查 `control_event_consumer.py:1748-1786` Brain决策逻辑
   - 调整阈值或规则
   - 重新验证

---

## 📎 附录

### A. 持续监控设置（macOS launchd）

创建 `~/Library/LaunchAgents/com.trading.brain_daily_report.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.trading.brain_daily_report</string>
    <key>ProgramArguments</key>
    <array>
        <string>./venv/bin/python</string>
        <string>./tools/daily_brain_report.py</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>22</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>./logs/brain_daily_report.log</string>
    <key>StandardErrorPath</key>
    <string>./logs/brain_daily_report.err</string>
</dict>
</plist>
```

加载:
```bash
launchctl load ~/Library/LaunchAgents/com.trading.brain_daily_report.plist
```

### B. 配置快照

**验证期配置** (`config.yml`):
```yaml
policies:
  ml:
    actions_conf_floor: 0.55  # 验证期
  apply:
    non_regression_tolerance:
      profit_factor_max_drop: 0.0
      sharpe_max_drop: 0.0
      total_return_max_drop: 0.0
```

**Level 1授权配置** (验证通过后):
```yaml
policies:
  ml:
    actions_conf_floor: 0.50  # Level 1
    brain_level: 1
  apply:
    non_regression_tolerance:
      profit_factor_max_drop: 0.05  # 可适当放宽
      sharpe_max_drop: 0.05
      total_return_max_drop: 0.05
```

---

**最后更新**: 2025-11-07
**负责人**: Trading Team
**相关文档**:
- `AI_BRAIN_CAPABILITY_ASSESSMENT.md` - 能力评估
- `BRAIN_VALIDATION_PLAN.md` - 完整验证计划
- `QUICKSTART_BRAIN_VALIDATION.md` - 快速启动
