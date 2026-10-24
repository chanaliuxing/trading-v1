# Brain效果验证方案

## 📊 当前证据（2025-11-07）

### 初步数据（N=2，统计不显著）

```
Brain影响的job: 100% 成功率 (2/2)
  - IC=0.1102, AUC=0.578

基线job（无Brain）: 16.7% 成功率 (2/12)
  - 平均IC=0.0590, AUC=0.520

差异: +83.3% (但样本太少，需要N≥5)
```

**结论**: 初步数据**极其乐观**，但需要更多样本才能确认。

---

## 🧪 3天快速验证计划

### 目标
在3天内收集至少**10个Brain影响的job**，达到统计显著性。

### Day 1（今天）: 基础配置

1. **降低动作阈值** - 让Brain更主动
   ```yaml
   # config.yml
   policies:
     ml:
       actions_conf_floor: 0.50  # 从0.6降到0.50
   ```

2. **重启consumer**
   ```bash
   pkill -f control_event_consumer.py
   PYTHONPATH=. nohup python scripts/ai_trader/control_event_consumer.py > \
     logs/control_event_consumer_$(date +%F_%H%M%S).log 2>&1 &
   ```

3. **触发5个测试job**（模拟不同场景）
   ```bash
   # Job 1: 短窗口（7天）- 预期成功
   python tools/nightly_oof_flow.py --symbols TSLA,AAPL \
     --start "2025-10-31 09:30:00" --end "2025-11-06 16:00:00" \
     --window-days 7 --quick --max-iters 2

   # Job 2: 中窗口（30天）- Brain可能建议调整
   python tools/nightly_oof_flow.py --symbols NVDA,SPY \
     --start "2025-10-07 09:30:00" --end "2025-11-06 16:00:00" \
     --window-days 30 --quick --max-iters 2

   # Job 3: 大窗口（60天）- Brain可能建议缩短
   python tools/nightly_oof_flow.py --symbols META,AMZN \
     --start "2025-09-07 09:30:00" --end "2025-11-06 16:00:00" \
     --window-days 60 --quick --max-iters 2

   # Job 4: 混合（让Brain自由发挥）
   python tools/nightly_oof_flow.py --symbols GOOGL,MSFT \
     --start "2025-10-15 09:30:00" --end "2025-11-06 16:00:00" \
     --quick --max-iters 2

   # Job 5: 单一股票高质量
   python tools/nightly_oof_flow.py --symbols TSLA \
     --start "2025-10-25 09:30:00" --end "2025-11-06 16:00:00" \
     --window-days 12 --quick --max-iters 2
   ```

4. **每小时检查进度**
   ```bash
   python tools/validate_brain_effectiveness.py --days 1
   ```

### Day 2: 分析与调优

1. **晨报**（09:00）
   ```bash
   python tools/validate_brain_effectiveness.py --days 2 \
     --output reports/brain_validation_day2.json
   ```

2. **如果Brain表现好（成功率>80%）**
   - ✅ 继续当前配置
   - ✅ 可选：降低conf_floor至0.45
   - ✅ 触发更多自动job

3. **如果Brain表现一般（成功率50-80%）**
   - ⚠️ 检查Brain诊断是否准确
   - ⚠️ 分析失败case的共同特征
   - ⚠️ 考虑调整Brain的建议逻辑

4. **如果Brain表现差（成功率<50%）**
   - ❌ 提高conf_floor至0.70
   - ❌ 审查Brain代码逻辑
   - ❌ 暂停自动执行，改为人工审核

### Day 3: 最终评估

1. **完整报告**（09:00）
   ```bash
   python tools/validate_brain_effectiveness.py --days 3 \
     --output reports/brain_validation_final.json
   ```

2. **决策矩阵**

| Brain成功率 | 基线成功率 | 提升 | 结论 | 行动 |
|------------|-----------|------|------|------|
| >90% | <80% | >10% | 🎉 Brain非常有效 | 扩展至Level 2权限 |
| 80-90% | <80% | >5% | ✅ Brain有效 | 保持配置，继续观察 |
| 70-80% | ~70% | <5% | ⚠️ Brain效果不明显 | 调查原因，优化逻辑 |
| <70% | >70% | 负值 | ❌ Brain有害 | 禁用或大幅提高阈值 |

---

## 📈 关键验证指标

### 主指标（必须改善）
1. **成功率**: Brain影响的job成功率 vs 基线
2. **IC提升**: Brain建议后的平均IC vs 基线平均IC
3. **AUC提升**: Brain建议后的平均AUC vs 基线平均AUC

### 辅助指标（参考）
4. **动作准确率**: Brain建议的动作是否对症
5. **响应时间**: Brain建议到执行的延迟
6. **误报率**: Brain建议但无效的动作比例

