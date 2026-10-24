# 🧠 Brain Calls 监控系统完整指南

**创建日期**: 2025-10-29
**状态**: ✅ 已完成并测试
**测试覆盖**: 16 个新增测试 (100% 通过率)

---

## 📋 系统概览

Brain Calls 监控系统提供了完整的 AI 大脑调用追踪、持久化存储和可视化监控，确保 Control Brain 的所有决策都有迹可循。

### 核心组件

```
┌─────────────────────────────────────────────────────────┐
│                    Control Brain                        │
│  - propose_plan() → 生成控制计划                         │
│  - decide() → 关键决策审批                               │
└────────────┬────────────────────────────────────────────┘
             │
             ↓ (追踪)
┌─────────────────────────────────────────────────────────┐
│          runs/ai_trader/YYYY-MM-DD/                     │
│              brain_calls.jsonl                          │
│  - 每次调用写入一行                                      │
│  - 包含完整元数据 (ts, kind, provider, I/O)             │
└────────────┬────────────────────────────────────────────┘
             │
             ↓ (导出)
┌─────────────────────────────────────────────────────────┐
│         ClickHouse: ai_trader_events                    │
│  - kind='brain' 标记                                    │
│  - 自动提取 session/action                              │
│  - Payload 存储为 JSON                                  │
└────────────┬────────────────────────────────────────────┘
             │
             ↓ (可视化)
┌─────────────────────────────────────────────────────────┐
│              监控界面 (3种方式)                          │
│  1. Grafana Dashboard (生产级)                          │
│  2. Streamlit Dashboard (开发调试)                      │
│  3. ClickHouse 直接查询                                 │
└─────────────────────────────────────────────────────────┘
```

---

## 🔧 1. Brain Calls 追踪 (brain.py)

### 实现位置
`scripts/ai_trader/brain/brain.py`

### 功能特性

#### 自动追踪
每次 LLM 调用自动写入 `runs/ai_trader/YYYY-MM-DD/brain_calls.jsonl`:

```json
{
  "ts": "2025-10-29T10:30:45.123456",
  "kind": "propose_plan",
  "session": "morning",
  "provider": "deepseek_nvidia",
  "model": "deepseek-ai/deepseek-r1",
  "input_keys": ["health", "gates", "registry"],
  "output": {
    "action": "proceed",
    "checks": ["health_gate", "data_gate"],
    "reasoning": "All systems healthy"
  }
}
```

#### 配置选项
```python
brain = ControlBrain(runs_dir="runs/ai_trader")
brain.trace_enabled = True  # 默认开启
brain.trace_enabled = False  # 关闭追踪
```

#### Best-Effort 设计
- ✅ 文件写入失败不会中断主流程
- ✅ 非序列化对象会被优雅跳过
- ✅ 目录不存在会自动创建

### 测试覆盖 (8 tests)

**文件**: `scripts/ai_trader/tests/unit/test_brain.py::TestBrainCallsTracing`

| 测试 | 验证内容 |
|------|---------|
| `test_trace_enabled_by_default` | 默认启用追踪 |
| `test_trace_write_creates_file` | 创建 JSONL 文件 |
| `test_trace_write_disabled` | 禁用时不写入 |
| `test_trace_write_handles_errors_gracefully` | 错误不崩溃 |
| `test_propose_plan_writes_trace` | propose_plan 写入完整记录 |
| `test_decide_writes_trace` | decide 写入完整记录 |
| `test_multiple_calls_append_to_trace` | 多次调用追加 |
| `test_trace_includes_timestamp` | ISO 时间戳 |

**运行测试**:
```bash
python -m pytest scripts/ai_trader/tests/unit/test_brain.py::TestBrainCallsTracing -v
```

---

## 📤 2. ClickHouse 导出 (events_exporter.py)

### 实现位置
`scripts/ai_trader/exporters/events_exporter.py`

### 功能特性

