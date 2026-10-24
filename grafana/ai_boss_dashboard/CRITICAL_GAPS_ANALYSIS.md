# Dashboard 致命缺陷分析 - 基于真实用户视角

**Date**: 2025-11-01 17:00 EST
**Method**: 以交易员和系统管理员视角实际验证 Dashboard
**Result**: 🔴 **发现多个致命缺陷**

---

## 🔴 **致命问题 1: 数据已经过期 13 小时**

### 问题描述
```
最后更新时间: 2025-11-01 03:26:32 ET
当前时间:     2025-11-01 16:55:00 ET
数据延迟:     13.5 小时 🔴
```

### 交易员视角
**问题**: "我打开 Dashboard 1 看到的 P&L 是 13 小时前的！"

**影响**:
- ❌ 无法实时监控盈亏
- ❌ 看不到最新持仓状态
- ❌ 根本不知道现在账户情况如何

**根本原因**:
- sync_ibkr_realtime 服务已停止 13 小时
- Dashboard 显示的是过期数据,但用户无法察觉

**Dashboard 缺陷**:
- ❌ **没有数据新鲜度指示器**
- ❌ **没有"最后更新时间"标签**
- ❌ **没有过期数据告警**

### 建议修复

**紧急** (必须添加):
1. 在每个 Dashboard 顶部添加 **"数据更新时间"** panel
2. 如果数据 > 10 分钟,显示 ⚠️ 黄色告警
3. 如果数据 > 1 小时,显示 🔴 红色告警 + "数据已过期"

**Dashboard 1 必须添加**:
```json
{
  "type": "stat",
  "title": "⚠️ 数据新鲜度",
  "targets": [{
    "rawSql": "
      SELECT
        formatDateTime(ts_ny, '%H:%M:%S') as last_update,
        round((now() - ts_ny) / 60, 0) as minutes_ago
      FROM llm.ibkr_account_realtime
      ORDER BY ts_ny DESC
      LIMIT 1
    "
  }],
  "fieldConfig": {
    "thresholds": {
      "steps": [
        {"value": 0, "color": "green"},    // < 10 min
        {"value": 10, "color": "yellow"},  // 10-60 min
        {"value": 60, "color": "red"}      // > 60 min
      ]
    }
  }
}
```

---

## 🔴 **致命问题 2: 持仓盈亏全是 0 - 完全误导**

### 问题描述
```sql
SELECT symbol, position, avg_cost, market_price, unrealized_pnl
FROM llm.ibkr_positions_realtime
WHERE position > 0;

结果:
AAPL  187  266.68  266.68  0  -- unrealized_pnl = 0 ❌
TSLA  100  448.17  448.17  0  -- unrealized_pnl = 0 ❌
NVDA  177  198.36  198.36  0  -- unrealized_pnl = 0 ❌

但是账户总 unrealized_pnl = $2,165.55 ✅
```

### 交易员视角
**问题**: "Dashboard 显示每个持仓盈亏都是 $0,但账户总盈亏却是 $2,165！到底哪个对？"

**影响**:
- ❌ 无法知道哪个股票赚钱,哪个亏钱
- ❌ 无法做持仓管理决策
- ❌ 数据矛盾导致不信任 Dashboard

**根本原因**:
- `ibkr_positions_realtime` 表的 `unrealized_pnl` 字段为 0
- `market_price = avg_cost` (IBKR 市场关闭后不更新报价)
- 虽然 Dashboard 1 用了"诚实估算",但实际查询没有使用！

**Dashboard 1 生成的查询** (错误):
```sql
-- 当前查询（错误）
SELECT
    symbol,
    shares,
    avg_cost,
    market_value,
    unrealized_pnl  -- 这个是 0！
FROM llm.ibkr_positions_realtime
```

