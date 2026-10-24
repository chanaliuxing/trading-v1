# Alpha Loop - AI 驱动的 HPO 完整指南

> **生成时间**: 2025-10-24
> **配置文件**: `alpha_config_tsla_robust.json`
> **目标**: 使用 LLM + ML + Optuna 三层优化找到最优参数

---

## 📋 系统概述

**Alpha Loop** 是一个多代、AI 驱动的超参数优化框架，结合了：
1. **LLM (GPT-4)**: 基于业务逻辑提议参数
2. **ML (Random Forest)**: 基于历史数据特征重要性提议参数
3. **Optuna (Bayesian)**: 在精细范围内深度搜索

### 三种核心模式

| 模式 | 描述 | 适用场景 | 优势 |
|------|------|---------|------|
| `optuna` | 纯贝叶斯优化 | 连续搜索空间 | 高效、快速收敛 |
| `coop` | LLM + ML 合作 | 离散网格探索 | 广度优先、发现新区域 |
| `coop_optuna` | **两阶段组合** | **推荐使用** | **先探索后挖掘** |

---

## 🔧 配置详解

### 关键配置项

我已经为你创建了 **`alpha_config_tsla_robust.json`**，针对你的发现做了优化：

#### 1. 时间范围 (基于你的数据分析)

```json
{
  "hpo": {
    "start": "2025-07-01 09:30:00",  // 低波动期开始
    "end": "2025-08-31 16:00:00",    // 2个月数据

    "folds": [
      // 3个 walk-forward folds (7-8月)
      {"fold_id": 1, "is": "07-01~07-14", "oos": "07-15~07-21"},
      {"fold_id": 2, "is": "07-22~08-04", "oos": "08-05~08-11"},
      {"fold_id": 3, "is": "08-12~08-25", "oos": "08-26~09-01"}
    ]
  }
}
```

**改进点**:
- ✅ 使用低波动期 (避开 9-10 月的 ATR spike)
- ✅ 3 个独立 fold 验证泛化能力
- ✅ 充足的数据量 (每个 fold 约 10-15 个交易日)

#### 2. 参数网格 (扩展 k_atr)

```json
{
  "grid": {
    "k_atr": {
      "type": "float",
      "low": 2.0,    // 原来最大值 2.4 → 现在起点 2.0
      "high": 4.5,   // 扩大到 4.5
      "step": 0.5
    },
    "A1_thr_sigma": {"type": "float", "low": 0.006, "high": 0.014, "step": 0.002},
    "A2_grid_g": {"type": "float", "low": 0.7, "high": 1.6, "step": 0.1},
    // ... 其他 7 个参数
  }
}
```

**改进点**:
- ✅ k_atr 范围扩大 (2.0-4.5 vs 1.6-2.4)
- ✅ 使用连续范围 (适合 Optuna Bayesian 优化)
- ✅ 覆盖所有 TSLA V7.2 策略参数

#### 3. 约束条件

```json
{
  "objective": {
    "metric": "sharpe",
    "maximize": true,
    "min_trades": 5,     // 至少 5 笔交易
    "max_mdd": 0.50,     // 最大回撤 50%
    "min_pnl": -20000    // 最低 PnL -$20k
  }
}
```

**改进点**:
- ✅ min_trades=5 (确保有足够交易验证)
- ✅ 约束合理 (不会太严格导致全部 pruned)

#### 4. AI 配置

```json
{
  "llm": {
    "provider": "openai",
    "model": "gpt-4o-mini",  // 成本友好的版本
    "api_key_env": "OPENAI_API_KEY"
  },
  "ml": {
    "n_points": 20,          // 用 20 个最佳 trial 训练 ML
    "model_type": "random_forest"
  },
  "optuna": {
    "trials": 50,            // Optuna 阶段运行 50 次试验
    "sampler": "tpe",        // Tree-structured Parzen Estimator
    "storage": "sqlite:///alpha_optuna_tsla.db"
  }
}
```

---

## 🚀 运行步骤

### 前置准备

1. **检查 OpenAI API Key**
```bash
echo $OPENAI_API_KEY
# 如果没有设置:
export OPENAI_API_KEY="sk-xxx"
```

2. **检查数据完整性**
```bash
curl -s "http://localhost:8123/" --data "
SELECT
    toDate(min(ts)) as start,
    toDate(max(ts)) as end,
    count(*) as bars
FROM trading.features_1m
WHERE symbol = 'TSLA'
  AND ts >= '2025-07-01'
  AND ts <= '2025-08-31'
"
```

