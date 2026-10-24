# Feature DSL System Guide

**日期**: 2025-11-12
**目标**: AI大脑安全地生成交易特征,无需编写Python代码

---

## 1. 系统概览

### 核心理念

> **"AI修改JSON,Python执行运算"**

传统方法的问题:
- ❌ AI直接写Python代码 → 安全风险 (任意代码执行)
- ❌ 硬编码特征 → 难以实验和版本控制
- ❌ 每次改动需修改源码 → 回滚困难

Feature DSL解决方案:
- ✅ AI只编辑JSON配置文件 → 安全可控
- ✅ 预定义操作符 → 数学定义明确
- ✅ 版本控制友好 → Git追踪每次改动
- ✅ HPO并行探索 → 同时测试多个特征程序

---

## 2. 架构设计

### 2.1 数据流

```
ClickHouse (原始特征)
    ↓
wf_hpo._prepare_model_data()
    ↓
_apply_feature_program(df, "baseline_v1")
    ↓
读取 feature_programs.json
    ↓
逐步执行操作符 (ratio, rolling_sum, ...)
    ↓
返回增强后的DataFrame (原始 + 派生特征)
    ↓
特征选择 (feature_subset)
    ↓
特征标准化 (RobustScaler)
    ↓
模型训练 (XGBoost/LSTM/Transformer)
```

### 2.2 文件结构

```
trading/
├── feature_programs.json          # AI大脑编辑的JSON配置
├── wf_hpo.py                       # 执行器: _apply_feature_program()
├── alpha_config_enhanced.json     # HPO参数: feature_program_id
└── scripts/
    └── test_feature_dsl.py        # 单元测试
```

---

## 3. JSON配置格式

### 3.1 基本结构

```json
{
  "program_id": {
    "description": "特征程序描述",
    "steps": [
      {
        "name": "feat_name",
        "op": "operator_name",
        "col": "column_name",
        "num": "numerator",
        "den": "denominator",
        "eps": 1e-6,
        "window_bars": 10,
        "min_periods": 3,
        "per_symbol": true
      }
    ]
  }
}
```

### 3.2 通用字段说明

| 字段 | 类型 | 必需? | 说明 |
|------|------|-------|------|
| `name` | string | ✅ | 生成的特征列名 (必须以 `feat_` 开头) |
| `op` | string | ✅ | 操作符名称 (见下文支持列表) |
| `per_symbol` | boolean | ❌ | 是否按symbol分组 (时序操作推荐true) |

---

## 4. 支持的操作符

### 4.1 比率运算

#### `ratio` - 简单比率
```json
{
  "name": "feat_ret_over_atr",
  "op": "ratio",
  "num": "ret_1",
  "den": "atr_14",
  "eps": 1e-6
}
```
**数学定义**: `feat = num / (den + eps)`
**用途**: 归一化收益率、相对强度

#### `ratio_minus_1` - 偏离度
```json
{
  "name": "feat_vwap_dev",
  "op": "ratio_minus_1",
  "num": "close",
  "den": "vwap",
  "eps": 1e-6
}
```
**数学定义**: `feat = (num / (den + eps)) - 1.0`
**用途**: 价格偏离VWAP、相对基准变化

---

### 4.2 变换函数

#### `log1p` - 对数变换
```json
{
  "name": "feat_log_volume",
  "op": "log1p",
  "col": "volume"
}
```
**数学定义**: `feat = log(1 + col)`
**用途**: 压缩长尾分布 (成交量、价差)

---

### 4.3 滚动聚合

#### `rolling_sum` - 滚动求和
```json
{
  "name": "feat_ret_10m_momentum",
  "op": "rolling_sum",
  "col": "ret_1",
  "window_bars": 10,
  "min_periods": 3,
  "per_symbol": true
}
```
**数学定义**: `feat[t] = sum(col[t-window+1 : t+1])`
**用途**: 累积动量、总成交量

#### `rolling_mean` - 滚动均值
```json
{
  "name": "feat_ret_20m_avg",
  "op": "rolling_mean",
  "col": "ret_1",
  "window_bars": 20,
  "min_periods": 5,
  "per_symbol": true
}
```
**数学定义**: `feat[t] = mean(col[t-window+1 : t+1])`
**用途**: 移动平均、趋势

#### `rolling_std` - 滚动标准差
```json
{
  "name": "feat_ret_20m_vol",
  "op": "rolling_std",
  "col": "ret_1",
  "window_bars": 20,
  "min_periods": 5,
  "per_symbol": true
}
```
**数学定义**: `feat[t] = std(col[t-window+1 : t+1])`
**用途**: 实现波动率、价格不稳定性

