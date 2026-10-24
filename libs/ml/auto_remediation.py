#!/usr/bin/env python3
"""
Auto-Remediation Module - 数据问题自动修复

当 DataReadinessChecker 检测到问题时,自动执行修复操作:
- needs_sync → 触发 sync_features_active.py
- needs_window_adjust → 自动应用窗口调整
- label_coverage_low → 触发标签补算

Usage:
    from libs.ml.auto_remediation import AutoRemediator, RemediationResult

    remediator = AutoRemediator()
    result = remediator.remediate(
        readiness_result=readiness_result,
        symbol="TSLA",
        start=start_date,
        end=end_date
    )

    if result.success:
        # 修复成功,可以继续训练
    else:
        # 修复失败,需要人工介入
"""
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Dict, Any, List
import time

from libs.logger import get_logger
from libs.ml.data_readiness_checker import ReadinessRecommendation, DataReadinessChecker
from libs.ml.event_emitter import emit_auto_remediation_event

logger = get_logger("ml.auto_remediation")

# Project root for running tools
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class RemediationResult:
    """修复结果"""
    success: bool
    action_taken: str
    reason: str
    duration_sec: float
    metadata: Optional[Dict[str, Any]] = None


class AutoRemediator:
    """数据问题自动修复器"""

    def __init__(self, dry_run: bool = False, max_retries: int = 2):
        """
        Args:
            dry_run: 如果为 True,只记录日志不实际执行
            max_retries: 修复失败时的最大重试次数
        """
        self.dry_run = dry_run
        self.max_retries = max_retries

    def remediate(
        self,
        readiness_result: ReadinessRecommendation,
        symbol: str,
        start: date,
        end: date,
        horizon: str = "30m",
        features_table: Optional[str] = None
    ) -> RemediationResult:
        """根据 ReadinessRecommendation 执行自动修复

        Args:
            readiness_result: DataReadinessChecker 的检查结果
            symbol: 股票代码
            start: 训练窗口开始日期
            end: 训练窗口结束日期
            horizon: 预测 horizon
            features_table: 特征表名称

        Returns:
            RemediationResult
        """
        logger.info(
            "auto_remediation_start",
            symbol=symbol,
            status=readiness_result.status,
            reason=readiness_result.reason,
            dry_run=self.dry_run
        )

        start_time = time.time()

        # 根据状态分发修复动作
        if readiness_result.status == "ready":
            return RemediationResult(
                success=True,
                action_taken="none",
                reason="data_already_ready",
                duration_sec=0.0
            )

        elif readiness_result.status == "needs_sync":
            return self._remediate_sync(
                symbol, start, end, readiness_result, start_time
            )

        elif readiness_result.status == "needs_window_adjust":
            return self._remediate_window_adjust(
                symbol, readiness_result, start_time
            )

        elif readiness_result.status == "not_ready":
            # 检查具体原因
            if "label_coverage_low" in readiness_result.reason:
                return self._remediate_labels(
                    symbol, start, end, horizon, readiness_result, start_time
                )
            else:
                # 其他 not_ready 原因暂不自动修复
                return RemediationResult(
                    success=False,
                    action_taken="none",
                    reason=f"cannot_auto_fix_{readiness_result.reason}",
                    duration_sec=time.time() - start_time,
                    metadata={"status": readiness_result.status}
                )

        else:
            # 未知状态
            return RemediationResult(
                success=False,
                action_taken="none",
                reason=f"unknown_status_{readiness_result.status}",
                duration_sec=time.time() - start_time
            )

    def _remediate_sync(
        self,
        symbol: str,
        start: date,
        end: date,
        readiness_result: ReadinessRecommendation,
        start_time: float
    ) -> RemediationResult:
        """修复: needs_sync → 触发 sync_features_active.py"""
        logger.info(
            "remediate_sync",
            symbol=symbol,
            action=readiness_result.action,
            dry_run=self.dry_run
        )

        if self.dry_run:
            return RemediationResult(
                success=True,
                action_taken="sync_features_active",
                reason="dry_run_mode",
                duration_sec=0.0,
                metadata={"would_sync": True}
            )

        # 构建同步命令
        sync_script = PROJECT_ROOT / "tools" / "sync_features_active.py"
        cmd = [
            sys.executable,
            str(sync_script),
            "--symbols", symbol,
            "--start", start.isoformat(),
            "--end", end.isoformat(),
            "--validate",
            "--batch-days", "7"
        ]

        logger.info("running_sync_command", cmd=" ".join(cmd))

        try:
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=600  # 10分钟超时
            )

            elapsed = time.time() - start_time

            if result.returncode == 0:
                logger.info(
                    "sync_success",
                    symbol=symbol,
                    duration_sec=elapsed,
                    stdout_lines=len(result.stdout.splitlines())
                )

                # 重新检查数据是否就绪
                checker = DataReadinessChecker()
                recheck = checker.check_training_window(
                    symbol=symbol,
                    start=start,
                    end=end,
                    horizon="30m",
                    features_table=readiness_result.features_coverage.table_name if readiness_result.features_coverage else None
                )

                remediation_result = RemediationResult(
                    success=(recheck.status == "ready"),
                    action_taken="sync_features_active",
                    reason="sync_completed_and_rechecked",
                    duration_sec=elapsed,
                    metadata={
                        "recheck_status": recheck.status,
                        "sync_stdout": result.stdout[-500:]  # Last 500 chars
                    }
                )

                # Meta v3.4: Emit remediation event
                emit_auto_remediation_event(
                    action_taken="sync_features_active",
                    status="success" if remediation_result.success else "failed",
                    symbol=symbol,
                    duration_sec=elapsed,
                    reason=remediation_result.reason,
                    metadata=remediation_result.metadata
                )

                return remediation_result
            else:
                logger.error(
                    "sync_failed",
                    symbol=symbol,
                    returncode=result.returncode,
                    stderr=result.stderr[:500]
                )
                remediation_result = RemediationResult(
                    success=False,
                    action_taken="sync_features_active",
                    reason=f"sync_failed_returncode_{result.returncode}",
                    duration_sec=elapsed,
                    metadata={"stderr": result.stderr[:500]}
                )

                # Meta v3.4: Emit failure event
                emit_auto_remediation_event(
                    action_taken="sync_features_active",
                    status="failed",
                    symbol=symbol,
                    duration_sec=elapsed,
                    reason=remediation_result.reason,
                    metadata=remediation_result.metadata
                )

                return remediation_result

        except subprocess.TimeoutExpired:
            logger.error("sync_timeout", symbol=symbol)
            return RemediationResult(
                success=False,
                action_taken="sync_features_active",
                reason="sync_timeout",
                duration_sec=time.time() - start_time
            )
        except Exception as e:
            logger.error("sync_exception", symbol=symbol, error=str(e), exc_info=True)
            return RemediationResult(
                success=False,
                action_taken="sync_features_active",
                reason=f"sync_exception_{type(e).__name__}",
                duration_sec=time.time() - start_time,
                metadata={"exception": str(e)}
            )

    def _remediate_window_adjust(
        self,
        symbol: str,
        readiness_result: ReadinessRecommendation,
        start_time: float
    ) -> RemediationResult:
        """修复: needs_window_adjust → 返回建议窗口 (暂不自动应用)"""
        logger.info(
            "remediate_window_adjust",
            symbol=symbol,
            suggested_start=readiness_result.suggested_start.isoformat() if readiness_result.suggested_start else None,
            suggested_end=readiness_result.suggested_end.isoformat() if readiness_result.suggested_end else None
        )

        # 窗口调整需要调用方决定,这里只记录建议
        return RemediationResult(
            success=True,
            action_taken="window_adjustment_suggested",
            reason="manual_intervention_recommended",
            duration_sec=time.time() - start_time,
            metadata={
                "suggested_start": readiness_result.suggested_start.isoformat() if readiness_result.suggested_start else None,
                "suggested_end": readiness_result.suggested_end.isoformat() if readiness_result.suggested_end else None,
                "adjusted_days": (readiness_result.suggested_end - readiness_result.suggested_start).days if readiness_result.suggested_start and readiness_result.suggested_end else None
            }
        )

    def _remediate_labels(
        self,
        symbol: str,
        start: date,
        end: date,
        horizon: str,
        readiness_result: ReadinessRecommendation,
        start_time: float
    ) -> RemediationResult:
        """修复: label_coverage_low → 触发标签补算 (暂不实现)"""
        logger.warning(
            "remediate_labels_not_implemented",
            symbol=symbol,
            horizon=horizon
        )

        # TODO: 实现标签补算逻辑
        # 需要调用 tools/recompute_labels_1m.py

        return RemediationResult(
            success=False,
            action_taken="label_recomputation",
            reason="not_implemented_yet",
            duration_sec=time.time() - start_time,
            metadata={
                "labels_coverage": readiness_result.labels_coverage.utilization if readiness_result.labels_coverage else None
            }
        )

    def remediate_batch(
        self,
        readiness_results: Dict[str, ReadinessRecommendation],
        start: date,
        end: date,
        horizon: str = "30m",
        features_table: Optional[str] = None
    ) -> Dict[str, RemediationResult]:
        """批量修复多个 symbols

        Args:
            readiness_results: symbol → ReadinessRecommendation 的字典
            start: 训练窗口开始日期
            end: 训练窗口结束日期
            horizon: 预测 horizon
            features_table: 特征表名称

        Returns:
            Dict[symbol, RemediationResult]
        """
        logger.info(
            "batch_remediation_start",
            symbols=list(readiness_results.keys()),
            dry_run=self.dry_run
        )

        results = {}
        for symbol, readiness_result in readiness_results.items():
            results[symbol] = self.remediate(
                readiness_result,
                symbol,
                start,
                end,
                horizon,
                features_table
            )

        # 统计
        success_count = sum(1 for r in results.values() if r.success)
        logger.info(
            "batch_remediation_complete",
            total=len(results),
            success=success_count,
            failed=len(results) - success_count
        )

        return results
