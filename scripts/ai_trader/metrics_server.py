#!/usr/bin/env python3
"""
Prometheus Metrics HTTP Server

Exposes AI trading system metrics on http://localhost:8000/metrics

Usage:
    python scripts/ai_trader/metrics_server.py [--port 8000]
"""
import argparse
import sys
import time
from pathlib import Path

# Add project root to path
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from libs.metrics import is_enabled
from libs.logger import get_logger

logger = get_logger(__name__)


def main():
    parser = argparse.ArgumentParser(description='Start Prometheus metrics HTTP server')
    parser.add_argument('--port', type=int, default=8000, help='Port to listen on (default: 8000)')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
    args = parser.parse_args()

    if not is_enabled():
        logger.error("metrics_disabled", reason="prometheus_client not installed")
        print("❌ Error: prometheus_client is not installed")
        print("Install with: pip install prometheus-client")
        sys.exit(1)

    try:
        from prometheus_client import start_http_server

        start_http_server(args.port, addr=args.host)

        logger.info("metrics_server_started", port=args.port, host=args.host)
        print(f"✅ Metrics server started on http://{args.host}:{args.port}/metrics")
        print("Press Ctrl+C to stop")

        # Keep server running
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        logger.info("metrics_server_stopped")
        print("\n✅ Metrics server stopped")
        sys.exit(0)
    except Exception as e:
        logger.error("metrics_server_failed", err=str(e), exc_info=True)
        print(f"❌ Failed to start metrics server: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
