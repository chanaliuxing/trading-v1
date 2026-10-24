# Brain ML 系统升级 - 终局版实施指南
## Sharpe + PF + MaxDD + IC 四维度完整闭环

## 🎯 核心目标

将 AI 大脑的"成功"定义从 **"脚本不报错"** 改为 **"Sharpe + PF + MaxDD + IC 四个指标真的变好了"**

---

## ✅ 已完成的修改（终局版）

### 1️⃣ alpha_loop.py - 三处关键修改

#### 修改 1: `_score_from_rows_or_summary()` 支持 IC (Line 550-576)

**功能**: 支持在 HPO objective 中使用 IC

```python
# 现在支持:
"objective": {"metric": "ic"}           # 直接优化 IC
"objective": {"formula": "0.7*sharpe + 0.3*ic"}  # 组合优化
```

**代码修改**:
- Line 561: 添加 `"ic": float(summary.get("oos_avg_ic", 0.0))`  # formula 支持
- Line 574: 添加 `"ic": "oos_avg_ic"`  # metric 映射

#### 修改 2&3: checkpoint payload 添加 IC (Line 698-720, 910-932)

**功能**: 每次 trial 完成后，保存完整的 IC 指标到 ClickHouse

**关键字段**:
```python
"oos_avg_ic": float(oos_summary.get("oos_avg_ic", 0.0))
```

---

### 2️⃣ ClickHouse Schema - 四维度分析系统

**文件**: `schema/clickhouse/ml_eval_analysis.sql`

#### 表 1: ml_eval_flat (Line 9-43)
- ✅ 添加 `oos_avg_ic Float64` 列 (Line 25)
- 存储所有 trial 的结构化指标

#### 物化视图: mv_wf_state_to_ml_eval_flat (Line 47-84)
- ✅ 解析 `oos_avg_ic` 字段 (Line 66)
- 自动同步 wf_state → ml_eval_flat

#### 视图: ml_eval_with_delta (Line 88-161)

**关键计算**:
```sql
-- Delta IC (Line 113)
oos_avg_ic - lagInFrame(oos_avg_ic) OVER w AS delta_ic,

-- 四维度成功定义 (Line 118-138)
is_success = (
    -- 绝对质量门槛
    oos_avg_sharpe >= 1.2           -- Sharpe
    AND oos_avg_profit_factor >= 1.2  -- PF
    AND oos_avg_ic >= 0.02            -- IC ⭐
    -- 改进门槛 (OR 逻辑)
    AND (
        delta_sharpe >= 0.02         -- Sharpe 改进
        OR delta_ic >= 0.0           -- 或 IC 不变差 ⭐
    )
    -- 风险控制
    AND delta_max_dd <= 0.03         -- 回撤控制
)
```

**is_big_success** (明显成功 - Line 140-154):
```sql
(
    (delta_sharpe >= 0.10 OR delta_ic >= 0.01)  -- 显著改进
    AND oos_avg_sharpe >= 1.5
    AND oos_avg_profit_factor >= 1.3
    AND oos_avg_ic >= 0.03  -- 强预测能力 ⭐
)
```

---

### 3️⃣ tools/oof_ic_calculator.py - IC 计算工具

**核心函数**:

1. **compute_fold_ic()** - 单个 OOS slice 的 Spearman IC
   ```python
   ic = compute_fold_ic(df_oos, pred_col='y_pred', label_col='y_true')
   # 返回 Spearman 相关系数 [-1, 1]
   ```

2. **aggregate_oof_summaries_with_ic()** - 汇总所有 folds 的 IC
   ```python
   oos_summary = aggregate_oof_summaries_with_ic(oof_folds)
   # 返回包含 'oos_avg_ic' 的完整 summary
   ```

---

## 🚀 实施步骤（完整链路）

### Step 0: 前置条件检查

```bash
# 检查依赖
python -c "import scipy; print(scipy.__version__)"  # 需要 SciPy

# 如果没有安装:
pip install scipy
```

---

### Step 1: 集成 IC 计算到 OOF/Walk-Forward

**找到你的 walk-forward 评估代码** (可能在 `oof_flow.py`, `walk_forward.py` 或类似文件):