#### `rolling_zscore` - 滚动Z分数
```json
{
  "name": "feat_ret_50m_zscore",
  "op": "rolling_zscore",
  "col": "ret_1",
  "window_bars": 50,
  "min_periods": 10,
  "per_symbol": true
}
```
**数学定义**:
```
mean[t] = mean(col[t-window+1 : t+1])
std[t] = std(col[t-window+1 : t+1])
feat[t] = (col[t] - mean[t]) / (std[t] + 1e-9)
```
**用途**: 标准化信号、均值回归检测

---

### 4.4 微观结构特征

#### `price_impact_hl` - 价格冲击
```json
{
  "name": "feat_price_impact",
  "op": "price_impact_hl",
  "per_symbol": true
}
```
**数学定义**: `feat[t] = (high[t] - low[t]) / close[t-1]`
**用途**: 日内波动率、流动性成本

#### `effective_spread` - 有效价差
```json
{
  "name": "feat_spread",
  "op": "effective_spread",
  "eps": 1e-6
}
```
**数学定义**: `feat = 2 * |close - vwap| / vwap`
**用途**: 买卖价差估计、交易成本

#### `amihud_illiquidity` - Amihud非流动性
```json
{
  "name": "feat_illiq",
  "op": "amihud_illiquidity",
  "eps": 1e-9
}
```
**数学定义**: `feat = |ret_1| / (volume * close)`
**用途**: 价格冲击成本、市场深度不足

---

## 5. 多频特征与 Resample 工具

### 5.1 可用别名
- `f.<col>`: 默认的 1m 基础列 (`trading.features_1m`)。
- `m5.<col>`: 通过 `tools/feature_resample.py --kind five_min --interval 5` 构建的 5 分钟聚合 (`trading.features_5m`)，Join 条件为 `m5.ts = toStartOfInterval(f.ts, 5m)`。
- `d.<col>`: 通过 `tools/feature_resample.py --kind daily_prev` 生成的上一交易日聚合 (`trading.features_1d_prev`)，表内 `ts` 已经 +1 天，因此 Join 条件为 `d.ts = toStartOfDay(f.ts)`。

### 5.2 如何在 Catalog/DSL 中使用
- 当表达式引用 `m5.` 或 `d.` 时，务必在 JSON spec 里添加 `\"required_sources\": [\"m5\", \"d\"]` 子集，平台才会自动 LEFT JOIN 对应数据源：
  ```json
  {
    "name": "mf_ema_gap",
    "expr": "(m5.ema_close_20 - f.close) / nullIf(abs(d.rvol20) + 1e-6, 0)",
    "dtype": "Float64",
    "notes": "5m EMA 与 1m spot 的差值，按昨日 RVOL 归一化",
    "required_sources": ["m5", "d"]
  }
  ```
- 所有 resample 表都由 1m 数据向后看聚合，不允许引用未来时间；`d.` 列已经代表“上一交易日”。

### 5.3 生成/刷新 resample 表
```bash
# 生成 5m 表
python tools/feature_resample.py --source trading.features_1m --target trading.features_5m --kind five_min --interval 5 --replace

# 生成上一交易日聚合表
python tools/feature_resample.py --source trading.features_1m --target trading.features_1d_prev --kind daily_prev --replace
```
建议将上述脚本接入 nightly job，确保 Brain 随时可以引用多频特征。

---

## 6. 预定义特征程序

### 5.1 `none`
**描述**: 无特征工程,仅使用原始特征
**步骤**: 0
**用途**: Baseline对照组

### 5.2 `baseline_v1`
**描述**: 原始Phase 1特征 (VWAP偏离、归一化收益、对数成交量、滚动动量)
**步骤**: 4
**特征**:
- `feat_vwap_dev`: VWAP偏离度
- `feat_ret1m_over_atr`: ATR归一化收益率
- `feat_log_volume`: 对数成交量
- `feat_ret_1m_roll_10`: 10分钟滚动动量

### 5.3 `microstructure_v1`
**描述**: 微观结构特征 (价格冲击、价差、非流动性)
**步骤**: 3
**特征**:
- `feat_price_impact`: 高低价差/前收盘价
- `feat_effective_spread`: 相对VWAP价差
- `feat_amihud_illiq`: Amihud非流动性指标

### 5.4 `momentum_v1`
**描述**: 动量特征 (多时间窗口滚动收益、Z分数)
**步骤**: 3
**特征**:
- `feat_ret_1m_roll_10`: 10分钟滚动收益
- `feat_ret_5m_roll_20`: 20分钟滚动5m收益
- `feat_zscore_ret1m_50`: 50分钟收益Z分数

### 5.5 `mean_revert_v1`
**描述**: 均值回归特征 (价格偏离、归一化收益、价格Z分数)
**步骤**: 3
**特征**:
- `feat_vwap_dev`: VWAP偏离度
- `feat_ret1m_over_atr`: ATR归一化收益
- `feat_zscore_close_20`: 20分钟价格Z分数