#### 自动导出时机
```python
# 晨间序列结束后
orch.morning_sequence()
export_events(runs_dir, date.today().isoformat(), session='morning')

# 盘中每次执行完计划后
orch.intraday_sequence()
export_events(runs_dir, date.today().isoformat(), session='intraday')

# 盘后学习结束后
orch.learn()
export_events(runs_dir, date.today().isoformat(), session='learn')
```

#### 数据库表结构
```sql
CREATE TABLE IF NOT EXISTS ai_trader_events (
    date Date,
    ts DateTime,
    run_name String,
    session LowCardinality(String),
    kind LowCardinality(String),       -- 'brain' 标记
    symbol String,
    payload String                      -- 完整 JSON
) ENGINE=MergeTree
ORDER BY (date, kind, ts)
```

#### Session 智能提取
```python
# propose_plan: 使用 payload.session
{
  "kind": "propose_plan",
  "session": "morning"     # → 存入 session 列
}

# decide: 使用 payload.action
{
  "kind": "decide",
  "action": "hpo_deploy"   # → 存入 session 列
}
```

### 测试覆盖 (8 tests)

**文件**: `scripts/ai_trader/tests/unit/test_events_exporter.py::TestBrainCallsExport`

| 测试 | 验证内容 |
|------|---------|
| `test_brain_calls_included_in_file_list` | brain_calls.jsonl 被导出 |
| `test_brain_calls_parsed_correctly` | 正确解析多条记录 |
| `test_brain_calls_session_from_payload` | 提取 payload.session |
| `test_brain_calls_action_as_fallback_session` | 提取 payload.action |
| `test_brain_calls_no_file_graceful` | 文件缺失不报错 |
| `test_brain_calls_invalid_json_skipped` | 跳过无效 JSON |
| `test_brain_calls_payload_json_encoded` | Payload 正确编码 |
| `test_brain_calls_timestamp_parsing` | 时间戳正确解析 |

**运行测试**:
```bash
python -m pytest scripts/ai_trader/tests/unit/test_events_exporter.py -v
```

---

## 📊 3. Grafana 大屏 (生产级监控)

### 配置文件
`dashboards/AI_Trader_Overview.json`

### 导入步骤

1. **打开 Grafana**
   ```bash
   # 访问 http://localhost:3000/
   ```

2. **导入 Dashboard**
   - Dashboards → Import
   - 选择 `dashboards/AI_Trader_Overview.json`
   - 绑定 ClickHouse 数据源

3. **配置数据源 UID**
   ```json
   {
     "datasource": {
       "type": "grafana-clickhouse-datasource",
       "uid": "YOUR_CLICKHOUSE_UID"  // 替换为你的 UID
     }
   }
   ```

### Brain Calls 面板

**面板名称**: "Brain Calls (Recent)"

**SQL 查询**:
```sql
SELECT
    ts,
    JSONExtractString(payload, 'kind') AS kind,
    JSONExtractString(payload, 'session') AS session,
    JSONExtractString(payload, 'action') AS action,
    JSONExtractString(payload, 'provider') AS provider,
    left(JSONExtractString(payload, 'output'), 200) AS output_snip
FROM ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 2 DAY
ORDER BY ts DESC
LIMIT 200
```

**展示字段**:
- `ts` - 调用时间
- `kind` - propose_plan / decide
- `session` - morning / intraday / learn
- `action` - hpo_deploy / start_morning
- `provider` - deepseek_nvidia / openai
- `output_snip` - 输出前 200 字符

### 高级查询示例

#### 1. 按 Provider 统计调用量
```sql
SELECT
    JSONExtractString(payload, 'provider') AS provider,
    count() AS call_count
FROM ai_trader_events
WHERE kind = 'brain'
  AND toDate(ts) = today()
GROUP BY provider
ORDER BY call_count DESC
```

#### 2. 查找失败的调用
```sql
SELECT
    ts,
    JSONExtractString(payload, 'kind') AS kind,
    JSONExtractString(payload, 'output.error') AS error
FROM ai_trader_events
WHERE kind = 'brain'
  AND JSONHas(payload, 'output.error')
  AND ts > now() - INTERVAL 7 DAY
ORDER BY ts DESC
```