**应该使用的查询** (Phase 1 报告中的):
```sql
-- 应该用这个（正确）
WITH latest_account AS (
    SELECT unrealized_pnl as total_unrealized_pnl,
           gross_position_value as total_position_value
    FROM llm.ibkr_account_realtime
    ORDER BY ts_ny DESC LIMIT 1
)
SELECT
    p.symbol,
    p.shares,
    p.avg_cost,
    -- 按比例分配账户总 P&L
    (a.total_unrealized_pnl / a.total_position_value * p.market_value) as unrealized_pnl,
    (a.total_unrealized_pnl / a.total_position_value * 100) as pnl_pct
FROM llm.ibkr_positions_realtime p
CROSS JOIN latest_account a
WHERE p.position > 0
```

### 建议修复

**立即修改 Dashboard 1 的 Positions Table 查询**:
使用 Phase 1 报告中验证过的"诚实估算"查询

---

## 🔴 **致命问题 3: 今天根本没有成交！**

### 问题描述
```sql
SELECT count(*) FROM llm.llm_fills WHERE trading_day = today();
结果: 0

最近的成交:
2025-10-31 15:24 (昨天盘中)
```

### 交易员视角
**问题**: "Dashboard 显示 7-day fills,但今天一笔成交都没有！系统在运行吗？"

**这是正常还是异常？**
- ✅ 可能正常: 今天市场没有触发交易信号
- ❌ 可能异常: 系统故障,无法下单
- ❌ **Dashboard 无法区分！**

**Dashboard 缺陷**:
- ❌ 没有"今日成交数"指标
- ❌ 没有"最后成交时间"标签
- ❌ 没有"X 天无成交"告警

### 建议修复

**添加 Panel**: "⚠️ 交易活跃度"
```sql
SELECT
    count(*) as fills_today,
    (SELECT formatDateTime(max(ts_ny), '%Y-%m-%d %H:%M') FROM llm.llm_fills) as last_fill,
    round((now() - (SELECT max(ts_ny) FROM llm.llm_fills)) / 3600, 1) as hours_since_last_fill
FROM llm.llm_fills
WHERE trading_day = today()
```

**告警阈值**:
- 绿色: 今天有成交
- 黄色: 今天无成交,但最后成交 < 48 小时
- 红色: 最后成交 > 48 小时 (可能系统故障)

---

## ⚠️ **严重问题 4: 无法知道 AI 是否在工作**

### 问题描述
```
AI Brain Calls (最近 24 小时):
- 16:00-17:00: 6 calls
- 02:00-03:00: 2 calls
- 01:00-02:00: 10 calls
...

问题: 现在是 17:00,过去 1 小时只有 6 calls,正常吗？
```

### 系统管理员视角
**问题**: "AI 是不是挂了？怎么知道调用频率是否正常？"

**Dashboard 缺陷**:
- ❌ 没有"预期调用频率"基线
- ❌ 没有"调用频率异常"告警
- ❌ 无法区分:
  - 正常低频 (晚上市场关闭)
  - 异常低频 (系统故障)

### 建议修复

**添加 Panel**: "🔄 AI 活跃度监控"
```sql
WITH recent_activity AS (
    SELECT
        toStartOfHour(ts_ny) as hour,
        count(*) as calls_per_hour
    FROM llm.ai_brain_calls
    WHERE ts_ny >= now() - INTERVAL 1 HOUR
    GROUP BY hour
),
historical_avg AS (
    SELECT
        avg(hourly_calls) as avg_calls_per_hour
    FROM (
        SELECT toStartOfHour(ts_ny) as hour, count(*) as hourly_calls
        FROM llm.ai_brain_calls
        WHERE ts_ny >= now() - INTERVAL 7 DAY
        GROUP BY hour
    )
)
SELECT
    r.calls_per_hour as last_hour_calls,
    h.avg_calls_per_hour as typical_calls,
    round((r.calls_per_hour / h.avg_calls_per_hour - 1) * 100, 0) as pct_change
FROM recent_activity r
CROSS JOIN historical_avg h
```

**告警阈值**:
- 绿色: 90-110% 正常范围
- 黄色: 50-90% 或 110-150% (可能异常)
- 红色: < 50% 或 > 150% (严重异常)

---

## ⚠️ **严重问题 5: 无错误追踪 - 看不到失败**

### 问题描述
```
ai_brain_calls 表字段:
- ts_ny ✅
- kind ✅
- provider ✅
- duration_ms ✅
- output_json ✅

缺失:
- ❌ success/failure 状态
- ❌ error_message
- ❌ retry_count
```

