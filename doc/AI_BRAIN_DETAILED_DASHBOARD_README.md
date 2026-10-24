# AI Brain - Detailed Analysis Dashboard 使用指南

**Dashboard URL**: http://localhost:3000/d/ai-brain-detailed/ai-brain-detailed-analysis
**创建时间**: 2025-11-02 01:56 EST
**最后更新**: 2025-11-02 01:35 EST (v2.0 修复版)
**用途**: 查看 AI 大脑的详细分析过程、诊断结果、建议方案

---

## 🎯 Dashboard 目的

这个 Dashboard 是为了满足你的需求：**了解 AI 大脑是怎么分析的**

现有的 dashboards 只显示统计数字（调用次数、延迟等），但这个新 Dashboard 展示：
- ✅ AI 的具体诊断结果 (diagnosis)
- ✅ AI 的建议方案 (prescription)
- ✅ AI 的决策细节 (decide actions)
- ✅ AI 的推理过程 (notes)
- ✅ 完整的 JSON 输出（可展开查看）
- 🆕 **数据新鲜度监控** (v2.0 新增)
- 🆕 **诊断演变时间线** (v2.0 新增)

---

## 📊 Dashboard 结构 (v2.0)

### Row 1: 🧠 AI 大脑实时状态

#### Panel 1: 📊 最近调用概览
- 显示最近一次 AI 调用的基本信息
- 包括：时间、类型 (kind)、provider、耗时、session ID、交易标的数量

**数据来源**: `llm.ai_brain_calls`

**示例**:
```
Time: 2025-11-01 14:49:06
Kind: decide
Provider: deepseek_nvidia
Duration: 5,382 ms
Session: abc123
Universe Size: 150
```

#### Panel 13: 🚨 数据新鲜度 (⭐ v2.0 新增)

**用途**: **最重要的监控面板** - 防止基于过期数据做决策

**显示内容**: AI 大脑数据的年龄 (距离最后一次调用的小时数)

**告警阈值**:
- 🟢 **绿色**: < 1 小时 → 数据新鲜,可以参考分析结果
- 🟡 **黄色**: 1-2 小时 → 轻微过期,注意观察
- 🟠 **橙色**: 2-6 小时 → 数据过期,需关注
- 🔴 **红色**: > 6 小时 → **严重过期,AI 大脑可能已停止**

**管理员行动指南**:
- 如果显示 🔴 红色: **立即** 检查 AI Brain orchestrator 状态
- 如果显示 🟡/🟠 黄色/橙色: 谨慎参考诊断结果,考虑重启
- 如果显示 🟢 绿色: 数据新鲜,可以放心查看分析

**查询**:
```sql
SELECT round((toUnixTimestamp(now()) - toUnixTimestamp(max(ts_ny))) / 3600, 1)
FROM llm.ai_brain_calls
```

---

#### Panel 14: 🕐 最后更新时间 (⭐ v2.0 新增)

**用途**: 显示最后一次 AI 调用的精确时间

**示例显示**:
```
最后更新时间
2025-11-01 14:00:06
```

**配合使用**: 与 "数据新鲜度" 面板配合,快速判断数据是否可用

---

#### Panel 16: 📋 AI 调用统计 (⭐ v2.0 新增)

**用途**: 对比各类型 AI 调用的活跃度

**显示内容**: 各类型调用的统计信息

**Table 列**:
- **调用类型**: propose_plan / decide / evaluate
- **总次数**: 历史总调用次数
- **平均耗时**: 平均处理时间 (ms)
- **最后调用**: 最后一次调用的时间
- **距今 (小时)**: 🎨 **带颜色背景** (绿→黄→橙→红)

**示例**:
| 调用类型 | 总次数 | 平均耗时 | 最后调用 | 距今 (小时) |
|---------|-------|---------|---------|------------|
| decide | 16 | 3,112 ms | 2025-11-01 14:06 | 🟢 0.5 |
| propose_plan | 128 | 25,566 ms | 2025-11-01 14:00 | 🟡 1.2 |
| evaluate | 4 | 23,042 ms | 2025-10-31 14:19 | 🔴 35.3 |

**管理员价值**:
- ✅ 快速识别哪种类型的调用停止了 (如 evaluate 已 35 小时未调用)
- ✅ 对比不同类型的调用频率
- ✅ 监控 AI 大脑的完整工作流程

---

### Row 2: 🔍 AI 诊断与建议 (propose_plan)

#### Panel: 🩺 系统诊断 (Diagnosis)

**显示内容**: AI 对当前系统状态的诊断

