#!/usr/bin/env python3
"""
Minimal ML Job Runner with Event Publishing (POC)

Features:
- Generates job_id and publishes started/completed/failed events to internal.ml.status
- Falls back to file-based logging (runs/.../ml_jobs/{job_id}.jsonl) if Kafka unavailable
- No heartbeat/watchdog in POC version (TODO for next session)

Usage:
    from scripts.ai_trader.ml.job_runner import run_job

    rc = run_job(
        cmd=["python", "tools/nightly_oof_flow.py", "--symbols", "AAPL,TSLA"],
        job_type="oof",
        params={"symbols": ["AAPL", "TSLA"], "window_days": 30}
    )
"""
import subprocess
import time
import json
import random
import string
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional

from libs.event_bus import publish
from libs.logger import get_logger

logger = get_logger("ai_trader.ml.job_runner")


def _generate_job_id() -> str:
    """Generate unique job ID: YYYYMMDDHHMMSS-{random8}"""
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
    return f"{ts}-{rand}"


def _get_job_log_path(job_id: str) -> Path:
    """Get path for job event log: runs/ai_trader/YYYY-MM-DD/ml_jobs/{job_id}.jsonl"""
    today = datetime.now().strftime("%Y-%m-%d")
    log_dir = Path("runs/ai_trader") / today / "ml_jobs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"{job_id}.jsonl"


def _publish_event(event: Dict[str, Any], job_id: str) -> None:
    """Publish event to Kafka and fall back to file"""
    # Publish to Kafka (no-op if unavailable)
    try:
        publish('ml', event, key="ai_trader")
    except Exception as e:
        logger.warning("kafka_publish_failed", job_id=job_id, error=str(e))

    # Always write to file (fallback + audit trail)
    log_path = _get_job_log_path(job_id)
    try:
        with open(log_path, 'a') as f:
            f.write(json.dumps(event) + '\n')
    except Exception as e:
        logger.error("file_write_failed", job_id=job_id, path=str(log_path), error=str(e))


def run_job(
    cmd: List[str],
    job_type: str,
    params: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
    artifacts: Optional[List[str]] = None
) -> int:
    """
    Run ML job as subprocess and publish lifecycle events.

    Args:
        cmd: Command to execute (e.g., ["python", "tools/nightly_oof_flow.py", ...])
        job_type: Type of job (e.g., "oof", "hpo", "rl_train", "rl_eval")
        params: Optional job parameters (for event metadata)
        metrics: Optional metrics to include in completed event (caller can pass after job finishes)
        artifacts: Optional artifact paths to include in completed event

    Returns:
        Return code (0 = success, non-zero = failure)

    Events published:
        - started: {event: "ml.job", status: "started", job_type, job_id, ts, params}
        - completed: {event: "ml.job", status: "completed", job_type, job_id, ts, duration_sec, metrics, artifacts}
        - failed: {event: "ml.job", status: "failed", job_type, job_id, ts, error_type, error_message, duration_sec}
    """
    job_id = _generate_job_id()
    start_time = time.time()

    # Publish started event
    started_event = {
        "event": "ml.job",
        "status": "started",
        "job_type": job_type,
        "job_id": job_id,
        "ts": datetime.now().isoformat(),
        "params": params or {},
        "cmd": " ".join(cmd)
    }
    _publish_event(started_event, job_id)
    logger.info("ml_job_started", job_id=job_id, job_type=job_type, cmd=" ".join(cmd))

    try:
        # Run subprocess
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False  # Don't raise on non-zero exit
        )

        duration_sec = time.time() - start_time

        if result.returncode == 0:
            # Success - publish completed event
            completed_event = {
                "event": "ml.job",
                "status": "completed",
                "job_type": job_type,
                "job_id": job_id,
                "ts": datetime.now().isoformat(),
                "duration_sec": round(duration_sec, 2),
                "metrics": metrics or {},
                "artifacts": artifacts or [],
                "return_code": 0
            }
            _publish_event(completed_event, job_id)
            logger.info("ml_job_completed", job_id=job_id, job_type=job_type, duration_sec=duration_sec)
            return 0
        else:
            # Non-zero exit - publish failed event
            failed_event = {
                "event": "ml.job",
                "status": "failed",
                "job_type": job_type,
                "job_id": job_id,
                "ts": datetime.now().isoformat(),
                "duration_sec": round(duration_sec, 2),
                "error_type": "non_zero_exit",
                "error_message": f"Process exited with code {result.returncode}",
                "return_code": result.returncode,
                "stderr": result.stderr[-500:] if result.stderr else ""  # Last 500 chars
            }
            _publish_event(failed_event, job_id)
            logger.error("ml_job_failed", job_id=job_id, job_type=job_type,
                        return_code=result.returncode, stderr=result.stderr[-200:])
            return result.returncode

    except Exception as e:
        # Exception during execution - publish failed event
        duration_sec = time.time() - start_time
        failed_event = {
            "event": "ml.job",
            "status": "failed",
            "job_type": job_type,
            "job_id": job_id,
            "ts": datetime.now().isoformat(),
            "duration_sec": round(duration_sec, 2),
            "error_type": "exception",
            "error_message": str(e),
            "return_code": -1
        }
        _publish_event(failed_event, job_id)
        logger.error("ml_job_exception", job_id=job_id, job_type=job_type, error=str(e), exc_info=True)
        return -1


# Convenience wrapper for direct event publishing (for jobs that handle their own execution)
def publish_ml_event(status: str, job_type: str, job_id: str, **kwargs) -> None:
    """
    Publish ML event directly (for jobs that manage their own execution).

    Args:
        status: "started", "completed", or "failed"
        job_type: "oof", "hpo", "rl_train", "rl_eval"
        job_id: Unique job identifier
        **kwargs: Additional event fields (metrics, artifacts, error_message, etc.)
    """
    event = {
        "event": "ml.job",
        "status": status,
        "job_type": job_type,
        "job_id": job_id,
        "ts": datetime.now().isoformat(),
        **kwargs
    }
    _publish_event(event, job_id)
    logger.info("ml_event_published", job_id=job_id, status=status, job_type=job_type)