### 系统管理员视角
**问题**: "AI 调用有没有失败？错误率多少？我看不到！"

**影响**:
- ❌ 无法监控 Provider 可用性
- ❌ 无法知道是否有错误
- ❌ 出问题了也不知道

**例子**:
```
假设:
- 100 次调用,10 次失败
- 但 ai_brain_calls 只记录成功的
- Dashboard 显示: "100 次调用,平均延迟 25s"
- 实际情况: "110 次尝试,10 次失败 (9% 错误率),成功的平均 25s"
```

### 建议修复

**方案 1**: 修改 brain_calls.jsonl 记录格式
```json
{
  "ts": "2025-11-01T01:43:27-04:00",
  "kind": "propose_plan",
  "provider": "deepseek_nvidia",
  "success": true,          // 新增
  "error": null,            // 新增
  "duration_ms": 25566,
  "output": {...}
}
```

**方案 2**: 创建单独的错误表
```sql
CREATE TABLE llm.ai_brain_errors (
    ts_ny DateTime('America/New_York'),
    kind String,
    provider String,
    error_type String,
    error_message String,
    retry_attempt Int32
) ENGINE = MergeTree()
ORDER BY (ts_ny, provider);
```

**Dashboard 添加**:
```sql
-- 错误率 Panel
SELECT
    provider,
    count(*) as total_attempts,
    countIf(success = false) as failures,
    round(failures / total_attempts * 100, 2) as error_rate_pct
FROM llm.ai_brain_calls
WHERE ts_ny >= now() - INTERVAL 24 HOUR
GROUP BY provider
```

---

## ⚠️ **严重问题 6: 203 个 RUNNING Sessions - 正常吗？**

### 问题描述
```sql
SELECT status, count(*) FROM llm.llm_sessions GROUP BY status;

结果:
RUNNING: 203
CLOSED:  371

最早的 RUNNING session:
start_ts_ny: 2025-10-25 01:00  (7 天前！)
```

### 系统管理员视角
**问题**: "203 个 session 在运行？7 天前的还在 RUNNING？这些是僵尸进程吗？"

**无法回答的问题**:
1. 这 203 个 session 真的在运行吗？
2. 有多少是僵尸 session (已死但状态未更新)？
3. 每个 session 最后活跃时间是什么时候？
4. 有没有 session 卡住了？

**Dashboard 缺陷**:
- ❌ 没有"最后活跃时间"
- ❌ 没有"session 健康度"指标
- ❌ 无法识别僵尸 session

### 建议修复

**添加查询**: "Session 健康度"
```sql
SELECT
    session_id,
    bot_id,
    status,
    formatDateTime(start_ts_ny, '%Y-%m-%d %H:%M') as started,
    round((now() - start_ts_ny) / 3600, 1) as running_hours,
    (SELECT max(ts_ny) FROM llm.llm_decisions WHERE session_id = s.session_id) as last_decision,
    round((now() - last_decision) / 3600, 1) as hours_idle,
    CASE
        WHEN hours_idle > 24 THEN '🔴 Zombie'
        WHEN hours_idle > 6 THEN '⚠️ Idle'
        ELSE '✅ Active'
    END as health_status
FROM llm.llm_sessions s
WHERE status = 'RUNNING'
ORDER BY hours_idle DESC
LIMIT 20
```

---

## ⚠️ **问题 7: 97.6% 决策是 HOLD - 系统在浪费资源**

### 问题描述
```
llm_decisions (464 total):
- HOLD: 453 (97.6%)
- BUY:   10 (2.2%)
- SELL:   1 (0.2%)
```

### 交易员 + 管理员视角
**问题**: "AI 做了 464 次决策,453 次是 HOLD？为什么要调用 AI？"

**资源浪费**:
- 每次 propose_plan 调用: 25.6 秒
- 453 次 HOLD 浪费: 453 × 25.6 = 11,597 秒 (3.2 小时)
- 成本: 453 × $0.01 (假设) = $4.53

**根本问题**:
1. 风控太严格？
2. 市场条件不符合策略？
3. 预筛选逻辑缺失？

