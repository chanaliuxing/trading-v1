#!/usr/bin/env python3
"""
Emit sync/fallback events for unhealthy targets.

This script scans the mutable_targets snapshot and emits remediation events
for targets with low health scores. These events are used for audit trails
and can trigger automated remediation workflows.

Usage:
  PYTHONPATH=. venv/bin/python tools/emit_unhealthy_target_events.py [--threshold 0.5]
"""
import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List


def load_mutable_targets_snapshot() -> Dict[str, Any]:
    """Load existing mutable_targets snapshot."""
    snapshot_path = Path("runs/memory/mutable_targets.json")
    if not snapshot_path.exists():
        return {}

    try:
        return json.loads(snapshot_path.read_text())
    except Exception:
        return {}


def emit_ml_event(event_type: str, status: str, entity_id: str, params: dict):
    """Emit an ml.event for observability."""
    try:
        from tools.nightly_oof_flow import publish_ml_event
        publish_ml_event(status, event_type, entity_id, params=params)
        return True
    except Exception as e:
        print(f"[warn] Failed to emit event: {e}")
        return False


def scan_unhealthy_targets(
    targets: Dict[str, Any],
    health_threshold: float
) -> List[Dict[str, Any]]:
    """
    Scan for targets with health_score below threshold.

    Returns list of unhealthy targets with recommended actions.
    """
    unhealthy_targets = []

    for target_id, target_data in targets.items():
        metrics = target_data.get("metrics", {})
        health_score = metrics.get("health_score", 1.0)
        recommendation = metrics.get("recommendation", "healthy")

        if health_score < health_threshold or recommendation in ["sync_or_fallback", "data_quality_issue"]:
            unhealthy_targets.append({
                "target_id": target_id,
                "health_score": health_score,
                "recommendation": recommendation,
                "stale_days": metrics.get("stale_days", 0),
                "last_updated": metrics.get("last_updated", "unknown"),
                "bad_symbols": metrics.get("bad_symbols", []),
                "error_rate": metrics.get("error_rate", 0.0),
            })

    return unhealthy_targets


def emit_remediation_events(unhealthy_targets: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Emit remediation events for unhealthy targets.

    Returns counts of emitted events by type.
    """
    event_counts = {
        "sync_requested": 0,
        "fallback_recommended": 0,
        "data_quality_issue": 0,
    }

    for target in unhealthy_targets:
        target_id = target["target_id"]
        recommendation = target["recommendation"]
        health_score = target["health_score"]

        # Determine event type based on recommendation
        if recommendation == "sync_or_fallback":
            if health_score < 0.3:
                # Very low health: recommend fallback
                event_type = "target.fallback_recommended"
                status = "warning"
                event_counts["fallback_recommended"] += 1
            else:
                # Medium health: request sync
                event_type = "target.sync_requested"
                status = "info"
                event_counts["sync_requested"] += 1

        elif recommendation == "data_quality_issue":
            event_type = "target.data_quality_issue"
            status = "warning"
            event_counts["data_quality_issue"] += 1

        else:
            # Generic unhealthy target
            event_type = "target.unhealthy"
            status = "warning"

        # Emit event
        success = emit_ml_event(
            event_type,
            status,
            target_id,
            params={
                "target_id": target_id,
                "health_score": health_score,
                "recommendation": recommendation,
                "stale_days": target["stale_days"],
                "bad_symbols": target["bad_symbols"],
                "error_rate": target["error_rate"],
                "timestamp": datetime.now().isoformat(),
            }
        )

        if success:
            print(f"[emit] ✅ {event_type}: {target_id} (health={health_score:.2f})")
        else:
            print(f"[emit] ❌ Failed: {target_id}")

    return event_counts


def main():
    ap = argparse.ArgumentParser(description="Emit events for unhealthy targets")
    ap.add_argument("--threshold", type=float, default=0.5, help="Health score threshold (default: 0.5)")
    args = ap.parse_args()

    print(f"[scan] Loading mutable_targets snapshot (threshold={args.threshold})...")
    targets = load_mutable_targets_snapshot()

    if not targets:
        print("[scan] No mutable_targets snapshot found")
        return

    print(f"[scan] Found {len(targets)} targets")

    print("[scan] Scanning for unhealthy targets...")
    unhealthy = scan_unhealthy_targets(targets, args.threshold)

    if not unhealthy:
        print("[scan] ✅ All targets healthy")
        return

    print(f"[scan] Found {len(unhealthy)} unhealthy targets:")
    for target in unhealthy:
        print(f"  - {target['target_id']}: health={target['health_score']:.2f}, rec={target['recommendation']}")

    print("\n[emit] Emitting remediation events...")
    event_counts = emit_remediation_events(unhealthy)

    print("\n[summary] Event counts:")
    for event_type, count in event_counts.items():
        if count > 0:
            print(f"  - {event_type}: {count}")

    # Emit summary event
    emit_ml_event(
        "unhealthy_targets.scan_completed",
        "completed",
        f"scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        params={
            "total_targets": len(targets),
            "unhealthy_count": len(unhealthy),
            "threshold": args.threshold,
            "event_counts": event_counts,
        }
    )

    print(f"\n✅ Scan complete: {len(unhealthy)} unhealthy targets processed")


if __name__ == "__main__":
    main()
