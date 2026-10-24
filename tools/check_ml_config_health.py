#!/usr/bin/env python3
"""
ML配置健康检查工具

在系统启动时自动运行，检查ML配置的一致性：
1. oof_policies.json 中的 horizon_min 对应的label列是否存在
2. 训练脚本中的默认label_col是否存在
3. 配置之间的一致性

Usage:
    venv/bin/python tools/check_ml_config_health.py --output /tmp/config_health.json
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Any, Optional

# Ensure project root on sys.path for standalone execution
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import the unified health check from libs
from libs.health.snapshot import check_ml_config_health_lib, ConfigHealthStatus
from datetime import datetime # Needed for the timestamp in the report


def check_ml_config_health(output_file: str = None, cli: Optional[Any] = None) -> Dict[str, Any]:
    """
    检查ML配置整体健康度

    Returns:
        {
            "healthy": True/False,
            "issues": [...],
            "warnings": [...],
            "checks": [...]
        }
    """
    # Call the unified library function, passing the client if provided.
    # To reduce memory pressure on large tables, pass a throttled client if none provided.
    if cli is None:
        import clickhouse_connect
        cli = clickhouse_connect.get_client(
            host="localhost",
            port=8123,
            database="trading",
            settings={
                "max_memory_usage": int(os.environ.get("CH_CFG_CHECK_MEM", 200_000_000)),
                "max_threads": int(os.environ.get("CH_CFG_CHECK_THREADS", 1)),
                "use_uncompressed_cache": 0,
            },
        )

    report_obj: ConfigHealthStatus = check_ml_config_health_lib(cli=cli)
    
    # Adapt to the old output format for the tool for now, can be simplified later
    all_issues = report_obj.problems
    all_warnings = report_obj.warnings
    
    print("=" * 80)
    print("ML CONFIG HEALTH CHECK")
    print("=" * 80)

    # Reproduce printing for tool compatibility
    # Extract checks from the report_obj.checks list which is structured
    # check_ml_config_health_lib returns a ConfigHealthStatus object which has a checks attribute (list of dicts)
    
    # Assuming the structure within report_obj.checks is a list containing dictionaries
    # for oof_check and script_check, similar to the original tool's output.
    oof_check_report = next((c for c in report_obj.checks if c.get("config_file") == "config/oof_policies.json"), {})
    script_check_report = next((c for c in report_obj.checks if c.get("script_file") == "pipelines/train_xgb_with_oof.py"), {})

    print(f"\n[Check] oof_policies.json")
    print(f"  Config file:       {oof_check_report.get('config_file', 'N/A')}")
    print(f"  Default horizon:   {oof_check_report.get('horizon_min', 'N/A')} minutes")
    print(f"  Expected label:    {oof_check_report.get('expected_label', 'N/A')}")
    print(f"  Existing labels:   {oof_check_report.get('existing_labels', 'N/A')}")
    if oof_check_report.get('issues'):
        print(f"  ❌ CRITICAL: Label {oof_check_report.get('expected_label', 'N/A')} does NOT exist!")
    else:
        print(f"  ✅ OK: Label exists")

    print(f"\n[Check] pipelines/train_xgb_with_oof.py")
    print(f"  Default label:     {script_check_report.get('default_label', 'N/A')}")
    if script_check_report.get('warnings'):
        print(f"  ⚠️  WARNING: Default label {script_check_report.get('default_label', 'N/A')} may not exist")
    else:
        print(f"  ✅ OK: Default label exists")
    
    print(f"\n[Check] Cross-Config Consistency")
    # This consistency check result is now part of the `all_warnings` list from `check_ml_config_health_lib`
    inconsistency_warning = next((w for w in all_warnings if w.get('type') == 'config_inconsistency'), None)
    if inconsistency_warning:
        print(f"  ⚠️  WARNING: Configs don't match ({oof_check_report.get('expected_label', 'N/A')} vs {script_check_report.get('default_label', 'N/A')})")
    else:
        print(f"  ✅ OK: Configs are consistent")


    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)

    healthy = len(all_issues) == 0 # Based on problems list

    if healthy:
        print("\n✅ All checks passed! ML config is healthy.")
    else:
        print(f"\n❌ Found {len(all_issues)} issue(s) and {len(all_warnings)} warning(s)")

        if all_issues:
            print("\n🔴 Critical Issues:")
            for i, issue in enumerate(all_issues, 1):
                print(f"\n{i}. [{issue['severity'].upper()}] {issue['type']}")
                print(f"   {issue['message']}")
                if 'fix' in issue:
                    if isinstance(issue['fix'], dict):
                        for option, cmd in issue['fix'].items():
                            print(f"   {option}: {cmd}")
                    else:
                        print(f"   Fix: {issue['fix']}")

        if all_warnings:
            print("\n🟡 Warnings:")
            for i, warning in enumerate(all_warnings, 1):
                print(f"\n{i}. [{warning['severity'].upper()}] {warning['type']}")
                print(f"   {warning['message']}")

    report = {
        "timestamp": datetime.now().isoformat(),
        "healthy": report_obj.status == "healthy",
        "issues_count": len(all_issues),
        "warnings_count": len(all_warnings),
        "issues": all_issues,
        "warnings": all_warnings,
        "checks": report_obj.to_dict().get("checks", []) # Pass through raw checks from lib
    }

    # Save to file
    if output_file:
        with open(output_file, 'w') as f:
            json.dump(report, f, indent=2)
        print(f"\n📄 Report saved to: {output_file}")

    return report


def main():
    parser = argparse.ArgumentParser(description="Check ML config health")
    parser.add_argument("--output", help="Output JSON file path")

    args = parser.parse_args()

    report = check_ml_config_health(output_file=args.output)

    # Return exit code: 0 if healthy, 1 if issues
    return 0 if report["healthy"] else 1
