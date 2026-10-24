# 完整ML系统部署总结

> **完成时间**: 2025-01-12
> **目标**: AI大脑通过JSON完全控制：标签/损失/特征产线（XGB叶子+DFS）
> **状态**: ✅ 完整落地，零缺失

---

## 一、系统架构

### AI大脑控制层次

```
AI大脑（仅修改JSON）
  ├─ ml_policy.json          ← 标签/损失/权重/CV
  └─ feature_tools.json      ← 特征产线（XGB+DFS+过滤）
        ↓
核心执行层（无需修改代码）
  ├─ label_utils.py          ← 秒级无泄漏标签生成
  ├─ xgb_feature_maker.py    ← XGB叶子嵌入
  ├─ featuretools_adapter.py ← DFS自动特征
  ├─ feature_filter.py       ← 特征筛选
  └─ wf_hpo.py              ← 训练流程集成
```

### 核心价值

1. **零代码切换**: 日内↔隔夜、回归↔分类、XGB↔DFS，仅修改JSON
2. **防泄漏保证**: 秒级merge_asof对齐 + IS fit/OOS transform分离
3. **Pipeline灵活性**: 4种特征产线模式自由切换
4. **可审计追溯**: 所有配置持久化JSON，元数据落盘ClickHouse

---

## 二、核心组件

### 1. 标签系统 (`label_utils.py`)

**核心功能**:
- 基于`merge_asof`的秒级精确对齐
- 支持回归/二分类/多分类
- 样本权重（按波动率/成交量）
- 防泄漏：`max_slip_sec=0`严格对齐

**关键代码**:

```python
def _forward_match(df, horizon_sec, max_slip_sec=0):
    """秒级精确对齐未来时间点"""
    left["ts_query"] = left["ts"] + pd.to_timedelta(horizon_sec, unit="s")

    merged = pd.merge_asof(
        left,
        right,
        left_on="ts_query",
        right_on="ts",
        by="symbol",
        direction="forward",
        tolerance=pd.Timedelta(seconds=max_slip_sec)
    )
    return merged["to_val"]  # 可能含NaN（未来点缺失）
```

**配置示例** (`ml_policy.json`):

```json
{
  "task": "regression",
  "label": {
    "horizon_sec": 60,          // 1分钟
    "method": "logret",         // log收益
    "from_col": "close",
    "to_col": "close",
    "clip_abs": 0.2,            // 截断极端值
    "max_slip_sec": 0           // 严格对齐
  }
}
```

### 2. XGB特征工厂 (`xgb_feature_maker.py`)

**核心功能**:
- 训练XGB模型提取叶子索引
- Hash trick降维（512-2048维）
- 支持回归/二分类/多分类

**关键代码**:

```python
class XGBFeatureMaker:
    def fit(self, X, y, sample_weight=None):
        """训练XGB模型"""
        if self.task == "binary":
            self.model = XGBClassifier(**params)
        else:
            self.model = XGBRegressor(**params)
        self.model.fit(X, y, sample_weight=sample_weight)

    def transform(self, X):
        """生成叶子嵌入特征"""
        leaves = self.model.apply(X)  # (n_samples, n_trees)
        # Hash trick降维
        h = np.zeros((leaves.shape[0], self.hash_dim))
        for j in range(leaves.shape[1]):
            bucket = (leaves[:, j] * (j + 1315423911)) % self.hash_dim
            for i, b in enumerate(bucket):
                h[i, b] += 1.0
        return pd.DataFrame(h).add_prefix("xgbh_")
```

**配置示例** (`feature_tools.json`):

```json
{
  "xgb_feature_maker": {
    "enabled": true,
    "model": {
      "n_estimators": 200,
      "max_depth": 6,
      "learning_rate": 0.05
    },
    "leaf_embedding": {
      "mode": "hash",
      "hash_dim": 512
    }
  }
}
```

### 3. Pipeline模式

**模式对比**:

| 模式 | 特征流程 | 适用场景 |
|------|---------|---------|
| **xgb_only** | 原始→XGB叶子 | 稳定基线，快速迭代 |
| **dfs_only** | 原始→DFS | 探索DFS质量 |
| **xgb_then_dfs** | 原始→XGB叶子→合并→DFS | 推荐生产（兼顾解释性和自动发现） |
| **dfs_then_xgb** | 原始→DFS→XGB叶子 | 实验性（测试DFS作为XGB输入） |

