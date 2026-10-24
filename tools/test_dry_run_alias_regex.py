#!/usr/bin/env python3
from __future__ import annotations
"""
Small sanity-check script for the Brain SQL dry-run helper.

It verifies that expressions using the 5-minute alias `m5` are parsed
correctly as alias 'm5' (not 'm' + column '5') during pre-flight schema
validation, so that columns like `m5.tick_imbalance` can be validated
against the trading.features_5m schema without spurious errors.

This script does NOT require a live ClickHouse connection; it patches
inspect_table_schema and get_client to use in-memory stubs.
"""

from unittest.mock import patch, MagicMock
import sys
from pathlib import Path

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.ai_trader.brain import tools_sql


def run() -> None:
    expr = "m5.tick_imbalance"

    # Stub schema and client so we don't depend on ClickHouse
    with patch("scripts.ai_trader.brain.tools_sql.inspect_table_schema") as mock_schema, patch(
        "scripts.ai_trader.brain.tools_sql.get_client"
    ) as mock_client:
        # Return a minimal schema where tick_imbalance exists on m5
        def _schema(table: str, save: bool = False):
            if "features_5m" in table:
                return {"columns": [{"name": "tick_imbalance", "type": "Float64"}]}
            return {"columns": [{"name": "close", "type": "Float64"}]}

        mock_schema.side_effect = _schema
        mock_client.return_value = MagicMock(query=lambda sql, params=None: [{"value": 0.0}])

        res = tools_sql.dry_run_feature_expr(expr, sources=["m5"], save=False)
        print("dry_run_feature_expr result:", res)


if __name__ == "__main__":
    run()
