# Transformer Models for Alpha Generation

**机构级三阶段 Transformer 升级路线**

本文档介绍三个新增的 Transformer 模型，用于替代/增强现有的 LSTM 模型。所有模型完全兼容现有的训练框架、OOF 验证和生产部署流程。

---

## 📋 目录

- [模型概览](#模型概览)
- [阶段 1: TransformerBaseline](#阶段-1-transformerbaseline)
- [阶段 2: ETSformer](#阶段-2-etsformer)
- [阶段 3: iTransformer](#阶段-3-itransformer)
- [快速开始](#快速开始)
- [超参数调优指南](#超参数调优指南)
- [性能评估标准](#性能评估标准)
- [部署到生产](#部署到生产)
- [常见问题](#常见问题)

---

## 模型概览

| 模型                  | 核心机制           | 最佳应用场景              | 预期提升                      |
| ------------------- | -------------- | ------------------- | ------------------------- |
| TransformerBaseline | 时间维度 Attention | 验证 Transformer 有效性 | IC 稳定性验证基准                |
| ETSformer           | 趋势/季节分解        | 趋势跟踪策略、周期性模式        | IC +10-20%, Sharpe +0.5-1 |
| iTransformer        | 特征维度 Attention | 多因子融合、跨资产信号         | 特征交互学习、更稳定的泛化能力           |

**执行顺序建议**：
1. 先用 `TransformerBaseline` 验证数据集适配性
2. 再用 `ETSformer` 捕捉时间结构
3. 最后用 `iTransformer` 进行多因子融合

---

## 阶段 1: TransformerBaseline

### 概述

Encoder-only Transformer（BERT 风格），用于验证 Transformer 在金融时序数据上的基础能力。

**核心特性**：
- ✅ 自注意力机制捕捉时间依赖
- ✅ 正弦位置编码
- ✅ 多种池化策略（mean, max, last）
- ✅ GPU 加速支持（CUDA/MPS）

### 使用示例

```python
from models import TransformerBaseline

# 推荐超参数配置
hparams = {
    "lookback": 32,           # 序列长度
    "d_model": 128,           # 嵌入维度 [64-256]
    "nhead": 4,               # 注意力头数 [4-8]
    "num_layers": 3,          # Encoder 层数 [2-4]
    "dim_feedforward": 256,   # FFN 维度 [256-512]
    "dropout": 0.1,           # Dropout [0.1-0.3]
    "pooling": "mean",        # 池化策略: mean/max/last
    "batch_size": 256,
    "epochs": 50,
    "lr": 1e-3,
    "patience": 6,
    "device": "auto"          # 自动检测 M1 GPU
}

# 训练
model = TransformerBaseline(task="reg", hparams=hparams)
model.fit(df_train, y_train, df_valid, y_valid)

# 预测
preds = model.predict(df_test)

# 保存/加载
model.save("models/saved/transformer_baseline")
model.load("models/saved/transformer_baseline")
```

### 性能指标

在 M1 32GB 上：
- 每 epoch 训练时间: ~2-4 秒
- 显存占用: ~2-4 GB
- 推荐样本量: ≥10k

### 验证清单

- [ ] 训练 Loss 曲线平滑
- [ ] Validation IC > 0.015
- [ ] 各 Fold IC 方差 < 30%
- [ ] Attention 权重在市场拐点聚集

---

## 阶段 2: ETSformer

### 概述

基于 Exponential Smoothing 的 Transformer，显式建模趋势和季节性成分。

**论文**：[ETSformer (arxiv.org/abs/2202.01381)](https://arxiv.org/abs/2202.01381)

**核心特性**：
- ✅ 分离 Trend 和 Seasonal 分支
- ✅ 指数平滑层用于长期依赖
- ✅ 可学习的融合参数 α
- ✅ 对非平稳序列更鲁棒

### 使用示例

```python
from models import ETSformer

hparams = {
    "lookback": 32,
    "d_model": 128,           # [128-256]
    "nhead": 4,               # [4-8]
    "num_layers": 2,          # [2-3] 比 Baseline 少
    "dim_feedforward": 256,
    "dropout": 0.1,
    "alpha_init": 0.5,        # 趋势/季节融合权重 [0.2-0.8]
    "batch_size": 256,
    "epochs": 50,
    "lr": 1e-3,
    "patience": 6
}

model = ETSformer(task="reg", hparams=hparams)
model.fit(df_train, y_train, df_valid, y_valid)
```

### 改进点

| 维度   | 相比 Baseline        |
| ---- | ------------------ |
| IC   | +10-20%            |
| 过拟合  | 下降 30%（更平滑的曲线）     |
| 稳定性  | Fold 间方差降低 20-30% |
| 趋势捕捉 | 对周/月效应更敏感          |

### 最佳应用场景

- ✅ 趋势跟踪策略
- ✅ 有明显周期性的资产（如商品期货）
- ✅ 日内策略中的微观趋势捕捉

---

## 阶段 3: iTransformer

### 概述

**反转 Transformer**：在特征维度而非时间维度应用 Attention，专为多变量时序设计。

**论文**：[iTransformer (arxiv.org/abs/2310.06625)](https://arxiv.org/abs/2310.06625)

**核心特性**：
- ✅ 特征间交互学习
- ✅ 可学习特征嵌入
- ✅ 自适应特征聚合
- ✅ 自动发现因子相关性

### 使用示例

```python
from models import iTransformer

hparams = {
    "lookback": 32,
    "d_model": 128,           # [128-256]
    "nhead": 4,               # [4-8]
    "num_layers": 3,          # [2-4]
    "dim_feedforward": 512,   # 更大的 FFN 用于特征交互
    "dropout": 0.1,           # [0.1-0.2]
    "batch_size": 256,
    "epochs": 50,
    "lr": 1e-3,
    "patience": 6
}

model = iTransformer(task="reg", hparams=hparams)
model.fit(df_train, y_train, df_valid, y_valid)
```

### 优势

| 优势       | 说明                  |
| -------- | ------------------- |
| 特征交互学习   | 自动发现哪些因子相互强化        |
| 特征选择     | Attention 权重可用于特征重要性 |
| 泛化能力     | 对特征数量变化更鲁棒          |
| 多资产适配性   | 适合跨资产组合优化           |

### 最佳应用场景

- ✅ 50+ 特征的多因子模型
- ✅ 跨资产 Alpha 融合
- ✅ 特征工程中的自动交互发现
- ✅ 组合优化中的资产相关性建模

### 样本需求

- 推荐: ≥100k 样本（用于稳定的特征交互学习）
- 最少: ≥10k 样本（需要更强正则化）

---

## 快速开始

### 1. 基础测试

运行测试脚本验证所有模型可用：

```bash
python temp/test_transformers.py
```

预期输出：
```
Testing TransformerBaseline...
✅ TransformerBaseline Results:
   Train IC: 0.XXXX
   Valid IC: 0.XXXX
   ✓ Save/Load verified

Testing ETSformer...
✅ ETSformer Results:
   Train IC: 0.XXXX
   Valid IC: 0.XXXX
   ✓ Save/Load verified

Testing iTransformer...
✅ iTransformer Results:
   Train IC: 0.XXXX
   Valid IC: 0.XXXX
   ✓ Save/Load verified
```

### 2. 集成到现有流程

**方式 1：直接替换 LSTM**

```python
# 原代码
from models import LSTMModel
model = LSTMModel(task="reg", hparams=lstm_hparams)

# 替换为 Transformer
from models import TransformerBaseline
model = TransformerBaseline(task="reg", hparams=transformer_hparams)
# 其余代码保持不变！
```

**方式 2：在 OOF 流程中使用**

修改 `tools/nightly_oof_flow.py` 中的模型类型：

```python
# 在 HPO 配置中指定
config = {
    "model_type": "TransformerBaseline",  # 或 ETSformer, iTransformer
    "hparams": {
        "lookback": 32,
        "d_model": 128,
        # ... 其他参数
    }
}
```

### 3. HPO 优化

使用现有 HPO 框架优化超参数：

```python
# config/hpo_transformer.json
{
  "model_type": "TransformerBaseline",
  "param_space": {
    "d_model": {"type": "int", "low": 64, "high": 256, "step": 64},
    "nhead": {"type": "categorical", "choices": [4, 8]},
    "num_layers": {"type": "int", "low": 2, "high": 4},
    "dropout": {"type": "float", "low": 0.1, "high": 0.3}
  }
}
```

运行 HPO：
```bash
python tools/nightly_hpo.py --config config/hpo_transformer.json --samples 100
```

---

## 超参数调优指南

### TransformerBaseline

| 参数                | 推荐范围       | 优先级  | 说明               |
| ----------------- | ---------- | ---- | ---------------- |
| `d_model`         | 64-256     | ⭐⭐⭐  | 模型容量的核心参数        |
| `nhead`           | 4-8        | ⭐⭐   | 4 对小数据集友好        |
| `num_layers`      | 2-4        | ⭐⭐   | 更多层 = 更强表达但易过拟合  |
| `dropout`         | 0.1-0.3    | ⭐⭐⭐  | 防止过拟合的关键         |
| `pooling`         | mean/last  | ⭐    | mean 通常更稳定       |
| `dim_feedforward` | 256-512    | ⭐    | 通常设为 d_model 的 2x |

### ETSformer

| 参数          | 推荐范围     | 优先级  | 说明         |
| ----------- | -------- | ---- | ---------- |
| `alpha_init`| 0.2-0.8  | ⭐⭐⭐  | 趋势/季节融合权重  |
| `num_layers`| 2-3      | ⭐⭐   | 2层通常足够     |
| `d_model`   | 128-256  | ⭐⭐   | 增加以捕捉更复杂模式 |

### iTransformer

| 参数                | 推荐范围   | 优先级  | 说明                |
| ----------------- | ------ | ---- | ----------------- |
| `dim_feedforward` | 512    | ⭐⭐⭐  | 特征交互需要更大的 FFN    |
| `d_model`         | 128-256| ⭐⭐   | 特征多时增加            |
| `dropout`         | 0.1-0.2| ⭐⭐⭐  | 强正则化防止特征维度上的过拟合   |

### 调优策略

1. **粗调 (Coarse Tuning)**
   - 固定 `lookback=32, batch_size=256, lr=1e-3`
   - 调整 `d_model`, `num_layers`, `dropout`
   - 目标：找到稳定的基础配置

2. **精调 (Fine Tuning)**
   - 固定步骤 1 的最优配置
   - 调整学习率、批量大小、early stopping
   - 目标：最大化 OOF IC 和 Sharpe

3. **验证**
   - 在多个时间窗口上验证稳定性
   - 检查 Fold 间 IC 方差
   - Attention 可视化检查合理性

---

## 性能评估标准

### 机构级指标

| 维度     | 指标                  | 目标                    |
| ------ | ------------------- | --------------------- |
| 预测准确度  | OOF IC              | > 0.015 (基准)          |
| 收益稳定性  | Sharpe              | > 2 或提升 ≥ 0.5        |
| 稳定性    | Fold IC Variance    | < 30%                 |
| 计算效率   | Training Time/Epoch | ≤ 5 min (M1 32GB)     |
| 可解释性   | Attention Pattern   | 在市场拐点聚集               |

### 对比测试

使用相同数据集测试所有模型：

```bash
# 运行对比测试
python tools/model_comparison.py \
  --models LSTMModel,TransformerBaseline,ETSformer,iTransformer \
  --symbols AAPL,TSLA,NVDA \
  --window-days 30 \
  --out runs/model_comparison/$(date +%Y%m%d)
```

---

## 部署到生产

### 1. 模型保存

所有 Transformer 模型使用与 LSTM 相同的保存格式：

```python
# 保存
model.save("models/production/transformer_baseline_v1")

# 目录结构
models/production/transformer_baseline_v1/
├── meta.json         # 元数据（超参数、特征列等）
└── model.pt          # PyTorch 模型权重
```

### 2. 加载到推理服务

```python
from models import TransformerBaseline

# 生产推理
model = TransformerBaseline(task="reg")
model.load("models/production/transformer_baseline_v1")

# 实时预测
features = fetch_latest_features(symbol="AAPL")
signal = model.predict(features)
```

### 3. 版本管理

使用 MLflow 或 Git 管理模型版本：

```bash
# 记录模型元数据
mlflow log-artifact models/production/transformer_baseline_v1
mlflow log-metric "oof_ic" 0.0234
mlflow log-metric "sharpe" 2.5
mlflow log-param "model_type" "TransformerBaseline"
```

---

## 常见问题

### Q1: 训练时显存不足？

**A**: 减小 `batch_size` 和 `d_model`：

```python
hparams = {
    "batch_size": 128,   # 从 256 降到 128
    "d_model": 64,       # 从 128 降到 64
    "num_layers": 2      # 减少层数
}
```

### Q2: 模型过拟合严重？

**A**: 增强正则化：

```python
hparams = {
    "dropout": 0.3,        # 增加 dropout
    "weight_decay": 1e-4,  # 添加 L2 正则
    "patience": 3          # 更早停止
}
```

### Q3: 三个模型如何选择？

**A**: 根据场景选择：

| 场景                | 推荐模型                |
| ----------------- | ------------------- |
| 快速验证/基准测试         | TransformerBaseline |
| 趋势跟踪、有周期性的资产      | ETSformer           |
| 多因子（50+特征）、跨资产融合 | iTransformer        |
| 生产部署（需要稳定性）       | 三者混合 Ensemble      |

### Q4: 性能比 LSTM 差？

**A**: 检查以下因素：

1. **数据量**: Transformer 需要更多数据（≥10k 样本）
2. **序列长度**: 确保 `lookback` 足够捕捉时间模式（推荐 32-64）
3. **特征质量**: Transformer 对噪声敏感，确保特征经过良好工程化
4. **超参数**: 尝试 HPO 优化

### Q5: M1 GPU 未被使用？

**A**: 检查 PyTorch MPS 后端：

```python
import torch
print(torch.backends.mps.is_available())  # 应返回 True

# 在模型配置中显式指定
hparams = {"device": "mps"}
```

### Q6: 如何可视化 Attention？

**A**: 访问模型内部的 Attention 权重：

```python
# 训练后提取 attention weights (高级功能)
model._model.encoder.layers[0].self_attn
# 绘制热力图分析哪些时间步互相关注
```

---

## 总结

三个 Transformer 模型的定位：

| 阶段 | 模型                  | 目标                | 成功标准               |
| -- | ------------------- | ----------------- | ------------------ |
| ①  | TransformerBaseline | 验证 Attention 有效性 | IC 稳定，训练平滑          |
| ②  | ETSformer           | 捕捉时间结构            | IC +10-20%, 过拟合降低  |
| ③  | iTransformer        | 多因子融合             | 特征交互学习，泛化能力提升      |

**推荐路径**：
1. 用 `TransformerBaseline` 建立基准
2. 用 `ETSformer` 优化时间建模
3. 用 `iTransformer` 处理复杂特征交互
4. 最终组合三者形成 Ensemble

---

**文档维护者**: Trading Team
**最后更新**: 2025-11-07
**版本**: 1.0
