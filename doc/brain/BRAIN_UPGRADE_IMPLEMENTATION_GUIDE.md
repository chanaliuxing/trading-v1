# Brain ML 系统升级实施指南

## 📋 概述

本次升级的核心目标：**将 AI 大脑的"成功"定义从"脚本不报错"改为"模型指标真的变好了"**

基于文档 `brainUpgrade.md` 的分析，我们完成了以下关键改造：

---

## ✅ 已完成的修改

### 1️⃣ alpha_loop.py - 丰富 Checkpoint Payload

**修改位置**:
- `alpha_loop.py:683-719` (_run_optuna 函数)
- `alpha_loop.py:892-929` (_run_optuna_with_space 函数)

**修改内容**: 将 checkpoint payload 从简单的 `{score, params}` 扩展为包含完整 OOS 指标的结构：

```python
payload = {
    "score": float(score),
    "params": params,
    # 元数据
    "symbol": symbol,
    "model_family": model_family,
    "horizon": horizon,
    "window_days": window_days,
    # OOS 性能指标 (使用实际字段名)
    "oos_avg_sharpe": float(...),
    "oos_avg_profit_factor": float(...),
    "oos_avg_max_drawdown": float(...),
    "oos_avg_win_rate": float(...),
    "oos_avg_total_return": float(...),
    "oos_trades": int(...),
    # 可选高级指标
    "oos_avg_sortino": float(...),
    "oos_avg_omega": float(...),
    "oos_avg_calmar": float(...),
}
```

**影响**: 每次 trial 完成后，ClickHouse wf_state 表的 payload_json 字段会包含完整的评估指标。

---

### 2️⃣ ClickHouse Schema - 分析视图和表

**新建文件**: `schema/clickhouse/ml_eval_analysis.sql`

**包含组件**:

1. **ml_eval_flat 表** - 展开 payload_json 的明细表
   - 存储所有 trial 的结构化指标
   - 按 symbol/model_family/horizon/window_days/updated_at 排序

2. **mv_wf_state_to_ml_eval_flat 物化视图** - 自动同步
   - 从 wf_state 自动提取 payload_json 并写入 ml_eval_flat
   - 触发条件: `step='trial_end' AND status='done'`

3. **ml_eval_with_delta 视图** - 核心分析视图
   - 计算 delta 指标 (当前 - 上一次):
     - `delta_sharpe`
     - `delta_pf`
     - `delta_max_dd`
     - `delta_win_rate`
   - 计算成功标签:
     - `is_success`: 温和版 (ΔS ≥ 0.02, S ≥ 1.2, PF ≥ 1.2, DD 控制)
     - `is_big_success`: 明显成功 (ΔS ≥ 0.10, S ≥ 1.5, PF ≥ 1.3)

4. **ml_model_stats_30d 视图** - 聚合统计
   - 预计算每个模型配置最近 30 天的成功率
   - 提供快速查询接口

**成功定义 (温和版)**:
```sql
is_success = (
    oos_avg_sharpe >= 1.2                  -- 绝对质量门槛
    AND oos_avg_profit_factor >= 1.2       -- profit factor 门槛
    AND delta_sharpe >= 0.02               -- 小幅改进即可
    AND delta_max_dd <= 0.03               -- 回撤不能恶化超过 3%
)
```

---

### 3️⃣ memory.py - 新增 ClickHouse 读取函数

**新增函数** (memory.py:275-521):

1. **get_model_success_rate_from_ch()** - 获取成功率
   - 查询 ml_eval_with_delta
   - 基于 is_success 计算，不是 status=ok
   - 返回 [0.0, 1.0]，无数据返回 0.5

2. **get_model_fail_streak_from_ch()** - 获取连续失败次数
   - 统计最近连续 is_success=0 的次数
   - 用于触发动作冻结

3. **get_model_stats_summary_from_ch()** - 综合统计
   - 一次性返回：
     - attempts_30d, successes_30d, success_rate_30d
     - big_successes_30d
     - latest_sharpe, latest_pf
     - fail_streak

**使用示例**:
```python
from scripts.ai_trader.brain.policy.memory import get_model_stats_summary_from_ch

stats = get_model_stats_summary_from_ch(
    symbol="NVDA",
    model_family="lgbm_rank",
    horizon="5m",
    window_days=365
)

print(f"Success rate: {stats['success_rate_30d']:.2%}")
print(f"Fail streak: {stats['fail_streak']}")
print(f"Latest Sharpe: {stats['latest_sharpe']:.2f}")
```

---

## 🚀 实施步骤

### Step 1: 执行 ClickHouse Schema 创建

**前提**: ClickHouse 服务正在运行

```bash
# 方法 1: 使用 clickhouse-client
clickhouse-client < schema/clickhouse/ml_eval_analysis.sql

# 方法 2: 手动执行
clickhouse-client
> SOURCE schema/clickhouse/ml_eval_analysis.sql;

# 方法 3: 如果使用 Docker
cd infra
docker-compose exec clickhouse clickhouse-client < ../schema/clickhouse/ml_eval_analysis.sql
```

