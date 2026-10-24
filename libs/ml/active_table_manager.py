#!/usr/bin/env python3
from __future__ import annotations

"""
Active Table Manager for automated sync/fallback logic.

Responsibilities:
- Check active table health from mutable_targets snapshot
- Trigger sync when health = sync_or_fallback
- Fallback to baseline on sync failure
- Record events for observability
"""

import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

try:
    from libs.db.ch_pool import get_client
except Exception:
    get_client = None  # type: ignore


@dataclass
class SyncResult:
    success: bool
    action: str  # "sync" | "fallback" | "no_action"
    rows_synced: int = 0
    error: Optional[str] = None
    features_table: str = "trading.features_1m"
    event: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ActiveTableManager:
    """
    Manages active feature table synchronization and fallback logic.
    """

    def __init__(
        self,
        baseline_table: str = "trading.features_1m",
        active_table: str = "trading.features_1m_active",
        snapshot_path: str = "runs/memory/mutable_targets.json",
    ):
        self.baseline_table = baseline_table
        self.active_table = active_table
        self.snapshot_path = Path(snapshot_path)

    def check_should_sync(self, symbols: Optional[List[str]] = None) -> bool:
        """
        Check mutable_targets snapshot to see if active table needs sync.

        Returns True if recommendation is "sync_or_fallback"
        """
        try:
            if not self.snapshot_path.exists():
                return False

            data = json.loads(self.snapshot_path.read_text())
            # Key format: "data:features_1m_active"
            active_key = f"data:{self.active_table.split('.')[-1]}"
            active = data.get(active_key) or {}
            metrics = active.get("metrics") or {}
            recommendation = metrics.get("recommendation")

            return recommendation == "sync_or_fallback"

        except Exception:
            return False

    def sync_active_from_baseline(
        self,
        symbols: List[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        chunk_days: int = 1,
    ) -> SyncResult:
        """
        Sync active table from baseline using tools/sync_features_active.py

        Args:
            symbols: List of symbols to sync
            start: Start datetime (default: 30 days ago)
            end: End datetime (default: now)
            chunk_days: Batch size for sync (default: 1 day per chunk)

        Returns:
            SyncResult with success status and metadata
        """
        event_start = datetime.now(timezone.utc)

        # Default window: last 30 days
        if end is None:
            end = datetime.now(timezone.utc).replace(hour=16, minute=0, second=0, microsecond=0)
        if start is None:
            start = end - timedelta(days=30)

        start_str = start.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end.strftime("%Y-%m-%d %H:%M:%S")
        symbols_str = ",".join(symbols)

        # Construct command
        sync_script = Path(__file__).resolve().parents[2] / "tools" / "sync_features_active.py"
        cmd = [
            sys.executable,
            str(sync_script),
            "--symbols",
            symbols_str,
            "--start",
            start_str,
            "--end",
            end_str,
            "--source",
            self.baseline_table,
            "--target",
            self.active_table,
            "--chunk-days",
            str(chunk_days),
        ]

        try:
            # Execute sync
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=600,  # 10 minutes timeout
                check=False,
            )

            rows_synced = 0
            if result.returncode == 0:
                # Parse output: "synced_rows=12345"
                for line in result.stdout.splitlines():
                    if "synced_rows=" in line:
                        try:
                            rows_synced = int(line.split("=")[1].strip())
                        except Exception:
                            pass

                # Generate success event
                event = self._create_event(
                    event_type="sync_completed",
                    symbols=symbols,
                    start=start_str,
                    end=end_str,
                    rows_synced=rows_synced,
                    duration_sec=(datetime.now(timezone.utc) - event_start).total_seconds(),
                )

                return SyncResult(
                    success=True,
                    action="sync",
                    rows_synced=rows_synced,
                    features_table=self.active_table,
                    event=event,
                )
            else:
                # Sync failed
                error_msg = result.stderr or result.stdout or "unknown error"
                event = self._create_event(
                    event_type="sync_failed",
                    symbols=symbols,
                    start=start_str,
                    end=end_str,
                    error=error_msg,
                    duration_sec=(datetime.now(timezone.utc) - event_start).total_seconds(),
                )

                return SyncResult(
                    success=False,
                    action="sync",
                    error=error_msg,
                    features_table=self.active_table,
                    event=event,
                )

        except subprocess.TimeoutExpired:
            event = self._create_event(
                event_type="sync_timeout",
                symbols=symbols,
                start=start_str,
                end=end_str,
                error="sync timeout after 600s",
                duration_sec=600,
            )
            return SyncResult(
                success=False,
                action="sync",
                error="timeout",
                features_table=self.active_table,
                event=event,
            )
        except Exception as e:
            event = self._create_event(
                event_type="sync_error",
                symbols=symbols,
                start=start_str,
                end=end_str,
                error=str(e),
                duration_sec=(datetime.now(timezone.utc) - event_start).total_seconds(),
            )
            return SyncResult(
                success=False,
                action="sync",
                error=str(e),
                features_table=self.active_table,
                event=event,
            )

    def fallback_to_baseline(self) -> SyncResult:
        """
        Fallback to baseline table when active sync fails.

        Returns:
            SyncResult indicating fallback action
        """
        event = self._create_event(
            event_type="fallback_to_baseline",
            reason="active_sync_failed",
            baseline_table=self.baseline_table,
        )

        return SyncResult(
            success=True,
            action="fallback",
            features_table=self.baseline_table,
            event=event,
        )

    def auto_sync_or_fallback(
        self,
        symbols: List[str],
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> SyncResult:
        """
        Automated sync-or-fallback flow:
        1. Check if sync is needed (from snapshot health)
        2. If needed, attempt sync
        3. On sync failure, fallback to baseline

        Returns:
            SyncResult with final decision
        """
        # Check if sync is needed
        if not self.check_should_sync(symbols):
            return SyncResult(
                success=True,
                action="no_action",
                features_table=self.active_table,
                event=self._create_event(event_type="sync_not_needed"),
            )

        # Attempt sync
        sync_result = self.sync_active_from_baseline(symbols, start, end)

        if sync_result.success:
            return sync_result
        else:
            # Sync failed, fallback to baseline
            fallback_result = self.fallback_to_baseline()
            # Combine events
            fallback_result.event["sync_error"] = sync_result.error
            return fallback_result

    def _create_event(self, event_type: str, **kwargs) -> Dict[str, Any]:
        """Create structured event for logging"""
        return {
            "event": f"active_table.{event_type}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "active_table": self.active_table,
            "baseline_table": self.baseline_table,
            **kwargs,
        }

    def verify_active_table(self, symbols: List[str], min_rows: int = 1000) -> tuple[bool, int]:
        """
        Verify active table has sufficient data after sync.

        Returns:
            (is_healthy, row_count)
        """
        if not get_client:
            return False, 0

        try:
            ch = get_client()
            result = ch.query(
                f"""
                SELECT count() AS rows
                FROM {self.active_table}
                WHERE symbol IN %(syms)s
                  AND ts >= now() - INTERVAL 30 DAY
                """,
                {"syms": symbols},
            )
            rows = int(result[0].get("rows", 0) or 0) if result else 0
            return rows >= min_rows, rows
        except Exception:
            return False, 0