```python
# BEFORE (旧代码 - 没有 IC)
def run_walk_forward(...):
    oos_folds = []
    for fold in folds:
        result = run_backtest(fold)
        oos_folds.append({
            'sharpe': result.sharpe,
            'profit_factor': result.pf,
            ...
        })

    # 简单平均
    oos_summary = {
        'oos_avg_sharpe': mean([f['sharpe'] for f in oos_folds]),
        ...  # 没有 oos_avg_ic
    }
```

**AFTER (新代码 - 包含 IC):**

```python
from tools.oof_ic_calculator import aggregate_oof_summaries_with_ic

def run_walk_forward(...):
    oos_folds = []
    for fold in folds:
        result = run_backtest(fold)

        # ⭐ 关键: 获取包含预测值和实际收益的 DataFrame
        # 必须包含两列:
        #   - 'y_pred': 模型预测值
        #   - 'y_true': 实际未来收益（用于计算 IC）
        df_oos = result.get_oos_predictions_df()

        oos_folds.append({
            'sharpe': result.sharpe,
            'profit_factor': result.pf,
            'max_drawdown': result.max_dd,
            'win_rate': result.win_rate,
            'total_return': result.total_return,
            'trades': result.num_trades,
            'df_oos': df_oos,  # ⭐ 新增这一行
        })

    # ⭐ 使用新的聚合函数（自动计算 IC）
    oos_summary = aggregate_oof_summaries_with_ic(oof_folds)
    # 现在 oos_summary 包含 'oos_avg_ic'

    return {'oos_summary': oos_summary, ...}
```

**关键点**:
- `df_oos` 必须包含 `y_pred` (预测) 和 `y_true` (实际收益) 两列
- 如果列名不同,可以传参: `aggregate_oof_summaries_with_ic(oof_folds, pred_col='my_pred', label_col='my_label')`

---

### Step 2: 部署 ClickHouse Schema

```bash
# 确保 ClickHouse 正在运行
make up  # 或者你的启动命令

# 执行 SQL 创建所有表和视图
clickhouse-client < schema/clickhouse/ml_eval_analysis.sql

# 验证表创建成功
clickhouse-client --query "SHOW TABLES FROM trading LIKE '%ml_eval%';"

# 应该看到:
# ml_eval_flat
# mv_wf_state_to_ml_eval_flat
# (视图不会在这里显示,但可以查询)
```

**验证物化视图**:
```sql
SELECT name, engine, create_table_query
FROM system.tables
WHERE database='trading' AND name LIKE '%ml_eval%';
```

---

### Step 3: 运行测试 Alpha Loop

**目的**: 生成包含 IC 的 checkpoint 数据

```bash
# 使用一个简单的测试配置
python alpha_loop.py --config config/hpo_test_minimal.json

# 或者如果你有特定的 IC 测试配置:
python alpha_loop.py --config config/hpo_test_with_ic.json
```

---

### Step 4: 验证完整链路

#### 4.1 检查 payload 是否包含 IC

```sql
-- 检查最新的 wf_state 记录
SELECT
    run_id,
    trial_id,
    JSONExtractString(payload_json, 'symbol') AS symbol,
    JSONExtractString(payload_json, 'oos_avg_sharpe') AS sharpe,
    JSONExtractString(payload_json, 'oos_avg_ic') AS ic,     -- ⭐ 应该有值
    JSONExtractString(payload_json, 'oos_avg_profit_factor') AS pf
FROM trading.wf_state
WHERE step = 'trial_end'
  AND status = 'done'
ORDER BY updated_at DESC
LIMIT 5;
```

**预期结果**: `ic` 列应该有值（不是空字符串或 "0.0"）

#### 4.2 检查物化视图同步

```sql
-- 检查 ml_eval_flat 表
SELECT
    symbol,
    trial_id,
    oos_avg_sharpe,
    oos_avg_ic,     -- ⭐ 应该有值
    oos_avg_profit_factor,
    updated_at
FROM trading.ml_eval_flat
ORDER BY updated_at DESC
LIMIT 10;
```

#### 4.3 检查 Delta 和 is_success

