#!/usr/bin/env python3
"""
Implementation Audit Script
===========================

Systematically checks which upgradeFeatures documents have been implemented
with evidence (files, tests, etc.)
"""

import os
import json
from pathlib import Path
from typing import Dict, List, Any

PROJECT_ROOT = Path(__file__).parent


def check_file_exists(path: str) -> bool:
    """Check if file exists relative to project root."""
    return (PROJECT_ROOT / path).exists()


def count_tests(test_file: str) -> int:
    """Count number of test functions in a test file."""
    path = PROJECT_ROOT / test_file
    if not path.exists():
        return 0

    count = 0
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            if line.strip().startswith('def test_'):
                count += 1
    return count


def check_implementation() -> Dict[str, Any]:
    """Check implementation status of all upgrade features."""

    status = {}

    # upgradeFeatures6: Label Engine
    status['upgradeFeatures6'] = {
        'name': 'Label Engine',
        'files': {
            'labels/label_engine.py': check_file_exists('labels/label_engine.py'),
            'config/label_registry.json': check_file_exists('config/label_registry.json'),
            'tests/unit/labels/test_label_engine.py': check_file_exists('tests/unit/labels/test_label_engine.py'),
        },
        'tests': count_tests('tests/unit/labels/test_label_engine.py'),
        'status': '✅ COMPLETE' if all([
            check_file_exists('labels/label_engine.py'),
            check_file_exists('tests/unit/labels/test_label_engine.py'),
        ]) else '❌ INCOMPLETE'
    }

    # upgradeFeatures7: Sandbox Executor
    status['upgradeFeatures7'] = {
        'name': 'Sandbox Executor',
        'files': {
            'features/sandbox_executor.py': check_file_exists('features/sandbox_executor.py'),
            'tests/unit/features/test_sandbox_executor.py': check_file_exists('tests/unit/features/test_sandbox_executor.py'),
        },
        'tests': count_tests('tests/unit/features/test_sandbox_executor.py'),
        'status': '✅ COMPLETE' if all([
            check_file_exists('features/sandbox_executor.py'),
            check_file_exists('tests/unit/features/test_sandbox_executor.py'),
        ]) else '❌ INCOMPLETE'
    }

    # upgradeFeatures8: Label Sanity
    status['upgradeFeatures8'] = {
        'name': 'Label Sanity',
        'files': {
            'labels/label_sanity.py': check_file_exists('labels/label_sanity.py'),
            'labels/label_integration.py': check_file_exists('labels/label_integration.py'),
            'tests/unit/labels/test_label_sanity.py': check_file_exists('tests/unit/labels/test_label_sanity.py'),
            'tests/unit/labels/test_label_integration.py': check_file_exists('tests/unit/labels/test_label_integration.py'),
        },
        'tests': count_tests('tests/unit/labels/test_label_sanity.py') + count_tests('tests/unit/labels/test_label_integration.py'),
        'status': '✅ COMPLETE' if all([
            check_file_exists('labels/label_sanity.py'),
            check_file_exists('labels/label_integration.py'),
        ]) else '❌ INCOMPLETE'
    }

    # upgradeFeatures9: Feature Registry + SQL Sandbox
    status['upgradeFeatures9'] = {
        'name': 'Feature Registry + SQL Sandbox',
        'files': {
            'features/registry.py': check_file_exists('features/registry.py'),
            'config/feature_registry.json': check_file_exists('config/feature_registry.json'),
            'pi/sandbox_sql.py': check_file_exists('pi/sandbox_sql.py'),
            'pi/feature_sql.py': check_file_exists('pi/feature_sql.py'),
        },
        'tests': 0,  # Need to check if tests exist
        'status': '🟡 PARTIAL' if check_file_exists('features/registry.py') else '❌ INCOMPLETE'
    }

    # Check for brain/dataset_gate.py
    status['dataset_gate'] = {
        'name': 'Dataset Gate (Brain Decision)',
        'files': {
            'brain/dataset_gate.py': check_file_exists('brain/dataset_gate.py'),
            'brain/contracts.py': check_file_exists('brain/contracts.py'),
        },
        'tests': 0,
        'status': '🟡 PARTIAL' if check_file_exists('brain/contracts.py') else '❌ INCOMPLETE'
    }

    # Check for experiment audit
    status['experiment_audit'] = {
        'name': 'Experiment Audit',
        'files': {
            'experiments/audit.py': check_file_exists('experiments/audit.py'),
            'experiments/trial_guard.py': check_file_exists('experiments/trial_guard.py'),
        },
        'tests': 0,
        'status': '🟡 PARTIAL' if check_file_exists('experiments/audit.py') else '❌ INCOMPLETE'
    }

    # wf_hpo.py integration
    status['wf_hpo_integration'] = {
        'name': 'wf_hpo.py Integration',
        'files': {
            'wf_hpo.py': check_file_exists('wf_hpo.py'),
            'test_wf_hpo_integration.py': check_file_exists('test_wf_hpo_integration.py'),
        },
        'tests': count_tests('test_wf_hpo_integration.py'),
        'status': '✅ COMPLETE' if check_file_exists('test_wf_hpo_integration.py') else '❌ INCOMPLETE'
    }

    return status


def print_report(status: Dict[str, Any]):
    """Print formatted audit report."""
    print("=" * 80)
    print(" IMPLEMENTATION AUDIT REPORT")
    print("=" * 80)
    print()

    total_complete = 0
    total_partial = 0
    total_incomplete = 0

    for key, data in status.items():
        print(f"{data['status']} {data['name']}")
        print(f"   Tests: {data['tests']}")
        print("   Files:")
        for filepath, exists in data['files'].items():
            status_icon = '✓' if exists else '✗'
            print(f"     {status_icon} {filepath}")
        print()

        if data['status'].startswith('✅'):
            total_complete += 1
        elif data['status'].startswith('🟡'):
            total_partial += 1
        else:
            total_incomplete += 1

    print("=" * 80)
    print(" SUMMARY")
    print("=" * 80)
    print(f"✅ Complete: {total_complete}")
    print(f"🟡 Partial: {total_partial}")
    print(f"❌ Incomplete: {total_incomplete}")
    print(f"Total components: {len(status)}")
    print()

    # Calculate test coverage
    total_tests = sum(data['tests'] for data in status.values())
    print(f"Total tests found: {total_tests}")
    print()


if __name__ == "__main__":
    status = check_implementation()
    print_report(status)
