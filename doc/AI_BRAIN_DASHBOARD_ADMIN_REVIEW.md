# AI Brain Detailed Dashboard - 管理员视角评审

**评审时间**: 2025-11-02 02:06 EST
**评审人**: 管理员视角 (Trading System Administrator)
**Dashboard**: http://localhost:3000/d/ai-brain-detailed/ai-brain-detailed-analysis

---

## 🎯 管理员关注点

作为管理员,我需要回答以下问题:
1. **数据新鲜度**: AI 大脑最近在运行吗?多久没活动了?
2. **完整性**: 所有 panel 都显示数据吗?有报错吗?
3. **可操作性**: 我能看懂 AI 在做什么吗?能据此做决策吗?
4. **告警**: 有什么异常需要立即关注吗?

---

## ❌ 发现的关键问题

### 问题 1: 🚨 **数据已停止 11.3 小时** (Critical)

**现状**:
- 最后一次 `propose_plan` 调用: **2025-11-01 14:00** (11.3小时前)
- 最后一次 `decide` 调用: **2025-11-01 14:06** (11.3小时前)
- 最后一次 `evaluate` 调用: **2025-10-31 14:19** (35.3小时前)

**统计数据**:
```
kind         | calls | avg_duration_ms | last_call           | hours_ago
-------------|-------|-----------------|---------------------|----------
decide       |    16 |          3,112  | 2025-11-01 14:06   |  11.3
propose_plan |   128 |         25,566  | 2025-11-01 14:00   |  11.3
evaluate     |     4 |         23,042  | 2025-10-31 14:19   |  35.3
```

**管理员第一反应**:
> ⚠️ "AI 大脑已经停止工作 11 小时了! 这是系统故障还是计划性停机?"

**影响**:
- ❌ Dashboard 显示的都是 **过时的分析结果**
- ❌ 无法了解 **当前** AI 大脑的想法
- ❌ 无法判断 AI 是否在正常监控系统

**需要立即回答的问题**:
1. AI Brain orchestrator 是否在运行?
2. 为什么停止调用 LLM?
3. 是否有错误日志?
4. 这 11 小时里系统发生了什么?

---

### 问题 2: 📊 **Panel 缺少数据新鲜度指示器** (High Priority)

**现状**: 所有 panels 都没有明确标注 "数据最后更新时间"

**问题场景**:
- 管理员打开 dashboard,看到 "系统诊断: 系统刚初始化,无交易历史数据"
- **但这是 11 小时前的诊断!** 现在情况可能已经完全不同
- 管理员无法判断: "这是现在的状态,还是历史快照?"

**缺失的关键信息**:
```
❌ 当前显示:
   诊断: "系统刚初始化,无交易历史数据"

✅ 应该显示:
   诊断: "系统刚初始化,无交易历史数据"
   ⚠️ 数据时间: 2025-11-01 14:00 (11.3 小时前)
   🚨 警告: 数据已过期,AI 大脑可能已停止运行
```

**用户体验问题**:
- 管理员可能误以为这是 **实时** 诊断
- 可能基于 **过时信息** 做决策
- 无法快速识别 AI 大脑是否异常

---

### 问题 3: 🔍 **JSON 显示不友好** (Medium Priority)

**现状**: Panel 5 (AI 建议方案) 显示的是一大段 JSON 字符串

**实际显示效果**:
```json
prescription_json: "[{\"action\":\"enable_baseline_data_collection\",\"params\":{\"providers\":[\"deepseek\",\"gemini\"],\"symbols\":[\"AAPL\",\"TSLA\"],\"interval_sec\":600,\"ab_split\":{\"champion\":0.7,\"challenger\":0.3}},\"confidence\":0.9},{\"action\":\"activate_safe_mode_with_limits\",\"params\":{\"restrictions\":[\"no_new_positions\",\"reduce_sizing\"],\"max_symbols\":2},\"confidence\":0.85},{\"action\":\"verify_system_health_before_live\",\"params\":{\"require_uptime_check\":true,\"max_api_latency\":1500},\"confidence\":0.8}]"
```

**问题**:
- ❌ 难以阅读 (escaped JSON 字符串)
- ❌ 无法快速扫描关键信息
- ❌ 需要手动复制粘贴到 JSON formatter

**期望效果**:
- ✅ 使用 **Table** 展示 prescription 数组
- ✅ 每个 action 一行,显示置信度、参数
- ✅ 支持排序、过滤

---

### 问题 4: ⏱️ **缺少时间线可视化** (Medium Priority)

**现状**: Row 5 只有一个堆叠柱状图显示调用频率

**管理员需求**:
- 我想看 AI 的 **决策演变过程**:
  - 第一次诊断: "系统刚初始化" → 建议启用数据收集
  - 第二次诊断: "数据不足" → 建议保守设置
  - 第三次诊断: "性能下降" → 建议调整参数
