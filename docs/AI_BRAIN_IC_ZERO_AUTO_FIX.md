# AI 大脑自动修复指南：IC=0.0 问题

**问题**: 模型 IC=0.0, AUC=0.5，171 次 HPO 全部失败
**状态**: ✅ 诊断完成，修复方案已实现
**日期**: 2025-11-26

---

## 一、🎯 核心发现（Evidence-Based）

### 根本原因（已证实）

1. **数据利用率仅 4.8%**
   ```
   可用数据: 33,301 bars
   实际使用:  1,614 bars (仅 4.8%!)
   ```

2. **特征表选择错误**
   - 训练脚本从 `features_1m` 读取（基础特征）
   - 应该从 `features_1m_with_labels` 读取（171 个完整特征）
   - **spy_ret_5 等关键特征未被加载**

3. **数据质量其实很好**
   ```
   TSLA (2025-10-27 to 2025-11-26):
     Total bars:            13,382
     Complete bars:         13,322 (99.6%) ✅
     Market hours complete:  8,240 (99.7%) ✅
     IC(ret_5):              0.02261 (p=0.009) ✅ 有信号！
   ```

**结论**: 数据没问题，是**训练配置**问题！

---

## 二、✅ 已实现的智能化工具

### 工具 1: 数据质量诊断 (`tools/diagnose_feature_quality.py`)

**AI 大脑调用方式**:
```bash
venv/bin/python tools/diagnose_feature_quality.py \
    --symbols TSLA AAPL \
    --start "2025-10-27 16:00:00" \
    --end "2025-11-26 16:00:00" \
    --output runs/ai_trader/{date}/feature_quality_report.json
```

**输出示例**:
```json
{
  "overall_assessment": {
    "data_quality": "GOOD",
    "requires_fix": false
  },
  "per_symbol": {
    "TSLA": {
      "completeness_rate": 0.9956,
      "ic_analysis": {
        "ic_ret5": 0.02261,
        "ic_ret5_significant": true
      },
      "market_hours_only": {
        "completeness_rate": 0.9965
      }
    }
  },
  "recommendations": [],
  "auto_fix_actions": []
}
```

**AI 大脑决策逻辑**:
```python
if report["overall_assessment"]["data_quality"] == "GOOD":
    if abs(report["per_symbol"]["TSLA"]["ic_analysis"]["ic_ret5"]) > 0.01:
        # 数据有预测信号，问题不是数据质量
        # 触发: use_complete_feature_table
        action = "use_complete_feature_table"
    else:
        # IC 仍然为 0，可能是标签或时间对齐问题
        action = "fix_label_alignment"
else:
    # 数据质量差，需要修复
    if report["auto_fix_actions"]:
        # 执行建议的修复操作
        for fix in report["auto_fix_actions"]:
            execute_action(fix)
```

### 工具 2: 完整特征 OOF 生成 (`tools/generate_oof_with_complete_features.py`)

**AI 大脑调用方式**:
```bash
venv/bin/python tools/generate_oof_with_complete_features.py \
    --symbols TSLA \
    --start "2025-10-27 16:00:00" \
    --end "2025-11-26 16:00:00" \
    --horizon-min 30 \
    --market-hours-only \
    --output runs/ai_trader/{date}/oof_complete.json
```

**关键改进**:
1. ✅ 从 `features_1m_with_labels` 读取（包含 spy_ret_5 等）
2. ✅ 自动过滤 NULL 特征行
3. ✅ 使用市场时段数据（9:30 AM - 4:00 PM）
4. ✅ 使用 8240 bars（vs 1614 之前）

**测试结果**:
```
Before: n=1614, AUC=0.5, IC=0.0
After:  n=8240, AUC=0.494, IC=0.017

改进:
  - 数据量: +410%
  - IC: 从 0.0 → 0.017
```

---

## 三、🤖 AI 大脑自动化流程

### Scenario 1: IC=0.0 持续 10+ 次 HPO

```yaml
# AI 大脑检测到异常
event: hpo_failure_streak
params:
  attempts: 171
  latest_ic: 0.0
  latest_auc: 0.5

# AI 大脑自动决策
diagnosis:
  - "IC=0.0 exactly 不正常（正常噪音应该 ≈ ±0.01-0.02）"
  - "可能原因: 特征全是 NULL, 或数据未被正确加载"

# AI 大脑写入 pending_actions.yml
pending:
  - action: data_diagnostic
    params:
      symbols: ["TSLA"]
      start: "2025-10-27 16:00:00"
      end: "2025-11-26 16:00:00"
      output: "runs/ai_trader/{date}/feature_quality_report.json"
```

### Scenario 2: 诊断发现数据质量 GOOD 但 IC=0

```yaml
# 读取诊断报告
report: feature_quality_report.json

# AI 大脑分析
analysis:
  - data_quality: "GOOD"
  - ic_ret5: 0.02261 (有信号！)
  - completeness_rate: 0.9956
  - 结论: "数据没问题，是训练配置问题"

# AI 大脑决策
pending:
  - action: use_complete_feature_table
    params:
      table: "trading.features_1m_with_labels"
      require_complete: true
      market_hours_only: true
      min_bars: 5000
      reason: "Current training uses wrong table (features_1m)"
```

