# AI Dashboard 使用指南

**Last Updated**: 2025-11-01 17:30 EST
**Status**: Phase 1+2 完成 ✅ | 🔴 **P0 Critical Fixes Applied** ✅

---

## 📊 **当前可用 Dashboard**

### Dashboard 1: AI Trading Performance
**状态**: ✅ 已部署 | 🔴 **P0 修复完成**
**URL**: http://localhost:3000/d/ai-trading-performance
**用途**: 交易表现总览

**🔴 新增关键功能 (P0 Critical Fixes)**:
- ⚠️ **Data Freshness Indicator** - 数据新鲜度实时监控（避免基于过期数据交易）
- 📊 **Trading Activity Monitor** - 今日成交监控（检测系统是否在工作）
- 🚦 **System Status Indicator** - 系统健康状态综合告警

**核心指标**:
- 💵 Today's P&L: $2,165.55
- 📊 Net Liquidation: $238,977.08
- 🎯 Open Positions: 3 (AAPL, TSLA, NVDA)
- 📈 7-day fills: 7 笔成交
- ⚠️ **Data Age**: Real-time monitoring (GREEN < 10min, RED > 6h)
- 📊 **Today's Fills**: 0 fills alert during market hours

**数据源**:
- `llm.ibkr_account_realtime` - 账户快照
- `llm.ibkr_positions_realtime` - 持仓快照
- `llm.llm_fills` - 成交记录

---

### Dashboard 2: AI Brain Command Center
**状态**: ✅ 已部署
**URL**: http://localhost:3000/d/ai-brain-command-center
**用途**: AI 大脑决策可视化

**核心视图**:
- 🎯 **Brain Status**: 当前会话、提供商、交易标的、A/B 分配
- 📊 **Activity Timeline**: 调用时间线、类型分布
- 🔍 **Performance**: 延迟分析、提供商分布
- 📝 **Plans History**: 执行计划表
- 📈 **Calls History**: 详细调用记录

**关键发现**:
- ⚡ **decide** 最快 (3.1秒)
- 🐢 **propose_plan** 最慢 (25.6秒)
- 🤖 100% 使用 **deepseek_nvidia**
- 📊 86.5% 调用是 **propose_plan**

---

### Dashboard 3: AI Brain Performance Analysis
**状态**: ✅ 已部署 | 🔴 **Health Monitoring 增强**
**URL**: http://localhost:3000/d/ai-brain-performance
**用途**: AI 大脑性能深度分析

**🔴 新增健康监控 (Health & Error Monitoring)**:
- 🐢 **Slow Calls Monitor** - 监控 >60s 慢调用（性能异常检测）
- 📊 **Call Rate Change** - 检测调用频率异常（vs yesterday）
- ⏰ **Last Call Time** - 监控 AI Brain 是否停止工作

**核心指标**:
- 📊 Total Calls: 148 (7 days)
- ⏱️ Average Latency: 23.1 秒
- 📈 P95 Latency: 50.7 秒 🔴 **需要优化**
- 🔄 Calls per Hour: 4.8
- 🐢 **Slow Calls (>60s)**: Real-time tracking
- 📊 **Call Rate**: Day-over-day comparison

**核心视图**:
- ⚡ **Performance Overview**: 调用量、延迟统计
- 🚨 **Health & Error Monitoring**: 慢调用、调用频率、最近调用时间 (NEW)
- 📊 **Latency Analysis**: 延迟趋势、分位数分析
- 🤖 **Provider & Model**: 提供商和模型使用分析
- 📈 **Session & Input**: 会话分布、输入参数模式
- 🌍 **Trading Universe**: 交易标的数量趋势

**性能问题发现**:
- 🔴 P95 延迟 50.7s - 严重慢，需要优化
- ⚠️ 100% 依赖 deepseek_nvidia - 单点故障风险
- 📊 overnight 会话占比 42.6% - 最活跃

---

### Dashboard 4: LLM Sessions Monitor
**状态**: ✅ 已部署 | 🔴 **Zombie Session Detection**
**URL**: http://localhost:3000/d/llm-sessions-monitor
**用途**: LLM 会话生命周期监控

**🔴 新增僵尸会话检测 (P0 Critical Fix)**:
- ⚠️ **Zombie Sessions Counter** - 监控 RUNNING >24h 的僵尸会话
- 📋 **Zombie Sessions Table** - 详细列出僵尸会话（按运行时长排序）

**核心指标**:
- 📊 Total Sessions: 574 (7 days)
- 🔄 Running Sessions: Real-time count
- ⚠️ **Zombie Sessions**: >24h running (should be ~0)
- 🤖 Bot Types: AI trader variants
- 🎯 Decisions Made: 464 (7 days)

**核心视图**:
- 📊 **Sessions Overview**: 总会话数、运行中、僵尸会话 (NEW)、Bot 类型、决策数
- 📈 **Sessions Timeline**: 会话启动趋势、活跃会话数
- 🤖 **Bot & Model Distribution**: 按 Bot、模型、模式分布
- 💼 **Trading Activity**: 按决策类型、决策时间线
- 📋 **Recent Sessions**: 僵尸会话表 (NEW)、最近会话列表

