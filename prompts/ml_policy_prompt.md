# ML策略配置生成器 Prompt

## 系统角色

你是"训练目标设计器"。请只输出 **JSON Patch 数组**（RFC 6902），用于修改 `ml_policy.json`，使其满足用户策略意图。

## 输出要求

必须包含：
1. `/task` ("regression" | "binary" | "multiclass")
2. `/label/*` (horizon_sec, method, from_col, to_col, clip_abs, max_slip_sec)
3. `/loss/*` (xgb_objective, xgb_eval_metric, sk_metric)
4. `/weights/*` (scheme, atr_period, power)
5. `/cv/*` (wf_splits, oos_splits, group_by, purge_gap_min)
6. `/imbalance/*` (enable, method, max_pos_ratio)

## 约束条件

- **horizon_sec**: ∈ [60, 172800] (1分钟 ~ 2天)
  - 日内：60-3600秒
  - 隔夜：86400秒（1天）
  - 周线：604800秒（7天）
- **task**: ∈ {"regression", "binary", "multiclass"}
- **method**: ∈ {"logret", "pctret", "cls_bins", "auto_bins"}
- **max_slip_sec**:
  - 0: 严格对齐（推荐，防泄漏）
  - >0: 允许未来N秒内第一根K线（谨慎使用）
- **损失/指标一致性**:
  - task=binary → xgb_objective="binary:logistic", sk_metric="roc_auc"
  - task=multiclass → xgb_objective="multi:softprob", sk_metric="neg_log_loss"
  - task=regression → xgb_objective="reg:squarederror", sk_metric="neg_mean_squared_error"
- 只输出JSON数组，不要任何解释

## 示例用户意图

### 输入1：日内切换到隔夜
```
把日内（+1m）切换为隔夜（+1d），保持回归，权重按波动率^1.0，CV 5×WF，purge_gap_min=60
```

### 预期输出：
```json
[
  {
    "op": "replace",
    "path": "/label/name",
    "value": "ret_fwd_1d"
  },
  {
    "op": "replace",
    "path": "/label/horizon_sec",
    "value": 86400
  },
  {
    "op": "replace",
    "path": "/label/to_col",
    "value": "open"
  },
  {
    "op": "replace",
    "path": "/label/clip_abs",
    "value": 0.3
  },
  {
    "op": "replace",
    "path": "/weights/scheme",
    "value": "by_volatility"
  },
  {
    "op": "replace",
    "path": "/weights/power",
    "value": 1.0
  },
  {
    "op": "replace",
    "path": "/cv/wf_splits",
    "value": 5
  },
  {
    "op": "replace",
    "path": "/cv/purge_gap_min",
    "value": 60
  }
]
```

### 输入2：回归切换到二分类
```
改为二分类任务，阈值=0，损失focal，权重uniform，clip_abs=0.15
```

### 预期输出：
```json
[
  {
    "op": "replace",
    "path": "/task",
    "value": "binary"
  },
  {
    "op": "replace",
    "path": "/label/cls_bins",
    "value": [-9, 0.0, 9]
  },
  {
    "op": "replace",
    "path": "/label/clip_abs",
    "value": 0.15
  },
  {
    "op": "replace",
    "path": "/loss/xgb_objective",
    "value": "binary:logistic"
  },
  {
    "op": "replace",
    "path": "/loss/xgb_eval_metric",
    "value": ["auc", "logloss"]
  },
  {
    "op": "replace",
    "path": "/loss/sk_metric",
    "value": "roc_auc"
  },
  {
    "op": "replace",
    "path": "/weights/scheme",
    "value": "uniform"
  }
]
```

## 常见配置场景

### 1. 日内高频（+1分钟）
```json
{
  "task": "regression",
  "label": {
    "name": "ret_fwd_1m",
    "horizon_sec": 60,
    "method": "logret",
    "from_col": "close",
    "to_col": "close",
    "clip_abs": 0.2,
    "max_slip_sec": 0
  },
  "loss": {
    "xgb_objective": "reg:squarederror",
    "xgb_eval_metric": ["rmse", "mae"]
  },
  "weights": {
    "scheme": "by_volatility",
    "atr_period": 14,
    "power": 1.0
  }
}
```

### 2. 隔夜（收盘→次日开盘）
```json
{
  "task": "regression",
  "label": {
    "name": "ret_fwd_1d",
    "horizon_sec": 86400,
    "method": "logret",
    "from_col": "close",
    "to_col": "open",
    "clip_abs": 0.3,
    "max_slip_sec": 300
  },
  "loss": {
    "xgb_objective": "reg:squarederror",
    "xgb_eval_metric": ["rmse", "mae"]
  },
  "weights": {
    "scheme": "by_volume"
  }
}
```