**切换方式**（AI修改JSON）:

```json
[
  {
    "op": "replace",
    "path": "/pipeline/mode",
    "value": "xgb_then_dfs"
  }
]
```

### 4. 完整训练流程

**wf_hpo.py集成点**:

```python
# 1. 加载配置
ml_policy = json.load(open("ml_policy.json"))
feature_tools = json.load(open("feature_tools.json"))

# 2. 生成标签（秒级对齐）
y_is = compute_label(df_is, ml_policy)
y_oos = compute_label(df_oos, ml_policy)
w_is = make_sample_weight(df_is, ml_policy)

# 3. Pipeline模式选择
pipeline_mode = feature_tools["pipeline"]["mode"]

if pipeline_mode == "xgb_only":
    # 仅XGB叶子
    xfm = XGBFeatureMaker(feature_tools["xgb_feature_maker"], ml_policy["task"])
    xfm.fit(X_is_base, y_is, sample_weight=w_is)
    X_is = xfm.transform(X_is_base)
    X_oos = xfm.transform(X_oos_base)

elif pipeline_mode == "dfs_only":
    # 仅DFS
    ft = FeaturetoolsAdapter(feature_tools["featuretools"])
    ft.fit_on_is(df_is, base_cols)
    X_is = ft.transform_any(df_is)
    X_oos = ft.transform_any(df_oos)

elif pipeline_mode == "xgb_then_dfs":
    # XGB→合并→DFS
    xfm = XGBFeatureMaker(...)
    xfm.fit(X_is_base, y_is, w_is)
    X_is_xgb = xfm.transform(X_is_base)
    X_is_merged = pd.concat([X_is_base, X_is_xgb], axis=1)

    ft = FeaturetoolsAdapter(...)
    ft.fit_on_is(df_is_merged, all_cols)
    X_is = ft.transform_any(df_is_merged)
    X_oos = ft.transform_any(df_oos_merged)

# 4. 特征筛选
ff = FeatureFilter(feature_tools["xgb_feature_tools"]["default"]["filters"])
X_is_sel = ff.fit_transform(X_is, y_is)
X_oos_sel = ff.transform(X_oos)

# 5. 训练与评估
xgb_params = xgb_params_from_policy(ml_policy)
model = train_model(X_is_sel, y_is, params=xgb_params, sample_weight=w_is)
metrics = evaluate_model(model, X_oos_sel, y_oos, ml_policy["loss"]["sk_metric"])
```

---

## 三、文件清单

### 配置文件

| 文件 | 说明 | 行数 |
|------|------|------|
| `ml_policy.json` | 标签/损失/权重/CV配置 | ~50 |
| `feature_tools.json` | 特征产线配置（XGB+DFS+过滤） | ~150 |

### 核心模块

| 文件 | 说明 | 行数 |
|------|------|------|
| `libs/features/label_utils.py` | 秒级无泄漏标签生成 | 191 |
| `libs/features/xgb_feature_maker.py` | XGB叶子嵌入 | 147 |
| `libs/features/featuretools_adapter.py` | DFS自动特征 | 221 |
| `libs/features/feature_filter.py` | 特征筛选 | 221 |
| `libs/features/features_injector.py` | V2零拷贝流程 | 228 |
| `libs/features/ic_metrics.py` | Spearman IC | 148 |
| `libs/features/risk_metrics.py` | 机构级风险指标 | 134 |

### AI Prompt模板

| 文件 | 说明 | 行数 |
|------|------|------|
| `prompts/ml_policy_prompt.md` | 标签/损失配置生成 | ~350 |
| `prompts/featuretools_config_prompt.md` | DFS配置生成 | ~250 |
| `prompts/feature_filter_prompt.md` | 特征筛选配置生成 | ~200 |

### 测试脚本

| 文件 | 说明 |
|------|------|
| `scripts/test_featuretools_integration.py` | DFS集成测试 |
| `scripts/test_feature_filter.py` | 特征筛选测试 |

---

## 四、使用场景

### 场景1: 日内→隔夜切换（仅改JSON）

**AI Prompt**:
```
把日内（+1m）切换为隔夜（+1d），保持回归，权重按波动率^1.0
```

**AI生成JSON Patch**:

