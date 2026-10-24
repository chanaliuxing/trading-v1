import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from libs.obs.audit_writer import LocalAuditWriter
from services.exec.execution_service import ExecutionService
from services.exec.trace import TraceContext
from services.reconciliation.runtime_reconciler import RuntimeReconciler


@pytest.mark.asyncio
async def test_local_audit_writer_writes_hourly_domain_file(tmp_path: Path):
    writer = LocalAuditWriter(root_dir=tmp_path, flush_interval_sec=0.05, batch_size=10)
    await writer.start()
    writer.emit(
        domain="exec",
        component="execution_service",
        stage="intent_requested",
        strategy_id="qqq_intraday_001",
        trace_id="trace-1",
        status="requested",
        payload={"qty": 10},
    )
    await writer.stop()

    files = list(tmp_path.rglob("exec-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["domain"] == "exec"
    assert rec["stage"] == "intent_requested"
    assert rec["trace_id"] == "trace-1"
    assert rec["payload"]["qty"] == 10


def test_local_audit_writer_queue_full_is_fail_open(tmp_path: Path):
    writer = LocalAuditWriter(root_dir=tmp_path, queue_size=1)
    for idx in range(writer.queue_size):
        assert writer.emit(domain="exec", component="test", stage=f"seed_{idx}")
    assert not writer.emit(domain="exec", component="test", stage="two")


def test_execution_service_local_audit_emit_is_non_blocking():
    audit_writer = MagicMock()
    svc = ExecutionService(
        pg=MagicMock(),
        risk_service=MagicMock(),
        safety_kernel=MagicMock(),
        enable_execution=False,
        audit_writer=audit_writer,
    )
    trace_ctx = TraceContext(
        trace_id="trace-1",
        causation_id="cause-1",
        strategy_id="qqq_intraday_001",
        strategy_run_id="run-1",
        decision_id="dec-1",
        trigger_id="trg-1",
        intent_id="intent-1",
    )

    svc._emit_local_audit(
        stage="intent_requested",
        trace_ctx=trace_ctx,
        symbol="TQQQ",
        intent_id="intent-1",
        status="requested",
        payload={"qty": 10},
    )

    audit_writer.emit.assert_called_once()
    kwargs = audit_writer.emit.call_args.kwargs
    assert kwargs["domain"] == "exec"
    assert kwargs["stage"] == "intent_requested"
    assert kwargs["strategy_id"] == "qqq_intraday_001"
    assert kwargs["trace_id"] == "trace-1"


def test_runtime_reconciler_local_audit_emit_is_non_blocking():
    audit_writer = MagicMock()
    reconciler = RuntimeReconciler(
        pg=MagicMock(),
        ib_adapter=MagicMock(),
        audit_writer=audit_writer,
    )

    reconciler._emit_audit(
        stage="orphan_detected",
        severity="warning",
        broker_order_id="22",
        symbol="SQQQ",
        status="detected",
        payload={"count": 1},
    )

    audit_writer.emit.assert_called_once()
    kwargs = audit_writer.emit.call_args.kwargs
    assert kwargs["domain"] == "recon"
    assert kwargs["stage"] == "orphan_detected"
    assert kwargs["severity"] == "warning"
    assert kwargs["broker_order_id"] == "22"
