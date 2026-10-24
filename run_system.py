#!/usr/bin/env python3
"""
Trading System - Real Production Entry Point
Starts ALL REAL services with IB Gateway connection
"""
import asyncio
import atexit
import fcntl
import signal
import sys
import os
import copy
import hashlib
import json
import warnings
import faulthandler
from importlib import import_module
from pathlib import Path
from datetime import datetime, time as _time
from typing import Any, Dict, List, Set
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Enable async debugging and fault handler for diagnostics
# PYTHONASYNCIODEBUG: Enables slow callback warnings (>100ms) - disable in production to reduce noise
# os.environ['PYTHONASYNCIODEBUG'] = '1'  # Commented out: causes too many warnings for normal async operations
os.environ['PYTHONTRACEMALLOC'] = '1'  # Keep tracemalloc for memory leak detection
faulthandler.enable()  # Keep fault handler for crash diagnostics

# Suppress ResourceWarning for unclosed event loops
# This is a known issue with ib_insync 0.9.86 and Python 3.13+
# The library creates internal event loops that trigger warnings on cleanup
# The warning is harmless and will be fixed in future ib_insync versions
warnings.simplefilter("ignore", ResourceWarning)

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

import boot_logging  # noqa: F401  # Ensure audit logging configured before other imports

from libs.logger import get_logger
from obs.audit import log_event
from libs.strategy import get_registry
from libs.config import config
from services.market.writer.ch_writer import ClickHouseBarWriter
from services.market.writer.lake_writer import ClickHouseLakeWriter
from services.metrics.exporter import start_metrics_http_server, mark_service_up
from services.obs.writers import ClickHouseWriter, LokiWriter
from libs.obs.audit_writer import LocalAuditWriter
from libs.obs.queue import ObservabilityQueue
from libs.cache.price_cache import PriceCache, Tick
from services.health import SystemHealthController
from tool.report.build_live_strategy_surface_report import (
    build_live_strategy_surface_report,
    evaluate_phase0_freeze_gate,
)
from tool.report.build_uom_caller_surface_report import (
    build_uom_caller_surface_report,
    evaluate_uom_caller_gate,
)

logger = get_logger(__name__)
_SMOKE_RESULT_PREFIX = "RUN_SYSTEM_SMOKE_CHECK_RESULT "
_RUN_SYSTEM_PROCESS_START_ET = datetime.now(ZoneInfo("America/New_York"))


def _effective_trading_mode() -> str:
    return str(os.getenv("TRADING_MODE") or config.get("trading.mode", "paper") or "paper").strip().lower()


def _hash_file(path: Path) -> str | None:
    try:
        if not path.exists() or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except Exception:
        return None