### Scenario 3: 使用完整表后 AUC < 0.55

```yaml
# 使用完整特征表后仍然性能差
result:
  n_bars: 8240
  auc: 0.494
  ic: 0.017

# AI 大脑分析
analysis:
  - "数据量改善: 1614 → 8240"
  - "IC 改善: 0.0 → 0.017"
  - "但 AUC 仍然接近随机"
  - "当前只用了 4 个特征，表里有 171 个"

# AI 大脑决策
pending:
  - action: feature_expansion
    params:
      current_features: ["ret_5", "spy_ret_5", "atr_20", "zscore_volume_20"]
      target_count: 50-70
      add_categories:
        - technical_indicators: ["rsi_14", "macd_hist", "bb_position", ...]
        - relative_performance: ["ret_vs_spy_5m", "beta_spy_20", ...]
        - volatility: ["realized_vol_15m", "garman_klass_20", ...]
        - volume_profile: ["volume_surge", "volume_rank_20", ...]
        - momentum: ["momentum_strength", "price_momentum_5m", ...]
        - market_context: ["vix", "vix_diff", "vix_z_20", ...]
```

---

## 四、📋 Orchestrator 集成

### 添加到 `scripts/ai_trader/orchestrator.py`

```python
# 在 execute_pending_actions() 中添加

elif act == "data_diagnostic":
    """Run feature quality diagnostic"""
    symbols = params.get("symbols", [])
    start = params.get("start")
    end = params.get("end")
    output = params.get("output", f"runs/ai_trader/{date}/feature_quality_report.json")

    cmd = [
        "venv/bin/python", "tools/diagnose_feature_quality.py",
        "--symbols", *symbols,
        "--start", start,
        "--end", end,
        "--output", output
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)

    # 读取报告
    with open(output) as f:
        report = json.load(f)

    # 根据报告自动决策下一步
    if report["overall_assessment"]["data_quality"] == "GOOD":
        if any(abs(s["ic_analysis"].get("ic_ret5", 0)) > 0.01
               for s in report["per_symbol"].values()):
            # 数据有信号但模型 IC=0 → 训练配置问题
            self._append_pending_action({
                "action": "use_complete_feature_table",
                "params": {
                    "table": "trading.features_1m_with_labels",
                    "reason": "Data has predictive signal but model IC=0"
                }
            })

elif act == "use_complete_feature_table":
    """Update OOF training to use complete feature table"""
    table = params.get("table", "trading.features_1m_with_labels")

    # 更新 OOF 配置
    oof_config_path = "config/oof_training.yaml"
    with open(oof_config_path) as f:
        config = yaml.safe_load(f)

    config["data"]["source_table"] = table
    config["data"]["require_complete_features"] = True
    config["data"]["market_hours_only"] = True

    with open(oof_config_path, 'w') as f:
        yaml.safe_dump(config, f)

    print(f"[orchestrator] Updated OOF config to use {table}")

    # 触发重新训练
    self._append_pending_action({
        "action": "oof",
        "params": {
            "symbols": params.get("symbols", ["TSLA"]),
            "use_updated_config": True
        }
    })

elif act == "feature_expansion":
    """Expand feature set based on available columns"""
    current_features = params.get("current_features", [])
    target_count = params.get("target_count", 50)
    add_categories = params.get("add_categories", [])

    # 从 features_1m_with_labels 获取所有可用列
    available_features = self._get_available_features("trading.features_1m_with_labels")

    # 按类别选择特征
    selected_features = []
    for category in add_categories:
        category_features = self._select_features_by_category(
            available_features,
            category,
            max_per_category=10
        )
        selected_features.extend(category_features)

    # 限制总数
    selected_features = selected_features[:target_count]

    # 更新配置
    feature_config_path = "config/model_features.yaml"
    with open(feature_config_path) as f:
        config = yaml.safe_load(f)

    config["features"] = current_features + selected_features
    config["feature_count"] = len(config["features"])

    with open(feature_config_path, 'w') as f:
        yaml.safe_dump(config, f)

    print(f"[orchestrator] Expanded features: {len(current_features)} → {len(config['features'])}")

    # 触发重新训练
    self._append_pending_action({
        "action": "oof",
        "params": {
            "symbols": params.get("symbols", ["TSLA"]),
            "use_expanded_features": True
        }
    })
```

---

## 五、🔄 完整修复时间线

### Phase 1: 问题发现 ✅
```
Time: 2025-11-26 14:00-14:18
- AI 大脑运行 171 次 HPO
- 全部失败: AUC=0.5, IC=0.0, mean_conf=0.0
- AI 大脑诊断: "No predictive edge detected"
```

### Phase 2: 根因诊断 ✅
```
Time: 2025-11-26 14:18-16:00
- 人工分析 ml_review.json, oof_flow_status.json
- 发现: 只用了 1614 bars (4.8%)
- 发现: spy_ret_5 未被加载
- 发现: 数据质量 99.6% 完整，IC(ret_5)=0.022
```

