# HPO参数部署指南

> **如何将HPO优化的参数应用到实盘交易系统**

---

## 📋 概览

HPO优化完成后会生成一个 `<study-name>_best_params.json` 文件，包含最优参数。本指南说明如何将这些参数应用到实盘策略配置。

---

## 🔄 完整工作流程

### 步骤1: 运行HPO优化

```bash
venv/bin/python hpo_cli_upgraded.py \
  --config config/hpo_tsla_v7_2.json \
  --study-name my-first-hpo \
  --runner wf_hpo:evaluate_trial \
  --backend optuna-tpe \
  --n-trials 20 \
  --n-jobs 4 \
  --seed 2025
```

**输出**: `my-first-hpo_best_params.json`

### 步骤2: 查看优化结果

```bash
cat my-first-hpo_best_params.json
```

**示例输出**:
```json
{
  "A1_thr_sigma": 0.006,
  "A1_rvol_lo": 0.85,
  "A1_rvol_hi": 1.6,
  "A2_grid_g": 0.8,
  "A2_grid_g_floor": 0.6,
  "A2_oneway_thr": 2.2,
  "A2_max_layers": 3,
  "B1_rvol_min": 0.85,
  "B1_pullback_window_M": 8,
  "k_atr": 1.6
}
```

### 步骤3: 应用参数到策略配置

**方式A: 生成新配置文件（推荐）**

```bash
venv/bin/python apply_hpo_params.py \
  --hpo-params my-first-hpo_best_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json \
  --output strategies/configs/tsla_v7_2_hpo_optimized.json
```

**优点**:
- ✅ 保留原配置文件
- ✅ 可以对比新旧参数
- ✅ 安全，不影响现有系统

**方式B: 直接覆盖原配置（谨慎）**

```bash
venv/bin/python apply_hpo_params.py \
  --hpo-params my-first-hpo_best_params.json \
  --strategy-config strategies/configs/tsla_v7_2.json
```

**注意**: 会自动备份原文件到 `tsla_v7_2.json.backup_<timestamp>`

### 步骤4: 验证参数更新

```bash
# 查看关键参数
cat strategies/configs/tsla_v7_2_hpo_optimized.json | jq '{
  k_atr,
  A1: {thr_sigma, rvol_lo, rvol_hi},
  A2: {grid_g, grid_g_floor, oneway_thr, max_layers},
  B1: {rvol_min, pullback_window_M}
}'

# 查看HPO元数据（参数变更历史）
cat strategies/configs/tsla_v7_2_hpo_optimized.json | jq '._hpo_metadata'
```

### 步骤5: 回测验证（推荐）

在Paper Trading或实盘前，先用新参数做回测验证：

```bash
# 方法1: 使用wf_hpo验证（单次运行）
venv/bin/python -c "
from wf_hpo import evaluate_trial
import json

# 加载配置
with open('config/hpo_tsla_v7_2.json') as f:
    config = json.load(f)

# 加载最佳参数
with open('my-first-hpo_best_params.json') as f:
    best_params = json.load(f)

# 回测验证（使用OOS期）
result = evaluate_trial(config, best_params, config['folds'][0])
print(json.dumps(result, indent=2))
"

# 方法2: 使用完整回测系统
# (需要根据你的系统调整命令)
```

### 步骤6: 部署到实盘系统

**6a. 更新config.yml中的策略配置路径**

找到你的 `config.yml` 中TSLA策略的配置:

```yaml
strategies:
  - id: "tsla_v7_2_001"
    enabled: true
    module: "strategies.tsla_v7_2"
    class: "TSLASimplifiedV7_2"
    params:
      config_file: "strategies/configs/tsla_v7_2.json"  # ← 修改这里
      # ...
```

改为:

```yaml
      config_file: "strategies/configs/tsla_v7_2_hpo_optimized.json"  # ← 使用优化后的配置
```

**6b. 重启交易系统**

```bash
# 如果使用systemd
sudo systemctl restart trading-system

# 或使用docker-compose
docker-compose restart trading-bot

# 或直接运行
make down
make up
```

**6c. 监控日志**

```bash
tail -f logs/trading_system.log | grep -i "tsla\|initialized"
```

验证:
- ✅ 策略加载新配置文件
- ✅ 参数值符合HPO优化结果
- ✅ 无报错

---

## 📊 参数映射表

| HPO参数名 | 策略配置位置 | 说明 |
|-----------|-------------|------|
| `k_atr` | `k_atr` (根级) | ATR止损倍数 |
| `A1_thr_sigma` | `A1.thr_sigma` | A1策略VWAP偏离阈值 |
| `A1_rvol_lo` | `A1.rvol_lo` | A1策略相对成交量下限 |
| `A1_rvol_hi` | `A1.rvol_hi` | A1策略相对成交量上限 |
| `A2_grid_g` | `A2.grid_g` | A2策略网格间距 |
| `A2_grid_g_floor` | `A2.grid_g_floor` | A2策略网格间距下限 |
| `A2_oneway_thr` | `A2.oneway_thr` | A2策略单边阈值 |
| `A2_max_layers` | `A2.max_layers` | A2策略最大加仓层数 |
| `B1_rvol_min` | `B1.rvol_min` | B1策略最小相对成交量 |
| `B1_pullback_window_M` | `B1.pullback_window_M` | B1策略回调窗口（分钟）|

---

## 🔍 验证清单

部署前必须验证的检查项:

- [ ] **参数范围合理性**
  - `k_atr` 在 1.4-3.0 之间
  - `A1_thr_sigma` 在 0.004-0.02 之间
  - `A2_max_layers` 在 1-4 之间

