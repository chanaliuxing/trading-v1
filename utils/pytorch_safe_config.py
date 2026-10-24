"""
PyTorch 安全配置模块
防止 Mac M1 上的 OpenMP LSTM 崩溃

使用方法：
    在所有使用 PyTorch 的脚本开头导入:
    from utils.pytorch_safe_config import configure_safe_pytorch
    configure_safe_pytorch()
"""
import os
import sys
import torch
from typing import Dict, Any

_configured = False


def configure_safe_pytorch(verbose: bool = True) -> None:
    """
    配置安全的 PyTorch 运行环境

    Args:
        verbose: 是否打印配置信息

    解决的问题：
        - PyTorch LSTM CPU 模式下 OpenMP 多线程崩溃
        - Mac M1 ARM 架构上的线程调度冲突
        - 大批量 tensor 分配导致的内存访问错误
    """
    global _configured

    if _configured:
        if verbose:
            print("⚠️  PyTorch 安全配置已启用，跳过重复配置")
        return

    if verbose:
        print("=" * 80)
        print("🛡️  配置 PyTorch 安全运行环境（防止崩溃）")
        print("=" * 80)

    # 1. 设置环境变量（限制 OpenMP/MKL 线程）
    env_vars = {
        "OMP_NUM_THREADS": "1",
        "OMP_DYNAMIC": "FALSE",
        "MKL_NUM_THREADS": "1",
        "MKL_THREADING_LAYER": "GNU",
        "PYTORCH_ENABLE_MPS_FALLBACK": "1"
    }

    if verbose:
        print("\n1. 环境变量配置:")

    for key, value in env_vars.items():
        old_value = os.environ.get(key)
        os.environ[key] = value
        if verbose:
            status = "✅ 新设置" if old_value != value else "✓ 已配置"
            print(f"   {key} = {value}  {status}")

    # 2. 限制 PyTorch 线程数
    torch.set_num_threads(1)

    if verbose:
        print("\n2. PyTorch 线程限制:")
        print(f"   ✅ torch.set_num_threads(1)")
        print(f"   当前线程数: {torch.get_num_threads()}")

    # 3. 环境信息
    if verbose:
        print("\n3. 环境信息:")
        print(f"   PyTorch: {torch.__version__}")
        print(f"   平台: {sys.platform}")
        print(f"   MPS 可用: {torch.backends.mps.is_available()}")

    _configured = True

    if verbose:
        print("\n" + "=" * 80)
        print("✅ 安全配置完成！")
        print("=" * 80)
        print()


def get_safe_hparams(size: str = "medium") -> Dict[str, Any]:
    """
    获取安全的 LSTM 超参数配置

    Args:
        size: 模型大小 ("small", "medium", "large")

    Returns:
        安全的超参数字典
    """
    configs = {
        "small": {
            "use_v2_architecture": True,
            "bidirectional": True,
            "hidden": 32,
            "num_layers": 1,
            "lookback": 10,
            "epochs": 5,
            "batch_size": 64,
            "device": "cpu",
            "verbose": False
        },
        "medium": {
            "use_v2_architecture": True,
            "bidirectional": True,
            "hidden": 64,
            "num_layers": 2,
            "lookback": 20,
            "epochs": 10,
            "batch_size": 128,
            "device": "cpu",
            "verbose": False
        },
        "large": {
            "use_v2_architecture": True,
            "bidirectional": True,
            "hidden": 128,
            "num_layers": 2,
            "lookback": 30,
            "epochs": 20,
            "batch_size": 256,
            "device": "cpu",
            "verbose": False
        }
    }

    return configs.get(size, configs["medium"])


def safe_dataloader_kwargs() -> Dict[str, Any]:
    """
    获取安全的 DataLoader 配置

    Returns:
        DataLoader 关键字参数
    """
    return {
        "num_workers": 0,  # 避免多进程问题
        "pin_memory": False,  # Mac 上不需要
        "persistent_workers": False
    }


# 自动配置（仅在直接导入时）
if __name__ != "__main__":
    # 静默配置
    configure_safe_pytorch(verbose=False)
