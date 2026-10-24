"""
演示在实际训练流程中使用断点续传

展示如何在 wf_hpo.py 类似的训练循环中:
1. 定期保存checkpoint
2. 从中断点恢复训练
3. 保留历史checkpoint

Author: Claude AI
Date: 2025-01-12
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import joblib
from datetime import datetime
from models.registry import build_model_from_policy


def train_with_checkpoints(
    X_train, y_train,
    policy: dict,
    epochs: int = 5,
    checkpoint_dir: str = "/tmp/checkpoints",
    resume_from: str = None
):
    """
    训练流程 + 断点续传支持

    Args:
        X_train, y_train: 训练数据
        policy: 模型配置
        epochs: 训练轮数(模拟多阶段训练)
        checkpoint_dir: checkpoint保存目录
        resume_from: 从指定checkpoint恢复(路径)
    """
    from pathlib import Path
    Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("训练流程 + 断点续传演示")
    print("=" * 80)

    # 1. 恢复或初始化
    start_epoch = 0
    if resume_from and Path(resume_from).exists():
        print(f"\n🔄 从checkpoint恢复: {resume_from}")
        pipe = joblib.load(resume_from)
        # 从文件名提取epoch (假设格式: model_epoch_3.joblib)
        try:
            start_epoch = int(Path(resume_from).stem.split("_")[-1])
            print(f"  ✅ 恢复成功,从 epoch {start_epoch} 继续")
        except:
            print("  ⚠️  无法解析epoch,从0开始")
    else:
        print("\n🆕 初始化新模型...")
        pipe, sw_ok = build_model_from_policy(policy["task"], policy)
        print(f"  ✅ 模型: {policy['model']['id']}")

    # 2. 训练循环 (模拟增量数据)
    print(f"\n📚 开始训练 ({start_epoch + 1} -> {epochs} epochs)...")
    print("-" * 80)

    batch_size = len(X_train) // epochs  # 每个epoch一批数据

    for epoch in range(start_epoch, epochs):
        # 模拟每个epoch使用不同数据batch (实际中可能是新数据)
        batch_start = epoch * batch_size
        batch_end = min((epoch + 1) * batch_size, len(X_train))
        X_batch = X_train[batch_start:batch_end]
        y_batch = y_train[batch_start:batch_end]

        # 训练 (实际应用中可能是增量训练或重新训练全部数据)
        if epoch == start_epoch:
            # 首次训练 (完整数据)
            pipe.fit(X_train[:batch_end], y_train[:batch_end])
        else:
            # 增量训练 (如果模型支持partial_fit)
            # 否则重新训练全部累积数据
            pipe.fit(X_train[:batch_end], y_train[:batch_end])

        train_r2 = pipe.score(X_train[:batch_end], y_train[:batch_end])

        print(f"  Epoch {epoch + 1}/{epochs} - "
              f"Samples: {batch_end}/{len(X_train)} - "
              f"Train R²: {train_r2:.4f}")

        # 3. 保存checkpoint
        checkpoint_path = f"{checkpoint_dir}/model_epoch_{epoch + 1}.joblib"
        joblib.dump(pipe, checkpoint_path)
        print(f"    💾 Checkpoint saved: {checkpoint_path}")

    print("-" * 80)
    print("✅ 训练完成!")

    # 4. 清理旧checkpoint (只保留最近3个)
    print("\n🧹 清理旧checkpoint...")
    checkpoints = sorted(Path(checkpoint_dir).glob("model_epoch_*.joblib"))
    if len(checkpoints) > 3:
        for old_cp in checkpoints[:-3]:
            old_cp.unlink()
            print(f"  🗑️  删除: {old_cp.name}")
    print(f"  ✅ 保留最近 {min(len(checkpoints), 3)} 个checkpoint")

    return pipe


def demo_resume_from_failure():
    """演示从失败点恢复"""
    print("\n" + "=" * 80)
    print("场景: 训练中断,从checkpoint恢复")
    print("=" * 80)

    # 准备数据
    np.random.seed(42)
    X_train = np.random.randn(1000, 20)
    y_train = X_train[:, 0] + 0.5 * X_train[:, 1] + np.random.randn(1000) * 0.1

    policy = {
        "task": "regression",
        "model": {
            "id": "ridge_cv",
            "scaler": "robust",
            "params": {"alphas": [0.1, 1.0, 10.0]}
        }
    }

    checkpoint_dir = "/tmp/demo_checkpoints"

    # 第一次训练 (模拟中断在epoch 3)
    print("\n🎬 第一次训练 (会在epoch 3中断)...")
    try:
        pipe = train_with_checkpoints(
            X_train, y_train,
            policy,
            epochs=3,  # 只训练3个epoch
            checkpoint_dir=checkpoint_dir
        )
        print("\n⚠️  模拟程序中断...")
    except Exception as e:
        print(f"\n❌ 训练失败: {e}")

    # 第二次训练 (从checkpoint恢复)
    print("\n🔄 重启程序,从最新checkpoint恢复...")
    import time
    time.sleep(1)

    # 找到最新checkpoint
    checkpoints = sorted(Path(checkpoint_dir).glob("model_epoch_*.joblib"))
    if checkpoints:
        latest_checkpoint = str(checkpoints[-1])
        print(f"📂 找到最新checkpoint: {latest_checkpoint}")

        # 继续训练
        pipe = train_with_checkpoints(
            X_train, y_train,
            policy,
            epochs=5,  # 训练到5个epoch
            checkpoint_dir=checkpoint_dir,
            resume_from=latest_checkpoint  # 从checkpoint恢复
        )

        # 验证最终模型
        print("\n" + "=" * 80)
        print("✅ 断点续传成功!")
        print("=" * 80)
        final_r2 = pipe.score(X_train, y_train)
        print(f"\n📊 最终模型性能: R² = {final_r2:.4f}")
    else:
        print("❌ 未找到checkpoint")


def demo_version_management():
    """演示checkpoint版本管理"""
    print("\n" + "=" * 80)
    print("场景: Checkpoint版本管理")
    print("=" * 80)

    np.random.seed(42)
    X = np.random.randn(500, 10)
    y = X[:, 0] + np.random.randn(500) * 0.1

    policy = {
        "task": "regression",
        "model": {
            "id": "lightgbm_reg",
            "params": {"n_estimators": 50, "random_state": 42, "verbose": -1}
        }
    }

    checkpoint_dir = "/tmp/versioned_checkpoints"
    Path(checkpoint_dir).mkdir(parents=True, exist_ok=True)

    # 训练多个版本
    print("\n📚 训练不同版本的模型...")
    configs = [
        {"n_estimators": 50, "learning_rate": 0.1},
        {"n_estimators": 100, "learning_rate": 0.05},
        {"n_estimators": 200, "learning_rate": 0.02},
    ]

    best_r2 = -np.inf
    best_checkpoint = None

    for i, config in enumerate(configs, 1):
        policy["model"]["params"] = {**config, "random_state": 42, "verbose": -1}
        pipe, _ = build_model_from_policy("regression", policy)
        pipe.fit(X, y)
        r2 = pipe.score(X, y)

        # 保存带版本号和时间戳
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        version_path = f"{checkpoint_dir}/lightgbm_v{i}_{timestamp}.joblib"
        joblib.dump(pipe, version_path)

        print(f"  版本 {i}: n_estimators={config['n_estimators']}, "
              f"lr={config['learning_rate']}, R²={r2:.4f}")
        print(f"    💾 {Path(version_path).name}")

        if r2 > best_r2:
            best_r2 = r2
            best_checkpoint = version_path

    print(f"\n🏆 最佳模型: {Path(best_checkpoint).name} (R²={best_r2:.4f})")

    # 创建符号链接指向最佳模型
    best_link = f"{checkpoint_dir}/best_model.joblib"
    import shutil
    if Path(best_link).exists():
        Path(best_link).unlink()
    shutil.copy2(best_checkpoint, best_link)
    print(f"  🔗 已创建快捷方式: best_model.joblib")


if __name__ == "__main__":
    # 演示1: 断点续传
    demo_resume_from_failure()

    # 演示2: 版本管理
    demo_version_management()

    print("\n" + "=" * 80)
    print("🎉 所有演示完成!")
    print("=" * 80)
