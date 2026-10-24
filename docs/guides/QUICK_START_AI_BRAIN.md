# 🚀 AI大脑 + 模型池快速上手指南

## 一、5分钟快速测试

### 1. 验证模板系统是否工作

```bash
cd .

# 测试1: 查看可用模板
venv/bin/python -c "
from wf_hpo import MODEL_SPACE_TEMPLATES
for name in MODEL_SPACE_TEMPLATES.keys():
    print(f'  - {name}')
"

# 应该看到8个模板：
#   - linear_reg_basic
#   - linear_clf_basic
#   - knn_basic
#   - xgb_reg_basic
#   - lgbm_reg_basic
#   - mixed_small
#   - tree_ensemble
#   - full_exploration
```

### 2. 运行最简单的模板测试

```bash
# 使用预配置的测试文件 (mixed_small 模板)
venv/bin/python alpha_loop.py \
  --config alpha_config_template_test.json \
  --provider optuna \
  --gens 1 \
  --out_dir /tmp/quick_test

# 观察日志，应该看到：
# [HPO] Using model_space_template='mixed_small' to populate optuna_space
# [HPO]   - model_type choices: ['xgb', 'lasso', 'ridge', 'elasticnet', 'knn']
# [HPO]   - model_params variants: 9
```

**预期结果**:
- 运行3个trials (配置中设置)
- 自动尝试 XGB/Lasso/Ridge/ElasticNet/KNN
- 生成 `/tmp/quick_test/index.json` 结果文件

### 3. 查看结果

```bash
# 查看最佳模型
cat /tmp/quick_test/index.json | jq '.best_params.model_type'

# 查看最佳 Sharpe
cat /tmp/quick_test/index.json | jq '.oos_summary.sharpe'

# 查看所有试验
cat /tmp/quick_test/leaderboard_is.csv | column -t -s,
```

---

## 二、AI大脑工作流（完整示例）

### Step 1: 准备 AI Prompt

将以下内容复制到你的 LLM (ChatGPT/Claude/DeepSeek):

```
【System Prompt - 从文件读取】
<内容来自 prompts/ml_hpo_brain_prompt.md>

【用户任务】
我要设计一个日内短线策略：
- 标的: TSLA, NVDA
- 数据: 1分钟K线
- 预测: 未来5分钟收益
- 模型: 线性 + XGB 混合
- 目标: 最大化 Sharpe，至少8笔交易
- 资源: M1 MacBook，不要超过30 trials
```

### Step 2: AI 生成配置

**AI大脑输出示例**:
```json
{
  "hpo": {
    "symbols": ["TSLA", "NVDA"],
    "start": "2025-10-01 09:30:00",
    "end": "2025-11-08 16:00:00",
    "interval_sec": 60,
    "features_table": "trading.features_1m",
    "model_task": "reg",
    "model_target_col": "y",
    "model_horizon_bars": 5,
    "model_space_template": "mixed_small",
    "fold_generator": {
      "is_days": 20,
      "oos_days": 5,
      "gap_days": 1,
      "step_days": 5,
      "embargo_days": 2
    },
    "objective": {
      "metric": "sharpe",
      "maximize": true,
      "min_trades": 8,
      "max_mdd": 0.4
    },
    "strategy_base_extras": {
      "market_filter": {
        "conf_floor": 0.6,
        "max_spread_bps": 10,
        "min_volume": 80000
      },
      "sizing": {
        "mode": "confidence_scaled",
        "base_qty": 50,
        "max_qty": 500
      }
    }
  },
  "optuna": {
    "trials": 30,
    "target": "is",
    "direction": "maximize",
    "study_name": "intraday_tsla_nvda_001"
  },
  "leakage_guard": {
    "enabled": true,
    "embargo_days": 2
  }
}
```

### Step 3: 保存并运行

```bash
# 保存配置
cat > ai_intraday_001.json << 'EOF'
{上面AI生成的完整JSON}
EOF

# 运行 HPO
venv/bin/python alpha_loop.py \
  --config ai_intraday_001.json \
  --provider optuna \
  --gens 1 \
  --out_dir /tmp/ai_intraday_001

# 观察进度 (另开终端)
tail -f /tmp/ai_intraday_001/*.log | grep -E "Trial|Sharpe|model_type"
```

### Step 4: 分析结果