#### 3. Session 耗时分析
```sql
SELECT
    JSONExtractString(payload, 'session') AS session,
    count() AS calls,
    round(avg(length(payload)), 2) AS avg_payload_size
FROM ai_trader_events
WHERE kind = 'brain'
  AND toDate(ts) = today()
GROUP BY session
```

---

## 🎛️ 4. Streamlit 监控仪表板 (开发调试)

### 启动方式
```bash
streamlit run scripts/ai_trader/monitoring_dashboard.py
```

**访问地址**: http://localhost:8501/

### 功能特性

#### 实时监控
- ✅ 最新 Brain Plan (propose_plan 输出)
- ✅ 关键决策审批 (decide 调用)
- ✅ 实时进程状态 (PID, Heartbeat)
- ✅ MLflow 实验追踪

#### 数据源
- `runs/ai_trader/YYYY-MM-DD/brain_plan_*.json` - 计划文件
- `runs/ai_trader/YYYY-MM-DD/brain_calls.jsonl` - 调用追踪
- `runs/ai_trader/YYYY-MM-DD/procs/` - 进程元数据

#### 优势
- ✅ 无需 ClickHouse (直接读取本地文件)
- ✅ 适合开发调试
- ✅ 自动刷新
- ✅ 交互式筛选

---

## 🔍 5. ClickHouse 直接查询

### 连接方式
```bash
clickhouse-client --host localhost
```

### 常用查询

#### 查看最近 10 条 Brain Calls
```sql
SELECT
    ts,
    payload
FROM ai_trader_events
WHERE kind = 'brain'
ORDER BY ts DESC
LIMIT 10
FORMAT Vertical;
```

#### 导出为 JSON
```bash
clickhouse-client --query "
SELECT payload
FROM ai_trader_events
WHERE kind = 'brain'
  AND toDate(ts) = today()
ORDER BY ts DESC
FORMAT JSONEachRow" > brain_calls_$(date +%Y%m%d).json
```

#### 统计今日调用
```bash
clickhouse-client --query "
SELECT
    JSONExtractString(payload, 'kind') AS kind,
    JSONExtractString(payload, 'session') AS session,
    count() AS cnt
FROM ai_trader_events
WHERE kind = 'brain'
  AND toDate(ts) = today()
GROUP BY kind, session
WITH TOTALS"
```

---

## 📈 6. 监控指标与告警

### 关键指标

#### 1. 调用成功率
```sql
SELECT
    countIf(NOT JSONHas(payload, 'output.error')) AS success,
    countIf(JSONHas(payload, 'output.error')) AS failure,
    round(success * 100.0 / (success + failure), 2) AS success_rate
FROM ai_trader_events
WHERE kind = 'brain'
  AND toDate(ts) = today()
```

**告警阈值**: success_rate < 95%

#### 2. 平均响应时间 (需添加 duration 字段)
```sql
SELECT
    JSONExtractString(payload, 'session') AS session,
    round(avg(JSONExtractFloat(payload, 'duration_ms')), 2) AS avg_duration_ms
FROM ai_trader_events
WHERE kind = 'brain'
  AND toDate(ts) = today()
GROUP BY session
```

**告警阈值**: avg_duration_ms > 30000 (30秒)

#### 3. 每小时调用量
```sql
SELECT
    toStartOfHour(ts) AS hour,
    count() AS calls
FROM ai_trader_events
WHERE kind = 'brain'
  AND ts > now() - INTERVAL 24 HOUR
GROUP BY hour
ORDER BY hour
```

**告警阈值**:
- calls < 1 (1小时无调用，可能异常)
- calls > 100 (调用过于频繁)

### 配置 Grafana 告警