**诊断类型**:
- `no_trading_history_due_to_new_system_init` → 系统刚初始化，无交易历史数据
- `normal_operation` → 系统运行正常
- `performance_degradation` → 性能下降，需要调整
- `risk_threshold_breached` → 风险阈值突破

**示例显示**:
```markdown
# AI 诊断结果

**状态**: 系统刚初始化，无交易历史数据

**时间**: 2025-11-01 14:49:00

**分析用时**: 5,577 ms

---

### 诊断说明

系统刚初始化，无交易历史数据
```

---

#### Panel 5: 💊 AI 建议方案 (Prescription) - ⭐ v2.0 优化

**显示内容**: AI 针对诊断结果提出的具体改进方案

**v2.0 改进**: 从 JSON 字符串改为 **结构化表格** ✅

**Table 列**:
- **action**: 建议的操作名称
- **confidence**: 置信度 (0-1) - 🎨 **带颜色背景进度条**
  - 🔴 < 0.5: 低置信度 (红色)
  - 🟡 0.5-0.8: 中等置信度 (黄色)
  - 🟢 > 0.8: 高置信度 (绿色)
- **params**: 操作参数 (JSON,可点击展开)

**示例显示**:

| action | confidence | params |
|--------|-----------|--------|
| enable_baseline_data_collection | 🟢 0.90 | {"providers":["deepseek","gemini"],...} |
| activate_safe_mode_with_limits | 🟢 0.85 | {"restrictions":["no_new_positions"],...} |
| verify_system_health_before_live | 🟡 0.80 | {"require_uptime_check":true,...} |

**字段说明**:
- `action`: 建议的操作 (如: 启用数据收集、激活安全模式)
- `confidence`: AI 的置信度 (越高越可信)
- `params`: 操作的具体参数

**管理员价值**:
- ✅ 快速扫描 AI 的建议列表 (不再需要手动格式化 JSON)
- ✅ 根据置信度优先级决定是否采纳
- ✅ 一目了然的表格显示

---

### Row 3: ⚙️ AI 决策执行 (decide)

#### Panel: 📝 最近配置变更决策

**显示内容**: AI 最近执行的配置修改

**Table 结构**:
| Time | Provider | Config Key | Value | Notes |
|------|----------|------------|-------|-------|
| 2025-11-01 14:49:06 | deepseek_nvidia | `risk_management.guardian.dd_intraday_pct` | -5.0 | Conservative initial settings |

**可展开**: 查看完整的 notes (推理说明)

---

#### Panel: 💭 决策推理

**显示内容**: AI 对最近一次决策的详细推理过程

**示例**:
```markdown
# 💭 AI 推理说明

Conservative initial settings: no trading history available,
starting with safe defaults to build confidence gradually.

---

**时间**: 2025-11-01 14:49:06
**Provider**: deepseek_nvidia
```

---

### Row 4: 📜 完整调用历史

#### Panel: 🔍 AI 调用详细记录

**显示内容**: 所有 AI 调用的完整 JSON 输出（可展开）

**Table 列**:
- `time`: 调用时间
- `kind`: 调用类型 (propose_plan / decide / evaluate_plan)
- `provider`: 使用的 LLM provider
- `duration_ms`: 耗时
- `AI 完整输出 (JSON)`: **可点击展开查看完整 JSON**

**JSON 查看器**:
- ✅ 支持语法高亮
- ✅ 支持折叠/展开
- ✅ 支持复制
- ✅ 显示完整的 diagnosis, prescription, apply, notes

**限制**: 最多显示最近 50 条记录

---

### Row 5: 📊 AI 推理过程时间线

#### Panel 15: 📊 AI 诊断类型时间线 (⭐ v2.0 新增)

**用途**: 可视化 AI 诊断类型随时间的演变

**显示内容**: 堆叠面积图显示不同诊断类型的出现频率

**诊断类型颜色映射**:
- 🔵 **系统初始化** (蓝色) - `no_trading_history_due_to_new_system_init`
- 🟢 **正常运行** (绿色) - `normal_operation`
- 🟡 **性能下降** (黄色) - `performance_degradation`
- 🔴 **风险告警** (红色) - `risk_threshold_breached`

**管理员价值**:
- ✅ 观察 AI 诊断的演变过程 (如: 从 "系统初始化" → "正常运行")
- ✅ 识别系统状态转变的时间点
- ✅ 快速发现异常时段 (如突然出现 "风险告警")

**示例分析**:
```
时间线显示:
08:00-10:00  █████ 系统初始化 (蓝色)
10:00-14:00  █████████ 正常运行 (绿色)
14:00-15:00  ███ 性能下降 (黄色)
15:00-16:00  █ 风险告警 (红色)

管理员判断: 14:00 后系统出现问题,需要查看日志
```

