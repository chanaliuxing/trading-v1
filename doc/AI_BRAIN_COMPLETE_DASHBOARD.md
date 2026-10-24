# AI Brain - 完整视图 Dashboard

**Dashboard URL**: http://localhost:3000/d/ai-brain-complete/
**创建时间**: 2025-11-02 02:00 EST
**包含**: A (推理) + B (配置) + C (状态) 三个维度

---

## 🎯 这个 Dashboard 显示什么

根据你的要求 "都要",这个 dashboard 包含 **所有三个维度**:

### A. AI 的推理过程 (为什么这么做)
- AI 的思考过程 (REASONING)
- 为什么做出这个判断

### B. AI 配置的参数 (做了什么)
- 诊断结果 (diagnosis)
- 建议方案 (prescription)
- 具体参数详情

### C. 系统运行状态 (效果怎么样)
- 当前配置 (最后一次 decide)
- 决策理由
- 今日交易结果 (待接入)

---

## 📊 Dashboard 结构

### Row 1: 🚦 系统健康检查

**快速判断 AI Brain 是否正常**

| Panel | 显示内容 | 正常值 |
|-------|---------|--------|
| AI Brain 状态 | 🟢 运行中 / 🔴 停止 | 🟢 绿色 |
| 最后活动 | 2025-11-01 14:49 | < 1 小时前 |
| 今日调用 | 6 次 | > 0 |
| 调用类型 | propose_plan: 3, decide: 3 | 有 propose 和 decide |

**用途**: 第一眼判断系统是否正常

---

### Row 2: 🧠 A. AI 的推理过程

#### Panel: 💡 AI 推理说明

**显示内容**: AI 的详细推理过程 (REASONING 文本)

**当前状态**:
- ⚠️ REASONING 目前存储在文件系统,未写入 ClickHouse
- 📁 文件位置: `./runs/ai_trader/YYYY-MM-DD/brain_raw_response_*.txt`

**示例内容**:
```
REASONING:
好的,让我理解一下任务。我需要作为交易系统的控制大脑,
根据最新的健康快照和约束条件生成 JSON 计划。

输入显示所有指标如 overall_pnl, consistency, market_alignment 等
都是 null 或 0。这说明没有历史交易数据...

对于建议,启用安全模式来限制风险是合理的。分配 deepseek 和
gemini,使用慢间隔 (600s) 确保低频交易...
```

**如何查看**:
```bash
# 查看最新推理
cat ./runs/ai_trader/$(date +%Y-%m-%d)/brain_raw_response_*.txt | tail -100

# 查看所有推理文件
ls -lt ./runs/ai_trader/$(date +%Y-%m-%d)/brain_raw_*
```

**未来改进**: 将 REASONING 写入 ClickHouse,在 dashboard 实时显示

---

### Row 3: ⚙️ B. AI 配置的参数

#### Panel: 📊 最新诊断 (Diagnosis)

**显示内容**: AI 对系统当前状态的诊断

**示例**:
```
⏰ 时间: 2025-11-01 14:49

🔍 诊断: no_trading_history_due_to_new_system_init

📝 含义:
  → 系统刚初始化,无交易历史
```

**字段说明**:
- `no_trading_history_due_to_new_system_init`: 系统刚启动
- `no_trading_history_due_to_zero_decisions`: 系统运行但未产生决策
- `''` (空): 正常运行

---

#### Panel: 💊 AI 建议 (Prescription)

**显示内容**: AI 提出的改进建议列表

**示例**:

| action | confidence |
|--------|-----------|
| enable_baseline_data_collection | 🟢 0.90 |
| activate_safe_mode_with_limits | 🟢 0.85 |
| verify_system_health_before_live | 🟡 0.80 |

**字段说明**:
- `action`: 建议的行动 (控制指令)
- `confidence`: AI 的置信度 (0-1)
  - 🟢 > 0.8: 高置信度
  - 🟡 0.5-0.8: 中等置信度
  - 🔴 < 0.5: 低置信度

---

#### Panel: 🔧 建议参数详情

**显示内容**: 每个建议的具体参数

**示例**:
```
📋 建议 #1

🎯 行动: enable_baseline_data_collection

⚙️ 参数: {"providers":["deepseek","gemini"],"symbols":["AAPL","TSLA"],
"interval_sec":600,"ab_split":{"champion":0.7,"challenger":0.3}}

🎲 置信度: 90%
```

**你能看到**:
- AI 建议使用哪些 LLM providers
- 交易哪些 symbols
- 交易间隔 (秒)
- Champion/Challenger 分配比例

---

### Row 4: 📊 C. 系统运行状态

#### Panel: 🎛️ 当前配置

**显示内容**: 最后一次 decide 调用配置的参数

**示例**:

| time | parameter | value |
|------|-----------|-------|
| 2025-11-01 14:49 | risk_management.guardian.dd_intraday_pct | -5 |
| 2025-11-01 14:49 | risk_management.hedge.auto_execute | true |

**字段说明**:
- `risk_management.guardian.dd_intraday_pct`: 日内回撤限制 (%)
  - 值 `-5` 表示亏损达到 -5% 时停止交易
- `risk_management.hedge.auto_execute`: 自动对冲
  - `true`: 启用
  - `false`: 禁用

---

#### Panel: 💭 决策理由

**显示内容**: AI 为什么这样配置

**示例**:
```
⏰ 2025-11-01 14:49

💭 AI 说:
"Conservative initial settings: no trading history, enable auto-hedging
and strict 5% intraday drawdown limit"

(翻译: 保守的初始设置:没有交易历史,启用自动对冲和严格的 5%
日内回撤限制)
```

---

#### Panel: 📈 今日交易结果

