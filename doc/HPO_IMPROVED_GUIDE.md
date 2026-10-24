# HPO 改进方案 - 使用更多历史数据

> **生成时间**: 2025-10-24
> **基于发现**: 你有 6 个月数据 (2025-04-17 ~ 2025-10-23, 约 130 个交易日)

---

## 📊 数据分析

### 可用数据总览

| 月份 | 交易日 | 平均价格 | 波动率 | HPO 适用性 |
|------|--------|---------|--------|-----------|
| 2025-04 | 20 | $262 | 22 | ⚠️ 数据量少 |
| 2025-05 | 52 | $324 | 31 | ✅ 适中波动 |
| 2025-06 | 49 | $325 | 15 | ✅ **低波动 - 理想** |
| 2025-07 | 54 | $315 | 11 | ✅ **低波动 - 理想** |
| 2025-08 | 52 | $330 | 14 | ✅ **低波动 - 理想** |
| 2025-09 | 52 | $395 | 42 | ❌ 高波动 - 避免 |
| 2025-10 | 26 | $439 | 12 | ✅ 低波动 |

### 推荐时间段

**最佳 HPO 时段**: 2025-06-01 ~ 2025-08-31 (3个月, ~70个交易日)
- ✅ 低波动率 (11-15)
- ✅ 数据量充足
- ✅ 不太可能有 ATR spike

---

## 🔧 改进方案

### 问题回顾

**原始配置问题**:
1. ❌ 时间段太短 (10月7-17, 仅10天)
2. ❌ 选择了高波动期 (ATR spike)
3. ❌ k_atr 范围太小 ([1.6, 2.0, 2.4])
4. ❌ 只有 1 个 fold (无法验证泛化能力)

**结果**:
- 0 笔交易 (IS 和 OOS 都是)
- 无法验证参数有效性
- HPO 选择了"躺平策略" (k_atr=1.6)

### 改进策略

#### 方案 1: 多 Fold Walk-Forward (推荐)

**配置文件**: `config/hpo_tsla_v7_2_multi_fold.json` (已创建)

**改进点**:
1. ✅ **3 个独立的 fold** (7月-8月)
2. ✅ **k_atr 范围扩大**: [2.0, 2.5, 3.0, 3.5, 4.0]
3. ✅ **min_trades 约束**: 至少 3 笔交易
4. ✅ **低波动时段**: 避开 9 月

**Fold 设计**:
```
Fold 1: IS=2025-07-01~07-14, OOS=2025-07-15~07-21
Fold 2: IS=2025-07-22~08-04, OOS=2025-08-05~08-11
Fold 3: IS=2025-08-12~08-25, OOS=2025-08-26~09-01
```

**运行命令**:
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2_multi_fold.json \
  --study-name tsla-multi-fold \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 100 \
  --n-jobs 4 \
  --seed 2025
```

**预期结果**:
- HPO 会在 3 个 fold 上平均优化
- 参数需要在多个时段都表现良好
- 更稳健的参数选择

#### 方案 2: 单 Fold 长时段

**适合快速测试**

创建配置:
```json
{
  "folds": [
    {
      "fold_id": 1,
      "is_start": "2025-06-01 09:30:00",
      "is_end":   "2025-07-31 16:00:00",  // 2个月 IS
      "oos_start":"2025-08-01 09:30:00",
      "oos_end":  "2025-08-31 16:00:00",  // 1个月 OOS
      "purge_days": 1,
      "embargo_days": 0
    }
  ]
}
```

**优点**: 数据量大, 训练充分
**缺点**: 可能过拟合单一时段

---

## 📝 运行步骤

### Step 1: 验证数据完整性

```bash
# 检查 7-8 月数据是否完整
curl -s "http://localhost:8123/" --data "
SELECT
    toDate(ts) as date,
    count(*) as bars
FROM trading.features_1m
WHERE symbol = 'TSLA'
  AND ts >= '2025-07-01 09:30:00'
  AND ts <= '2025-08-31 16:00:00'
GROUP BY date
ORDER BY date
" | head -20
```

**检查点**:
- [ ] 每个交易日约 390 bars
- [ ] 没有缺失日期
- [ ] 数据连续

### Step 2: 运行改进的 HPO

```bash
# 使用多 fold 配置
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2_multi_fold.json \
  --study-name tsla-robust-hpo \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 100 \
  --n-jobs 4 \
  --seed 2025 \
  2>&1 | tee hpo_multi_fold.log
```

**参数说明**:
- `--n-trials 100`: 尝试 100 组参数
- `--n-jobs 4`: 并行 4 个进程
- `tee hpo_multi_fold.log`: 保存日志

### Step 3: 监控进度

```bash
# 另开一个终端，实时查看日志
tail -f hpo_multi_fold.log | grep -E "Trial|Best|sharpe"
```

**健康指标**:
- ✅ 至少 20% 的 trials 完成 (not pruned)
- ✅ Best trial 有 > 5 笔交易
- ✅ Sharpe ratio > 0.5

### Step 4: 验证结果

```bash
# 查看最佳参数
cat tsla-robust-hpo_best_params.json