### 5.6 `volatility_v1`
**描述**: 波动率特征 (ATR比率、成交量比率、滚动波动)
**步骤**: 4
**特征**:
- `feat_atr_ratio_5_14`: 短期/长期ATR比率
- `feat_vol_ratio`: 当前/20日均成交量
- `feat_log_volume`: 对数成交量
- `feat_rolling_volatility_20`: 20分钟收益标准差

### 5.7 `comprehensive_v1` (默认)
**描述**: 综合特征集,结合所有类别
**步骤**: 7
**特征**: 包含baseline + microstructure + momentum的子集

---

## 6. 使用方法

### 6.1 在HPO中使用

**配置文件**: `alpha_config_enhanced.json`

```json
{
  "hpo": {
    "grid": {
      "feature_program_id": {
        "type": "choice",
        "values": ["baseline_v1", "momentum_v1", "comprehensive_v1"]
      }
    }
  }
}
```

**运行HPO**:
```bash
export PYTHONPATH=.
venv/bin/python alpha_loop.py \
  --config alpha_config_enhanced.json \
  --provider optuna --gens 1 \
  --out_dir ./alpha_dsl_test
```

### 6.2 创建新特征程序

**步骤**:

1. **编辑 `feature_programs.json`**:
```json
{
  "my_custom_v1": {
    "description": "自定义特征: 价格动量 + 成交量信号",
    "steps": [
      {
        "name": "feat_close_momentum_5",
        "op": "rolling_sum",
        "col": "ret_1",
        "window_bars": 5,
        "min_periods": 3,
        "per_symbol": true
      },
      {
        "name": "feat_volume_surge",
        "op": "ratio",
        "num": "volume",
        "den": "vol_avg_20",
        "eps": 1.0
      }
    ]
  }
}
```

2. **添加到HPO配置** (`alpha_config_enhanced.json`):
```json
"feature_program_id": {
  "type": "choice",
  "values": ["baseline_v1", "my_custom_v1"]
}
```

3. **运行测试**:
```bash
venv/bin/python scripts/test_feature_dsl.py
```

---

## 7. 最佳实践

### 7.1 特征命名规范

- ✅ 使用 `feat_` 前缀: `feat_vwap_dev`
- ✅ 描述性命名: `feat_ret_10m_momentum` (不要 `feat1`)
- ✅ 包含窗口信息: `feat_zscore_ret1m_50` (50分钟窗口)

### 7.2 时序操作安全

**规则**: 所有滚动操作必须 `per_symbol: true`

**原因**: 防止不同股票数据混合

```json
{
  "name": "feat_ret_roll_10",
  "op": "rolling_sum",
  "col": "ret_1",
  "window_bars": 10,
  "per_symbol": true  // ← 必须
}
```

### 7.3 窗口大小选择

| 时间尺度 | 窗口大小 | min_periods | 用途 |
|---------|---------|-------------|------|
| 超短期 | 5-10 bars | 3 | 瞬时动量 |
| 短期 | 10-20 bars | 5 | 日内趋势 |
| 中期 | 20-50 bars | 10 | 多小时模式 |
| 长期 | 50-100 bars | 20 | 日内宏观 |

### 7.4 避免过度拟合

**警告**: 不要创建过多相似特征

❌ **错误示例**:
```json
"steps": [
  {"name": "feat_ret_roll_5", "op": "rolling_sum", "col": "ret_1", "window_bars": 5},
  {"name": "feat_ret_roll_6", "op": "rolling_sum", "col": "ret_1", "window_bars": 6},
  {"name": "feat_ret_roll_7", "op": "rolling_sum", "col": "ret_1", "window_bars": 7}
]
```

✅ **正确示例**:
```json
"steps": [
  {"name": "feat_ret_roll_5", "op": "rolling_sum", "col": "ret_1", "window_bars": 5},
  {"name": "feat_ret_roll_20", "op": "rolling_sum", "col": "ret_1", "window_bars": 20},
  {"name": "feat_ret_roll_50", "op": "rolling_sum", "col": "ret_1", "window_bars": 50}
]
```

### 7.5 数据要求

| 操作符 | 必需列 | 可选列 |
|--------|--------|--------|
| `ratio_minus_1` | num, den | - |
| `rolling_zscore` | col | symbol (for per_symbol) |
| `price_impact_hl` | high, low, close | symbol |
| `effective_spread` | close, vwap | - |
| `amihud_illiquidity` | ret_1, volume, close | - |

---

## 8. 调试与测试

### 8.1 运行单元测试

```bash
cd .
venv/bin/python scripts/test_feature_dsl.py
```

