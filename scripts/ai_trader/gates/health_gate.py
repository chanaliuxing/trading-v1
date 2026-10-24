import json
import datetime
import time
import os
import sys
import subprocess
from zoneinfo import ZoneInfo
from pathlib import Path

NY_TZ = ZoneInfo("America/New_York")


class HealthGate:
    """Reads logs/system_health.json and returns gating decision.

    Enhanced version with auto-refresh capability to ensure data freshness.
    """

    def __init__(self, report_path: str, max_age_sec: int = 600):
        """Initialize HealthGate.

        Args:
            report_path: Path to system_health.json
            max_age_sec: Maximum age of health data before refresh (default: 600s = 10min)
        """
        self.report_path = Path(report_path)
        self.max_age_sec = max_age_sec

    def check(self, auto_refresh: bool = True) -> dict:
        """Check health status, with optional auto-refresh if data is stale.

        Args:
            auto_refresh: If True, automatically refresh health data if stale

        Returns:
            dict with keys:
                - ok: bool, True if system is healthy
                - status: str, "healthy", "warning", "error", "stale", or "missing"
                - at: str, timestamp when health data was generated
                - data_age_sec: float, age of health data in seconds
                - raw: dict, raw health check data
                - reason: str, explanation if not ok
        """
        # 1. Check if file exists
        if not self.report_path.exists():
            if auto_refresh:
                # File missing, try to create it
                success = self._run_health_check()
                if not success or not self.report_path.exists():
                    return {
                        "ok": False,
                        "status": "missing",
                        "reason": "system_health.json not found and refresh failed"
                    }
            else:
                return {
                    "ok": False,
                    "status": "missing",
                    "reason": "system_health.json not found"
                }

        # 2. Check file age
        try:
            file_mtime = os.path.getmtime(self.report_path)
            file_age = time.time() - file_mtime
        except Exception as e:
            return {
                "ok": False,
                "status": "error",
                "reason": f"Cannot read file timestamp: {e}"
            }

        # 3. Refresh if stale
        if file_age > self.max_age_sec:
            if auto_refresh:
                # Data is stale, refresh it
                success = self._run_health_check()
                if success:
                    # Re-read file age after refresh
                    try:
                        file_mtime = os.path.getmtime(self.report_path)
                        file_age = time.time() - file_mtime
                    except Exception:
                        pass  # Use old file_age
            else:
                # Return stale warning without refreshing
                try:
                    data = json.loads(self.report_path.read_text("utf-8"))
                    return {
                        "ok": False,
                        "status": "stale",
                        "reason": f"Health data is {file_age:.0f}s old (threshold: {self.max_age_sec}s)",
                        "data_age_sec": file_age,
                        "at": data.get("timestamp_readable", "unknown"),
                        "raw": data,
                    }
                except Exception as e:
                    return {
                        "ok": False,
                        "status": "error",
                        "reason": f"Stale data and cannot read: {e}"
                    }

        # 4. Read and parse health data
        try:
            data = json.loads(self.report_path.read_text("utf-8"))
            status = data.get("overall_status", "error")
            ok = status == "healthy"

            return {
                "ok": ok,
                "status": status,
                "at": data.get("timestamp_readable", "unknown"),
                "data_age_sec": file_age,
                "raw": data,
            }
        except Exception as e:
            return {
                "ok": False,
                "status": "error",
                "reason": f"invalid_json: {e}",
                "data_age_sec": file_age,
            }

    def _run_health_check(self) -> bool:
        """Run health check script.

        Returns:
            bool: True if health check succeeded, False otherwise
        """
        try:
            # Find project root and health check script
            project_root = self.report_path.parent.parent
            script = project_root / "docs" / "daily" / "system_health_check.py"

            if not script.exists():
                return False

            # Run health check with timeout
            result = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(project_root),
                timeout=30,
                capture_output=True,
                text=True
            )

            # Check if it succeeded (exit code 0, 1, or 2 are all acceptable)
            # 0 = healthy, 1 = error, 2 = warning
            return result.returncode in [0, 1, 2]

        except subprocess.TimeoutExpired:
            # Timeout is not fatal, health check might still have written file
            return self.report_path.exists()
        except Exception:
            # Any other error
            return False