**验证**:
```sql
-- 检查表是否创建成功
SHOW TABLES FROM trading LIKE '%ml_eval%';

-- 应该看到:
-- ml_eval_flat
-- mv_wf_state_to_ml_eval_flat
```

---

### Step 2: 运行一次 Alpha Loop 测试

**目的**: 生成包含新 payload 的 checkpoint 数据

```bash
# 运行一个简单的测试配置
python alpha_loop.py --config config/hpo_test_minimal.json
```

**验证新 payload**:
```sql
-- 检查最新的 wf_state 记录
SELECT
    run_id,
    trial_id,
    step,
    JSONExtractString(payload_json, 'symbol') AS symbol,
    JSONExtractString(payload_json, 'oos_avg_sharpe') AS sharpe,
    JSONExtractString(payload_json, 'oos_avg_profit_factor') AS pf
FROM trading.wf_state
WHERE step = 'trial_end'
  AND status = 'done'
ORDER BY updated_at DESC
LIMIT 5;
```

**预期结果**: 应该看到 symbol, sharpe, pf 等字段都有值（不是空字符串）

---

### Step 3: 验证物化视图同步

**检查 ml_eval_flat 表**:
```sql
SELECT *
FROM trading.ml_eval_flat
ORDER BY updated_at DESC
LIMIT 10;
```

**预期结果**: 应该看到最近的 trial 数据，所有字段都已展开

---

### Step 4: 验证 Delta 计算

**检查 ml_eval_with_delta 视图**:
```sql
SELECT
    symbol,
    trial_id,
    updated_at,
    oos_avg_sharpe,
    delta_sharpe,
    is_success,
    is_big_success
FROM trading.ml_eval_with_delta
ORDER BY updated_at DESC
LIMIT 20;
```

**预期结果**:
- `delta_sharpe` 应该是当前 - 上一次的差值（第一条记录可能是 NULL）
- `is_success` 应该是 0 或 1

---

### Step 5: 测试 Memory 函数

**Python 测试脚本**:
```python
# test_memory_ch.py
from scripts.ai_trader.brain.policy.memory import (
    get_model_success_rate_from_ch,
    get_model_fail_streak_from_ch,
    get_model_stats_summary_from_ch
)

# 测试 1: 获取成功率
sr = get_model_success_rate_from_ch(symbol="TSLA")
print(f"TSLA Success Rate: {sr:.2%}")

# 测试 2: 获取失败连击
fs = get_model_fail_streak_from_ch(symbol="TSLA")
print(f"TSLA Fail Streak: {fs}")

# 测试 3: 完整统计
stats = get_model_stats_summary_from_ch(symbol="TSLA")
print("\nTSLA Stats (last 30 days):")
for k, v in stats.items():
    print(f"  {k}: {v}")
```

```bash
python test_memory_ch.py
```

**预期结果**: 应该返回有意义的数值（不是全部 0.5 或 0）

---

## 🔧 后续集成步骤 (需要你手动做)

### 集成到 control_event_consumer.py

在 `control_event_consumer.py` 中的 `_execute_ml_actions` 函数里，替换原有的信心校准逻辑：

**原逻辑** (基于 status=ok):
```python
# 旧代码 (在 summarize_actions 里)
ok = (st == "ok")  # ❌ 基于脚本是否报错
success_rate = successes / attempts
```

**新逻辑** (基于真实指标):
```python
from scripts.ai_trader.brain.policy.memory import get_model_stats_summary_from_ch

def _calibrate_action_confidence(action, symbol, model_family, horizon, window_days, cfg):
    """基于真实指标历史来校准 LLM 的 confidence"""
    raw_conf = action.get("confidence", 0.7)

    # 从 ClickHouse 获取真实成功率
    stats = get_model_stats_summary_from_ch(
        symbol=symbol,
        model_family=model_family,
        horizon=horizon,
        window_days=window_days
    )

    success_rate = stats["success_rate_30d"]
    fail_streak = stats["fail_streak"]

    # 信心校准
    base = cfg.get("policies", {}).get("ml", {}).get("confidence_calibration", {}).get("base", 0.6)
    hist_w = cfg.get("policies", {}).get("ml", {}).get("confidence_calibration", {}).get("history_weight", 0.4)

    effective_conf = raw_conf * (base + hist_w * success_rate)

    # 失败连击检查
    skip_streak = cfg.get("policies", {}).get("ml", {}).get("confidence_calibration", {}).get("skip_fail_streak", 5)
    if fail_streak >= skip_streak:
        return 0.0, "frozen_due_to_fail_streak"

    return effective_conf, "ok"
```

---

### 添加 Experimental Mode 配置

在你的 config 文件中 (例如 `config/ml_policies.json` 或类似):

