# HPO升级系统 - 快速命令参考

> **快速参考**: 最常用的HPO命令和配置

---

## 🚀 立即开始

### 1. 快速测试 (3 trials, 单线程)
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_working.json \
  --study-name quick-test \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 3 \
  --n-jobs 1 \
  --seed 2025
```

### 2. 标准优化 (20 trials, 4并行)
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_working.json \
  --study-name standard-opt \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 20 \
  --n-jobs 4 \
  --seed 2025
```

### 3. 生产级优化 (50 trials, 带存储)
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_working.json \
  --study-name production-opt \
  --storage sqlite:///hpo_studies.db \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 50 \
  --n-jobs 4 \
  --pruner asha \
  --seed 2025
```

### 4. 使用BoTorch后端 (更先进)
```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_working.json \
  --study-name botorch-opt \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-botorch \
  --n-trials 30 \
  --n-jobs 4 \
  --seed 2025
```

---

## 📝 参数说明

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `--config` | 配置文件路径 | config/hpo_tsla_working.json |
| `--study-name` | 研究名称 (唯一标识) | 任意字符串 |
| `--runner` | 评估函数 | wf_hpo:evaluate_trial |
| `--backend` | 优化后端 | optuna-tpe 或 optuna-botorch |
| `--n-trials` | 试验次数 | 3-5 (测试), 20-50 (生产) |
| `--n-jobs` | 并行线程数 | 1 (测试), 4 (生产) |
| `--storage` | 持久化存储 | sqlite:///hpo_studies.db (可选) |
| `--pruner` | 早停策略 | median 或 asha (可选) |
| `--seed` | 随机种子 | 2025 (可复现) |

---

## 🔍 检查结果

### 查看最佳参数
```bash
cat {study_name}_best_params.json
```

示例输出:
```json
{
  "A1_thr_sigma": 0.01,
  "A2_grid_g": 0.8,
  "k_atr": 2.4
}
```

### 查看ClickHouse数据 (如启用日志)
```bash
clickhouse-client --query "
  SELECT trial, value, params_json
  FROM trading.hpo_trials
  WHERE study = 'my-study'
  ORDER BY value DESC
  LIMIT 5
"
```

---

## ⚙️ 配置文件要点

### 必需字段
```json
{
  "strategy_path": "strategies.tsla_v7_2.TSLASimplifiedV7_2",

  "grid": {
    "A1_thr_sigma": [0.006, 0.008, 0.010],
    "A2_grid_g": [0.8, 1.0, 1.2],
    "k_atr": [1.6, 2.0, 2.4]
  },

  "folds": [
    {
      "fold_id": 1,
      "is_start": "2025-10-07 09:30:00",
      "is_end": "2025-10-13 16:00:00",
      "oos_start": "2025-10-14 09:30:00",
      "oos_end": "2025-10-17 16:00:00",
      "purge_days": 1
    }
  ],

  "objective": {
    "metric": "sharpe",
    "maximize": true,
    "min_trades": 0,
    "max_mdd": 0.50,
    "min_pnl": -10000
  }
}
```

### 常见调整

**如果所有试验被剪枝**:
```json
"objective": {
  "min_trades": 0,     // 放宽到0
  "max_mdd": 0.80,     // 放宽到80%
  "min_pnl": -50000    // 放宽到-50000
}
```

**如果需要更大搜索空间**:
```json
"grid": {
  "A1_thr_sigma": [0.004, 0.006, 0.008, 0.010, 0.012],
  "A2_grid_g": [0.6, 0.8, 1.0, 1.2, 1.5],
  "k_atr": [1.2, 1.6, 2.0, 2.4, 2.8]
}
```

---

## 🐛 常见问题

### Q1: ModuleNotFoundError: No module named 'optuna'
**A**: 使用venv中的Python
```bash
venv/bin/python hpo_cli_upgraded.py ...
```

### Q2: ValueError: Missing required columns
**A**: 检查ClickHouse数据是否存在
```bash
clickhouse-client --query "
  SELECT toDate(ts) as date, count(*)
  FROM trading.features_1m
  WHERE symbol='TSLA' AND ts >= '2025-10-01'
  GROUP BY date
  ORDER BY date
"
```

### Q3: All trials pruned
**A**: 放宽约束或调整时间范围
- 设置 `min_trades=0`
- 增加 `max_mdd`
- 使用策略活跃交易的日期

### Q4: Port 9000 error
**A**: 使用HTTP端口8123
```json
"clickhouse": {
  "port": 8123
}
```

---

## 📊 性能预期

| 试验数 | 单线程耗时 | 4线程耗时 | 加速比 |
|--------|-----------|-----------|--------|
| 5 | ~5分钟 | ~2分钟 | 2.5x |
| 20 | ~20分钟 | ~6分钟 | 3.3x |
| 50 | ~50分钟 | ~15分钟 | 3.3x |

注: 实际耗时取决于回测时间范围和策略复杂度

---

## 🎯 推荐流程

1. **测试配置** (3 trials, 1 job)
   ```bash
   venv/bin/python hpo_cli_upgraded.py \
     --config config/hpo_tsla_working.json \
     --study-name test \
     --runner wf_hpo:evaluate_trial \
     --backend optuna-tpe \
     --n-trials 3 \
     --n-jobs 1 \
     --seed 2025
   ```

2. **检查结果** - 确认至少有1个试验完成

3. **正式优化** (20-50 trials, 4 jobs)
   ```bash
   venv/bin/python hpo_cli_upgraded.py \
     --config config/hpo_tsla_working.json \
     --study-name production \
     --runner wf_hpo:evaluate_trial \
     --backend optuna-tpe \
     --n-trials 20 \
     --n-jobs 4 \
     --seed 2025
   ```

4. **应用最佳参数** - 查看 `{study_name}_best_params.json`

---

**快速帮助**: 详细文档请参考 `HPO_FINAL_STATUS.md`