预期输出: `bars ≈ 38,000` (2个月 × 20天/月 × 390 bars/天)

3. **创建输出目录**
```bash
mkdir -p alpha_out
```

---

### 场景 1: 快速测试 (Optuna 模式)

**适合**: 验证配置是否正确, 快速迭代

```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider optuna \
  --gens 1 \
  --out_dir alpha_out/quick_test \
  --k-top 10
```

**预期**:
- 运行时间: 30-60 分钟 (50 trials × 3 folds)
- 输出: `alpha_out/quick_test/gen_01/gen_summary.json`
- 检查: best_params 和 oos_summary

---

### 场景 2: 完整优化 (Coop + Optuna 两阶段)

**适合**: 生产环境, 寻找全局最优

```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider coop_optuna \
  --gens 3 \
  --out_dir alpha_out/coop_optuna_tsla \
  --k-top 15
```

**工作流程**:

```
第 1 代 (Coop 阶段):
  ├─ LLM 分析基础网格 → 提议新参数范围
  ├─ ML 分析历史 trials → 提议重要特征
  ├─ 合并 LLM + ML 提议 → 生成扩展网格
  └─ 运行 HPO on 扩展网格
     结果: 发现潜在优势区域

第 2 代 (Optuna 阶段):
  ├─ 分析第 1 代 top-15 结果
  ├─ 在 best params 周围创建邻域 (±25%)
  ├─ 启动 Optuna Bayesian 优化 (50 trials)
  └─ 在邻域内精细搜索
     结果: 精确定位最优参数

第 3 代 (深度挖掘):
  ├─ 分析第 2 代结果
  ├─ 进一步缩小邻域 (±15%)
  ├─ Optuna 再次优化
  └─ 最终收敛
     结果: 全局最优 + 验证稳定性
```

**预期**:
- 运行时间: 3-6 小时
- 输出:
  ```
  alpha_out/coop_optuna_tsla/
  ├── gen_01/
  │   ├── coop_merged.json      # LLM+ML 合并的网格
  │   ├── llm_refine.json       # LLM 分析和理由
  │   ├── ml_refine.json        # ML 特征重要性
  │   ├── rows.csv              # 所有 trial 详情
  │   └── oos_summary.json      # OOS 性能
  ├── gen_02/
  │   ├── optuna_space.json     # Optuna 邻域定义
  │   ├── gen_summary.json      # 最佳结果
  │   └── ...
  ├── gen_03/
  │   └── gen_summary.json      # 最终最优
  └── index.json                # 所有代汇总
  ```

---

### 场景 3: 仅 LLM 或 ML 驱动

**仅 LLM** (适合业务逻辑驱动):
```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider llm \
  --gens 2 \
  --out_dir alpha_out/llm_only
```

**仅 ML** (适合数据驱动):
```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider ml \
  --gens 2 \
  --out_dir alpha_out/ml_only
```

---

## 📊 监控和分析

### 实时监控

```bash
# 另开一个终端
tail -f alpha_out/coop_optuna_tsla/alpha_loop.log | grep -E "Gen|Trial|Best"
```

### 查看进度

```bash
# 检查 index.json
cat alpha_out/coop_optuna_tsla/index.json | jq '.generations[] | {gen, best_sharpe: .best_trial.oos_sharpe}'
```

预期输出:
```json
{
  "gen": 1,
  "best_sharpe": 0.85
}
{
  "gen": 2,
  "best_sharpe": 1.23
}
{
  "gen": 3,
  "best_sharpe": 1.35
}
```

### 分析最佳结果

```bash
# 查看最终最优参数
cat alpha_out/coop_optuna_tsla/gen_03/gen_summary.json | jq '.stage2.best_params'
```

预期输出:
```json
{
  "k_atr": 3.2,
  "A1_thr_sigma": 0.009,
  "A2_grid_g": 1.1,
  "A1_rvol_lo": 0.82,
  "A1_rvol_hi": 1.75,
  "A2_grid_g_floor": 0.75,
  "A2_oneway_thr": 2.15,
  "A2_max_layers": 4,
  "B1_rvol_min": 0.88,
  "B1_pullback_window_M": 6
}
```

### 验证 OOS 性能

```bash
cat alpha_out/coop_optuna_tsla/gen_03/gen_summary.json | jq '.stage2.oos_summary'
```

预期输出:
```json
{
  "oos_sharpe": 1.35,
  "oos_pnl": 4250.00,
  "oos_trades": 22,
  "oos_mdd": 0.08,
  "oos_win_rate": 0.64
}
```

---