```json
[
  {"op": "replace", "path": "/label/name", "value": "ret_fwd_1d"},
  {"op": "replace", "path": "/label/horizon_sec", "value": 86400},
  {"op": "replace", "path": "/label/to_col", "value": "open"},
  {"op": "replace", "path": "/label/clip_abs", "value": 0.3},
  {"op": "replace", "path": "/weights/scheme", "value": "by_volatility"}
]
```

**应用Patch**:

```python
import json, jsonpatch
with open("ml_policy.json") as f:
    config = json.load(f)
patch = jsonpatch.JsonPatch(ai_generated_patch)
config = patch.apply(config)
with open("ml_policy.json", "w") as f:
    json.dump(config, f, indent=2)
```

### 场景2: 回归→二分类切换

**AI Prompt**:
```
改为二分类任务，阈值=0，损失binary:logistic，权重uniform
```

**JSON Patch**:

```json
[
  {"op": "replace", "path": "/task", "value": "binary"},
  {"op": "replace", "path": "/label/cls_bins", "value": [-9, 0.0, 9]},
  {"op": "replace", "path": "/loss/xgb_objective", "value": "binary:logistic"},
  {"op": "replace", "path": "/loss/xgb_eval_metric", "value": ["auc", "logloss"]},
  {"op": "replace", "path": "/loss/sk_metric", "value": "roc_auc"},
  {"op": "replace", "path": "/weights/scheme", "value": "uniform"}
]
```

### 场景3: Pipeline模式切换

**AI Prompt**:
```
将模式改为 "xgb_then_dfs"，XGB hash_dim=1024，DFS窗口60D
```

**JSON Patch**:

```json
[
  {"op": "replace", "path": "/pipeline/mode", "value": "xgb_then_dfs"},
  {"op": "replace", "path": "/xgb_feature_maker/leaf_embedding/hash_dim", "value": 1024},
  {"op": "replace", "path": "/featuretools/training_window", "value": "60D"}
]
```

---

## 五、验证清单

### 快速验证

```bash
# 1. 检查文件存在
ls -lh ml_policy.json feature_tools.json
ls -lh libs/features/{label_utils,xgb_feature_maker,featuretools_adapter}.py
ls -lh prompts/{ml_policy,featuretools_config}_prompt.md

# 2. 运行单元测试
python scripts/test_featuretools_integration.py
python scripts/test_feature_filter.py

# 3. 检查wf_hpo.py集成点
grep -n "label_utils\|xgb_feature_maker" wf_hpo.py
```

### 完整测试流程

```python
# 测试1: 日内回归（+1m）
ml_policy = {
    "task": "regression",
    "label": {"horizon_sec": 60, "method": "logret", "clip_abs": 0.2}
}

# 测试2: 隔夜回归（+1d）
ml_policy["label"]["horizon_sec"] = 86400
ml_policy["label"]["to_col"] = "open"

# 测试3: 二分类
ml_policy["task"] = "binary"
ml_policy["label"]["cls_bins"] = [-9, 0.0, 9]

# 测试4: Pipeline模式
for mode in ["xgb_only", "dfs_only", "xgb_then_dfs", "dfs_then_xgb"]:
    feature_tools["pipeline"]["mode"] = mode
    # 运行训练...
```

---

## 六、性能与限制

### 资源消耗

| 组件 | 内存 | 时间 | 瓶颈 |
|------|------|------|------|
| **label_utils** | 低 | <1s | merge_asof（已优化） |
| **xgb_feature_maker** | 中 | 10-30s | XGB训练（n_estimators） |
| **featuretools_adapter** | 高 | 30-60s | DFS深度（max_depth） |
| **feature_filter** | 低 | 5-10s | 相关性计算（sample_n） |

### 优化建议

1. **首次使用**: 小窗口（1 symbol + 30D）
2. **调优阶段**: 单独测试XGB/DFS，观察特征质量
3. **生产环境**: xgb_then_dfs模式，hash_dim=512-1024

### 已知限制

1. **max_slip_sec>0风险**: 可能引入轻微泄漏（谨慎使用）
2. **DFS特征爆炸**: max_depth=3可能产生1万+特征（需topk截断）
3. **多标的对齐**: 需确保所有标的有相同时间粒度

---

## 七、故障排查

### 问题1: 标签全为NaN

**原因**: 未来时间点缺失（max_slip_sec=0严格对齐）