- 我想看 **diagnosis 的变化趋势**:
  - 什么时候从 "new_system_init" 转变为 "normal_operation"?
  - 有没有出现过 "risk_threshold_breached"?

**缺失的可视化**:
- ❌ 没有 diagnosis 类型的时间序列图
- ❌ 没有 confidence 的趋势图
- ❌ 没有 AI 决策效果的反馈循环可视化

---

### 问题 5: 🎯 **缺少可操作性建议** (Low Priority)

**现状**: Dashboard 只显示 AI 的输出,没有给管理员提供 "下一步怎么办"

**场景**:
- 管理员看到 AI 建议: `"action": "activate_safe_mode_with_limits"`
- **但不知道**:
  - ✅ 这个建议已经被采纳了吗? (有没有 apply)
  - ✅ 如果采纳,效果如何? (有没有 evaluate)
  - ✅ 我应该手动干预吗?

**缺失的关联分析**:
- ❌ 没有 `propose_plan` → `decide` → `evaluate` 的完整流程追踪
- ❌ 没有 "建议采纳率" 统计
- ❌ 没有 "AI 决策效果评分" 趋势

---

## ✅ 做得好的地方

### 1. **数据完整性** ✅
- 所有 AI 调用都被正确记录到 `llm.ai_brain_calls`
- `output_json` 字段包含完整的 AI 输出
- 数据结构清晰 (diagnosis, prescription, apply, notes)

### 2. **查询设计** ✅
- Panel 7 (配置变更) 的查询设计很好,能正确解析 `apply` 数组
- Panel 8 (推理说明) 直接显示 `notes`,简洁易懂
- Panel 10 (完整历史) 使用 `json-view` cell type,支持展开

### 3. **文档质量** ✅
- `AI_BRAIN_DETAILED_DASHBOARD_README.md` 非常详细 (400+ 行)
- 包含使用场景、故障排查、最佳实践
- 对 AI 调用类型有清晰说明

---

## 🚨 紧急行动项 (管理员需要立即做的)

### 行动 1: 检查 AI Brain Orchestrator 状态
```bash
# 1. 检查进程是否运行
ps aux | grep orchestrator

# 2. 检查最近的错误日志
tail -100 logs/ai_brain_orchestrator.log | grep -i error

# 3. 检查 LLM provider 连接
docker logs trading_system 2>&1 | grep -i "deepseek\|gemini" | tail -20
```

### 行动 2: 确认数据管道健康
```bash
# 检查相关表的最新数据
docker exec trading_clickhouse clickhouse-client --query "
SELECT
    'llm.ai_brain_calls' as table,
    formatDateTime(max(ts_ny), '%Y-%m-%d %H:%M:%S') as last_update,
    round((toUnixTimestamp(now()) - toUnixTimestamp(max(ts_ny))) / 3600, 1) as hours_ago
FROM llm.ai_brain_calls
UNION ALL
SELECT
    'llm.llm_sessions' as table,
    formatDateTime(max(start_ts_ny), '%Y-%m-%d %H:%M:%S') as last_update,
    round((toUnixTimestamp(now()) - toUnixTimestamp(max(start_ts_ny))) / 3600, 1) as hours_ago
FROM llm.llm_sessions
"
```

### 行动 3: 重启 AI Brain (如果需要)
```bash
# 如果确认 orchestrator 停止,重启
cd .
python scripts/ai_trader/orchestrator.py &

# 监控重启后的日志
tail -f logs/ai_brain_orchestrator.log
```

---

## 📋 Dashboard 改进计划 (优先级排序)

### P0: 🚨 **立即修复**

#### 1. 添加数据新鲜度告警 (所有 panels)
**实现**:
- Row 1: 添加 "数据年龄" stat panel (类似 AI Boss 的 Panel 18)
- 如果 `hours_ago > 1`: 显示黄色警告
- 如果 `hours_ago > 6`: 显示红色告警

```sql
-- 新 Panel: Data Freshness Alert
SELECT
    round((toUnixTimestamp(now()) - toUnixTimestamp(max(ts_ny))) / 3600, 1) as hours_since_last_call
FROM llm.ai_brain_calls

-- Thresholds:
-- Green: < 1 hour
-- Yellow: 1-6 hours
-- Red: > 6 hours
```

#### 2. 优化 Panel 5 (Prescription) 显示为 Table
**实现**:
```sql
-- 改进查询: 直接解析 prescription 数组为表格
WITH latest AS (
    SELECT output_json
    FROM llm.ai_brain_calls
    WHERE kind = 'propose_plan'
    ORDER BY ts_ny DESC
    LIMIT 1
)
SELECT
    JSONExtractString(item, 'action') as action,
    JSONExtractFloat(item, 'confidence') as confidence,
    JSONExtract(item, 'params', 'String') as params
FROM (
    SELECT arrayJoin(JSONExtract(output_json, 'prescription', 'Array(String)')) as item
    FROM latest
)
```