**Dashboard 缺陷**:
- ❌ 没有显示 "HOLD 比例"
- ❌ 没有 "为什么 HOLD" 的原因分析
- ❌ 没有成本效益分析

### 建议修复

**添加 Panel**: "决策效率分析"
```sql
WITH decision_stats AS (
    SELECT
        JSONExtractString(decision_json, 'action') as action,
        count(*) as count,
        avg(duration_ms) as avg_duration,
        count() * avg_duration as total_time_ms
    FROM llm.ai_brain_calls
    JOIN llm.llm_decisions ON ai_brain_calls.session = llm_decisions.session_id
    GROUP BY action
)
SELECT
    action,
    count,
    round(count / sum(count) OVER () * 100, 1) as pct,
    round(total_time_ms / 1000, 0) as total_seconds,
    CASE
        WHEN action = 'HOLD' AND pct > 90 THEN '🔴 Too Conservative'
        WHEN action = 'HOLD' AND pct > 70 THEN '⚠️ Conservative'
        ELSE '✅ Active'
    END as efficiency_status
FROM decision_stats
ORDER BY count DESC
```

---

## 📊 **缺失信息总结**

### Dashboard 1 (Trading Performance) 缺失

| 缺失信息 | 影响 | 优先级 |
|---------|------|--------|
| 数据新鲜度指示器 | 🔴 用户不知道数据已过期 13 小时 | P0 紧急 |
| 持仓盈亏准确值 | 🔴 所有持仓显示 $0 P&L | P0 紧急 |
| 今日交易活跃度 | ⚠️ 不知道今天是否有成交 | P1 高 |
| 最后成交时间 | ⚠️ 无法判断系统是否在交易 | P1 高 |
| Intraday P&L 曲线 | ⚠️ 只有总 P&L,看不到日内波动 | P2 中 |
| Commission 总计 | ⚠️ 不知道交易成本 | P2 中 |

### Dashboard 2 (AI Brain Command Center) 缺失

| 缺失信息 | 影响 | 优先级 |
|---------|------|--------|
| 最后调用时间 | 🔴 不知道 AI 是否还在工作 | P0 紧急 |
| 调用频率是否正常 | ⚠️ 无法判断 6 calls/hour 是否异常 | P1 高 |
| Plans 执行结果 | ⚠️ 只看到 plan,不知道执行结果 | P1 高 |
| Session 对应的 symbols | ⚠️ 不知道每个 session 交易什么 | P2 中 |

### Dashboard 3 (AI Brain Performance) 缺失

| 缺失信息 | 影响 | 优先级 |
|---------|------|--------|
| 错误率 / 失败追踪 | 🔴 看不到调用失败 | P0 紧急 |
| Provider 可用性监控 | 🔴 100% deepseek,挂了怎么办 | P0 紧急 |
| 成本追踪 (Token usage) | ⚠️ 不知道花了多少钱 | P1 高 |
| 调用频率基线/告警 | ⚠️ 无法判断是否异常 | P1 高 |
| Prompt 内容采样 | ⚠️ 不知道发送了什么 prompt | P2 中 |

### Dashboard 4 (LLM Sessions Monitor) 缺失

| 缺失信息 | 影响 | 优先级 |
|---------|------|--------|
| Session 健康度 | 🔴 203 个 RUNNING,哪些是僵尸？ | P0 紧急 |
| 最后活跃时间 | 🔴 7 天前的 session 还在 RUNNING | P0 紧急 |
| Session P&L | ⚠️ 不知道每个 session 赚了多少 | P1 高 |
| 决策效率 (HOLD 比例) | ⚠️ 97.6% HOLD,资源浪费 | P1 高 |
| Session 生命周期 | ⚠️ 不知道正常 session 运行多久 | P2 中 |

---

## 🎯 **最致命的 3 个问题**

### 1. 数据已过期 - 用户被误导 🔴

**问题**: 所有 Dashboard 显示 13 小时前的数据,但用户不知道

