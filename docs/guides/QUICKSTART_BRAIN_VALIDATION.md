# Brain效果验证 - 5分钟快速启动

## 📊 当前状态（诚实评估）

```bash
✅ 代码修复完成度: 100% (7/7 P0 bugs)
✅ OOF成功率: 79.5% (vs 31% before fixes)
⚠️  Brain样本量: 2 (需要 ≥10 for 统计显著性)

初步证据（N=2，不显著）:
  Brain影响job: 100% 成功 (IC=0.1102)
  基线job:      16.7% 成功 (IC=0.0590)
  差异:        +83.3% ⬆️
```

**结论**: 初步数据**极其乐观**，但需要更多样本。

---

## 🚀 30秒快速验证

```bash
# 1. 运行验证工具
python tools/validate_brain_effectiveness.py --days 1

# 2. 如果样本不足，触发测试job
python tools/nightly_oof_flow.py --symbols TSLA,AAPL \
  --start "2025-10-31 09:30:00" --end "2025-11-06 16:00:00" \
  --window-days 7 --quick --max-iters 2

# 3. 等待4-5小时后再次验证
python tools/validate_brain_effectiveness.py --days 1
```

---

## 🎯 3天完整验证计划

### Day 1: 降低阈值 + 触发5个测试job

```bash
# 步骤1: 降低动作阈值（让Brain更主动）
# 编辑 config.yml:
#   policies:
#     ml:
#       actions_conf_floor: 0.50  # 从0.6降到0.50

# 步骤2: 重启consumer
pkill -f control_event_consumer.py
PYTHONPATH=. nohup python scripts/ai_trader/control_event_consumer.py > \
  logs/control_event_consumer_$(date +%F_%H%M%S).log 2>&1 &

# 步骤3: 触发5个测试job（不同窗口大小）
./scripts/trigger_brain_test_jobs.sh  # 见下方
```

**测试job脚本** (`scripts/trigger_brain_test_jobs.sh`):
```bash
#!/bin/bash
# 短窗口（7天）- 预期成功
python tools/nightly_oof_flow.py --symbols TSLA,AAPL \
  --start "2025-10-31 09:30:00" --end "2025-11-06 16:00:00" \
  --window-days 7 --quick --max-iters 2 &

sleep 300  # 5分钟间隔

# 中窗口（30天）- Brain可能调整
python tools/nightly_oof_flow.py --symbols NVDA,SPY \
  --start "2025-10-07 09:30:00" --end "2025-11-06 16:00:00" \
  --window-days 30 --quick --max-iters 2 &

sleep 300

# 大窗口（60天）- Brain可能缩短
python tools/nightly_oof_flow.py --symbols META \
  --start "2025-09-07 09:30:00" --end "2025-11-06 16:00:00" \
  --window-days 60 --quick --max-iters 2 &

wait
```

### Day 2: 分析 + 决策

```bash
# 晨报（09:00）
python tools/validate_brain_effectiveness.py --days 2 \
  --output reports/brain_day2.json

# 根据结果决定:
# - 成功率 >80%: 继续，可降低conf_floor至0.45
# - 成功率 50-80%: 分析原因，调优
# - 成功率 <50%: 提高conf_floor至0.70，审查逻辑
```

### Day 3: 最终评估

```bash
# 完整报告
python tools/validate_brain_effectiveness.py --days 3 \
  --output reports/brain_final.json

# 决策树：
# Brain成功率 > 基线+10% → ✅ 正式启用，扩展权限
# Brain成功率 > 基线+5%  → ✅ 保持配置，继续观察
# Brain成功率 ≈ 基线     → ⚠️ 调查原因，优化逻辑
# Brain成功率 < 基线     → ❌ 禁用Brain
```

---

## 📈 关键指标解读

### 看什么？

```bash
python tools/validate_brain_effectiveness.py --days 1
```

**重点关注**:
1. **Brain成功率 vs 基线**: 差异>10%才算有效
2. **样本量**: N≥10才有统计意义
3. **IC/AUC提升**: Brain建议后是否真的变好

### 好的信号 ✅

```
✅ Brain影响job: 成功率 90%+
✅ 基线job: 成功率 <80%
✅ IC提升: Brain建议后 IC > 0.08
✅ 无误报: Brain诊断准确
```