### 定性指标（人工评估）
7. **诊断质量**: Brain的diagnosis是否准确描述问题
8. **建议合理性**: Brain的next_actions是否符合直觉
9. **稳定性**: Brain是否会陷入循环建议（如反复extend_window）

---

## 🔬 高级验证方法（可选）

### A/B测试框架

如果有足够的job流量，可以进行严格的A/B测试：

```python
# scripts/ai_trader/control_event_consumer.py

def _should_execute_brain_action(action, job_id):
    """A/B测试：随机50%执行Brain建议"""
    import hashlib

    # 用job_id作为随机种子，确保可复现
    seed = int(hashlib.md5(job_id.encode()).hexdigest()[:8], 16)

    # 50% control group, 50% treatment group
    if seed % 2 == 0:
        logger.info("ab_test_control", job_id=job_id, action=action)
        return False  # Control: 不执行Brain建议
    else:
        logger.info("ab_test_treatment", job_id=job_id, action=action)
        return True  # Treatment: 执行Brain建议
```

**优点**:
- 科学严谨，排除混杂因素
- 可计算统计显著性（p-value）

**缺点**:
- 需要2倍样本量
- 可能错过50%的优化机会

---

## 🚨 风险控制

### 自动熔断机制

如果Brain表现异常，自动禁用：

```python
# scripts/ai_trader/control_event_consumer.py

def _check_brain_health(recent_actions, recent_jobs):
    """检查Brain健康度，异常时熔断"""

    # 统计最近10个Brain影响的job
    if len(recent_jobs) < 10:
        return True  # 样本不足，继续观察

    success_rate = sum(j['ok'] for j in recent_jobs) / len(recent_jobs)

    # 熔断条件：成功率低于30%
    if success_rate < 0.3:
        logger.error("brain_circuit_breaker_triggered",
                    success_rate=success_rate,
                    recent_jobs=len(recent_jobs))

        # 写入熔断标记
        (RUNS / "brain_disabled.flag").write_text(
            f"Circuit breaker triggered: success_rate={success_rate:.1%}"
        )
        return False

    return True
```

### 人工审核模式

在Brain不稳定时，改为人工审核：

```yaml
# config.yml
policies:
  ml:
    actions_conf_floor: 0.90  # 只接受非常高置信度的建议
    require_manual_approval: true  # 需要人工在Slack确认
```

---

## 📊 实时监控Dashboard

### Grafana面板建议

```promql
# Brain动作执行率
rate(brain_actions_executed_total[1h])

# Brain影响job的成功率
brain_influenced_jobs_success_rate

# Brain vs 基线成功率差异
brain_success_rate - baseline_success_rate

# Brain建议类型分布
sum by (action_type) (brain_actions_executed_total)
```

### Slack告警

关键事件实时通知：

```python
# Brain建议高价值动作
if action == "extend_window" and confidence > 0.85:
    slack_notify("Brain建议扩大窗口（高置信度）", job_details)

# Brain熔断
if brain_circuit_breaker_triggered:
    slack_notify("⚠️ Brain熔断！成功率过低", health_report)

# Brain异常建议
if action == "extend_window" and window_days > 90:
    slack_notify("⚠️ Brain建议异常窗口（>90天）", action_details)
```

---

## 📝 验证清单

在宣布"Brain有效"之前，确保：

- [ ] 至少10个Brain影响的job（统计显著性）
- [ ] Brain成功率明显高于基线（>10%差异）
- [ ] 没有明显的误报case（诊断准确）
- [ ] Brain不会陷入循环建议
- [ ] 动作执行无副作用（不影响其他系统）
- [ ] 有回滚预案（可快速禁用）
- [ ] 监控和告警已就位

---

## 🎯 最终决策树

```
开始
 │
 ├─ Brain样本 < 10?
 │   └─ Yes → 继续收集数据（Day 1-2）
 │   └─ No → 进入评估
 │
 ├─ Brain成功率 > 基线 + 10%?
 │   ├─ Yes → Brain有效
 │   │   └─ Brain稳定（无循环/误报）?
 │   │       ├─ Yes → ✅ 正式启用，扩展权限
 │   │       └─ No → ⚠️ 优化逻辑后再试
 │   │
 │   └─ No → Brain效果不佳
 │       └─ Brain成功率 < 基线?
 │           ├─ Yes → ❌ 禁用Brain
 │           └─ No → ⚠️ 继续观察或调优
```

---

## 📚 参考

- **验证工具**: `tools/validate_brain_effectiveness.py`
- **日志**: `runs/ai_trader/*/ml_actions.jsonl`, `ml_events.jsonl`
- **配置**: `config.yml:policies.ml`
- **Brain代码**: `scripts/ai_trader/control_event_consumer.py:_handle_ml_completed`
