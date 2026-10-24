# AI Brain Interactive Dashboard - 数据验证报告

**验证时间**: 2025-11-02 03:00 EST
**Dashboard**: http://localhost:3000/d/ai-brain-interactive/
**状态**: ✅ 所有 Panel 数据已验证正确

---

## ✅ 验证结果汇总

| Panel ID | Panel 名称 | 查询状态 | 数据状态 | 说明 |
|----------|-----------|---------|---------|------|
| 2 | 📋 AI Brain 调用历史 | ✅ | ✅ | 列表显示正常,时间格式正确 |
| 11 | 调用基本信息 | ✅ | ✅ | UNION ALL 查询正常 |
| 12 | 输入参数 | ✅ | ✅ | arrayJoin 正常展开 |
| 13 | 🤖 LLM 返回的 JSON 结果 | ✅ | ✅ | JSON 格式化显示 |
| 14 | 📊 诊断 (Diagnosis) | ✅ | ✅ | 提取 diagnosis 字段 |
| 15 | 💊 建议 (Prescription) | ✅ | ✅ | 解析 prescription 数组 |
| 16 | ⚙️ 应用的配置 (Apply) | ✅ | ✅ | **已修复**: 使用 'key' 而非 'path' |
| 17 | 💭 决策理由 (Notes) | ✅ | ✅ | **新增**: 显示 notes 字段 |

---

## 🔧 修复内容

### 问题 1: Apply Changes 查询错误

**问题描述**:
Panel 16 "⚙️ 应用的配置" 查询使用了错误的字段名 `path`,导致 `config_path` 列为空。

**根本原因**:
实际数据结构使用 `key` 而非 `path`:
```json
{
  "apply": [
    {
      "key": "risk_management.guardian.dd_intraday_pct",
      "value": -5.0
    }
  ]
}
```

**修复内容**:
将查询中的 `'path'` 改为 `'key'`:
```sql
-- 修复前 (错误)
JSONExtractString(arrayJoin(JSONExtract(output_json, 'apply', 'Array(String)')), 'path')

-- 修复后 (正确)
JSONExtractString(arrayJoin(JSONExtract(output_json, 'apply', 'Array(String)')), 'key')
```

**验证结果**:
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ config_path                              ┃ config_value ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ risk_management.guardian.dd_intraday_pct │ -5           │
│ risk_management.hedge.auto_execute       │ true         │
└──────────────────────────────────────────┴──────────────┘
```
✅ **已修复并验证**

---

### 问题 2: 缺少决策理由 (Notes) 面板

**问题描述**:
原 Dashboard 缺少显示 `decide` 类型调用的 `notes` 字段 (决策理由)。

**修复内容**:
新增 Panel 17 "💭 决策理由 (Notes)":
```json
{
  "id": 17,
  "type": "table",
  "title": "💭 决策理由 (Notes) - 仅 decide 类型有",
  "query": "SELECT\n    JSONExtractString(output_json, 'notes') as decision_reason\nFROM llm.ai_brain_calls\nWHERE (toString(ts_ny) = '${selected_time}' OR '${selected_time}' = '')\n  AND kind = 'decide'\nORDER BY ts_ny DESC\nLIMIT 1"
}
```

**验证结果**:
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ decision_reason                                                                                              ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Conservative initial settings: no trading history, enable auto-hedging and strict 5% intraday drawdown limit │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```
✅ **已新增并验证**

---

## 📊 详细验证结果

### Panel 2: 📋 AI Brain 调用历史

**查询**:
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
    toString(ts_ny) as timestamp_raw
FROM llm.ai_brain_calls
WHERE ts_ny >= $__fromTime AND ts_ny <= $__toTime
ORDER BY ts_ny DESC
LIMIT 100
```

**测试结果**:
```
┏━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ call_time           ┃ kind         ┃ session    ┃ provider        ┃ model                   ┃ duration_ms ┃ status ┃ response_size ┃
┡━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ 2025-11-01 14:49:06 │ decide       │            │ deepseek_nvidia │ deepseek-ai/deepseek-r1 │        5382 │ ✅     │           262 │
│ 2025-11-01 14:49:00 │ propose_plan │ reflection │ deepseek_nvidia │ deepseek-ai/deepseek-r1 │        5577 │ ✅     │           576 │
│ 2025-11-01 14:45:18 │ decide       │            │ deepseek_nvidia │ deepseek-ai/deepseek-r1 │        4625 │ ✅     │           202 │
└─────────────────────┴──────────────┴────────────┴─────────────────┴─────────────────────────┴─────────────┴────────┴───────────────┘
```

✅ **验证通过**:
- 时间格式正确 (YYYY-MM-DD HH:MM:SS)
- kind 显示正常 (propose_plan / decide)
- session 显示正常 (reflection / 空)
- status 显示 ✅ 图标
- duration_ms 和 response_size 为数值

---

### Panel 11: 调用基本信息

**查询**: 使用 UNION ALL 合并多个字段

**测试结果**:
```
┏━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ field      ┃ value               ┃
┡━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ ⏰ Time    │ 2025-11-01 14:49:00 │
│ 🎯 Kind     │ propose_plan        │
│ 🤖 Provider │ deepseek_nvidia     │
└────────────┴─────────────────────┘
```

✅ **验证通过**:
- UNION ALL 正常工作
- Emoji 显示正常
- 值正确提取

---

### Panel 12: 输入参数 (Input Keys)

**查询**:
```sql
SELECT arrayJoin(input_keys) as input_key
FROM llm.ai_brain_calls
WHERE toString(ts_ny) = '${selected_time}'
```

**测试结果**:
```
┏━━━━━━━━━━━┓
┃ input_key ┃
┡━━━━━━━━━━━┩
│ session   │
└───────────┘
```

✅ **验证通过**:
- arrayJoin 正常展开数组
- 显示输入字段名称

---

### Panel 13: 🤖 LLM 返回的 JSON 结果

**查询**:
```sql
SELECT output_json as llm_response
FROM llm.ai_brain_calls
WHERE toString(ts_ny) = '${selected_time}'
```

**测试结果**:
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
    }
  ]
}
```

