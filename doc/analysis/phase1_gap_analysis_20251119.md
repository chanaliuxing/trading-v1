# Phase 1: Gap Analysis Report
**Date**: 2025-11-19
**Analyst**: Claude Code
**Scope**: 验证 upgradeFeatures_finalcheck.md 中提到的系统 Gap

---

## 执行摘要（Executive Summary）

**分析方法**: 扫描关键模块源代码，验证 finalcheck 文档中提到的 Gap
**分析时间**: ~30 分钟
**结论**: **Finalcheck 诊断准确。所有主要 Gap 已验证，需要立即修复。**

---

## ✅ 已验证的 Gap（Evidence-Based）

### **Gap 1: Brain 层未接入 ClickHouse 战绩统计**

**文件**: `scripts/ai_trader/control_event_consumer.py`

**证据**:
```python
# Line 1427-1428
from scripts.ai_trader.memory.aggregator import memory_summary
mem = memory_summary("afterhours", lookback_days=14)

# Line 2558-2568
memory_summary = _summarize_actions(str(_runs_dir()), days=7)

# Line 2691
ms = memory_summary.get(str(action_type)) or {}
```

**问题**:
- ✅ 确认：仍在使用 `memory_summary`（老的文件扫描方法）
- ❌ 缺失：没有引用 `get_model_stats_summary_from_ch` 或任何 ClickHouse stats
- ❌ 影响：Brain 根据"有没有报错"而不是"模型实际战绩"做决策

**优先级**: **P0（最高）**

---

### **Gap 2: ML 推理链路断裂（OOF vs 实时推理）**

**文件**: `services/ml/real_inference_service.py`

**证据**:
```python
# Line 3-8: 注释明确说明
"""
Real ML Inference Service - Uses actual OOF predictions from ClickHouse

This service provides REAL ML predictions based on trained XGB/LSTM models.
Uses out-of-fold predictions stored in ClickHouse as the source of truth.
"""

# Line 58: 查询历史 OOF 表
query = "SELECT COUNT(*) as count FROM trading.oof_xgb_h5 LIMIT 1"

# Line 186-194: _get_xgb_prediction() 查询历史预测
query = f"""
SELECT pred
FROM trading.oof_xgb_h5
WHERE symbol = '{symbol}'
  AND ts >= ... AND ts <= ...
ORDER BY abs(dateDiff('second', ts, toDateTime(...)))
LIMIT 1
"""

# Line 114-120: 如果没找到历史预测，返回 neutral
if xgb_pred is None:
    logger.warning("no_xgb_prediction", symbol=symbol, ts=str(ts))
    xgb_pred = 0.5  # Neutral
```

**问题**:
- ✅ 确认：`RealMLInferenceService` **只是查询历史 OOF 表**
- ❌ 缺失：**没有实时推理**（不加载模型，不计算预测）
- ❌ 影响：策略拿到的是"过去某次训练的回测结果"或 neutral，对实时决策无意义

**正确的服务**: `ModelMLInferenceService` (services/ml/model_inference_service.py)
```python
# Line 5-6: 正确的实时推理服务
"""
加载训练好的 XGBoost/LSTM 模型进行实时推理
替代 RealMLInferenceService (基于 OOF 查询)
"""

# Line 48-53: 加载模型文件
model_path = self.model_dir / "model.json"
self.model = xgb.Booster()
self.model.load_model(str(model_path))
```

**当前状态**:
- ✅ `enhanced_model_signal_strategy.py` 在使用正确的 `get_model_service()`（Line 36, 692）
- ⚠️ 未知：是否所有实盘策略都在用正确的服务？

**优先级**: **P0（最高）**

---

### **Gap 3: 特征计算可能存在冗余路径**

**潜在冗余路径**:
1. **FeatureCalculator**: 写入 ClickHouse (1m/5m/15m/1h 特征)
2. **RealtimeFeatureEngine (RFE)**: 通过 Redis 实时计算 (1s/5s 微结构)
3. **ModelMLInferenceService 内部**: 可能再次计算某些特征

**状态**: **需要进一步扫描确认**（Gap 3 尚未完全验证）

**优先级**: **P1**

---

### **Gap 4: Mock/LSTM 幽灵代码风险**

**文件**: `services/ml/mock_inference_service.py`（存在）

**证据**:
```python
# real_inference_service.py: Line 211-237
def _get_lstm_prediction(self, symbol: str, ts: datetime):
    """
    Get LSTM prediction from OOF table.

    Note: LSTM predictions are sparse, may not be available for all timestamps.
    """
    query = f"""
    SELECT pred
    FROM trading.oof_lstm_h5
    ...
    """
```