```bash
# 最佳模型
echo "Best Model:"
cat /tmp/ai_intraday_001/index.json | jq '{
  model_type: .best_params.model_type,
  model_params: .best_params.model_params,
  is_sharpe: .best_metrics.sharpe,
  oos_sharpe: .oos_summary.sharpe
}'

# Top 5 模型对比
echo -e "\nTop 5 Models:"
cat /tmp/ai_intraday_001/leaderboard_is.csv | head -6 | column -t -s,

# OOS 验证指标
echo -e "\nOOS Validation:"
cat /tmp/ai_intraday_001/index.json | jq '.oos_summary'
```

### Step 5: 迭代优化 (可选)

**场景1: ElasticNet 表现最好** → 下一轮专注线性模型

```
【对AI大脑说】
上一轮结果显示 ElasticNet (alpha=0.01, l1_ratio=0.5) 表现最好，
Sharpe=0.52。请生成下一轮配置：
- 专注线性模型家族 (Lasso/Ridge/ElasticNet)
- 扩展 alpha 搜索空间 (1e-5 到 1e-1)
- l1_ratio 细化搜索 (0.1 到 0.9，步长0.1)
- 其他配置保持不变
```

**AI生成**: `model_space_template = "linear_reg_basic"` + 可能手动扩展 optuna_space

**场景2: 交易次数太少** → 降低置信度阈值

```
【对AI大脑说】
交易次数只有5笔，低于目标8笔。请调整配置：
- 降低 conf_floor 从 0.6 到 0.55
- 降低 min_volume 从 80000 到 50000
- 其他保持不变
```

---

## 三、常见场景配置模板

### 场景1: 快速验证（5分钟）

```json
{
  "hpo": {
    "symbols": ["TSLA"],
    "start": "2025-10-15 09:30:00",
    "end": "2025-11-08 16:00:00",
    "model_task": "reg",
    "model_space_template": "linear_reg_basic",
    "objective": {"metric": "sharpe"}
  },
  "optuna": {"trials": 8}
}
```

**特点**: 8个trials × 线性模型 (快) ≈ 2-5分钟

### 场景2: 标准探索（15分钟）

```json
{
  "hpo": {
    "symbols": ["TSLA", "AAPL"],
    "start": "2025-10-01 09:30:00",
    "end": "2025-11-08 16:00:00",
    "model_task": "reg",
    "model_space_template": "mixed_small",
    "fold_generator": {
      "is_days": 20,
      "oos_days": 5,
      "step_days": 5
    },
    "objective": {
      "metric": "sharpe",
      "min_trades": 5
    }
  },
  "optuna": {"trials": 30}
}
```

**特点**: 30 trials × 混合模型 ≈ 10-15分钟

### 场景3: 全面扫描（30-60分钟）

```json
{
  "hpo": {
    "symbols": ["TSLA", "NVDA", "AAPL"],
    "start": "2025-09-15 09:30:00",
    "end": "2025-11-08 16:00:00",
    "model_task": "reg",
    "model_space_template": "full_exploration",
    "fold_generator": {
      "is_days": 30,
      "oos_days": 7,
      "step_days": 7,
      "embargo_days": 2
    },
    "objective": {
      "metric": "sharpe",
      "min_trades": 10,
      "max_mdd": 0.3
    }
  },
  "optuna": {"trials": 60}
}
```

**特点**: 60 trials × 全模型 × 3个股票 ≈ 30-60分钟

### 场景4: 方向分类（15分钟）

```json
{
  "hpo": {
    "symbols": ["TSLA"],
    "start": "2025-10-01 09:30:00",
    "end": "2025-11-08 16:00:00",
    "model_task": "clf",
    "model_horizon_bars": 15,
    "model_space_template": "linear_clf_basic",
    "objective": {
      "metric": "winrate",
      "min_trades": 10
    }
  },
  "optuna": {"trials": 15}
}
```

**特点**: 分类任务 + 优化胜率

---

## 四、模板选择速查表

| 你的情况 | 推荐模板 | 原因 | trials建议 |
|---------|---------|------|-----------|
| 快速验证思路 | `linear_reg_basic` | 训练快，稳定 | 8-15 |
| 方向预测 | `linear_clf_basic` | 分类器，胜率优化 | 10-15 |
| 不确定用哪种模型 | `mixed_small` | 线性+树+KNN混合 | 20-40 |
| 追求非线性捕捉 | `tree_ensemble` | XGB + LightGBM | 15-30 |
| 全面探索阶段 | `full_exploration` | 所有模型类型 | 40-60 |
| 只用 XGBoost | `xgb_reg_basic` | 单模型深度搜索 | 15-30 |
| 只用 LightGBM | `lgbm_reg_basic` | 大数据集优化 | 10-20 |
| 时序局部模式 | `knn_basic` | KNN记忆效应 | 10-15 |

