#!/usr/bin/env python3
"""
Self-Programming CLI Tool

Runs the self-programming pipeline on Blueprint gaps or manual tasks.

Usage:
    # Run with a manual task
    python scripts/run_self_programming.py \
        --task "Add docstrings to demo.py" \
        --files demo.py \
        --use-llm

    # Run in dry-run mode (no LLM, no Git)
    python scripts/run_self_programming.py \
        --task "Refactor function foo" \
        --files demo.py \
        --dry-run

    # Run with Git sandbox
    python scripts/run_self_programming.py \
        --task "Fix bug in calculate_sum" \
        --files demo.py \
        --use-llm \
        --use-sandbox

    # From Blueprint gap (future)
    python scripts/run_self_programming.py \
        --gap-id abc123 \
        --use-llm \
        --use-sandbox
"""
import argparse
import sys
import json
from pathlib import Path
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from meta_agent.self_programming.engine import SelfProgrammingEngine
from meta_agent.self_programming.schema import ProgrammingTask
from meta_agent.self_programming.integration.blueprint_adapter import (
    create_gap_from_todo,
    blueprint_gap_to_programming_task,
    is_file_whitelisted,
)


def create_task_from_args(args) -> ProgrammingTask:
    """Create ProgrammingTask from CLI arguments."""
    # Parse files
    files = []
    if args.files:
        files = [f.strip() for f in args.files.split(",")]

    # Check whitelist
    if args.enforce_whitelist:
        unsafe_files = [f for f in files if not is_file_whitelisted(f)]
        if unsafe_files:
            print(f"⚠️  WARNING: These files are not whitelisted:")
            for f in unsafe_files:
                print(f"   - {f}")
            if not args.force:
                print("\nUse --force to proceed anyway, or remove unsafe files.")
                sys.exit(1)

    # Generate task ID
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_id = f"manual-{timestamp}"

    return ProgrammingTask(
        task_id=task_id,
        origin=args.origin,
        description=args.task,
        repo_root=args.repo_root,
        related_files=files,
        context_summary=args.context or f"Manual task created at {timestamp}",
        constraints={
            "risk_level": args.risk_level,
            "max_files": args.max_files,
            "requires_tests": not args.skip_tests,
            "whitelist_enforced": args.enforce_whitelist,
        },
        acceptance_criteria=[
            "Code compiles without errors",
            "Tests pass",
            "No new linting warnings",
        ],
        style_guide=args.style_guide,
    )


def print_result(result):
    """Pretty-print ProgrammingResult."""
    print("\n" + "=" * 60)
    print("SELF-PROGRAMMING RESULT")
    print("=" * 60)

    print(f"\n📋 Task: {result.task.task_id}")
    print(f"   Description: {result.task.description}")
    print(f"   Origin: {result.task.origin}")

    print(f"\n🏗️  Dev Plan:")
    print(f"   Summary: {result.dev_plan.task_summary}")
    print(f"   Work Items: {len(result.dev_plan.work_items)}")

    print(f"\n📝 Patch Set:")
    print(f"   Summary: {result.patch_set.summary}")
    print(f"   Files Modified: {len(result.patch_set.files)}")
    for mod in result.patch_set.files:
        print(f"      - {mod.file_path} ({mod.action})")

    print(f"\n🧪 Test Report:")
    print(f"   Status: {result.test_report.overall_status}")
    print(f"   Results: {len(result.test_report.results)}")
    if result.test_report.results:
        for r in result.test_report.results:
            status_emoji = "✅" if r.get("status") == "passed" else "❌"
            print(f"      {status_emoji} {r.get('id', 'unknown')}: {r.get('status', 'N/A')}")

    print(f"\n👁️  Review Decision:")
    print(f"   Status: {result.review.status}")
    print(f"   Severity: {result.review.severity}")
    print(f"   Summary: {result.review.summary}")
    if result.review.issues:
        print(f"   Issues: {len(result.review.issues)}")
        for issue in result.review.issues:
            print(f"      - [{issue.get('type')}] {issue.get('description')}")
    if result.review.follow_up_actions:
        print(f"   Follow-up:")
        for action in result.review.follow_up_actions:
            print(f"      - {action}")
    if result.patch_reports:
        print(f"\n🧩 Patch Reports ({len(result.patch_reports)}):")
        for rep in result.patch_reports:
            flag = "⚠️" if rep.get("conflict") else "✅" if rep.get("applied") else "ℹ️"
            print(f"   {flag} {rep.get('file_path')} - {rep.get('message')}")
            if rep.get("diff") and result.test_report.overall_status == "skipped":
                print("      (diff available in report)")
    if result.audit_log_path:
        print(f"\n🗂️  Audit log: {result.audit_log_path}")

    print("\n" + "=" * 60)

    # Final verdict
    if result.review.status == "approve":
        print("✅ CHANGES APPROVED")
    elif result.review.status == "reject":
        print("❌ CHANGES REJECTED")
    else:
        print("⚠️  CHANGES REQUIRE MODIFICATION")

    print("=" * 60 + "\n")


