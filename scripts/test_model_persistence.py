"""
模型持久化测试 - 断点续传支持验证

测试所有28个模型是否支持:
1. 训练后保存 (pickle/joblib)
2. 加载后预测 (结果一致性)
3. 继续训练 (部分模型支持 partial_fit)

Author: Claude AI
Date: 2025-01-12
"""

import sys
import os
import tempfile
import pickle
import joblib
import numpy as np
from pathlib import Path
from typing import Dict, Any, Tuple

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.registry import build_model_from_policy, get_supported_models


def generate_test_data(task: str, n_samples: int = 500, n_features: int = 20) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """生成测试数据"""
    np.random.seed(42)
    X_train = np.random.randn(n_samples, n_features)
    X_test = np.random.randn(200, n_features)

    if task in ["regression"]:
        y_train = X_train[:, 0] + 0.5 * X_train[:, 1] + np.random.randn(n_samples) * 0.1
        y_test = X_test[:, 0] + 0.5 * X_test[:, 1] + np.random.randn(200) * 0.1
    else:  # binary/multiclass
        y_train = (X_train[:, 0] + X_train[:, 1] > 0).astype(int)
        y_test = (X_test[:, 0] + X_test[:, 1] > 0).astype(int)

        if task == "multiclass":
            # 三分类
            y_train = np.digitize(X_train[:, 0] + X_train[:, 1], bins=[-1.0, 1.0])
            y_test = np.digitize(X_test[:, 0] + X_test[:, 1], bins=[-1.0, 1.0])

    return X_train, y_train, X_test, y_test


def test_model_persistence(model_id: str, task: str) -> Dict[str, Any]:
    """
    测试单个模型的持久化能力

    Returns:
        {
            "model_id": str,
            "task": str,
            "pickle_save": bool,
            "pickle_load": bool,
            "joblib_save": bool,
            "joblib_load": bool,
            "prediction_consistent": bool,
            "partial_fit_support": bool,
            "status": "PASS" | "FAIL",
            "error": str | None
        }
    """
    result = {
        "model_id": model_id,
        "task": task,
        "pickle_save": False,
        "pickle_load": False,
        "joblib_save": False,
        "joblib_load": False,
        "prediction_consistent": False,
        "partial_fit_support": False,
        "status": "FAIL",
        "error": None
    }

    try:
        # 1. 构建模型
        policy = {
            "task": task,
            "model": {
                "id": model_id,
                "scaler": "standard",
                "params": {}
            }
        }

        # 特殊配置
        if "cv" in model_id:
            policy["model"]["params"] = {"cv": 3}
        elif "xgboost" in model_id:
            policy["model"]["params"] = {"max_depth": 3, "n_estimators": 50, "random_state": 42}
        elif "lightgbm" in model_id:
            policy["model"]["params"] = {"num_leaves": 31, "n_estimators": 50, "random_state": 42, "verbose": -1}
        elif "catboost" in model_id:
            policy["model"]["params"] = {"iterations": 50, "depth": 4, "random_state": 42, "verbose": False}
        elif "rf" in model_id or "etr" in model_id:
            policy["model"]["params"] = {"n_estimators": 50, "max_depth": 5, "random_state": 42}
        elif "hgbt" in model_id:
            policy["model"]["params"] = {"max_iter": 50, "max_depth": 4, "random_state": 42}

        pipe, sw_ok = build_model_from_policy(task, policy)

        # 2. 生成数据并训练
        X_train, y_train, X_test, y_test = generate_test_data(task)
        pipe.fit(X_train, y_train)

        # 原始预测
        if task in ["binary", "multiclass"] and hasattr(pipe, "predict_proba"):
            y_pred_original = pipe.predict_proba(X_test)
        else:
            y_pred_original = pipe.predict(X_test)

        # 3. 测试 pickle 保存
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pkl") as f:
            pickle_path = f.name

        try:
            with open(pickle_path, "wb") as f:
                pickle.dump(pipe, f)
            result["pickle_save"] = True

            # 4. 测试 pickle 加载
            with open(pickle_path, "rb") as f:
                pipe_loaded_pickle = pickle.load(f)
            result["pickle_load"] = True

            # 预测一致性
            if task in ["binary", "multiclass"] and hasattr(pipe_loaded_pickle, "predict_proba"):
                y_pred_pickle = pipe_loaded_pickle.predict_proba(X_test)
            else:
                y_pred_pickle = pipe_loaded_pickle.predict(X_test)

            if np.allclose(y_pred_original, y_pred_pickle, rtol=1e-5):
                result["prediction_consistent"] = True

        except Exception as e:
            result["error"] = f"pickle error: {str(e)}"
        finally:
            if os.path.exists(pickle_path):
                os.unlink(pickle_path)

        # 5. 测试 joblib 保存
        with tempfile.NamedTemporaryFile(delete=False, suffix=".joblib") as f:
            joblib_path = f.name

        try:
            joblib.dump(pipe, joblib_path)
            result["joblib_save"] = True

            # 6. 测试 joblib 加载
            pipe_loaded_joblib = joblib.load(joblib_path)
            result["joblib_load"] = True

            # 预测一致性验证(joblib)
            if task in ["binary", "multiclass"] and hasattr(pipe_loaded_joblib, "predict_proba"):
                y_pred_joblib = pipe_loaded_joblib.predict_proba(X_test)
            else:
                y_pred_joblib = pipe_loaded_joblib.predict(X_test)

            if not np.allclose(y_pred_original, y_pred_joblib, rtol=1e-5):
                result["prediction_consistent"] = False
                result["error"] = "joblib prediction mismatch"

        except Exception as e:
            if result["error"] is None:
                result["error"] = f"joblib error: {str(e)}"
        finally:
            if os.path.exists(joblib_path):
                os.unlink(joblib_path)

        # 7. 测试 partial_fit 支持 (增量学习)
        final_step = pipe.steps[-1][1]
        if hasattr(final_step, "partial_fit"):
            result["partial_fit_support"] = True

        # 8. 判断是否通过
        if result["pickle_save"] and result["pickle_load"] and \
           result["joblib_save"] and result["joblib_load"] and \
           result["prediction_consistent"]:
            result["status"] = "PASS"

    except Exception as e:
        result["error"] = f"build/train error: {str(e)}"
        result["status"] = "FAIL"

    return result


