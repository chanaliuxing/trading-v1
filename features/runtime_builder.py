# features/runtime_builder.py

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from collections import Counter

import pandas as pd
import clickhouse_connect


@dataclass
class FeatureRuntimeConfig:
    """
    控制 runtime 特征构建的参数。

    Attributes:
        warmup_minutes: How many minutes of historical data to fetch
        ts_column: Timestamp column name in bars table
        symbol_column: Symbol column name
        default_table: Default table name if not specified in registry
    """
    warmup_minutes: int = 120  # 拉过去多少分钟的数据作为上下文
    ts_column: str = "ts"      # 时间列名，根据你的 bars 表情况调整
    symbol_column: str = "symbol"
    default_table: str = "bars_1m"


class FeatureRuntimeBuilder:
    """
    使用 ClickHouse + feature_registry 在实盘/回放时构建特征向量。

    This class builds feature vectors at runtime by:
    1. Fetching historical bars from ClickHouse
    2. Using feature registry to generate SELECT expressions
    3. Returning the latest feature row for prediction
    """

    def __init__(
        self,
        ch_cfg: Dict[str, Any],
        feature_registry: Dict[str, Any],
        config: Optional[FeatureRuntimeConfig] = None,
    ) -> None:
        """
        Initialize runtime builder.

        Args:
            ch_cfg: ClickHouse configuration dict with host, port, username, password, database
            feature_registry: Feature registry dict loaded from JSON
            config: Optional runtime configuration
        """
        self._client = clickhouse_connect.get_client(
            host=ch_cfg.get("host", "localhost"),
            port=ch_cfg.get("port", 8123),
            username=ch_cfg.get("username", ch_cfg.get("user", "default")),
            password=ch_cfg.get("password", ""),
            database=ch_cfg.get("database", "default"),
        )
        self._registry = feature_registry
        self._cfg = config or FeatureRuntimeConfig()

    def _get_feature_meta(self, name: str) -> Dict[str, Any]:
        """
        Get feature metadata from registry.

        Args:
            name: Feature name

        Returns:
            Feature metadata dict

        Raises:
            KeyError: If feature not found in registry
        """
        meta = self._registry.get("features", {}).get(name)
        if not meta:
            raise KeyError(f"Feature {name!r} not found in feature_registry")
        return meta

    def build_for_now(
        self,
        *,
        symbol: str,
        now_ts: str,
        feature_subset: List[str],
        override_table: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        针对**单个 symbol、单个时刻**构建特征：
        - 先从 ClickHouse 拉一段 warmup 的原始数据
        - 用 feature_registry 中的 SQL 表达式拼出 SELECT
        - 取结果中最新一行作为 runtime feature row

        Args:
            symbol: Trading symbol
            now_ts: Current timestamp (ISO format)
            feature_subset: List of feature names to build
            override_table: Optional table name override

        Returns:
            DataFrame with one row containing feature values

        Raises:
            RuntimeError: If no data returned from ClickHouse
        """
        # 1) 解析时间窗口
        ts_now = datetime.fromisoformat(now_ts.replace("Z", "+00:00"))
        ts_from = ts_now - timedelta(minutes=self._cfg.warmup_minutes)

        # 2) 找出这些特征对应的 source_table（目前假设都来自同一个 freq 表，如果不同可以再扩展）
        table = override_table or self._guess_source_table(feature_subset)

        # 3) 根据 registry 生成 SELECT 列
        select_exprs = []
        for name in feature_subset:
            meta = self._get_feature_meta(name)
            expr = meta.get("sql_expr", name)
            select_exprs.append(f"{expr} AS {name}")

        ts_col = self._cfg.ts_column
        sym_col = self._cfg.symbol_column

        sql = f"""
        SELECT
            {ts_col},
            {", ".join(select_exprs)}
        FROM {table}
        WHERE {sym_col} = %(symbol)s
          AND {ts_col} BETWEEN %(ts_from)s AND %(ts_to)s
        ORDER BY {ts_col} ASC
        """

        df = self._client.query_df(
            sql,
            parameters={
                "symbol": symbol,
                "ts_from": ts_from,
                "ts_to": ts_now,
            },
        )

        if df.empty:
            raise RuntimeError(
                f"No data returned for symbol={symbol}, table={table}, "
                f"window=[{ts_from}, {ts_now}]"
            )

        # 4) 取最后一行作为「当前时刻」的特征向量
        last_row = df.iloc[[-1]].copy()
        # 确保列顺序与 feature_subset 对齐
        cols = [c for c in feature_subset if c in last_row.columns]
        return last_row[cols]

    def _guess_source_table(self, feature_subset: List[str]) -> str:
        """
        简单规则：看 feature_registry 里 majority 的 source_table。
        如果没有配置，就退化到 default_table。

        Args:
            feature_subset: List of feature names

        Returns:
            Table name to query
        """
        tables = []
        for name in feature_subset:
            meta = self._get_feature_meta(name)
            tbl = meta.get("source_table")
            if tbl:
                tables.append(tbl)

        if not tables:
            return self._cfg.default_table

        # 多数表作为最终源表
        tbl, _ = Counter(tables).most_common(1)[0]
        return tbl