**输出示例**:
```
======================================================================
Testing program: baseline_v1
======================================================================
[Test] Applying feature program 'baseline_v1': Original Phase 1 features
[Test] 4 steps to execute
[Test] Step 1/4: created 'feat_vwap_dev' using 'ratio_minus_1' ✓
[Test] Step 2/4: created 'feat_ret1m_over_atr' using 'ratio' ✓
[Test] Step 3/4: created 'feat_log_volume' using 'log1p' ✓
[Test] Step 4/4: created 'feat_ret_1m_roll_10' using 'rolling_sum' ✓

[Test] New features created: 4
  - feat_vwap_dev: 100/100 non-null values
  - feat_ret1m_over_atr: 100/100 non-null values
  - feat_log_volume: 100/100 non-null values
  - feat_ret_1m_roll_10: 93/100 non-null values
```

### 8.2 检查特征质量

```python
import pandas as pd
from wf_hpo import WalkForwardHPO

# 加载数据
df = pd.read_parquet("data/features_tsla.parquet")

# 应用特征程序
hpo = WalkForwardHPO(config)
df_enriched = hpo._apply_feature_program(df, "baseline_v1")

# 检查特征
new_cols = [c for c in df_enriched.columns if c.startswith("feat_")]
print(f"New features: {new_cols}")
print(df_enriched[new_cols].describe())
```

### 8.3 验证无数据泄漏

**检查点**:
1. ✅ 滚动操作使用 `per_symbol=true`
2. ✅ 窗口计算向后看 (lookback)
3. ✅ 无 `shift(-N)` (未来数据)

---

## 9. 扩展性

### 9.1 添加新操作符

**步骤**:

1. **编辑 `wf_hpo.py:_apply_feature_program()`**:
```python
elif op == "ewma":
    col = step.get("col")
    span = step.get("span", 10)
    if col in df.columns:
        df[feat_name] = df[col].ewm(span=span).mean()
```

2. **更新文档** (`FEATURE_DSL_GUIDE.md`):
```markdown
#### `ewma` - 指数加权移动平均
**数学定义**: `feat[t] = EWMA(col, span)`
**用途**: 平滑噪音、趋势跟踪
```

3. **添加单元测试** (`scripts/test_feature_dsl.py`):
```python
def test_ewma():
    program = {
        "test_ewma": {
            "steps": [{"name": "feat_ewma", "op": "ewma", "col": "close", "span": 10}]
        }
    }
    # ... 测试逻辑
```

### 9.2 支持的未来操作符 (TODO)

| 操作符 | 描述 | 优先级 |
|--------|------|--------|
| `diff` | 一阶差分 | P1 |
| `pct_change` | 百分比变化 | P1 |
| `rank` | 横截面排名 | P2 |
| `zscore_cross` | 跨symbol Z分数 | P2 |
| `correlation` | 滚动相关性 | P3 |

---

## 10. 性能优化

### 10.1 计算成本

| 操作符 | 时间复杂度 | 内存占用 |
|--------|-----------|----------|
| `ratio` | O(N) | O(1) |
| `rolling_sum` | O(N*W) | O(W) |
| `rolling_zscore` | O(N*W) | O(W) |

**建议**:
- ✅ 限制程序总步骤 ≤ 10
- ✅ 滚动窗口 ≤ 100 bars
- ✅ 避免嵌套滚动操作

### 10.2 缓存策略

**当前**: 每次HPO trial重新计算
**未来优化**: 缓存常用特征程序结果

---

## 11. 故障排查

### 常见错误

#### 错误 1: "Unknown operator 'xxx'"
**原因**: 操作符名称拼写错误或不存在
**解决**: 检查 `feature_programs.json` 中 `"op"` 字段

#### 错误 2: "cannot join with no overlapping index names"
**原因**: `rolling_zscore` 未正确处理groupby索引
**解决**: 已修复 (wf_hpo.py:735-736)

#### 错误 3: "Column 'xxx' not found"
**原因**: 基础特征缺失 (如 `atr_5` 未生成)
**解决**: 检查 ClickHouse 表或运行 `pipelines/rebuild_features_1m.py`

---

## 12. 参考资料

### 相关文档
- `ML_SYSTEM_LEARNING_SUMMARY.md`: Phase 1 完整实施记录
- `PHASE1_IMPLEMENTATION_PLAN.md`: 原始实施计划
- `alpha_config_enhanced.json`: HPO配置示例

### 关键代码位置
- 特征DSL执行器: `wf_hpo.py:586-774`
- 特征程序定义: `feature_programs.json`
- HPO集成: `wf_hpo.py:859-862`
- 单元测试: `scripts/test_feature_dsl.py`

---

**最后更新**: 2025-11-12
**维护者**: Trading Team
**版本**: 1.0.0
