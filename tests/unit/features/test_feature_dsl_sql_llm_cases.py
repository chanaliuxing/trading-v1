#!/usr/bin/env python3
"""
测试 E: DSL → SQL 编译验证

验证 Feature DSL → ClickHouse SQL 编译器的正确性,使用预定义的测试用例。

运行方式:
    pytest tests/unit/features/test_feature_dsl_sql_llm_cases.py -v
    pytest tests/unit/features/test_feature_dsl_sql_llm_cases.py::test_dsl_compilation[simple_ratio] -v
"""
import pytest
import json
import re
from pathlib import Path
from typing import Any, Dict, List

# Import DSL compiler
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from libs.features.feature_dsl_sql import dsl_to_sql, FeatureDSLCompileError


# Load test cases
TEST_CASES_FILE = Path(__file__).parent / "test_dsl_cases.json"


def load_test_cases() -> List[Dict[str, Any]]:
    """Load test cases from JSON file"""
    if not TEST_CASES_FILE.exists():
        pytest.fail(f"Test cases file not found: {TEST_CASES_FILE}")

    with open(TEST_CASES_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    return data['test_cases']


# Load cases once
TEST_CASES = load_test_cases()


@pytest.mark.parametrize('case', TEST_CASES, ids=[c['case_id'] for c in TEST_CASES])
def test_dsl_compilation(case):
    """
    测试 DSL → SQL 编译

    对于 expect_success=True 的用例:
      - 编译应该成功
      - 返回的 SQL 应包含预期关键字

    对于 expect_success=False 的用例:
      - 编译应该失败
      - 异常消息应匹配 error_pattern
    """
    case_id = case['case_id']
    dsl_spec = case['dsl']
    expect_success = case['expect_success']
    description = case.get('description', '')

    print(f"\nTesting {case_id}: {description}")

    if expect_success:
        # 应该编译成功
        try:
            sql = dsl_to_sql(dsl_spec)
            print(f"  Generated SQL: {sql}")

            # 检查预期关键字
            expected_keywords = case.get('expected_keywords', [])
            for keyword in expected_keywords:
                assert keyword in sql, \
                    f"{case_id}: Expected keyword '{keyword}' not found in SQL: {sql}"

            # 检查 SQL 不为空
            assert sql.strip(), f"{case_id}: Generated SQL is empty"

            print(f"  ✅ PASS: Compilation successful, all keywords found")

        except Exception as e:
            pytest.fail(f"{case_id}: Expected success but got exception: {type(e).__name__}: {e}")

    else:
        # 应该编译失败
        error_pattern = case.get('error_pattern', '')

        try:
            sql = dsl_to_sql(dsl_spec)
            pytest.fail(
                f"{case_id}: Expected compilation to fail but got SQL: {sql}"
            )

        except FeatureDSLCompileError as e:
            error_msg = str(e).lower()
            print(f"  Caught expected error: {e}")

            # 检查错误消息是否匹配模式
            if error_pattern:
                pattern_parts = error_pattern.lower().split('|')
                matched = any(part.strip() in error_msg for part in pattern_parts)

                assert matched, \
                    f"{case_id}: Error message '{error_msg}' does not match pattern '{error_pattern}'"

            print(f"  ✅ PASS: Compilation failed as expected with matching error")

        except Exception as e:
            pytest.fail(
                f"{case_id}: Expected FeatureDSLCompileError but got {type(e).__name__}: {e}"
            )


def test_all_cases_loaded():
    """验证测试用例已正确加载"""
    assert len(TEST_CASES) > 0, "No test cases loaded"

    metadata_path = TEST_CASES_FILE
    with open(metadata_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        metadata = data['metadata']

    print(f"\nLoaded {len(TEST_CASES)} test cases")
    print(f"Expected pass: {metadata['expected_pass']}")
    print(f"Expected fail: {metadata['expected_fail']}")
    print(f"DSL format: {metadata['dsl_format']}")

    assert len(TEST_CASES) == metadata['total_cases']


def test_success_cases_summary():
    """统计成功用例"""
    success_cases = [c for c in TEST_CASES if c['expect_success']]

    print(f"\n{'='*80}")
    print(f"Success Cases Summary ({len(success_cases)} cases)")
    print(f"{'='*80}")

    for case in success_cases:
        dsl_spec = case['dsl']
        try:
            sql = dsl_to_sql(dsl_spec)
            status = "✅"
        except Exception as e:
            sql = f"ERROR: {e}"
            status = "❌"

        print(f"{status} {case['case_id']:<25} {case['description']}")
        print(f"   SQL: {sql[:100]}{'...' if len(sql) > 100 else ''}")


def test_failure_cases_summary():
    """统计失败用例"""
    failure_cases = [c for c in TEST_CASES if not c['expect_success']]

    print(f"\n{'='*80}")
    print(f"Failure Cases Summary ({len(failure_cases)} cases)")
    print(f"{'='*80}")

    for case in failure_cases:
        dsl_spec = case['dsl']
        try:
            sql = dsl_to_sql(dsl_spec)
            status = "❌ UNEXPECTED SUCCESS"
            error = f"Generated SQL: {sql}"
        except FeatureDSLCompileError as e:
            status = "✅"
            error = str(e)
        except Exception as e:
            status = "⚠️ WRONG EXCEPTION"
            error = f"{type(e).__name__}: {e}"

        print(f"{status} {case['case_id']:<30} {case['description']}")
        print(f"   Error: {error[:80]}{'...' if len(error) > 80 else ''}")


if __name__ == "__main__":
    # 允许直接运行
    pytest.main([__file__, '-v', '--tb=short'])
