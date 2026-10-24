# wf_hpo.py 重构补丁说明

## 目标
确保 IS 和 OOS 使用完全相同的特征计算逻辑,避免训练/测试不一致

## 需要修改的地方

### 1. 在文件顶部添加导入

**位置**: wf_hpo.py 第18行附近 (import语句区域)

**添加**:
```python
# Feature DSL
from libs.features.feature_dsl import apply_feature_program
```

### 2. 删除 `_apply_feature_program` 方法

**位置**: wf_hpo.py 第586-774行

**操作**: 完全删除这个方法,因为已经迁移到 `libs/features/feature_dsl.py`

### 3. 修改 `_prepare_model_data` 中的 IS 特征计算

**位置**: wf_hpo.py 第859-862行

**原代码**:
```python
# ========== PHASE 1: Feature Engineering (DSL-based) ==========
# Apply feature program from JSON configuration
feature_program_id = params.get("feature_program_id") or self.cfg.get("feature_program_id", "comprehensive_v1")
df_is_sorted = self._apply_feature_program(df_is_sorted, feature_program_id)
```

**替换为**:
```python
# ========== PHASE 1: Feature Engineering (DSL-based) ==========
# Apply feature program from JSON configuration
feature_program_id = params.get("feature_program_id") or self.cfg.get("feature_program_id", "comprehensive_v1")

if feature_program_id and feature_program_id != "none":
    print(f"[WalkForwardHPO] Applying feature program '{feature_program_id}' to IS data")
    df_is_sorted, dsl_features, dsl_meta = apply_feature_program(
        df_is_sorted,
        feature_program_id,
        verbose=False
    )
    print(f"[WalkForwardHPO] DSL created {len(dsl_features)} features: {dsl_features}")
else:
    dsl_features = []
    dsl_meta = {"program_id": "none", "status": "skipped"}
```

### 4. 删除 OOS 硬编码特征计算,改用 DSL

**位置**: wf_hpo.py 约950-1050行 (OOS部分)

**需要删除的代码** (约100行硬编码特征):
```python
# 删除这整块硬编码 ↓↓↓
if "close" in df_oos_sorted.columns and "vwap" in df_oos_sorted.columns:
    df_oos_sorted["feat_vwap_dev"] = (df_oos_sorted["close"] / df_oos_sorted["vwap"]) - 1.0

if "ret_1" in df_oos_sorted.columns and "atr_14" in df_oos_sorted.columns:
    df_oos_sorted["feat_ret1m_over_atr"] = ...

... (所有硬编码特征计算)
```

**替换为** (统一使用 DSL):
```python
# ========== OOS: 应用相同的 DSL 特征程序 ==========
if feature_program_id and feature_program_id != "none":
    print(f"[WalkForwardHPO] Applying feature program '{feature_program_id}' to OOS data")
    df_oos_sorted, _, _ = apply_feature_program(
        df_oos_sorted,
        feature_program_id,
        verbose=False
    )
else:
    print(f"[WalkForwardHPO] No feature program, using raw features for OOS")
```

### 5. 确保 OOS 使用相同的 scaler

**位置**: OOS 标准化部分 (约1060行)

**确认这段代码存在且正确**:
```python
# 使用 IS 的 scaler 参数标准化 OOS
if scale_mode in ("zscore", "robust"):
    if scale_mode == "zscore":
        features_oos = (features_oos - scaler_meta["mean"]) / scaler_meta["std"]
    else:  # robust
        features_oos = (features_oos - scaler_meta["median"]) / scaler_meta["iqr"]

    # Clip to [-5, 5]
    features_oos = features_oos.clip(-5.0, 5.0)
```

## 关键检查点

修改完成后,确保:

1. ✅ IS 和 OOS 都调用 `apply_feature_program(df, feature_program_id)`
2. ✅ 没有任何硬编码的特征计算 (除非是 `feature_program_id="none"` 的情况)
3. ✅ OOS 使用 IS 的 `scaler_meta` 做标准化
4. ✅ `feature_cols` 基于 IS 构建,OOS 只取相同的列
5. ✅ 删除了旧版 `_apply_feature_program` 方法

## 测试

修改后运行:
```bash
venv/bin/python scripts/test_feature_dsl.py
```

确保所有7个特征程序都能正确生成特征。

## 预期效果

- IS 和 OOS 特征完全一致
- AI 只需修改 `feature_programs.json` 就能改变特征
- 训练/回测/实盘可以共用 `libs/features/feature_dsl.py`