def main():
    print("=" * 80)
    print("模型持久化测试 - 断点续传支持验证")
    print("=" * 80)
    print()

    # 获取所有支持的模型
    all_models = get_supported_models()

    print(f"📦 共 {len(all_models)} 个模型待测试")
    print()

    # 按任务类型分组测试
    results = []

    for task_name, models in all_models.items():
        print(f"\n{'=' * 80}")
        print(f"任务类型: {task_name.upper()}")
        print(f"{'=' * 80}\n")

        for model_id in models:
            print(f"测试: {model_id} ...", end=" ", flush=True)

            result = test_model_persistence(model_id, task_name)
            results.append(result)

            if result["status"] == "PASS":
                print(f"✅ PASS")
            else:
                print(f"❌ FAIL")
                if result["error"]:
                    print(f"   错误: {result['error']}")

    # 统计汇总
    print("\n" + "=" * 80)
    print("测试汇总")
    print("=" * 80)
    print()

    passed = sum(1 for r in results if r["status"] == "PASS")
    total = len(results)

    print(f"✅ 通过: {passed}/{total}")
    print(f"❌ 失败: {total - passed}/{total}")
    print()

    # 详细功能统计
    pickle_save_count = sum(1 for r in results if r["pickle_save"])
    pickle_load_count = sum(1 for r in results if r["pickle_load"])
    joblib_save_count = sum(1 for r in results if r["joblib_save"])
    joblib_load_count = sum(1 for r in results if r["joblib_load"])
    consistent_count = sum(1 for r in results if r["prediction_consistent"])
    partial_fit_count = sum(1 for r in results if r["partial_fit_support"])

    print("功能支持统计:")
    print(f"  Pickle保存:    {pickle_save_count}/{total} ({100*pickle_save_count/total:.1f}%)")
    print(f"  Pickle加载:    {pickle_load_count}/{total} ({100*pickle_load_count/total:.1f}%)")
    print(f"  Joblib保存:    {joblib_save_count}/{total} ({100*joblib_save_count/total:.1f}%)")
    print(f"  Joblib加载:    {joblib_load_count}/{total} ({100*joblib_load_count/total:.1f}%)")
    print(f"  预测一致性:    {consistent_count}/{total} ({100*consistent_count/total:.1f}%)")
    print(f"  增量学习支持:  {partial_fit_count}/{total} ({100*partial_fit_count/total:.1f}%)")
    print()

    # 失败详情
    failed_results = [r for r in results if r["status"] == "FAIL"]
    if failed_results:
        print("=" * 80)
        print("失败模型详情")
        print("=" * 80)
        print()

        for r in failed_results:
            print(f"❌ {r['model_id']} ({r['task']})")
            print(f"   Pickle保存: {'✅' if r['pickle_save'] else '❌'}")
            print(f"   Pickle加载: {'✅' if r['pickle_load'] else '❌'}")
            print(f"   Joblib保存: {'✅' if r['joblib_save'] else '❌'}")
            print(f"   Joblib加载: {'✅' if r['joblib_load'] else '❌'}")
            print(f"   预测一致性: {'✅' if r['prediction_consistent'] else '❌'}")
            if r['error']:
                print(f"   错误信息: {r['error']}")
            print()

    # 增量学习支持列表
    partial_fit_models = [r for r in results if r["partial_fit_support"]]
    if partial_fit_models:
        print("=" * 80)
        print("支持增量学习的模型 (partial_fit)")
        print("=" * 80)
        print()
        for r in partial_fit_models:
            print(f"  ✅ {r['model_id']} ({r['task']})")
        print()

    # 最终判断
    print("=" * 80)
    print("最终结论")
    print("=" * 80)
    print()

    if passed == total:
        print(f"🎉 所有 {total} 个模型都支持断点续传!")
        print()
        print("✅ 断点续传完全支持:")
        print("   1. 所有模型可以通过 pickle/joblib 保存")
        print("   2. 所有模型可以从文件加载")
        print("   3. 加载后预测结果与原始模型完全一致")
        print()
        if partial_fit_count > 0:
            print(f"📈 其中 {partial_fit_count} 个模型额外支持增量学习 (partial_fit)")
            print("   可以在不重新训练的情况下继续学习新数据")
        print()
        return 0
    else:
        print(f"⚠️  {total - passed} 个模型不支持断点续传")
        print()
        print("建议:")
        print("  1. 使用通过测试的模型进行生产部署")
        print("  2. 失败的模型可能需要特殊处理或不支持序列化")
        print()
        return 1


if __name__ == "__main__":
    sys.exit(main())
