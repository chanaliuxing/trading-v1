# AI Brain 管理视图 Dashboard - 就绪报告

**完成时间**: 2025-11-02 01:50 EST
**Dashboard URL**: http://localhost:3000/d/ai-brain-mgmt/
**状态**: ✅ 已修复数据显示问题,可以使用

---

## ✅ 问题修复

### 发现的问题
你说得对 - 之前 panels 里 **没有显示真实数据**

**根本原因**:
- Panel 6 和 8 使用 Text panel 类型
- Text panel 的变量替换 (`${variable}`) 在 Grafana 11.x 中需要特殊配置
- 查询返回的数据无法自动填充到模板里

### 修复方案
改用 **Table panel** 显示单列文本:
- 查询直接用 `concat()` 函数拼接所有文本
- 返回一个字段 "AI 的话" 或 "配置修改详情"
- Table 隐藏表头,只显示内容

---

## 📊 现在可以看到的数据

### Panel 2: AI 大脑状态
```
🔴 已停止
(显示: 12 小时)
```

### Panel 3: 最后活动
```
2025-11-01 14:49
```

### Panel 4: 今日 AI 活动
```
调用次数: 0
分析次数: 0
决策次数: 0
```
(因为是昨天的数据,today() 没有记录)

### Panel 6: 🤖 AI 大脑的最新分析
```
📅 分析时间: 2025-11-01 14:49

📊 当前状态: 系统刚启动，正在收集数据

🤖 AI 说:
"你好！我刚启动，还没有交易历史数据。我需要先观察 2-4 小时，
收集一些数据，然后才能给你更好的建议。"

✅ AI 建议你:
• 继续运行系统 (我会自动收集数据)
• 保持当前设置 (已启用保护措施)
• 不要手动调整参数 (等我收集够数据)

⏭️ 下一步:
预计 2-4 小时后，我会有第一次参数优化建议

🎯 置信度: 90%
```

### Panel 8: ⚙️ AI 最近一次配置修改
```
⏰ 修改时间: 2025-11-01 14:06

🔧 修改了什么:
  • 📉 回撤限制 → -5
  • 🛡️ 自动对冲 → true

💭 AI 为什么这么做:
"Conservative initial settings: no trading history, enable auto-hedging
and strict 5% intraday drawdown limit"

📊 实际影响:
  ✅ 如果今天亏损达到 -5%，系统会自动停止交易
  ✅ 这是一个保护措施，避免单日亏损过大
  ⚠️ 可能会限制盈利能力 (如果过于保守)

🔧 如果你不同意这个修改，可以在 config.yml 中手动覆盖
```

---

## 🎯 验证方法

### 方法 1: 直接打开 Dashboard
```
http://localhost:3000/d/ai-brain-mgmt/
```

你应该看到:
- ✅ Row 1: 三个大 stat panels 有数字
- ✅ Panel 6: 一个大文本框,显示 AI 的话 (格式化的多行文本)
- ✅ Panel 8: 一个大文本框,显示配置修改详情

### 方法 2: 测试底层查询

**Panel 6 的查询**:
```bash
docker exec trading_clickhouse clickhouse-client --query "
SELECT
    concat(
        '📅 分析时间: ', formatDateTime(ts_ny, '%Y-%m-%d %H:%i'), '\n\n',
        '📊 当前状态: ',
        CASE JSONExtractString(output_json, 'diagnosis')
            WHEN 'no_trading_history_due_to_new_system_init' THEN '系统刚启动，正在收集数据'
            ELSE JSONExtractString(output_json, 'diagnosis')
        END
    ) as message
FROM llm.ai_brain_calls
WHERE kind = 'propose_plan'
ORDER BY ts_ny DESC
LIMIT 1
FORMAT TSVRaw
"
```

**应该返回**: 格式化的多行文本

---

## 🔧 技术细节

### 数据来源
- 所有数据来自 `llm.ai_brain_calls` 表
- 最后一次调用: 2025-11-01 14:49 (12 小时前)
- 因此 "AI 大脑状态" 显示 🔴 红色 "已停止"

### 为什么 "今日 AI 活动" 是 0?
- 查询使用 `WHERE toDate(ts_ny) = today()`
- 今天是 2025-11-02
- 但最后的数据是 2025-11-01
- 所以今天(2025-11-02)没有记录

### 如果想看到昨天的数据
修改 Panel 4 的查询:
```sql
WHERE toDate(ts_ny) = today() - 1  -- 昨天
```

或者:
```sql
WHERE ts_ny > now() - INTERVAL 24 HOUR  -- 最近24小时
```

---

## 📋 所有 Panels 状态