---

#### Panel 12: 📈 AI 调用频率时间线

**显示内容**: AI 调用频率和类型分布（堆叠柱状图）

**时间粒度**: 每小时
**分组**: 按 `kind` (propose_plan, decide, evaluate_plan)

**用途**:
- 观察 AI 活跃度
- 识别异常模式（突然停止或激增）
- 对比不同类型的调用频率

---

## 🎛️ Variables (变量)

### Time Range
- **选项**: 1 hour, 6 hours, 24 hours, 3 days, 7 days
- **默认**: 24 hours
- **影响**: Row 4 (完整调用历史) 和 Row 5 (时间线)

---

## 📖 使用场景

### 场景 1: 查看 AI 最近的决策

**步骤**:
1. 打开 Dashboard
2. 查看 **Row 3: AI 决策执行**
3. 阅读 "📝 最近配置变更决策" 表格
4. 点击 "💭 决策推理" 查看 AI 的思考过程

**你会看到**:
- AI 修改了哪些配置参数
- 每个修改的具体值
- AI 为什么做这个决策（推理说明）

---

### 场景 2: 理解 AI 的诊断和建议

**步骤**:
1. 打开 Dashboard
2. 查看 **Row 2: AI 诊断与建议**
3. 阅读 "🩺 系统诊断" - 了解 AI 认为当前系统状态如何
4. 查看 "💊 AI 建议方案" JSON - 了解 AI 建议做什么

**你会看到**:
- AI 诊断出的问题（如：系统刚初始化、性能下降、风险过高）
- AI 建议的具体方案（如：启用数据收集、激活安全模式）
- 每个建议的置信度

---

### 场景 3: 深度分析 AI 的完整输出

**步骤**:
1. 打开 Dashboard
2. 查看 **Row 4: 完整调用历史**
3. 找到感兴趣的调用记录
4. 点击 "AI 完整输出 (JSON)" 列的单元格
5. 在弹出的 JSON 查看器中查看完整内容

**你会看到**:
- 完整的 `diagnosis` 字段
- 完整的 `prescription` 数组
- 完整的 `apply` 决策数组
- 完整的 `notes` 推理说明

---

### 场景 4: 监控 AI 活跃度

**步骤**:
1. 打开 Dashboard
2. 查看 **Row 5: AI 推理过程时间线**
3. 观察不同时间段的调用频率

**你会看到**:
- AI 在哪些时间段最活跃
- propose_plan 和 decide 的调用比例
- 是否有异常的调用模式（如突然停止）

---

## 🔍 AI 调用类型说明

### 1. propose_plan (提议方案)
**用途**: AI 分析系统状态并提出改进方案

**输出结构**:
```json
{
  "diagnosis": "no_trading_history_due_to_new_system_init",
  "prescription": [
    {
      "action": "enable_baseline_data_collection",
      "params": {...},
      "confidence": 0.9
    }
  ]
}
```

**关键字段**:
- `diagnosis`: 系统状态诊断
- `prescription`: 改进方案数组
  - `action`: 建议的操作
  - `params`: 操作参数
  - `confidence`: 置信度

---

### 2. decide (执行决策)
**用途**: AI 决定具体修改哪些配置参数

**输出结构**:
```json
{
  "apply": [
    {
      "key": "risk_management.guardian.dd_intraday_pct",
      "value": -5.0
    },
    {
      "key": "risk_management.hedge.auto_execute",
      "value": true
    }
  ],
  "notes": "Conservative initial settings..."
}
```

**关键字段**:
- `apply`: 配置变更数组
  - `key`: 配置参数路径
  - `value`: 新值
- `notes`: 决策推理说明

---

### 3. evaluate_plan (评估方案)
**用途**: AI 评估当前方案的执行效果

**输出结构**:
```json
{
  "evaluation": "success",
  "metrics": {...},
  "recommendations": [...]
}
```

---

## 💡 实用技巧

### 技巧 1: 快速查看最近的 AI 思考

**最快路径**:
1. 打开 Dashboard
2. 直接看 Row 3 的 "💭 决策推理" 面板

**你会看到**: AI 最近一次的推理说明（通常是最有价值的信息）

---

### 技巧 2: 对比不同时间点的 AI 决策

**步骤**:
1. Row 4 查看完整历史
2. 筛选 `kind = decide`
3. 对比不同时间点的 `apply` 字段
4. 观察 AI 如何调整参数

**用途**: 理解 AI 的学习过程

---

### 技巧 3: 导出 AI 分析结果

**方法 1: 从 Table 导出**
1. 点击 Table 右上角的菜单
2. 选择 "Export CSV"

