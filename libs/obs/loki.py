from __future__ import annotations
"""Minimal Loki HTTP push helper.

Usage:
  from libs.obs.loki import push
  push(labels={"job": "ai_trader", "component": "Strategy"}, payload={...})

Enabled when env LOKI_URL is set (e.g., http://localhost:3100) and LOKI_PUSH_ENABLED=1.
Silently ignores all errors.
"""
import os
import time
import json
from typing import Dict, Any


def _endpoint() -> str | None:
    if os.getenv("LOKI_PUSH_ENABLED", "").strip() not in {"1", "true", "TRUE", "yes"}:
        return None
    base = os.getenv("LOKI_URL")
    if not base:
        return None
    return base.rstrip("/") + "/loki/api/v1/push"


def push(labels: Dict[str, str], payload: Dict[str, Any]) -> None:
    url = _endpoint()
    if not url:
        return
    try:
        import requests  # type: ignore
    except Exception:
        return
    try:
        # Loki expects {streams: [{stream: labels, values: [[ts_ns, line], ...]}]}
        ts_ns = str(int(time.time() * 1e9))
        line = json.dumps(payload, ensure_ascii=False)
        data = {"streams": [{"stream": labels, "values": [[ts_ns, line]]}]}
        requests.post(url, json=data, timeout=2)
    except Exception:
        return