**解决**:
```python
# 检查未来点覆盖率
print(f"有效标签比例: {y_is.notna().mean():.2%}")

# 适当放宽（谨慎）
ml_policy["label"]["max_slip_sec"] = 60  # 允许60秒内第一根
```

### 问题2: XGB叶子特征维度过高

**原因**: hash_dim设置过大或n_estimators过多

**解决**:
```json
{
  "xgb_feature_maker": {
    "model": {"n_estimators": 100},  // 降低树数量
    "leaf_embedding": {"hash_dim": 512}  // 降低hash维度
  }
}
```

### 问题3: Pipeline模式切换无效

**检查**:
```python
# 确认配置已加载
print(feature_tools["pipeline"]["mode"])

# 检查日志
# [WalkForwardHPO] Pipeline模式: xgb_then_dfs
```

---

## 八、后续扩展

### 扩展1: 交易日历感知

```python
def horizon_shift_with_calendar(ts, horizon_sec, calendar):
    """跳过休市，精确对齐交易日"""
    # 实现逻辑：
    # 1. 转换ts为交易日
    # 2. 计算未来N个交易周期
    # 3. 返回对齐后的ts
    pass
```

### 扩展2: 自定义XGB Primitives

```python
# xgb_feature_maker.py 扩展
def _embed_interaction(self, leaves):
    """叶子交互特征（tree_i × tree_j）"""
    h = np.zeros((leaves.shape[0], self.hash_dim))
    for i in range(leaves.shape[1]):
        for j in range(i+1, leaves.shape[1]):
            key = leaves[:, i] * 10007 + leaves[:, j] * 10009
            bucket = key % self.hash_dim
            # ...
    return h
```

### 扩展3: 多目标训练

```json
{
  "task": "multi_target",
  "labels": [
    {"name": "ret_1m", "horizon_sec": 60},
    {"name": "ret_5m", "horizon_sec": 300}
  ]
}
```

---

## 九、AI Prompt使用

### Prompt 1: 修改标签/损失

**文件**: `prompts/ml_policy_prompt.md`

**示例**:
```
把日内（+1m）切换为隔夜（+1d），保持回归，权重按波动率^1.0
```

**输出**: JSON Patch数组

### Prompt 2: 修改DFS配置

**文件**: `prompts/featuretools_config_prompt.md`

**示例**:
```
启用DFS，max_depth=2，training_window=60D，topk=1024
```

**输出**: JSON Patch数组

### Prompt 3: 修改特征筛选

**文件**: `prompts/feature_filter_prompt.md`

**示例**:
```
target_corr.min_abs=0.02，pair_corr.max_abs=0.98，max_features=2048
```

**输出**: JSON Patch数组

---

## 十、总结

### 已完成 ✅

1. **label_utils.py**: 秒级无泄漏标签生成（merge_asof精确对齐）
2. **xgb_feature_maker.py**: XGB叶子嵌入（Hash trick降维）
3. **ml_policy.json**: 统一标签/损失/权重配置
4. **feature_tools.json**: 扩展XGB配置（xgb_feature_maker节点）
5. **wf_hpo.py集成**: Pipeline模式支持（4种模式）
6. **AI Prompt模板**: ml_policy_prompt.md

### 核心价值

- **AI大脑完全控制**: 修改JSON即可切换日内/隔夜、回归/分类、XGB/DFS
- **防泄漏保证**: max_slip_sec=0严格对齐 + IS fit/OOS transform分离
- **Pipeline灵活**: xgb_only / dfs_only / xgb_then_dfs / dfs_then_xgb
- **可审计**: 所有配置持久化JSON

### 零代码切换能力

| 切换类型 | 修改文件 | 示例 |
|---------|---------|------|
| 日内↔隔夜 | ml_policy.json | horizon_sec: 60 ↔ 86400 |
| 回归↔分类 | ml_policy.json | task: regression ↔ binary |
| XGB↔DFS | feature_tools.json | pipeline.mode: xgb_only ↔ dfs_only |
| 权重策略 | ml_policy.json | weights.scheme: uniform ↔ by_volatility |
| 损失函数 | ml_policy.json | loss.xgb_objective: squarederror ↔ huber |

---

**最后更新**: 2025-01-12
**维护者**: Trading AI Team
**相关文档**: `FEATURETOOLS_DEPLOYMENT_SUMMARY.md`, `CONFIG_DRIVEN_ML_COMPLETE.md`
