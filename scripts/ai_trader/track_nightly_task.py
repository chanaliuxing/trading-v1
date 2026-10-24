#!/usr/bin/env python3
"""
Nightly Task Metrics Tracker

Wrapper to track completion and duration of nightly tasks.

Usage:
    python scripts/ai_trader/track_nightly_task.py --task alpha_loop --command "python alpha_loop.py --config alpha_config_enhanced.json"
"""
import argparse
import subprocess
import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from libs import metrics
from libs.logger import get_logger

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description='Track nightly task execution')
    parser.add_argument('--task', required=True, help='Task name (alpha_loop, rl_train, hpo_sweep, reflection, prompt_evolver)')
    parser.add_argument('--command', required=True, help='Command to execute')
    parser.add_argument('--shell', action='store_true', help='Execute command in shell')
    args = parser.parse_args()

    task_name = args.task
    command = args.command

    logger.info("nightly_task_start", task=task_name, command=command)

    t0 = time.time()
    try:
        # Execute command
        result = subprocess.run(
            command if args.shell else command.split(),
            shell=args.shell,
            capture_output=False,  # Let output go to stdout/stderr
            text=True
        )

        duration = time.time() - t0
        success = result.returncode == 0

        # Track metrics
        metrics.nightly_task_completion.labels(task=task_name).set(1 if success else 0)
        metrics.nightly_task_duration_seconds.labels(task=task_name).observe(duration)
        if success:
            metrics.nightly_task_last_run_timestamp.labels(task=task_name).set(time.time())

        logger.info(
            "nightly_task_complete",
            task=task_name,
            success=success,
            duration_sec=round(duration, 2),
            returncode=result.returncode
        )

        if not success:
            logger.error("nightly_task_failed", task=task_name, returncode=result.returncode)
            sys.exit(result.returncode)

    except Exception as e:
        duration = time.time() - t0
        logger.error("nightly_task_error", task=task_name, err=str(e), exc_info=True)

        # Mark as failed
        metrics.nightly_task_completion.labels(task=task_name).set(0)
        metrics.nightly_task_duration_seconds.labels(task=task_name).observe(duration)

        sys.exit(1)


if __name__ == '__main__':
    main()
