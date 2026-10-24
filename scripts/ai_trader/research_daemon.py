#!/usr/bin/env python3
from __future__ import annotations

"""
Event-driven research/self-evolution daemon.

- Listens to research triggers from Kafka (event_bus.topics.research) or local queue file.
- Runs research cycle via scripts.ai_trader.research.agent.run_research_cycle_once.
- Publishes outcomes back to the event bus, writes memory_fabric, and appends audit logs.
"""

import argparse
import asyncio
import contextlib
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import boot_logging  # noqa: F401  # Ensure logging config is loaded
from libs import event_bus, metrics
from libs.config import config
from libs.logger import get_logger
from scripts.ai_trader.research.agent import run_research_cycle_once

try:
    from scripts.ai_trader.memory.memory_fabric import write_memory  # type: ignore
except Exception:  # pragma: no cover - optional dependency fallback
    write_memory = None  # type: ignore

LOG = get_logger("research_daemon")


class ResearchDaemon:
    def __init__(self, runs_dir: Path, poll_interval_sec: int = 900):
        self.runs_dir = runs_dir
        self.poll_interval_sec = poll_interval_sec
        self._event_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._consumer_task: Optional[asyncio.Task] = None
        self._stop = False

    def _ingest_local_triggers(self) -> None:
        """Load queued triggers from runs/ai_trader/queue/research_triggers.jsonl (best-effort)."""
        queue_file = self.runs_dir / "queue" / "research_triggers.jsonl"
        if not queue_file.exists():
            return
        try:
            lines = queue_file.read_text(encoding="utf-8").splitlines()
            queue_file.unlink(missing_ok=True)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("research_trigger_load_failed", error=str(exc))
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except Exception:
                continue
            evt.setdefault("source", "file")
            self._event_queue.put_nowait(evt)

    async def _start_kafka_consumer(self) -> bool:
        """Start Kafka consumer for research topic if aiokafka is available."""
        try:
            from aiokafka import AIOKafkaConsumer  # type: ignore
        except Exception:
            LOG.info("kafka_consumer_disabled", reason="aiokafka_not_available")
            return False

        kc = config.kafka_config
        tmap = config.get("event_bus.topics", {}) or {}
        topic = tmap.get("research") or "internal.research"

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=kc.get("bootstrap_servers") or ["127.0.0.1:9092"],
            group_id="ai_research_daemon",
            client_id="ai_research_daemon",
            enable_auto_commit=True,
            auto_offset_reset="latest",
            value_deserializer=lambda v: v,
        )

        async def _consume() -> None:
            await consumer.start()
            LOG.info("research_daemon_consumer_started", topic=topic)
            try:
                async for msg in consumer:
                    try:
                        raw = msg.value.decode("utf-8") if isinstance(msg.value, (bytes, bytearray)) else str(msg.value)
                        evt = json.loads(raw)
                        evt.setdefault("source", "kafka")
                        await self._event_queue.put(evt)
                    except Exception:
                        continue
            finally:
                await consumer.stop()
                LOG.info("research_daemon_consumer_stopped", topic=topic)

        self._consumer_task = asyncio.create_task(_consume(), name="research_kafka_consumer")
        return True

    async def _handle_event(self, evt: Dict[str, Any]) -> None:
        """Handle a single trigger event."""
        reason = evt.get("reason") or evt.get("event") or evt.get("type") or "trigger"
        await self._run_cycle(reason=reason, trigger=evt)

    async def _run_cycle(self, reason: str = "periodic", trigger: Optional[Dict[str, Any]] = None) -> None:
        """Execute one research/self-evolution cycle."""
        start = time.time()
        status = "success"
        error: Optional[str] = None
        plan: Dict[str, Any] = {}
        state_json: Dict[str, Any] = {}
        policy_version: Optional[str] = None

        try:
            plan, state, policy = await run_research_cycle_once()
            state_json = state.to_json_dict() if hasattr(state, "to_json_dict") else {}
            policy_version = getattr(policy, "version", None)
        except Exception as exc:  # pragma: no cover - defensive
            status = "error"
            error = str(exc)
            LOG.error("research_cycle_failed", reason=reason, error=error, exc_info=True)

        duration = time.time() - start
        summary = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "status": status,
            "error": error,
            "policy_version": policy_version,
            "plan_focus": plan.get("focus"),
            "experiments": plan.get("experiments"),
        }

        self._persist_log(summary, plan, state_json)
        self._publish_event(summary, trigger)
        self._write_memory(summary, plan, state_json)
        self._record_metrics(status, reason, duration, plan)

    def _persist_log(self, summary: Dict[str, Any], plan: Dict[str, Any], state_json: Dict[str, Any]) -> None:
        day_dir = self.runs_dir / date.today().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        log_file = day_dir / "research_daemon.jsonl"
        record = {**summary, "plan": plan, "state": state_json}
        try:
            with log_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("research_daemon_log_failed", error=str(exc))

    def _publish_event(self, summary: Dict[str, Any], trigger: Optional[Dict[str, Any]]) -> None:
        payload = {
            "event": "research.cycle.completed",
            "summary": summary,
            "trigger": trigger or {},
        }
        try:
            event_bus.publish("research", payload)
        except Exception as exc:  # pragma: no cover
            LOG.warning("research_daemon_publish_failed", error=str(exc))

    def _write_memory(self, summary: Dict[str, Any], plan: Dict[str, Any], state_json: Dict[str, Any]) -> None:
        if write_memory is None:
            return
        try:
            lessons = []
            telemetry = plan.get("telemetry") if isinstance(plan, dict) else {}
            if telemetry and isinstance(telemetry, dict):
                lessons = telemetry.get("risk_warnings") or telemetry.get("key_observations") or []
            payload = {
                "type": "research_cycle",
                "summary": summary,
                "lessons": lessons,
                "plan_focus": plan.get("focus"),
                "experiments": plan.get("experiments"),
                "state": state_json,
            }
            write_memory("STS", payload, doc_id=f"research_cycle_{summary['ts']}")
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("research_daemon_memory_failed", error=str(exc))

    def _record_metrics(self, status: str, reason: str, duration: float, plan: Dict[str, Any]) -> None:
        try:
            metrics.research_cycle_total.labels(status=status, reason=reason).inc()
            metrics.research_cycle_duration_seconds.labels(reason=reason).observe(duration)
            if plan.get("experiments"):
                metrics.auto_evolution_outcome.labels(status="success" if status == "success" else "fail").inc()
        except Exception:
            pass

    async def run(self, once: bool = False) -> None:
        """Main loop: wait for triggers or run periodically."""
        await self._start_kafka_consumer()
        last_run_at = 0.0
        try:
            while not self._stop:
                self._ingest_local_triggers()
                timeout = max(0.0, self.poll_interval_sec - (time.time() - last_run_at))
                try:
                    evt = await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
                    await self._handle_event(evt)
                    last_run_at = time.time()
                except asyncio.TimeoutError:
                    await self._run_cycle(reason="periodic")
                    last_run_at = time.time()
                if once:
                    break
        finally:
            if self._consumer_task:
                self._consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._consumer_task


def main() -> None:
    parser = argparse.ArgumentParser(description="Research/self-evolution daemon")
    parser.add_argument("--poll-interval", type=int, default=900, help="Periodic run interval in seconds")
    parser.add_argument("--once", action="store_true", help="Run a single cycle then exit")
    args = parser.parse_args()

    runs_dir = Path("runs/ai_trader")
    daemon = ResearchDaemon(runs_dir=runs_dir, poll_interval_sec=args.poll_interval)
    asyncio.run(daemon.run(once=args.once))


if __name__ == "__main__":
    main()