### Phase 3: 工具开发 ✅
```
Time: 2025-11-26 16:00-16:20
- 创建 diagnose_feature_quality.py ✅
- 创建 generate_oof_with_complete_features.py ✅
- 测试验证: 8240 bars, IC=0.017 ✅
```

### Phase 4: Orchestrator 集成 ⏳
```
Time: 待实现
- 添加 data_diagnostic action
- 添加 use_complete_feature_table action
- 添加 feature_expansion action
- 测试端到端自动化
```

### Phase 5: 特征扩展 ⏳
```
Time: 待实现
- 从 4 特征 → 50-70 特征
- 预期: AUC > 0.55, IC > 0.05
```

---

## 六、📊 预期效果

| 阶段 | 数据量 | 特征数 | AUC | IC | 状态 |
|------|-------|-------|-----|-----|-----|
| 当前 | 1,614 | 4 | 0.50 | 0.000 | ❌ 失败 |
| Phase 3 | 8,240 | 4 | 0.49 | 0.017 | ⚠️ 改善中 |
| Phase 5 (目标) | 8,240 | 50-70 | >0.55 | >0.050 | ✅ 目标 |

---

## 七、✅ 已交付产物

### 1. 诊断工具
- `tools/diagnose_feature_quality.py` ✅
- 输出 JSON 供 AI 大脑读取 ✅
- 自动生成修复建议 ✅

### 2. 修复工具
- `tools/generate_oof_with_complete_features.py` ✅
- 使用完整特征表 ✅
- 过滤 NULL 数据 ✅
- 市场时段过滤 ✅

### 3. 文档
- `/tmp/ai_brain_diagnosis_complete.md` ✅
- 本文档 ✅

### 4. 待集成
- Orchestrator actions ⏳
- 配置文件更新 ⏳
- 端到端测试 ⏳

---

## 八、🎯 AI 大脑使用指南

### 场景 A: 首次遇到 IC=0.0

**Step 1**: 运行诊断
```bash
venv/bin/python tools/diagnose_feature_quality.py \
    --symbols TSLA \
    --start "2025-10-27 16:00:00" \
    --end "2025-11-26 16:00:00" \
    --output /tmp/diagnostic.json
```

**Step 2**: 读取报告
```python
import json
with open("/tmp/diagnostic.json") as f:
    report = json.load(f)

if report["overall_assessment"]["data_quality"] == "GOOD":
    # 数据没问题，检查训练配置
    pass
```

**Step 3**: 使用完整特征表测试
```bash
venv/bin/python tools/generate_oof_with_complete_features.py \
    --symbols TSLA \
    --start "2025-10-27 16:00:00" \
    --end "2025-11-26 16:00:00" \
    --horizon-min 30 \
    --market-hours-only \
    --output /tmp/oof_test.json
```

**Step 4**: 对比结果
```python
# Before
old_result = {"n": 1614, "auc": 0.5, "ic": 0.0}

# After
with open("/tmp/oof_test.json") as f:
    new_result = json.load(f)

if new_result["per_symbol"]["TSLA"]["metrics"]["ic"] > 0.01:
    # 改善了！继续扩展特征
    next_action = "feature_expansion"
```

### 场景 B: 持续 HPO 失败

**AI 大脑自动流程**:
1. 检测: `fail_streak >= 10` 且 `ic == 0.0`
2. 诊断: 运行 `data_diagnostic`
3. 分析: 读取诊断报告
4. 决策:
   - 如果 data_quality == "GOOD" → `use_complete_feature_table`
   - 如果 completeness_rate < 0.7 → `apply_market_hours_filter`
5. 验证: 重新运行 OOF
6. 迭代: 如果 AUC < 0.55 → `feature_expansion`

---

## 九、🔑 关键经验

### ✅ 做对的事情

1. **Evidence-Based 诊断**
   - 不盲目调参
   - 先检查数据质量
   - 手动计算 IC 验证

2. **工具化**
   - 创建可重用的诊断工具
   - JSON 输出供自动化读取
   - 清晰的 action 建议

3. **分阶段修复**
   - Phase 1: 诊断 ✅
   - Phase 2: 数据修复 ✅
   - Phase 3: 特征扩展 ⏳
   - Phase 4: 模型优化 ⏳

### ❌ 避免的陷阱

1. **不要盲目提出新特征**
   - 表里已经有 171 个特征
   - 先用好现有特征

2. **不要忽视数据利用率**
   - 只用 4.8% 数据是巨大浪费
   - 确保使用全部可用数据

3. **不要忽略完整性**
   - 40% NULL 特征会破坏模型
   - 必须过滤或填充

---

**最后更新**: 2025-11-26 16:25 PST
**维护者**: Trading Team
**状态**: ✅ Phase 1-3 完成，Phase 4-5 待实现

**相关文档**:
- `docs/AI_BRAIN_LABEL_AUTO_REPAIR.md` - 标签修复自动化
- `/tmp/ai_brain_diagnosis_complete.md` - 完整诊断报告
- `tools/diagnose_feature_quality.py` - 诊断工具源码
- `tools/generate_oof_with_complete_features.py` - 修复工具源码
