#!/usr/bin/env python3
"""LSTM训练速度基准测试"""

from models import LSTMModel
import pandas as pd
import numpy as np
import time

np.random.seed(42)
n_samples = 5000
n_features = 26  # 当前特征数
df = pd.DataFrame(np.random.randn(n_samples, n_features), columns=[f"f{i}" for i in range(n_features)])
y = np.random.randn(n_samples)

configs = {
    "V1 CPU": {"use_v2_architecture": False, "device": "cpu", "hidden": 64},
    "V1 GPU": {"use_v2_architecture": False, "device": "auto", "hidden": 64},
    "V2 GPU (双向)": {"use_v2_architecture": True, "bidirectional": True, "device": "auto", "hidden": 64},
    "V2 GPU (完整)": {"use_v2_architecture": True, "bidirectional": True, "use_attention": True, "layer_norm": True, "device": "auto", "hidden": 64},
}

print("=" * 70)
print("LSTM训练速度基准测试")
print(f"数据量: {n_samples} 样本 × {n_features} 特征")
print("=" * 70)
print(f"{'配置':<20} {'训练时间':<15} {'推理时间':<15} {'加速比'}")
print("=" * 70)

baseline_time = None
for name, config in configs.items():
    config.update({"lookback": 32, "epochs": 10, "batch_size": 256, "verbose": False})

    model = LSTMModel(task="reg", hparams=config)

    start = time.time()
    model.fit(df, y)
    train_time = time.time() - start

    start = time.time()
    model.predict(df)
    pred_time = time.time() - start

    if baseline_time is None:
        baseline_time = train_time
        speedup = "基线"
    else:
        speedup = f"{baseline_time / train_time:.1f}x"

    print(f"{name:<20} {train_time:>6.2f}s        {pred_time:>6.3f}s        {speedup}")

print("=" * 70)
print("\n💡 提示：")
print("  - V1 GPU vs V1 CPU: 显示GPU加速效果")
print("  - V2完整配置略慢但模型质量更高（双向+注意力+归一化）")
print("  - 实际项目中建议使用V2完整配置以获得最佳预测性能")