---

## 五、常见问题 Q&A

### Q1: 配置中不写 `model_space_template` 会怎样？

**A**: 系统会跳过模板应用，你需要手动提供完整的 `optuna_space`。老配置完全兼容。

### Q2: 既想用模板又想自定义部分参数？

**A**: 暂不支持。建议：
- 方案1: 直接手动写完整 `optuna_space` (模板代码可作参考)
- 方案2: Fork 一个新模板到 `MODEL_SPACE_TEMPLATES`

### Q3: 如何查看某个模板的具体内容？

```bash
venv/bin/python -c "
import json
from wf_hpo import MODEL_SPACE_TEMPLATES
print(json.dumps(MODEL_SPACE_TEMPLATES['mixed_small'], indent=2))
"
```

### Q4: trials 设置多少合适？

**规则**: 约为 参数变体数 × 3~10 倍

| 模板 | 变体数 | 建议 trials | 预估时间 |
|------|--------|------------|---------|
| `linear_reg_basic` | 8 | 10-20 | 2-5min |
| `mixed_small` | 9 | 20-40 | 5-15min |
| `full_exploration` | 12 | 40-60 | 20-40min |

### Q5: 内存不足怎么办？

**解决方案**:
1. 减少 `trials` (≤ 20)
2. 使用轻量模板 (`linear_reg_basic`, `knn_basic`)
3. 减少 symbols (单个股票)
4. 缩短时间范围
5. 降低 XGB `max_depth` (≤ 4)

### Q6: 如何让AI大脑生成配置？

**方法1: 手动提示词**
- 复制 `prompts/ml_hpo_brain_prompt.md` 到 ChatGPT/Claude
- 描述你的需求
- 获得 JSON 配置

**方法2: 使用 API (未来)**
- 调用 OpenAI/Anthropic API
- 自动生成配置
- 直接运行 HPO

### Q7: 模板搜索空间太小/太大？

**太小** (不够覆盖):
- 增加 `trials`
- 切换到更大的模板 (`full_exploration`)
- 手动扩展 `optuna_space`

**太大** (搜索时间长):
- 减少 `trials`
- 切换到更小的模板 (`linear_reg_basic`)
- 减少 symbols 或时间范围

---

## 六、调试技巧

### 1. 确认模板是否应用

```bash
# 查看日志中的模板应用信息
grep "model_space_template" /tmp/your_output_dir/*.log

# 应该看到：
# [HPO] Using model_space_template='mixed_small' to populate optuna_space
# [HPO]   - model_type choices: [...]
# [HPO]   - model_params variants: 9
```

### 2. 查看实际搜索的模型

```bash
# 从 Optuna 数据库查询
sqlite3 alpha_optuna_*.db "
  SELECT
    trial_id,
    JSON_EXTRACT(params, '$.model_type') as model,
    value as sharpe
  FROM trials
  ORDER BY value DESC
  LIMIT 10;
"
```

### 3. 验证配置是否合法

```bash
venv/bin/python -c "
import json
cfg = json.load(open('your_config.json'))

# 必需字段检查
required = ['symbols', 'start', 'end', 'model_task', 'objective']
for key in required:
    assert key in cfg.get('hpo', {}), f'Missing hpo.{key}'

# 模板检查
from wf_hpo import MODEL_SPACE_TEMPLATES
tpl_name = cfg['hpo'].get('model_space_template')
if tpl_name:
    assert tpl_name in MODEL_SPACE_TEMPLATES, f'Unknown template: {tpl_name}'
    print(f'✅ Template {tpl_name} exists')

print('✅ Config is valid')
"
```

### 4. 测试单个模型

```bash
# 强制使用单一模型类型（调试用）
cat > debug_single.json << 'EOF'
{
  "hpo": {
    "symbols": ["TSLA"],
    "start": "2025-10-15 09:30:00",
    "end": "2025-11-08 16:00:00",
    "model_task": "reg",
    "optuna_space": {
      "model_type": {
        "type": "categorical",
        "choices": ["lasso"]
      },
      "model_params": {
        "type": "categorical",
        "choices": [{"alpha": 0.01}]
      }
    },
    "objective": {"metric": "sharpe"}
  },
  "optuna": {"trials": 1}
}
EOF

venv/bin/python alpha_loop.py --config debug_single.json --provider optuna --gens 1
```

