from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from libs.logger import get_logger
from libs.obs.audit_schema import build_audit_record

logger = get_logger(__name__)
ET = ZoneInfo("America/New_York")


class LocalAuditWriter:
    def __init__(
        self,
        *,
        root_dir: str | Path = "runtime/audit",
        queue_size: int = 20000,
        flush_interval_sec: float = 0.5,
        batch_size: int = 500,
        enabled: bool = True,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser()
        self.queue_size = max(100, int(queue_size))
        self.flush_interval_sec = max(0.1, float(flush_interval_sec))
        self.batch_size = max(1, int(batch_size))
        self.enabled = bool(enabled)
        self._queue: asyncio.Queue[Optional[Dict[str, Any]]] = asyncio.Queue(maxsize=self.queue_size)
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if (not self.enabled) or self._running:
            return
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._running = True
        self._task = asyncio.create_task(self._run(), name="local_audit_writer")
        logger.info(
            "local_audit_writer_started",
            root_dir=str(self.root_dir),
            queue_size=self.queue_size,
            flush_interval_sec=self.flush_interval_sec,
            batch_size=self.batch_size,
        )

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass
        if self._task is not None:
            await self._task
            self._task = None
        logger.info("local_audit_writer_stopped", root_dir=str(self.root_dir))

    def emit(
        self,
        *,
        domain: str,
        stage: str,
        severity: str = "info",
        component: Optional[str] = None,
        ts: Optional[datetime] = None,
        **fields: Any,
    ) -> bool:
        if not self.enabled:
            return False
        record = build_audit_record(
            domain=domain,
            stage=stage,
            severity=severity,
            component=component,
            ts=ts,
            strategy_id=fields.pop("strategy_id", None),
            strategy_run_id=fields.pop("strategy_run_id", None),
            trigger_id=fields.pop("trigger_id", None),
            decision_id=fields.pop("decision_id", None),
            trace_id=fields.pop("trace_id", None),
            causation_id=fields.pop("causation_id", None),
            intent_id=fields.pop("intent_id", None),
            order_id=fields.pop("order_id", None),
            broker_order_id=fields.pop("broker_order_id", None),
            perm_id=fields.pop("perm_id", None),
            symbol=fields.pop("symbol", None),
            status=fields.pop("status", None),
            reason=fields.pop("reason", None),
            payload=fields.pop("payload", None),
        )
        if fields:
            payload = dict(record.get("payload") or {})
            payload.update(fields)
            record["payload"] = payload
        try:
            self._queue.put_nowait(record)
            return True
        except asyncio.QueueFull:
            logger.warning(
                "local_audit_queue_full",
                domain=record.get("domain"),
                stage=record.get("stage"),
                strategy_id=record.get("strategy_id"),
                trace_id=record.get("trace_id"),
                queue_size=self.queue_size,
            )
            return False

    async def _run(self) -> None:
        pending: List[Dict[str, Any]] = []
        while self._running or pending or (not self._queue.empty()):
            try:
                timeout = self.flush_interval_sec if pending else None
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
                if item is None:
                    self._queue.task_done()
                    continue
                pending.append(item)
                self._queue.task_done()
                while len(pending) < self.batch_size:
                    try:
                        nxt = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nxt is not None:
                        pending.append(nxt)
                    self._queue.task_done()
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("local_audit_writer_loop_error", error=str(exc))

            if pending:
                batch = pending
                pending = []
                try:
                    await asyncio.to_thread(self._write_batch_sync, batch)
                except Exception as exc:
                    logger.warning("local_audit_write_failed", error=str(exc), batch_size=len(batch))

    def _write_batch_sync(self, batch: List[Dict[str, Any]]) -> None:
        grouped: Dict[Path, List[str]] = defaultdict(list)
        for record in batch:
            grouped[self._resolve_path(record)].append(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        for path, lines in grouped.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.writelines(lines)

    def _resolve_path(self, record: Dict[str, Any]) -> Path:
        try:
            ts = datetime.fromisoformat(str(record.get("ts")))
        except Exception:
            ts = datetime.now(ET)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        else:
            ts = ts.astimezone(ET)
        day = ts.strftime("%Y-%m-%d")
        hour = ts.strftime("%H")
        domain = str(record.get("domain") or "system").strip().lower() or "system"
        return self.root_dir / day / f"{domain}-{hour}.jsonl"