**修复**:
```sql
-- 必须在每个 Dashboard 添加
SELECT
    formatDateTime(max(ts_ny), '%Y-%m-%d %H:%M:%S ET') as last_update,
    round((now() - max(ts_ny)) / 60, 0) as minutes_ago
FROM llm.ibkr_account_realtime  -- 或相应表
```

**颜色编码**:
- < 10 分钟: 绿色 ✅
- 10-60 分钟: 黄色 ⚠️
- > 60 分钟: 红色 🔴 + "数据已过期"

---

### 2. 持仓盈亏显示错误 - 数据矛盾 🔴

**问题**: Dashboard 显示每个持仓 P&L = $0,但总 P&L = $2,165

**修复**: 使用 Phase 1 报告中的"诚实估算"查询
```sql
WITH latest_account AS (
    SELECT unrealized_pnl, gross_position_value
    FROM llm.ibkr_account_realtime
    ORDER BY ts_ny DESC LIMIT 1
)
SELECT
    symbol,
    shares,
    (unrealized_pnl / gross_position_value * market_value) as estimated_pnl
FROM llm.ibkr_positions_realtime
CROSS JOIN latest_account
WHERE position > 0
```

---

### 3. 无错误追踪 - 系统健康度未知 🔴

**问题**: 看不到 AI 调用失败、Provider 错误、Session 僵尸

**修复**:
1. 记录错误到 brain_calls.jsonl
2. 创建 ai_brain_errors 表
3. 添加错误率 Panel
4. 添加 Session 健康度检查

---

## 📋 **优先级修复清单**

### P0 - 紧急 (本周必须修复)

- [ ] **Dashboard 1**: 添加"数据新鲜度"Panel (红/黄/绿)
- [ ] **Dashboard 1**: 修复持仓 P&L 查询 (使用诚实估算)
- [ ] **Dashboard 3**: 添加错误追踪字段到 brain_calls
- [ ] **Dashboard 4**: 添加 Session 健康度检查 (识别僵尸)
- [ ] **所有 Dashboard**: 添加"最后更新时间"标签

### P1 - 高优先级 (下周)

- [ ] **Dashboard 1**: 添加"交易活跃度"监控
- [ ] **Dashboard 2**: 添加"AI 活跃度"基线告警
- [ ] **Dashboard 3**: 添加"Provider 可用性"监控
- [ ] **Dashboard 4**: 添加"决策效率"分析 (HOLD 比例)

### P2 - 中优先级 (两周内)

- [ ] **Dashboard 1**: Intraday P&L 曲线
- [ ] **Dashboard 2**: Plans 执行结果追踪
- [ ] **Dashboard 3**: 成本追踪 (Token usage)
- [ ] **Dashboard 4**: Session P&L 归因

---

## 🎓 **经验教训**

### 1. "Be Honest" 不够 - 还需要 "Be Obvious"

**问题**: 我们诚实地使用了真实数据,但没有明确告诉用户数据的限制

**例子**:
- ✅ 诚实: 使用账户总 P&L 按比例估算
- ❌ 不明显: 没在 Dashboard 上标注"估算值"
- 结果: 用户看到 $0 P&L,以为数据错误

**教训**: 除了 Be Honest,还要 **明确标注数据来源和限制**

### 2. 数据验证 ≠ 用户体验验证

**问题**: 我们验证了查询能跑通,但没有从用户视角验证

**例子**:
- ✅ 查询验证: "SELECT * FROM table" 返回结果
- ❌ 用户验证: 结果是 13 小时前的,用户不知道
- 结果: 技术上正确,但用户被误导

**教训**: 必须从真实用户视角验证,不能只验证技术

### 3. 关键信息缺失比错误信息更危险

**问题**: 我们专注于"不伪造数据",但忽略了"缺失的关键信息"

**例子**:
- ✅ 不伪造: 不创建假的 token usage
- ❌ 缺失: 没有错误追踪、新鲜度指示、健康度检查
- 结果: Dashboard "能用",但无法判断系统是否正常

**教训**: **缺失的告警比错误的数据更危险**

---

**Report Complete**: 2025-11-01 17:00 EST
**Severity**: 🔴 **3 个致命问题 + 4 个严重问题**
**Action Required**: 紧急修复 P0 问题