**数据源**:
- `llm.llm_sessions` - 会话生命周期
- `llm.llm_decisions` - AI 决策记录

**告警逻辑**:
- 🟢 0 zombies: 健康
- 🟡 1-9 zombies: 警告
- 🟠 10-49 zombies: 严重
- 🔴 50+ zombies: 致命（需要立即清理）

---

## ⚡ **快速命令参考**

### 数据导入
```bash
# 导入所有历史 AI Brain 日志
python scripts/ingest_brain_logs.py --all

# 导入今日数据
python scripts/ingest_brain_logs.py

# 导入最近 7 天
python scripts/ingest_brain_logs.py --days 7
```

### Dashboard 生成
```bash
# 重新生成 Dashboard 1
python grafana/ai_boss_dashboard/generate_trading_performance_dashboard.py

# 重新生成 Dashboard 2
python grafana/ai_boss_dashboard/generate_ai_brain_dashboard.py

# 重新生成 Dashboard 3
python grafana/ai_boss_dashboard/generate_ai_brain_performance_dashboard.py
```

### 数据验证
```bash
# AI Brain 调用统计
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT kind, count(*) as cnt, round(avg(duration_ms), 1) as avg_ms
FROM llm.ai_brain_calls
GROUP BY kind
ORDER BY cnt DESC
"

# 最近执行计划
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%M') as time,
    session,
    arrayStringConcat(providers, ', ') as providers,
    arrayStringConcat(symbols, ', ') as symbols
FROM llm.ai_brain_plans
ORDER BY ts_ny DESC
LIMIT 10
FORMAT Pretty
"

# 账户状态
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%M:%S') as time,
    net_liquidation,
    unrealized_pnl,
    realized_pnl
FROM llm.ibkr_account_realtime
ORDER BY ts_ny DESC
LIMIT 1
"
```

---

## 🎯 **使用工作流**

### 交易员日常

**早上开盘前** (09:00 - 09:30):
1. Dashboard 1: 检查账户状态和昨日 P&L
2. Dashboard 2: 确认 overnight 计划已执行
3. Dashboard 3: 检查 AI 性能指标（延迟是否正常）

**盘中监控** (09:30 - 16:00):
1. Dashboard 1: 实时 P&L 监控 (每 30 秒刷新)
2. Dashboard 2: AI 决策监控 (intraday 会话调用)
3. Dashboard 3: 监控 AI 延迟和调用频率

**盘后复盘** (16:00 - 17:00):
1. Dashboard 1: 统计当日总 P&L 和成交
2. Dashboard 2: 分析全天 AI 行为
3. Dashboard 3: 分析性能瓶颈和优化机会

### 管理员监控

**每日检查**:
```bash
# 验证数据导入
python scripts/ingest_brain_logs.py --date $(date +%Y-%m-%d)

# 检查数据量
docker exec -i trading_clickhouse clickhouse-client --query "
SELECT 'calls' as type, count(*) FROM llm.ai_brain_calls WHERE date = today()
UNION ALL
SELECT 'plans', count(*) FROM llm.ai_brain_plans WHERE date = today()
"
```

---

## 📚 **参考文档**

**成功报告**:
- `grafana/ai_boss_dashboard/PHASE1_SUCCESS_REPORT.md` - Dashboard 1 报告
- `grafana/ai_boss_dashboard/PHASE1B_SUCCESS_REPORT.md` - Dashboard 2 报告
- `grafana/ai_boss_dashboard/PHASE2_COMPLETION_REPORT.md` - Dashboard 3 报告

**设计文档**:
- `grafana/ai_boss_dashboard/AI_TRADING_DASHBOARDS_MASTER_PLAN.md` - 总体设计

**生成脚本**:
- `grafana/ai_boss_dashboard/generate_trading_performance_dashboard.py`
- `grafana/ai_boss_dashboard/generate_ai_brain_dashboard.py`
- `grafana/ai_boss_dashboard/generate_ai_brain_performance_dashboard.py`

**数据导入**:
- `scripts/ingest_brain_logs.py`

---

## ⚠️ **性能优化建议**

基于 Dashboard 3 发现的问题：

1. **优化 AI Brain 延迟** (P95 50.7s 太慢)
   - 分析 propose_plan 为何需要 25+ 秒
   - 优化 prompt 长度和复杂度
   - 实现结果缓存机制
   - 考虑异步化处理

2. **添加 Provider Fallback**
   - 当前 100% 依赖 deepseek_nvidia
   - 配置 Gemini 或 OpenAI 备用
   - 实现自动故障切换
   - 监控各提供商可用性

3. **增加监控告警**
   - P95 延迟 > 60s 告警
   - Provider 失败率 > 5% 告警
   - 每小时调用数异常告警

---

**Status**: Phase 1+2 完成 ✅ | 🔴 **P0 Critical Fixes Applied** ✅
**Dashboards**: 4/4 完成 (75 components total)
**Data**: 4 Tables, 154 Records
**Health Monitoring**: Data Freshness, Trading Activity, Zombie Sessions, Error Tracking