#### 告警规则示例
```yaml
# grafana_alert_brain_calls.yml
groups:
  - name: brain_calls_alerts
    interval: 5m
    rules:
      - alert: BrainCallsHighFailureRate
        expr: |
          (sum(rate(ai_trader_events{kind="brain",error!=""}[5m])) /
           sum(rate(ai_trader_events{kind="brain"}[5m]))) > 0.05
        for: 10m
        labels:
          severity: critical
        annotations:
          summary: "Brain Calls 失败率过高"
          description: "过去 10 分钟失败率 {{ $value | humanizePercentage }}"

      - alert: BrainCallsNoActivity
        expr: |
          sum(increase(ai_trader_events{kind="brain"}[1h])) == 0
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "Brain Calls 无活动"
          description: "过去 1 小时无任何 Brain 调用"
```

---

## 🧪 7. 测试验证

### 完整测试套件统计

| 测试类别 | 文件 | 测试数 | 通过率 |
|---------|------|--------|--------|
| Brain Tracing | test_brain.py | 8 | 100% ✅ |
| Events Export | test_events_exporter.py | 8 | 100% ✅ |
| **总计** | - | **16** | **100%** ✅ |

### 运行所有测试
```bash
# 所有 Brain Calls 相关测试
python -m pytest \
  scripts/ai_trader/tests/unit/test_brain.py::TestBrainCallsTracing \
  scripts/ai_trader/tests/unit/test_events_exporter.py \
  -v

# 完整测试套件 (217 tests)
python -m pytest scripts/ai_trader/tests/ -v

# 快速验证 (无详细输出)
python -m pytest scripts/ai_trader/tests/ -q
```

### 端到端验证流程

#### 步骤 1: 生成 Brain Calls
```bash
# 运行晨间序列
python -c "
from scripts.ai_trader.orchestrator import Orchestrator
orch = Orchestrator()
orch.morning_sequence()
"
```

#### 步骤 2: 验证文件生成
```bash
# 查看今日 brain_calls.jsonl
TODAY=$(date +%Y-%m-%d)
cat runs/ai_trader/$TODAY/brain_calls.jsonl | jq .

# 统计调用次数
wc -l runs/ai_trader/$TODAY/brain_calls.jsonl
```

#### 步骤 3: 导出到 ClickHouse
```bash
python -c "
from scripts.ai_trader.exporters.events_exporter import export_events
from datetime import date
export_events('runs/ai_trader', date.today().isoformat(), 'morning')
"
```

#### 步骤 4: 查询验证
```bash
clickhouse-client --query "
SELECT
    count() AS total_calls,
    countIf(JSONExtractString(payload, 'kind') = 'propose_plan') AS propose_plans,
    countIf(JSONExtractString(payload, 'kind') = 'decide') AS decisions
FROM ai_trader_events
WHERE kind = 'brain'
  AND toDate(ts) = today()
"
```

#### 步骤 5: 查看 Grafana
1. 打开 http://localhost:3000/
2. 找到 "AI Trader Overview" Dashboard
3. 查看 "Brain Calls (Recent)" 面板
4. 验证数据正确显示

---

## 🔧 8. 故障排查

### 问题 1: brain_calls.jsonl 未生成

**症状**: 运行后没有 brain_calls.jsonl 文件

**排查步骤**:
```bash
# 1. 检查追踪是否启用
python -c "
from scripts.ai_trader.brain.brain import ControlBrain
brain = ControlBrain(runs_dir='runs/ai_trader')
print('Trace enabled:', brain.trace_enabled)
"

# 2. 检查目录权限
ls -la runs/ai_trader/$(date +%Y-%m-%d)/

# 3. 手动测试写入
python -c "
from scripts.ai_trader.brain.brain import ControlBrain
brain = ControlBrain(runs_dir='runs/ai_trader')
brain._trace_write({'test': 'data'})
"
```

### 问题 2: ClickHouse 导出失败

**症状**: export_events() 不报错但数据库无数据

**排查步骤**:
```bash
# 1. 检查 ClickHouse 连接
python -c "
from libs.db.clickhouse import ClickHouseDB
ch = ClickHouseDB()
print('Tables:', ch.query('SHOW TABLES'))
"

# 2. 检查表是否存在
clickhouse-client --query "SHOW CREATE TABLE ai_trader_events"

# 3. 手动查询
clickhouse-client --query "SELECT count() FROM ai_trader_events WHERE kind='brain'"

# 4. 查看日志
grep "events_exported" logs/trading_system.log | tail -20
```