def _write_boot_production_state_artifact(*, stage: str, reason: str, details: Dict[str, Any] | None = None) -> None:
    """Write a current-PID no-go artifact before late production emitters exist."""
    try:
        now = datetime.now(ZoneInfo("America/New_York"))
        root = Path(__file__).parent
        out_dir = root / "runtime" / "readiness" / "production_state"
        out_dir.mkdir(parents=True, exist_ok=True)
        config_path = root / "config.yml"
        payload: Dict[str, Any] = {
            "artifact_type": "production_state",
            "generated_at": now.isoformat(),
            "runtime_pid": os.getpid(),
            "runtime_process_start_time": _RUN_SYSTEM_PROCESS_START_ET.isoformat(),
            "runtime_git_sha": "",
            "runtime_config_hash": _hash_file(config_path),
            "runtime_lifecycle_generation": f"boot:{os.getpid()}:{int(_RUN_SYSTEM_PROCESS_START_ET.timestamp())}",
            "artifact_source_pid": os.getpid(),
            "artifact_source_component": "run_system_boot",
            "go_no_go": "no_go",
            "status": "HOLD",
            "failure_count": 1,
            "failures": [
                {
                    "source": "run_system_boot",
                    "issue_key": "startup_incomplete",
                    "severity": "hold",
                    "stage": str(stage),
                    "reason": str(reason),
                }
            ],
            "boot": {
                "stage": str(stage),
                "reason": str(reason),
                "details": details or {},
            },
            "runtime_version": {
                "process_pid": os.getpid(),
                "process_start_time": _RUN_SYSTEM_PROCESS_START_ET.isoformat(),
                "config_hash": _hash_file(config_path),
                "config_files": [{"path": str(config_path), "sha256": _hash_file(config_path)}],
            },
        }
        target = out_dir / "production_state_latest.json"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        tmp.replace(target)
        legacy_target = root / "runtime" / "readiness" / "production_state_latest.json"
        legacy_payload = dict(payload)
        legacy_payload["deprecated_alias"] = True
        legacy_payload["canonical_path"] = str(target)
        legacy_tmp = legacy_target.with_suffix(".tmp")
        legacy_tmp.write_text(json.dumps(legacy_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        legacy_tmp.replace(legacy_target)
    except Exception as exc:
        logger.warning(
            "run_system_boot_production_state_write_failed",
            stage=stage,
            reason=reason,
            error=str(exc),
        )

# Suppress noisy IBKR option-chain warnings that are expected during chain scanning.
# We still keep IB connection / order-level errors visible.
import logging as _logging
_ib_wrapper_logger = _logging.getLogger("ib_insync.wrapper")
_ib_core_logger = _logging.getLogger("ib_insync.ib")
_kafka_gc_logger = _logging.getLogger("aiokafka.consumer.group_coordinator")


class _IbNoiseFilter(_logging.Filter):
    def filter(self, record: _logging.LogRecord) -> bool:  # type: ignore[override]
        msg = record.getMessage()
        if "No security definition has been found for the request" in msg:
            return False
        if "Unknown contract:" in msg:
            return False
        try:
            from services.exec.adapters.ib_adapter import is_expected_cancel_not_found_message
            if is_expected_cancel_not_found_message(msg):
                return False
        except Exception:
            pass
        return True


_ib_wrapper_logger.addFilter(_IbNoiseFilter())
_ib_core_logger.addFilter(_IbNoiseFilter())
_kafka_gc_logger.setLevel(_logging.ERROR)

# Ensure .env is loaded before reading environment variables
load_dotenv()

# Global shutdown event for graceful shutdown coordination
SHUTDOWN_EVENT = asyncio.Event()

# Global observability queue (initialized in main)
obs_queue = None
audit_writer = None
_RUN_SYSTEM_LOCK_HANDLE = None
_RUN_SYSTEM_LOCK_PATH: Path | None = None
_RUN_SYSTEM_ATEXIT_REGISTERED = False


def _run_system_lock_path() -> Path:
    return Path("runtime/locks/run_system.lock")


def _acquire_run_system_lock() -> bool:
    global _RUN_SYSTEM_LOCK_HANDLE, _RUN_SYSTEM_LOCK_PATH, _RUN_SYSTEM_ATEXIT_REGISTERED
    lock_path = _run_system_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    try:
        # Important: do not truncate before lock acquisition.
        # Opening with "w" can wipe the pid while another instance still holds flock.
        handle = open(lock_path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fsync(handle.fileno())
        _RUN_SYSTEM_LOCK_HANDLE = handle
        _RUN_SYSTEM_LOCK_PATH = lock_path
        if not _RUN_SYSTEM_ATEXIT_REGISTERED:
            atexit.register(_release_run_system_lock)
            _RUN_SYSTEM_ATEXIT_REGISTERED = True
        return True
    except OSError:
        try:
            if handle is not None:
                handle.close()
        except Exception:
            pass
        return False


def _release_run_system_lock() -> None:
    global _RUN_SYSTEM_LOCK_HANDLE, _RUN_SYSTEM_LOCK_PATH
    handle = _RUN_SYSTEM_LOCK_HANDLE
    _RUN_SYSTEM_LOCK_HANDLE = None
    _RUN_SYSTEM_LOCK_PATH = None
    if handle is None:
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        handle.close()
    except Exception:
        pass


class RealTradingSystem:
    """Real trading system with actual service implementations"""

    def __init__(self):
        self.services = []
        self.running = False
        self.pg = None
        self.telegram_command_bot = None
        self._ch_writers = []
        self._lake_writers = []
        self._backfills = []
        self._catchups = []
        self._feature_calcs = []
        self.obs_ch_writer = None
        self.obs_loki_writer = None
        self.audit_writer = None
        self.price_cache = None
        self._price_cache_tasks = []
        self._price_cache_consumers = []
        self._rfe_fallback_task = None
        self.feature_calc_1m_fallback = None
        self.notification_manager = None
        self.system_health_controller = None
        self.sfs_1s_service = None
        self._sfs_1s_task = None
        self._last_sfs_1s_restart_mono = 0.0
        self._phase0_live_strategy_ids: Set[str] = set()
        self._phase0_blocked_strategy_ids: Set[str] = set()
        self._operator_ib_restart_drill_task = None

    @staticmethod
    def _ib_startup_status_snapshot(
        ib_manager: Any,
        *,
        exec_enabled: bool,
        master_enabled: bool | None = None,
    ) -> Dict[str, Any]:
        market_ready = bool(ib_manager.market_connected())
        exec_ready = bool(ib_manager.exec_connected()) if exec_enabled else True
        master_required = exec_enabled if master_enabled is None else bool(master_enabled)
        master_ready = bool(ib_manager.master_connected()) if master_required else True
        master_truth_complete = bool(ib_manager.master_truth_complete()) if master_required else True
        return {
            "market_ready": market_ready,
            "exec_ready": exec_ready,
            "master_ready": master_ready,
            "master_truth_complete": master_truth_complete,
            "ready": bool(market_ready and exec_ready and master_ready and master_truth_complete),
        }

    @staticmethod
    def _runtime_reconciliation_resume_metadata(startup_result: Any) -> Dict[str, Any]:
        metadata = dict(getattr(startup_result, "metadata", None) or {})
        order_truth = dict(metadata.get("order_truth") or {})
        position_truth = dict(metadata.get("position") or {})
        ghosts = int(metadata.get("ghosts_found") or order_truth.get("ghosts_found") or 0)
        orphans = int(metadata.get("orphans_found") or order_truth.get("orphans_found") or 0)
        mismatches = int(metadata.get("mismatches") or order_truth.get("mismatches") or 0)
        position_mismatches = int(
            metadata.get("position_mismatches") or position_truth.get("mismatch_count") or 0
        )
        active_quarantine_missing = not (
            "active_quarantine_count" in metadata
            or "active_quarantine_count" in order_truth
            or "active_quarantine_count" in position_truth
        )
        active_quarantine_count = int(
            metadata.get("active_quarantine_count")
            or order_truth.get("active_quarantine_count")
            or position_truth.get("active_quarantine_count")
            or 0
        )
        return {
            "broker_connected": True,
            "broker_truth_complete": True,
            "order_truth_clean": ghosts == 0 and orphans == 0 and mismatches == 0,
            "position_ledger_clean": position_mismatches == 0,
            "active_quarantine_clean": (not active_quarantine_missing) and active_quarantine_count == 0,
            "active_quarantine_evidence_present": not active_quarantine_missing,
            "strategies_paused_ack": True,
            "ghosts_found": ghosts,
            "orphans_found": orphans,
            "mismatches": mismatches,
            "position_mismatches": position_mismatches,
            "active_quarantine_count": active_quarantine_count,
        }

    @staticmethod
    def _build_scoped_tradability_inputs(context: Dict[str, Any]) -> Any:
        from services.production.scoped_tradability import (
            TradabilityInputs,
            TradabilityScope,
            build_tradability_inputs_from_readiness_artifacts,
        )

        scope = (context or {}).get("scope")
        if not isinstance(scope, TradabilityScope):
            return TradabilityInputs()
        return build_tradability_inputs_from_readiness_artifacts(
            repo_root=Path(__file__).resolve().parent,
            scope=scope,
        )

    def _start_account_snapshot_task(self, exec_adapter: Any) -> None:
        enabled = str(os.getenv("RUN_SYSTEM_ACCOUNT_SNAPSHOT_ENABLED", "true")).lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        if not enabled or exec_adapter is None:
            return
        interval_sec = max(30.0, float(os.getenv("RUN_SYSTEM_ACCOUNT_SNAPSHOT_INTERVAL_SEC", "300")))
        task = asyncio.create_task(
            self._account_snapshot_loop(exec_adapter, interval_sec=interval_sec),
            name="account_snapshot_writer",
        )
        self.services.append(("account_snapshot_writer", task))
        logger.info("account_snapshot_writer_started", interval_sec=interval_sec)

    async def _account_snapshot_loop(self, exec_adapter: Any, *, interval_sec: float) -> None:
        while self.running:
            try:
                await self._sync_account_snapshot_once(exec_adapter)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("account_snapshot_writer_failed", error=str(exc), exc_info=True)
            await asyncio.sleep(interval_sec)

    async def _sync_account_snapshot_once(self, exec_adapter: Any) -> None:
        ib = getattr(exec_adapter, "ib", None)
        if ib is None or not bool(getattr(ib, "isConnected", lambda: False)()):
            logger.warning("account_snapshot_writer_ib_unavailable")
            return

        from tool.sync_ibkr_realtime import CREATE_EQUITY_REALTIME_TABLE, _safe_float
        from libs.db.clickhouse import ClickHouseDB

        account = str(getattr(exec_adapter, "account", "") or os.getenv("IB_ACCOUNT", "") or "")
        rows = await ib.accountSummaryAsync(account)
        summary: Dict[str, float] = {}
        for item in list(rows or []):
            if str(getattr(item, "currency", "") or "") != "USD":
                continue
            value = _safe_float(getattr(item, "value", None))
            if value is None:
                continue
            summary[str(getattr(item, "tag", "") or "")] = float(value)

        now_ny = datetime.now(ZoneInfo("America/New_York"))
        values = [[
            now_ny,
            now_ny.date(),
            float(summary.get("NetLiquidation", 0.0)),
            float(summary.get("TotalCashValue", 0.0)),
            float(summary.get("GrossPositionValue", 0.0)),
            float(summary.get("UnrealizedPnL", 0.0)),
            float(summary.get("RealizedPnL", 0.0)),
            float(summary.get("BuyingPower", 0.0)),
            float(summary.get("AvailableFunds", 0.0)),
            float(summary.get("ExcessLiquidity", 0.0)),
            float(summary.get("InitMarginReq", 0.0)),
            float(summary.get("MaintMarginReq", 0.0)),
        ]]
        column_names = [
            "ts_ny",
            "date",
            "net_liquidation",
            "total_cash",
            "gross_position_value",
            "unrealized_pnl",
            "realized_pnl",
            "buying_power",
            "available_funds",
            "excess_liquidity",
            "init_margin_req",
            "maint_margin_req",
        ]

        def _write() -> None:
            ch = ClickHouseDB()
            ch.execute(CREATE_EQUITY_REALTIME_TABLE)
            ch.insert("ibkr_account_realtime", values, column_names=column_names)

        await asyncio.to_thread(_write)
        logger.info(
            "account_snapshot_writer_synced",
            net_liquidation=round(float(summary.get("NetLiquidation", 0.0)), 2),
            available_funds=round(float(summary.get("AvailableFunds", 0.0)), 2),
            account=account or None,
        )

    async def _run_phase0_freeze_gate(self) -> Dict[str, Any]:
        report = build_live_strategy_surface_report("config.yml", repo_root=Path(__file__).parent)
        report_dir = Path("reports/strategy_surface")
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"live_strategy_surface_report_{datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d')}.json"
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        freeze_cfg = config.get("phase0.freeze_gate", {}) or {}
        baseline_path = Path(
            str(
                os.getenv("PHASE0_FREEZE_BASELINE_PATH", "")
                or freeze_cfg.get("baseline_path")
                or "reports/strategy_surface/live_strategy_surface_freeze_baseline.json"
            )
        )
        baseline = None
        if baseline_path.exists():
            try:
                baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"phase0_freeze_baseline_unreadable:{baseline_path}:{exc}") from exc
        require_baseline = bool(freeze_cfg.get("require_baseline", True))
        uom_report = build_uom_caller_surface_report(Path(__file__).parent)
        uom_report_dir = Path("reports/orders")
        uom_report_dir.mkdir(parents=True, exist_ok=True)
        uom_report_path = uom_report_dir / f"uom_caller_surface_{datetime.now(ZoneInfo('America/New_York')).strftime('%Y%m%d')}.json"
        uom_report_path.write_text(json.dumps(uom_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        gate = evaluate_phase0_freeze_gate(
            report,
            baseline=baseline,
            require_baseline=require_baseline,
        )
        uom_gate = evaluate_uom_caller_gate(uom_report)
        logger.info(
            "phase0_freeze_gate_evaluated",
            ok=bool(gate.get("ok")) and bool(uom_gate.get("ok")),
            blocking_strategy_ids=list(gate.get("blocking_strategy_ids") or []),
            report_path=str(report_path),
            baseline_path=str(baseline_path),
            baseline_loaded=baseline is not None,
            uom_caller_report_path=str(uom_report_path),
            uom_production_pending_count=int((uom_report.get("summary") or {}).get("production_pending_count") or 0),
            uom_blocking_files=list(uom_gate.get("blocking_files") or []),
            trading_mode=_effective_trading_mode(),
            forbidden_mode_allowed=False,
            forbidden_promotion_allowed=False,
        )
        if not bool(gate.get("ok")):
            raise RuntimeError(
                "phase0_freeze_gate_blocked:" + ",".join(str(x) for x in (gate.get("blocking_reasons") or []))
            )
        if not bool(uom_gate.get("ok")):
            raise RuntimeError(
                "uom_caller_gate_blocked:" + ",".join(str(x) for x in (uom_gate.get("blocking_files") or []))
            )
        self._phase0_live_strategy_ids = {
            str(item).strip()
            for item in (gate.get("current_live_strategy_ids") or [])
            if str(item).strip()
        }
        self._phase0_blocked_strategy_ids = {
            str(item).strip()
            for item in (gate.get("current_forbidden_strategy_ids") or [])
            if str(item).strip()
        }
        return gate

    def _phase0_strategy_admission_provider(
        self,
        *,
        strategy_id: str,
        symbol: str,
        intent_kind: str,
        result: Any = None,
        envelope_extras: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        del symbol, result, envelope_extras
        if str(intent_kind or "").strip().lower() == "cancel":
            return {"allowed": True, "reason": "cancel_allowed"}
        sid = str(strategy_id or "").strip()
        if sid in self._phase0_live_strategy_ids:
            return {"allowed": True, "reason": "phase0_live_strategy_admitted"}
        if sid in self._phase0_blocked_strategy_ids:
            return {
                "allowed": False,
                "reason": "phase0_forbidden_strategy_not_live_admitted",
                "strategy_id": sid,
            }
        return {
            "allowed": False,
            "reason": "phase0_strategy_not_in_live_baseline",
            "strategy_id": sid,
        }
    @staticmethod
    def _is_rth_now() -> bool:
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() > 4:
            return False
        cur = now_et.time()
        return _time(9, 30) <= cur < _time(16, 0)

    async def _start_sfs_1s_service(self) -> bool:
        if os.getenv("DISABLE_SFS_1S", "").lower() in ("1", "true", "yes", "on"):
            logger.info("1️⃣c  SFS-1s Service disabled by env (DISABLE_SFS_1S)")
            return False

        sfs_cfg = config.get("services.sfs_1s", {}) or {}
        if not sfs_cfg.get("enabled", False):
            logger.info("1️⃣c  SFS-1s Service disabled (services.sfs_1s.enabled=false)")
            return False

        from services.market.sfs_1s_service import SFS1sService

        ttl_sec = int(sfs_cfg.get("ttl_sec", 3600))
        flush_delay_ms = int(sfs_cfg.get("flush_delay_ms", 250))
        group_id = str(sfs_cfg.get("group_id", "sfs_1s_agg"))
        topic = str(sfs_cfg.get("topic", "md.tick.trade.v1"))
        sfs_rth_only = bool(sfs_cfg.get("rth_only", False))
        trading_hours_cfg = config.get("trading_hours", {}) or {}

        self.sfs_1s_service = SFS1sService(
            ttl_sec=ttl_sec,
            flush_delay_ms=flush_delay_ms,
            group_id=group_id,
            topic=topic,
            rth_only=sfs_rth_only,
            trading_hours=trading_hours_cfg,
        )
        self._sfs_1s_task = asyncio.create_task(self.sfs_1s_service.start(), name="sfs_1s_service")
        self.services = [(n, t) for n, t in self.services if n != "sfs_1s_service"]
        self.services.append(("sfs_1s_service", self._sfs_1s_task))
        startup_timeout_sec = float(sfs_cfg.get("startup_ready_timeout_sec", 20.0))
        try:
            await asyncio.wait_for(self.sfs_1s_service._startup_ready.wait(), timeout=startup_timeout_sec)
        except Exception as exc:
            logger.warning(
                "1️⃣c  ⚠️  SFS-1s consumer not ready before timeout",
                timeout_sec=startup_timeout_sec,
                error=str(exc),
            )
        logger.info(
            "1️⃣c  ✅ SFS-1s Service started (Redis-only)",
            ttl_sec=ttl_sec,
            flush_delay_ms=flush_delay_ms,
            group_id=group_id,
            topic=topic,
        )
        return True

    async def _stop_sfs_1s_service(self) -> None:
        task = self._sfs_1s_task
        svc = self.sfs_1s_service
        self.services = [(n, t) for n, t in self.services if n != "sfs_1s_service"]
        self._sfs_1s_task = None
        self.sfs_1s_service = None

        if svc is not None:
            try:
                await asyncio.wait_for(svc.stop(), timeout=10.0)
            except Exception as exc:
                logger.warning("sfs_1s_service_stop_failed", error=str(exc))
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except Exception:
                pass

    async def _restart_sfs_1s_service(self, *, reason: str, details: Dict[str, Any] | None = None) -> bool:
        now_mono = asyncio.get_running_loop().time()
        if (now_mono - float(self._last_sfs_1s_restart_mono or 0.0)) < 15.0:
            return False
        self._last_sfs_1s_restart_mono = now_mono
        logger.warning("sfs_1s_service_restarting", reason=reason, details=details or {})
        await self._stop_sfs_1s_service()
        try:
            return await self._start_sfs_1s_service()
        except Exception as exc:
            logger.error("sfs_1s_service_restart_failed", reason=reason, error=str(exc), exc_info=True)
            return False

    async def _maybe_recover_sfs_1s_service(self) -> None:
        svc = self.sfs_1s_service
        if svc is None or not self._is_rth_now():
            return
        sfs_cfg = config.get("services.sfs_1s", {}) or {}
        stall_sec = float(sfs_cfg.get("stall_restart_after_sec", 20.0) or 20.0)
        startup_grace_sec = float(sfs_cfg.get("stall_startup_grace_sec", 45.0) or 45.0)
        if not svc.is_stalled(max_idle_sec=stall_sec, startup_grace_sec=startup_grace_sec):
            return
        await self._restart_sfs_1s_service(reason="stalled", details=svc.health_snapshot())

    def _ensure_notification_manager(self):
        if self.notification_manager is not None:
            return self.notification_manager
        try:
            from services.notifications import build_notification_manager

            self.notification_manager = build_notification_manager(config.all)
        except Exception as exc:
            logger.warning("system_notification_manager_init_failed", error=str(exc), exc_info=True)
            self.notification_manager = None
        return self.notification_manager

    async def _send_system_alert(
        self,
        *,
        status: str,
        reason: str,
        service: str,
        error: str,
        error_type: str = "",
        detail: str = "",
    ) -> bool:
        manager = self._ensure_notification_manager()
        if manager is None:
            return False

        try:
            from services.notifications import NotificationEvent

            event = NotificationEvent(
                event_type="system_alert",
                status=str(status or "ERROR"),
                strategy_id="SYSTEM",
                symbol=str(service or "TRADING_SYSTEM"),
                side="N/A",
                qty=0.0,
                order_type="SYSTEM",
                decision_id="",
                reason=str(reason or "system_failure"),
                metadata={
                    "host": os.uname().nodename,
                    "service": str(service or "TRADING_SYSTEM"),
                    "error": str(error or ""),
                    "error_type": str(error_type or ""),
                    "detail": str(detail or ""),
                    "pid": os.getpid(),
                    "cwd": os.getcwd(),
                },
            )
            ok = await manager.send_now(event)
            if not ok:
                logger.warning(
                    "system_alert_send_failed_or_blocked",
                    service=service,
                    reason=reason,
                    error=error,
                    error_type=error_type,
                )
            return ok
        except Exception as exc:
            logger.warning("system_alert_send_exception", service=service, error=str(exc), exc_info=True)
            return False

    def _get_writers_cfg(self):
        """Get writers configuration from config (compatible with different config structures)"""
        # Try to get from config object
        if hasattr(config, 'writers'):
            val = getattr(config, 'writers')
            if val:
                return val

        # Try to get from raw config dict
        try:
            raw = getattr(config, 'raw', None) or getattr(config, 'data', None) or getattr(config, '_config', None)
            if raw and isinstance(raw, dict) and "writers" in raw:
                return raw["writers"]
        except Exception:
            pass

        # Try to get using config.get() method
        try:
            val = config.get('writers')
            if val:
                return val
        except Exception:
            pass

        return []

    def _get_lake_writers_cfg(self):
        """Get lake_writers configuration from config"""
        # Try to get from config object
        if hasattr(config, 'lake_writers'):
            val = getattr(config, 'lake_writers')
            if val:
                return val

        # Try to get from raw config dict
        try:
            raw = getattr(config, 'raw', None) or getattr(config, 'data', None) or getattr(config, '_config', None)
            if raw and isinstance(raw, dict) and "lake_writers" in raw:
                return raw["lake_writers"]
        except Exception:
            pass

        # Try to get using config.get() method
        try:
            val = config.get('lake_writers')
            if val:
                return val
        except Exception:
            pass

        return []

    @staticmethod
    def _normalize_feature_resolution(value: Any) -> str:
        raw = str(value or "").strip().lower()
        mapping = {
            "5": "5s",
            "5s": "5s",
            "15": "15s",
            "15s": "15s",
            "60": "1m",
            "60s": "1m",
            "1m": "1m",
            "300": "5m",
            "300s": "5m",
            "5m": "5m",
            "600": "10m",
            "600s": "10m",
            "10m": "10m",
            "900": "15m",
            "900s": "15m",
            "15m": "15m",
            "1800": "30m",
            "1800s": "30m",
            "30m": "30m",
            "3600": "1h",
            "3600s": "1h",
            "1h": "1h",
            "7200": "2h",
            "2h": "2h",
            "10800": "3h",
            "3h": "3h",
            "14400": "4h",
            "4h": "4h",
            "1d": "1d",
            "day": "1d",
            "daily": "1d",
        }
        return mapping.get(raw, raw or "1m")

    async def _start_vix_ingestor_if_enabled(self, *, ib_cfg: Dict[str, Any]) -> None:
        """Start VIX ingestion after the shared IB market session is ready."""
        if getattr(self, "_vix_ingestor_started", False):
            return
        vix_cfg = config.get('vix', {}) if hasattr(config, 'get') else getattr(config, 'vix', {})
        if not (vix_cfg and vix_cfg.get('enabled', True)):
            logger.info("   ⏭️  VIX ingestor disabled")
            return

        from libs.marketdata.vix_ingestor import ingest_vix
        from services.ibkr.ib_connection_manager import get_ib_connection_manager

        market_data = getattr(self, "market_data", None)
        try:
            working_port = int(getattr(getattr(market_data, "ib_adapter", None), 'ib_port', ib_cfg.get("port", 4002)))
        except Exception:
            working_port = int(ib_cfg.get("port", 4002))
        ib_host = ib_cfg.get("host", "127.0.0.1")
        market_ib = None
        try:
            existing_market_ib = getattr(getattr(market_data, "ib_adapter", None), "ib", None)
            if existing_market_ib and existing_market_ib.isConnected():
                market_ib = existing_market_ib
        except Exception:
            market_ib = None
        if market_ib is None:
            market_ib = await get_ib_connection_manager().get_market_ib(host=ib_host, port=working_port)
        task = asyncio.create_task(
            ingest_vix(ib=market_ib, ib_host=ib_host, ib_port=working_port),
            name="vix_ingestor",
        )
        self.services.append(("vix_ingestor", task))
        self._vix_ingestor_started = True
        logger.info("   ✅ VIX ingestor started (shared IB)", ib_host=ib_host, ib_port=working_port)

    def _feature_runtime_requirements(self) -> Dict[str, Any]:
        """
        Determine whether enabled runtime components explicitly require online
        feature calculators during RTH.

        Safety posture:
        - Default to "no online feature calculators" unless a strategy/service
          explicitly declares the need.
        - Continue to allow off-hours calculators for backfill/research-style use.
        """
        strategies_cfg = config.get("strategies", []) or []
        option_ml_auto = os.getenv("OPTION_ML_AUTO_START", "").lower() in ("1", "true", "yes", "on")

        required_rth_resolutions: Set[str] = set()
        required_offhours_resolutions: Set[str] = set()
        strategy_audit: List[Dict[str, Any]] = []
        has_rfe_strategy = False

        for spec in strategies_cfg:
            if not spec.get("enabled", True):
                continue

            params = spec.get("params") or {}
            extras = params.get("extras") or {}
            strategy_id = str(spec.get("id") or "")
            audit_entry: Dict[str, Any] = {
                "strategy_id": strategy_id,
                "module": str(spec.get("module") or ""),
                "class": str(spec.get("class") or ""),
                "online_feature_resolutions": [],
                "reasons": [],
            }

            rfe_cfg = extras.get("realtime_feature_engine") or {}
            if bool(rfe_cfg.get("enabled", False)):
                has_rfe_strategy = True
                emit_on = self._normalize_feature_resolution(rfe_cfg.get("emit_on", "1m"))
                required_rth_resolutions.add(emit_on)
                required_offhours_resolutions.add(emit_on)
                audit_entry["online_feature_resolutions"].append(emit_on)
                audit_entry["reasons"].append(f"rfe:{emit_on}")

            explicit_resolutions = (
                extras.get("required_online_feature_resolutions")
                or extras.get("online_feature_requirements")
                or []
            )
            if isinstance(explicit_resolutions, (str, int, float)):
                explicit_resolutions = [explicit_resolutions]
            for res in explicit_resolutions:
                normalized = self._normalize_feature_resolution(res)
                required_rth_resolutions.add(normalized)
                required_offhours_resolutions.add(normalized)
                if normalized not in audit_entry["online_feature_resolutions"]:
                    audit_entry["online_feature_resolutions"].append(normalized)
                audit_entry["reasons"].append(f"explicit:{normalized}")

            if bool(extras.get("requires_online_feature_calculator", False)):
                normalized = self._normalize_feature_resolution(extras.get("online_feature_resolution", "1m"))
                required_rth_resolutions.add(normalized)
                required_offhours_resolutions.add(normalized)
                if normalized not in audit_entry["online_feature_resolutions"]:
                    audit_entry["online_feature_resolutions"].append(normalized)
                audit_entry["reasons"].append(f"flag:{normalized}")

            strategy_audit.append(audit_entry)

        service_reasons: List[str] = []
        if option_ml_auto:
            required_rth_resolutions.add("1m")
            required_offhours_resolutions.add("1m")
            service_reasons.append("option_ml:auto_start->1m")

        return {
            "has_rfe_strategy": has_rfe_strategy,
            "required_rth_resolutions": sorted(required_rth_resolutions),
            "required_offhours_resolutions": sorted(required_offhours_resolutions),
            "strategy_audit": strategy_audit,
            "service_reasons": service_reasons,
            "enabled_strategy_ids": [entry["strategy_id"] for entry in strategy_audit],
        }

    def _is_option_quotes_ready(self, underlying: str = "QQQ") -> bool:
        """
        Readiness gate for 0DTE option strategy startup.
        If quotes stream is stale/missing, skip loading option strategy while keeping stock strategies alive.
        """
        try:
            from libs.db.ch_pool import get_client as _get_ch_client

            ch = _get_ch_client()
            max_age_min = float(config.get("services.options_quotes_ingest.health_max_age_min", 3.0) or 3.0)
            min_dte0_rows = int(config.get("services.options_quotes_ingest.health_min_dte0_rows", 1) or 1)
            rows = ch.query(
                """
                SELECT
                  maxIf(ts, dte = 0) AS max_ts_dte0,
                  dateDiff('minute', maxIf(ts, dte = 0), now()) AS age_min_dte0,
                  countIf(dte=0) AS n_dte0
                FROM trading.options_quotes_1m
                WHERE underlying = {u:String}
                """,
                parameters={"u": str(underlying).strip().upper()},
                settings={"use_query_cache": False},
            )
            if not rows:
                logger.warning("options_quotes_readiness_no_rows", underlying=underlying)
                return False
            age_min = float(rows[0].get("age_min_dte0") or 9_999.0)
            n_dte0 = int(rows[0].get("n_dte0") or 0)
            if age_min > max_age_min:
                logger.warning(
                    "options_quotes_readiness_stale",
                    underlying=underlying,
                    age_min=age_min,
                    max_age_min=max_age_min,
                )
                return False
            if n_dte0 < min_dte0_rows:
                logger.warning(
                    "options_quotes_readiness_missing_dte0",
                    underlying=underlying,
                    n_dte0=n_dte0,
                    min_dte0_rows=min_dte0_rows,
                )
                return False
            return True
        except Exception as exc:
            logger.warning("options_quotes_readiness_check_failed", underlying=underlying, error=str(exc))
            return False

    def _ensure_option_quotes_table(self) -> None:
        """Best-effort DDL guard so ingest/readiness checks do not fail on missing table."""
        try:
            from tools.ingest_options_quotes import ensure_options_quotes_table

            ensure_options_quotes_table()
        except Exception as exc:
            logger.warning("options_quotes_table_ensure_failed", error=str(exc))

    async def _load_enabled_strategies_after_exec_ready(
        self,
        *,
        registry_cfg: Dict[str, Any],
        mandatory_live_strategy_ids: Set[str],
        exec_enabled: bool,
    ) -> tuple[List[str], List[Dict[str, str]]]:
        """Load enabled strategies only after the real execution safety lane is ready."""
        registry = get_registry()
        strategies_cfg = config.get('strategies', []) or []
        loaded_strategies: List[str] = []
        strategy_load_failures: List[Dict[str, str]] = []

        for strategy_spec in strategies_cfg:
            if not strategy_spec.get('enabled', True):
                continue

            strategy_id = str(strategy_spec.get('id') or "").strip()
            module_name = strategy_spec.get('module')
            class_name = strategy_spec.get('class')
            if not strategy_id or not module_name or not class_name:
                logger.warning("Skipping malformed strategy spec", spec=strategy_spec)
                strategy_load_failures.append(
                    {"strategy_id": strategy_id or "<missing>", "stage": "spec", "error": "malformed_strategy_spec"}
                )
                continue

            if module_name == "strategies.qqq_0dte_opt_live_strategy":
                opt_underlying = (
                    ((strategy_spec.get("params") or {}).get("extras") or {}).get("underlying")
                    or "QQQ"
                )
                strict_gate = bool(config.get("services.options_quotes_ingest.strict_startup_gate", False))
                opt_ready = self._is_option_quotes_ready(str(opt_underlying))
                if strict_gate and (not opt_ready):
                    logger.warning(
                        "Skipping option strategy due to quotes readiness",
                        strategy_id=strategy_id,
                        underlying=str(opt_underlying).upper(),
                    )
                    if strategy_id in mandatory_live_strategy_ids:
                        strategy_load_failures.append(
                            {
                                "strategy_id": strategy_id,
                                "stage": "option_quotes_ready",
                                "error": "mandatory_live_strategy_option_quotes_not_ready",
                            }
                        )
                    continue
                if (not strict_gate) and (not opt_ready):
                    logger.warning(
                        "Option quotes not ready at startup; loading strategy anyway (entries wait for quotes)",
                        strategy_id=strategy_id,
                        underlying=str(opt_underlying).upper(),
                    )

            module_path = f"{module_name}.{class_name}"
            strategy_name = None
            for name, path in registry_cfg.items():
                if path == module_path:
                    strategy_name = name
                    break

            if strategy_name is None:
                try:
                    cls = getattr(import_module(module_name), class_name)
                    strategy_name = strategy_spec.get('registry_name', strategy_id)
                    registry.register(strategy_name, cls)
                    logger.debug(
                        "Registered strategy via fallback",
                        strategy_id=strategy_id,
                        module=module_path,
                        registry_name=strategy_name,
                    )
                except ModuleNotFoundError as exc:
                    logger.warning("Strategy module not found; skipping", strategy_id=strategy_id, module=module_path, error=str(exc))
                    if strategy_id in mandatory_live_strategy_ids:
                        strategy_load_failures.append({"strategy_id": strategy_id, "stage": "import", "error": str(exc)})
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to prepare strategy", strategy_id=strategy_id, module=module_path, error=str(exc))
                    if strategy_id in mandatory_live_strategy_ids:
                        strategy_load_failures.append({"strategy_id": strategy_id, "stage": "prepare", "error": str(exc)})
                    continue

            params = copy.deepcopy(strategy_spec.get('params') or {})
            params.setdefault('extras', {})
            capital_limit = strategy_spec.get('capital_limit_usd')
            if capital_limit is not None:
                try:
                    cap_val = float(capital_limit)
                    params.setdefault('max_notional_usd', cap_val)
                    params.setdefault('per_symbol_max_usd', cap_val)
                    params.setdefault('capital_limit_usd', cap_val)
                    params['extras'].setdefault('capital_limit_usd', cap_val)
                except (TypeError, ValueError):
                    logger.warning("Invalid capital limit for strategy", strategy_id=strategy_id, value=capital_limit)

            try:
                await self.strategy_engine.load_strategy(strategy_id=strategy_id, name=strategy_name, params=params)
                if getattr(self, "pg", None) is not None:
                    extras_cfg = dict((params.get("extras") or {}))
                    single_source_budget = bool(
                        strategy_spec.get("capital_budget_single_source")
                        or extras_cfg.get("capital_budget_single_source")
                    )
                    default_reserve_buffer_bps = 0 if single_source_budget else 10
                    try:
                        reserve_buffer_bps = int(
                            strategy_spec.get("reserve_buffer_bps")
                            or extras_cfg.get("reserve_buffer_bps")
                            or default_reserve_buffer_bps
                        )
                    except (TypeError, ValueError):
                        reserve_buffer_bps = default_reserve_buffer_bps
                    strategy_capital_limit = float(
                        params.get("capital_limit_usd") or params.get("max_notional_usd") or 0.0
                    )
                    await self.pg.execute(
                        """
                        INSERT INTO core.core_strategy (
                            strategy_id, name, capital_limit_usd, reserve_buffer_bps, is_enabled, created_at
                        ) VALUES ($1, $2, $3, $4, $5, now())
                        ON CONFLICT (strategy_id) DO UPDATE
                        SET name = EXCLUDED.name,
                            capital_limit_usd = EXCLUDED.capital_limit_usd,
                            reserve_buffer_bps = EXCLUDED.reserve_buffer_bps,
                            is_enabled = EXCLUDED.is_enabled
                        """,
                        str(strategy_id),
                        str(strategy_id),
                        strategy_capital_limit,
                        reserve_buffer_bps,
                        bool(strategy_spec.get("enabled", True)),
                    )
                    await self.pg.execute(
                        """
                        INSERT INTO core.core_strategy_ledger (
                            strategy_id, reserved_usd, realized_pnl_usd, updated_at, version
                        ) VALUES ($1, 0, 0, now(), 0)
                        ON CONFLICT (strategy_id) DO NOTHING
                        """,
                        str(strategy_id),
                    )
                loaded_strategies.append(strategy_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to load strategy", strategy_id=strategy_id, error=str(exc), exc_info=True)
                if strategy_id in mandatory_live_strategy_ids:
                    strategy_load_failures.append(
                        {"strategy_id": strategy_id, "stage": "load_strategy", "error": str(exc)}
                    )

        if loaded_strategies:
            logger.info("   ✅ Loaded strategies", strategies=loaded_strategies)
        else:
            logger.warning("   ⚠️  No strategies were loaded")

        loaded_live_strategy_ids = sorted(set(loaded_strategies) & mandatory_live_strategy_ids)
        missing_live_strategy_ids = sorted(mandatory_live_strategy_ids - set(loaded_strategies))
        if exec_enabled and strategy_load_failures:
            raise RuntimeError("mandatory_live_strategy_load_failed:" + json.dumps(strategy_load_failures, sort_keys=True))
        if exec_enabled and mandatory_live_strategy_ids and not loaded_live_strategy_ids:
            raise RuntimeError(
                "no_live_strategies_loaded:"
                + json.dumps(
                    {
                        "mandatory_live_strategy_ids": sorted(mandatory_live_strategy_ids),
                        "missing_live_strategy_ids": missing_live_strategy_ids,
                    },
                    sort_keys=True,
                )
            )
        if exec_enabled and missing_live_strategy_ids:
            raise RuntimeError("mandatory_live_strategy_missing_after_load:" + json.dumps(missing_live_strategy_ids, sort_keys=True))
        return loaded_strategies, strategy_load_failures

    async def start(self):
        """Start all REAL services"""
        # Must be set before spawning background loops that guard on self.running.
        # Otherwise those loops can exit immediately during startup.
        self.running = True

        logger.info("=" * 80)
        logger.info("🚀 STARTING REAL TRADING SYSTEM")
        logger.info("=" * 80)
        _write_boot_production_state_artifact(
            stage="booting",
            reason="run_system_starting",
            details={"trading_mode": _effective_trading_mode()},
        )
        _assert_no_shadow_enabled_strategies(_enabled_strategy_configs())

        # ==================================================================
        # 0. OBSERVABILITY INFRASTRUCTURE
        # ==================================================================
        logger.info("0️⃣  Starting Observability Infrastructure...")

        # Start Prometheus metrics server
        try:
            metrics_port = int(os.getenv('PROMETHEUS_PORT', '9108'))
            start_metrics_http_server(port=metrics_port)
            mark_service_up("TradingSystem", True)
            logger.info(f"   ✅ Prometheus metrics server started on port {metrics_port}")
        except Exception as exc:
            logger.error("   ❌ Failed to start Prometheus metrics server", error=str(exc))

        # Initialize observability writers
        try:
            ch_url = os.getenv('CH_URL', 'http://localhost:8123')
            ch_db = os.getenv('CH_DB', 'trading')
            ch_user = os.getenv('CH_USER', 'default')
            ch_pass = os.getenv('CH_PASS', 'clickhouse')
            loki_url = os.getenv('LOKI_URL', 'http://localhost:3100')

            self.obs_ch_writer = ClickHouseWriter(
                base_url=ch_url,
                database=ch_db,
                user=ch_user,
                password=ch_pass
            )
            await self.obs_ch_writer.start()
            logger.info(f"   ✅ ClickHouse writer started ({ch_url})")

            self.obs_loki_writer = LokiWriter(
                loki_url=loki_url,
                default_job="trader",
                default_service="TradingSystem"
            )
            await self.obs_loki_writer.start()
            logger.info(f"   ✅ Loki writer started ({loki_url})")

            # Initialize global observability queue
            global obs_queue
            obs_queue = ObservabilityQueue()
            obs_queue.start_flushers(
                loki_writer=self.obs_loki_writer,
                ch_writer=self.obs_ch_writer
            )
            logger.info("   ✅ Observability queue started")

            audit_cfg = config.get("observability.local_audit", {}) or {}
            global audit_writer
            audit_writer = LocalAuditWriter(
                root_dir=audit_cfg.get("root_dir", "runtime/audit"),
                queue_size=int(audit_cfg.get("queue_size", 20000)),
                flush_interval_sec=float(audit_cfg.get("flush_interval_sec", 0.5)),
                batch_size=int(audit_cfg.get("batch_size", 500)),
                enabled=bool(audit_cfg.get("enabled", True)),
            )
            self.audit_writer = audit_writer
            await audit_writer.start()
            audit_writer.emit(
                domain="system",
                component="run_system",
                stage="service_started",
                status="started",
                payload={"service": "local_audit_writer"},
            )
            logger.info("   ✅ Local audit writer started")

        except Exception as exc:
            logger.error("   ❌ Failed to start observability infrastructure", error=str(exc), exc_info=True)

        logger.info("")

        # ==================================================================
        # 1c. SFS-1s Service (Redis-only microbars for ultra-low latency)
        # ==================================================================
        try:
            await self._start_sfs_1s_service()
        except Exception as exc:
            logger.error("1️⃣c  ❌ Failed to start SFS-1s Service", error=str(exc))

        # ==================================================================
        # 1d. SFS-5s Writer (Redis 1s → ClickHouse sfs.sfs_5s)
        # ==================================================================
        try:
            s5_cfg = config.get('services.sfs_5s', {}) or {}
            if s5_cfg.get('enabled', False):
                # Ensure SFS schema exists before starting writer
                try:
                    from libs.db.ddl import ensure_sfs_schema
                    ensure_sfs_schema()
                    logger.info("   ✅ SFS schema ensured (sfs.sfs_5s)")
                except Exception as exc:
                    logger.warning("   ⚠️  Failed to ensure SFS schema", error=str(exc))

                from services.market.sfs_5s_writer import Sfs5sWriter

                flush_ms = int(s5_cfg.get('flush_period_ms', 1000))
                lookback_n = int(s5_cfg.get('lookback_n', 5))
                symbols = s5_cfg.get('symbols') or None
                group = str(s5_cfg.get('group', 'sfs_5s'))
                s5_rth_only = bool(s5_cfg.get('rth_only', False))
                trading_hours_cfg = config.get('trading_hours', {}) or {}

                self.sfs_5s_writer = Sfs5sWriter(
                    flush_period_ms=flush_ms,
                    lookback_n=lookback_n,
                    group=group,
                    symbols=symbols,
                    rth_only=s5_rth_only,
                    trading_hours=trading_hours_cfg,
                )
                s5_task = asyncio.create_task(self.sfs_5s_writer.run())
                self.services.append(("sfs_5s_writer", s5_task))
                logger.info(
                    "1️⃣d  ✅ SFS-5s Writer started",
                    flush_ms=flush_ms,
                    lookback_n=lookback_n,
                    group=group,
                    symbols=(symbols or config.universe),
                )
            else:
                logger.info("1️⃣d  SFS-5s Writer disabled (services.sfs_5s.enabled=false)")
        except Exception as exc:
            logger.error("1️⃣d  ❌ Failed to start SFS-5s Writer", error=str(exc))

        # Enable event loop debugging only when explicitly requested.
        # By default this stays off to keep Ctrl+C shutdown quiet.
        loop = asyncio.get_running_loop()
        async_debug = os.getenv("ASYNC_DEBUG", "").lower()
        if async_debug in ("1", "true", "yes", "on"):
            loop.set_debug(True)
            logger.info("🔍 Async debug mode enabled")

        # Start event loop watchdog with stack dumping
        try:
            from libs.loop_watchdog import loop_watchdog
            asyncio.create_task(loop_watchdog(threshold_ms=200, dump_after=2, interval=0.5))
            logger.info("🔍 Event loop watchdog started (dumps stacks when lag > 200ms x2)")
        except Exception as exc:
            logger.warning("Failed to start loop watchdog", error=str(exc))

        try:
            # Get configuration
            trading_mode = _effective_trading_mode()
            # Use universe from config, or fall back to environment variable
            symbols = config.universe if hasattr(config, 'universe') and config.universe else os.getenv('TRADING_SYMBOLS', 'AAPL,MSFT,NVDA').split(',')
            kafka_bootstrap = ",".join(config.kafka_config.get('bootstrap_servers', ['127.0.0.1:9092']))
            redis_url = os.getenv('STATEBUS_REDIS', 'redis://127.0.0.1:6379/0')

            # IB config from libs.config (handles port selection based on ENV_MODE)
            ib_cfg = config.ib_config

            logger.info(f"Configuration:")
            logger.info(f"  Trading Mode: {trading_mode.upper()}")
            logger.info(f"  Symbols: {symbols}")
            logger.info(f"  IB Gateway: {ib_cfg['host']}:{ib_cfg['port']} ({ib_cfg['mode']} mode)")
            logger.info(f"  Kafka: {kafka_bootstrap}")
            logger.info(f"  Redis: {redis_url}")
            logger.info("")

            # Optional lightweight Telegram inbound command listener (in-process, long polling).
            try:
                from services.telegram import build_telegram_command_bot
                from services.ibkr.client_id_registry import get_client_id_registry

                self.telegram_command_bot = build_telegram_command_bot(
                    repo_root=Path(__file__).resolve().parent,
                    trading_mode=trading_mode,
                    ib_host=ib_cfg["host"],
                    ib_port=int(ib_cfg["port"]),
                    ib_client_id=int(get_client_id_registry().resolve("exec")),
                )
                if self.telegram_command_bot is not None:
                    await self.telegram_command_bot.start()
                    logger.info("   ✅ Telegram command bot started (inbound commands enabled)")
                else:
                    logger.info("   ℹ️  Telegram command bot disabled")
            except Exception as exc:
                logger.warning("   ⚠️  Telegram command bot init failed", error=str(exc))

            # Log available strategies (if registry is available)
            try:
                registry = get_registry()
                # Registry might not have .keys() method, try to get strategies differently
                if hasattr(registry, 'keys'):
                    strategy_registry = sorted(registry.keys())
                elif hasattr(registry, 'list_strategies'):
                    strategy_registry = sorted(registry.list_strategies())
                elif hasattr(registry, '_registry'):
                    strategy_registry = sorted(registry._registry.keys())
                elif hasattr(registry, '_strategies'):
                    strategy_registry = sorted(registry._strategies.keys())
                else:
                    strategy_registry = []
                log_event(
                    "strategy_registry",
                    component="System",
                    strategies=strategy_registry,
                )
                logger.info("Available strategies", strategies=strategy_registry)
            except Exception as exc:
                logger.warning("Could not load strategy registry", error=str(exc))
                strategy_registry = []

            if trading_mode == 'paper':
                logger.info("⚠️  PAPER TRADING MODE - Orders sent to IB paper account")
            elif trading_mode == 'live':
                logger.warning("🔴 LIVE TRADING MODE - REAL MONEY AT RISK!")
            else:
                logger.info("📋 DRY_RUN MODE - Decisions logged only, no orders sent")
            logger.info("")

            # ==================================================================
            # 1. MARKET DATA SERVICE (IB Connection)
            # ==================================================================
            logger.info("1️⃣  Starting Market Data Service (IB Connection)...")
            mds_config = config.get('services.market_data_service', {})
            if mds_config and mds_config.get('enabled', False):
                from services.market.market_data_service import MarketDataService
                from services.production.strategy_data_contract import (
                    build_strategy_data_contracts,
                    required_symbols_from_contracts,
                )

                rth_only = bool(mds_config.get('rth_only', False))
                allowed_sessions = mds_config.get('allowed_sessions', ['PREMARKET', 'RTH', 'AFTERHOURS', 'OVERNIGHT'])
                trading_hours_cfg = config.get('trading_hours', {}) or {}
                strategy_data_contracts = build_strategy_data_contracts(getattr(config, "_config", {}) or {})
                required_subscription_symbols = required_symbols_from_contracts(strategy_data_contracts) or list(symbols)
                market_data_symbols = list(dict.fromkeys([*(symbols or []), *required_subscription_symbols]))
                self.strategy_data_contracts = strategy_data_contracts
                try:
                    from services.data_availability import configure_data_availability_service

                    data_availability = configure_data_availability_service(
                        durable_state_path=Path("runtime/state/data_availability_gates.json")
                    )
                    data_availability.set_strategy_data_contracts(strategy_data_contracts)
                    data_availability.set_audit_writer(self.audit_writer)
                except Exception as exc:
                    logger.warning("strategy_data_contracts_data_availability_wiring_failed", error=str(exc))
                logger.info(
                    "strategy_data_contracts_loaded",
                    contract_count=len(strategy_data_contracts),
                    required_subscription_symbols=required_subscription_symbols,
                    market_data_symbols=market_data_symbols,
                )
                # Allow disabling the heavy tick publisher (Kafka) via config/env to reduce IB load.
                tick_pub_enabled = bool(mds_config.get('enable_tick_publisher', False))
                if os.getenv("DISABLE_TICK_PUBLISHER", "").lower() in ("1", "true", "yes", "on"):
                    tick_pub_enabled = False

                # Do not eagerly open an IB API session here. A synchronous startup
                # handshake plus MarketDataService recovery can double-hit Gateway
                # during restart windows. MarketDataService uses the shared manager
                # lazily and applies the global reconnect gate/backoff.
                market_ib = None
                logger.info(
                    "run_system_market_ib_lazy_start",
                    host=ib_cfg['host'],
                    port=ib_cfg['port'],
                )

                self.market_data = MarketDataService(
                    symbols=market_data_symbols,
                    enable_backfill=bool(mds_config.get('enable_backfill', True)),
                    enable_tick_publisher=tick_pub_enabled,
                    kafka_bootstrap=kafka_bootstrap,
                    redis_url=redis_url,
                    ib_host=ib_cfg['host'],
                    ib_port=ib_cfg['port'],
                    ib=market_ib,
                    use_shared_ib_manager=True,
                    rth_only=rth_only,
                    trading_hours=trading_hours_cfg,
                    allowed_sessions=allowed_sessions,
                    audit_writer=self.audit_writer,
                    required_subscription_symbols=required_subscription_symbols,
                )

                self._market_data_start_deferred_until_broker_ready = True
                logger.info(
                    "   ⏳ Market Data Service constructed; start deferred until broker readiness gate passes"
                )
                if not tick_pub_enabled:
                    logger.info("   ℹ️  Tick publisher disabled (enable_tick_publisher=false or DISABLE_TICK_PUBLISHER set)")
            else:
                logger.warning("   SKIPPING Market Data Service as it is not enabled in the configuration.")

            # ==================================================================
            # 1b. PRICE CACHE (Low-latency L1 data for strategies)
            # ==================================================================
            logger.info("1️⃣b  Starting PriceCache (Low-latency L1 data)...")
            try:
                self._price_cache_consumers = []
                t_pc0 = asyncio.get_running_loop().time()

                # Initialize PriceCache
                self.price_cache = PriceCache(
                    rvol_lookback_days=60,
                    max_1m_bars=390,
                    max_5m_bars=78
                )
                logger.info(
                    "   ✅ PriceCache initialized",
                    init_ms=round((asyncio.get_running_loop().time() - t_pc0) * 1000.0, 1),
                )

                # Import Kafka deps after cache init so this step is not blocked by aiokafka import latency.
                from aiokafka import AIOKafkaConsumer
                from aiokafka.errors import KafkaError, KafkaConnectionError, CommitFailedError
                import json

                # Create Kafka consumers for tick streams
                # Increased timeouts to handle slow coordinator responses (esp. during rebalance)
                trade_consumer = AIOKafkaConsumer(
                    'md.tick.trade.v1',
                    bootstrap_servers=kafka_bootstrap,
                    group_id='price_cache_trades',
                    value_deserializer=lambda m: json.loads(m.decode('utf-8')),
                    enable_auto_commit=True,
                    auto_offset_reset='latest',
                    auto_commit_interval_ms=5000,  # Commit every 5 seconds
                    session_timeout_ms=60000,  # 60s session timeout (increased from 30s)
                    heartbeat_interval_ms=6000,  # Send heartbeat every 6s (increased from 3s)
                    request_timeout_ms=60000,  # 60s request timeout (increased from 30s)
                    max_poll_interval_ms=300000,  # 5 min max poll interval
                    connections_max_idle_ms=540000,  # 9 min idle timeout
                )

                quote_consumer = AIOKafkaConsumer(
                    'md.tick.quote.v1',
                    bootstrap_servers=kafka_bootstrap,
                    group_id='price_cache_quotes',
                    value_deserializer=lambda m: json.loads(m.decode('utf-8')),
                    enable_auto_commit=True,
                    auto_offset_reset='latest',
                    auto_commit_interval_ms=5000,  # Commit every 5 seconds
                    session_timeout_ms=60000,  # 60s session timeout (increased from 30s)
                    heartbeat_interval_ms=6000,  # Send heartbeat every 6s (increased from 3s)
                    request_timeout_ms=60000,  # 60s request timeout (increased from 30s)
                    max_poll_interval_ms=300000,  # 5 min max poll interval
                    connections_max_idle_ms=540000,  # 9 min idle timeout
                )

                # Start consumers concurrently to avoid coordinator blocking
                # Serial start can cause second consumer to timeout if first is slow
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            trade_consumer.start(),
                            quote_consumer.start(),
                        ),
                        timeout=20.0,
                    )
                except Exception as exc:
                    for consumer in (trade_consumer, quote_consumer):
                        try:
                            await asyncio.wait_for(consumer.stop(), timeout=5.0)
                        except Exception as close_err:
                            logger.warning("Error closing Kafka consumer during PriceCache startup timeout",
                                           error=str(close_err))
                    raise TimeoutError(f"PriceCache Kafka consumer startup failed or timed out: {exc}") from exc
                self._price_cache_consumers.append(trade_consumer)
                self._price_cache_consumers.append(quote_consumer)
                logger.info("   ✅ Kafka consumers started concurrently (md.tick.trade.v1, md.tick.quote.v1)")

                # Create consumer tasks with retry logic
                async def consume_trades():
                    """Consume trade ticks and update PriceCache (with Kafka error recovery)"""
                    max_retries = 5
                    retry_base_delay = 1.0
                    attempt = 0

                    while self.running:
                        try:
                            attempt += 1
                            if attempt > 1:
                                logger.info("Restarting trade consumer", attempt=attempt)

                            async for msg in trade_consumer:
                                data = msg.value
                                # Update last price from trade
                                symbol = data.get('symbol')
                                if symbol in self.price_cache._snapshots:
                                    snapshot = self.price_cache._snapshots[symbol]
                                    # Create updated tick with last price from trade
                                    tick = Tick(
                                        symbol=symbol,
                                        last=float(data.get('price', snapshot.last)),
                                        bid=snapshot.bid,
                                        ask=snapshot.ask,
                                        last_size=int(data.get('size', 0)),
                                        bid_size=snapshot.bid_size,
                                        ask_size=snapshot.ask_size,
                                        ts_us=int(data.get('ts_us', 0))
                                    )
                                else:
                                    # First tick for this symbol
                                    tick = Tick(
                                        symbol=symbol,
                                        last=float(data.get('price', 0)),
                                        bid=0.0,
                                        ask=0.0,
                                        last_size=int(data.get('size', 0)),
                                        ts_us=int(data.get('ts_us', 0))
                                    )
                                await self.price_cache.handle_tick(tick)

                            # Normal exit
                            break

                        except asyncio.CancelledError:
                            logger.info("Trade consumer cancelled")
                            break

                        except (KafkaError, KafkaConnectionError, CommitFailedError) as exc:
                            error_msg = str(exc)
                            is_retriable = (
                                "UnknownError" in error_msg or
                                "coordinator" in error_msg.lower() or
                                "connection" in error_msg.lower() or
                                "commit failed" in error_msg.lower() or
                                "timed out" in error_msg.lower() or
                                "not available" in error_msg.lower()
                            )

                            if is_retriable and attempt <= max_retries:
                                delay = retry_base_delay * (2 ** (attempt - 1))
                                logger.warning(
                                    "Kafka trade consumer error (retriable), restarting",
                                    attempt=attempt,
                                    error=error_msg,
                                    retry_in_sec=delay
                                )

                                try:
                                    await asyncio.wait_for(trade_consumer.stop(), timeout=5.0)
                                except Exception as stop_exc:
                                    logger.warning("Error stopping trade consumer", error=str(stop_exc))

                                await asyncio.sleep(delay)

                                try:
                                    await trade_consumer.start()
                                    logger.info("Trade consumer restarted successfully")
                                    attempt = 0  # Reset on successful restart
                                except Exception as start_exc:
                                    logger.error("Failed to restart trade consumer", error=str(start_exc), exc_info=True)
                            else:
                                logger.error(
                                    "Trade consumer error (non-retriable or exhausted retries)",
                                    attempt=attempt,
                                    error=error_msg,
                                    exc_info=True
                                )
                                raise

                        except Exception as e:
                            logger.error("Trade consumer error (unknown)", error=str(e), exc_info=True)
                            raise

                    # Cleanup
                    try:
                        await trade_consumer.stop()
                    except Exception as close_err:
                        logger.warning("Error closing trade Kafka consumer", error=str(close_err))

                async def consume_quotes():
                    """Consume quote ticks and update PriceCache (with Kafka error recovery)"""
                    max_retries = 5
                    retry_base_delay = 1.0
                    attempt = 0

                    while self.running:
                        try:
                            attempt += 1
                            if attempt > 1:
                                logger.info("Restarting quote consumer", attempt=attempt)

                            async for msg in quote_consumer:
                                data = msg.value
                                symbol = data.get('symbol')
                                # Get current snapshot or create new one
                                if symbol in self.price_cache._snapshots:
                                    snapshot = self.price_cache._snapshots[symbol]
                                    tick = Tick(
                                        symbol=symbol,
                                        last=snapshot.last,
                                        bid=float(data.get('bid', snapshot.bid)),
                                        ask=float(data.get('ask', snapshot.ask)),
                                        last_size=snapshot.last_size,
                                        bid_size=int(data.get('bid_size', snapshot.bid_size)),
                                        ask_size=int(data.get('ask_size', snapshot.ask_size)),
                                        ts_us=int(data.get('ts_us', 0))
                                    )
                                else:
                                    # First quote for this symbol
                                    tick = Tick(
                                        symbol=symbol,
                                        last=0.0,
                                        bid=float(data.get('bid', 0)),
                                        ask=float(data.get('ask', 0)),
                                        bid_size=int(data.get('bid_size', 0)),
                                        ask_size=int(data.get('ask_size', 0)),
                                        ts_us=int(data.get('ts_us', 0))
                                    )
                                await self.price_cache.handle_tick(tick)

                            # Normal exit
                            break

                        except asyncio.CancelledError:
                            logger.info("Quote consumer cancelled")
                            break

                        except (KafkaError, KafkaConnectionError, CommitFailedError) as exc:
                            error_msg = str(exc)
                            is_retriable = (
                                "UnknownError" in error_msg or
                                "coordinator" in error_msg.lower() or
                                "connection" in error_msg.lower() or
                                "commit failed" in error_msg.lower() or
                                "timed out" in error_msg.lower() or
                                "not available" in error_msg.lower()
                            )

                            if is_retriable and attempt <= max_retries:
                                delay = retry_base_delay * (2 ** (attempt - 1))
                                logger.warning(
                                    "Kafka quote consumer error (retriable), restarting",
                                    attempt=attempt,
                                    error=error_msg,
                                    retry_in_sec=delay
                                )

                                try:
                                    await asyncio.wait_for(quote_consumer.stop(), timeout=5.0)
                                except Exception as stop_exc:
                                    logger.warning("Error stopping quote consumer", error=str(stop_exc))

                                await asyncio.sleep(delay)

                                try:
                                    await quote_consumer.start()
                                    logger.info("Quote consumer restarted successfully")
                                    attempt = 0  # Reset on successful restart
                                except Exception as start_exc:
                                    logger.error("Failed to restart quote consumer", error=str(start_exc), exc_info=True)
                            else:
                                logger.error(
                                    "Quote consumer error (non-retriable or exhausted retries)",
                                    attempt=attempt,
                                    error=error_msg,
                                    exc_info=True
                                )
                                raise

                        except Exception as e:
                            logger.error("Quote consumer error (unknown)", error=str(e), exc_info=True)
                            raise

                    # Cleanup
                    try:
                        await quote_consumer.stop()
                    except Exception as close_err:
                        logger.warning("Error closing quote Kafka consumer", error=str(close_err))

                # Start consumer tasks
                trade_task = asyncio.create_task(consume_trades())
                quote_task = asyncio.create_task(consume_quotes())
                self._price_cache_tasks = [trade_task, quote_task]
                self.services.append(("price_cache_trades", trade_task))
                self.services.append(("price_cache_quotes", quote_task))

                logger.info("   ✅ PriceCache tick consumers started")
            except Exception as exc:
                for consumer in self._price_cache_consumers:
                    try:
                        await consumer.stop()
                    except Exception as close_err:
                        logger.warning("Error closing Kafka consumer during PriceCache startup failure",
                                       error=str(close_err))
                self._price_cache_consumers = []
                logger.warning(
                    "   ⚠️  PriceCache unavailable; continuing without low-latency cache",
                    error=str(exc),
                    exc_type=type(exc).__name__,
                )
                self.price_cache = None

            # ==================================================================
            # 2. CLICKHOUSE BAR WRITER SERVICE(S) - Multi-instance support
            # ==================================================================
            logger.info("2️⃣  Starting ClickHouse Bar Writer Service(s)...")

            # Get writers configuration from config
            writers_cfg = self._get_writers_cfg()
            if writers_cfg:
                for w in writers_cfg:
                    if not w.get("enabled", False):
                        logger.info(f"   ⏭️  Skipping disabled writer: {w.get('name', 'unnamed')}")
                        continue

                    topic = w["topic"]
                    table = w["table"]
                    group = w.get("group_id", f"ch_writer_{table}")
                    bs = int(w.get("batch_size", 200))
                    bt = float(w.get("batch_timeout_sec", 2.0))
                    allow_extended_hours = bool(w.get("allow_extended_hours", False))

                    writer = ClickHouseBarWriter(
                        topic=topic,
                        table_name=table,
                        group_id=group,
                        batch_size=bs,
                        batch_timeout_sec=bt,
                        allow_extended_hours=allow_extended_hours,
                    )

                    writer_task = asyncio.create_task(writer.run())
                    self._ch_writers.append(writer)
                    self.services.append((f"ch_writer_{table}", writer_task))
                    logger.info(f"   ✅ ClickHouse Writer started: {topic} → {table}")

                if self._ch_writers:
                    logger.info(f"   ✅ Total {len(self._ch_writers)} ClickHouse writer(s) started")
                else:
                    logger.warning("   ⚠️  No writers enabled in config")
            else:
                # Fallback to legacy single writer if no config
                logger.info("   📋 No 'writers' config found, using legacy single writer")
                self.ch_writer = ClickHouseBarWriter(
                    topic="md.bar.5s.v2",
                    table_name="bars_5s",
                    group_id="ch_writer_bars_5s"
                )
                ch_writer_task = asyncio.create_task(self.ch_writer.run())
                self._ch_writers.append(self.ch_writer)
                self.services.append(("ch_writer", ch_writer_task))
                logger.info("   ✅ ClickHouse Bar Writer Service started (legacy mode)")

            # ==================================================================
            # 2b. LAKE WRITERS (Phase 4: Data Lake)
            # ==================================================================
            logger.info("2️⃣b  Starting Data Lake Writer Service(s)...")

            lake_writers_cfg = self._get_lake_writers_cfg()
            if lake_writers_cfg:
                for lw in lake_writers_cfg:
                    if not lw.get("enabled", False):
                        logger.info(f"   ⏭️  Skipping disabled lake writer: {lw.get('name', 'unnamed')}")
                        continue

                    topic = lw["topic"]
                    mode = lw["mode"]
                    group = lw.get("group_id", f"lake_writer_{mode}")
                    bs = int(lw.get("batch_size", 500))
                    bt = float(lw.get("batch_timeout_sec", 1.0))

                    lake_writer = ClickHouseLakeWriter(
                        topic=topic,
                        mode=mode,
                        group_id=group,
                        batch_size=bs,
                        batch_timeout_sec=bt,
                    )

                    lake_writer_task = asyncio.create_task(lake_writer.run())
                    self._lake_writers.append(lake_writer)
                    table = "lake.ticks_trades" if mode == "trades" else "lake.quotes_l1"
                    self.services.append((f"lake_writer_{mode}", lake_writer_task))
                    logger.info(f"   ✅ Lake Writer started: {topic} → {table} (mode={mode})")

                if self._lake_writers:
                    logger.info(f"   ✅ Total {len(self._lake_writers)} lake writer(s) started")
                else:
                    logger.info("   ℹ️  No lake writers enabled in config")
            else:
                logger.info("   ℹ️  No 'lake_writers' config found")

            # ==================================================================
            # 3. FEATURE CALCULATOR SERVICE
            # ==================================================================
            logger.info("3️⃣  Starting Feature Calculator Service...")
            feature_calc_cfg = config.get('feature_calculator', {})
            if feature_calc_cfg.get('enabled', True):
                enable_15s = bool(feature_calc_cfg.get('enable_15s', True))
                enable_5s = bool(feature_calc_cfg.get('enable_5s', True))
                enable_1h = bool(feature_calc_cfg.get('enable_1h', True))
                # Time-based gating: disable low-latency polling calculators during RTH (ET 09:30–16:00, Mon–Fri)
                try:
                    now_et = datetime.now(ZoneInfo("America/New_York"))
                    is_weekend = now_et.weekday() >= 5
                    is_rth = (not is_weekend) and (_time(9, 30) <= now_et.time() <= _time(16, 0))
                except Exception:
                    # Fail-open: assume off-hours
                    is_rth = False
                logger.info("feature_calc_time_gating", is_rth=is_rth)

                feature_runtime = self._feature_runtime_requirements()
                force_rth_resolutions_raw = feature_calc_cfg.get("force_rth_resolutions", []) or []
                if isinstance(force_rth_resolutions_raw, (str, int, float)):
                    force_rth_resolutions_raw = [force_rth_resolutions_raw]
                forced_rth_resolutions = {
                    self._normalize_feature_resolution(res) for res in force_rth_resolutions_raw
                }
                required_rth_resolutions = {
                    self._normalize_feature_resolution(res)
                    for res in (feature_runtime.get("required_rth_resolutions") or [])
                }
                logger.info(
                    "feature_calc_runtime_audit",
                    is_rth=is_rth,
                    enabled_strategy_ids=feature_runtime.get("enabled_strategy_ids", []),
                    strategy_audit=feature_runtime.get("strategy_audit", []),
                    service_reasons=feature_runtime.get("service_reasons", []),
                    required_rth_resolutions=sorted(required_rth_resolutions),
                    forced_rth_resolutions=sorted(forced_rth_resolutions),
                    has_rfe_strategy=bool(feature_runtime.get("has_rfe_strategy", False)),
                )
                from services.market.features.feature_calculator import FeatureCalculator

                # 15s/5s calculators: skip during RTH to avoid duplicate low-latency paths
                if not is_rth:
                    if enable_15s:
                        try:
                            self.feature_calc_15s = FeatureCalculator(
                                resolution="15s",
                                horizon_minutes=30,
                                waterline_delay_sec=3,
                                poll_interval_sec=2.0,
                            )
                            await self.feature_calc_15s.start()
                            logger.info("   ✅ Feature Calculator (15s) started")
                        except Exception as exc:
                            logger.error("   ❌ Failed to start Feature Calculator (15s)", error=str(exc))
                    else:
                        logger.info("   ⏭️  Feature Calculator (15s) disabled by config")

                    if enable_5s:
                        try:
                            self.feature_calc_5s = FeatureCalculator(
                                resolution="5s",
                                horizon_minutes=5,  # 减少from 15→5分钟（减少66%数据扫描）
                                waterline_delay_sec=2,
                                poll_interval_sec=5.0  # 减少查询频率from 1→5秒
                            )
                            await self.feature_calc_5s.start()
                            logger.info("   ✅ Feature Calculator (5s) started")
                        except Exception as exc:
                            logger.error("   ❌ Failed to start Feature Calculator (5s)", error=str(exc))
                    else:
                        logger.info("   ⏭️  Feature Calculator (5s) disabled by config")
                else:
                    logger.info("   ⏭️  Skipping Feature Calculator (15s, 5s) during RTH (RFE-first)")

                # During RTH, only start resolutions explicitly required by live
                # consumers or force-enabled in config.
                allow_1h_during_rth = ("1h" in required_rth_resolutions) or ("1h" in forced_rth_resolutions)
                if enable_1h and (not is_rth or allow_1h_during_rth):
                    try:
                        self.feature_calc_1h = FeatureCalculator(
                            resolution="1h",
                            horizon_minutes=240,   # recompute last 4 hours to absorb late bars
                            waterline_delay_sec=30,
                            poll_interval_sec=30.0
                        )
                        await self.feature_calc_1h.start()
                        logger.info("   ✅ Feature Calculator (1h) started")
                    except Exception as exc:
                        logger.error("   ❌ Failed to start Feature Calculator (1h)", error=str(exc))
                elif enable_1h and is_rth:
                    logger.info(
                        "   ⏭️  Skipping Feature Calculator (1h) during RTH",
                        reason="not_explicitly_required",
                    )
                else:
                    logger.info("   ⏭️  Feature Calculator (1h) disabled by config")

                # ==================================================================
                # 3c. VIX STREAM INGESTOR (refs_vix_ticks)
                # ==================================================================
                try:
                    self._vix_ingestor_deferred_until_broker_ready = True
                    logger.info("   ⏳ VIX ingestor start deferred until broker readiness gate passes")
                except Exception as exc:
                    logger.error("   ❌ Failed to start VIX ingestor", error=str(exc), exc_info=True)

                # Start additional online feature calculators for other resolutions
                more_feature_res = [
                    {"res": "1m",  "horizon": 70,    "waterline": 5,   "poll": 10.0},
                    {"res": "5m",  "horizon": 320,   "waterline": 10,  "poll": 20.0},
                    {"res": "10m", "horizon": 1440,  "waterline": 15,  "poll": 30.0},
                    {"res": "15m", "horizon": 1440,  "waterline": 20,  "poll": 45.0},
                    {"res": "30m", "horizon": 2880,  "waterline": 30,  "poll": 60.0},
                    {"res": "2h",  "horizon": 10080, "waterline": 60,  "poll": 120.0},
                    {"res": "3h",  "horizon": 10080, "waterline": 90,  "poll": 180.0},
                    {"res": "4h",  "horizon": 20160, "waterline": 120, "poll": 240.0},
                    {"res": "1d",  "horizon": 43200, "waterline": 300, "poll": 600.0},
                ]

                # Safety default: keep RTH online calculators minimal and only
                # run resolutions explicitly required by enabled live consumers.
                allowed_rth_res = set(forced_rth_resolutions)
                rth_allowed_override_raw = feature_calc_cfg.get("rth_allowed_resolutions", []) or []
                if isinstance(rth_allowed_override_raw, (str, int, float)):
                    rth_allowed_override_raw = [rth_allowed_override_raw]
                rth_allowed_override = {
                    self._normalize_feature_resolution(res) for res in rth_allowed_override_raw
                }
                if rth_allowed_override:
                    allowed_rth_res.update(rth_allowed_override)
                if bool(feature_calc_cfg.get("enable_1d_during_rth", False)):
                    allowed_rth_res.add("1d")
                if not rth_allowed_override:
                    allowed_rth_res.update(required_rth_resolutions)
                logger.info(
                    "feature_calc_rth_resolution_gate",
                    is_rth=is_rth,
                    allowed_rth_resolutions=sorted(allowed_rth_res),
                    override_active=bool(rth_allowed_override),
                    inferred_required_resolutions=sorted(required_rth_resolutions),
                )
                for cfg in more_feature_res:
                    res = cfg.get("res")
                    if is_rth and res not in allowed_rth_res:
                        logger.info(
                            "   ⏭️  Skipping Feature Calculator due to RTH",
                            resolution=res
                        )
                        continue
                    try:
                        calc = FeatureCalculator(
                            resolution=res,
                            horizon_minutes=cfg["horizon"],
                            waterline_delay_sec=cfg["waterline"],
                            poll_interval_sec=cfg["poll"],
                        )
                        await calc.start()
                        self._feature_calcs.append(calc)
                        logger.info(
                            "   ✅ Feature Calculator started",
                            resolution=res,
                            horizon_minutes=cfg["horizon"],
                            waterline_delay_sec=cfg["waterline"],
                            poll_interval_sec=cfg["poll"],
                        )
                        # Stagger calculator startups slightly to smooth out initial load
                        await asyncio.sleep(0.5)
                    except Exception as exc:
                        logger.error(
                            "   ❌ Failed to start Feature Calculator",
                            resolution=res,
                            error=str(exc),
                        )

                # Start RFE fallback supervisor (starts/stops 1m calc dynamically if RFE stalls during RTH)
                try:
                    if self._rfe_fallback_task is None and bool(feature_runtime.get("has_rfe_strategy", False)):
                        self._rfe_fallback_task = asyncio.create_task(self._rfe_fallback_supervisor(), name="rfe_fallback_supervisor")
                        logger.info("   🔍 RFE fallback supervisor started")
                    elif not bool(feature_runtime.get("has_rfe_strategy", False)):
                        logger.info("   ⏭️  RFE fallback supervisor skipped (no enabled RFE strategies)")
                except Exception as exc:
                    logger.warning("   ⚠️  RFE fallback supervisor failed to start", error=str(exc))
            else:
                logger.info("   ⏭️  Feature Calculator Service disabled in config.")

            # ==================================================================
            # 3b. BACKFILL SERVICES (Phase 3 - Multiple Instances)
            # ==================================================================
            logger.info("3️⃣b  Starting Backfill Service(s)...")
            from services.market.backfill_service import BackfillService

            backfill_cfg = config.get('services.backfill', {})
            instances = backfill_cfg.get('instances', [])

            if instances:
                for idx, bf in enumerate(instances):
                    if not bf.get("enabled", False):
                        logger.info(f"   ⏭️  Skipping disabled backfill: {bf.get('name', 'unnamed')}")
                        continue

                    try:
                        svc = BackfillService(
                            kafka_bootstrap=config.kafka_config["bootstrap_servers"],
                            gap_topic=bf["gap_topic"],
                            request_topic=bf.get("request_topic"),
                            bar_topic=bf["bar_topic"],
                            group_id=bf.get("group_id", f"backfill.{bf['name']}"),
                            max_ib_concurrency=int(bf.get("max_ib_concurrency", 1)),
                            min_ib_interval_sec=float(bf.get("min_ib_interval_sec", 1.0)),
                            default_use_rth=bool(bf.get("default_use_rth", True)),
                            default_bar_size=bf.get("default_bar_size", "5 secs"),
                            ib_client_id=None,
                            shutdown_event=SHUTDOWN_EVENT,
                        )
                        await svc.start()
                        self._backfills.append(svc)
                        logger.info(f"   ✅ Backfill Service started: {bf['name']} ({bf['bar_topic']})")
                        
                        # [CRITICAL FIX] Stagger startup to prevent Event Loop storm and Kafka timeouts
                        # Give each service 5s to settle connections (Kafka JoinGroup + IB Handshake)
                        await asyncio.sleep(5.0)
                    except Exception as exc:
                        logger.error(f"   ❌ Failed to start backfill {bf.get('name')}", error=str(exc))

                if self._backfills:
                    logger.info(f"   ✅ Total {len(self._backfills)} Backfill service(s) started")
            else:
                logger.info("   📋 No backfill instances configured")

            # ==================================================================
            # 3c. CATCHUP SERVICE(S) (Phase 3/5 - Gap Detection)
            # ==================================================================
            logger.info("3️⃣c  Starting Catchup Service(s)...")
            try:
                from services.market.catchup_service import CatchupService, CatchupConfig

                catchup_cfg_raw = config.get('services.catchup', {})
                catchup_instances = catchup_cfg_raw.get('instances', []) if catchup_cfg_raw else []

                if catchup_instances:
                    for catchup_inst in catchup_instances:
                        if not catchup_inst.get("enabled", False):
                            logger.info(f"   ⏭️  Skipping disabled catchup: {catchup_inst.get('name', 'unnamed')}")
                            continue

                        try:
                            catchup_cfg = CatchupConfig(
                                enabled=bool(catchup_inst.get("enabled", True)),
                                interval_sec=int(catchup_inst.get("interval_sec", 120)),
                                bar_size=str(catchup_inst.get("bar_size", "1 min")),
                                request_topic=str(catchup_inst.get("request_topic", "md.backfill.request.1m.v1")),
                                use_rth=bool(catchup_inst.get("use_rth", True)),
                                lag_grace_sec=int(catchup_inst.get("lag_grace_sec", 30)),
                                max_range_minutes=int(catchup_inst.get("max_range_minutes", 60)),
                                symbols=list(catchup_inst.get("symbols") or []),
                                view=str(catchup_inst.get("view", "bars_1m_current")),
                            )
                            catchup_svc = CatchupService(catchup_cfg)
                            await catchup_svc.start()
                            self._catchups.append(catchup_svc)
                            logger.info(f"   ✅ Catchup Service started: {catchup_inst.get('name', 'unnamed')} ({catchup_cfg.view})")
                        except Exception as exc:
                            logger.error(f"   ❌ Failed to start catchup {catchup_inst.get('name', 'unnamed')}", error=str(exc), exc_info=True)

                    logger.info(f"   ✅ Total {len(self._catchups)} Catchup service(s) started")
                else:
                    logger.info("   📋 No Catchup Services configured")
            except Exception as exc:
                logger.error("❌ Catchup services section failed - continuing without catchup services", error=str(exc), exc_info=True)

            # ==================================================================
            # 3d. BROKER READINESS GATE
            # ==================================================================
            _write_boot_production_state_artifact(
                stage="broker_waiting",
                reason="waiting_for_ib_broker_readiness",
                details={
                    "host": ib_cfg.get("host"),
                    "port": ib_cfg.get("port"),
                    "trading_mode": trading_mode,
                },
            )
            logger.info("3️⃣d  Waiting for IB broker readiness...")
            from services.ibkr.ib_connection_manager import get_ib_connection_manager

            ib_manager = get_ib_connection_manager()
            self.system_health_controller = SystemHealthController(
                alert_sink=self._send_system_alert,
                audit_writer=self.audit_writer,
            )
            from services.runtime.lifecycle_controller import RuntimeLifecycleController, RuntimeLifecycleState

            self.runtime_lifecycle_controller = RuntimeLifecycleController(
                runtime_identity={
                    "runtime_pid": os.getpid(),
                    "process_pid": os.getpid(),
                    "process_start_time": _RUN_SYSTEM_PROCESS_START_ET.isoformat(),
                    "config_hash": _hash_file(Path(__file__).parent / "config.yml"),
                }
            )
            self.runtime_lifecycle_controller.transition(
                RuntimeLifecycleState.IB_PORT_WAITING,
                reason="waiting_for_ib_broker_readiness",
                source="run_system.start",
                metadata={"host": ib_cfg.get("host"), "port": ib_cfg.get("port")},
            )
            ib_manager.register_health_event_listener(
                "run_system_health_controller",
                self.system_health_controller.on_ib_connection_event,
            )
            exec_enabled = trading_mode in ('paper', 'live')
            self.ib_adapter_exec = None
            startup_gate_attempt = 0
            while True:
                startup_gate_attempt += 1
                market_ready, exec_ready = await ib_manager.wait_until_ready(
                    host=ib_cfg['host'],
                    port=ib_cfg['port'],
                    account=os.getenv("IB_ACCOUNT", ""),
                    require_exec=exec_enabled,
                    require_master=exec_enabled,
                    timeout_sec=float(os.getenv("RUN_SYSTEM_IB_STARTUP_GATE_SEC", "90")),
                    initiator="run_system.start",
                )

                exec_adapter_error = None
                if exec_enabled and exec_ready:
                    try:
                        self.ib_adapter_exec = await ib_manager.get_exec_adapter(
                            host=ib_cfg['host'],
                            port=ib_cfg['port'],
                            account=os.getenv("IB_ACCOUNT", ""),
                        )
                    except Exception as exc:
                        exec_adapter_error = exc
                        logger.error(
                            "run_system_startup_exec_adapter_not_ready",
                            attempt=startup_gate_attempt,
                            error=str(exc),
                            exc_info=True,
                        )

                ib_startup_status = self._ib_startup_status_snapshot(ib_manager, exec_enabled=exec_enabled)
                if bool(ib_startup_status["ready"]) and exec_adapter_error is None:
                    break

                retry_sec = max(5.0, float(os.getenv("RUN_SYSTEM_IB_STARTUP_RETRY_SEC", "60")))
                ib_gateway_circuit = {}
                try:
                    ib_gateway_circuit = ib_manager.reconnect_gatekeeper.gateway_circuit_snapshot()
                    circuit_retry_after = float(ib_gateway_circuit.get("retry_after_sec") or 0.0)
                    if bool(ib_gateway_circuit.get("open")) and circuit_retry_after > 0.0:
                        retry_sec = max(retry_sec, min(circuit_retry_after, float(os.getenv("RUN_SYSTEM_IB_STARTUP_CIRCUIT_MAX_SLEEP_SEC", "300"))))
                except Exception:
                    ib_gateway_circuit = {}
                circuit_manual_required = bool(ib_gateway_circuit.get("manual_required")) if ib_gateway_circuit else False
                startup_not_ready_reason = (
                    "manual_recovery_required"
                    if circuit_manual_required
                    else "startup_ib_not_ready_retrying"
                )
                logger.error(
                    "run_system_startup_manual_recovery_required"
                    if circuit_manual_required
                    else "run_system_startup_ib_not_ready_retrying",
                    attempt=startup_gate_attempt,
                    retry_sec=retry_sec,
                    ib_gateway_circuit_open=bool(ib_gateway_circuit.get("open")) if ib_gateway_circuit else False,
                    ib_gateway_circuit_manual_required=circuit_manual_required,
                    ib_gateway_circuit_reason=ib_gateway_circuit.get("reason") if ib_gateway_circuit else None,
                    market_ib_connected=bool(ib_startup_status["market_ready"]),
                    exec_ib_connected=bool(ib_startup_status["exec_ready"]) if exec_enabled else None,
                    master_ib_connected=bool(ib_startup_status["master_ready"]) if exec_enabled else None,
                    master_truth_complete=bool(ib_startup_status["master_truth_complete"]) if exec_enabled else None,
                    trading_mode=trading_mode,
                )
                if self.system_health_controller is not None:
                    await self.system_health_controller.publish(
                        event_type="system_degraded",
                        reason=startup_not_ready_reason,
                        source="run_system.start",
                        detail=(
                            f"attempt={startup_gate_attempt},retry_sec={retry_sec},"
                            f"market={bool(ib_startup_status['market_ready'])},"
                            f"exec={bool(ib_startup_status['exec_ready'])},"
                            f"master={bool(ib_startup_status['master_ready'])},"
                            f"master_truth={bool(ib_startup_status['master_truth_complete'])},"
                            f"exec_adapter_error={str(exec_adapter_error) if exec_adapter_error else ''},"
                            f"ib_gateway_circuit_open={bool(ib_gateway_circuit.get('open')) if ib_gateway_circuit else False},"
                            f"ib_gateway_circuit_manual_required={circuit_manual_required},"
                            f"ib_gateway_circuit_retry_after_sec={ib_gateway_circuit.get('retry_after_sec') if ib_gateway_circuit else None}"
                        ),
                        metadata={"ib_gateway_circuit": ib_gateway_circuit},
                    )
                if circuit_manual_required:
                    raise RuntimeError("manual_recovery_required:ib_gateway_api_half_open")
                await asyncio.sleep(retry_sec)
            logger.info(
                "run_system_startup_ib_ready",
                market_ib_connected=bool(ib_startup_status["market_ready"]),
                exec_ib_connected=bool(ib_startup_status["exec_ready"]) if exec_enabled else None,
                master_ib_connected=bool(ib_startup_status["master_ready"]) if exec_enabled else None,
                master_truth_complete=bool(ib_startup_status["master_truth_complete"]) if exec_enabled else None,
                trading_mode=trading_mode,
            )
            if self.system_health_controller is not None:
                await self.system_health_controller.publish(
                    event_type="ib_connected",
                    reason="startup_ib_ready",
                    source="run_system.start",
                    detail=(
                        f"market_ready={bool(ib_startup_status['market_ready'])},"
                        f"exec_ready={bool(ib_startup_status['exec_ready'])},"
                        f"master_ready={bool(ib_startup_status['master_ready'])},"
                        f"master_truth_complete={bool(ib_startup_status['master_truth_complete'])}"
                    ),
                )
            if getattr(self, "_market_data_start_deferred_until_broker_ready", False):
                if not hasattr(self, "market_data") or self.market_data is None:
                    raise RuntimeError("market_data_deferred_start_missing_service")
                market_data_task = asyncio.create_task(self.market_data.start(), name="market_data_service")
                self.services.append(("market_data", market_data_task))
                self._market_data_start_deferred_until_broker_ready = False
                logger.info("   ✅ Market Data Service started after broker readiness gate")
                await asyncio.sleep(3)
            if getattr(self, "_vix_ingestor_deferred_until_broker_ready", False):
                await self._start_vix_ingestor_if_enabled(ib_cfg=ib_cfg)
                self._vix_ingestor_deferred_until_broker_ready = False
            if exec_enabled and self.ib_adapter_exec is not None:
                self._start_account_snapshot_task(self.ib_adapter_exec)

            # ==================================================================
            # 4. STRATEGY ENGINE SERVICE
            # ==================================================================
            logger.info("4️⃣  Starting Strategy Engine...")
            from services.strategy.engine import StrategyEngine

            # Initialize Decision Pipeline
            decision_pipeline = None
            try:
                logger.info("   🔧 Initializing Decision Pipeline...")
                from services.decision import (
                    DecisionPipelineService,
                    MarketStateProvider,
                    BarStore,
                    FeatureStore,
                    ReserveLedger,
                    ADVProvider,
                    TickSizeProvider,
                    ValidateConfig,
                )
                from libs.db.ch_pool import get_client
                from libs.db.postgres import PostgresDB
                from libs.dal.market_data_facade import MarketDataFacade

                # Initialize dependencies (using global connection pool)
                ch = get_client()
                self.pg = PostgresDB()
                await asyncio.sleep(0.05) # Yield control to event loop before connecting to DB
                await self.pg.connect()  # Create asyncpg connection pool
                from services.ibkr.ib_order_truth_schema import assert_ib_order_truth_schema
                await assert_ib_order_truth_schema(self.pg, source="run_system_startup")
                md_facade = MarketDataFacade(ch=ch)

                # Create providers
                market_state_provider = MarketStateProvider(md_facade)
                bar_store = BarStore(ch)
                feature_store = FeatureStore()
                reserve_ledger = ReserveLedger(self.pg)
                adv_provider = ADVProvider(ch)
                tick_size_provider = TickSizeProvider()

                # Initialize pipeline in ACTIVE mode for real paper trading
                dp_cfg = config.get('decision_pipeline', {}) or {}
                trace_cfg = dp_cfg.get("trace_validation", {}) or {}
                trace_validation_blocking = bool(trace_cfg.get("blocking", False))
                bypass_all_system_gates = bool(config.get('system_gate_bypass.enabled', False))
                bypass_all_system_gates = bypass_all_system_gates or (
                    os.getenv("BYPASS_SYSTEM_GATES", "").lower() in ("1", "true", "yes", "on")
                )
                translator_cfg = {
                    "respect_order_style": bool(dp_cfg.get("respect_order_style", False)) or bypass_all_system_gates,
                    "fallback_to_market_on_missing_l1": bool(dp_cfg.get("fallback_to_market_on_missing_l1", False)) or bypass_all_system_gates,
                }
                validator_cfg_raw = dp_cfg.get("validator", {}) or {}
                validator_cfg = ValidateConfig(
                    min_hist_bars=int(validator_cfg_raw.get("min_hist_bars", 36)),
                    bar_gap_ms_max=int(validator_cfg_raw.get("bar_gap_ms_max", 1200)),
                    min_dlb_ms=int(validator_cfg_raw.get("min_dlb_ms", 2000)),
                    require_nbbo=bool(validator_cfg_raw.get("require_nbbo", True)),
                    enforce_quote_age=bool(validator_cfg_raw.get("enforce_quote_age", False)),
                    max_quote_age_sec=float(validator_cfg_raw.get("max_quote_age_sec", 10.0)),
                    min_volume=float(validator_cfg_raw.get("min_volume", 1.0)),
                    min_atr_bp=float(validator_cfg_raw.get("min_atr_bp", 1.0)),
                    edge_buffer_bp=float(validator_cfg_raw.get("edge_buffer_bp", 2.0)),
                    allow_strategy_history_ready_bypass=bool(
                        validator_cfg_raw.get("allow_strategy_history_ready_bypass", True)
                    ),
                    history_ready_allowed_strategy_ids=tuple(
                        str(x).strip()
                        for x in (validator_cfg_raw.get("history_ready_allowed_strategy_ids", []) or [])
                        if str(x).strip()
                    ),
                )
                decision_pipeline = DecisionPipelineService(
                    market_state_provider=market_state_provider,
                    bar_store=bar_store,
                    feature_store=feature_store,
                    reserve_ledger=reserve_ledger,
                    adv_provider=adv_provider,
                    tick_size_provider=tick_size_provider,
                    validator_cfg=validator_cfg,
                    translator_cfg=translator_cfg,
                    dry_run_mode=False,  # ACTIVE mode - decisions will execute as orders
                    bypass_validator=bool(dp_cfg.get('bypass_validator', False)) or bypass_all_system_gates,
                    bypass_arbiter=bool(dp_cfg.get('bypass_arbiter', False)) or bypass_all_system_gates,
                    trace_validation_blocking=trace_validation_blocking,
                    audit_writer=self.audit_writer,
                )
                logger.info("   ✅ Decision Pipeline initialized (ACTIVE MODE)")
            except Exception as e:
                logger.error("   ❌ Failed to initialize Decision Pipeline",
                           error=str(e),
                           exc_info=True)
                decision_pipeline = None
            if exec_enabled and decision_pipeline is None:
                raise RuntimeError("decision_pipeline_init_failed")

            self.strategy_engine = StrategyEngine(
                decision_pipeline=decision_pipeline,
                price_cache=self.price_cache,
                market_data_service=self.market_data if hasattr(self, 'market_data') else None,
                system_health_controller=self.system_health_controller,
            )
            self.strategy_engine.audit_writer = self.audit_writer
            setattr(self.strategy_engine.engine, "audit_writer", self.audit_writer)
            # Note: StrategyEngine is event-driven, strategies loaded separately
            logger.info("   ✅ Strategy Engine initialized")
            if self.price_cache:
                logger.info("   🔗 PriceCache connected to StrategyEngine")

            # ==============================================================
            # 3b. Load enabled strategies from config.yml
            # ==============================================================
            self._ensure_option_quotes_table()
            phase0_gate = await self._run_phase0_freeze_gate()
            logger.info(
                "   ✅ Phase0 freeze gate admitted live strategy surface",
                live_strategy_ids=list(phase0_gate.get("current_live_strategy_ids") or []),
                blocked_strategy_ids=list(phase0_gate.get("current_forbidden_strategy_ids") or []),
            )
            try:
                from services.production.strategy_data_contract import validate_strategy_production_manifests

                manifest_validation = validate_strategy_production_manifests(
                    getattr(config, "_config", {}) or {},
                    mandatory_strategy_ids=getattr(self, "_phase0_live_strategy_ids", set()) or set(),
                )
                self.strategy_production_manifests = list(manifest_validation.manifests)
                logger.info(
                    "strategy_production_manifest_gate_evaluated",
                    ok=bool(manifest_validation.ok),
                    manifest_count=len(manifest_validation.manifests),
                    blockers=list(manifest_validation.blockers),
                    missing_strategy_ids=list(manifest_validation.missing_strategy_ids),
                )
                if not manifest_validation.ok:
                    raise RuntimeError(
                        "strategy_production_manifest_gate_blocked:"
                        + json.dumps(manifest_validation.to_dict(), sort_keys=True)
                    )
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(f"strategy_production_manifest_gate_failed:{exc}") from exc
            registry_cfg = config.get('strategy_registry', {}) or {}
            registry = get_registry()

            # Ensure registry entries reflect config mappings
            for name, path in registry_cfg.items():
                try:
                    module_name, class_name = path.rsplit('.', 1)
                    cls = getattr(import_module(module_name), class_name)
                    registry.register(name, cls)
                except ModuleNotFoundError as exc:
                    logger.warning("Strategy module not found; skipping registration",
                                   name=name,
                                   path=path,
                                   error=str(exc))
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to register strategy class",
                                 name=name,
                                 path=path,
                                 error=str(exc))

            loaded_strategies: List[str] = []
            strategy_load_failures: List[Dict[str, str]] = []
            mandatory_live_strategy_ids = {
                str(item).strip()
                for item in getattr(self, "_phase0_live_strategy_ids", set())
                if str(item).strip()
            }
            logger.info(
                "   ⏸️  Deferring enabled strategy load until execution/UOM/reconciler/EOD are ready",
                mandatory_live_strategy_ids=sorted(mandatory_live_strategy_ids),
            )

            # ==================================================================
            # 5. EXECUTION SYSTEM (SafetyKernel + ExecutionService)
            # ==================================================================
            logger.info("5️⃣  Starting Execution System...")

            execution_service = None
            runtime_reconciler = None
            execution_init_error = None
            startup_reconcile_result = None
            runtime_reconcile_evidence: Dict[str, Any] = {}

            try:
                from services.exec import ExecutionService, SafetyKernel, FillEventHandler
                from services.reconciliation import RuntimeReconciler
                from services.exec.adapters.ib_adapter import IBInsyncAdapter
                from services.exec.manual_intervention_lock import PostgresManualInterventionLockStore
                from services.notifications import build_notification_manager
                from services.telegram.manual_flatten_service import ManualFlattenService
                from services.orders import PositionLedger

                # Check if execution is enabled (respects trading mode)
                exec_enabled = trading_mode in ('paper', 'live')

                if exec_enabled:
                    notification_manager = self._ensure_notification_manager()
                    self.notification_manager = notification_manager

                    daily_summary_enabled = str(os.getenv("DAILY_TRADE_SUMMARY_ENABLED", "1")).strip().lower() in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    )
                    if notification_manager and daily_summary_enabled:
                        try:
                            from services.reports import DailyTradeSummaryService

                            self.daily_trade_summary_service = DailyTradeSummaryService(
                                pg=self.pg,
                                notification_manager=notification_manager,
                            )
                            daily_summary_task = asyncio.create_task(self.daily_trade_summary_service.run_forever())
                            self.services.append(("daily_trade_summary", daily_summary_task))
                            logger.info(
                                "   ✅ DailyTradeSummaryService started",
                                report_time_et=str(os.getenv("DAILY_TRADE_SUMMARY_TIME_ET", "16:01")),
                            )
                        except Exception as exc:
                            logger.warning("   ⚠️  DailyTradeSummaryService initialization failed", error=str(exc), exc_info=True)
                    elif not notification_manager:
                        logger.info("   ⏭️  DailyTradeSummaryService disabled (telegram notifier unavailable)")
                    else:
                        logger.info("   ⏭️  DailyTradeSummaryService disabled (DAILY_TRADE_SUMMARY_ENABLED=false)")

                    # Use shared IB execution adapter to avoid extra IB sessions.
                    from services.ibkr.ib_connection_manager import get_ib_connection_manager
                    self.ib_adapter_exec = self.ib_adapter_exec or await get_ib_connection_manager().get_exec_adapter(
                        host=ib_cfg['host'],
                        port=ib_cfg['port'],
                        account=os.getenv("IB_ACCOUNT", ""),
                    )
                    logger.info(f"   🔗 Using shared IB execution adapter (client_id={self.ib_adapter_exec.client_id})")

                    # Decide risk-bypass mode before wiring reservation dependencies.
                    exec_cfg = config.get('execution', {}) or {}
                    trace_cfg = exec_cfg.get('trace_validation', {}) or {}
                    order_event_cfg = exec_cfg.get('order_event', {}) or {}
                    trace_validation_enabled = bool(trace_cfg.get('enabled', True))
                    trace_validation_blocking = bool(trace_cfg.get('blocking', False))
                    paper_force_non_blocking = bool(trace_cfg.get('paper_force_non_blocking', True))
                    allow_paper_trace_blocking = os.getenv("ALLOW_PAPER_TRACE_BLOCKING", "").lower() in ("1", "true", "yes", "on")
                    if trading_mode == "paper" and trace_validation_blocking and paper_force_non_blocking and not allow_paper_trace_blocking:
                        logger.warning(
                            "   ⚠️  Forcing non-blocking trace validation in paper mode",
                            requested_blocking=True,
                            paper_force_non_blocking=paper_force_non_blocking,
                            env_allow_paper_trace_blocking=allow_paper_trace_blocking,
                        )
                        trace_validation_blocking = False
                    order_event_enabled = bool(order_event_cfg.get('enabled', True))
                    entry_isolation_cfg = exec_cfg.get('entry_isolation', {}) or {}
                    entry_isolation_enabled = bool(entry_isolation_cfg.get('enabled', False))
                    entry_isolation_symbols = [
                        str(s).upper() for s in (entry_isolation_cfg.get('symbols', []) or []) if str(s).strip()
                    ]
                    entry_isolation_mode = str(entry_isolation_cfg.get('mode') or 'cross_strategy').strip().lower()
                    if entry_isolation_mode not in {'cross_strategy', 'strategy_only'}:
                        logger.warning(
                            "   ⚠️  Invalid entry isolation mode; falling back to cross_strategy",
                            configured_mode=entry_isolation_mode,
                        )
                        entry_isolation_mode = 'cross_strategy'

                    if order_event_enabled:
                        try:
                            reg = await self.pg.fetchone("SELECT to_regclass('core.core_order_event') AS reg")
                            event_table_ready = bool((reg or {}).get('reg'))
                            if not event_table_ready:
                                logger.warning(
                                    "   ⚠️  core.core_order_event not found; event writes will fail-open",
                                    table="core.core_order_event",
                                )
                        except Exception as exc:
                            logger.warning(
                                "   ⚠️  Failed to verify core_order_event table; keeping fail-open mode",
                                error=str(exc),
                            )

                    logger.info(
                        "   ℹ️  Execution trace/event settings",
                        trading_mode=trading_mode,
                        trace_validation_enabled=trace_validation_enabled,
                        trace_validation_blocking=trace_validation_blocking,
                        order_event_enabled=order_event_enabled,
                        entry_isolation_enabled=entry_isolation_enabled,
                        entry_isolation_symbols=entry_isolation_symbols,
                        entry_isolation_mode=entry_isolation_mode,
                    )

                    bypass_all_system_gates = bool(config.get('system_gate_bypass.enabled', False))
                    bypass_all_system_gates = bypass_all_system_gates or (
                        os.getenv("BYPASS_SYSTEM_GATES", "").lower() in ("1", "true", "yes", "on")
                    )
                    bypass_risk_checks = bool(exec_cfg.get('bypass_risk_checks', False))
                    bypass_risk_checks = bypass_risk_checks or (
                        os.getenv("BYPASS_RISK_CHECKS", "").lower() in ("1", "true", "yes", "on")
                    )
                    bypass_risk_checks = bypass_risk_checks or bypass_all_system_gates

                    # SafetyKernel expects a ReservationService-like object exposing release_on_terminal().
                    # In bypass mode there is no reservation lifecycle, so keep it as None.
                    kernel_reservation_service = None
                    if not bypass_risk_checks:
                        from services.risk.reservation_service import ReservationService
                        kernel_reservation_service = ReservationService(db=self.pg)

                    # Initialize SafetyKernel with rate limiting
                    net_cfg = config.get('execution.netting', {}) or {}
                    net_enabled = bool(net_cfg.get('enabled', False))
                    net_batch_ms = int(net_cfg.get('batch_window_ms', 120))
                    net_strategy_id = str(net_cfg.get('strategy_id', '__netting__') or '__netting__')
                    net_fanout_enabled = bool(net_cfg.get('fill_fanout_enabled', net_enabled))

                    netting_fill_fanout = None
                    if net_enabled and net_fanout_enabled:
                        try:
                            from services.exec.netting import NettingFillFanout
                            netting_fill_fanout = NettingFillFanout(
                                pg=self.pg,
                                reservation_service=kernel_reservation_service,
                            )
                        except Exception as exc:
                            logger.warning("   ⚠️  NettingFillFanout init failed", error=str(exc))

                    manual_intervention_lock_store = PostgresManualInterventionLockStore(
                        pg=self.pg,
                    )
                    from services.exec.flatten_controller import FlattenController
                    from services.strategy_runtime_v2 import build_local_audit_transition_sink
                    flatten_controller = FlattenController(
                        pg=self.pg,
                        manual_intervention_lock_store=manual_intervention_lock_store,
                        transition_sink=build_local_audit_transition_sink(
                            self.audit_writer,
                            component="flatten_controller",
                        ),
                    )
                    position_ledger = PositionLedger(pg=self.pg)
                    await position_ledger.ensure_schema()

                    self.safety_kernel = SafetyKernel(
                        broker_adapter=self.ib_adapter_exec,
                        pg=self.pg,  # From decision pipeline initialization
                        reservation_service=kernel_reservation_service,
                        netting_fill_fanout=netting_fill_fanout,
                        position_ledger=position_ledger,
                        manual_intervention_lock_store=manual_intervention_lock_store,
                        flatten_controller=flatten_controller,
                        place_rpm=10,  # 10 orders per minute max
                        modify_rpm=30,
                        cancel_rpm=30,
                        oco_attach_timeout_sec=1.0
                    )
                    await self.safety_kernel.start()
                    logger.info("   ✅ SafetyKernel started (rate limits: 10 place/min, 30 modify/min, 30 cancel/min)")

                    # Initialize FillEventHandler
                    self.fill_handler = FillEventHandler(
                        ib_adapter=self.ib_adapter_exec,
                        safety_kernel=self.safety_kernel,
                        notification_manager=notification_manager,
                        order_event_enabled=order_event_enabled,
                        audit_writer=self.audit_writer,
                    )
                    await self.fill_handler.start()
                    logger.info("   ✅ FillEventHandler started (subscribed to IB fill events)")

                    # Initialize ExecutionService with RiskService (or test bypass)
                    if bypass_risk_checks:
                        from services.risk.noop_risk_service import NoopRiskService
                        risk_service = NoopRiskService()
                        if bypass_all_system_gates:
                            logger.warning("   ⚠️  Risk checks bypassed (system_gate_bypass.enabled=true)")
                        else:
                            logger.warning("   ⚠️  Risk checks bypassed (execution.bypass_risk_checks=true)")
                    else:
                        from services.risk.risk_service import RiskService
                        from services.risk.reg_lite import RegLiteGuard

                        # Initialize RegLiteGuard (regulatory compliance)
                        reg_lite_guard = RegLiteGuard(
                            price_collar_pct=5.0,  # 5% price collar
                            enable_luld_warning=True,
                            enable_ssr_strict=True
                        )

                        # Initialize RiskService
                        risk_service = RiskService(
                            reservation_service=kernel_reservation_service,
                            reg_lite_guard=reg_lite_guard,
                            capital_quota_service=None,  # Optional - not used yet
                            market_state_provider=market_state_provider if decision_pipeline else None,
                            default_buffer_bps=10
                        )

                    execution_router = None
                    netted_execution_service = None
                    try:
                        from services.exec.netting import ExecutionRouter, RoutingConfig, NettedExecutionService

                        mode_overrides = net_cfg.get('strategy_mode_overrides', {}) or {}
                        symbol_overrides = {
                            str(k).upper(): str(v).lower()
                            for k, v in (net_cfg.get('symbol_mode_overrides', {}) or {}).items()
                        }
                        netted_symbols = {str(s).upper() for s in (net_cfg.get('symbols', []) or [])}
                        default_mode = str(net_cfg.get('default_mode', 'direct')).lower()
                        router_cfg = RoutingConfig(
                            default_mode=default_mode,
                            strategy_mode_overrides={str(k): str(v).lower() for k, v in mode_overrides.items()},
                            symbol_mode_overrides=symbol_overrides,
                            netted_symbols=netted_symbols,
                        )
                        execution_router = ExecutionRouter(router_cfg)

                        if net_enabled:
                            netted_execution_service = NettedExecutionService(
                                pg=self.pg,
                                risk_service=risk_service,
                                safety_kernel=self.safety_kernel,
                                enable_execution=True,
                                notification_manager=notification_manager,
                                batch_window_ms=net_batch_ms,
                                netting_strategy_id=net_strategy_id,
                            )
                            logger.info(
                                "   ✅ NettedExecutionService initialized",
                                netting_enabled=True,
                                batch_window_ms=net_batch_ms,
                                netting_strategy_id=net_strategy_id,
                            )
                        else:
                            logger.info("   ℹ️  NettedExecutionService disabled by config")
                    except Exception as exc:
                        logger.warning("   ⚠️  Netting services init failed; fallback to direct only", error=str(exc))

                    execution_service = ExecutionService(
                        manual_intervention_lock_store=manual_intervention_lock_store,
                        flatten_controller=flatten_controller,
                        pg=self.pg,
                        risk_service=risk_service,
                        safety_kernel=self.safety_kernel,
                        enable_execution=True,  # Actually place orders (paper mode)
                        notification_manager=notification_manager,
                        execution_router=execution_router,
                        netted_execution_service=netted_execution_service,
                        trace_validation_enabled=trace_validation_enabled,
                        trace_validation_blocking=trace_validation_blocking,
                        order_event_enabled=order_event_enabled,
                        entry_isolation_enabled=entry_isolation_enabled,
                        entry_isolation_symbols=entry_isolation_symbols,
                        entry_isolation_mode=entry_isolation_mode,
                        trading_gate_provider=lambda _symbol: {
                            "exec_ready": bool(ib_manager.exec_connected()),
                            "market_ready": bool(ib_manager.market_connected()),
                            "master_truth_complete": bool(ib_manager.master_truth_complete()),
                            "trading_enabled": bool(
                                ib_manager.exec_connected()
                                and ib_manager.market_connected()
                                and ib_manager.master_truth_complete()
                            ),
                        },
                        tradability_inputs_provider=self._build_scoped_tradability_inputs,
                        audit_writer=self.audit_writer,
                    )
                    from services.orders import UnifiedOrderManager

                    unified_order_manager = UnifiedOrderManager(
                        execution_service=execution_service,
                        system_health_controller=self.system_health_controller,
                        position_ledger=position_ledger,
                        strategy_admission_provider=self._phase0_strategy_admission_provider,
                    )
                    await unified_order_manager.ensure_startup_ready()
                    logger.info("   ✅ ExecutionService initialized (execution ENABLED)")
                    logger.info("   ✅ UnifiedOrderManager initialized (phase2 wrapper)")

                    # Wire ExecutionService into DecisionPipeline
                    if decision_pipeline:
                        decision_pipeline.set_execution_service(unified_order_manager)
                        logger.info("   🔗 ExecutionService connected to DecisionPipeline")
                    if getattr(self, "strategy_engine", None) is not None:
                        setattr(self.strategy_engine.engine, "broker_adapter", self.ib_adapter_exec)
                        setattr(self.strategy_engine.engine, "execution_service", execution_service)

                    # Initialize RuntimeReconciler (ghost order detection)
                    recon_cfg = config.get('reconciliation', {}) or {}
                    rr_cfg = config.get('reconciliation.runtime', {}) or {}
                    from services.reconciliation.strategy_recovery_policy import (
                        default_strategy_recovery_policy_registry,
                        strategy_recovery_policy_registry_from_manifests,
                    )
                    recovery_policy_registry = strategy_recovery_policy_registry_from_manifests(
                        getattr(self, "strategy_production_manifests", []) or []
                    )
                    if not recovery_policy_registry.registered_strategy_ids():
                        recovery_policy_registry = default_strategy_recovery_policy_registry(
                            getattr(self, "_phase0_live_strategy_ids", set()) or set()
                        )
                    recovery_required_components = [
                        "market_data_service",
                        "market_data_ib",
                        "market_data_subscriptions",
                        "bar_pipeline",
                        "tick_producer",
                    ]
                    self.runtime_reconciler = RuntimeReconciler(
                        pg=self.pg,
                        ib_adapter=self.ib_adapter_exec,
                        account_id=str(getattr(self.ib_adapter_exec, "account", "") or os.getenv("IB_ACCOUNT", "") or ""),
                        manual_intervention_lock_store=manual_intervention_lock_store,
                        flatten_controller=flatten_controller,
                        reservation_service=kernel_reservation_service,
                        interval_seconds=int(rr_cfg.get('interval_seconds', 60)),
                        reconciliation_mode=str(recon_cfg.get('mode', 'report_only')),
                        trading_interval_seconds=float(rr_cfg.get('trading_interval_seconds', 300.0)),
                        offhours_interval_seconds=float(rr_cfg.get('offhours_interval_seconds', 60.0)),
                        trading_window_start_et=str(rr_cfg.get('trading_window_start_et', '09:30')),
                        trading_window_end_et=str(rr_cfg.get('trading_window_end_et', '16:00')),
                        dynamic_interval_enabled=bool(rr_cfg.get('dynamic_interval_enabled', True)),
                        query_offhours=bool(rr_cfg.get('query_offhours', False)),
                        alert_cooldown_seconds=int(rr_cfg.get('alert_cooldown_seconds', 300)),
                        notification_manager=notification_manager,
                        system_health_controller=self.system_health_controller,
                        order_event_enabled=order_event_enabled,
                        audit_writer=self.audit_writer,
                        startup_block_on_ghosts=bool(rr_cfg.get('startup_block_on_ghosts', True)),
                        startup_block_on_orphans=bool(rr_cfg.get('startup_block_on_orphans', True)),
                        startup_block_on_mismatches=bool(rr_cfg.get('startup_block_on_mismatches', True)),
                        flat_protective_auto_cancel_enabled=bool(rr_cfg.get('flat_protective_auto_cancel_enabled', False)),
                        strategy_recovery_policy_registry=recovery_policy_registry,
                        recovery_order_manager=unified_order_manager,
                        recovery_required_components=recovery_required_components,
                    )
                    startup_reconcile_result = await self.runtime_reconciler.ensure_startup_ready()
                    runtime_reconcile_evidence = self._runtime_reconciliation_resume_metadata(startup_reconcile_result)
                    try:
                        from services.production.recovery_readiness import get_recovery_readiness_service

                        recovery_readiness = get_recovery_readiness_service().snapshot(
                            required_components=recovery_required_components
                        )
                    except Exception as exc:
                        recovery_readiness = {
                            "ready": False,
                            "blockers": ["recovery_readiness_snapshot_failed"],
                            "error": str(exc),
                        }
                    runtime_reconcile_evidence["recovery_readiness"] = recovery_readiness
                    runtime_reconcile_evidence["platform_recovery_ready"] = bool(
                        recovery_readiness.get("ready")
                    )
                    await self.runtime_reconciler.start()
                    runtime_reconciler = self.runtime_reconciler
                    if self.system_health_controller is not None:
                        await self.system_health_controller.subscribe(
                            "runtime_reconciler",
                            self.runtime_reconciler,
                        )
                        startup_event_type = (
                            "system_resumed"
                            if bool(runtime_reconcile_evidence.get("platform_recovery_ready"))
                            else "system_recovering"
                        )
                        startup_reason = (
                            "startup_broker_truth_ready"
                            if startup_event_type == "system_resumed"
                            else "startup_platform_recovery_readiness_not_met"
                        )
                        await self.system_health_controller.publish(
                            event_type=startup_event_type,
                            reason=startup_reason,
                            source="run_system.runtime_reconciler",
                            metadata=runtime_reconcile_evidence,
                        )
                    if self.audit_writer is not None:
                        self.audit_writer.emit(
                            domain="system",
                            component="run_system",
                            stage="service_started",
                            status="started",
                            payload={"service": "runtime_reconciler"},
                        )
                    logger.info(
                        "   ✅ RuntimeReconciler started (dynamic polling for ghost orders)",
                        trading_interval_seconds=float(rr_cfg.get('trading_interval_seconds', 300.0)),
                        offhours_interval_seconds=float(rr_cfg.get('offhours_interval_seconds', 60.0)),
                        query_offhours=bool(rr_cfg.get('query_offhours', False)),
                        alert_cooldown_seconds=int(rr_cfg.get('alert_cooldown_seconds', 300)),
                    )

                    try:
                        from services.production.eod_supervisor import (
                            PlatformEODSupervisor,
                            session_close_policies_from_config,
                        )
                        from services.production.position_safety_supervisor import PositionSafetySupervisor
                        from services.production.production_state_emitter import ProductionStateEmitter
                        from services.production.session_end_supervisor import SessionEndSupervisor

                        eod_cfg = config.get("production.eod_supervisor", {}) or {}
                        eod_flatten = ManualFlattenService(
                            pg=self.pg,
                            execution_service=unified_order_manager,
                            ib_adapter=self.ib_adapter_exec,
                            runtime_reconciler=self.runtime_reconciler,
                            order_event_enabled=order_event_enabled,
                            fallback_strategy_id=str(
                                eod_cfg.get("fallback_strategy_id") or "platform_eod_flatten"
                            ),
                        )
                        self.platform_eod_supervisor = PlatformEODSupervisor(
                            manual_flatten_service=eod_flatten,
                            manual_intervention_lock_store=manual_intervention_lock_store,
                            flatten_time_et=str(
                                os.getenv(
                                    "PLATFORM_EOD_FLATTEN_TIME_ET",
                                    str(eod_cfg.get("flatten_time_et") or "15:59:00"),
                                )
                            ),
                            artifact_dir=str(
                                eod_cfg.get("artifact_dir") or "runtime/readiness/eod"
                            ),
                            enabled=bool(eod_cfg.get("enabled", True)),
                            poll_interval_sec=float(eod_cfg.get("poll_interval_sec", 5.0)),
                            session_close_policies=session_close_policies_from_config(
                                getattr(config, "_config", {}) or {}
                            ),
                        )
                        await self.platform_eod_supervisor.start()
                        eod_task = getattr(self.platform_eod_supervisor, "_task", None)
                        if eod_task is not None:
                            self.services.append(("platform_eod_supervisor", eod_task))
                        if exec_enabled and not bool(getattr(self.platform_eod_supervisor, "ready", False)):
                            raise RuntimeError("platform_eod_supervisor_not_active")
                        logger.info("   ✅ PlatformEODSupervisor started")

                        ps_cfg = config.get("production.position_safety_supervisor", {}) or {}
                        self.position_safety_supervisor = PositionSafetySupervisor(
                            pg=self.pg,
                            ib_adapter=self.ib_adapter_exec,
                            manual_intervention_lock_store=manual_intervention_lock_store,
                            strategy_data_contracts=getattr(self, "strategy_data_contracts", []) or [],
                            recovery_order_manager=unified_order_manager,
                            artifact_dir=str(
                                ps_cfg.get("artifact_dir") or "runtime/readiness/position_safety"
                            ),
                            live_broker_truth_repo_root=Path(__file__).parent,
                            enabled=bool(ps_cfg.get("enabled", True)),
                            poll_interval_sec=float(ps_cfg.get("poll_interval_sec", 30.0)),
                            fallback_strategy_id=str(
                                ps_cfg.get("fallback_strategy_id") or "platform_position_safety"
                            ),
                            flat_live_safety_order_auto_cancel_enabled=bool(
                                ps_cfg.get("flat_live_safety_order_auto_cancel_enabled", False)
                            ),
                        )
                        await self.position_safety_supervisor.start()
                        ps_task = getattr(self.position_safety_supervisor, "_task", None)
                        if ps_task is not None:
                            self.services.append(("position_safety_supervisor", ps_task))
                        if exec_enabled and not bool(getattr(self.position_safety_supervisor, "ready", False)):
                            raise RuntimeError("position_safety_supervisor_not_active")
                        logger.info("   ✅ PositionSafetySupervisor started")

                        session_end_cfg = config.get("production.session_end_supervisor", {}) or {}
                        self.session_end_supervisor = SessionEndSupervisor(
                            ib_adapter=self.ib_adapter_exec,
                            config=getattr(config, "_config", {}) or {},
                            strategy_data_contracts=getattr(self, "strategy_data_contracts", []) or [],
                            manual_intervention_lock_store=manual_intervention_lock_store,
                            artifact_dir=str(
                                session_end_cfg.get("artifact_dir") or "runtime/readiness/session_end"
                            ),
                            manual_lock_dir=str(
                                session_end_cfg.get("manual_lock_dir") or "runtime/readiness/manual_locks"
                            ),
                            enabled=bool(session_end_cfg.get("enabled", True)),
                            poll_interval_sec=float(session_end_cfg.get("poll_interval_sec", 30.0)),
                            default_deadline_et=str(session_end_cfg.get("default_deadline_et") or "15:59:00"),
                            manual_required_after_sec=float(session_end_cfg.get("manual_required_after_sec", 300.0)),
                        )
                        await self.session_end_supervisor.start()
                        session_end_task = getattr(self.session_end_supervisor, "_task", None)
                        if session_end_task is not None:
                            self.services.append(("session_end_supervisor", session_end_task))
                        if exec_enabled and not bool(getattr(self.session_end_supervisor, "ready", False)):
                            raise RuntimeError("session_end_supervisor_not_active")
                        logger.info("   ✅ SessionEndSupervisor started")

                        ps_emitter_cfg = config.get("production.production_state_emitter", {}) or {}
                        self.production_state_emitter = ProductionStateEmitter(
                            repo_root=Path(__file__).parent,
                            enabled=bool(ps_emitter_cfg.get("enabled", True)),
                            interval_sec=float(ps_emitter_cfg.get("interval_sec", 30.0)),
                            system_health_controller=self.system_health_controller,
                            runtime_lifecycle_controller=getattr(self, "runtime_lifecycle_controller", None),
                            readiness_refreshers=[
                                ("position_safety", self.position_safety_supervisor.run_once),
                            ],
                            readiness_refresh_timeout_sec=float(
                                ps_emitter_cfg.get("readiness_refresh_timeout_sec", 20.0)
                            ),
                        )
                        await self.production_state_emitter.start()
                        ps_emitter_task = getattr(self.production_state_emitter, "_task", None)
                        if ps_emitter_task is not None:
                            self.services.append(("production_state_emitter", ps_emitter_task))
                        if exec_enabled and not bool(getattr(self.production_state_emitter, "ready", False)):
                            raise RuntimeError("production_state_emitter_not_active")
                        logger.info("   ✅ ProductionStateEmitter started")
                    except Exception as exc:
                        raise RuntimeError(f"platform_safety_supervisor_init_failed:{exc}") from exc

                    loaded_strategies, strategy_load_failures = await self._load_enabled_strategies_after_exec_ready(
                        registry_cfg=registry_cfg,
                        mandatory_live_strategy_ids=mandatory_live_strategy_ids,
                        exec_enabled=exec_enabled,
                    )

                    # Force hot-reload of RevF-001 to pick latest params (e.g., schedule_pt overrides)
                    try:
                        if 'RevF-001' in loaded_strategies:
                            await self.strategy_engine.reload_strategy('RevF-001')
                            logger.info("   🔄 Hot-reloaded RevF-001 with latest config")
                    except Exception as exc:
                        logger.warning("   ⚠️  Hot-reload RevF-001 failed", error=str(exc))

                    if self.telegram_command_bot is not None:
                        manual_flatten = ManualFlattenService(
                            pg=self.pg,
                            execution_service=execution_service,
                            ib_adapter=self.ib_adapter_exec,
                            runtime_reconciler=self.runtime_reconciler,
                            order_event_enabled=order_event_enabled,
                        )

                        async def _telegram_flatten_handler(
                            trigger_id: str,
                            strategy_id: str | None,
                            source_trigger_id: str | None,
                        ) -> str:
                            if strategy_id:
                                flatten_result = await manual_flatten.execute_flatten_for_strategy(
                                    trigger_id=trigger_id,
                                    strategy_id=strategy_id,
                                )
                            elif source_trigger_id:
                                flatten_result = await manual_flatten.execute_flatten_for_source_trigger(
                                    trigger_id=trigger_id,
                                    source_trigger_id=source_trigger_id,
                                )
                            else:
                                flatten_result = await manual_flatten.execute_flatten_all(trigger_id=trigger_id)
                            scope_labels = {
                                "ACCOUNT_ALL": "全账户清仓",
                                "STRATEGY_ALL": "策略整仓清仓",
                                "TRIGGER_NET_CAPPED": "按触发净量清仓",
                            }
                            verify_labels = {
                                "VERIFIED_FLAT": "已验证平仓",
                                "SUBMITTED_NOT_VERIFIED": "已提交，未验证平仓",
                                "SUBMIT_FAILED": "提交失败",
                                "NO_TARGETS": "无可清仓目标",
                                "NOT_CHECKED": "未检查",
                            }
                            summary_lines = [
                                "🚨 手动清仓回执",
                                "",
                                "🧭 范围",
                                f"• scope_mode: {flatten_result.scope_mode}",
                                f"• scope_label: {scope_labels.get(flatten_result.scope_mode, flatten_result.scope_mode)}",
                                f"• target_strategy_id: {strategy_id or '-'}",
                                f"• source_trigger_id: {source_trigger_id or '-'}",
                                f"• scope_warning: {flatten_result.scope_warning or '-'}",
                                "",
                                "🆔 跟踪",
                                f"• intervention_id: {flatten_result.intervention_id}",
                                f"• trigger_id: {flatten_result.trigger_id}",
                                "",
                                "📦 目标",
                                f"• db_targets: {flatten_result.db_targets}",
                                f"• broker_fallback_targets: {flatten_result.broker_fallback_targets}",
                                "",
                                "📤 执行",
                                f"• orders_total: {flatten_result.total_orders}",
                                f"• orders_success: {flatten_result.success_orders}",
                                f"• orders_failed: {flatten_result.failed_orders}",
                                f"• position_reconcile_ran: {flatten_result.position_reconcile_ran}",
                                "",
                                "✅ 校验",
                                f"• verification_state: {flatten_result.verification_state}",
                                f"• verification_label: {verify_labels.get(flatten_result.verification_state, flatten_result.verification_state)}",
                                f"• verified_flat: {flatten_result.verified_flat}",
                                f"• verified_targets: {flatten_result.verified_targets}",
                                f"• remaining_position_targets: {flatten_result.remaining_position_targets}",
                                f"• remaining_open_orders: {flatten_result.remaining_open_orders}",
                            ]
                            for item in flatten_result.order_results:
                                item_lines = [
                                    "🔹 子订单",
                                    f"  strategy_id: {item.strategy_id}",
                                    f"  symbol: {item.symbol}",
                                    f"  qty: {item.qty}",
                                    f"  source_trigger_id: {flatten_result.source_trigger_id or '-'}",
                                    f"  success: {item.success}",
                                    f"  decision_id: {item.decision_id}",
                                    f"  order_id: {item.order_id}",
                                    f"  idem: {item.idempotency_key}",
                                    f"  stage: {item.stage}",
                                    f"  reason: {item.reason}",
                                ]
                                summary_lines.append(
                                    "\n".join(item_lines)
                                )
                            return "\n".join(summary_lines)

                        self.telegram_command_bot.set_flatten_handler(_telegram_flatten_handler)
                        logger.info("   ✅ Telegram flatten command routed to ExecutionService")

                    # Initialize HedgingService (quantitative dynamic hedging)
                    try:
                        from services.risk.hedging_service import HedgingService
                        from services.risk.order_router import SafetyKernelOrderRouter

                        hedge_order_router = SafetyKernelOrderRouter(self.safety_kernel) if hasattr(self, "safety_kernel") else None
                        self.hedging_service = HedgingService(order_router=hedge_order_router)
                        hedge_task = asyncio.create_task(self.hedging_service.start())
                        self.services.append(("hedging_service", hedge_task))
                        logger.info("   ✅ HedgingService initialized (dry_run=%s)", self.hedging_service.dry_run)
                    except Exception as exc:
                        logger.warning("   ⚠️  HedgingService initialization failed (non-critical)", error=str(exc))

                    # Initialize PerformanceGuard (automatic risk reduction on poor performance)
                    try:
                        from services.risk.performance_guard import PerformanceGuard, PerformanceGuardConfig

                        perf_guard_cfg = config.get('risk_management.performance_guard', {}) or {}
                        if perf_guard_cfg.get('enabled', False):
                            perf_config = PerformanceGuardConfig(perf_guard_cfg)
                            self.performance_guard = PerformanceGuard(perf_config)
                            await self.performance_guard.initialize()

                            # Start periodic check task
                            async def performance_guard_loop():
                                check_interval = perf_guard_cfg.get('check_interval_sec', 600)
                                while self.running and not SHUTDOWN_EVENT.is_set():
                                    try:
                                        await self.performance_guard.check_and_apply()
                                    except Exception as e:
                                        logger.error("performance_guard_check_error", error=str(e), exc_info=True)
                                    await asyncio.sleep(check_interval)

                            guard_task = asyncio.create_task(performance_guard_loop())
                            self.services.append(("performance_guard", guard_task))
                            logger.info(
                                "   ✅ PerformanceGuard initialized (window=%sd, min_sharpe=%.2f, dry_run=%s)",
                                perf_config.window_days,
                                perf_config.min_sharpe,
                                perf_config.dry_run
                            )
                        else:
                            logger.info("   ⏭️  PerformanceGuard disabled (risk_management.performance_guard.enabled=false)")
                    except Exception as exc:
                        logger.warning("   ⚠️  PerformanceGuard initialization failed (non-critical)", error=str(exc), exc_info=True)

                    logger.info(f"   ✅ Execution System ACTIVE ({trading_mode} mode)")
                else:
                    logger.info(f"   ⏭️  Execution System DISABLED (paper/live execution disabled - decisions logged only)")

            except Exception as e:
                execution_init_error = e
                logger.error("   ❌ Failed to initialize Execution System",
                           error=str(e),
                           exc_info=True)
                execution_service = None
                runtime_reconciler = None
                if self.system_health_controller is not None and exec_enabled:
                    await self.system_health_controller.publish(
                        event_type="system_halted",
                        reason="execution_system_init_failed",
                        source="run_system.start",
                        detail=str(e),
                        metadata={
                            "execution_system_ready": False,
                            "runtime_reconciler_ready": False,
                        },
                    )

            if not exec_enabled:
                loaded_strategies, strategy_load_failures = await self._load_enabled_strategies_after_exec_ready(
                    registry_cfg=registry_cfg,
                    mandatory_live_strategy_ids=mandatory_live_strategy_ids,
                    exec_enabled=exec_enabled,
                )

            # ==================================================================
            # 5b. OPTIONS QUOTES INGEST (options_quotes_1m)
            # ==================================================================
            options_quotes_cfg = config.get('services.options_quotes_ingest', {}) or {}
            auto_env_raw = os.getenv("OPTIONS_QUOTES_AUTO_START", "").strip()
            if auto_env_raw:
                options_quotes_auto = auto_env_raw.lower() in ("1", "true", "yes", "on")
            else:
                options_quotes_auto = bool(options_quotes_cfg.get("enabled", False))
            options_quotes_env_overrides = str(
                os.getenv("OPTIONS_QUOTES_USE_ENV_OVERRIDES", "")
            ).strip().lower() in ("1", "true", "yes", "on")
            options_quotes_running = False
            if options_quotes_auto:
                try:
                    from tools.ingest_options_quotes import OptionQuotesIngestService
                    from services.ibkr.client_id_registry import get_client_id_registry

                    self._ensure_option_quotes_table()
                    default_quotes_client_id = int(get_client_id_registry().resolve("options_quotes"))
                    if options_quotes_env_overrides:
                        symbols_raw = os.getenv(
                            "OPTIONS_QUOTES_SYMBOLS",
                            ",".join(options_quotes_cfg.get("symbols", ["QQQ"])),
                        )
                        dte_raw = os.getenv(
                            "OPTIONS_QUOTES_DTE",
                            ",".join(str(x) for x in options_quotes_cfg.get("dte", [0])),
                        )
                        right_raw = os.getenv(
                            "OPTIONS_QUOTES_RIGHT",
                            str(options_quotes_cfg.get("right", "") or ""),
                        )
                        provider = os.getenv("OPTIONS_QUOTES_PROVIDER", str(options_quotes_cfg.get("provider", "ibkr")))
                        poll_sec = int(os.getenv("OPTIONS_QUOTES_POLL_SEC", str(options_quotes_cfg.get("poll_sec", 60))))
                        max_contracts = int(os.getenv("OPTIONS_QUOTES_MAX_CONTRACTS", str(options_quotes_cfg.get("max_contracts", 0))))
                        ibkr_host = os.getenv("OPTIONS_QUOTES_IBKR_HOST", str(options_quotes_cfg.get("ibkr_host", ib_cfg.get('host', '127.0.0.1'))))
                        ibkr_port = int(os.getenv("OPTIONS_QUOTES_IBKR_PORT", str(options_quotes_cfg.get("ibkr_port", ib_cfg.get('port', 4002)))))
                        ibkr_client_id = int(os.getenv("OPTIONS_QUOTES_IBKR_CLIENT_ID", str(default_quotes_client_id)))
                    else:
                        symbols_raw = ",".join(options_quotes_cfg.get("symbols", ["QQQ"]))
                        dte_raw = ",".join(str(x) for x in options_quotes_cfg.get("dte", [0]))
                        right_raw = str(options_quotes_cfg.get("right", "") or "")
                        provider = str(options_quotes_cfg.get("provider", "ibkr"))
                        poll_sec = int(options_quotes_cfg.get("poll_sec", 60))
                        max_contracts = int(options_quotes_cfg.get("max_contracts", 0))
                        ibkr_host = str(options_quotes_cfg.get("ibkr_host", ib_cfg.get('host', '127.0.0.1')))
                        ibkr_port = int(options_quotes_cfg.get("ibkr_port", ib_cfg.get('port', 4002)))
                        ibkr_client_id = int(default_quotes_client_id)

                    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
                    dte = [int(x.strip()) for x in dte_raw.split(",") if x.strip()]
                    right = right_raw.strip().upper() if right_raw.strip() else None

                    self.options_quotes_service = OptionQuotesIngestService(
                        provider=provider,
                        symbols=symbols,
                        dte=dte,
                        right=right,
                        poll_sec=poll_sec,
                        max_contracts=max_contracts,
                        ibkr_host=ibkr_host,
                        ibkr_port=ibkr_port,
                        ibkr_client_id=ibkr_client_id,
                    )
                    quotes_task = asyncio.create_task(self.options_quotes_service.run())
                    self.services.append(("options_quotes_ingest", quotes_task))
                    options_quotes_running = True
                    logger.info(
                        "   ✅ Options quotes ingest service started",
                        symbols=symbols,
                        dte=dte,
                        right=right or "ALL",
                        env_overrides=options_quotes_env_overrides,
                    )
                except Exception as exc:
                    logger.warning("   ⚠️  Options quotes ingest failed to start", error=str(exc), exc_info=True)
            else:
                logger.info("   ⏭️  Options quotes ingest disabled (services.options_quotes_ingest.enabled=false)")

            # Options quotes health snapshot (ClickHouse)
            try:
                from libs.db.ch_pool import get_client as _get_ch_client
                ch = _get_ch_client()
                health_sql = """
                SELECT
                  max(ts) AS max_ts,
                  dateDiff('minute', max(ts), now()) AS age_min
                FROM trading.options_quotes_1m
                """
                rows = ch.query(health_sql)
                if rows:
                    max_ts = rows[0].get("max_ts")
                    age_min = rows[0].get("age_min")
                    logger.info("   📈 options_quotes_1m health", max_ts=str(max_ts), age_min=int(age_min or 0))
                else:
                    logger.warning("   ⚠️  options_quotes_1m health unavailable (no rows)")
            except Exception as exc:
                logger.warning("   ⚠️  options_quotes_1m health check failed", error=str(exc))

            # ==================================================================
            # 5c. OPTION ML SIGNAL + EXECUTION
            # ==================================================================
            option_ml_auto = os.getenv("OPTION_ML_AUTO_START", "").lower() in ("1", "true", "yes", "on")
            if os.getenv("OPTION_ML_EXECUTION_ENABLED", "0") == "1":
                raise RuntimeError(
                    "legacy_option_ml_execution_disabled:"
                    "OPTION_ML_EXECUTION_ENABLED direct IB path bypasses UOM"
                )
            option_ml_running = False
            if option_ml_auto:
                try:
                    from services.options.option_ml_signal_service import OptionMLSignalService
                    from tools.option_ml_exec_service import OptionMLExecService

                    option_ml_poll_sec = int(os.getenv("OPTION_ML_POLL_SEC", "60"))
                    option_ml_max_age_sec = int(os.getenv("OPTION_ML_MAX_AGE_SEC", "300"))
                    option_ml_strategy_name = os.getenv("OPTION_ML_STRATEGY_NAME", "intraday_options_long")

                    self.option_ml_signal_service = OptionMLSignalService(
                        poll_interval_sec=option_ml_poll_sec,
                        strategy_name=option_ml_strategy_name,
                    )
                    self.option_ml_exec_service = OptionMLExecService(
                        poll_sec=option_ml_poll_sec,
                        strategy_name=option_ml_strategy_name,
                        max_age_sec=option_ml_max_age_sec,
                    )

                    sig_task = asyncio.create_task(self.option_ml_signal_service.run())
                    exec_task = asyncio.create_task(self.option_ml_exec_service.run())
                    self.services.append(("option_ml_signals", sig_task))
                    self.services.append(("option_ml_exec", exec_task))
                    option_ml_running = True
                    logger.info("   ✅ Option ML signal+exec services started")
                except Exception as exc:
                    logger.warning("   ⚠️  Option ML services failed to start", error=str(exc), exc_info=True)
            else:
                logger.info("   ⏭️  Option ML services disabled (OPTION_ML_AUTO_START not set)")

            # ==================================================================
            # 6. SERVICES SUMMARY
            # ==================================================================
            market_ready = ib_manager.market_connected()
            exec_ready = ib_manager.exec_connected() if execution_service else True
            master_ready = ib_manager.master_connected() if execution_service else True
            master_truth_complete = ib_manager.master_truth_complete() if execution_service else True
            execution_required_ready = (not exec_enabled) or bool(execution_service)
            from services.production.readiness_gate import ProductionReadinessGate

            readiness = ProductionReadinessGate().evaluate(
                exec_enabled=bool(exec_enabled),
                market_ready=bool(market_ready),
                exec_ready=bool(exec_ready),
                master_ready=bool(master_ready),
                master_truth_complete=bool(master_truth_complete),
                decision_pipeline_ready=bool(decision_pipeline is not None),
                execution_service_ready=bool(execution_required_ready),
                runtime_reconciler_ready=bool(runtime_reconciler),
                runtime_reconcile_clean=bool(
                    runtime_reconcile_evidence
                    and runtime_reconcile_evidence.get("order_truth_clean")
                    and runtime_reconcile_evidence.get("position_ledger_clean")
                    and runtime_reconcile_evidence.get("active_quarantine_clean")
                ),
                runtime_reconcile_evidence=runtime_reconcile_evidence,
                eod_supervisor_ready=bool(
                    (not exec_enabled)
                    or bool(getattr(getattr(self, "platform_eod_supervisor", None), "ready", False))
                ),
                loaded_strategy_ids=loaded_strategies,
                mandatory_live_strategy_ids=getattr(self, "_phase0_live_strategy_ids", set()) or set(),
            )
            startup_ready = bool(readiness.get("production_ready"))

            logger.info("6️⃣  Decision Pipeline: ✅ INTEGRATED (ACTIVE MODE)")
            if execution_service:
                logger.info("7️⃣  Execution System: " + ("✅ ACTIVE" if exec_ready else "⚠️ DEGRADED (IB disconnected)"))
            elif exec_enabled and execution_init_error is not None:
                logger.error("7️⃣  Execution System: ❌ FAILED", error=str(execution_init_error))
            else:
                logger.info("7️⃣  Execution System: ⏭️  DISABLED")
            logger.info("8️⃣  Options Quotes: " + ("✅ ACTIVE" if options_quotes_running else "⏭️  DISABLED"))
            logger.info("9️⃣  Option ML Services: " + ("✅ ACTIVE" if option_ml_running else "⏭️  DISABLED"))

            logger.info("")
            logger.info("=" * 80)
            if startup_ready:
                logger.info("✅ ALL SERVICES STARTED - SYSTEM RUNNING", production_readiness=readiness)
            elif exec_enabled and execution_init_error is not None:
                logger.error(
                    "❌ SERVICES STARTED WITHOUT EXECUTION SYSTEM - TRADING HALTED",
                    market_ib_connected=market_ready,
                    execution_system_ready=False,
                    execution_error=str(execution_init_error),
                )
            else:
                logger.warning(
                    "⚠️ SERVICES STARTED IN NON-TRADING DEGRADED MODE",
                    startup_gate_sec=round(float(os.getenv("RUN_SYSTEM_IB_STARTUP_GATE_SEC", "90")), 1),
                    market_ib_connected=market_ready,
                    exec_ib_connected=exec_ready if execution_service else None,
                    master_ib_connected=master_ready if execution_service else None,
                    master_truth_complete=master_truth_complete if execution_service else None,
                    production_readiness=readiness,
                )
            logger.info("=" * 80)
            logger.info("")
            logger.info("📊 Active Services:")
            logger.info("   1. Market Data (IB) → Kafka (5s bars)")
            logger.info("   2. Kafka → ClickHouse bar writer")
            logger.info("   3. Feature Calculator → ClickHouse features")
            logger.info("   4. Strategy Engine → Decision Pipeline (ACTIVE MODE)")
            logger.info("   5. Decision Pipeline (Validator → Arbiter → Translator)")
            account_label = "IB Live Account" if trading_mode == "live" else "IB Paper Account"
            logger.info(f"   6-7. Risk/Execution → {account_label}")
            logger.info("   8. Options Quotes → options_quotes_1m")
            logger.info("   9. Option ML → Signals + Execution (paper/live)")
            logger.info("")
            logger.info("Press Ctrl+C to stop")
            logger.info("=" * 80)

            self.running = True

            # Keep running until interrupted
            while self.running:
                await asyncio.sleep(1)

                # Check if any service crashed (not just completed)
                for name, task in self.services:
                    if task.done():
                        try:
                            exception = task.exception()
                        except asyncio.CancelledError as exc:
                            exception = exc
                        if name == "sfs_1s_service":
                            await self._restart_sfs_1s_service(
                                reason="task_done_exception" if exception else "task_done_completed",
                                details={"error": str(exception) if exception else ""},
                            )
                            break
                        if exception:
                            # Task crashed with an error - shut down system
                            logger.error(f"❌ Service {name} crashed!")
                            logger.error(f"   Error: {exception}")
                            await self._send_system_alert(
                                status="CRITICAL",
                                reason="service_crashed",
                                service=name,
                                error=str(exception),
                                error_type=type(exception).__name__,
                                detail=f"Background service task {name} exited with an exception.",
                            )
                            self.running = False
                            break
                        else:
                            # Task completed normally (e.g., backfill finished)
                            # This is OK - don't stop the system
                            logger.info(f"ℹ️  Service {name} completed normally")
                            # Remove from monitoring list to avoid repeated logging
                            self.services = [(n, t) for n, t in self.services if t != task]
                            break
                else:
                    await self._maybe_recover_sfs_1s_service()

        except Exception as e:
            logger.error(f"❌ System startup failed: {e}", exc_info=True)
            await self._send_system_alert(
                status="CRITICAL",
                reason="system_startup_failed",
                service="run_system.start",
                error=str(e),
                error_type=type(e).__name__,
                detail="System startup aborted before reaching steady state.",
            )
            self.running = False

    def trigger_operator_ib_restart_drill(self, *, reason: str = "operator_signal") -> bool:
        task = getattr(self, "_operator_ib_restart_drill_task", None)
        if task is not None and not task.done():
            logger.warning("operator_ib_restart_drill_already_running", reason=reason)
            return False
        self._operator_ib_restart_drill_task = asyncio.create_task(
            self._run_operator_ib_restart_drill(reason=reason),
            name="operator-paper-ib-restart-drill",
        )
        return True

    async def _run_operator_ib_restart_drill(self, *, reason: str) -> None:
        """Emit a current-process paper IB restart drill through the real audit and health paths."""
        drill_started_at = datetime.now(ZoneInfo("America/New_York"))
        drill_start_ts = drill_started_at.isoformat()
        logger.warning("operator_ib_restart_drill_started", reason=reason, drill_start_ts=drill_start_ts)

        def emit_audit(*, domain: str, component: str, stage: str, status: str, payload: Dict[str, Any]) -> None:
            writer = getattr(self, "audit_writer", None)
            if writer is None:
                return
            try:
                writer.emit(
                    domain=domain,
                    component=component,
                    stage=stage,
                    status=status,
                    reason="operator_paper_ib_restart_drill",
                    payload={
                        **payload,
                        "drill_mode": "operator_paper",
                        "drill_reason": reason,
                        "drill_start_ts": drill_start_ts,
                    },
                )
            except Exception as exc:
                logger.warning("operator_ib_restart_drill_audit_emit_failed", stage=stage, error=str(exc))

        emit_audit(
            domain="system",
            component="market_data_service",
            stage="ib_disconnected",
            status="disconnected",
            payload={"service": "market_data_service", "simulated": True},
        )
        if self.system_health_controller is not None:
            await self.system_health_controller.publish(
                event_type="ib_disconnected",
                reason="operator_paper_ib_restart_drill",
                source="run_system.operator_drill",
                detail=f"reason={reason}",
                metadata={"drill_mode": "operator_paper", "drill_start_ts": drill_start_ts},
            )
        await asyncio.sleep(1.0)

        emit_audit(
            domain="system",
            component="market_data_service",
            stage="ib_connected",
            status="connected",
            payload={"service": "market_data_service", "simulated": True},
        )
        if self.system_health_controller is not None:
            await self.system_health_controller.publish(
                event_type="ib_connected",
                reason="operator_paper_ib_restart_drill_reconnected",
                source="run_system.operator_drill",
                detail=f"reason={reason}",
                metadata={"drill_mode": "operator_paper", "drill_start_ts": drill_start_ts},
            )

        runtime_reconcile_evidence: Dict[str, Any] = {
            "broker_connected": True,
            "broker_truth_complete": False,
            "order_truth_clean": False,
            "position_ledger_clean": False,
            "active_quarantine_clean": False,
            "active_quarantine_evidence_present": False,
            "strategies_paused_ack": True,
            "ghosts_found": 0,
            "orphans_found": 0,
            "mismatches": 0,
            "position_mismatches": 0,
            "active_quarantine_count": 0,
        }
        if getattr(self, "runtime_reconciler", None) is not None:
            try:
                startup_result = await self.runtime_reconciler.ensure_startup_ready()
                runtime_reconcile_evidence = self._runtime_reconciliation_resume_metadata(startup_result)
            except Exception as exc:
                logger.error("operator_ib_restart_drill_reconcile_failed", error=str(exc), exc_info=True)

        try:
            from services.production.recovery_readiness import get_recovery_readiness_service

            recovery_readiness = get_recovery_readiness_service().snapshot(
                required_components=[
                    "market_data_service",
                    "market_data_ib",
                    "market_data_subscriptions",
                    "bar_pipeline",
                    "tick_producer",
                ]
            )
        except Exception as exc:
            recovery_readiness = {
                "ready": False,
                "blockers": ["recovery_readiness_snapshot_failed"],
                "error": str(exc),
            }
        runtime_reconcile_evidence["recovery_readiness"] = recovery_readiness
        runtime_reconcile_evidence["platform_recovery_ready"] = bool(recovery_readiness.get("ready"))
        runtime_reconcile_evidence["drill_mode"] = "operator_paper"
        runtime_reconcile_evidence["drill_start_ts"] = drill_start_ts

        emit_audit(
            domain="recon",
            component="runtime_reconciler",
            stage="position_audit_snapshot",
            status="ok",
            payload={
                "ghosts": int(runtime_reconcile_evidence.get("ghosts_found") or 0),
                "orphans_unresolved": int(runtime_reconcile_evidence.get("orphans_found") or 0),
                "mismatches": int(runtime_reconcile_evidence.get("mismatches") or 0),
                "position_mismatches": int(runtime_reconcile_evidence.get("position_mismatches") or 0),
                "active_quarantine_count": int(runtime_reconcile_evidence.get("active_quarantine_count") or 0),
            },
        )
        if self.system_health_controller is not None:
            await self.system_health_controller.publish(
                event_type="system_resumed",
                reason="operator_paper_ib_restart_drill_verified",
                source="run_system.operator_drill",
                metadata=runtime_reconcile_evidence,
            )

        async def build_drill_artifact(*, stage: str) -> Dict[str, Any]:
            from services.production.ib_restart_drill_artifact import build_ib_restart_drill_artifact

            artifact = build_ib_restart_drill_artifact(
                repo_root=Path(__file__).parent,
                start_ts=drill_start_ts,
                drill_id="paper_ib_restart_latest",
            )
            logger.warning(
                "operator_ib_restart_drill_artifact_built",
                stage=stage,
                go_no_go=artifact.get("go_no_go"),
                blockers=list(artifact.get("blockers") or []),
                drill_start_ts=drill_start_ts,
            )
            return artifact

        try:
            artifact = await build_drill_artifact(stage="post_resume")
            blockers = {str(item) for item in (artifact.get("blockers") or [])}
            if "post_resume_stable_window_not_met" in blockers:
                try:
                    stable_window_sec = max(
                        30.0,
                        float(os.getenv("IB_RESTART_DRILL_STABLE_WINDOW_SEC", "120")),
                    )
                except Exception:
                    stable_window_sec = 120.0
                elapsed_since_resume = 0.0
                flapping = dict(artifact.get("flapping_thresholds") or {})
                try:
                    elapsed_since_resume = float(flapping.get("stable_window_observed_sec") or 0.0)
                except Exception:
                    elapsed_since_resume = 0.0
                wait_sec = max(0.0, stable_window_sec - elapsed_since_resume + 1.0)
                logger.warning(
                    "operator_ib_restart_drill_waiting_for_stable_window",
                    wait_sec=round(wait_sec, 3),
                    stable_window_sec=stable_window_sec,
                    observed_sec=round(elapsed_since_resume, 3),
                    drill_start_ts=drill_start_ts,
                )
                await asyncio.sleep(wait_sec)
                await build_drill_artifact(stage="post_stable_window")
        except Exception as exc:
            logger.error("operator_ib_restart_drill_artifact_build_failed", error=str(exc), exc_info=True)
        logger.warning("operator_ib_restart_drill_completed", reason=reason, drill_start_ts=drill_start_ts)

    async def stop(self):
        """Stop all services"""
        logger.info("")
        logger.info("=" * 80)
        logger.info("🛑 STOPPING TRADING SYSTEM")
        logger.info("=" * 80)

        # Set global shutdown event first to signal all services
        SHUTDOWN_EVENT.set()
        logger.info("   📡 Shutdown signal sent to all services")
        await asyncio.sleep(0.5)  # Give services a moment to start cleanup

        # Stop individual services with timeout (longer timeout for Kafka-based services)
        async def stop_with_timeout(coro, name, timeout=20.0):
            try:
                await asyncio.wait_for(coro, timeout=timeout)
                logger.info(f"   ✅ {name} stopped")
            except asyncio.TimeoutError:
                logger.warning(f"   ⚠️  {name} stop timed out after {timeout}s - forcing shutdown")
            except Exception as e:
                logger.warning(f"   ⚠️  {name} stop error: {e}")

        # Stop individual services
        if getattr(self, 'telegram_command_bot', None):
            await stop_with_timeout(self.telegram_command_bot.stop(), "telegram_command_bot")

        if hasattr(self, 'market_data'):
            await stop_with_timeout(self.market_data.stop(), "market_data")

        # Stop Catchup Services
        if hasattr(self, '_catchups'):
            for i, catchup in enumerate(self._catchups):
                await stop_with_timeout(catchup.stop(), f"catchup_{i}")

        # Stop Backfill Services
        if hasattr(self, '_backfills'):
            for i, svc in enumerate(self._backfills):
                await stop_with_timeout(svc.stop(), f"backfill_{i}")

        # Stop all CH writers
        if hasattr(self, '_ch_writers'):
            for i, writer in enumerate(self._ch_writers):
                await stop_with_timeout(writer.stop(), f"ch_writer_{i}")

        # Stop all lake writers
        if hasattr(self, '_lake_writers'):
            for i, writer in enumerate(self._lake_writers):
                await stop_with_timeout(writer.stop(), f"lake_writer_{i}")

        # Legacy single writer fallback
        if hasattr(self, 'ch_writer') and self.ch_writer not in self._ch_writers:
            await stop_with_timeout(self.ch_writer.stop(), "ch_writer_legacy")

        # Stop RFE fallback supervisor and dynamic 1m calc
        if getattr(self, '_rfe_fallback_task', None):
            self._rfe_fallback_task.cancel()
            try:
                await asyncio.wait_for(self._rfe_fallback_task, timeout=3.0)
            except Exception:
                pass
        if getattr(self, 'feature_calc_1m_fallback', None):
            await stop_with_timeout(self.feature_calc_1m_fallback.stop(), "feature_calc_1m_fallback")

        if hasattr(self, 'feature_calc_5s'):
            await stop_with_timeout(self.feature_calc_5s.stop(), "feature_calc_5s")

        if hasattr(self, 'strategy_engine'):
            await stop_with_timeout(self.strategy_engine.shutdown(), "strategy_engine")

        if hasattr(self, 'option_ml_signal_service'):
            await stop_with_timeout(self.option_ml_signal_service.stop(), "option_ml_signal_service")

        if hasattr(self, 'option_ml_exec_service'):
            await stop_with_timeout(self.option_ml_exec_service.stop(), "option_ml_exec_service")

        if hasattr(self, 'options_quotes_service'):
            await stop_with_timeout(self.options_quotes_service.stop(), "options_quotes_service")

        if hasattr(self, 'platform_eod_supervisor') and self.platform_eod_supervisor:
            await stop_with_timeout(self.platform_eod_supervisor.stop(), "platform_eod_supervisor")
        if hasattr(self, 'session_end_supervisor') and self.session_end_supervisor:
            await stop_with_timeout(self.session_end_supervisor.stop(), "session_end_supervisor")
        if hasattr(self, 'position_safety_supervisor') and self.position_safety_supervisor:
            await stop_with_timeout(self.position_safety_supervisor.stop(), "position_safety_supervisor")
        if hasattr(self, 'production_state_emitter') and self.production_state_emitter:
            await stop_with_timeout(self.production_state_emitter.stop(), "production_state_emitter")

        # Stop SFS-1s Service
        if getattr(self, 'sfs_1s_service', None) is not None or getattr(self, '_sfs_1s_task', None) is not None:
            await stop_with_timeout(self._stop_sfs_1s_service(), "sfs_1s_service")

        # Stop SFS-5s Writer
        if hasattr(self, 'sfs_5s_writer'):
            await stop_with_timeout(self.sfs_5s_writer.stop(), "sfs_5s_writer")

        # Stop execution system components
        # Stop IB adapter FIRST to ensure clean disconnection before event loop closes
        if hasattr(self, 'ib_adapter_exec') and self.ib_adapter_exec:
            await stop_with_timeout(self.ib_adapter_exec.stop(), "ib_adapter_exec")

        if hasattr(self, 'runtime_reconciler') and self.runtime_reconciler:
            await stop_with_timeout(self.runtime_reconciler.stop(), "runtime_reconciler")

        if hasattr(self, 'hedging_service') and self.hedging_service:
            await stop_with_timeout(self.hedging_service.stop(), "hedging_service")

        if hasattr(self, 'safety_kernel') and self.safety_kernel:
            await stop_with_timeout(self.safety_kernel.stop(), "safety_kernel")

        # Shutdown shared IB sessions before tearing down observability/network clients.
        try:
            from services.ibkr.ib_connection_manager import get_ib_connection_manager
            await stop_with_timeout(get_ib_connection_manager().shutdown(), "ib_connection_manager")
        except Exception:
            pass

        if getattr(self, "notification_manager", None) is not None:
            await stop_with_timeout(self.notification_manager.shutdown(), "notification_manager")

        # Stop observability infrastructure
        if getattr(self, "audit_writer", None) is not None:
            try:
                self.audit_writer.emit(
                    domain="system",
                    component="run_system",
                    stage="service_stopped",
                    status="stopping",
                    payload={"service": "local_audit_writer"},
                )
            except Exception:
                pass
            await stop_with_timeout(self.audit_writer.stop(), "local_audit_writer")
        global obs_queue
        if obs_queue:
            await stop_with_timeout(obs_queue.stop_flushers(), "obs_queue_flushers")

        if hasattr(self, 'obs_loki_writer') and self.obs_loki_writer:
            await stop_with_timeout(self.obs_loki_writer.close(), "obs_loki_writer")

        if hasattr(self, 'obs_ch_writer') and self.obs_ch_writer:
            await stop_with_timeout(self.obs_ch_writer.close(), "obs_ch_writer")

        # Give aiohttp time to cleanup sessions (prevents "Unclosed client session" warnings)
        await asyncio.sleep(0.25)

        # Note: ClickHouse client uses global connection pool - no close needed

        # Close PostgreSQL connection pool
        if hasattr(self, 'pg') and self.pg:
            await stop_with_timeout(self.pg.close(), "postgresql_pool")

        # Mark service as down in Prometheus
        try:
            mark_service_up("TradingSystem", False)
        except Exception:
            pass

        # Cancel any remaining tasks as a final cleanup
        for name, task in self.services:
            if not task.done():
                logger.debug(f"   Cancelling remaining task: {name}")
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=2.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
                except Exception:
                    pass

        self._price_cache_tasks = []
        self._price_cache_consumers = []

        logger.info("")
        logger.info("✅ SYSTEM SHUTDOWN COMPLETE")
        logger.info("=" * 80)

    async def _rfe_fallback_supervisor(self):
        """Monitor RFE heartbeat; during RTH, if stalled across most symbols, start 1m calc as fallback.

        Heartbeat source: runs/ai_trader/<date>/rfe_heartbeat.json (written per RFE emit by StrategyContext).
        Config (defaults): policies.features.rfe_fallback.{enabled=true, stale_sec=15, symbols_pct_threshold=0.7}
        """
        from pathlib import Path as _P
        import json as _J
        from services.market.features.feature_calculator import FeatureCalculator

        while not SHUTDOWN_EVENT.is_set():
            try:
                now_et = datetime.now(ZoneInfo("America/New_York"))
                is_weekend = now_et.weekday() >= 5
                is_rth = (not is_weekend) and (_time(9, 30) <= now_et.time() <= _time(16, 0))

                # Config
                try:
                    fcfg = (config.get('policies') or {}).get('features', {}) or {}
                    fb = fcfg.get('rfe_fallback', {}) or {}
                    enabled = bool(fb.get('enabled', True))
                    stale_sec = int(fb.get('stale_sec', 15))
                    pct_thr = float(fb.get('symbols_pct_threshold', 0.7))
                except Exception:
                    enabled = True
                    stale_sec = 15
                    pct_thr = 0.7

                if not enabled or not is_rth:
                    # If fallback running but not needed, stop it
                    if self.feature_calc_1m_fallback is not None:
                        await self.feature_calc_1m_fallback.stop()
                        self.feature_calc_1m_fallback = None
                        logger.info("   🔻 Stopped 1m fallback (not needed)")
                    await asyncio.sleep(5.0)
                    continue

                # Read heartbeat
                dstr = now_et.date().isoformat()
                hb = _P("runs/ai_trader") / dstr / "rfe_heartbeat.json"
                stale_frac = 1.0
                total = 0
                stale = 0
                if hb.exists():
                    try:
                        data = _J.loads(hb.read_text("utf-8")) or {}
                        last = data.get('symbols') or {}
                        total = len(last)
                        now_us = int(datetime.now(ZoneInfo("UTC")).timestamp() * 1_000_000)
                        for sym, ts_us in last.items():
                            try:
                                age_s = max(0, (now_us - int(ts_us)) / 1_000_000.0)
                                if age_s > stale_sec:
                                    stale += 1
                            except Exception:
                                stale += 1
                        stale_frac = (stale / total) if total > 0 else 1.0
                    except Exception:
                        stale_frac = 1.0

                # Start/stop fallback based on threshold
                if total > 0 and stale_frac >= pct_thr:
                    if self.feature_calc_1m_fallback is None:
                        try:
                            self.feature_calc_1m_fallback = FeatureCalculator(
                                resolution="1m",
                                horizon_minutes=180,
                                waterline_delay_sec=5,
                                poll_interval_sec=10.0,
                            )
                            await self.feature_calc_1m_fallback.start()
                            logger.warning(
                                "   🚨 RFE stall detected — starting 1m fallback",
                                stale_frac=round(stale_frac, 2),
                                total=total,
                                stale=stale
                            )
                        except Exception as exc:
                            logger.error("   ❌ Failed to start 1m fallback", error=str(exc))
                else:
                    if self.feature_calc_1m_fallback is not None:
                        await self.feature_calc_1m_fallback.stop()
                        self.feature_calc_1m_fallback = None
                        logger.info("   🔻 RFE recovered — stopped 1m fallback")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("rfe_fallback_supervisor_error", error=str(exc))
            finally:
                await asyncio.sleep(5.0)


def _enabled_strategy_configs() -> List[Dict[str, Any]]:
    strategies = config.get("strategies", []) or []
    if not isinstance(strategies, list):
        raise RuntimeError("config_strategies_not_list")
    return [dict(item) for item in strategies if isinstance(item, dict) and bool(item.get("enabled"))]


def _assert_no_shadow_enabled_strategies(enabled_strategies: List[Dict[str, Any]]) -> None:
    def _walk(value: Any, *, path: str) -> list[tuple[str, str]]:
        if isinstance(value, dict):
            found: list[tuple[str, str]] = []
            for key, item in value.items():
                child_path = f"{path}.{key}" if path else str(key)
                found.extend(_walk(item, path=child_path))
            return found
        if isinstance(value, list):
            found = []
            for index, item in enumerate(value):
                found.extend(_walk(item, path=f"{path}[{index}]"))
            return found
        if isinstance(value, str) and "shadow" in value.strip().lower():
            return [(path, value)]
        return []

    offenders: list[dict[str, str]] = []
    for spec in enabled_strategies:
        strategy_id = str(spec.get("id") or "").strip()
        for field, value in _walk(spec, path="strategy"):
            offenders.append({"strategy_id": strategy_id or "<missing>", "field": field, "value": value})
    if offenders:
        raise RuntimeError("shadow_strategy_enabled_forbidden:" + json.dumps(offenders, sort_keys=True))


def _strategy_params_for_startup(strategy_spec: Dict[str, Any]) -> Dict[str, Any]:
    params = copy.deepcopy(strategy_spec.get("params") or {})
    params.setdefault("extras", {})
    capital_limit = strategy_spec.get("capital_limit_usd")
    if capital_limit is not None:
        cap_val = float(capital_limit)
        params.setdefault("max_notional_usd", cap_val)
        params.setdefault("per_symbol_max_usd", cap_val)
        params.setdefault("capital_limit_usd", cap_val)
        params["extras"].setdefault("capital_limit_usd", cap_val)
    return params


def _register_strategy_spec_for_startup(
    strategy_spec: Dict[str, Any],
    registry_cfg: Dict[str, Any],
) -> tuple[str, str]:
    strategy_id = str(strategy_spec.get("id") or "").strip()
    module_name = str(strategy_spec.get("module") or "").strip()
    class_name = str(strategy_spec.get("class") or "").strip()
    if not strategy_id or not module_name or not class_name:
        raise RuntimeError(f"enabled_strategy_missing_identity:{strategy_spec}")

    module_path = f"{module_name}.{class_name}"
    strategy_name = None
    for name, path in (registry_cfg or {}).items():
        if str(path) == module_path:
            strategy_name = str(name)
            break

    if not strategy_name:
        strategy_name = str(strategy_spec.get("registry_name") or strategy_id)

    cls = getattr(import_module(module_name), class_name)
    registry = get_registry()
    if registry.get(strategy_name) is None:
        registry.register(strategy_name, cls)
    return strategy_id, strategy_name


async def _dry_load_enabled_strategies_for_smoke(
    enabled_strategies: List[Dict[str, Any]],
    *,
    registry_cfg: Dict[str, Any],
) -> Dict[str, Any]:
    from services.strategy.engine import StrategyEngine

    engine = StrategyEngine(dry_run=True)
    loaded: List[str] = []
    failures: List[Dict[str, str]] = []
    for strategy_spec in enabled_strategies:
        strategy_id = str(strategy_spec.get("id") or "").strip() or "<missing>"
        try:
            strategy_id, strategy_name = _register_strategy_spec_for_startup(strategy_spec, registry_cfg)
            params = _strategy_params_for_startup(strategy_spec)
            await engine.load_strategy(strategy_id=strategy_id, name=strategy_name, params=params)
            loaded.append(strategy_id)
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "strategy_id": strategy_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return {
        "loaded_strategy_ids": loaded,
        "loaded_count": len(loaded),
        "failed_count": len(failures),
        "failures": failures,
        "engine_strategy_count": len(getattr(engine, "_strategies", {}) or {}),
    }


def _load_phase0_baseline_for_smoke() -> Dict[str, Any] | None:
    freeze_cfg = config.get("phase0.freeze_gate", {}) or {}
    baseline_path = Path(
        str(
            os.getenv("PHASE0_FREEZE_BASELINE_PATH", "")
            or freeze_cfg.get("baseline_path")
            or "reports/strategy_surface/live_strategy_surface_freeze_baseline.json"
        )
    )
    if not baseline_path.exists():
        return None
    return json.loads(baseline_path.read_text(encoding="utf-8"))


def run_startup_smoke_check() -> Dict[str, Any]:
    """Validate production bootstrap wiring without connecting to external services."""
    checks: List[Dict[str, Any]] = []

    def record(name: str, *, ok: bool = True, **metadata: Any) -> None:
        checks.append({"name": name, "ok": bool(ok), **metadata})

    try:
        system = RealTradingSystem()
        record("real_trading_system_instantiates")

        enabled_strategies = _enabled_strategy_configs()
        _assert_no_shadow_enabled_strategies(enabled_strategies)
        imported_strategy_ids: List[str] = []
        for strategy_cfg in enabled_strategies:
            strategy_id = str(strategy_cfg.get("id") or "").strip()
            module_name = str(strategy_cfg.get("module") or "").strip()
            class_name = str(strategy_cfg.get("class") or "").strip()
            if not strategy_id or not module_name or not class_name:
                raise RuntimeError(f"enabled_strategy_missing_identity:{strategy_cfg}")
            module = import_module(module_name)
            getattr(module, class_name)
            imported_strategy_ids.append(strategy_id)
        record("enabled_strategy_modules_import", count=len(imported_strategy_ids), strategy_ids=imported_strategy_ids)

        report = build_live_strategy_surface_report("config.yml", repo_root=Path(__file__).parent)
        baseline = _load_phase0_baseline_for_smoke()
        freeze_cfg = config.get("phase0.freeze_gate", {}) or {}
        phase0_gate = evaluate_phase0_freeze_gate(
            report,
            baseline=baseline,
            require_baseline=bool(freeze_cfg.get("require_baseline", True)),
        )
        if not bool(phase0_gate.get("ok")):
            raise RuntimeError(
                "phase0_freeze_gate_blocked:"
                + ",".join(str(x) for x in (phase0_gate.get("blocking_reasons") or []))
            )
        system._phase0_live_strategy_ids = {
            str(item).strip()
            for item in (phase0_gate.get("current_live_strategy_ids") or [])
            if str(item).strip()
        }
        system._phase0_blocked_strategy_ids = {
            str(item).strip()
            for item in (phase0_gate.get("current_forbidden_strategy_ids") or [])
            if str(item).strip()
        }
        record(
            "phase0_freeze_gate",
            live_strategy_ids=sorted(system._phase0_live_strategy_ids),
            blocked_strategy_ids=sorted(system._phase0_blocked_strategy_ids),
        )

        registry_cfg = config.get("strategy_registry", {}) or {}
        dry_load_report = asyncio.run(
            _dry_load_enabled_strategies_for_smoke(
                enabled_strategies,
                registry_cfg=registry_cfg,
            )
        )
        loaded_strategy_ids = set(dry_load_report.get("loaded_strategy_ids") or [])
        required_live_ids = set(system._phase0_live_strategy_ids or set())
        missing_live_ids = sorted(required_live_ids - loaded_strategy_ids)
        if dry_load_report.get("failed_count") or missing_live_ids:
            raise RuntimeError(
                "strategy_dry_load_failed:"
                + json.dumps(
                    {
                        "missing_live_strategy_ids": missing_live_ids,
                        "failures": dry_load_report.get("failures") or [],
                    },
                    sort_keys=True,
                )
            )
        record(
            "strategy_engine_dry_load",
            enabled_count=len(enabled_strategies),
            live_strategy_ids=sorted(required_live_ids),
            **dry_load_report,
        )

        from services.production.eod_supervisor import session_close_policies_from_config

        session_close_policies = session_close_policies_from_config(getattr(config, "_config", {}) or {})
        if not session_close_policies:
            raise RuntimeError("scoped_eod_session_close_policies_missing")
        record(
            "scoped_eod_session_close_policies",
            policy_count=len(session_close_policies),
            selected_for_flatten_count=sum(1 for policy in session_close_policies if policy.should_flatten()),
            extended_policy_count=sum(1 for policy in session_close_policies if policy.policy_type == "EXTENDED_EQUITY"),
            strategy_ids=[policy.strategy_id for policy in session_close_policies],
        )

        uom_report = build_uom_caller_surface_report(Path(__file__).parent)
        uom_gate = evaluate_uom_caller_gate(uom_report)
        if not bool(uom_gate.get("ok")):
            raise RuntimeError(
                "uom_caller_gate_blocked:" + ",".join(str(x) for x in (uom_gate.get("blocking_files") or []))
            )
        record("uom_caller_gate", summary=dict(uom_gate.get("summary") or {}))

        from services.orders.manager import UnifiedOrderManager
        from services.reconciliation.runtime_reconciler import RuntimeReconciler
        from services.reconciliation.strategy_recovery_policy import (
            QQQIntradayRecoveryPolicy,
            StrategyRecoveryPolicyRegistry,
        )

        health = SystemHealthController(alert_sink=None)
        record("system_health_controller_instantiates", state=str(getattr(health, "state", "")))

        class _SmokeExecutionService:
            pg = None

            async def execute(self, result: Any) -> Any:
                return result

        UnifiedOrderManager(execution_service=_SmokeExecutionService(), system_health_controller=health)
        record("unified_order_manager_instantiates")

        registry = StrategyRecoveryPolicyRegistry()
        registry.register("qqq_intraday_001", QQQIntradayRecoveryPolicy())
        RuntimeReconciler(pg=None, ib_adapter=None, strategy_recovery_policy_registry=registry)
        record("runtime_reconciler_instantiates")

        class _ReadyIB:
            @staticmethod
            def market_connected() -> bool:
                return True

            @staticmethod
            def exec_connected() -> bool:
                return True

            @staticmethod
            def master_connected() -> bool:
                return True

            @staticmethod
            def master_truth_complete() -> bool:
                return True

        snapshot = RealTradingSystem._ib_startup_status_snapshot(_ReadyIB(), exec_enabled=True)
        if not bool(snapshot.get("ready")):
            raise RuntimeError(f"ib_startup_snapshot_not_ready:{snapshot}")
        record("ib_startup_status_snapshot", snapshot=snapshot)

        return {"ok": True, "checks": checks}
    except Exception as exc:
        record("startup_smoke_failed", ok=False, error=str(exc), error_type=type(exc).__name__)
        return {"ok": False, "checks": checks, "error": str(exc), "error_type": type(exc).__name__}


async def main():
    """Main entry point"""
    system = RealTradingSystem()
    force_exit = False

    # Signal handlers for graceful shutdown
    def signal_handler(sig):
        nonlocal force_exit
        logger.info(f"Received signal {sig}")

        if not SHUTDOWN_EVENT.is_set():
            # First Ctrl+C: initiate graceful shutdown
            logger.info("Initiating graceful shutdown... (Press Ctrl+C again to force exit)")
            SHUTDOWN_EVENT.set()
            system.running = False
        else:
            # Second Ctrl+C: force exit
            force_exit = True
            logger.warning("Force exit requested!")
            import os
            os._exit(1)

    def drill_signal_handler(sig):
        logger.warning(f"Received operator IB restart drill signal {sig}")
        started = system.trigger_operator_ib_restart_drill(reason=f"signal:{sig}")
        if not started:
            logger.warning("Operator IB restart drill signal ignored because a drill is already running")

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda s=sig: signal_handler(s))
    if hasattr(signal, "SIGUSR1"):
        loop.add_signal_handler(signal.SIGUSR1, lambda: drill_signal_handler(signal.SIGUSR1))

    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        SHUTDOWN_EVENT.set()
        system.running = False
    finally:
        if not force_exit:
            await system.stop()


if __name__ == "__main__":
    if os.getenv("RUN_SYSTEM_SMOKE_CHECK", "").lower() in ("1", "true", "yes", "on"):
        result = run_startup_smoke_check()
        print(_SMOKE_RESULT_PREFIX + json.dumps(result, ensure_ascii=False, sort_keys=True))
        sys.exit(0 if bool(result.get("ok")) else 1)
    if not _acquire_run_system_lock():
        logger.error("run_system_single_instance_lock_failed", lock_path=str(_run_system_lock_path()))
        sys.exit(1)
    # Enhanced cleanup for asyncio event loop (Python 3.13+)
    try:
        # asyncio.run() in Python 3.13+ handles loop creation/cleanup internally
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("\nKeyboard interrupt - Shutdown initiated")
    except asyncio.CancelledError:
        logger.info("Main task was cancelled")
    finally:
        _release_run_system_lock()
        # Ensure proper cleanup messaging
        logger.info("=" * 80)
        logger.info("✅ System shutdown complete")
        logger.info("=" * 80)
