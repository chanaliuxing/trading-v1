# -*- coding: utf-8 -*-
"""
EnhancedModelSignalStrategy Adapter
-----------------------------------
适配 EnhancedModelSignalStrategy 到通用 HPO/回测框架：
- 从 ClickHouse 加载 OHLCV 数据 (bars_1m_ibkr)
- LEFT JOIN features_1m 获取技术指标 (ATR、可选 ML 信号与 VIX)
- 校验关键列
- 将网格参数（点分隔）合并进策略 extras 结构

Author: Trading Team
Date: 2025-11-09 (Fixed: Use bars_1m_ibkr for OHLC)
"""

from __future__ import annotations
from typing import Dict, Any, List
import copy
import pandas as pd

from strategy_adapter import StrategyAdapter


class EnhancedModelAdapter(StrategyAdapter):
    """EnhancedModelSignalStrategy 的策略适配器"""

    def load_data(
        self,
        symbols: List[str],
        start: str,
        end: str,
        clickhouse_config: Dict[str, Any],
        **kwargs,
    ) -> pd.DataFrame:
        features_table = kwargs.get("features_table", "trading.features_1m")
        syms = ",".join([f"'{s}'" for s in symbols])

        # Use bars_1m_ibkr for OHLC data and LEFT JOIN features_1m for indicators
        sql_full = f"""
        SELECT
          b.ts_start as ts, b.symbol,
          b.open, b.high, b.low, b.close, b.volume,
          f.atr_14,
          f.vwap,
          f.ret_1 as ret_1m,
          f.ret_5 as ret_5m,
          f.rsi_14,
          f.vix
        FROM trading.bars_1m_ibkr b
        LEFT JOIN {features_table} f
          ON b.symbol = f.symbol AND b.ts_start = f.ts
        WHERE b.symbol IN ({syms})
          AND b.ts_start >= toDateTime('{start}')
          AND b.ts_start < toDateTime('{end}')
        ORDER BY b.ts_start, b.symbol
        """
        sql_min = f"""
        SELECT
          b.ts_start as ts, b.symbol,
          b.open, b.high, b.low, b.close, b.volume,
          f.atr_14,
          f.vwap,
          f.ret_1 as ret_1m,
          f.ret_5 as ret_5m,
          f.rsi_14
        FROM trading.bars_1m_ibkr b
        LEFT JOIN {features_table} f
          ON b.symbol = f.symbol AND b.ts_start = f.ts
        WHERE b.symbol IN ({syms})
          AND b.ts_start >= toDateTime('{start}')
          AND b.ts_start < toDateTime('{end}')
        ORDER BY b.ts_start, b.symbol
        """
        def _query_http(cfg, q):
            try:
                import clickhouse_connect  # type: ignore
            except Exception:
                return None
            try:
                client = clickhouse_connect.get_client(
                    host=cfg.get("host", "localhost"),
                    port=int(cfg.get("port", 8123) or 8123),
                    username=cfg.get("user") or cfg.get("username") or "default",
                    password=cfg.get("password", ""),
                    database=cfg.get("database", "default"),
                    secure=bool(cfg.get("secure", False)),
                    connect_timeout=5,
                    send_receive_timeout=30,
                )
                res = client.query(q)
                return res.result_rows
            except Exception:
                return None

        def _query_native(cfg, q):
            try:
                from clickhouse_driver import Client  # type: ignore
            except Exception:
                return None
            cfg2 = dict(cfg)
            try:
                if int(cfg2.get("port", 9000) or 9000) == 8123:
                    cfg2["port"] = 9000
            except Exception:
                pass
            try:
                client = Client(**cfg2)
                return client.execute(q)
            except Exception:
                return None

        data = _query_http(clickhouse_config, sql_full)
        columns = [
            "ts", "symbol", "open", "high", "low", "close", "volume",
            "atr_14", "vwap", "ret_1m", "ret_5m", "rsi_14", "vix",
        ]
        if data is None:
            data = _query_native(clickhouse_config, sql_full)
        if data is None:
            # Try minimal projection
            data = _query_http(clickhouse_config, sql_min)
            columns = ["ts", "symbol", "open", "high", "low", "close", "volume", "atr_14", "vwap", "ret_1m", "ret_5m", "rsi_14"]
        if data is None:
            data = _query_native(clickhouse_config, sql_min)
            columns = ["ts", "symbol", "open", "high", "low", "close", "volume", "atr_14", "vwap", "ret_1m", "ret_5m", "rsi_14"]
        if data is None:
            raise RuntimeError(
                "无法连接 ClickHouse (HTTP/native 驱动均失败)。请检查端口与驱动 (HTTP=8123, 原生=9000)。"
            )
        df = pd.DataFrame(data, columns=columns)

        # Map common OOF columns to ml_signal/ml_conf if not already present
        if "ml_signal" not in df.columns:
            for cand in ("lstm_oof_h5", "oof_signal", "pred_signal"):
                if cand in df.columns:
                    df["ml_signal"] = df[cand]
                    break
        if "ml_conf" not in df.columns:
            for cand in ("lstm_oof_conf_h5", "oof_conf", "pred_conf"):
                if cand in df.columns:
                    df["ml_conf"] = df[cand]
                    break

        # Provide explicit model-prefixed columns for ensemble experiments (方案A)
        # - If LSTM OOF columns exist, expose lstm_signal/lstm_conf
        # - If generic ml_signal/ml_conf exist, also expose as xgb_signal/xgb_conf as a baseline alias
        try:
            if "lstm_oof_h5" in df.columns and "lstm_oof_conf_h5" in df.columns:
                df["lstm_signal"] = df["lstm_oof_h5"].astype(float)
                df["lstm_conf"] = df["lstm_oof_conf_h5"].astype(float)
        except Exception:
            pass
        try:
            if "ml_signal" in df.columns and "ml_conf" in df.columns:
                # Assume current ml_* 来源于 XGB/传统模型推理，暴露为 xgb_* 以便和 lstm_* 做融合/对比
                if "xgb_signal" not in df.columns:
                    df["xgb_signal"] = df["ml_signal"].astype(float)
                if "xgb_conf" not in df.columns:
                    df["xgb_conf"] = df["ml_conf"].astype(float)
        except Exception:
            pass

        # Optional: inject sector as a column to help explorations
        try:
            if not df.empty:
                syms = ",".join([f"'{s}'" for s in sorted(df["symbol"].unique())])
                q = f"SELECT symbol, sector FROM trading.dim_symbol WHERE symbol IN ({syms})"
                # FIX: Ensure we use native port 9000, not HTTP port 8123
                cfg_native = dict(clickhouse_config)
                if int(cfg_native.get("port", 9000) or 9000) == 8123:
                    cfg_native["port"] = 9000
                m = {r[0]: r[1] for r in Client(**cfg_native).execute(q)}
                df["sector"] = df["symbol"].map(lambda s: m.get(s, ""))
        except Exception:
            pass
        return df

    def validate_data(self, df: pd.DataFrame) -> None:
        required = ["ts", "symbol", "open", "high", "low", "close", "volume", "atr_14"]
        missing = set(required) - set(df.columns)
        if missing:
            raise ValueError(f"缺少必需列: {missing}")
        if df.empty:
            raise ValueError("数据为空")

    def parse_params(self, grid_params: Dict[str, Any], base_extras: Dict[str, Any]) -> Dict[str, Any]:
        """将点分隔参数合并到 extras 嵌套结构。

        例如：
        sizing.mode=confidence_scaled -> extras['sizing']['mode'] = 'confidence_scaled'
        market_filter.conf_floor=0.6  -> extras['market_filter']['conf_floor']=0.6
        risk.stop_loss_pct=0.01       -> extras['risk']['stop_loss_pct']=0.01
        """
        def _merge_dot(dst: Dict[str, Any], dotted_key: str, val: Any):
            cur = dst
            parts = dotted_key.split(".")
            for p in parts[:-1]:
                if p not in cur or not isinstance(cur[p], dict):
                    cur[p] = {}
                cur = cur[p]
            cur[parts[-1]] = val

        extras = copy.deepcopy(base_extras or {})
        for k, v in (grid_params or {}).items():
            if "." in k:
                _merge_dot(extras, k, v)
            else:
                extras[k] = v
        return {"extras": extras}

    def get_required_columns(self) -> List[str]:
        return ["ts", "symbol", "open", "high", "low", "close", "volume", "atr_14"]

    def supports_ml_signals(self) -> bool:
        return True

    def get_strategy_description(self) -> str:
        return "EnhancedModelSignalStrategy Adapter (bars_1m_ibkr LEFT JOIN features_1m for indicators)"