### 问题 3: Grafana 面板无数据

**症状**: Grafana "Brain Calls (Recent)" 面板显示空

**排查步骤**:
1. **验证数据源连接**
   - Settings → Data Sources → ClickHouse
   - Test & Save

2. **直接在 Grafana Explore 运行查询**
   ```sql
   SELECT * FROM ai_trader_events WHERE kind='brain' LIMIT 10
   ```

3. **检查时间范围**
   - 确保面板时间范围包含数据时间
   - 尝试 "Last 7 days"

4. **检查 JSON 函数支持**
   ```sql
   -- 测试 JSONExtractString 是否可用
   SELECT JSONExtractString('{"a":"b"}', 'a')
   ```

---

## 📚 9. 最佳实践

### 开发环境
- ✅ 使用 Streamlit Dashboard 快速调试
- ✅ 定期运行测试套件
- ✅ 本地保留最近 7 天的 brain_calls.jsonl

### 生产环境
- ✅ 使用 Grafana Dashboard 监控
- ✅ 配置告警规则
- ✅ ClickHouse 数据保留 30 天 (通过 TTL)
- ✅ 每日备份关键决策记录

### 数据保留策略
```sql
-- 设置 TTL (已在 events_exporter.py 中配置)
ALTER TABLE ai_trader_events
MODIFY TTL date + INTERVAL 30 DAY
```

### 性能优化
```sql
-- 添加索引加速查询
ALTER TABLE ai_trader_events
ADD INDEX idx_kind kind TYPE minmax GRANULARITY 4;

-- 物化视图加速统计
CREATE MATERIALIZED VIEW brain_calls_daily
ENGINE = SummaryTable
AS SELECT
    toDate(ts) AS date,
    JSONExtractString(payload, 'session') AS session,
    count() AS calls
FROM ai_trader_events
WHERE kind = 'brain'
GROUP BY date, session
```

---

## 🚀 10. 后续扩展建议

### 高级功能
1. **LLM 成本追踪**
   - 添加 token_count 字段
   - 按 provider 统计成本
   - 月度成本报表

2. **决策回溯分析**
   - 关联 brain_calls → decisions → trades → PnL
   - 分析哪些决策带来收益
   - A/B 测试不同 Provider 效果

3. **异常检测**
   - 训练模型识别异常调用模式
   - 自动标记可疑决策
   - 实时告警

4. **多环境支持**
   - Paper Trading vs Live Trading 分离
   - 不同环境不同 ClickHouse 表
   - 环境标签

### 集成增强
- Slack / 钉钉告警
- PagerDuty 集成
- Prometheus Exporter
- Custom Metrics API

---

## 📝 11. 总结

### ✅ 已完成功能
- ✅ Brain Calls 自动追踪 (brain.py)
- ✅ ClickHouse 持久化存储 (events_exporter.py)
- ✅ Grafana 可视化监控 (AI_Trader_Overview.json)
- ✅ Streamlit 开发仪表板 (monitoring_dashboard.py)
- ✅ 完整测试覆盖 (16 tests, 100% pass)

### 📊 测试覆盖总结
| 组件 | 测试数 | 通过率 | 文件 |
|------|--------|--------|------|
| Brain Tracing | 8 | 100% | test_brain.py |
| Events Export | 8 | 100% | test_events_exporter.py |
| **Total** | **16** | **100%** | - |

### 🎯 核心价值
1. **可观测性** - 所有 AI 决策有迹可循
2. **调试效率** - 快速定位问题决策
3. **性能监控** - 实时追踪 LLM 调用状态
4. **审计合规** - 完整的决策审计日志
5. **优化依据** - 数据驱动的模型优化

---

**最后更新**: 2025-10-29
**维护者**: Trading Team
**相关文档**:
- `TEST_TOOLS_COVERAGE.md` - 工具健康检查
- `CLAUDE.md` - 开发规范
- `SFS_QUICK_REFERENCE.md` - SFS 系统参考
