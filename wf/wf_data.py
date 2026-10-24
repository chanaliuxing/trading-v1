"""Auxiliary data loaders for walk-forward pipeline."""

from __future__ import annotations

from typing import Any, Dict

import clickhouse_connect
import pandas as pd


def load_df_from_clickhouse(sql: str, ch_cfg: Dict[str, Any]) -> pd.DataFrame:
    client = clickhouse_connect.get_client(
        host=ch_cfg.get("host", "localhost"),
        port=int(ch_cfg.get("port", 8123)),
        username=ch_cfg.get("username", ch_cfg.get("user", "default")),
        password=ch_cfg.get("password", ""),
        database=ch_cfg.get("database", "default"),
    )
    return client.query_df(sql)