---

## 七、性能优化建议

### 1. 并行执行 (多核加速)

```json
{
  "hpo": {
    "parallel": {
      "enabled": true,
      "workers": 4
    }
  }
}
```

**注意**: M1 MacBook 建议 `workers ≤ 4`

### 2. 减少数据加载

```json
{
  "hpo": {
    "fold_generator": {
      "is_days": 15,    // 更短的训练窗口
      "step_days": 10   // 更大的滚动步长 (更少fold)
    }
  }
}
```

### 3. 使用 Optuna Pruning

```json
{
  "optuna": {
    "pruner": "median",
    "n_startup_trials": 5,
    "n_warmup_steps": 3
  }
}
```

**效果**: 提前终止明显差的 trials

---

## 八、下一步学习

### 1. 深入理解模板设计

**阅读**: `wf_hpo.py` lines 103-270 (`MODEL_SPACE_TEMPLATES`)

**思考**:
- 为什么用 categorical 而非 loguniform?
- 参数组合如何设计才合理?
- 如何平衡搜索空间和资源?

### 2. 自定义模板

**在 `wf_hpo.py` 中添加**:
```python
MODEL_SPACE_TEMPLATES["my_custom"] = {
    "model_type": {
        "type": "categorical",
        "choices": ["xgb", "lasso"]
    },
    "model_params": {
        "type": "categorical",
        "choices": [
            {"n_estimators": 100, "max_depth": 3},
            {"alpha": 0.01}
        ]
    }
}
```

### 3. AI 大脑闭环 (高级)

**目标**: AI自动分析结果并生成下一轮配置

**步骤**:
1. HPO 运行完成
2. 解析 `index.json` 结果
3. 生成分析报告 (最佳模型、OOS指标、建议)
4. LLM 根据报告生成新配置
5. 自动运行下一轮

**参考**: `brain.py` + `plan_and_run_hpo()` (待实现)

---

## 九、故障排查

| 症状 | 可能原因 | 解决方案 |
|------|---------|---------|
| `unknown model_space_template` | 模板名拼写错误 | 检查大小写和下划线 |
| 所有 trials 返回负数 Sharpe | 模型过拟合/数据质量差 | 降低模型复杂度，检查特征 |
| 内存溢出 (OOM) | trials太多/数据太大 | 减少trials、缩短时间范围 |
| 搜索时间过长 | 模板太大/单次训练慢 | 换小模板或减少trials |
| 找不到 `optuna_space` | 模板未应用 | 确认alpha_loop.py已集成 |
| 配置JSON解析失败 | 格式错误 | 用 `jq . config.json` 验证 |

---

## 十、最终检查清单

使用配置前，确保：

- [ ] `hpo.symbols` 不为空
- [ ] `hpo.start` < `hpo.end`
- [ ] `hpo.model_task` 是 `"reg"` 或 `"clf"`
- [ ] `hpo.model_space_template` 存在于8个模板中
- [ ] `optuna.trials` 在 1-100 之间
- [ ] `hpo.objective.metric` 有效 (sharpe/sortino/calmar/winrate)
- [ ] JSON 格式正确（无注释、无尾随逗号）
- [ ] 如有 `fold_generator`，所有字段都有值

---

## 快速命令速查卡

```bash
# 1. 列出所有模板
venv/bin/python -c "from wf_hpo import MODEL_SPACE_TEMPLATES; print(list(MODEL_SPACE_TEMPLATES.keys()))"

# 2. 查看模板详情
venv/bin/python -c "import json; from wf_hpo import MODEL_SPACE_TEMPLATES; print(json.dumps(MODEL_SPACE_TEMPLATES['mixed_small'], indent=2))"

# 3. 运行快速测试
venv/bin/python alpha_loop.py --config alpha_config_template_test.json --provider optuna --gens 1

# 4. 查看结果
cat /tmp/alpha_out/index.json | jq '{best_model: .best_params.model_type, oos_sharpe: .oos_summary.sharpe}'

# 5. 查看排行榜
head -10 /tmp/alpha_out/leaderboard_is.csv | column -t -s,

# 6. 观察实时日志
tail -f /tmp/alpha_out/*.log | grep -E "Trial|model_type|Sharpe"
```

---

**准备好使用了！从最简单的测试开始，逐步探索更复杂的场景。**

**下一步**: 阅读 `prompts/ml_hpo_brain_prompt.md`，理解AI大脑的完整能力。
