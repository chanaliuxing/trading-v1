#!/usr/bin/env python3
"""
Configuration Validation Tool

Standalone tool for validating trading system configurations.
Can be run independently or integrated into CI/CD pipelines.

Usage:
    # Validate main config only
    python tools/validate_all_configs.py

    # Validate main config + HPO config
    python tools/validate_all_configs.py --hpo-config config/hpo_enhanced.json

    # With ClickHouse validation
    python tools/validate_all_configs.py --clickhouse-host localhost:9000

    # Strict mode (warnings become errors)
    python tools/validate_all_configs.py --strict

    # Output JSON report
    python tools/validate_all_configs.py --report-json validation_report.json

Features:
    - Fast validation (<2 seconds)
    - Colored terminal output
    - JSON export for CI/CD integration
    - Exit codes (0=success, 1=failure)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.ai_trader.gates.config_gate import ConfigGate, ConfigGatePolicy
from libs.logger import get_logger

logger = get_logger(__name__)


# Terminal colors
class Colors:
    """ANSI color codes for terminal output"""
    RESET = '\033[0m'
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'


def print_header(text: str):
    """Print colored header"""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text:^80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}\n")


def print_error(text: str):
    """Print colored error"""
    print(f"{Colors.RED}❌ {text}{Colors.RESET}")


def print_warning(text: str):
    """Print colored warning"""
    print(f"{Colors.YELLOW}⚠️  {text}{Colors.RESET}")


def print_success(text: str):
    """Print colored success"""
    print(f"{Colors.GREEN}✅ {text}{Colors.RESET}")


def print_info(text: str):
    """Print info"""
    print(f"   {text}")


def print_results(result: Dict[str, Any], strict: bool = False):
    """
    Print validation results with colors

    Args:
        result: Validation result dict
        strict: If True, treat warnings as errors
    """
    errors = result.get("reasons", [])
    warnings = result.get("warnings", [])
    details = result.get("details", {})

    # Print errors
    if errors:
        print(f"\n{Colors.RED}{Colors.BOLD}ERRORS ({len(errors)}):{Colors.RESET}")
        for i, err in enumerate(errors, 1):
            print_error(f"{i}. {err}")

    # Print warnings
    if warnings:
        print(f"\n{Colors.YELLOW}{Colors.BOLD}WARNINGS ({len(warnings)}):{Colors.RESET}")
        for i, warn in enumerate(warnings, 1):
            print_warning(f"{i}. {warn}")

    # Print summary
    print(f"\n{Colors.BOLD}SUMMARY:{Colors.RESET}")
    print_info(f"Total errors:   {len(errors)}")
    print_info(f"Total warnings: {len(warnings)}")

    # Print validation details
    if details:
        print(f"\n{Colors.BOLD}VALIDATION DETAILS:{Colors.RESET}")

        if "schema" in details:
            status = "✅ PASS" if details["schema"]["ok"] else "❌ FAIL"
            print_info(f"Schema validation:      {status}")

        if "grid" in details:
            status = "✅ PASS" if details["grid"]["ok"] else "❌ FAIL"
            print_info(f"Grid validation:        {status}")

            # Show grid size
            if "total_combinations" in details["grid"].get("details", {}):
                combos = details["grid"]["details"]["total_combinations"]
                print_info(f"  Grid combinations:    {combos:,}")

        if "file_refs" in details:
            status = "✅ PASS" if details["file_refs"]["ok"] else "❌ FAIL"
            print_info(f"File ref validation:    {status}")

        if "consistency" in details:
            status = "✅ PASS" if details["consistency"]["ok"] else "❌ FAIL"
            print_info(f"Consistency validation: {status}")

        if "features_table" in details:
            ft_details = details["features_table"]
            status = "✅ PASS" if ft_details["ok"] else "❌ FAIL"
            print_info(f"Features table check:   {status}")

            # Show warnings for skipped checks
            if ft_details.get("warnings"):
                for warn in ft_details["warnings"]:
                    if "not_verified" in warn:
                        print_info(f"  ⚠️  Skipped (no ClickHouse connection)")
                        break

    # Final result
    print()
    if result["ok"] and (not warnings or not strict):
        print_success("All validations passed!")
        return True
    else:
        if strict and warnings:
            print_error(f"Validation failed (strict mode: {len(warnings)} warnings treated as errors)")
        else:
            print_error(f"Validation failed with {len(errors)} errors")
        return False


def save_json_report(result: Dict[str, Any], output_path: str):
    """Save validation report as JSON"""
    try:
        with open(output_path, 'w') as f:
            json.dump(result, f, indent=2, default=str)
        print_info(f"JSON report saved to: {output_path}")
    except Exception as e:
        print_error(f"Failed to save JSON report: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Validate trading system configurations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Validate main config
  python tools/validate_all_configs.py

  # Validate with HPO config
  python tools/validate_all_configs.py --hpo-config config/hpo_enhanced.json

  # With ClickHouse validation
  python tools/validate_all_configs.py --clickhouse-host localhost:9000

  # Strict mode (warnings = errors)
  python tools/validate_all_configs.py --strict

  # Save JSON report
  python tools/validate_all_configs.py --report-json report.json
        """
    )

    parser.add_argument(
        "--config",
        default="config.yml",
        help="Path to main config file (default: config.yml)"
    )

    parser.add_argument(
        "--hpo-config",
        help="Path to HPO config file (optional)"
    )

    parser.add_argument(
        "--strategy-configs-dir",
        default="strategies/configs",
        help="Directory containing strategy configs (default: strategies/configs)"
    )

    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (fail on any warning)"
    )

    parser.add_argument(
        "--report-json",
        help="Save validation report as JSON to specified path"
    )

    parser.add_argument(
        "--no-grid-validation",
        action="store_true",
        help="Skip HPO grid validation (faster)"
    )

    parser.add_argument(
        "--no-file-validation",
        action="store_true",
        help="Skip file reference validation"
    )

    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )

    parser.add_argument(
        "--clickhouse-host",
        help="ClickHouse host for features_table validation (format: host:port, e.g., localhost:9000)"
    )

    args = parser.parse_args()

    # Disable colors if requested
    if args.no_color:
        Colors.RESET = ''
        Colors.RED = ''
        Colors.GREEN = ''
        Colors.YELLOW = ''
        Colors.BLUE = ''
        Colors.BOLD = ''

    # Print header
    print_header("Configuration Validation Report")

    # Create ConfigGate with policy
    policy = ConfigGatePolicy(
        schema_validation=True,
        grid_validation=not args.no_grid_validation and bool(args.hpo_config),
        file_ref_validation=not args.no_file_validation,
        consistency_validation=True,
        fail_on_schema_error=True,
        fail_on_file_not_found=True,
        fail_on_inconsistency=args.strict,
        fail_on_grid_violation=True
    )

    print_info(f"Config file:          {args.config}")
    if args.hpo_config:
        print_info(f"HPO config file:      {args.hpo_config}")
    print_info(f"Strategy config dir:  {args.strategy_configs_dir}")
    print_info(f"Strict mode:          {'Yes' if args.strict else 'No'}")
    print()

    # Create ClickHouse client if host provided
    clickhouse_client = None
    if args.clickhouse_host:
        try:
            from clickhouse_driver import Client
            host, port = args.clickhouse_host.split(':')
            clickhouse_client = Client(host=host, port=int(port))
            print_info(f"ClickHouse connection: {args.clickhouse_host}")
        except ImportError:
            print_warning("clickhouse-driver not installed, skipping ClickHouse validation")
        except ValueError:
            print_error(f"Invalid ClickHouse host format: {args.clickhouse_host} (expected: host:port)")
            sys.exit(1)
        except Exception as e:
            print_warning(f"Failed to connect to ClickHouse: {e}")

    # Create gate
    try:
        gate = ConfigGate(policy, clickhouse_client=clickhouse_client)
    except Exception as e:
        print_error(f"Failed to initialize ConfigGate: {e}")
        sys.exit(1)

    # Run validation
    print(f"{Colors.BOLD}Running validations...{Colors.RESET}\n")

    try:
        result = gate.check(
            config_path=args.config,
            hpo_config_path=args.hpo_config,
            strategy_configs_dir=args.strategy_configs_dir
        )
    except Exception as e:
        print_error(f"Validation failed with exception: {e}")
        logger.exception("Validation exception")
        sys.exit(1)

    # Print results
    success = print_results(result, strict=args.strict)

    # Save JSON report if requested
    if args.report_json:
        save_json_report(result, args.report_json)

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
