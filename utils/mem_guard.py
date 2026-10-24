"""
Memory Watchdog for Alpha Loop
------------------------------
Monitors memory usage and triggers graceful degradation before OOM kill.

Usage:
    guard = MemoryGuard(max_mem_gb=12.0)
    guard.check("after_data_load")  # Raises MemoryBudgetExceeded if over limit
    stats = guard.get_stats()
"""

import os
import psutil
from typing import Dict, Optional
import structlog

logger = structlog.get_logger(__name__)


class MemoryBudgetExceeded(RuntimeError):
    """Raised when memory usage exceeds soft threshold."""
    def __init__(self, message: str, stats: Dict):
        super().__init__(message)
        self.stats = stats


class MemoryGuard:
    """Monitor process memory and enforce soft limits."""

    def __init__(self, max_mem_gb: float = 14.0, warn_threshold: float = 0.85):
        """
        Args:
            max_mem_gb: Maximum memory in GB before triggering degradation
            warn_threshold: Warn at this fraction of max (default 85%)
        """
        self.max_mem_bytes = int(max_mem_gb * 1024 * 1024 * 1024)
        self.warn_threshold = warn_threshold
        self.process = psutil.Process(os.getpid())
        self.peak_rss_mb = 0
        self.checkpoints = []

        logger.info(
            "memory_guard_initialized",
            max_mem_gb=max_mem_gb,
            warn_threshold_pct=int(warn_threshold * 100)
        )

    def get_stats(self) -> Dict:
        """Get current memory statistics."""
        try:
            mem_info = self.process.memory_info()
            rss_mb = mem_info.rss / (1024 * 1024)
            self.peak_rss_mb = max(self.peak_rss_mb, rss_mb)

            return {
                "rss_mb": round(rss_mb, 1),
                "peak_rss_mb": round(self.peak_rss_mb, 1),
                "max_mem_mb": round(self.max_mem_bytes / (1024 * 1024), 1),
                "usage_pct": round((mem_info.rss / self.max_mem_bytes) * 100, 1),
                "warn_threshold_mb": round((self.max_mem_bytes * self.warn_threshold) / (1024 * 1024), 1)
            }
        except Exception as e:
            logger.warning("memory_stats_failed", err=str(e))
            return {"error": str(e)}

    def check(self, checkpoint_name: str = "unknown", round_idx: Optional[int] = None):
        """
        Check memory usage at a checkpoint.

        Args:
            checkpoint_name: Name of the checkpoint (e.g., "after_data_load")
            round_idx: Current retry round number

        Raises:
            MemoryBudgetExceeded: If memory exceeds max_mem_bytes
        """
        stats = self.get_stats()

        if "error" in stats:
            return  # Skip check if we can't get stats

        checkpoint_data = {
            "checkpoint": checkpoint_name,
            "round": round_idx,
            **stats
        }
        self.checkpoints.append(checkpoint_data)

        rss_bytes = stats["rss_mb"] * 1024 * 1024
        usage_pct = stats["usage_pct"]

        # Warn if approaching limit
        if rss_bytes >= (self.max_mem_bytes * self.warn_threshold):
            logger.warning(
                "memory_guard_warning",
                checkpoint=checkpoint_name,
                round=round_idx,
                **stats
            )

        # Trigger degradation if exceeded
        if rss_bytes >= self.max_mem_bytes:
            logger.error(
                "memory_budget_exceeded",
                checkpoint=checkpoint_name,
                round=round_idx,
                **stats
            )
            raise MemoryBudgetExceeded(
                f"Memory limit exceeded at {checkpoint_name}: {stats['rss_mb']:.1f} MB > {stats['max_mem_mb']:.1f} MB",
                stats=stats
            )

        # Log periodic checkpoints
        logger.info(
            "memory_checkpoint",
            checkpoint=checkpoint_name,
            round=round_idx,
            rss_mb=stats["rss_mb"],
            peak_mb=stats["peak_rss_mb"],
            usage_pct=usage_pct
        )

    def get_checkpoints(self):
        """Return all checkpoint history."""
        return self.checkpoints