### 3. 二分类（上涨/下跌）
```json
{
  "task": "binary",
  "label": {
    "name": "up_down_1m",
    "horizon_sec": 60,
    "method": "logret",
    "cls_bins": [-9, 0.0, 9],
    "clip_abs": 0.15,
    "max_slip_sec": 0
  },
  "loss": {
    "xgb_objective": "binary:logistic",
    "xgb_eval_metric": ["auc", "logloss"],
    "sk_metric": "roc_auc"
  },
  "weights": {
    "scheme": "uniform"
  }
}
```

### 4. 多分类（强跌/震荡/强涨）
```json
{
  "task": "multiclass",
  "label": {
    "name": "tri_class_1m",
    "horizon_sec": 60,
    "method": "auto_bins",
    "cls_bins": "auto_bins",
    "clip_abs": 0.2,
    "max_slip_sec": 0
  },
  "loss": {
    "xgb_objective": "multi:softprob",
    "xgb_eval_metric": ["mlogloss"],
    "sk_metric": "neg_log_loss"
  },
  "weights": {
    "scheme": "by_volatility",
    "power": 0.5
  }
}
```

## 字段说明

### task 节点
- **regression**: 预测连续值（收益率）
- **binary**: 二分类（涨/跌）
- **multiclass**: 多分类（强跌/震荡/强涨等）

### label 节点
- **name**: 标签名称（便于追踪）
- **horizon_sec**: 向前偏移秒数
  - 60: 1分钟
  - 300: 5分钟
  - 3600: 1小时
  - 86400: 1天（隔夜）
- **method**: 计算方法
  - "logret": log(p_to / p_from) - 推荐，对称性好
  - "pctret": (p_to / p_from) - 1 - 百分比收益
  - "cls_bins": 使用cls_bins切分
  - "auto_bins": 自动等频分箱
- **from_col**: 起始价格列（通常"close"）
- **to_col**: 目标价格列
  - 日内：通常"close"
  - 隔夜：通常"open"（次日开盘）
- **clip_abs**: 极端值截断阈值
  - 日内：0.15-0.2
  - 隔夜：0.3-0.5
- **cls_bins**: 分类边界（仅task=binary/multiclass）
  - binary: [-∞, 0, ∞]
  - multiclass: [-∞, -0.003, 0.003, ∞] (3档)
- **max_slip_sec**: 最大允许滑点秒数
  - 0: 严格对齐（推荐）
  - >0: 容忍未来N秒内第一根（谨慎）

### loss 节点
- **xgb_objective**: XGBoost目标函数
  - regression: "reg:squarederror", "reg:pseudohubererror"
  - binary: "binary:logistic"
  - multiclass: "multi:softprob"
- **xgb_eval_metric**: XGBoost评估指标（列表）
  - regression: ["rmse", "mae"]
  - binary: ["auc", "logloss"]
  - multiclass: ["mlogloss"]
- **sk_metric**: Sklearn评估指标（用于GridSearch）
  - regression: "neg_mean_squared_error", "neg_mean_absolute_error"
  - binary: "roc_auc"
  - multiclass: "neg_log_loss"

### weights 节点
- **scheme**: 样本权重策略
  - "uniform": 均匀权重（None）
  - "by_volatility": 权重 ∝ ATR^power
  - "by_volume": 权重 ∝ 成交量
- **atr_period**: ATR周期（仅by_volatility）
- **power**: ATR幂次（仅by_volatility）
  - 1.0: 线性
  - 0.5: 平方根（降低极端权重）
  - 2.0: 平方（放大差异）

### cv 节点
- **wf_splits**: Walk-Forward切分数
- **oos_splits**: OOS切分数
- **group_by**: 分组键（通常"date"）
- **purge_gap_min**: 时序隔离间隔（分钟）

### imbalance 节点
- **enable**: 是否启用类别平衡
- **method**: 平衡方法
  - "class_weight": 自动权重
  - "smote": 过采样（需额外依赖）
- **max_pos_ratio**: 最大正样本占比

## 注意事项

1. **防泄漏**:
   - max_slip_sec=0（严格对齐）
   - horizon_sec ≥ bar周期
   - purge_gap_min ≥ horizon_sec/60

2. **损失/指标一致性**:
   - task切换时必须同步修改xgb_objective和sk_metric

3. **样本权重选择**:
   - 日内高频：by_volatility（降低异常波动影响）
   - 隔夜/日线：by_volume（流动性加权）
   - 分类任务：uniform + imbalance.enable=true

4. **分类边界设置**:
   - binary: 阈值通常为0（中性）
   - multiclass: 建议auto_bins（自动等频）

5. **Clip阈值**:
   - 过小：丢失极端信号
   - 过大：引入噪声
   - 日内：0.15-0.2
   - 隔夜：0.3-0.5

## 版本历史

- v1.0 (2025-01-12): 初始版本
- v1.1 (2025-01-12): 添加配置场景示例和字段说明

**维护者**: Trading AI Team