```json
{
  "policies": {
    "ml": {
      "experimental_mode": true,
      "confidence_calibration": {
        "base": 0.50,
        "history_weight": 0.50,
        "skip_fail_streak": 10
      },
      "memory_source": {
        "use_clickhouse": true,
        "table": "ml_eval_with_delta"
      }
    }
  }
}
```

**Experimental Mode 特点**:
- `skip_fail_streak: 10` - 更宽容，允许更多失败后再冻结
- `base: 0.50` - 降低基础门槛
- `history_weight: 0.50` - 成功率影响更大

**Production Mode** (以后):
- `skip_fail_streak: 3`
- `base: 0.60`
- `history_weight: 0.40`

---

## 📊 监控与调试

### 1. 查看成功率分布

```sql
SELECT
    symbol,
    count(*) AS total_trials,
    sum(is_success::UInt8) AS successes,
    round(100.0 * successes / total_trials, 2) AS success_rate_pct
FROM trading.ml_eval_with_delta
WHERE updated_at >= now() - INTERVAL 30 DAY
GROUP BY symbol
ORDER BY success_rate_pct DESC;
```

### 2. 查看 Delta Sharpe 分布

```sql
SELECT
    symbol,
    quantile(0.25)(delta_sharpe) AS p25,
    quantile(0.50)(delta_sharpe) AS p50,
    quantile(0.75)(delta_sharpe) AS p75,
    max(delta_sharpe) AS max_delta
FROM trading.ml_eval_with_delta
WHERE updated_at >= now() - INTERVAL 30 DAY
  AND delta_sharpe IS NOT NULL
GROUP BY symbol;
```

### 3. 查看失败连击情况

```python
from scripts.ai_trader.brain.policy.memory import get_model_fail_streak_from_ch

symbols = ["TSLA", "NVDA", "AAPL", "SPY"]
for sym in symbols:
    fs = get_model_fail_streak_from_ch(symbol=sym)
    print(f"{sym}: {fs} consecutive failures")
```

---

## 🎯 成功标准调优

如果运行一段时间后，发现成功样本太少或太多，可以调整阈值：

### 调整位置: `schema/clickhouse/ml_eval_analysis.sql` Line 109-118

**当前阈值 (温和版)**:
```sql
is_success = (
    oos_avg_sharpe >= 1.2
    AND oos_avg_profit_factor >= 1.2
    AND delta_sharpe >= 0.02
    AND delta_max_dd <= 0.03
)
```

**更严格版本** (如果成功率 > 80%):
```sql
is_success = (
    oos_avg_sharpe >= 1.4
    AND oos_avg_profit_factor >= 1.3
    AND delta_sharpe >= 0.05
    AND delta_max_dd <= 0.02
)
```

**更宽松版本** (如果成功率 < 20%):
```sql
is_success = (
    oos_avg_sharpe >= 1.0
    AND oos_avg_profit_factor >= 1.1
    AND delta_sharpe >= 0.01
    AND delta_max_dd <= 0.05
)
```

修改后需要重新创建视图:
```sql
DROP VIEW IF EXISTS trading.ml_eval_with_delta;
-- 然后重新执行创建视图的 SQL
```

---

## 📝 待完成事项 (你需要做的)

- [ ] 执行 ClickHouse Schema (Step 1)
- [ ] 运行测试 alpha_loop，验证 payload (Step 2-4)
- [ ] 测试 memory 函数 (Step 5)
- [ ] 集成到 control_event_consumer.py 的信心校准逻辑
- [ ] 添加 experimental_mode 配置
- [ ] 运行 2-3 周，收集数据
- [ ] 根据实际分布调整成功阈值
- [ ] (可选) 添加 IC 字段到 alpha_loop 的 oos_summary

---

## 🚨 注意事项

1. **ClickHouse 数据安全**
   - ✅ 所有新表都在 `trading` 数据库
   - ✅ 使用 MergeTree 引擎，自动去重
   - ✅ 有分区和 TTL（视图会自动过滤 30 天）

2. **向后兼容**
   - ✅ 旧的 `summarize_actions()` 仍然保留，不影响现有逻辑
   - ✅ 新函数独立，可以逐步切换

3. **性能**
   - ✅ 物化视图自动同步，无需手动触发
   - ✅ ml_model_stats_30d 视图提供预聚合，查询快速

4. **错误处理**
   - ✅ 所有 ClickHouse 查询都有 try/except
   - ✅ 查询失败返回中性值 (0.5)，不影响系统运行

---

## 📚 相关文档

- 原始分析: `doc/brain/brainUpgrade.md`
- ClickHouse Schema: `schema/clickhouse/ml_eval_analysis.sql`
- CLAUDE 规则: `CLAUDE.md` (Section VII - 工具与命令 - ClickHouse)

---

**最后更新**: 2025-11-08
**实施者**: AI Assistant + User
**状态**: 代码修改完成，等待 ClickHouse Schema 部署和测试