# 在 OOS 上验证
venv/bin/python validate_hpo_params.py \
  --config config/hpo_tsla_v7_2_multi_fold.json \
  --params tsla-robust-hpo_best_params.json \
  --fold 0 \
  --period OOS
```

**验证标准**:
- ✅ OOS Sharpe > 0
- ✅ OOS Sharpe > IS Sharpe * 0.6 (没有严重过拟合)
- ✅ OOS 至少 3 笔交易

---

## 🎯 预期改进

### Before (原始配置)

```
时间段: 2025-10-07 ~ 2025-10-17 (10天)
k_atr 范围: [1.6, 2.0, 2.4]
Folds: 1

结果:
- IS trades: 0
- OOS trades: 0
- 无法验证参数有效性
```

### After (改进配置)

```
时间段: 2025-07-01 ~ 2025-09-01 (2个月)
k_atr 范围: [2.0, 2.5, 3.0, 3.5, 4.0]
Folds: 3

预期结果:
- IS trades: 10-50 (每个 fold)
- OOS trades: 5-20 (每个 fold)
- 可以充分验证参数有效性
```

---

## 🔍 故障排查

### 问题 1: 仍然 0 交易

**检查**:
```bash
# 查看 k_atr 的选择范围
grep "k_atr" tsla-robust-hpo_best_params.json

# 如果还是选了最小值 (2.0), 说明还需要扩大范围
```

**解决**:
```json
{
  "grid": {
    "k_atr": [3.0, 4.0, 5.0, 6.0]  // 进一步放宽
  }
}
```

### 问题 2: 所有 trials 被 pruned

**原因**: 约束太严格

**解决**:
```json
{
  "objective": {
    "min_trades": 1,     // 从 3 降到 1
    "max_mdd": 0.80,     // 从 0.50 放宽到 0.80
    "min_pnl": -20000    // 从 -10000 放宽到 -20000
  }
}
```

### 问题 3: HPO 运行太慢

**优化**:
```bash
# 减少 trials 数量
--n-trials 50  # 从 100 降到 50

# 增加并行度
--n-jobs 8  # 从 4 增加到 8 (如果你的 CPU 支持)

# 减少 fold 数量
# 在配置中只保留 fold 1 和 fold 2
```

---

## 📊 结果对比

运行完成后，创建对比报告：

```bash
# 对比原始参数 vs HPO 优化参数
venv/bin/python compare_hpo_params.py \
  --hpo-params tsla-robust-hpo_best_params.json \
  --config config/hpo_tsla_v7_2_multi_fold.json
```

**期望看到**:
```
Parameter Comparison:
  k_atr: 1.4 (original) → 3.0 (HPO) [+114%]
  A1_thr_sigma: 0.017 → 0.009 [-47%]
  ...

Performance on OOS:
  Sharpe: 0.00 (original) → 1.25 (HPO) [+∞%]
  Trades: 0 (original) → 15 (HPO)
  PnL: $0 (original) → $2,450 (HPO)

Verdict: ✅ HPO Parameters Show SIGNIFICANT Improvement
```

---

## 🚀 下一步

### 立即执行

```bash
# 1. 运行改进的 HPO
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2_multi_fold.json \
  --study-name tsla-robust-hpo \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 100 \
  --n-jobs 4 \
  --seed 2025

# 2. 等待完成后验证结果
venv/bin/python validate_hpo_params.py \
  --config config/hpo_tsla_v7_2_multi_fold.json \
  --params tsla-robust-hpo_best_params.json \
  --fold 0 --period OOS

# 3. 如果满意，应用到策略
venv/bin/python apply_hpo_params.py \
  --hpo-params tsla-robust-hpo_best_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_hpo_optimized.json
```

### 可选进阶

**更大规模 HPO** (如果时间允许):
```bash
# 使用全部 6 个月数据，5 个 fold
# 需要修改配置文件添加更多 fold
# 运行时间: 数小时到 1 天

venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2_full_history.json \
  --study-name tsla-full-hpo \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 200 \
  --n-jobs 8 \
  --seed 2025
```

---

## 📚 相关文档

- `HPO_PROCESS_EXPLAINED.md` - HPO 流程详解
- `HPO_VALIDATION_DIAGNOSIS.md` - 原始问题诊断
- `HONEST_HPO_TEST_RESULTS.md` - 测试分析
- `HPO_DEPLOYMENT_GUIDE.md` - 如何应用参数

---

**关键改进点总结**:
1. ✅ 使用更多历史数据 (2个月 vs 10天)
2. ✅ 选择低波动时段 (7-8月 vs 10月)
3. ✅ 扩大 k_atr 范围 ([2.0-4.0] vs [1.6-2.4])
4. ✅ 多 fold 验证 (3 folds vs 1 fold)
5. ✅ 增加约束 (min_trades=3)

**最后更新**: 2025-10-24
**配置文件**: `config/hpo_tsla_v7_2_multi_fold.json`