### 坏的信号 ❌

```
❌ Brain影响job: 成功率 <70%
❌ Brain建议后: IC反而下降
❌ 循环建议: 反复extend_window
❌ 高误报: Brain诊断与实际不符
```

---

## 🔧 快速调优

### 如果Brain太保守（动作太少）

```yaml
# config.yml
policies:
  ml:
    actions_conf_floor: 0.45  # 降低阈值
```

### 如果Brain太激进（误报多）

```yaml
# config.yml
policies:
  ml:
    actions_conf_floor: 0.70  # 提高阈值
```

### 如果Brain建议不合理

检查Brain的诊断逻辑:
```bash
# 查看最近的Brain审查
cat runs/ai_trader/$(date +%F)/ml_review.json | jq .

# 查看Brain动作历史
cat runs/ai_trader/$(date +%F)/ml_actions.jsonl | jq 'select(.executed_at)'
```

---

## 🚨 紧急回滚

如果Brain表现异常，立即禁用：

```yaml
# config.yml
policies:
  ml:
    actions_conf_floor: 0.95  # 几乎禁用
```

或直接删除Brain审查逻辑（临时）：
```bash
# 编辑 control_event_consumer.py
# 注释掉 _handle_ml_completed 中的Brain调用
```

---

## 📊 实时监控

### 每小时检查

```bash
# 定时任务（cron）
0 * * * * cd . && \
  python tools/validate_brain_effectiveness.py --days 1 > \
  logs/brain_hourly_$(date +\%F_\%H).log 2>&1
```

### 关键文件监控

```bash
# Brain动作日志
tail -f runs/ai_trader/$(date +%F)/ml_actions.jsonl

# OOF job结果
tail -f runs/ai_trader/$(date +%F)/ml_events.jsonl | grep completed

# Consumer日志（Brain审查）
tail -f logs/control_event_consumer*.log | grep brain
```

---

## 🎓 进阶：A/B测试

如果job流量足够（每天>20个），可以做严格A/B测试：

```python
# scripts/ai_trader/control_event_consumer.py:_execute_ml_actions

# 随机50%执行Brain建议
import hashlib
seed = int(hashlib.md5(job_id.encode()).hexdigest()[:8], 16)
if seed % 2 == 0:
    logger.info("ab_test_control", job_id=job_id)
    continue  # 跳过Brain建议
else:
    logger.info("ab_test_treatment", job_id=job_id)
    # 正常执行
```

然后对比control vs treatment组的成功率。

---

## 📚 文档

- **完整验证方案**: `docs/BRAIN_VALIDATION_PLAN.md`
- **验证工具**: `tools/validate_brain_effectiveness.py --help`
- **Brain代码**: `scripts/ai_trader/control_event_consumer.py`
- **配置文件**: `config.yml`

---

## ✅ 检查清单

宣布"Brain有效"前确保：

- [ ] Brain样本 ≥10
- [ ] Brain成功率 > 基线 + 10%
- [ ] 无明显误报
- [ ] 无循环建议
- [ ] 有监控和告警
- [ ] 有回滚预案

---

## 💡 快速FAQ

**Q: Brain建议后多久能看到效果？**
A: 4-6小时（job运行时间）

**Q: 需要多少样本才能确认有效？**
A: 至少10个Brain影响的job（N≥10）

**Q: 如果Brain表现不好怎么办？**
A: 提高`actions_conf_floor`至0.70或直接禁用

**Q: Brain会不会搞坏系统？**
A: 不会。Brain只是建议参数，不会修改代码或删除数据

**Q: 可以让Brain管理更多参数吗？**
A: 可以！验证成功后可扩展至Level 2（阈值调优、特征选择）

---

## 🎯 今天就开始！

```bash
# 1. 验证当前状态
python tools/validate_brain_effectiveness.py --days 1

# 2. 如果样本不足，触发测试
python tools/nightly_oof_flow.py --symbols TSLA,AAPL \
  --start "2025-10-31 09:30:00" --end "2025-11-06 16:00:00" \
  --window-days 7 --quick --max-iters 2

# 3. 明天早上查看结果
python tools/validate_brain_effectiveness.py --days 1
```

**预期**: 如果初步数据成立，3天后你会看到Brain将成功率从79.5%提升至90%+！