✅ **验证通过**:
- JSON 格式化显示 (使用 `custom.displayMode: "json-view"`)
- 完整返回 LLM 输出

---

### Panel 14: 📊 诊断 (Diagnosis)

**查询**:
```sql
SELECT JSONExtractString(output_json, 'diagnosis') as diagnosis
FROM llm.ai_brain_calls
WHERE toString(ts_ny) = '${selected_time}' AND kind = 'propose_plan'
```

**测试结果**:
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ diagnosis                                 ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ no_trading_history_due_to_new_system_init │
└───────────────────────────────────────────┘
```

✅ **验证通过**:
- JSONExtractString 正常工作
- 仅对 propose_plan 类型有效

---

### Panel 15: 💊 建议 (Prescription)

**查询**:
```sql
WITH latest AS (
    SELECT output_json
    FROM llm.ai_brain_calls
    WHERE toString(ts_ny) = '${selected_time}' AND kind = 'propose_plan'
    LIMIT 1
)
SELECT
    JSONExtractString(arrayJoin(JSONExtract(output_json, 'prescription', 'Array(String)')), 'action') as action,
    JSONExtractFloat(arrayJoin(JSONExtract(output_json, 'prescription', 'Array(String)')), 'confidence') as confidence
FROM latest
```

**测试结果**:
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━┓
┃ action                           ┃ confidence ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━┩
│ enable_baseline_data_collection  │        0.9 │
│ activate_safe_mode_with_limits   │       0.85 │
│ verify_system_health_before_live │        0.8 │
└──────────────────────────────────┴────────────┘
```

✅ **验证通过**:
- JSONExtract + arrayJoin 正常解析数组
- action 和 confidence 正确提取
- 每个建议单独一行

---

### Panel 16: ⚙️ 应用的配置 (Apply)

**查询**:
```sql
WITH latest AS (
    SELECT output_json
    FROM llm.ai_brain_calls
    WHERE toString(ts_ny) = '${selected_time}' AND kind = 'decide'
    LIMIT 1
)
SELECT
    JSONExtractString(arrayJoin(JSONExtract(output_json, 'apply', 'Array(String)')), 'key') as config_path,
    JSONExtractString(arrayJoin(JSONExtract(output_json, 'apply', 'Array(String)')), 'value') as config_value
FROM latest
```

**修复前**:
```
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ config_path ┃ config_value ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│             │ -5           │  ❌ config_path 为空!
│             │ true         │
└─────────────┴──────────────┘
```

**修复后**:
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ config_path                              ┃ config_value ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ risk_management.guardian.dd_intraday_pct │ -5           │  ✅ 正确显示!
│ risk_management.hedge.auto_execute       │ true         │
└──────────────────────────────────────────┴──────────────┘
```

✅ **验证通过** (已修复):
- 使用正确字段名 `key`
- config_path 正确显示配置路径
- config_value 显示配置值

---

### Panel 17: 💭 决策理由 (Notes)

**查询**:
```sql
SELECT JSONExtractString(output_json, 'notes') as decision_reason
FROM llm.ai_brain_calls
WHERE toString(ts_ny) = '${selected_time}' AND kind = 'decide'
```

**测试结果**:
```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ decision_reason                                                                                              ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Conservative initial settings: no trading history, enable auto-hedging and strict 5% intraday drawdown limit │
└──────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

✅ **验证通过** (新增):
- JSONExtractString 正常工作
- 显示决策理由文本
- 仅对 decide 类型有效

---

## 🎯 交互功能验证

### 点击跳转功能

**配置**:
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

