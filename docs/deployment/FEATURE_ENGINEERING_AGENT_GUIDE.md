# Feature Engineering Agent 使用指南

**创建时间**: 2025-11-12
**版本**: 1.0
**状态**: ✅ 已实现并可用

---

## 📋 概述

Feature Engineering Agent 是AI Brain的一个新功能,能够根据HPO/回测结果智能地修改特征工程配置,而不是让AI直接写Python代码。

### 核心理念

1. **AI只修改JSON配置** (`feature_programs.json`)
2. **Python执行预定义的安全算子** (`libs/features/feature_dsl.py`)
3. **训练/回测/实盘使用完全相同的逻辑** (无代码分歧)

---

## 🎯 功能特性

### 1. 智能特征程序设计
- 分析HPO试验结果,识别高性能和低性能的特征组合
- 基于数据驱动的证据提出新的特征程序
- 自动平衡特征复杂度(建议6-10步)

### 2. 因果性保证
- 只允许"向后看"的算子(rolling, lag, diff)
- 禁止未来数据泄漏
- 严格的算子白名单控制

### 3. HPO空间优化
- 推荐新的`feature_program_id`候选
- 生成优化的`feature_subset`组合
- 建议合适的`model_scale_mode`

### 4. 多频扩展 (NEW)
- `tools/feature_resample.py` 可将 `trading.features_1m` 聚合为：
  - `trading.features_5m` (`m5.` 别名，5 分钟桶，join: `toStartOfInterval(f.ts, 5m)`）。
  - `trading.features_1d_prev` (`d.` 别名，上一交易日聚合，join: `toStartOfDay(f.ts)`，值在次日生效)。
- Catalog spec 中若引用 `m5.`/`d.`，需要写入 `\"required_sources\": [\"m5\", \"d\"]` 子集，系统会自动 LEFT JOIN 对应表并确保无未来泄漏。
- Prompt 也会自动告诉 Brain 可以组合 1m + 5m + 1d 特征（例如“1m micro * 前日 RVOL”）。

---

## 🛠️ 可用算子列表

### 基础比率
- `ratio`: num / den
- `ratio_minus_1`: (num / den) - 1

### 标准变换
- `log1p`: log(1 + x)
- `abs`: |x|
- `clip`: 截断到[min, max]
- `power`: x^p
- `sign`: sign(x)

### 时间序列滚动 (只向后看)
- `rolling_sum`: 滚动求和
- `rolling_mean`: 滚动均值
- `rolling_std`: 滚动标准差
- `rolling_max`: 滚动最大值
- `rolling_min`: 滚动最小值
- `rolling_zscore`: (x - 滚动均值) / 滚动标准差

### 滞后/差分
- `lag`: 向后滞后n个bar
- `diff`: n阶差分

### 平滑
- `ema`: 指数滑动均值

### 交互特征
- `product`: col1 * col2
- `sum`: col1 + col2

### 微观结构/流动性
- `price_impact_hl`: 使用high/low与前一根收盘衡量价格冲击
- `effective_spread`: (high - low) / mid
- `amihud_illiquidity`: |ret| / (volume * close)

---

## 📖 使用方法

### 方法1: 测试脚本 (推荐入门)

```bash
# 运行测试脚本
venv/bin/python scripts/test_feature_engineering_agent.py
```

这个脚本会:
1. 加载当前的`feature_programs.json`和`alpha_config_enhanced.json`
2. 使用模拟的HPO试验数据
3. 调用AI Brain获取特征工程建议
4. 保存建议到`feature_engineering_suggestion_latest.json`

### 方法2: 在代码中调用

```python
from scripts.ai_trader.brain.brain import ControlBrain
import json

# 初始化AI Brain
brain = ControlBrain(
    runs_dir="runs/ai_trader",
    provider="deepseek_nvidia",
    model="deepseek-ai/deepseek-r1"
)

# 准备上下文
context = {
    "recent_trials": [
        {
            "trial_id": 1,
            "params": {
                "feature_program_id": "baseline_v1",
                "feature_subset": ["ret_1", "atr_14", "volume"],
                "model_scale_mode": "robust"
            },
            "oos_summary": {
                "oos_avg_sharpe": 1.2,
                "oos_avg_ic": 0.08,
                "profit_factor": 1.5,
                "max_dd": -0.12
            }
        },
        # ... 更多试验数据
    ],
    "available_features": [
        "close", "high", "low", "open", "volume",
        "vwap", "ret_1", "ret_5", "atr_14",
        # ... 更多可用特征
    ],
    "current_feature_programs": { ... },  # 从feature_programs.json加载
    "current_alpha_hpo_config": { ... }   # 从alpha_config_enhanced.json提取
}

# 获取建议
suggestion = brain.suggest_feature_engineering(context)

# 查看结果
print(suggestion["analysis"])
print(suggestion["feature_programs_patch"])
print(suggestion["alpha_hpo_patch"])
```

### 方法3: 异步调用

```python
import asyncio

async def main():
    brain = ControlBrain(...)
    context = { ... }

    # 异步调用
    suggestion = await brain.suggest_feature_engineering_async(context)

    # 处理建议
    ...

asyncio.run(main())
```

---

## 📤 输出格式

AI Brain返回的建议JSON格式:

```json
{
  "analysis": "基于最近5次试验,comprehensive_v1表现最佳...",

  "feature_programs_patch": {
    "add_or_update": {
      "momentum_v2": {
        "description": "Multi-horizon momentum with volatility scaling",
        "steps": [
          {
            "name": "feat_ret_1m_roll_10",
            "op": "rolling_sum",
            "col": "ret_1",
            "window_bars": 10,
            "per_symbol": true
          },
          {
            "name": "feat_vol_20",
            "op": "rolling_std",
            "col": "ret_1",
            "window_bars": 20,
            "per_symbol": true
          },
          {
            "name": "feat_momo_vol_ratio",
            "op": "ratio",
            "num": "feat_ret_1m_roll_10",
            "den": "feat_vol_20",
            "eps": 1e-6
          }
        ]
      }
    },
    "delete": ["microstructure_v1"]
  },

  "alpha_hpo_patch": {
    "feature_program_id_choices": [
      "none",
      "baseline_v1",
      "momentum_v1",
      "momentum_v2",
      "comprehensive_v1"
    ],
    "feature_subset_choices": [
      ["ret_1", "atr_14", "volume"],
      ["ret_1", "vwap", "atr_14", "volume"],
      ["feat_ret_1m_roll_10", "feat_zscore_ret1m_50", "feat_vol_20"]
    ],
    "model_scale_mode_choices": ["zscore", "robust"]
  }
}
```

---

## 🔄 应用建议的工作流

### 步骤1: 获取建议

```bash
venv/bin/python scripts/test_feature_engineering_agent.py
```

### 步骤2: 审查建议

检查`feature_engineering_suggestion_latest.json`:
- ✅ 所有算子都在白名单中?
- ✅ 特征列名都存在于`available_features`?
- ✅ 没有未来数据泄漏?
- ✅ 特征程序复杂度合理(6-10步)?

### 步骤3: 应用建议

#### 3a. 更新feature_programs.json

```python
import json

# 加载当前配置
with open("feature_programs.json", "r") as f:
    programs = json.load(f)

# 加载建议
with open("feature_engineering_suggestion_latest.json", "r") as f:
    suggestion = json.load(f)

# 应用patch
patch = suggestion["feature_programs_patch"]

# 添加/更新程序
for prog_id, prog_def in patch.get("add_or_update", {}).items():
    programs[prog_id] = prog_def
    print(f"✓ Added/Updated: {prog_id}")

# 删除程序
for prog_id in patch.get("delete", []):
    if prog_id in programs:
        del programs[prog_id]
        print(f"✓ Deleted: {prog_id}")

# 保存
with open("feature_programs.json", "w") as f:
    json.dump(programs, f, indent=2)

print("✓ feature_programs.json updated")
```

#### 3b. 更新alpha_config_enhanced.json

```python
import json

# 加载配置
with open("alpha_config_enhanced.json", "r") as f:
    config = json.load(f)

# 加载建议
with open("feature_engineering_suggestion_latest.json", "r") as f:
    suggestion = json.load(f)

# 应用HPO patch
hpo_patch = suggestion["alpha_hpo_patch"]

grid = config["hpo"]["grid"]
grid["feature_program_id"]["values"] = hpo_patch["feature_program_id_choices"]
grid["feature_subset"]["values"] = hpo_patch["feature_subset_choices"]
grid["model_scale_mode"]["values"] = hpo_patch["model_scale_mode_choices"]

# 保存
with open("alpha_config_enhanced.json", "w") as f:
    json.dump(config, f, indent=2)

print("✓ alpha_config_enhanced.json updated")
```

### 步骤4: 运行HPO验证

```bash
venv/bin/python alpha_loop.py \
  --config alpha_config_enhanced.json \
  --provider coop_optuna \
  --gens 1 \
  --out_dir alpha_out/feature_test
```

### 步骤5: 分析结果

- 对比新特征程序 vs baseline
- 检查OOS Sharpe, IC, Profit Factor
- 验证无过拟合

---

## 🔐 安全特性

### 1. 算子白名单
- AI只能使用预定义的安全算子
- 无法执行任意Python代码

### 2. 因果性验证
- 所有rolling/lag/diff算子都强制"向后看"
- `per_symbol=true`确保不跨symbol泄漏

### 3. 列名验证
- 所有特征必须来自`available_features`或之前步骤生成
- 防止引用不存在的列

### 4. 复杂度限制
- AI被指示将程序限制在6-10步
- 避免过度工程

---

## 📊 示例场景

### 场景1: HPO发现momentum特征表现差

**AI建议**:
- 删除`momentum_v1`
- 新增`momentum_v2`,使用波动率归一化的动量
- 调整`feature_subset`排除旧动量特征

### 场景2: microstructure特征过拟合

**AI建议**:
- 保留`microstructure_v1`但简化步骤
- 增加`min_periods`防止数据稀疏
- 推荐与baseline特征混合使用

### 场景3: 需要更多时间尺度特征

**AI建议**:
- 新增`multi_horizon_v1`
- 包含1m, 5m, 15m多时间框架的ret滚动和
- 使用`rolling_zscore`标准化

---

## 🐛 故障排除

### 问题1: AI建议了不存在的算子

**原因**: Prompt可能过时
**解决**: 更新`FEATURE_ENGINEERING_PROMPT`中的算子列表,与`feature_dsl.py`保持一致

### 问题2: 生成的特征名冲突

**原因**: 程序之间有重复的`name`
**解决**: 人工审查时修改特征名,确保唯一性

### 问题3: AI建议删除关键程序

**原因**: 上下文中缺少重要信息
**解决**:
- 提供更多试验历史
- 明确说明某些程序是baseline,不可删除

### 问题4: LLM超时

**原因**: DeepSeek R1模型推理慢
**解决**:
- 已设置300s超时
- 如果仍超时,简化上下文(减少trials数量)

---

## 📁 相关文件

| 文件 | 作用 |
|------|------|
| `scripts/ai_trader/brain/brain.py` | AI Brain核心,包含`FEATURE_ENGINEERING_PROMPT` |
| `libs/features/feature_dsl.py` | 特征DSL执行引擎 |
| `feature_programs.json` | 特征程序配置 |
| `alpha_config_enhanced.json` | HPO配置(包含特征空间) |
| `scripts/test_feature_engineering_agent.py` | 测试脚本 |
| `FEATURE_ENGINEERING_AGENT_GUIDE.md` | 本文档 |

---

## 🚀 快速开始

```bash
# 1. 运行测试脚本
venv/bin/python scripts/test_feature_engineering_agent.py

# 2. 审查建议
cat feature_engineering_suggestion_latest.json

# 3. 手动应用建议 (或使用自动化脚本)
# 编辑 feature_programs.json 和 alpha_config_enhanced.json

# 4. 验证
venv/bin/python alpha_loop.py --config alpha_config_enhanced.json --gens 1
```

---

## 📝 最佳实践

1. **渐进式修改**: 每次只添加1-2个新程序,逐步验证
2. **保留baseline**: 始终保留`none`和`baseline_v1`作为对照组
3. **OOS验证**: 只相信OOS指标,IS指标仅供参考
4. **版本控制**: 每次修改后提交Git,方便回滚
5. **监控过拟合**: 对比IS vs OOS Sharpe,差距>0.5认为过拟合

---

## 🔗 参考文档

- [FEATURE_DSL_GUIDE.md](FEATURE_DSL_GUIDE.md) - 特征DSL详细文档
- [ACTION_PLAN_ML_FIX.md](ACTION_PLAN_ML_FIX.md) - ML系统修复计划
- [CLAUDE.md](CLAUDE.md) - 项目开发规范

---

**最后更新**: 2025-11-12
**维护者**: Trading Team