```sql
-- 检查完整的四维度评估
SELECT
    symbol,
    trial_id,
    updated_at,

    -- 当前值
    oos_avg_sharpe,
    oos_avg_ic,         -- ⭐
    oos_avg_profit_factor,
    oos_avg_max_drawdown,

    -- Delta 值
    delta_sharpe,
    delta_ic,           -- ⭐ 应该是当前 - 上一次
    delta_pf,
    delta_max_dd,

    -- 成功标签
    is_success,         -- ⭐ 基于四维度判定
    is_big_success
FROM trading.ml_eval_with_delta
ORDER BY updated_at DESC
LIMIT 20;
```

**预期结果**:
- `delta_ic` 应该有意义（第一条可能是 NULL）
- `is_success` 应该根据四维度条件正确判定 (0 或 1)

---

### Step 5: 测试 Memory 函数（基于 IC）

创建测试脚本:

```python
# test_ic_integration.py
from scripts.ai_trader.brain.policy.memory import get_model_stats_summary_from_ch

# 测试获取包含 IC 的统计
stats = get_model_stats_summary_from_ch(symbol="TSLA")

print("TSLA Model Stats (last 30 days):")
print(f"  Attempts: {stats['attempts_30d']}")
print(f"  Successes: {stats['successes_30d']}")
print(f"  Success Rate: {stats['success_rate_30d']:.2%}")
print(f"  Latest Sharpe: {stats['latest_sharpe']:.2f}")
print(f"  Latest PF: {stats['latest_pf']:.2f}")
print(f"  Fail Streak: {stats['fail_streak']}")

# 验证: success_rate 现在是基于四维度的 is_success，而不是 status=ok
```

```bash
python test_ic_integration.py
```

---

## 📊 成功标准调优

### 当前阈值（终局版 - 温和）

```sql
is_success = (
    Sharpe >= 1.2           -- 绝对质量
    AND PF >= 1.2
    AND IC >= 0.02          -- ⭐ IC 门槛
    AND (
        delta_Sharpe >= 0.02    -- Sharpe 改进
        OR delta_IC >= 0.0      -- 或 IC 不变差
    )
    AND delta_MaxDD <= 0.03
)
```

### 调优建议

**如果运行 2-3 周后，发现成功率过高 (>80%)**:

```sql
-- 更严格版本
is_success = (
    Sharpe >= 1.4
    AND PF >= 1.3
    AND IC >= 0.03          -- 提高 IC 门槛
    AND (
        delta_Sharpe >= 0.05
        OR delta_IC >= 0.005  -- IC 也需要有改进
    )
    AND delta_MaxDD <= 0.02
)
```

**如果成功率过低 (<20%)**:

```sql
-- 更宽松版本
is_success = (
    Sharpe >= 1.0
    AND PF >= 1.1
    AND IC >= 0.01          -- 降低 IC 门槛
    AND (
        delta_Sharpe >= 0.01
        OR delta_IC >= -0.005  -- 允许 IC 轻微下降
    )
    AND delta_MaxDD <= 0.05
)
```

**修改后需要**:
```sql
DROP VIEW IF EXISTS trading.ml_eval_with_delta;
-- 然后重新执行 CREATE OR REPLACE VIEW ... 的 SQL
```

---

## 🔧 后续集成（control_event_consumer.py）

在 `_execute_ml_actions` 或信心校准的地方:

```python
from scripts.ai_trader.brain.policy.memory import get_model_stats_summary_from_ch

def calibrate_action_confidence(action, symbol, model_family, horizon, window_days, cfg):
    """基于四维度真实指标历史来校准 LLM 的 confidence"""
    raw_conf = action.get("confidence", 0.7)

    # 从 ClickHouse 获取真实成功率（基于四维度）
    stats = get_model_stats_summary_from_ch(
        symbol=symbol,
        model_family=model_family,
        horizon=horizon,
        window_days=window_days
    )

    success_rate = stats["success_rate_30d"]  # 基于 is_success (Sharpe+PF+IC+DD)
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

## 📈 监控查询

### 1. 四维度成功率分布

```sql
SELECT
    symbol,
    count(*) AS total_trials,
    sum(is_success::UInt8) AS successes,
    sum(is_big_success::UInt8) AS big_successes,
    round(100.0 * successes / total_trials, 2) AS success_rate_pct,
    round(avg(oos_avg_sharpe), 3) AS avg_sharpe,
    round(avg(oos_avg_ic), 4) AS avg_ic,           -- ⭐
    round(avg(oos_avg_profit_factor), 3) AS avg_pf