## 🔍 故障排查

### 问题 1: LLM API 失败

**错误**: `OpenAI API key not found`

**解决**:
```bash
# 检查环境变量
echo $OPENAI_API_KEY

# 或者在配置中直接设置 (不推荐, 不安全)
{
  "llm": {
    "api_key": "sk-xxx"  // 仅测试用
  }
}
```

### 问题 2: 所有 trials 被 pruned

**原因**: 约束太严格或数据问题

**诊断**:
```bash
# 检查第一代的 rows.csv
cat alpha_out/coop_optuna_tsla/gen_01/rows.csv | grep "COMPLETE"
```

**解决**:
```json
{
  "objective": {
    "min_trades": 2,    // 从 5 降到 2
    "max_mdd": 0.60,    // 从 0.50 放宽到 0.60
    "min_pnl": -30000   // 从 -20000 放宽
  }
}
```

### 问题 3: Optuna 阶段没有改进

**原因**: 邻域太小或 trials 不够

**解决**:
```json
{
  "optuna": {
    "trials": 100,         // 从 50 增加到 100
    "neighbor_scale": 0.35 // 从 0.25 扩大到 0.35
  }
}
```

### 问题 4: 运行太慢

**优化**:
```json
{
  "hpo": {
    "parallel": {
      "enabled": true,
      "workers": 8  // 从 4 增加到 8
    }
  },
  "optuna": {
    "trials": 30  // 从 50 减少到 30
  }
}
```

---

## 🎯 结果应用

### Step 1: 提取最佳参数

```bash
# 自动提取
cat alpha_out/coop_optuna_tsla/gen_03/gen_summary.json | \
  jq '.stage2.best_params' > alpha_best_params.json
```

### Step 2: 应用到策略

```bash
venv/bin/python apply_hpo_params.py \
  --hpo-params alpha_best_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_alpha_optimized.json
```

### Step 3: 验证参数

```bash
# 在更长时间段验证
venv/bin/python validate_hpo_params.py \
  --config config/hpo_tsla_v7_2_multi_fold.json \
  --params alpha_best_params.json \
  --fold 0 --period OOS
```

---

## 📈 预期改进

### Before (手动 HPO)

```
方法: 单次 Grid Search
参数: 固定网格 [1.6, 2.0, 2.4]
结果: k_atr=1.6, Sharpe=0.00, Trades=0
```

### After (Alpha Loop)

```
方法: LLM+ML+Optuna 三层优化
参数: 动态探索 + 精细搜索
预期结果:
  - k_atr: 2.8-3.5 (自动发现最优范围)
  - IS Sharpe: 1.2-1.8
  - OOS Sharpe: 0.9-1.4
  - Trades: 15-35
  - 泛化能力: 强 (3 fold 验证)
```

---

## 🚀 快速开始

```bash
# 1. 确保环境变量
export OPENAI_API_KEY="sk-xxx"

# 2. 快速测试 (5分钟)
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider optuna \
  --gens 1 \
  --out_dir alpha_out/quick_test

# 3. 查看结果
cat alpha_out/quick_test/gen_01/gen_summary.json | jq '.best_params'

# 4. 如果满意, 运行完整优化 (3-6小时)
venv/bin/python alpha_loop.py \
  --config alpha_config_tsla_robust.json \
  --provider coop_optuna \
  --gens 3 \
  --out_dir alpha_out/production_run

# 5. 应用最佳参数
cat alpha_out/production_run/gen_03/gen_summary.json | \
  jq '.stage2.best_params' > final_best_params.json

venv/bin/python apply_hpo_params.py \
  --hpo-params final_best_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_final.json
```

---

## 📚 相关文档

- `HPO_PROCESS_EXPLAINED.md` - 基础 HPO 原理
- `HPO_IMPROVED_GUIDE.md` - 手动 HPO 改进方案
- `HPO_VALIDATION_DIAGNOSIS.md` - 问题诊断
- `alpha_config_tsla_robust.json` - 优化后的配置

---

**关键优势总结**:
1. ✅ **AI 驱动**: LLM 提供业务洞察, ML 提供数据洞察
2. ✅ **两阶段优化**: 先探索后挖掘, 避免局部最优
3. ✅ **自动化**: 无需手动调整参数范围
4. ✅ **可解释**: LLM 提供优化理由 (llm_refine.json)
5. ✅ **稳健**: 3 fold walk-forward 验证

**最后更新**: 2025-10-24
**配置文件**: `alpha_config_tsla_robust.json`
**推荐模式**: `coop_optuna` (3 代)
