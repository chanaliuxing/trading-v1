#!/usr/bin/env python3
# scripts/test_model_registry.py
"""
测试机构级统一模型注册表

验证所有模型:
- 线性: Lasso, ElasticNet, Ridge, LogisticRegression
- 集成: RandomForest, ExtraTrees, HistGBDT
- 梯度提升: XGBoost, LightGBM, CatBoost

测试场景:
1. 模型构建 (build_model_from_policy)
2. 标准化器 (StandardScaler / RobustScaler)
3. 特征预选 (Lasso preselection)
4. 样本权重支持
5. 训练与预测
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.registry import build_model_from_policy, get_supported_models

print("=" * 80)
print("机构级统一模型注册表测试")
print("=" * 80)

# ========== 生成模拟数据 ==========

np.random.seed(42)
n_samples = 500
n_features = 20

X_train = np.random.randn(n_samples, n_features).astype(np.float32)
y_train_reg = X_train[:, 0] + 0.5 * X_train[:, 1] + 0.1 * np.random.randn(n_samples)
y_train_clf = (y_train_reg > 0).astype(int)

X_test = np.random.randn(100, n_features).astype(np.float32)
y_test_reg = X_test[:, 0] + 0.5 * X_test[:, 1] + 0.1 * np.random.randn(100)
y_test_clf = (y_test_reg > 0).astype(int)

# Sample weights
w_train = np.random.uniform(0.5, 2.0, n_samples)

print(f"\n数据生成:")
print(f"  - 训练集: {X_train.shape}")
print(f"  - 测试集: {X_test.shape}")
print(f"  - 回归目标范围: [{y_train_reg.min():.2f}, {y_train_reg.max():.2f}]")
print(f"  - 分类目标分布: {np.bincount(y_train_clf)}")

# ========== 测试模型清单 ==========

test_cases = [
    # 线性回归模型
    {
        "name": "ElasticNet (标准化)",
        "task": "regression",
        "policy": {
            "task": "regression",
            "model": {
                "id": "elasticnet_cv",
                "scaler": "standard",
                "params": {
                    "l1_ratio_grid": [0.1, 0.5, 0.9],
                    "alphas": [0.001, 0.01, 0.1],
                    "cv": 3,
                    "max_iter": 2000
                }
            }
        }
    },
    {
        "name": "Ridge (鲁棒化 + 权重)",
        "task": "regression",
        "policy": {
            "task": "regression",
            "model": {
                "id": "ridge_cv",
                "scaler": "robust",
                "params": {
                    "alphas": [0.1, 1.0, 10.0]
                }
            }
        },
        "use_weight": True
    },
    {
        "name": "Lasso特征选择 + XGBoost",
        "task": "regression",
        "policy": {
            "task": "regression",
            "preselection": {
                "enabled": True,
                "model_id": "lasso_cv",
                "scaler": "robust",
                "keep_top_k": 10,
                "alphas": [0.001, 0.01],
                "cv": 3,
                "max_iter": 2000
            },
            "model": {
                "id": "xgboost_reg",
                "scaler": "none",
                "params": {
                    "max_depth": 3,
                    "n_estimators": 50,
                    "learning_rate": 0.1
                }
            }
        },
        "use_weight": True
    },

    # 逻辑回归 (分类)
    {
        "name": "LogisticRegression L2",
        "task": "binary",
        "policy": {
            "task": "binary",
            "model": {
                "id": "logreg_saga_l2",
                "scaler": "standard",
                "params": {
                    "C": 1.0,
                    "max_iter": 2000
                }
            }
        }
    },

    # 树模型
    {
        "name": "HistGradientBoosting",
        "task": "regression",
        "policy": {
            "task": "regression",
            "model": {
                "id": "hgbt_reg",
                "scaler": "none",
                "params": {
                    "max_depth": 4,
                    "learning_rate": 0.1,
                    "max_iter": 100
                }
            }
        },
        "use_weight": True
    },
    {
        "name": "RandomForest",
        "task": "regression",
        "policy": {
            "task": "regression",
            "model": {
                "id": "rf_reg",
                "scaler": "none",
                "params": {
                    "n_estimators": 50,
                    "max_depth": 5
                }
            }
        },
        "use_weight": True
    },
]

# 如果LightGBM/CatBoost可用，添加测试
try:
    import lightgbm
    test_cases.append({
        "name": "LightGBM",
        "task": "regression",
        "policy": {
            "task": "regression",
            "model": {
                "id": "lightgbm_reg",
                "scaler": "none",
                "params": {
                    "num_leaves": 31,
                    "learning_rate": 0.05,
                    "n_estimators": 100,
                    "num_threads": 4
                }
            }
        },
        "use_weight": True
    })
except ImportError:
    print("\n⚠️  LightGBM not installed (pip install lightgbm)")

try:
    import catboost
    test_cases.append({
        "name": "CatBoost",
        "task": "regression",
        "policy": {
            "task": "regression",
            "model": {
                "id": "catboost_reg",
                "scaler": "none",
                "params": {
                    "iterations": 100,
                    "depth": 4,
                    "learning_rate": 0.05,
                    "thread_count": 4
                }
            }
        },
        "use_weight": True
    })
except ImportError:
    print("⚠️  CatBoost not installed (pip install catboost)")

# ========== 运行测试 ==========

results = []

for i, tc in enumerate(test_cases, 1):
    print(f"\n{'=' * 80}")
    print(f"测试 {i}/{len(test_cases)}: {tc['name']}")
    print("=" * 80)

    try:
        # 1. 构建模型
        pipe, sw_ok = build_model_from_policy(tc["task"], tc["policy"])
        print(f"✓ 模型构建成功")
        print(f"  - 任务: {tc['task']}")
        print(f"  - 支持样本权重: {sw_ok}")
        print(f"  - Pipeline步骤: {len(pipe.steps)}")

        # 2. 训练
        X, y, w = X_train, y_train_reg if tc["task"] == "regression" else y_train_clf, w_train

        use_weight = tc.get("use_weight", False) and sw_ok
        if use_weight:
            pipe.fit(X, y, model__sample_weight=w)
            print(f"✓ 训练完成 (with sample_weight)")
        else:
            pipe.fit(X, y)
            print(f"✓ 训练完成 (no sample_weight)")

        # 3. 预测
        X_t, y_t = X_test, y_test_reg if tc["task"] == "regression" else y_test_clf
        y_pred = pipe.predict(X_t)
        print(f"✓ 预测完成: {y_pred.shape}")

        # 4. 评估
        if tc["task"] == "regression":
            from sklearn.metrics import mean_squared_error, r2_score
            mse = mean_squared_error(y_t, y_pred)
            r2 = r2_score(y_t, y_pred)
            print(f"\n性能指标:")
            print(f"  - MSE: {mse:.6f}")
            print(f"  - R²: {r2:.4f}")
            results.append({"name": tc["name"], "task": tc["task"], "mse": mse, "r2": r2, "status": "PASS"})
        else:
            from sklearn.metrics import accuracy_score, roc_auc_score
            acc = accuracy_score(y_t, (y_pred > 0.5).astype(int))

            # AUC (需要概率)
            try:
                y_proba = pipe.predict_proba(X_t)[:, 1]
                auc = roc_auc_score(y_t, y_proba)
                print(f"\n性能指标:")
                print(f"  - Accuracy: {acc:.4f}")
                print(f"  - AUC: {auc:.4f}")
                results.append({"name": tc["name"], "task": tc["task"], "acc": acc, "auc": auc, "status": "PASS"})
            except:
                print(f"\n性能指标:")
                print(f"  - Accuracy: {acc:.4f}")
                results.append({"name": tc["name"], "task": tc["task"], "acc": acc, "status": "PASS"})

    except Exception as e:
        print(f"❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        results.append({"name": tc["name"], "task": tc["task"], "status": f"❌ FAIL: {str(e)[:50]}"})

# ========== 汇总结果 ==========

print("\n" + "=" * 80)
print("测试汇总")
print("=" * 80)

passed_count = sum(1 for r in results if r.get("status") == "PASS")

for r in results:
    status_icon = "✅" if r["status"] == "PASS" else "❌"
    status_text = r.pop("status")
    print(f"{status_icon} {status_text} {r['name']}")
    for k, v in r.items():
        if isinstance(v, float):
            print(f"    {k}: {v:.4f}")
        else:
            print(f"    {k}: {v}")

passed = passed_count
total = len(results)

print(f"\n{'=' * 80}")
print(f"测试通过: {passed}/{total}")

if passed == total:
    print("✅ 所有测试通过!")
    sys.exit(0)
else:
    print(f"❌ {total - passed} 个测试失败")
    sys.exit(1)