**显示内容**: 系统实际交易效果 (待接入)

**应该显示**:
- 📊 今日决策数
- 💰 今日盈亏
- 📈 胜率
- 🎯 当前持仓

**数据源** (需要接入):
```sql
-- 今日决策
SELECT count() FROM llm.llm_decisions WHERE toDate(ts_ny) = today()

-- 今日盈亏
SELECT sum(realized_pnl) FROM trading.fills WHERE toDate(fill_time) = today()

-- 当前持仓
SELECT symbol, quantity, avg_price FROM trading.positions WHERE quantity != 0
```

---

## 🎯 使用场景

### 场景 1: 全面了解 AI Brain

**步骤**:
1. 打开 Dashboard: http://localhost:3000/d/ai-brain-complete/
2. Row 1: 快速检查系统是否正常
3. Row 2: 阅读 AI 推理文件,了解 AI 为什么这么想
4. Row 3: 查看 AI 的诊断和建议
5. Row 4: 确认实际配置的参数

**预计耗时**: 5-10 分钟

---

### 场景 2: 理解 AI 的决策逻辑

**触发**: 你想知道 AI 为什么做出某个决定

**步骤**:
1. Row 2 查看 AI 推理文件
   - 看 AI 分析了哪些指标
   - 看 AI 的思考过程
2. Row 3 查看诊断
   - 看 AI 判断的系统状态
   - 看 AI 建议的行动
3. Row 4 查看决策理由
   - 看 AI 实际配置了什么
   - 看 AI 的简短理由

---

### 场景 3: 验证 AI 配置是否生效

**触发**: AI 提出了建议,你想确认是否执行了

**步骤**:
1. Row 3 查看 "AI 建议" (Prescription)
   - 例如: `activate_safe_mode_with_limits`
2. Row 4 查看 "当前配置"
   - 确认相关参数是否被修改
   - 例如: `risk_management.guardian.dd_intraday_pct = -5`

**判断**:
- ✅ 如果参数已修改 → 建议已执行
- ⚠️ 如果参数未变 → 建议未被采纳

---

## 📋 三个维度对比

| 维度 | 数据来源 | 显示内容 | 用途 |
|------|---------|---------|------|
| **A. 推理** | 文件系统 (brain_raw_response_*.txt) | REASONING 文本 | 了解 AI 为什么这么想 |
| **B. 配置** | ClickHouse (output_json) | diagnosis + prescription | 了解 AI 建议做什么 |
| **C. 状态** | ClickHouse (decide output_json) | 实际配置 + 理由 | 了解系统当前状态 |

---

## ⚠️ 当前限制

### 1. REASONING 未进数据库

**问题**: A (推理) 部分只能从文件读取,不能在 dashboard 实时显示

**影响**: 需要手动 SSH 到服务器查看推理文件

**解决方案**:
1. 修改 AI Brain 代码,将 REASONING 写入 ClickHouse
2. 新增字段: `reasoning_text String`
3. 更新 dashboard 查询直接显示

---

### 2. 交易结果未接入

**问题**: C (状态) 部分的交易结果是 placeholder

**影响**: 无法看到 AI 配置的实际效果

**解决方案**:
1. 查询 `llm.llm_decisions` 表获取决策数
2. 查询 `trading.fills` 表获取盈亏
3. 查询 `trading.positions` 表获取持仓

---

## 🔧 快速参考

### 查看 AI 推理文件

```bash
# 今天的所有推理
ls -lt ./runs/ai_trader/$(date +%Y-%m-%d)/brain_raw_*

# 最新的推理
cat ./runs/ai_trader/$(date +%Y-%m-%d)/brain_raw_response_*.txt | tail -100

# 特定时间的推理
cat ./runs/ai_trader/2025-11-01/brain_raw_response_reflection_144900.txt
```

---

### 查询当前配置

```sql
-- 最后一次配置
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%i') as time,
    JSONExtractString(output_json, 'notes') as reason,
    JSONExtractArrayRaw(output_json, 'apply') as config_changes
FROM llm.ai_brain_calls
WHERE kind = 'decide'
ORDER BY ts_ny DESC
LIMIT 1
```

---

### 查询最新诊断

```sql
-- 最新诊断
SELECT
    formatDateTime(ts_ny, '%Y-%m-%d %H:%i') as time,
    JSONExtractString(output_json, 'diagnosis') as diagnosis,
    JSONExtractArrayRaw(output_json, 'prescription') as prescriptions
FROM llm.ai_brain_calls
WHERE kind = 'propose_plan'
ORDER BY ts_ny DESC
LIMIT 1
```

---

## ✅ 总结

### 这个 Dashboard 回答了:

1. ✅ **AI 为什么这么做?** → Row 2 (推理)
2. ✅ **AI 建议做什么?** → Row 3 (配置)
3. ✅ **系统实际配置了什么?** → Row 4 (状态)
4. ✅ **效果怎么样?** → Row 4 (交易结果,待接入)

### 与其他 Dashboard 的区别

| Dashboard | 定位 | 目标用户 |
|-----------|------|---------|
| **ai-brain-complete** (新) | 完整视图 (A+B+C) | 想全面了解 AI 的人 |
| ai-brain-mgmt | 管理视图 (简化) | 日常监控 |
| ai-brain-detailed | 技术视图 (JSON) | 开发调试 |

---

**Dashboard URL**: http://localhost:3000/d/ai-brain-complete/
**建议**: 第一次使用先全面浏览,了解所有信息后,日常用 ai-brain-mgmt 即可

---

**创建时间**: 2025-11-02 02:00 EST
**版本**: v1.0 (Complete View)
**包含**: A (推理) + B (配置) + C (状态)