def save_result_json(result, output_path: str):
    """Save result to JSON file."""
    from dataclasses import asdict

    result_dict = asdict(result)
    with open(output_path, "w") as f:
        json.dump(result_dict, f, indent=2, default=str)

    print(f"💾 Result saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Self-Programming CLI - Autonomous code modification with safety checks"
    )

    # Task definition
    task_group = parser.add_argument_group("Task Definition")
    task_group.add_argument(
        "--task",
        type=str,
        help="Task description (what to do)",
        required=False,
    )
    task_group.add_argument(
        "--files",
        type=str,
        help="Comma-separated list of files to modify",
        required=False,
    )
    task_group.add_argument(
        "--gap-id",
        type=str,
        help="Blueprint gap ID (alternative to --task)",
        required=False,
    )
    task_group.add_argument(
        "--origin",
        type=str,
        default="refactor",
        choices=["blueprint_gap", "bugfix", "refactor", "experiment"],
        help="Task origin type",
    )
    task_group.add_argument(
        "--context",
        type=str,
        help="Additional context for the task",
    )

    # Execution modes
    mode_group = parser.add_argument_group("Execution Modes")
    mode_group.add_argument(
        "--use-llm",
        action="store_true",
        help="Use LLM for code generation (default: False, uses stubs)",
    )
    mode_group.add_argument(
        "--tool",
        type=str,
        default="local",
        choices=["local", "gemini", "codex"],
        help="LLM tool to use (default: local via Ollama)",
    )
    mode_group.add_argument(
        "--tool-model",
        type=str,
        help="Model name for the selected tool (e.g., 'gemini-pro', 'gpt-4')",
    )
    mode_group.add_argument(
        "--tool-cli",
        type=str,
        help="Custom CLI command for external tools (e.g., 'aichat', 'openai')",
    )
    mode_group.add_argument(
        "--use-sandbox",
        action="store_true",
        help="Use Git sandbox branch (default: False)",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run mode: no LLM, no Git, no file modifications",
    )

    # Safety controls
    safety_group = parser.add_argument_group("Safety Controls")
    safety_group.add_argument(
        "--enforce-whitelist",
        action="store_true",
        default=True,
        help="Enforce file whitelist (default: True)",
    )
    safety_group.add_argument(
        "--no-whitelist",
        action="store_true",
        help="Disable whitelist enforcement (USE WITH CAUTION)",
    )
    safety_group.add_argument(
        "--force",
        action="store_true",
        help="Force execution even if files are not whitelisted",
    )
    safety_group.add_argument(
        "--risk-level",
        type=str,
        default="low",
        choices=["low", "medium", "high"],
        help="Risk level of the task",
    )
    safety_group.add_argument(
        "--max-files",
        type=int,
        default=5,
        help="Maximum number of files to modify (safety limit)",
    )
    safety_group.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip test execution (NOT RECOMMENDED)",
    )
    safety_group.add_argument(
        "--style-guide",
        type=str,
        help="Optional style guide text injected into LLM prompts",
    )

    # Output
    output_group = parser.add_argument_group("Output")
    output_group.add_argument(
        "--output-json",
        type=str,
        help="Save result to JSON file",
    )
    output_group.add_argument(
        "--repo-root",
        type=str,
        default=".",
        help="Repository root directory",
    )

    args = parser.parse_args()

    # Validation
    if not args.task and not args.gap_id:
        parser.error("Either --task or --gap-id is required")

    if args.task and not args.files:
        parser.error("--files is required when using --task")

    # Handle whitelist override
    if args.no_whitelist:
        args.enforce_whitelist = False

    # Dry-run mode
    if args.dry_run:
        print("🔍 DRY RUN MODE: No files will be written or committed")
        args.use_sandbox = False

    # Print configuration
    print("\n" + "=" * 60)
    print("SELF-PROGRAMMING CONFIGURATION")
    print("=" * 60)
    print(f"Task: {args.task}")
    print(f"Files: {args.files}")
    print(f"Origin: {args.origin}")
    print(f"Use LLM: {args.use_llm}")
    print(f"Use Git Sandbox: {args.use_sandbox}")
    print(f"Whitelist Enforced: {args.enforce_whitelist}")
    print(f"Risk Level: {args.risk_level}")
    print(f"Repo Root: {args.repo_root}")
    print("=" * 60 + "\n")

    # Create task
    if args.gap_id:
        print(f"📋 Loading Blueprint gap: {args.gap_id}")
        print("❌ Gap loading not implemented yet - use --task instead")
        sys.exit(1)
    else:
        task = create_task_from_args(args)

    # Build tool config
    tool_config = {}
    if args.tool_model:
        tool_config["model"] = args.tool_model
    if args.tool_cli:
        tool_config["cli_command"] = args.tool_cli

    # Initialize engine
    print(f"🚀 Initializing SelfProgrammingEngine...")
    print(f"   Tool: {args.tool}")
    if tool_config:
        print(f"   Config: {tool_config}")

    engine = SelfProgrammingEngine(
        repo_root=args.repo_root,
        use_llm=args.use_llm,
        tool=args.tool if args.use_llm else None,
        tool_config=tool_config if tool_config else None,
        style_guide=args.style_guide,
    )

    # Run
    print(f"⚙️  Running self-programming pipeline...\n")
    try:
        result = engine.run(task, use_sandbox=args.use_sandbox, dry_run=args.dry_run)

        # Print result
        print_result(result)

        # Save JSON if requested
        if args.output_json:
            save_result_json(result, args.output_json)

        # Exit code based on review status
        if result.review.status == "approve":
            sys.exit(0)
        elif result.review.status == "reject":
            sys.exit(1)
        else:  # request_changes
            sys.exit(2)

    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(3)


if __name__ == "__main__":
    main()