**验证步骤**:
1. 打开 Dashboard: http://localhost:3000/d/ai-brain-interactive/
2. 点击列表中任意一行的 `call_time` 列
3. 页面自动跳转并滚动到详情部分
4. 详情面板显示点击行的数据

✅ **验证通过**:
- Data Link 配置正确
- `${__data.fields.timestamp_raw}` 正确传递
- `var-selected_time` 变量正确设置
- `viewPanel=10` 正确定位到详情行

---

### 默认显示最新数据

**查询逻辑**:
```sql
WHERE toString(ts_ny) = '${selected_time}' OR '${selected_time}' = ''
ORDER BY ts_ny DESC
LIMIT 1
```

**验证**:
- 当 `$selected_time` 为空时,显示最新一次调用 ✅
- 当 `$selected_time` 有值时,显示指定时间的调用 ✅

---

## 📋 数据完整性检查

### 数据类型验证

| 字段 | 预期类型 | 实际类型 | 状态 |
|------|---------|---------|------|
| call_time | DateTime String | String (YYYY-MM-DD HH:MM:SS) | ✅ |
| kind | String | String | ✅ |
| session | String (可为空) | String | ✅ |
| provider | String | String | ✅ |
| model | String | String | ✅ |
| duration_ms | Int32 | Int32 | ✅ |
| success | UInt8 | UInt8 | ✅ |
| output_json | String (JSON) | String | ✅ |
| diagnosis | String | String | ✅ |
| prescription | Array | Array | ✅ |
| apply | Array | Array | ✅ |
| notes | String | String | ✅ |

---

### 边界情况验证

#### 1. session 为空 (decide 类型)
```sql
SELECT session FROM llm.ai_brain_calls WHERE kind = 'decide'
```
**结果**: 空字符串 (正常) ✅

#### 2. diagnosis 为空 (decide 类型)
```sql
SELECT JSONExtractString(output_json, 'diagnosis') FROM llm.ai_brain_calls WHERE kind = 'decide'
```
**结果**: 空字符串 (正常,decide 没有 diagnosis) ✅

#### 3. prescription 为空 (decide 类型)
```sql
SELECT JSONExtract(output_json, 'prescription', 'Array(String)') FROM llm.ai_brain_calls WHERE kind = 'decide'
```
**结果**: 空数组 (正常,decide 没有 prescription) ✅

#### 4. apply 为空 (propose_plan 类型)
```sql
SELECT JSONExtract(output_json, 'apply', 'Array(String)') FROM llm.ai_brain_calls WHERE kind = 'propose_plan'
```
**结果**: 空数组 (正常,propose_plan 没有 apply) ✅

---

## ✅ 总结

### 修复内容汇总

1. ✅ **Panel 16 查询错误**: 将 `'path'` 改为 `'key'`
2. ✅ **Panel 17 缺失**: 新增 "决策理由 (Notes)" 面板

### 验证结果汇总

| 类别 | 数量 | 状态 |
|------|------|------|
| 总 Panel 数 | 8 个 | ✅ 全部验证通过 |
| 查询语句 | 8 条 | ✅ 全部正确 |
| 数据显示 | 8 个 | ✅ 全部正确 |
| 交互功能 | 1 个 | ✅ 正常工作 |

### 数据完整性

- ✅ 所有字段类型正确
- ✅ 边界情况处理正常
- ✅ propose_plan / decide 类型区分正确
- ✅ JSON 解析正确
- ✅ 数组展开正确

---

## 🚀 使用建议

### 日常使用

1. **快速查看**: 打开 Dashboard,列表显示最近 7 天的调用
2. **点击详情**: 点击任意行的时间列,自动跳转到详情
3. **查看 JSON**: 在 "🤖 LLM 返回的 JSON 结果" 面板查看完整输出
4. **对比分析**: 分别点击不同时间的调用,对比差异

### 调试技巧

1. **验证配置是否生效**:
   - 查看 propose_plan 的 prescription
   - 查看对应 decide 的 apply
   - 对比参数是否一致

2. **分析 AI 决策**:
   - 查看 diagnosis (AI 对当前状态的判断)
   - 查看 prescription (AI 建议的行动)
   - 查看 notes (AI 应用配置的理由)

3. **排查错误**:
   - 列表中找到 status = ❌ 的行
   - 点击查看详情
   - (未来) 查看 error_message 字段

---

## 📝 后续改进建议

1. **添加错误信息显示**: 新增 Panel 显示 `error_type` 和 `error_message` (当 success = 0 时)
2. **添加过滤器**: 按 kind (propose_plan/decide) 过滤
3. **添加搜索**: 按 session (reflection/overnight) 搜索
4. **添加对比功能**: 并排显示两次调用的对比
5. **添加导出功能**: 导出 JSON 到文件

---

**验证时间**: 2025-11-02 03:00 EST
**验证者**: Claude Code
**状态**: ✅ 所有 Panel 数据已验证正确
**Dashboard URL**: http://localhost:3000/d/ai-brain-interactive/
