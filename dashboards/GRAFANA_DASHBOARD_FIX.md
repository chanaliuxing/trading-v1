# 🔧 Grafana Dashboard 修复指南

**问题**: AI Trader Overview Dashboard 所有面板报错
**根本原因**: 使用了错误的 JSON 函数 (`JSON_VALUE` 而非 `JSONExtractString`)
**状态**: ✅ 已修复

---

## 🐛 问题分析

### 错误的查询 (❌ 不工作)
```sql
-- 错误: 使用 JSON_VALUE (SQL Server/MySQL 函数)
SELECT
    ts,
    JSON_VALUE(payload,'$.action') AS action
FROM ai_trader_events
```

### 正确的查询 (✅ 工作)
```sql
-- 正确: 使用 JSONExtractString (ClickHouse 函数)
SELECT
    ts,
    JSONExtractString(payload, 'action') AS action
FROM ai_trader_events
```

---

## 📊 数据验证

### 检查表和数据
```bash
python -c "
from libs.db.clickhouse import ClickHouseDB
ch = ClickHouseDB()

# 检查表
tables = ch.query('SHOW TABLES')
print('Available tables:', [t['name'] for t in tables if 'ai_trader' in t['name']])

# 检查数据量
events = ch.query('SELECT count() as cnt FROM ai_trader_events')[0]['cnt']
print(f'ai_trader_events: {events} rows')

brain_calls = ch.query(\"SELECT count() as cnt FROM ai_trader_events WHERE kind='brain'\")[0]['cnt']
print(f'Brain calls: {brain_calls} rows')

# 测试 JSON 提取
sample = ch.query(\"SELECT payload FROM ai_trader_events WHERE kind='brain' LIMIT 1\")[0]
print(f'Sample payload: {sample}')
"
```

**当前数据状态**:
- ✅ `ai_trader_events`: 8,339 rows
- ✅ `ai_trader_dash_metrics`: 105 rows
- ✅ `brain` 类型事件: 6,984 rows

---

## 🔄 两种修复方案

### 方案 1: 使用新的固定 Dashboard (推荐)

#### Step 1: 导入新 Dashboard
1. 打开 Grafana: http://localhost:3000/
2. 点击左侧菜单 → Dashboards → Import
3. 选择文件: `dashboards/AI_Trader_Overview_Fixed.json`
4. 配置数据源:
   - 选择你的 ClickHouse 数据源
   - 或者编辑 JSON 替换 `"uid": "CLICKHOUSE_DS"` 为你的实际 UID

#### Step 2: 查找你的 ClickHouse 数据源 UID
```bash
# 方法 1: 通过 Grafana UI
# Settings → Data Sources → ClickHouse → 查看 URL 中的 UID

# 方法 2: 通过 API
curl -s http://admin:admin@localhost:3000/api/datasources | jq '.[] | select(.type=="grafana-clickhouse-datasource") | {name, uid}'
```

#### Step 3: 替换 UID (如果需要)
```bash
# 在 AI_Trader_Overview_Fixed.json 中全局替换
sed -i '' 's/CLICKHOUSE_DS/YOUR_ACTUAL_UID/g' dashboards/AI_Trader_Overview_Fixed.json
```

---

### 方案 2: 手动修复现有 Dashboard

如果你想保留现有 Dashboard 的其他配置，可以手动修复每个面板的查询。

#### 修复模式

**查找**: `JSON_VALUE(payload,'$.xxx')`
**替换为**: `JSONExtractString(payload, 'xxx')`

**查找**: `JSON_VALUE(payload,'$.nested.field')`
**替换为**: `JSONExtractString(payload, 'nested', 'field')` 或直接访问 `JSONExtractString(payload, 'nested.field')`

#### 逐个面板修复步骤

1. **Guardian Events 面板**
   ```sql
   -- Before
   SELECT ts, symbol,
     JSON_VALUE(payload,'$.action') AS action,
     JSON_VALUE(payload,'$.reason') AS reason
   FROM ai_trader_events WHERE kind='guardian'

   -- After
   SELECT ts, symbol,
     JSONExtractString(payload, 'action') AS action,
     JSONExtractString(payload, 'reason') AS reason
   FROM ai_trader_events WHERE kind='guardian'
   ```

2. **Tool Watchdog 面板**
   ```sql
   -- Before
   SELECT ts,
     JSON_VALUE(payload,'$.event') AS event,
     JSON_VALUE(payload,'$.reason') AS reason
   FROM ai_trader_events WHERE kind='tool'

   -- After
   SELECT ts,
     JSONExtractString(payload, 'event') AS event,
     JSONExtractString(payload, 'reason') AS reason
   FROM ai_trader_events WHERE kind='tool'
   ```

