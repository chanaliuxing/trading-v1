# Phase 1 紧急修复 - 机构级ML系统升级

**创建时间**: 2025-11-12
**目标**: 将模型IC从0.05提升到0.08+，AUC从0.589提升到0.68+
**预计完成时间**: 1周

---

## 学习总结：关键发现

### ✅ 系统做对的地方

1. **时间序列分割** - Walk-Forward CV + Embargo机制防止数据泄漏
2. **模型验证指标** - IC/IR/AUC/Sharpe分层评估
3. **Optuna优化框架** - TPE Sampler + MedianPruner
4. **检查点恢复机制** - 支持断点续训

### ❌ 关键问题

1. **特征未标准化** - 97%特征使用原始值，导致梯度不稳定
2. **标签构建有偏差** - 使用close而非next bar open，未扣除交易成本
3. **LSTM缺失标准化** - 直接使用原始序列，收敛极慢
4. **Embargo=0天** - 标签泄漏风险

5. **数据质量** - `features_1m`表缺失关键字段（open/high/low），导致97%特征为NULL

---

## Phase 1: 紧急修复清单

### 任务1: 修复数据质量 ⚠️ **最优先**

**问题**: `ema_9`等97%字段缺失

**解决方案**:
```bash
# 步骤1: 检查数据表schema
python -c "
from clickhouse_driver import Client
c = Client(host='localhost', port=9000)
result = c.execute('SELECT name, type FROM system.columns WHERE database = \\'trading\\' AND table = \\'features_1m\\' ORDER BY name')
for row in result:
    print(f'{row[0]:20s} {row[1]}')
" > /tmp/features_1m_schema.txt

# 步骤2: 重新生成特征（如果missing）
python pipelines/rebuild_features_1m.py --symbols TSLA --start 2025-10-01 --end 2025-11-08

# 步骤3: 验证完整性
python scripts/check_data_quality.py --symbol TSLA --start 2025-10-01 --end 2025-11-08
```

**预期结果**:
- `ema_9`完整性: 3% → 95%+
- 可用样本数: 33,960行全部可用

---

### 任务2: 实现特征标准化 ⭐⭐⭐⭐⭐

**创建文件**: `libs/features/feature_standardizer.py`

```python
#!/usr/bin/env python3
"""特征标准化模块 - 机构级最佳实践"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import RobustScaler
import pickle

class FeatureStandardizer:
    """
    特征标准化器
    使用RobustScaler（基于中位数+IQR，抗异常值）
    """

    def __init__(self):
        self.scaler = RobustScaler()
        self.feature_cols = None
        self.fitted = False

    def fit(self, df: pd.DataFrame, feature_cols: list):
        """拟合scaler（仅在训练集上）"""
        self.feature_cols = feature_cols
        self.scaler.fit(df[feature_cols])
        self.fitted = True

    def transform(self, df: pd.DataFrame):
        """转换特征"""
        if not self.fitted:
            raise ValueError("Must fit() before transform()")

        df_scaled = df.copy()
        df_scaled[self.feature_cols] = self.scaler.transform(df[self.feature_cols])
        return df_scaled

    def fit_transform(self, df: pd.DataFrame, feature_cols: list):
        """拟合并转换"""
        self.fit(df, feature_cols)
        return self.transform(df)

    def save(self, path: str):
        """保存scaler"""
        with open(path, 'wb') as f:
            pickle.dump({'scaler': self.scaler, 'features': self.feature_cols}, f)

    def load(self, path: str):
        """加载scaler"""
        with open(path, 'rb') as f:
            data = pickle.load(f)
            self.scaler = data['scaler']
            self.feature_cols = data['features']
            self.fitted = True
```

**集成到训练流程**:

修改 `wf_hpo.py` 的训练函数（约L669）:

```python
# wf_hpo.py L669 (添加)
from libs.features.feature_standardizer import FeatureStandardizer

def run_single_backtest(...):
    # ... existing code ...

    # ✅ 添加特征标准化
    feature_cols = [col for col in df_is.columns if col not in ['ts', 'symbol', 'label_*']]

    standardizer = FeatureStandardizer()
    df_is_scaled = standardizer.fit_transform(df_is, feature_cols)
    df_oos_scaled = standardizer.transform(df_oos)

    # 保存scaler到模型目录
    standardizer.save(f"{model_output_dir}/scaler.pkl")

    # ... 继续使用 df_is_scaled, df_oos_scaled ...
```

**预期效果**:
- XGBoost AUC: 0.589 → 0.65+ (+10%)
- LSTM收敛速度: 50 epochs → 10 epochs (5x)
- IC: 0.05 → 0.08+ (+60%)

---

### 任务3: 修复LSTM标准化 ⭐⭐⭐⭐

**修改文件**: `models/lstm_model.py`