- [ ] **配置文件完整性**
  - 所有必需字段存在
  - JSON格式正确
  - 元数据正确记录

- [ ] **回测验证**
  - Sharpe > 0
  - MDD < 配置上限
  - 交易数量合理

- [ ] **Paper Trading测试**
  - 运行至少1个交易日
  - 无异常报错
  - 交易逻辑符合预期

---

## ⚠️ 注意事项

### 1. 市场环境变化

HPO基于历史数据优化，市场条件变化时可能不适用：

- **过拟合风险**: 参数过于贴合训练期数据
- **市场制度变化**: 波动率、流动性、相关性改变
- **极端事件**: 黑天鹅事件下参数可能失效

**应对措施**:
- 定期重新运行HPO（建议每月）
- 监控实盘表现 vs 回测预期
- 设置自动熔断机制

### 2. Walk-Forward验证

确保HPO配置使用了walk-forward validation:

```json
"folds": [
  {
    "fold_id": 1,
    "is_start": "2025-10-07 09:30:00",  // In-Sample
    "is_end": "2025-10-13 16:00:00",
    "oos_start": "2025-10-14 09:30:00", // Out-of-Sample
    "oos_end": "2025-10-17 16:00:00"
  }
]
```

优化在IS期，验证在OOS期，避免数据泄露。

### 3. 增量更新 vs 全量替换

**增量更新（推荐）**:
- 只更新HPO优化的参数
- 保留其他配置（如风控、仓位管理）
- `apply_hpo_params.py` 默认行为

**全量替换（不推荐）**:
- 可能覆盖重要的风控设置
- 需手动验证所有字段

### 4. 多策略系统

如果运行多个策略：

```bash
# 为每个策略分别优化
venv/bin/python hpo_cli_upgraded.py --config config/hpo_tsla_v7_2.json --study-name tsla-hpo ...
venv/bin/python hpo_cli_upgraded.py --config config/hpo_nvda_v7_2.json --study-name nvda-hpo ...

# 应用各自的最佳参数
venv/bin/python apply_hpo_params.py --hpo-params tsla-hpo_best_params.json --strategy-config strategies/configs/tsla_v7_2.json --output strategies/configs/tsla_v7_2_optimized.json
venv/bin/python apply_hpo_params.py --hpo-params nvda-hpo_best_params.json --strategy-config strategies/configs/nvda_v7_2.json --output strategies/configs/nvda_v7_2_optimized.json
```

---

## 🛠️ 故障排查

### 问题1: "KeyError: 'A1'" 或参数未找到

**原因**: 策略配置文件结构与HPO参数不匹配

**解决**:
```bash
# 检查策略配置结构
cat strategies/configs/tsla_v7_2.json | jq 'keys'

# 确认A1/A2/B1节存在
cat strategies/configs/tsla_v7_2.json | jq '{A1, A2, B1}'
```

### 问题2: 参数应用后策略不交易

**可能原因**:
- 优化参数过于保守（如 `A1_thr_sigma` 过大）
- 市场条件不满足新参数阈值

**排查**:
```bash
# 查看策略日志
tail -f logs/trading_system.log | grep -i "tsla\|threshold\|rvol"

# 对比旧参数
diff <(jq '{A1, A2, B1, k_atr}' strategies/configs/tsla_v7_2.json) \
     <(jq '{A1, A2, B1, k_atr}' strategies/configs/tsla_v7_2_hpo_optimized.json)
```

### 问题3: apply_hpo_params.py报错

**常见错误**:
```bash
# FileNotFoundError
→ 检查文件路径是否正确

# JSONDecodeError
→ 检查JSON格式: python -m json.tool my-first-hpo_best_params.json

# PermissionError
→ 检查文件权限: chmod 644 strategies/configs/*.json
```

---

## 📚 相关文档

- `HPO_QUICK_COMMANDS.md` - HPO快速命令参考
- `HPO_COMPLETE_SUMMARY.md` - HPO系统完整总结
- `HPO_AUTO_LOAD_FEATURE.md` - 自动数据加载功能
- `doc/hpo/HPO_UPGRADE_GUIDE.md` - HPO升级指南

---

## 🎯 最佳实践

### 1. 参数更新节奏

```
每月第一个交易日:
  1. 使用最近60天数据运行HPO
  2. Walk-forward验证（IS: 40天, OOS: 20天）
  3. Paper Trading测试3天
  4. 实盘部署新参数
```

### 2. 版本控制

```bash
# 为每次HPO创建git tag
git add strategies/configs/tsla_v7_2_hpo_optimized.json
git commit -m "chore: apply HPO params from study my-first-hpo

Optimized on 2025-10-07 to 2025-10-17 data
Best trial sharpe: 0.45
Changes:
  - A1_thr_sigma: 0.013 → 0.006
  - k_atr: 1.4 → 1.6
"
git tag hpo-tsla-20251023
git push origin hpo-tsla-20251023
```

### 3. A/B测试

```yaml
# config.yml - 同时运行新旧参数对比
strategies:
  - id: "tsla_v7_2_baseline"
    config_file: "strategies/configs/tsla_v7_2.json"
    paper_trading: true  # 旧参数仅模拟

  - id: "tsla_v7_2_hpo"
    config_file: "strategies/configs/tsla_v7_2_hpo_optimized.json"
    paper_trading: false  # 新参数实盘
```

---

**更新日期**: 2025-10-23
**维护者**: Trading Team
**版本**: 1.0