3. **Hedging Events 面板**
   ```sql
   -- Before
   SELECT ts, symbol,
     JSON_VALUE(payload,'$.hedge_symbol') AS hedge_sym,
     toFloat64OrZero(JSON_VALUE(payload,'$.hedge_qty')) * toFloat64OrZero(JSON_VALUE(payload,'$.ref_px')) AS notional
   FROM ai_trader_events WHERE kind='hedging'

   -- After
   SELECT ts, symbol,
     JSONExtractString(payload, 'hedge_symbol') AS hedge_sym,
     toFloat64OrZero(JSONExtractString(payload, 'hedge_qty')) * toFloat64OrZero(JSONExtractString(payload, 'ref_px')) AS notional
   FROM ai_trader_events WHERE kind='hedging'
   ```

4. **Brain Calls 面板** (最重要)
   ```sql
   -- Before
   SELECT ts,
     JSON_VALUE(payload,'$.kind') AS kind,
     JSON_VALUE(payload,'$.session') AS session,
     JSON_VALUE(payload,'$.action') AS action,
     left(JSON_VALUE(payload,'$.output'), 200) AS output_snip
   FROM ai_trader_events WHERE kind='brain'

   -- After
   SELECT ts,
     JSONExtractString(payload, 'kind') AS kind,
     JSONExtractString(payload, 'session') AS session,
     JSONExtractString(payload, 'action') AS action,
     JSONExtractString(payload, 'provider') AS provider,
     left(JSONExtractString(payload, 'output'), 200) AS output_snip
   FROM ai_trader_events WHERE kind='brain'
   AND ts > now() - INTERVAL 2 DAY
   ORDER BY ts DESC
   LIMIT 200
   ```

---

## ✅ 新 Dashboard 的改进

### 新增面板

1. **Brain Calls (Today)** - 今日调用数统计
   ```sql
   SELECT count() AS count
   FROM ai_trader_events
   WHERE kind='brain' AND toDate(ts) = today()
   ```

2. **Brain Success Rate (Today)** - 成功率监控
   ```sql
   SELECT
     round(countIf(NOT JSONHas(payload, 'output.error')) * 100.0 / count(), 2) AS success_rate
   FROM ai_trader_events
   WHERE kind='brain' AND toDate(ts) = today()
   ```

### 面板增强

- ✅ 所有查询使用正确的 ClickHouse 函数
- ✅ 添加颜色阈值 (红/黄/绿)
- ✅ 优化字段宽度
- ✅ 默认按时间倒序排列
- ✅ 使用 `rawSql` 而非 `query` (更兼容)

---

## 🧪 测试查询

在修复 Dashboard 之前，可以先在 ClickHouse 或 Grafana Explore 中测试这些查询:

### 测试 1: Brain Calls 基础查询
```sql
SELECT count() as total
FROM ai_trader_events
WHERE kind='brain'
-- 期望: 应返回 6984 (或更多)
```

### 测试 2: JSON 提取
```sql
SELECT
    ts,
    JSONExtractString(payload, 'kind') as kind,
    JSONExtractString(payload, 'session') as session,
    JSONExtractString(payload, 'provider') as provider
FROM ai_trader_events
WHERE kind='brain'
LIMIT 10
-- 期望: 返回 10 行，包含 kind, session, provider 列
```

### 测试 3: 嵌套 JSON 访问
```sql
SELECT
    ts,
    payload,
    JSONExtractString(payload, 'output') as output_full,
    JSONHas(payload, 'output.error') as has_error
FROM ai_trader_events
WHERE kind='brain'
LIMIT 5
-- 期望: 返回完整 output 和 error 标志
```

### 测试 4: 聚合查询
```sql
SELECT
    toDate(ts) as date,
    JSONExtractString(payload, 'session') as session,
    count() as calls
FROM ai_trader_events
WHERE kind='brain'
  AND ts > now() - INTERVAL 7 DAY
GROUP BY date, session
ORDER BY date DESC, session
-- 期望: 每天每个 session 的调用数
```

---

## 🔍 故障排查

### 问题 1: 查询返回空结果

**可能原因**:
- 数据不在时间范围内
- `kind` 字段值不匹配
- 表名错误

**检查**:
```sql
-- 查看所有 kind 类型
SELECT DISTINCT kind, count() as cnt
FROM ai_trader_events
GROUP BY kind
ORDER BY cnt DESC

-- 查看时间范围
SELECT
    min(ts) as earliest,
    max(ts) as latest
FROM ai_trader_events
WHERE kind='brain'
```

### 问题 2: JSON 字段为空

**可能原因**:
- JSON 路径错误
- Payload 不是有效 JSON