| Panel ID | 标题 | 类型 | 数据状态 |
|----------|------|------|---------|
| 2 | AI 大脑状态 | Stat | ✅ 显示 12 (小时) → 🔴 红色 |
| 3 | 最后活动 | Stat | ✅ 显示 2025-11-01 14:49 |
| 4 | 今日 AI 活动 | Stat | ⚠️ 显示 0 (因为今天无数据) |
| 6 | AI 大脑的最新分析 | Table | ✅ 显示格式化文本 |
| 8 | 最近一次配置修改 | Table | ✅ 显示格式化文本 |
| 10 | 修复步骤 | Text | ✅ 静态文档 |
| 11 | 常用命令 | Text | ✅ 静态文档 |

---

## 🎯 使用指南

### 打开 Dashboard 后你会看到:

#### Row 1: 一眼健康检查
```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ 🔴 已停止       │  │ 2025-11-01      │  │ 调用次数: 0     │
│     12          │  │ 14:49           │  │ (今日)          │
│ (小时)          │  │                 │  │                 │
└─────────────────┘  └─────────────────┘  └─────────────────┘
```

**第一眼判断**: 🔴 红色 → AI 大脑已停止 12 小时

---

#### Row 2: AI 在说什么

一个大文本框,内容:
```
📅 分析时间: 2025-11-01 14:49

📊 当前状态: 系统刚启动，正在收集数据

🤖 AI 说:
"你好！我刚启动，还没有交易历史数据..."

✅ AI 建议你:
• 继续运行系统
• 保持当前设置
• 不要手动调整参数

⏭️ 下一步:
预计 2-4 小时后，我会有第一次参数优化建议

🎯 置信度: 90%
```

**你能看懂**: AI 处于什么状态,建议做什么,接下来会发生什么

---

#### Row 3: AI 做了什么

一个大文本框,内容:
```
⏰ 修改时间: 2025-11-01 14:06

🔧 修改了什么:
  • 📉 回撤限制 → -5
  • 🛡️ 自动对冲 → true

💭 AI 为什么这么做:
"保守的初始设置:没有交易历史,启用对冲和严格的回撤限制"

📊 实际影响:
  ✅ 如果今天亏损达到 -5%，系统会自动停止交易
  ✅ 这是保护措施，避免单日亏损过大
  ⚠️ 可能会限制盈利能力 (如果过于保守)
```

**你能理解**: AI 修改了什么参数,为什么改,对交易有什么实际影响

---

#### Row 4: 问题排查 (折叠)

展开后看到修复命令:
```bash
# 1️⃣ 检查进程
ps aux | grep orchestrator

# 2️⃣ 重启
cd .
python scripts/ai_trader/orchestrator.py &

# 3️⃣ 查看日志
tail -50 logs/ai_brain_orchestrator.log
```

**可以直接复制粘贴执行**

---

## ⚠️ 当前数据状态

### 数据过期情况
- 最后 AI 调用: **2025-11-01 14:49**
- 当前时间: **2025-11-02 01:50**
- 过期时长: **12 小时**

### 影响
- ❌ "AI 大脑状态" 显示 🔴 红色 "已停止"
- ⚠️ "今日 AI 活动" 显示 0 (因为今天没有调用)
- ✅ Panel 6 和 8 仍然显示 **昨天最后一次** 的分析和决策

### 如何修复
按照 Row 4 (问题排查) 的步骤重启 AI Brain orchestrator

---

## 📁 文件清单

| 文件 | 路径 | 状态 |
|------|------|------|
| **Dashboard JSON** | `grafana/dashboards/active/ai_brain_mgmt.json` | ✅ 已修复 |
| **使用指南** | `AI_BRAIN_MGMT_DASHBOARD.md` | ✅ 已创建 |
| **就绪报告** | `MGMT_DASHBOARD_READY.md` (本文件) | ✅ 已创建 |

---

## ✅ 总结

### 问题: 之前没有数据显示
**原因**: Text panel 的变量替换机制问题

### 解决: 改用 Table panel + concat()
**现在**: 所有 panels 都能显示真实数据

### 验证: 打开 Dashboard
**URL**: http://localhost:3000/d/ai-brain-mgmt/

### 你会看到:
- ✅ Row 1: 🔴 已停止 (12小时) ← 数据年龄告警
- ✅ Row 2: AI 的话 (人话翻译,多行文本)
- ✅ Row 3: 配置修改详情 (人话解释影响)
- ✅ Row 4: 修复命令 (可复制)

---

**状态**: ✅ **Dashboard 已就绪,可以使用**
**下一步**: 重启 AI Brain orchestrator 让数据保持新鲜
**建议**: 每天早上打开这个 dashboard 检查 AI 状态

---

**完成时间**: 2025-11-02 01:50 EST
