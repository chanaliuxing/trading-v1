# AI Brain - 交互式调用列表使用指南

**Dashboard URL**: http://localhost:3000/d/ai-brain-interactive/

**创建时间**: 2025-11-02 02:45 EST

**功能**: 点击查看每次 AI Brain 调用的详细 LLM 返回结果

---

## 🎯 这个 Dashboard 做什么

提供**交互式界面**,让你可以:

1. 📋 **浏览所有 AI Brain 调用历史** (列表视图)
2. 🔍 **点击某一行** → 查看该次调用的详细信息
3. 🤖 **查看完整的 LLM 返回结果** (JSON 格式)
4. 📊 **解析后的数据** (diagnosis, prescription, apply)

---

## 🖱️ 如何使用

### 步骤 1: 打开 Dashboard

访问: http://localhost:3000/d/ai-brain-interactive/

你会看到两个部分:
- **上方**: 📋 AI Brain 调用列表
- **下方**: 📄 LLM 返回详情 (默认显示最新一次)

---

### 步骤 2: 浏览调用列表

**列表显示内容**:

| 列名 | 说明 | 示例 |
|------|------|------|
| call_time | 调用时间 | 2025-11-01 14:49:00 |
| kind | 调用类型 | propose_plan / decide |
| session | 会话类型 | reflection / overnight / (空) |
| provider | LLM 提供商 | deepseek_nvidia |
| model | 模型名称 | deepseek-ai/deepseek-r1 |
| duration_ms | 耗时 (毫秒) | 5577 ms |
| status | 成功状态 | ✅ / ❌ |
| response_size | 返回大小 | 576 bytes |

**特点**:
- 默认显示最近 7 天的调用
- 按时间倒序排列 (最新在上)
- 最多显示 100 条

---

### 步骤 3: 点击查看详情

**操作**: 点击列表中任意一行的 **call_time** 列

**效果**: 页面自动滚动到下方,显示该次调用的详细信息

---

### 步骤 4: 查看详细信息

点击后,下方会显示 **6 个详情面板**:

#### Panel 1: 📋 调用基本信息

显示这次调用的元数据:
```
⏰ 时间:     2025-11-01 14:49:00
🎯 类型:     propose_plan
📅 Session:  reflection
🤖 Provider: deepseek_nvidia
🧠 Model:    deepseek-ai/deepseek-r1
⏱️ Duration: 5577 ms
✅ Success:  ✅ Yes
```

---

#### Panel 2: 📥 输入参数 (Input Keys)

显示传给 LLM 的输入数据字段:
```
facts
constraints
market_context
trading_universe
```

---

#### Panel 3: 🤖 LLM 返回的 JSON 结果

**这是最重要的部分!**

显示 LLM 完整的 JSON 返回内容:
```json
{
  "diagnosis": "no_trading_history_due_to_new_system_init",
  "prescription": [
    {
      "action": "enable_baseline_data_collection",
      "params": {
        "providers": ["deepseek", "gemini"],
        "symbols": ["AAPL", "TSLA"],
        "interval_sec": 600,
        "ab_split": {"champion": 0.7, "challenger": 0.3}
      },
      "confidence": 0.9
    },
    {
      "action": "activate_safe_mode_with_limits",
      "params": {
        "restrictions": ["no_new_positions", "reduce_sizing"],
        "max_symbols": 2
      },
      "confidence": 0.85
    },
    {
      "action": "verify_system_health_before_live",
      "params": {
        "require_uptime_check": true,
        "max_api_latency": 1500
      },
      "confidence": 0.8
    }
  ]
}
```

**特点**:
- JSON 格式化显示 (可折叠/展开)
- 包含 LLM 返回的所有字段
- 这是 AI Brain 的**原始输出**

---

#### Panel 4: 📊 解析后的诊断 (Diagnosis)

提取并显示 `diagnosis` 字段:
```
no_trading_history_due_to_new_system_init
```

**仅对 `propose_plan` 类型有效**

---

#### Panel 5: 💊 解析后的建议 (Prescription)

提取并格式化显示 `prescription` 数组:

| action | params | confidence |
|--------|--------|------------|
| enable_baseline_data_collection | {"providers": ["deepseek", "gemini"], ...} | 0.9 |
| activate_safe_mode_with_limits | {"restrictions": ["no_new_positions", ...]} | 0.85 |
| verify_system_health_before_live | {"require_uptime_check": true, ...} | 0.8 |

**特点**:
- 每个建议单独一行
- `params` 列以 JSON 格式显示 (可展开查看)
- 置信度显示为数值

**仅对 `propose_plan` 类型有效**

---

#### Panel 6: ⚙️ 应用的配置 (Apply Changes)

提取并显示 `apply` 数组 (实际应用到系统的配置):

| config_path | config_value |
|-------------|--------------|
| risk_management.guardian.dd_intraday_pct | -5 |
| risk_management.hedge.auto_execute | true |

