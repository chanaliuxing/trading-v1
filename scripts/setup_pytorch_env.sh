#!/bin/bash
# PyTorch 安全环境配置
# 防止 Mac M1 上的 OpenMP LSTM 崩溃
#
# 使用方法：
#   source scripts/setup_pytorch_env.sh
#
# 或添加到 ~/.zshrc:
#   source ~/Documents/trading/scripts/setup_pytorch_env.sh

echo "🛡️  配置 PyTorch 安全环境变量..."

# 限制 OpenMP 线程数
export OMP_NUM_THREADS=1
export OMP_DYNAMIC=FALSE

# 限制 MKL 线程数
export MKL_NUM_THREADS=1
export MKL_THREADING_LAYER=GNU

# PyTorch MPS 回退
export PYTORCH_ENABLE_MPS_FALLBACK=1

# 禁用 PyTorch JIT（可选，更稳定）
# export PYTORCH_JIT=0

echo "✅ 环境变量已配置:"
echo "   OMP_NUM_THREADS=$OMP_NUM_THREADS"
echo "   OMP_DYNAMIC=$OMP_DYNAMIC"
echo "   MKL_NUM_THREADS=$MKL_NUM_THREADS"
echo "   MKL_THREADING_LAYER=$MKL_THREADING_LAYER"
echo ""
echo "💡 提示: 可将以下行添加到 ~/.zshrc 以永久生效:"
echo "   source ~/Documents/trading/scripts/setup_pytorch_env.sh"
