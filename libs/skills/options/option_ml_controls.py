"""
Option ML controls read helper for AI Brain.

Provides read-only access to the latest control row for a strategy.
"""

from __future__ import annotations

from typing import Dict

from libs.db.ch_pool import get_client


def read_latest_option_ml_controls(strategy_name: str = "intraday_options_long") -> Dict[str, object]:
    """
    Return the latest controls row for the given strategy.
    """
    client = get_client()
    sql = """
    SELECT *
    FROM trading.option_ml_controls
    WHERE strategy_name = %(strategy_name)s
    ORDER BY updated_at DESC
    LIMIT 1
    """
    rows = client.query(sql, parameters={"strategy_name": strategy_name})
    if not rows:
        return {}
    return dict(rows[0])