**仅对 `decide` 类型有效**

---

## 🎯 使用场景

### 场景 1: 调试 AI Brain 输出

**问题**: AI Brain 做出了奇怪的决定,我想看看 LLM 到底返回了什么

**步骤**:
1. 打开 Dashboard
2. 找到对应时间的调用记录
3. 点击 call_time
4. 查看 "🤖 LLM 返回的 JSON 结果" 面板
5. 检查 diagnosis 和 prescription 是否合理

---

### 场景 2: 对比不同时间的 AI 判断

**问题**: 我想看看 AI 在不同时间段的诊断和建议有什么变化

**步骤**:
1. 浏览列表,找到两个不同时间的 `propose_plan` 调用
2. 分别点击查看
3. 对比 diagnosis 和 prescription 的差异

**提示**: 可以打开两个浏览器窗口并排对比

---

### 场景 3: 检查 AI 是否应用了配置

**问题**: AI 提出了建议,我想确认是否真的应用到系统

**步骤**:
1. 找到 `propose_plan` 调用,查看 prescription
2. 找到紧接着的 `decide` 调用
3. 查看 "⚙️ 应用的配置" 面板
4. 对比 prescription 的 params 和 apply 的 config_value

---

### 场景 4: 分析 LLM 性能

**问题**: 我想知道 LLM 调用的延迟情况

**步骤**:
1. 查看列表中的 `duration_ms` 列
2. 找出耗时最长的调用
3. 点击查看详情,检查是否返回正常

**正常值**:
- propose_plan: 5000-15000 ms (5-15 秒)
- decide: 2000-6000 ms (2-6 秒)

**异常值**:
- > 30000 ms (30 秒): 可能 LLM 响应慢或网络问题
- < 1000 ms: 可能使用了缓存或出错

---

### 场景 5: 查看失败的调用

**问题**: AI Brain 调用失败了,我想看错误信息

**步骤**:
1. 列表中找到 status = ❌ 的行
2. 点击查看详情
3. 检查 "调用基本信息" 面板确认失败
4. (未来) 查看 error_message 字段

---

## 📊 Dashboard 结构详解

### Row 1: 📋 AI Brain 调用列表

**Panel 类型**: Table

**数据源**: `llm.ai_brain_calls`

**时间范围**: 最近 7 天 (可调整)

**刷新间隔**: 30 秒

**交互**: 点击 call_time 列 → 跳转到详情

**SQL 查询**:
```sql
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%i:%S') as call_time,
    kind,
    session,
    provider,
    model,
    duration_ms,
    if(success = 1, '✅', '❌') as status,
    length(output_json) as response_size,
    ts_ny as timestamp_raw
FROM llm.ai_brain_calls
WHERE ts_ny >= $__fromTime AND ts_ny <= $__toTime
ORDER BY ts_ny DESC
LIMIT 100
```

---

### Row 2: 📄 LLM 返回详情

**触发条件**:
- 默认显示最新一次调用
- 点击列表某行后,显示该行的详情

**如何工作**:
1. 用户点击列表某行
2. Grafana 设置变量 `$selected_time` = 该行的 timestamp
3. 详情面板的查询使用 `WHERE toString(ts_ny) = '${selected_time}'`
4. 自动显示对应的详细信息

---

## ⚙️ 技术实现

### 交互原理

使用 Grafana 的 **Data Links** 功能:

```json
{
  "links": [
    {
      "title": "查看详情",
      "url": "/d/ai-brain-interactive?var-selected_time=${__data.fields.timestamp_raw}&viewPanel=10"
    }
  ]
}
```

**参数说明**:
- `var-selected_time=${__data.fields.timestamp_raw}`: 设置变量为点击行的时间戳
- `viewPanel=10`: 自动滚动到 Panel ID = 10 (详情部分)

---

### 变量定义

```json
{
  "name": "selected_time",
  "type": "textbox",
  "label": "Selected Call Time",
  "hide": 2
}
```

**hide: 2** 表示变量对用户不可见 (内部使用)

---

### 查询模式

所有详情面板使用相同的 WHERE 条件:

```sql
WHERE toString(ts_ny) = '${selected_time}' OR '${selected_time}' = ''
ORDER BY ts_ny DESC
LIMIT 1
```

**逻辑**:
- 如果 `$selected_time` 有值: 显示该时间的数据
- 如果 `$selected_time` 为空: 显示最新一次 (`ORDER BY ts_ny DESC LIMIT 1`)

---

## 🔍 调试技巧

### 查看 SQL 查询

在 Grafana 中:
1. 点击任意 Panel 的标题
2. 选择 "Explore" → "Edit"
3. 查看实际执行的 SQL

---

### 手动测试查询