**Panel 配置**:
- Type: `table`
- Column: `confidence` → 显示为进度条 (0-1)
- Column: `params` → 可展开的 JSON

---

### P1: ⚡ **本周完成**

#### 3. 添加 Diagnosis 时间线图
**实现**: 新 Panel 显示 diagnosis 类型随时间的变化

```sql
SELECT
    toStartOfInterval(ts_ny, INTERVAL 1 HOUR) as time,
    JSONExtractString(output_json, 'diagnosis') as diagnosis_type,
    count() as occurrences
FROM llm.ai_brain_calls
WHERE kind = 'propose_plan'
  AND ts_ny > now() - INTERVAL 7 DAY
GROUP BY time, diagnosis_type
ORDER BY time
```

**可视化**: 堆叠面积图,颜色映射:
- `normal_operation` → 绿色
- `no_trading_history` → 蓝色
- `performance_degradation` → 黄色
- `risk_threshold_breached` → 红色

#### 4. 添加 "建议 → 决策 → 评估" 流程追踪
**实现**: Sankey 图或 Flow 图显示:
- `propose_plan` (提议) → `decide` (决策) → `evaluate` (评估)
- 每个环节的成功率、延迟

---

### P2: 📅 **本月完成**

#### 5. 添加 AI 决策效果评分
**实现**: 新 Panel 显示 AI 建议的采纳率和效果

```sql
-- 建议采纳率
WITH proposals AS (
    SELECT
        session,
        ts_ny as propose_time,
        JSONExtractArrayRaw(output_json, 'prescription') as prescriptions
    FROM llm.ai_brain_calls
    WHERE kind = 'propose_plan'
),
decisions AS (
    SELECT
        session,
        ts_ny as decide_time,
        JSONExtractArrayRaw(output_json, 'apply') as applies
    FROM llm.ai_brain_calls
    WHERE kind = 'decide'
)
SELECT
    p.session,
    length(p.prescriptions) as recommendations,
    length(d.applies) as actions_taken,
    round(length(d.applies) / length(p.prescriptions), 2) as adoption_rate
FROM proposals p
LEFT JOIN decisions d ON p.session = d.session
WHERE d.decide_time > p.propose_time
  AND d.decide_time < p.propose_time + INTERVAL 10 MINUTE
```

#### 6. 添加交互式过滤器
**实现**: Variables 支持:
- `$provider` (deepseek_nvidia, gemini, etc.)
- `$diagnosis_type` (all, normal_operation, performance_degradation, etc.)
- `$time_range` (1h, 6h, 24h, 7d)

---

## 💡 管理员建议

### 建议 1: **设置 Grafana Alert**
如果 AI 大脑停止超过 2 小时,自动发送告警:

```yaml
# Grafana Alert Rule
name: "AI Brain Stopped"
condition: hours_since_last_call > 2
notification:
  - Slack: #trading-alerts
  - Email: admin@trading.com
```

### 建议 2: **定期导出 AI 决策日志**
```bash
# 每周导出 AI 的所有决策用于审计
docker exec trading_clickhouse clickhouse-client --query "
SELECT * FROM llm.ai_brain_calls
WHERE toDate(ts_ny) >= today() - 7
FORMAT CSV
" > ai_brain_decisions_$(date +%Y%m%d).csv
```

### 建议 3: **建立 AI 决策审查机制**
- 每天早晨查看 AI Brain Dashboard
- 检查 AI 的诊断是否合理
- 验证 AI 的决策是否有效
- 必要时手动覆盖 AI 的配置

---

## 📊 总结评分

| 维度 | 评分 | 说明 |
|------|------|------|
| **数据完整性** | 5/5 ⭐⭐⭐⭐⭐ | 所有 AI 输出都被完整记录 |
| **数据新鲜度** | 1/5 ⭐ | 数据停止 11 小时,严重过期 |
| **可读性** | 3/5 ⭐⭐⭐ | 部分 JSON 显示不友好 |
| **可操作性** | 2/5 ⭐⭐ | 缺少告警和决策建议 |
| **可视化** | 3/5 ⭐⭐⭐ | 基础可视化到位,缺少高级分析 |
| **文档质量** | 5/5 ⭐⭐⭐⭐⭐ | README 非常详细 |

**总体评分**: **3.2/5 ⭐⭐⭐**

**一句话总结**:
> Dashboard 结构设计良好,数据完整,但 **数据已过期 11 小时**,且缺少新鲜度告警和可操作性建议。需要 **立即** 检查 AI Brain orchestrator 状态并添加数据新鲜度监控。

---

**评审完成时间**: 2025-11-02 02:10 EST
**下一步行动**: 执行紧急行动项 1-3,然后实施 P0 改进计划