**问题**:
- ⚠️ LSTM 推理路径存在，但可能没有实时写入
- ⚠️ MockInferenceService 存在，可能被误用于生产环境

**优先级**: **P2**

---

## 📊 系统现状总结

### ✅ **已实现的核心能力**（来自之前测试）

- **ML 基础设施**: PI, Label/Feature Registry, DatasetSanity, Experiment Registry
- **测试覆盖**: 62 test files, ~730 passing (98.6% pass rate)
- **Brain 能力**: 10+ strategic planning methods
- **正确的推理服务**: `ModelMLInferenceService` 已实现

### ❌ **关键集成缺失**（来自本次分析）

- **Brain 层**: 没有接入 ClickHouse 模型战绩统计
- **推理层**: 可能有策略仍在使用 OOF 查询服务而非实时推理
- **监控层**: 缺少对"哪个策略用哪个 inference service"的全局视图

---

## 🎯 下一步行动计划

### **P0 - 立即修复（阻塞实盘安全）**

#### 1. **修复 Brain 层：接入 ClickHouse 战绩统计**

**目标**: 让 Brain 根据模型真实战绩（Sharpe/IC/win_rate）做决策

**步骤**:
1. 实现/确认 `get_model_stats_summary_from_ch()`
2. 在 `control_event_consumer.py` 中替换 `memory_summary` 调用
3. 添加 gating 逻辑：
   ```python
   stats = get_model_stats_summary_from_ch(symbol=..., strategy_id=...)
   if stats["sharpe_30d"] < 0.5 or stats["ic_30d"] < 0.02:
       action.mode = "DISABLE"
       action.reason = "model_performance_bad"
   ```

**负责人**: [待分配]
**预计时间**: 4-6 小时

---

#### 2. **审计并修复推理服务使用**

**目标**: 确保所有实盘策略使用 `ModelMLInferenceService`

**步骤**:
1. 扫描所有策略文件，列出 inference service 使用情况
2. 生成清单：
   | 策略名 | 当前 Service | 使用场景 | 是否需要修复 |
   |--------|-------------|----------|-------------|
   | enhanced_model_signal | get_model_service | 实盘 | ✅ 正确 |
   | ... | ... | ... | ... |
3. 修复所有使用 `RealMLInferenceService` 的实盘策略
4. 在生产配置中禁用 `RealMLInferenceService` / `MockInferenceService`

**负责人**: [待分配]
**预计时间**: 6-8 小时

---

### **P1 - 系统加固（提升可靠性）**

#### 3. **特征管线去重**
- 列出所有特征计算路径
- 明确"权威源"（训练用 CH，实盘用 RFE）
- 关停冗余计算

#### 4. **DatasetSanity 全链路接通**
- 确保所有训练入口都经过 DatasetSanity
- 添加硬错误（拒绝全 NaN/单值数据集）

#### 5. **ExperimentRegistry 接通实时推理**
- 实现"选冠军 run"函数
- StrategyManifest 绑定 experiment_id
- 推理服务从 manifest 加载模型

---

### **P2 - 技术债清理**

#### 6. **清理幽灵代码**
- LSTM 推理路径标记为 experimental 或下线
- MockInferenceService 添加生产环境禁用检查

#### 7. **Fail Fast 硬化**
- 去掉 silent fallback
- 添加 config_sanity_check.py

---

## 📈 成功指标

修复完成后，系统应该满足：

1. ✅ Brain 决策基于 ClickHouse 真实战绩，不基于文件扫描
2. ✅ 所有实盘策略使用实时推理服务（ModelMLInferenceService）
3. ✅ 生产环境无法启动 Mock/OOF 服务
4. ✅ 所有训练入口都经过 DatasetSanity
5. ✅ 特征计算无重复路径

---

## 附录：关键文件路径

| 模块 | 文件路径 | 状态 |
|------|---------|------|
| Brain Consumer | `scripts/ai_trader/control_event_consumer.py` | ❌ 需修复 |
| OOF Service | `services/ml/real_inference_service.py` | ⚠️ 应禁用于实盘 |
| Model Service | `services/ml/model_inference_service.py` | ✅ 正确实现 |
| Mock Service | `services/ml/mock_inference_service.py` | ⚠️ 需添加生产禁用 |
| Enhanced Strategy | `strategies/enhanced_model_signal_strategy.py` | ✅ 使用正确服务 |

---

**报告生成时间**: 2025-11-19 03:20
**下次更新**: Phase 2 开始后