```bash
# 测试列表查询
docker exec trading_clickhouse clickhouse-client --query "
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%i:%S') as call_time,
    kind,
    session,
    provider,
    duration_ms
FROM llm.ai_brain_calls
ORDER BY ts_ny DESC
LIMIT 10
FORMAT Pretty"

# 测试详情查询 (替换时间)
docker exec trading_clickhouse clickhouse-client --query "
SELECT output_json
FROM llm.ai_brain_calls
WHERE formatDateTime(ts_ny, '%Y-%m-%d %H:%i:%S') = '2025-11-01 14:49:00'
LIMIT 1
FORMAT Pretty"
```

---

### 检查变量值

在 Grafana Dashboard 右上角:
1. 点击 "Dashboard settings" (齿轮图标)
2. 选择 "Variables"
3. 查看 `selected_time` 的当前值

---

## ⚠️ 注意事项

### 1. REASONING 文本未显示

**问题**: 这个 Dashboard 只显示 `output_json`,不显示 REASONING 推理过程

**原因**: REASONING 存储在文件系统,未写入 ClickHouse

**查看方法**: 参考 [AI_BRAIN_COMPLETE_DASHBOARD.md](./AI_BRAIN_COMPLETE_DASHBOARD.md) 中的文件位置

---

### 2. decide 类型没有 prescription

**正常**: `decide` 调用只有 `apply` 字段,没有 `diagnosis` 和 `prescription`

**区别**:
- `propose_plan`: 提出建议 (有 diagnosis + prescription)
- `decide`: 应用配置 (有 apply + notes)

---

### 3. 列表最多显示 100 条

**限制**: SQL 查询使用 `LIMIT 100`

**原因**: 避免数据量过大影响性能

**调整方法**: 修改 Dashboard JSON,将 `LIMIT 100` 改为更大的值 (如 `LIMIT 500`)

---

### 4. session 列可能为空

**正常**: `decide` 调用通常没有 session 字段

**说明**:
- `propose_plan`: 通常有 session (reflection / overnight)
- `decide`: session 为空

---

## 🚀 快速参考

### 常用操作

| 操作 | 方法 |
|------|------|
| 查看最新调用 | 打开 Dashboard,列表第一行 |
| 查看某个时间的调用 | 列表中找到对应时间,点击 call_time |
| 查看完整 JSON | 点击行后,查看 "🤖 LLM 返回的 JSON 结果" 面板 |
| 对比两次调用 | 分别点击,记录差异 (或打开两个窗口) |
| 刷新数据 | 等待 30 秒自动刷新,或手动点击右上角刷新按钮 |

---

### 调用类型识别

| kind | session | 含义 |
|------|---------|------|
| propose_plan | reflection | 反思会话提出建议 |
| propose_plan | overnight | 隔夜会话提出建议 |
| decide | (空) | 应用配置到系统 |

---

### 数据完整性检查

**健康的 AI Brain 应该显示**:
- ✅ status 全部是 ✅
- ✅ duration_ms 在合理范围 (2-15 秒)
- ✅ response_size > 100 bytes
- ✅ propose_plan 和 decide 成对出现

**异常情况**:
- ❌ status 有 ❌ → 调用失败
- ❌ duration_ms > 30000 → 超时或网络问题
- ❌ response_size < 50 → 返回内容可能不完整

---

## 📝 与其他 Dashboard 的区别

| Dashboard | 定位 | 适合场景 |
|-----------|------|---------|
| **ai-brain-interactive** (新) | 交互式列表 + 详情 | 调试、查看原始 LLM 输出 |
| ai-brain-complete | 完整视图 (推理+配置+状态) | 全面了解 AI 的三个维度 |
| ai-brain-mgmt | 管理视图 (简化) | 日常监控 |
| ai-brain-detailed | 技术视图 (JSON) | 开发调试 |

---

## ✅ 总结

### 这个 Dashboard 解决了什么问题?

1. ✅ **查看所有调用历史** → 列表视图
2. ✅ **点击查看详情** → 交互式导航
3. ✅ **查看完整 LLM 返回** → JSON 格式化显示
4. ✅ **解析后的数据** → diagnosis, prescription, apply

### 主要特点

- 🖱️ **交互式**: 点击列表行自动显示详情
- 📊 **结构化**: 6 个详情面板,层次清晰
- 🤖 **完整**: 显示 LLM 返回的原始 JSON
- 🔍 **易调试**: 可对比不同时间的调用

### 使用建议

1. **日常使用**: 打开 Dashboard,快速查看最新调用
2. **调试问题**: 找到异常时间,点击查看完整 JSON
3. **对比分析**: 打开两个窗口,对比不同时间的输出
4. **配合使用**: 结合 `ai-brain-complete` Dashboard 全面了解 AI

---

**Dashboard URL**: http://localhost:3000/d/ai-brain-interactive/

**创建时间**: 2025-11-02 02:45 EST

**版本**: v1.0 Interactive

**下一步**: 使用这个 Dashboard 查看和调试 AI Brain 的 LLM 调用!