**方法 2: 直接查询 ClickHouse**
```sql
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%M:%S') as time,
    kind,
    provider,
    output_json
FROM llm.ai_brain_calls
WHERE kind = 'propose_plan'
ORDER BY ts_ny DESC
LIMIT 10
FORMAT JSONEachRow
```

---

## 📊 数据来源

**表**: `llm.ai_brain_calls`

**关键字段**:
```sql
CREATE TABLE llm.ai_brain_calls (
    ts_ny DateTime('America/New_York'),
    date Date,
    kind String,                    -- propose_plan / decide / evaluate_plan
    session String,
    provider String,
    model String,
    input_keys Array(String),
    output_json String,             -- 🔑 核心：AI 的完整输出
    duration_ms Int32,
    trading_universe_count Int32,
    success UInt8,
    error_type String,
    error_message String,
    fallback_used UInt8,
    providers_attempted Array(String)
)
```

**关键**: `output_json` 字段包含了 AI 的所有分析结果

---

## ⚠️ 已知限制

### 限制 1: 数据新鲜度

**问题**: 如果 AI Brain orchestrator 停止运行，数据会过期

**检查方法**:
```sql
SELECT max(ts_ny) FROM llm.ai_brain_calls
-- 如果超过 1 小时，说明数据过期
```

**解决方法**: 重启 orchestrator

---

### 限制 2: JSON 查看器性能

**问题**: 如果 `output_json` 非常大（>10KB），查看器可能卡顿

**解决方法**: 使用 ClickHouse 直接查询并用 `jq` 格式化
```bash
docker exec trading_clickhouse clickhouse-client --query "
SELECT output_json FROM llm.ai_brain_calls
WHERE kind = 'propose_plan'
ORDER BY ts_ny DESC LIMIT 1
FORMAT TSVRaw
" | jq .
```

---

### 限制 3: 历史数据

**问题**: 只显示最近 50 条记录（性能考虑）

**解决方法**:
- 调整 Time Range 变量
- 或直接查询 ClickHouse 获取更多数据

---

## 🔧 故障排查

### 问题 1: Dashboard 显示 "No data"

**可能原因**:
1. AI Brain orchestrator 未运行
2. 数据过期（超过 Time Range）
3. ClickHouse 连接问题

**检查步骤**:
```sql
-- 1. 检查是否有数据
SELECT count() FROM llm.ai_brain_calls

-- 2. 检查最近的数据
SELECT max(ts_ny) FROM llm.ai_brain_calls

-- 3. 检查是否有 propose_plan
SELECT count() FROM llm.ai_brain_calls WHERE kind = 'propose_plan'
```

---

### 问题 2: JSON 无法展开

**可能原因**: Grafana 版本不支持 json-view cell type

**解决方法**: 直接查询 ClickHouse
```bash
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT output_json FROM llm.ai_brain_calls
WHERE kind = 'propose_plan'
ORDER BY ts_ny DESC LIMIT 1
FORMAT JSONEachRow
" | python3 -c "import sys, json; print(json.dumps(json.loads(sys.stdin.read()), indent=2))"
```

---

### 问题 3: 数据源报错 "clickhouse not found"

**可能原因**: Datasource UID 配置错误

**解决方法**: 已修复为 `ch`，如果仍报错：
```bash
# 检查 datasource UID
curl -s -u admin:admin http://localhost:3000/api/datasources | jq '.[] | select(.type=="grafana-clickhouse-datasource") | {name, uid}'
```

---

## 📁 文件位置

**Provisioned Dashboard**:
```
./grafana/dashboards/active/ai_brain_detailed_analysis.json
```

**Export Backup**:
```
./grafana/dashboards/exported/ai-brain-detailed_20251102_015608.json
```

**使用文档** (本文件):
```
./AI_BRAIN_DETAILED_DASHBOARD_README.md
```

---

## 🎯 下一步改进

### 建议 1: 添加 AI 对话历史

如果 AI Brain 支持多轮对话，可以添加：
- 对话上下文展示
- 多轮推理过程
- 用户反馈记录

### 建议 2: AI 决策效果追踪

添加 Panel 对比：
- AI 建议的方案 vs 实际执行效果
- 预测收益 vs 实际收益
- 置信度 vs 成功率

### 建议 3: AI 推理可视化

使用 Sankey 或 Flow 图展示：
- 诊断 → 建议 → 决策 → 执行 的流程
- 不同方案的分支和选择

---

**创建时间**: 2025-11-02 01:56 EST
**Dashboard URL**: http://localhost:3000/d/ai-brain-detailed/ai-brain-detailed-analysis
**作者**: Claude AI
**版本**: 1.0
