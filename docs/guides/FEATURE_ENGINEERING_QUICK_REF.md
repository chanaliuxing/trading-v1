# Feature Engineering Agent - 快速参考

**快速访问**: 收藏此页面用于日常工作流

---

## ⚡ 快速开始 (3步)

```bash
# 1. 生成AI建议
venv/bin/python scripts/test_feature_engineering_agent.py

# 2. 预览变更
venv/bin/python scripts/apply_feature_engineering_suggestion.py --dry-run

# 3. 应用建议
venv/bin/python scripts/apply_feature_engineering_suggestion.py
```

---

## 📋 常用命令

### 基本操作

```bash
# 测试Feature Engineering Agent
venv/bin/python scripts/test_feature_engineering_agent.py

# Dry run (只预览,不修改)
venv/bin/python scripts/apply_feature_engineering_suggestion.py --dry-run

# 实际应用 (会自动备份)
venv/bin/python scripts/apply_feature_engineering_suggestion.py

# 应用指定建议文件
venv/bin/python scripts/apply_feature_engineering_suggestion.py \
  --suggestion runs/ai_trader/2025-11-12/feature_engineering_suggestion_143052.json
```

### 验证与回滚

```bash
# 验证JSON语法
python -m json.tool feature_programs.json > /dev/null
python -m json.tool alpha_config_enhanced.json > /dev/null

# 回滚到备份
cp feature_programs_backup_20251112_143052.json feature_programs.json
cp alpha_config_enhanced_backup_20251112_143052.json alpha_config_enhanced.json

# 运行HPO验证新特征
venv/bin/python alpha_loop.py --config alpha_config_enhanced.json --gens 1
```

---

## 🔧 可用算子速查

### 基础比率
- `ratio`: num / den
- `ratio_minus_1`: (num / den) - 1

### 标准变换
- `log1p`, `abs`, `clip`, `power`, `sign`

### 时间序列滚动
- `rolling_sum`, `rolling_mean`, `rolling_std`
- `rolling_max`, `rolling_min`, `rolling_zscore`

### 滞后/差分
- `lag`, `diff`, `ema`

### 交互/微观结构
- `product`, `sum`
- `price_impact_hl`, `effective_spread`, `amihud_illiquidity`

---

## 📤 输出格式速查

```json
{
  "analysis": "AI的分析...",
  "feature_programs_patch": {
    "add_or_update": {
      "prog_id": {
        "description": "...",
        "steps": [
          {
            "name": "feat_xxx",
            "op": "rolling_sum",
            "col": "ret_1",
            "window_bars": 10,
            "per_symbol": true
          }
        ]
      }
    },
    "delete": ["prog_id_to_delete"]
  },
  "alpha_hpo_patch": {
    "feature_program_id_choices": [...],
    "feature_subset_choices": [[...]],
    "model_scale_mode_choices": [...]
  }
}
```

---

## 🐛 快速故障排除

| 问题 | 解决方法 |
|------|----------|
| LLM超时 | 已设置300s,如仍超时→减少trials数量 |
| 建议了不存在的算子 | 更新`FEATURE_ENGINEERING_PROMPT`与`feature_dsl.py`同步 |
| 特征名冲突 | 人工修改建议中的`name`字段 |
| JSON语法错误 | `python -m json.tool <file>` |

---

## 📂 关键文件位置

| 文件 | 路径 |
|------|------|
| AI Brain核心 | `scripts/ai_trader/brain/brain.py` |
| 特征DSL引擎 | `libs/features/feature_dsl.py` |
| 特征程序配置 | `feature_programs.json` |
| HPO配置 | `alpha_config_enhanced.json` |
| 测试脚本 | `scripts/test_feature_engineering_agent.py` |
| 应用脚本 | `scripts/apply_feature_engineering_suggestion.py` |
| 使用指南 | `FEATURE_ENGINEERING_AGENT_GUIDE.md` |

---

## 💡 最佳实践速记

1. ✅ **渐进式修改**: 每次只添加1-2个新程序
2. ✅ **保留baseline**: 始终保留`none`和`baseline_v1`
3. ✅ **OOS优先**: 只相信OOS指标
4. ✅ **版本控制**: 每次修改后git commit
5. ✅ **监控过拟合**: IS vs OOS Sharpe差距>0.5警惕

---

## 📞 帮助

详细文档: `FEATURE_ENGINEERING_AGENT_GUIDE.md`

---

**最后更新**: 2025-11-12