FROM trading.ml_eval_with_delta
WHERE updated_at >= now() - INTERVAL 30 DAY
GROUP BY symbol
ORDER BY success_rate_pct DESC;
```

### 2. IC 分布分析

```sql
SELECT
    symbol,
    quantile(0.25)(oos_avg_ic) AS ic_p25,
    quantile(0.50)(oos_avg_ic) AS ic_median,
    quantile(0.75)(oos_avg_ic) AS ic_p75,
    max(oos_avg_ic) AS ic_max,
    min(oos_avg_ic) AS ic_min
FROM trading.ml_eval_with_delta
WHERE updated_at >= now() - INTERVAL 30 DAY
  AND oos_avg_ic IS NOT NULL
GROUP BY symbol;
```

### 3. Delta IC vs Delta Sharpe 相关性

```sql
SELECT
    symbol,
    corr(delta_ic, delta_sharpe) AS ic_sharpe_correlation
FROM trading.ml_eval_with_delta
WHERE updated_at >= now() - INTERVAL 30 DAY
  AND delta_ic IS NOT NULL
  AND delta_sharpe IS NOT NULL
GROUP BY symbol;
```

---

## ⚠️ 常见问题

### Q1: IC 一直是 0 怎么办？

**可能原因**:
1. OOF 评估没有正确集成 IC 计算
2. df_oos 没有包含 `y_pred` 和 `y_true` 列
3. df_oos 为空或样本太少

**调试**:
```python
# 在 walk_forward 代码中添加调试
df_oos = result.get_oos_predictions_df()
print(f"df_oos shape: {df_oos.shape}")
print(f"Columns: {df_oos.columns.tolist()}")
print(f"Sample:\n{df_oos.head()}")

# 手动计算 IC 测试
from tools.oof_ic_calculator import compute_fold_ic
ic = compute_fold_ic(df_oos)
print(f"IC: {ic:.4f}")
```

### Q2: ClickHouse 查询报错 "column oos_avg_ic doesn't exist"

**解决方案**:
```sql
-- 1. 检查表定义
DESC trading.ml_eval_flat;

-- 2. 如果没有 oos_avg_ic 列，删除重建
DROP TABLE IF EXISTS trading.ml_eval_flat;
DROP VIEW IF EXISTS trading.mv_wf_state_to_ml_eval_flat;

-- 3. 重新执行 schema
clickhouse-client < schema/clickhouse/ml_eval_analysis.sql
```

### Q3: is_success 全是 0 或全是 1

**检查阈值设置**:
```sql
-- 查看实际分布
SELECT
    min(oos_avg_sharpe) AS min_sharpe,
    max(oos_avg_sharpe) AS max_sharpe,
    min(oos_avg_ic) AS min_ic,
    max(oos_avg_ic) AS max_ic,
    min(oos_avg_profit_factor) AS min_pf,
    max(oos_avg_profit_factor) AS max_pf
FROM trading.ml_eval_with_delta
WHERE updated_at >= now() - INTERVAL 30 DAY;
```

根据实际分布调整阈值。

---

## 📚 相关文档

- 原始分析: `doc/brain/brainUpgrade.md`
- ClickHouse Schema: `schema/clickhouse/ml_eval_analysis.sql`
- IC 计算工具: `tools/oof_ic_calculator.py`
- Memory 函数: `scripts/ai_trader/brain/policy/memory.py`

---

**版本**: 2.0 (终局版 - IC 完整集成)
**最后更新**: 2025-11-08
**状态**: 代码修改完成，等待 OOF IC 集成和测试

---

## 🎉 成功标志

当你完成所有步骤后，应该能看到：

1. ✅ ClickHouse 中每条 trial 记录都有 `oos_avg_ic` 值
2. ✅ `ml_eval_with_delta` 视图显示 `delta_ic` 和基于四维度的 `is_success`
3. ✅ Memory 函数返回基于真实指标改善的 `success_rate`
4. ✅ AI 大脑开始根据 Sharpe+PF+IC+DD 的综合表现调整动作置信度

**这时候，你的 AI 大脑才真正开始学习"什么样的动作能让模型变好"！** 🚀