```python
# models/lstm_model.py (修改)
from sklearn.preprocessing import RobustScaler

class LSTMModel(ModelAdapter):
    def __init__(self, ...):
        super().__init__(...)
        self.scaler = None  # ✅ 新增

    def fit(self, df_train, y_train, df_valid=None, y_valid=None, **kwargs):
        # ✅ 步骤1: 拟合scaler (仅用训练集)
        from sklearn.preprocessing import RobustScaler
        self.scaler = RobustScaler()
        feature_cols = self.feature_cols()
        self.scaler.fit(df_train[feature_cols])

        # ✅ 步骤2: 标准化
        df_train_scaled = df_train.copy()
        df_train_scaled[feature_cols] = self.scaler.transform(df_train[feature_cols])

        train_loader = self._build_dataloader(df_train_scaled, y_train, shuffle=True)

        if df_valid is not None:
            df_valid_scaled = df_valid.copy()
            df_valid_scaled[feature_cols] = self.scaler.transform(df_valid[feature_cols])
            valid_loader = self._build_dataloader(df_valid_scaled, y_valid, shuffle=False)

        # ... 继续训练 ...

    def predict(self, df: pd.DataFrame):
        # ✅ 预测时使用相同scaler
        df_scaled = df.copy()
        feature_cols = self.feature_cols()
        df_scaled[feature_cols] = self.scaler.transform(df[feature_cols])

        loader = self._build_dataloader(df_scaled, None, shuffle=False)
        # ... 返回预测 ...

    def save_checkpoint(self, path: str):
        """保存模型+scaler"""
        torch.save({
            'model_state_dict': self.model.state_dict(),
            'scaler': self.scaler,  # ✅ 保存scaler
            'feature_cols': self.feature_cols(),
        }, path)

    def load_checkpoint(self, path: str):
        """加载模型+scaler"""
        checkpoint = torch.load(path)
        self.model.load_state_dict(checkpoint['model_state_dict'])
        self.scaler = checkpoint['scaler']  # ✅ 加载scaler
```

**预期效果**:
- LSTM AUC: 0.55 → 0.72+ (+30%)
- 训练稳定性: 避免NaN/Inf
- 梯度正常: 无爆炸/消失

---

### 任务4: 增加Embargo到2天 ⭐⭐⭐

**修改文件**: `alpha_config.json`

```json
{
  "hpo": {
    "fold_generator": {
      "embargo_days": 2.0,  // ✅ 从0改为2
      "gap_days": 1.0       // ✅ 增加purge gap
    }
  }
}
```

**预期效果**:
- 减少标签泄漏风险
- OOS性能更真实
- IC标准差减小

---

### 任务5: 改进标签构建（长期）⭐⭐⭐⭐

**注意**: 需要数据表有`open`字段，当前`features_1m`缺失

**未来实施** (当数据表完善后):

```sql
-- pipelines/generate_labels_v2.sql
WITH forward_returns AS (
    SELECT
        ts,
        symbol,
        close AS current_close,
        lead(open, 1) OVER w AS entry_price,  -- ✅ 使用next bar open
        lead(close, 10) OVER w AS exit_price,

        -- ✅ 净收益（扣除成本5bps）
        (exit_price - entry_price) / entry_price - 0.0005 AS net_return,

        -- ✅ 检测跨时段
        CASE
            WHEN toHour(lead(ts, 1) OVER w) < 9 OR toHour(lead(ts, 1) OVER w) >= 16 THEN 1
            ELSE 0
        END AS crosses_session
    FROM features_1m
    WINDOW w AS (PARTITION BY symbol ORDER BY ts)
)
SELECT
    f.*,
    CASE
        WHEN fr.crosses_session = 1 THEN NULL  -- 过滤跨时段
        WHEN fr.net_return >= 0.001 THEN 1     -- +10bps阈值
        WHEN fr.net_return <= -0.001 THEN 0    -- -10bps阈值
        ELSE NULL  -- 丢弃噪音
    END AS label_h10
FROM features_1m f
LEFT JOIN forward_returns fr ON f.ts = fr.ts
```

**预期效果**:
- 标签信噪比: 51% → 65%+
- IC: 0.08 → 0.10+

---

## 实施顺序（本周）

### Day 1-2: 数据质量修复
```bash
# 1. 重新生成特征
python pipelines/rebuild_features_1m.py --symbols TSLA --start 2025-10-01 --end 2025-11-08

# 2. 验证
python scripts/check_data_quality.py --symbol TSLA
```

### Day 3-4: 特征标准化
```bash
# 1. 创建FeatureStandardizer模块
cp PHASE1_IMPLEMENTATION_PLAN.md libs/features/feature_standardizer.py

# 2. 修改wf_hpo.py集成标准化

# 3. 修改models/lstm_model.py

# 4. 测试
python scripts/train_improved_baseline.py
```

### Day 5: Embargo修复
```bash
# 1. 修改alpha_config.json
sed -i 's/"embargo_days": 0/"embargo_days": 2.0/' alpha_config.json

# 2. 重新运行HPO
python wf_hpo.py --config alpha_config.json
```

### Day 6-7: 验证与对比
```bash
# 对比Phase 1前后
python scripts/compare_models.py \
    --baseline models/xgb_h30_tsla_selected_features \
    --improved models/xgb_phase1_improved
```

---

## 成功标准

| 指标 | 当前 | Phase 1目标 | 验证方法 |
|------|------|-------------|----------|
| IC | 0.05 | 0.08+ | 5折CV平均 |
| AUC | 0.589 | 0.65+ | 5折CV平均 |
| IR | 0.8 | 1.5+ | 每日IC稳定性 |
| 特征完整性 | 3% (ema_9) | 95%+ | 数据质量检查 |
| LSTM收敛 | 50 epochs | 10 epochs | Tensorboard日志 |

---

## Phase 2 预告（2-4周后）

1. 添加微观结构特征（价格冲击、有效价差）
2. 实现订单流特征（VPIN）
3. 特征选择优化（Permutation Importance）
4. 增加历史数据（1个月 → 6个月）

**预期Phase 2收益**:
- IC: 0.08 → 0.12+
- AUC: 0.68 → 0.75+
- Sharpe: 1.2 → 1.8+

---

**最后更新**: 2025-11-12
**负责人**: Trading Team
**状态**: 🚀 Ready to Execute
