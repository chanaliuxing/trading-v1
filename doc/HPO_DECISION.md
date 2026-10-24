# HPO系统使用决策 - 2025-11-05

## 决策

**使用 `alpha_loop.py` 的 `provider=optuna` 模式**

## 理由

### 1. 数学必然性
- 当前参数空间: **264,049,632,000** 组合
- Grid Search物理时间: 663年 (4核)
- Bayesian Optimization: 8.3分钟 (100 trials)
- **结论**: Grid Search不可行，必须用贝叶斯优化

### 2. 系统设计意图
- `wf_hpo.py`: 遗留系统，小规模验证用
- `hpo_cli_upgraded.py`: 独立生产工具
- `alpha_loop.py`: **推荐使用** - 集成LLM先验+贝叶斯优化

### 3. 性能证据
```
文档实测 (RevE策略, 6561组合):
- Grid Search: 182小时
- TPE + ASHA: 5.5小时
- 加速比: 33x
```

### 4. 机构级实践
- Optuna被Two Sigma, Citadel等顶级量化基金使用
- TPE算法论文引用>5000次
- 生产就绪: 支持分布式、监控、早停

## 实施步骤

### 立即执行 (修复当前bug)

```bash
# 使用纯Optuna模式
python alpha_loop.py \
  --config alpha_config_enhanced.json \
  --provider optuna \
  --gens 1 \
  --out_dir alpha_out/production
```

### 配置优化

```json
{
  "optuna": {
    "trials": 150,              // 推荐100-200
    "target": "is",             // 优化IS性能
    "direction": "maximize",
    "sampler": "tpe",           // TPE适合混合参数空间
    "study_name": "alpha_enhanced_ml_${DATE}",
    "storage": "sqlite:///alpha_optuna_enhanced.db",
    "n_startup_trials": 15,     // 前15次随机采样
    "multivariate": true        // 考虑参数相关性
  }
}
```

### 监控与调优

1. **监控收敛**:
   ```bash
   sqlite3 alpha_optuna_enhanced.db
   > SELECT number, value FROM trials ORDER BY number;
   ```

2. **参数重要度**:
   ```python
   study = optuna.load_study(...)
   importances = optuna.importance.get_param_importances(study)
   ```

3. **可视化**:
   ```python
   optuna.visualization.plot_optimization_history(study)
   optuna.visualization.plot_param_importances(study)
   ```

## 长期策略

### 保留 wf_hpo.py 用于

1. **快速验证** (参数<100组合)
2. **教学/调试** (行为可预测)
3. **回归测试** (验证Optuna结果)

### 全面迁移到 Optuna 用于

1. **生产优化** (参数>1000组合)
2. **持续改进** (可累积历史)
3. **A/B测试** (多study对比)

## 附录: 技术对比

| 特性 | wf_hpo.py | alpha_loop (optuna) | hpo_cli_upgraded.py |
|------|-----------|---------------------|---------------------|
| 适用组合数 | <1,000 | >1,000,000 | >1,000,000 |
| 学习能力 | 无 | TPE/BoTorch | TPE/BoTorch |
| LLM集成 | 无 | ✅ (coop模式) | 无 |
| 早停 | 无 | MedianPruner | ASHA/Median |
| 推荐度 | ⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |

---

**结论**: 使用 `alpha_loop.py --provider optuna` 是当前唯一可行且最优的方案。