**检查**:
```sql
-- 查看原始 payload
SELECT payload
FROM ai_trader_events
WHERE kind='brain'
LIMIT 1

-- 检查 JSON 有效性
SELECT
    isValidJSON(payload) as is_valid,
    count() as cnt
FROM ai_trader_events
WHERE kind='brain'
GROUP BY is_valid
```

### 问题 3: 数据源连接失败

**检查步骤**:
1. 验证 ClickHouse 运行
   ```bash
   docker ps | grep clickhouse
   ```

2. 测试连接
   ```bash
   python -c "from libs.db.clickhouse import ClickHouseDB; ch = ClickHouseDB(); print('Connected:', ch.query('SELECT 1'))"
   ```

3. 检查 Grafana 数据源配置
   - Settings → Data Sources → ClickHouse
   - Test & Save

---

## 📖 ClickHouse JSON 函数参考

### 常用函数

| 函数 | 用途 | 示例 |
|------|------|------|
| `JSONExtractString(json, 'path')` | 提取字符串 | `JSONExtractString(payload, 'kind')` |
| `JSONExtractInt(json, 'path')` | 提取整数 | `JSONExtractInt(payload, 'count')` |
| `JSONExtractFloat(json, 'path')` | 提取浮点数 | `JSONExtractFloat(payload, 'value')` |
| `JSONHas(json, 'path')` | 检查字段存在 | `JSONHas(payload, 'error')` |
| `isValidJSON(json)` | 验证 JSON | `isValidJSON(payload)` |
| `JSONExtract(json, 'path', 'Type')` | 通用提取 | `JSONExtract(payload, 'data', 'Array(String)')` |

### 路径语法

```sql
-- 简单路径
JSONExtractString(payload, 'kind')  -- payload.kind

-- 嵌套路径 (多种写法)
JSONExtractString(payload, 'output.error')  -- 方法 1
JSONExtractString(JSONExtractString(payload, 'output'), 'error')  -- 方法 2

-- 数组访问
JSONExtractString(payload, 'items[0].name')

-- 条件检查
JSONHas(payload, 'output.error')  -- 检查 payload.output.error 是否存在
```

---

## 🚀 快速修复命令

### 一键替换 (如果你有原始 JSON 文件)
```bash
cd dashboards

# 备份原文件
cp AI_Trader_Overview.json AI_Trader_Overview.json.backup

# 全局替换 JSON_VALUE 为 JSONExtractString
sed -i '' "s/JSON_VALUE(payload,'\$\.\([^']*\)')/JSONExtractString(payload, '\1')/g" AI_Trader_Overview.json

# 替换 format 字段
sed -i '' 's/"format": 1/"format": 1, "rawSql":/g' AI_Trader_Overview.json

# 验证修改
diff AI_Trader_Overview.json.backup AI_Trader_Overview.json
```

**注意**: 这个一键脚本可能需要手动调整复杂的嵌套 JSON 路径。

---

## 📊 推荐的 Dashboard 布局

### 第一行 (统计指标)
```
[OOS Sharpe] [DQ Fail] [Brain Calls] [Success Rate]
    6x4         6x4         6x4           6x4
```

### 第二行 (趋势图)
```
[OOS Sharpe 7d Trend] [Hedging Events]
       12x6                  12x6
```

### 第三行 (事件表格)
```
[Guardian Events]  [Tool Watchdog]
      12x8              12x8
```

### 第四行 (Brain Calls 详情)
```
[Brain Calls Recent - Full Width]
          24x8
```

---

## ✅ 验证修复成功

修复后，每个面板应该:
1. ✅ 不显示错误信息
2. ✅ 显示实际数据 (如果有数据)
3. ✅ 查询响应时间 < 1秒
4. ✅ 自动刷新正常工作

### 最终检查清单
- [ ] 所有面板无报错
- [ ] Brain Calls 面板显示 6,984+ 行
- [ ] 时间筛选器工作正常
- [ ] 自动刷新 (30秒) 工作
- [ ] 查询性能良好

---

## 📞 需要帮助?

如果修复后仍有问题:

1. **导出当前 Dashboard JSON**
   - Dashboard Settings → JSON Model → Copy

2. **检查错误日志**
   ```bash
   # Grafana 日志
   docker logs grafana

   # ClickHouse 查询日志
   docker exec clickhouse-server clickhouse-client --query "SELECT * FROM system.query_log WHERE type='ExceptionWhileProcessing' ORDER BY event_time DESC LIMIT 10"
   ```

3. **提供诊断信息**
   - Grafana 版本
   - ClickHouse 插件版本
   - 具体错误信息截图

---

**最后更新**: 2025-10-29
**文件**: `dashboards/AI_Trader_Overview_Fixed.json` (已修复版本)
**原文件**: `dashboards/AI_Trader_Overview.json` (需要手动修复)
