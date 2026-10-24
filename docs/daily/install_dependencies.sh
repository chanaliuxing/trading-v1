#!/bin/bash
#
# 安装每日运维工具依赖
#

set -e

TRADING_ROOT="."

echo "=========================================="
echo "安装每日运维工具依赖"
echo "=========================================="
echo ""

# 检查是否有 venv
if [ -d "$TRADING_ROOT/venv" ]; then
    echo "✅ 发现 venv 虚拟环境"
    PIP="$TRADING_ROOT/venv/bin/pip"
else
    echo "⚠️  未发现 venv，使用系统 pip"
    PIP="pip3"
fi

# 安装 psutil
echo "安装 psutil..."
$PIP install psutil

echo ""
echo "=========================================="
echo "✅ 依赖安装完成"
echo "=========================================="
echo ""
echo "现在可以运行健康检查："
echo "  cd $TRADING_ROOT/docs/daily"
echo "  ./system_health_check.py"
echo ""
