太好了！接下来我把**喂给 LLM 的数据清单**落到你现有 ClickHouse 结构，给出**精简可用的取数方案（SQL 片段 + 聚合思路）**与**最终输入 JSON 结构**。目标：信息够用、层次分明、token 省、能驱动止盈/止损与仓位动态。

---

# 1) 时间 & 阶段（全局开关）

**目的**：让 LLM 有“时间压力”和“收盘意识”。

* `now_utc`, `now_et`
* `market_phase`（PREMARKET / OPEN / MIDDAY / POWER_HOUR / CLOSE）
* `minutes_since_open`, `minutes_to_close`（由引擎计算后直接传）

> 这些字段不从 CH 查，直接在引擎里根据时区与日历计算。

---

# 2) 仓位 & 现金（行为约束）

**目的**：避免“空仓也 HOLD”；驱动仓位大小决策。

* `position_size`, `avg_cost`, `current_price`, `unrealized_pnl_pct`
* `available_cash`, `capital_limit`
* `open_orders_brief`（可无）
* （派生）`risk_budget_per_trade`（见第6点的风险映射）

> 来自你的 Portfolio/Redis/IBKR，本地注入，不走 CH。

---

# 3) 多周期行情与特征（“看树也看林”）

**总原则**：每个周期**只给最新行 + 简短统计摘要**，不要整段序列。所有表均过滤 `symbol = :sym`。

### 3.1 超短线（5s，最近 60–120s）— `trading.features_5s`

**摘要指标**（建议 10–20 行窗口）：

* 微动量：`ret_5`（mean / std / min / max）
* 微趋势：`close_slope_per_sec`（用 ts vs close 的协方差/方差估算）
* 波动/量能：`rv_20`, `rvol_roll_20`, `trade_count_ratio`, `avg_trade_size`
* 微结构/交易成本：`spread_bps`, `close_to_mid_bps`, `vwap_dev_bps`, `price_impact_5m`
* 盘口偏向：`tick_imbalance`
* 信号：`ema_cross`

**取数思路（写法要点）**

* 先 LIMIT 最近 N 行，再做一次外层聚合，避免全表扫：

```sql
WITH t AS (
  SELECT ts, close, ret_5, rv_20, rvol_roll_20, tick_imbalance, trade_count_ratio,
         close_to_mid_bps, vwap_dev_bps, spread_bps, price_impact_5m, ema_cross
  FROM trading.features_5s
  WHERE symbol = {sym} AND ts >= now() - INTERVAL 120 SECOND
  ORDER BY ts DESC
  LIMIT 40
)
SELECT
  max(ts)                                      AS ts_last,
  avg(ret_5)                                   AS ret5_mean,
  stddevSamp(ret_5)                            AS ret5_std,
  min(ret_5)                                   AS ret5_min,
  max(ret_5)                                   AS ret5_max,
  covarSamp(close, toUnixTimestamp(ts)) / nullIf(varSamp(toUnixTimestamp(ts)),0) AS close_slope_per_sec,
  avg(rv_20)                                   AS rv20,
  avg(rvol_roll_20)                            AS rvol20,
  avg(tick_imbalance)                          AS tick_imbalance,
  avg(trade_count_ratio)                       AS trade_count_ratio,
  avg(close_to_mid_bps)                        AS close_to_mid_bps,
  avg(vwap_dev_bps)                            AS vwap_dev_bps,
  avg(spread_bps)                              AS spread_bps,
  avg(price_impact_5m)                         AS price_impact_5m,
  anyLast(ema_cross)                           AS ema_cross_last
FROM t;
```

### 3.2 分钟线（1m，最近 10–15 分钟）— `trading.features_1m`

**关键字段**：`ret_1`, `atr_20`, `zscore_close_20`, `vwap_day`, `vwap_slope_20`, `rsi_14`, `cmf_20`, `stoch_k_14`

```sql
WITH t AS (
  SELECT ts, ret_1, atr_20, zscore_close_20, vwap_day, vwap_slope_20,
         rsi_14, cmf_20, stoch_k_14
  FROM trading.features_1m
  WHERE symbol = {sym}
  ORDER BY ts DESC
  LIMIT 15
)
SELECT
  max(ts)                     AS ts_last,
  avg(ret_1)                  AS ret1_mean,
  stddevSamp(ret_1)           AS ret1_std,
  anyLast(atr_20)             AS atr20_last,
  anyLast(zscore_close_20)    AS zc20_last,
  anyLast(vwap_day)           AS vwap_day_last,
  anyLast(vwap_slope_20)      AS vwap_slope_last,
  anyLast(rsi_14)             AS rsi_last,
  anyLast(cmf_20)             AS cmf_last,
  anyLast(stoch_k_14)         AS stoch_k_last
FROM t;
```

### 3.3 五分钟/十五分钟（方向与节奏）— `trading.features_5m`, `trading.features_15m`

**关键字段（聚合窗口建议：5m 近 60–90 分钟；15m 近 2–4 小时）**：

* `ema_9`, `ema_21`, `bb_pct_20`, `donchian_hi_20/lo_20`, `slope_20`, `r2_20`, `rv_20`

```sql
-- 5m
WITH t AS (
  SELECT ts, ema_9, ema_21, bb_pct_20, donchian_hi_20, donchian_lo_20, slope_20, r2_20, rv_20
  FROM trading.features_5m
  WHERE symbol = {sym} AND ts >= now() - INTERVAL 90 MINUTE
  ORDER BY ts DESC
  LIMIT 20
)
SELECT
  max(ts)                AS ts_last,
  anyLast(ema_9)         AS ema9_last,
  anyLast(ema_21)        AS ema21_last,
  anyLast(bb_pct_20)     AS bb_pct_last,
  anyLast(donchian_hi_20) AS don_hi_last,
  anyLast(donchian_lo_20) AS don_lo_last,
  avg(slope_20)          AS slope20_mean,
  avg(r2_20)             AS r2_mean,
  avg(rv_20)             AS rv20_mean
FROM t;

-- 15m
WITH t AS (
  SELECT ts, ema_9, ema_21, bb_pct_20, slope_20, r2_20, rv_20
  FROM trading.features_15m
  WHERE symbol = {sym} AND ts >= now() - INTERVAL 4 HOUR
  ORDER BY ts DESC
  LIMIT 16
)
SELECT
  max(ts)            AS ts_last,
  anyLast(ema_9)     AS ema9_last,
  anyLast(ema_21)    AS ema21_last,
  anyLast(bb_pct_20) AS bb_pct_last,
  avg(slope_20)      AS slope20_mean,
  avg(r2_20)         AS r2_mean,
  avg(rv_20)         AS rv20_mean
FROM t;
```

### 3.4 高级别（2h/4h）— `trading.features_2h`, `trading.features_4h`（只取最新行）

**关键字段**：`ema_20`, `ema_50`, `atr_14/20`, `z_price_60`, `kama_10`, `skew_60`, `kurt_60`

```sql
SELECT ts, ema_20, ema_50, atr_14, atr_20, z_price_60, kama_10
FROM trading.features_2h
WHERE symbol = {sym}
ORDER BY ts DESC
LIMIT 1;
```

### 3.5 日线背景— `trading.bars_1d_current`（只取当前/最新）

**关键字段**：`open, high, low, close, vwap, volume`（位置与波动的大背景）

```sql
SELECT ts_start, open, high, low, close, vwap, volume
FROM trading.bars_1d_current
WHERE symbol = {sym}
ORDER BY ts_start DESC
LIMIT 1;
```

---

# 4) 市场广谱/联动（风险情绪）

**来源**：优先直接取你 `features_1m` 最新行里的 VIX/指数相关字段（省一次 JOIN）。

* `vix`, `vix_z_20`, `vix_chg_5m`, `vix_rsi_14`
* `spy_ret_1/5`, `qqq_ret_1/5`
* `beta_spy_20/60`, `corr_spy_20/60`, `corr_qqq_20`

```sql
SELECT vix, vix_z_20, vix_chg_5m, vix_rsi_14,
       spy_ret_1, spy_ret_5, qqq_ret_1, qqq_ret_5,
       beta_spy_20, beta_spy_60, corr_spy_20, corr_spy_60, corr_qqq_20
FROM trading.features_1m
WHERE symbol = {sym}
ORDER BY ts DESC
LIMIT 1;
```

> 若你更信任 `trading.refs_vix_ticks`，可在引擎里补充一个“最近 5/30 分钟均值与斜率”的轻聚合。

---

# 5) 流动性/交易成本（做或不做、仓位多大）

* `spread_bps`, `price_impact_5m`, `volume`, `avg_trade_size`, `trades`
* 来自 5s/1m 特征最新行 + 近窗口均值（上文已覆盖）

---

# 6) 风险映射（系统先算好再给 LLM）

**由引擎生成**（非 CH）：

* `regime_volatility_score`（基于 1m/5m 的 `atr/rv` 归一化 0–1）
* `regime_trend_score`（多周期 EMA/斜率一致性打分）
* `liquidity_cost_bps`（由 spread/impact 归一化）
* `market_stress_score`（由 vix/vix_z/指数波动映射）
* `risk_budget_per_trade`（例如 `min(cash*1%, cap*0.5%) * f(vol_score)`）

> LLM 输出 `risk_units`，你再根据“建议止损距离”（见 3.2 的 ATR 或 5s/1m 波动）折算成 shares。

---

# 7) 记忆（让模型“会学”）

* 最近 10–20 笔**压缩摘要**：`{t, action, conf, qty, entry, exit?, pnl_pct, hold_min}`
* 统计：`win_rate, avg_pnl_pct, avg_hold_min, last_3_mistakes`

> 放到 prompt 的 “RECENT MEMORY SUMMARY” 模块，短句描述，省 token。

---

# 8) 统一喂给 LLM 的 JSON 结构（建议）

> 这不是让 LLM 输出，是**你传给 LLM 的输入结构**；内部会被拼成自然语言/表格型 prompt。

```json
{
  "time": {
    "now_utc": "2025-10-21T20:04:09Z",
    "now_et": "2025-10-21T16:04:09-04:00",
    "market_phase": "MIDDAY",
    "minutes_since_open": 394,
    "minutes_to_close": 26
  },
  "portfolio": {
    "position_size": 0,
    "avg_cost": null,
    "current_price": 442.58,
    "unrealized_pnl_pct": 0.0,
    "available_cash": 50000,
    "capital_limit": 50000
  },
  "micro_5s": {
    "ts_last": "2025-10-21T20:04:08Z",
    "ret5_mean": -0.0003,
    "ret5_std": 0.0012,
    "ret5_min": -0.0031,
    "ret5_max": 0.0028,
    "close_slope_per_sec": -0.0042,
    "rv20": 0.35,
    "rvol20": 0.9,
    "tick_imbalance": -0.2,
    "trade_count_ratio": 1.1,
    "close_to_mid_bps": -5.2,
    "vwap_dev_bps": -8.4,
    "spread_bps": 2.1,
    "price_impact_5m": 3.6,
    "ema_cross_last": -1
  },
  "intra_1m": {
    "ts_last": "2025-10-21T20:04:00Z",
    "ret1_mean": 0.0001,
    "ret1_std": 0.0009,
    "atr20_last": 1.82,
    "zc20_last": 1.1,
    "vwap_day_last": 441.9,
    "vwap_slope_last": 0.4,
    "rsi_last": 62.0,
    "cmf_last": 0.18,
    "stoch_k_last": 74.0
  },
  "intra_5m": { "ema9_last": 441.8, "ema21_last": 441.1, "bb_pct_last": 0.68, "slope20_mean": 0.3, "r2_mean": 0.42, "rv20_mean": 0.28 },
  "intra_15m": { "ema9_last": 441.2, "ema21_last": 440.9, "bb_pct_last": 0.55, "slope20_mean": 0.2, "r2_mean": 0.36, "rv20_mean": 0.25 },
  "higher_tf": {
    "features_2h": { "ema_20": 438.0, "ema_50": 434.5, "atr_14": 6.8, "z_price_60": 0.9, "kama_10": 440.6 }
  },
  "daily": { "open": 435.1, "high": 445.0, "low": 432.0, "close": 442.6, "vwap": 439.8, "volume": 2.1e7 },
  "market_wide": {
    "vix": 14.2, "vix_z_20": -0.4, "vix_chg_5m": -0.3, "vix_rsi_14": 38.0,
    "spy_ret_5": 0.0012, "qqq_ret_5": 0.0018,
    "beta_spy_20": 1.22, "corr_spy_20": 0.86, "corr_qqq_20": 0.88
  },
  "liquidity_cost": { "spread_bps": 2.1, "impact_bps": 3.6 },
  "risk_map": {
    "regime_volatility_score": 0.62,
    "regime_trend_score": 0.55,
    "liquidity_cost_bps": 2.9,
    "market_stress_score": 0.35,
    "risk_budget_per_trade": 400.0,
    "suggested_stop_distance": 0.85
  },
  "memory": {
    "recent_trades_summary": "近10笔：胜率 60%，均盈亏 +0.35%，均持有 7.5 分钟；最近3笔过度谨慎错失向上突破。",
    "last_3": [
      {"t":"16:00-04:00","a":"HOLD","c":0.58,"pnl":0.0,"note":"空仓HOLD，错过+0.6%"},
      {"t":"15:40-15:55","a":"SELL","c":0.71,"pnl":+0.3,"note":"尾盘减仓正确"},
      {"t":"14:10-14:18","a":"BUY","c":0.63,"pnl":-0.1,"note":"入场偏早"}
    ]
  },
  "rules": {
    "no_overnight": true,
    "no_new_positions_within_minutes_to_close": 20,
    "hold_means_keep_only_if_position_gt_zero": true,
    "max_risk_per_trade_fraction": 0.01
  }
}
```

> 在实际 prompt 中，把这些字段转成**紧凑的中文要点**（而不是整 JSON 串），会更省 token、更利于 R1 的推理阅读。

---

# 9) 运行层面的小贴士

* **R1 模型节奏**：建议 30–60s/轮；近收盘自动禁开新仓。
* **空仓 HOLD 处理**：空仓时若 `action == HOLD` → 视为 `WAIT`，并在反馈中提示“错失可能入场”。
* **止盈止损**：先把“建议位”喂给 LLM，让它决策；你根据 `risk_units` 折算 `shares`。
* **记忆**：每轮把“本轮决策摘要 + 若成交则结果”写入 episodic memory；盘后触发 reflection 更新 policy memory。

---

如果你点头，我可以把上面这些**SQL 片段 + 序列压缩器 + Prompt Builder**直接写成你项目里可复用的模块（保持与你的 `run_single_bot.py / market_data_fetcher.py` 风格一致），并接入你目前的 LLM “持仓感知”模板。
太好了。我按你刚才确认的方向，把**SQL 取数 + 序列压缩 + 风险映射 + 记忆 + LLM Prompt Builder**做成可直接落到你项目里的模块；尽量**不动你现有 `run_single_bot.py / market_data_fetcher.py / portfolio_manager.py` 的主体**，只需要在入口处调用这些新模块即可。

下面直接给可复制的代码（按文件分发）。如果你希望合成到现有文件里，也可以把这些模块内容粘到对应文件的底部并调整 import。

---

### `market_data_summarizer.py`

多周期 SQL 聚合（基于你贴的 ClickHouse 视图/表），返回*已压缩*指标，避免把整段序列塞进 LLM。

```python
# market_data_summarizer.py
# -*- coding: utf-8 -*-
import math
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import pandas as pd

# 兼容 clickhouse_driver.Client 或 clickhouse_connect.Client
def _df(client, sql: str) -> pd.DataFrame:
    try:
        # clickhouse_connect
        return client.query_df(sql)
    except Exception:
        rows, cols = client.execute(sql, with_column_types=True)
        colnames = [c[0] for c in cols]
        return pd.DataFrame(rows, columns=colnames)

class MarketDataSummarizer:
    """
    用 SQL 直接聚合你需要喂给 LLM 的“摘要指标”，严格对应你给的 DDL。
    所有方法都返回字典（数值尽量 float/int），方便直接序列化做 prompt 输入。
    """
    def __init__(self, ch_client, symbol: str):
        self.ch = ch_client
        self.symbol = symbol

    # -------- 5s（最近 120s 内的摘要） --------
    def micro_5s_summary(self, lookback_sec: int = 120, limit_rows: int = 40) -> Dict[str, Any]:
        sql = f"""
        WITH t AS (
          SELECT ts, close, ret_5, rv_20, rvol_roll_20, tick_imbalance, trade_count_ratio,
                 close_to_mid_bps, vwap_dev_bps, spread_bps, price_impact_5m, ema_cross
          FROM trading.features_5s
          WHERE symbol = '{self.symbol}' AND ts >= now() - INTERVAL {lookback_sec} SECOND
          ORDER BY ts DESC
          LIMIT {limit_rows}
        )
        SELECT
          max(ts) AS ts_last,
          avg(ret_5) AS ret5_mean,
          stddevSamp(ret_5) AS ret5_std,
          min(ret_5) AS ret5_min,
          max(ret_5) AS ret5_max,
          covarSamp(close, toUnixTimestamp(ts)) / nullIf(varSamp(toUnixTimestamp(ts)),0) AS close_slope_per_sec,
          avg(rv_20) AS rv20,
          avg(rvol_roll_20) AS rvol20,
          avg(tick_imbalance) AS tick_imbalance,
          avg(trade_count_ratio) AS trade_count_ratio,
          avg(close_to_mid_bps) AS close_to_mid_bps,
          avg(vwap_dev_bps) AS vwap_dev_bps,
          avg(spread_bps) AS spread_bps,
          avg(price_impact_5m) AS price_impact_5m,
          anyLast(ema_cross) AS ema_cross_last
        FROM t
        """
        df = _df(self.ch, sql)
        return {} if df.empty else {k: (None if pd.isna(v) else (float(v) if isinstance(v, (float, int)) else v))
                                    for k, v in df.iloc[0].items()}

    # -------- 1m（最近 15 根的摘要） --------
    def intra_1m_summary(self, limit_rows: int = 15) -> Dict[str, Any]:
        sql = f"""
        WITH t AS (
          SELECT ts, ret_1, atr_20, zscore_close_20, vwap_day, vwap_slope_20,
                 rsi_14, cmf_20, stoch_k_14
          FROM trading.features_1m
          WHERE symbol = '{self.symbol}'
          ORDER BY ts DESC
          LIMIT {limit_rows}
        )
        SELECT
          max(ts) AS ts_last,
          avg(ret_1) AS ret1_mean,
          stddevSamp(ret_1) AS ret1_std,
          anyLast(atr_20) AS atr20_last,
          anyLast(zscore_close_20) AS zc20_last,
          anyLast(vwap_day) AS vwap_day_last,
          anyLast(vwap_slope_20) AS vwap_slope_last,
          anyLast(rsi_14) AS rsi_last,
          anyLast(cmf_20) AS cmf_last,
          anyLast(stoch_k_14) AS stoch_k_last
        FROM t
        """
        df = _df(self.ch, sql)
        return {} if df.empty else {k: (None if pd.isna(v) else float(v) if isinstance(v, (float, int)) else v)
                                    for k, v in df.iloc[0].items()}

    # -------- 5m（方向与节奏，近 90 分钟） --------
    def intra_5m_summary(self, lookback_min: int = 90, limit_rows: int = 20) -> Dict[str, Any]:
        sql = f"""
        WITH t AS (
          SELECT ts, ema_9, ema_21, bb_pct_20, donchian_hi_20, donchian_lo_20, slope_20, r2_20, rv_20
          FROM trading.features_5m
          WHERE symbol = '{self.symbol}' AND ts >= now() - INTERVAL {lookback_min} MINUTE
          ORDER BY ts DESC
          LIMIT {limit_rows}
        )
        SELECT
          max(ts) AS ts_last,
          anyLast(ema_9) AS ema9_last,
          anyLast(ema_21) AS ema21_last,
          anyLast(bb_pct_20) AS bb_pct_last,
          anyLast(donchian_hi_20) AS don_hi_last,
          anyLast(donchian_lo_20) AS don_lo_last,
          avg(slope_20) AS slope20_mean,
          avg(r2_20) AS r2_mean,
          avg(rv_20) AS rv20_mean
        FROM t
        """
        df = _df(self.ch, sql)
        return {} if df.empty else {k: (None if pd.isna(v) else float(v) if isinstance(v, (float, int)) else v)
                                    for k, v in df.iloc[0].items()}

    # -------- 15m（方向与节奏，近 4 小时） --------
    def intra_15m_summary(self, lookback_hours: int = 4, limit_rows: int = 16) -> Dict[str, Any]:
        sql = f"""
        WITH t AS (
          SELECT ts, ema_9, ema_21, bb_pct_20, slope_20, r2_20, rv_20
          FROM trading.features_15m
          WHERE symbol = '{self.symbol}' AND ts >= now() - INTERVAL {lookback_hours} HOUR
          ORDER BY ts DESC
          LIMIT {limit_rows}
        )
        SELECT
          max(ts) AS ts_last,
          anyLast(ema_9) AS ema9_last,
          anyLast(ema_21) AS ema21_last,
          anyLast(bb_pct_20) AS bb_pct_last,
          avg(slope_20) AS slope20_mean,
          avg(r2_20) AS r2_mean,
          avg(rv_20) AS rv20_mean
        FROM t
        """
        df = _df(self.ch, sql)
        return {} if df.empty else {k: (None if pd.isna(v) else float(v) if isinstance(v, (float, int)) else v)
                                    for k, v in df.iloc[0].items()}

    # -------- 2h（高周期，只取最新） --------
    def higher_tf_2h_latest(self) -> Dict[str, Any]:
        sql = f"""
        SELECT ts, ema_20, ema_50, atr_14, atr_20, z_price_60, kama_10
        FROM trading.features_2h
        WHERE symbol = '{self.symbol}'
        ORDER BY ts DESC
        LIMIT 1
        """
        df = _df(self.ch, sql)
        return {} if df.empty else {k: (None if pd.isna(v) else float(v) if isinstance(v, (float, int)) else v)
                                    for k, v in df.iloc[0].items() if k != 'ts'}

    # -------- 日线（背景） --------
    def daily_snapshot(self) -> Dict[str, Any]:
        sql = f"""
        SELECT ts_start, open, high, low, close, vwap, volume
        FROM trading.bars_1d_current
        WHERE symbol = '{self.symbol}'
        ORDER BY ts_start DESC
        LIMIT 1
        """
        df = _df(self.ch, sql)
        return {} if df.empty else {
            "open": float(df["open"].iloc[0]),
            "high": float(df["high"].iloc[0]),
            "low": float(df["low"].iloc[0]),
            "close": float(df["close"].iloc[0]),
            "vwap": (None if pd.isna(df["vwap"].iloc[0]) else float(df["vwap"].iloc[0])),
            "volume": float(df["volume"].iloc[0]),
        }

    # -------- 市场广谱（VIX/指数/相关性） --------
    def market_wide_latest(self) -> Dict[str, Any]:
        sql = f"""
        SELECT vix, vix_z_20, vix_chg_5m, vix_rsi_14,
               spy_ret_1, spy_ret_5, qqq_ret_1, qqq_ret_5,
               beta_spy_20, beta_spy_60, corr_spy_20, corr_spy_60, corr_qqq_20
        FROM trading.features_1m
        WHERE symbol = '{self.symbol}'
        ORDER BY ts DESC
        LIMIT 1
        """
        df = _df(self.ch, sql)
        if df.empty:
            return {}
        row = df.iloc[0].to_dict()
        for k, v in list(row.items()):
            row[k] = None if pd.isna(v) else float(v) if isinstance(v, (float, int)) else v
        return row
```

---

### `time_phase.py`

计算美股 Session 的阶段与“距收盘分钟”，喂给 LLM 做全局决策约束。

```python
# time_phase.py
# -*- coding: utf-8 -*-
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

@dataclass
class MarketClock:
    now_utc_iso: str
    now_et_iso: str
    phase: str            # PREMARKET / OPEN / MIDDAY / POWER_HOUR / CLOSE / POST
    minutes_since_open: int
    minutes_to_close: int
    is_rth: bool

def compute_market_clock(now_utc: datetime) -> MarketClock:
    """
    简版美股 RTH：周一到周五 09:30-16:00 ET，无节假日过滤（如需更精确，可接入 exchange-calendars）。
    """
    et = now_utc.astimezone(ET)
    open_t = datetime.combine(et.date(), time(9, 30), tzinfo=ET)
    close_t = datetime.combine(et.date(), time(16, 0), tzinfo=ET)

    is_rth = open_t <= et <= close_t
    if et < open_t:
        phase = "PREMARKET"
    elif et <= open_t + timedelta(minutes=45):
        phase = "OPEN"
    elif et < close_t - timedelta(minutes=60):
        phase = "MIDDAY"
    elif et < close_t - timedelta(minutes=10):
        phase = "POWER_HOUR"
    elif et <= close_t:
        phase = "CLOSE"
    else:
        phase = "POST"

    minutes_since_open = max(0, int((et - open_t).total_seconds() // 60))
    minutes_to_close = max(0, int((close_t - et).total_seconds() // 60))
    return MarketClock(
        now_utc_iso=now_utc.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        now_et_iso=et.replace(microsecond=0).isoformat(),
        phase=phase,
        minutes_since_open=minutes_since_open,
        minutes_to_close=minutes_to_close,
        is_rth=is_rth,
    )
```

---

### `risk_mapper.py`

将波动/趋势/流动性/情绪映射成 0–1 分数 + 每笔建议风险预算 R。

```python
# risk_mapper.py
# -*- coding: utf-8 -*-
from typing import Dict, Any
import math

def _nz(x, default=0.0):
    return default if x is None else x

def clip01(x): 
    return 0.0 if x is None else max(0.0, min(1.0, float(x)))

def norm(x, lo, hi):
    if x is None or hi == lo:
        return 0.5
    return clip01((x - lo) / (hi - lo))

def build_risk_map(portfolio: Dict[str, Any], intra_1m: Dict[str, Any], market_wide: Dict[str, Any]) -> Dict[str, Any]:
    """
    输入：portfolio、1m特征、市场广谱；输出：四个评分 + 风险预算 + 建议止损距离
    """
    # 粗归一化示例：可按你的标的做更贴合的范围
    atr20 = _nz(intra_1m.get("atr20_last"), 1.0)
    rsi = _nz(intra_1m.get("rsi_last"), 50.0)
    zc20 = intra_1m.get("zc20_last")
    vix = market_wide.get("vix")
    vix_z = market_wide.get("vix_z_20")
    # 1) 波动分（atr 越大分越高→风险更大）
    regime_volatility_score = norm(atr20, 0.2, 5.0)
    # 2) 趋势分（rsi 偏离 50 越大、zscore_close_20 越一致 → 趋势更“干净”）
    trend_strength = (abs(rsi - 50.0) / 50.0 + abs(_nz(zc20, 0.0)) / 3.0) / 2.0
    regime_trend_score = clip01(trend_strength)
    # 3) 情绪/压力分（VIX 越高越紧张）
    if vix is not None:
        market_stress_score = norm(vix, 10.0, 35.0)
    elif vix_z is not None:
        market_stress_score = norm(vix_z, -1.5, 2.0)
    else:
        market_stress_score = 0.4
    # 4) 流动性成本（spread/impact 可来自 5s 摘要，也可外部传入）
    # 这里先占位，调用方可覆盖
    liquidity_cost_bps = 3.0

    # 建议每笔风险预算 R（美元）
    # 例：基础 = min(现金1%, 上限0.5%资本)，随波动增加而缩小
    cash = _nz(portfolio.get("available_cash"), 0.0)
    cap = _nz(portfolio.get("capital_limit"), cash)
    base_R = min(cash * 0.01, cap * 0.005)
    vol_penalty = (0.6 + 0.4 * (1.0 - regime_volatility_score))  # 波动高→系数更低
    risk_budget_per_trade = max(50.0, base_R * vol_penalty)      # 最小 $50 的动作

    # 建议止损距离（美元）：使用 atr20 的 0.6~1.0 倍
    suggested_stop_distance = max(0.1, 0.8 * atr20)

    return {
        "regime_volatility_score": float(regime_volatility_score),
        "regime_trend_score": float(regime_trend_score),
        "liquidity_cost_bps": float(liquidity_cost_bps),
        "market_stress_score": float(market_stress_score),
        "risk_budget_per_trade": float(risk_budget_per_trade),
        "suggested_stop_distance": float(suggested_stop_distance),
    }
```

---

### `memory_store.py`

用 Redis 存最近 N 笔“情节记忆”，并给 LLM 的摘要。

```python
# memory_store.py
# -*- coding: utf-8 -*-
import json
from typing import Dict, Any, List, Optional
from statistics import mean

class TradeMemoryStore:
    """
    Redis 列表：mem:{symbol} -> 每项是JSON字符串 {ts, action, conf, qty, entry, exit, pnl_pct, hold_min, note}
    """
    def __init__(self, redis_client, max_items_per_symbol: int = 50):
        self.r = redis_client
        self.max_items = max_items_per_symbol

    def append(self, symbol: str, item: Dict[str, Any]) -> None:
        key = f"mem:{symbol}"
        self.r.lpush(key, json.dumps(item, ensure_ascii=False))
        self.r.ltrim(key, 0, self.max_items - 1)

    def recent(self, symbol: str, n: int = 10) -> List[Dict[str, Any]]:
        key = f"mem:{symbol}"
        raw = self.r.lrange(key, 0, n - 1) or []
        out = []
        for b in raw:
            try:
                if isinstance(b, bytes):
                    b = b.decode("utf-8")
                out.append(json.loads(b))
            except Exception:
                continue
        return out

    def summary(self, symbol: str, n: int = 10) -> Dict[str, Any]:
        items = self.recent(symbol, n=n)
        if not items:
            return {"recent_trades_summary": "暂无历史记忆", "last_n": []}
        pnls = [i.get("pnl_pct") for i in items if isinstance(i.get("pnl_pct"), (int, float))]
        holds = [i.get("hold_min") for i in items if isinstance(i.get("hold_min"), (int, float))]
        wins = [p for p in pnls if p is not None and p > 0]
        win_rate = (len(wins) / len(pnls)) if pnls else 0.0
        desc = f"近{len(items)}笔：胜率 {win_rate:.0%}，均盈亏 {mean(pnls):+.2f}% ，均持有 {mean(holds) if holds else 0:.1f} 分钟。"
        return {"recent_trades_summary": desc, "last_n": items}
```

---

### `prompt_builder_r1.py`

面向 LLM 的**位置感知 + 风险意识** Prompt 构建器（中英混简要，带 JSON Schema）。

```python
# prompt_builder_r1.py
# -*- coding: utf-8 -*-
from typing import Dict, Any, Optional
import json

def _fmt_num(x, prec=4):
    if x is None:
        return "NA"
    try:
        return f"{float(x):.{prec}f}"
    except Exception:
        return str(x)

def build_r1_prompt(context: Dict[str, Any]) -> str:
    """
    context 统一结构参考你之前确认的 JSON（time/portfolio/micro_5s/.../risk_map/memory/rules）。
    核心规则：
      - 空仓时不允许输出 HOLD，请输出 WAIT 或 BUY
      - 有仓位时才允许 HOLD（保持仓位），否则 HOLD==WAIT
      - 输出 JSON（严格遵守 schema），并给出简短理由 rationale
    """
    t = context.get("time", {})
    p = context.get("portfolio", {})
    micro = context.get("micro_5s", {})
    i1 = context.get("intra_1m", {})
    i5 = context.get("intra_5m", {})
    i15 = context.get("intra_15m", {})
    htf = context.get("higher_tf", {}).get("features_2h", {})
    dly = context.get("daily", {})
    mw = context.get("market_wide", {})
    risk = context.get("risk_map", {})
    mem = context.get("memory", {})
    rules = context.get("rules", {})

    pos_size = int(p.get("position_size") or 0)
    can_hold = pos_size > 0

    header = f"""You are an intraday trading decision assistant. Act like a disciplined trader with risk awareness.

[MARKET CLOCK]
- now_utc: {t.get('now_utc')}, now_et: {t.get('now_et')}, phase: {t.get('market_phase')}
- minutes_since_open: {t.get('minutes_since_open')}, minutes_to_close: {t.get('minutes_to_close')}

[PORTFOLIO]
- position_size: {pos_size}, avg_cost: {_fmt_num(p.get('avg_cost'))}, px: {_fmt_num(p.get('current_price'))}
- uPnL%: {_fmt_num(p.get('unrealized_pnl_pct'), 2)}, cash: {_fmt_num(p.get('available_cash'), 2)}, cap_limit: {_fmt_num(p.get('capital_limit'), 2)}
- RULE: {'HOLD allowed' if can_hold else 'HOLD NOT allowed (use WAIT instead)'}; No overnight; No new positions within {rules.get('no_new_positions_within_minutes_to_close', 20)} min to close.

[MICRO 5s]
- ret5 mean/std/min/max: {_fmt_num(micro.get('ret5_mean'),4)}/{_fmt_num(micro.get('ret5_std'),4)}/{_fmt_num(micro.get('ret5_min'),4)}/{_fmt_num(micro.get('ret5_max'),4)}
- slope/sec: {_fmt_num(micro.get('close_slope_per_sec'),4)}, rv20: {_fmt_num(micro.get('rv20'))}, rvol20: {_fmt_num(micro.get('rvol20'))}
- tick_imbalance: {_fmt_num(micro.get('tick_imbalance'))}, vwap_dev_bps: {_fmt_num(micro.get('vwap_dev_bps'))}, spread_bps: {_fmt_num(micro.get('spread_bps'))}

[INTRADAY 1m/5m/15m]
- 1m: atr20={_fmt_num(i1.get('atr20_last'))}, zc20={_fmt_num(i1.get('zc20_last'))}, rsi={_fmt_num(i1.get('rsi_last'))}, cmf={_fmt_num(i1.get('cmf_last'))}
- 5m: ema9={_fmt_num(i5.get('ema9_last'))}, ema21={_fmt_num(i5.get('ema21_last'))}, bb_pct={_fmt_num(i5.get('bb_pct_last'))}, donchian_hi/lo={_fmt_num(i5.get('don_hi_last'))}/{_fmt_num(i5.get('don_lo_last'))}
- 15m: ema9={_fmt_num(i15.get('ema9_last'))}, ema21={_fmt_num(i15.get('ema21_last'))}, slope20_mean={_fmt_num(i15.get('slope20_mean'))}

[HIGHER TF / DAILY]
- 2h: ema20={_fmt_num(htf.get('ema_20'))}, ema50={_fmt_num(htf.get('ema_50'))}, atr20={_fmt_num(htf.get('atr_20'))}, z60={_fmt_num(htf.get('z_price_60'))}
- 1d: high/low/vwap={_fmt_num(dly.get('high'))}/{_fmt_num(dly.get('low'))}/{_fmt_num(dly.get('vwap'))}

[MARKET WIDE]
- VIX={_fmt_num(mw.get('vix'))} (z20={_fmt_num(mw.get('vix_z_20'))}, d5m={_fmt_num(mw.get('vix_chg_5m'))}), beta_spy20={_fmt_num(mw.get('beta_spy_20'))}, corr_spy20={_fmt_num(mw.get('corr_spy_20'))}

[RISK MAP]
- vol_score={_fmt_num(risk.get('regime_volatility_score'))}, trend_score={_fmt_num(risk.get('regime_trend_score'))}, stress={_fmt_num(risk.get('market_stress_score'))}
- liquidity_cost_bps={_fmt_num(risk.get('liquidity_cost_bps'))}, R=${_fmt_num(risk.get('risk_budget_per_trade'),2)}, stop_dist≈${_fmt_num(risk.get('suggested_stop_distance'),2)}

[RECENT MEMORY]
- {mem.get('recent_trades_summary', 'N/A')}
"""

    guidance = """Decision rules to obey:
1) If position_size == 0, you MUST NOT output "HOLD". Use "WAIT" for no-action; or "BUY" to enter.
2) If minutes_to_close <= configured limit, DO NOT open new positions; prefer reduce/exit.
3) Prefer smaller size in high volatility / high VIX / poor liquidity; larger only when trend score is strong and volatility is moderate.
4) Suggest position by "risk_units" (0..3). Each unit risks roughly R dollars with suggested stop distance. Keep integers.
5) Keep output terse and in JSON only.

Return strictly this JSON schema:
{
  "action": "BUY" | "SELL" | "HOLD" | "WAIT",
  "confidence": 0.0-1.0,
  "risk_units": 0 | 1 | 2 | 3,
  "reason": "one or two short sentences"
}
"""

    return header + "\n" + guidance

def parse_llm_decision(raw_text: str, position_size: int) -> Dict[str, Any]:
    """
    解析 LLM 输出；空仓 HOLD → 归一化为 WAIT；字段兜底。
    """
    try:
        obj = json.loads(raw_text)
    except Exception:
        return {"action": "WAIT", "confidence": 0.0, "risk_units": 0, "reason": "invalid JSON"}

    action = str(obj.get("action", "WAIT")).upper()
    conf = float(obj.get("confidence", 0.0))
    units = int(obj.get("risk_units", 0))
    reason = str(obj.get("reason", ""))[:240]

    if position_size <= 0 and action == "HOLD":
        action = "WAIT"

    if action not in {"BUY", "SELL", "HOLD", "WAIT"}:
        action = "WAIT"
    units = max(0, min(3, units))
    conf = max(0.0, min(1.0, conf))
    return {"action": action, "confidence": conf, "risk_units": units, "reason": reason}
```

---

### `analytics_pipeline.py`

把一切串起来：取数 → 风险映射 → 记忆 → 统一上下文 → 构建 Prompt。

```python
# analytics_pipeline.py
# -*- coding: utf-8 -*-
from datetime import datetime, timezone
from typing import Dict, Any

from market_data_summarizer import MarketDataSummarizer
from time_phase import compute_market_clock
from risk_mapper import build_risk_map
from memory_store import TradeMemoryStore
from prompt_builder_r1 import build_r1_prompt, parse_llm_decision

def assemble_context(ch_client, redis_client, symbol: str, portfolio_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    # 1) 时间相位
    clock = compute_market_clock(datetime.now(timezone.utc))

    # 2) 市场与多周期摘要
    s = MarketDataSummarizer(ch_client, symbol)
    micro_5s = s.micro_5s_summary()
    intra_1m = s.intra_1m_summary()
    intra_5m = s.intra_5m_summary()
    intra_15m = s.intra_15m_summary()
    higher_2h = s.higher_tf_2h_latest()
    daily = s.daily_snapshot()
    market_wide = s.market_wide_latest()

    # 3) 风险映射
    risk_map = build_risk_map(portfolio_snapshot, intra_1m, market_wide)
    # 使用 5s 的成本代理覆盖
    if micro_5s:
        risk_map["liquidity_cost_bps"] = float(micro_5s.get("spread_bps") or 0.0) + float(micro_5s.get("price_impact_5m") or 0.0)

    # 4) 记忆
    mem = TradeMemoryStore(redis_client).summary(symbol)

    # 5) 规则
    rules = {
        "no_overnight": True,
        "no_new_positions_within_minutes_to_close": 20,
        "hold_means_keep_only_if_position_gt_zero": True,
        "max_risk_per_trade_fraction": 0.01
    }

    ctx = {
        "time": {
            "now_utc": clock.now_utc_iso,
            "now_et": clock.now_et_iso,
            "market_phase": clock.phase,
            "minutes_since_open": clock.minutes_since_open,
            "minutes_to_close": clock.minutes_to_close
        },
        "portfolio": portfolio_snapshot,
        "micro_5s": micro_5s,
        "intra_1m": intra_1m,
        "intra_5m": intra_5m,
        "intra_15m": intra_15m,
        "higher_tf": {"features_2h": higher_2h},
        "daily": daily,
        "market_wide": market_wide,
        "liquidity_cost": {
            "spread_bps": float(micro_5s.get("spread_bps") or 0.0),
            "impact_bps": float(micro_5s.get("price_impact_5m") or 0.0),
        },
        "risk_map": risk_map,
        "memory": mem,
        "rules": rules
    }
    return ctx

def build_prompt_for_r1(context: Dict[str, Any]) -> str:
    return build_r1_prompt(context)

def interpret_llm_decision(raw_text: str, position_size: int) -> Dict[str, Any]:
    return parse_llm_decision(raw_text, position_size)
```

---

## 如何接入你现有的 Claude 代码框架（不改动业务逻辑，只换“输入和解释器”）

1. 在 `run_single_bot.py`（或你负责发起 LLM 的位置）里，补几行：

```python
from analytics_pipeline import assemble_context, build_prompt_for_r1, interpret_llm_decision

# ...在每个 trading cycle 里，拿到 portfolio 快照：
portfolio_snapshot = {
    "position_size": position.size,
    "avg_cost": position.avg_cost,
    "current_price": last_price,
    "unrealized_pnl_pct": upnl_pct,
    "available_cash": cash,
    "capital_limit": args.cash or 50000,
}

ctx = assemble_context(ch_client, redis_client, symbol, portfolio_snapshot)
prompt = build_prompt_for_r1(ctx)

# 调用 DeepSeek R1（或你的 provider 接口），拿到字符串 raw, 其他 llm 也有做
# raw = provider.generate(prompt, ...)

decision = interpret_llm_decision(raw, position_size=portfolio_snapshot["position_size"])

# 将 "risk_units" → shares（按照 risk_map 的 R 与建议止损距离换算）
R = ctx["risk_map"]["risk_budget_per_trade"]
stop = ctx["risk_map"]["suggested_stop_distance"]
px = portfolio_snapshot["current_price"] or 0.0
shares = 0
if decision["action"] in ("BUY","SELL") and R > 0 and stop > 0 and px > 0:
    shares = max(0, int(round((decision["risk_units"] * R) / stop / px)))
```

2. 成交后，把结果写回记忆（让下轮 LLM“知道自己做了什么”）：

```python
from memory_store import TradeMemoryStore
mem = TradeMemoryStore(redis_client)
mem.append(symbol, {
    "ts": ctx["time"]["now_utc"],
    "action": decision["action"],
    "conf": decision["confidence"],
    "qty": filled_qty,
    "entry": entry_price,
    "exit": exit_price or None,
    "pnl_pct": pnl_pct or 0.0,
    "hold_min": hold_minutes or 0.0,
    "note": decision.get("reason","")
})
```

> 注意：如果你是**空仓**而 R1 返回了 `HOLD`，解析器会自动转为 `WAIT`，避免“空仓 HOLD”的尴尬。
> 收盘前 `no_new_positions_within_minutes_to_close` 约束仍由你在下单前做一道保护（例如直接跳过 BUY）。

---

如果你愿意，我可以在下一步给出**最小化的 `run_single_bot.py` 补丁（diff）**，只改动 15–25 行，把这套“供给+Prompt+解释器”串进去。
棒！我把你刚加的“数据供给 + 风险映射 + 记忆 + R1 Prompt”整合成**最小改动补丁**，直接套在你现有的 Claude 基座（`run_single_bot.py`）即可。下面是**统一 diff**，只改 20 多行，变量名用你项目里常见的字段；若与你的命名不同，按注释替换即可。

---

### `run_single_bot.py` 最小整合补丁（统一 diff）

```diff
--- a/run_single_bot.py
+++ b/run_single_bot.py
@@
+# === NEW: 智能供给与 Prompt 组件 ===
+from analytics_pipeline import assemble_context, build_prompt_for_r1, interpret_llm_decision

@@ def trading_cycle(...):
-    # 你原本的行情与仓位获取逻辑 ...
-    # current_price = ...
-    # position = portfolio_manager.get_position(symbol)
-    # upnl_pct, cash = ...
+    # 你原本的行情与仓位获取逻辑 ...
+    # 确保拿到以下变量：current_price, position(size/avg_cost), upnl_pct(未实现盈亏%), cash(可用现金)
+
+    # === NEW: 构建给 LLM 的“组合快照” ===
+    portfolio_snapshot = {
+        "position_size": int(getattr(position, "size", 0) or 0),
+        "avg_cost": float(getattr(position, "avg_cost", 0.0) or 0.0),
+        "current_price": float(current_price or 0.0),
+        "unrealized_pnl_pct": float(upnl_pct or 0.0),
+        "available_cash": float(cash or 0.0),
+        "capital_limit": float(args.cash or 50000.0),
+    }
+
+    # === NEW: 组装多周期摘要 + 风险映射 + 记忆 + 时钟相位 ===
+    ctx = assemble_context(ch_client, redis_client, symbol, portfolio_snapshot)
+
+    # === NEW: DeepSeek-R1 风格 Prompt,其他 llm 也做 ===
+    prompt = build_prompt_for_r1(ctx)
+
+    # === 调用你现有 provider 的推理接口（保持原来用法）===
+    # raw_text = provider.generate(prompt, model=args.model, timeout=args.llm_timeout, ...)
+    raw_text = llm_provider.generate(prompt)  # ← 若你方法名不同，替换为你的调用
+
+    # === NEW: 解析 LLM 决策（空仓不允许 HOLD，会被归一化为 WAIT）===
+    decision = interpret_llm_decision(raw_text, position_size=portfolio_snapshot["position_size"])
+
+    # === NEW: 收盘前禁止开新仓（例如 5 分钟，可改 args）===
+    if ctx["time"]["minutes_to_close"] <= 5 and decision["action"] == "BUY":
+        decision["action"] = "WAIT"
+        decision["reason"] = "Within 5 minutes to close—no new positions."
+
+    # === NEW: risk_units → shares（基于风险预算R与建议止损距离换算）===
+    R = float(ctx["risk_map"]["risk_budget_per_trade"] or 0.0)
+    stop = float(ctx["risk_map"]["suggested_stop_distance"] or 0.0)
+    px = float(portfolio_snapshot["current_price"] or 0.0)
+    shares = 0
+    if decision["action"] in ("BUY", "SELL") and R > 0 and stop > 0 and px > 0:
+        shares = max(0, int(round((decision["risk_units"] * R) / (stop * px))))
+
+    # === NEW: 无仓位时 SELL/ HOLD 的保护（SELL→WAIT；HOLD→WAIT 已在解析器做过）===
+    if portfolio_snapshot["position_size"] <= 0 and decision["action"] == "SELL":
+        decision["action"] = "WAIT"
+        decision["risk_units"] = 0
+
+    # === 使用 shares 走你原有的下单逻辑 ===
+    # if decision["action"] == "BUY" and shares > 0: place_buy(symbol, shares)
+    # elif decision["action"] == "SELL" and shares > 0: place_sell(symbol, shares)
+    # elif decision["action"] in ("HOLD", "WAIT"): pass
+
+    # === （可选）落盘打印，便于对账 ===
+    logger.info(f"[{symbol}] LLM -> action={decision['action']} units={decision['risk_units']} shares={shares} conf={decision['confidence']:.2f} | {decision.get('reason','')}")
```

---

### 放置与运行

1. 把我之前给你的 5 个新文件放到你的项目根目录（或你习惯的 `llm_trader/` 包里）：

* `market_data_summarizer.py`
* `time_phase.py`
* `risk_mapper.py`
* `memory_store.py`
* `prompt_builder_r1.py`
* `analytics_pipeline.py`



---

### 生效后的三个关键变化（对照你日志里遇到的问题）

* **空仓不再输出 HOLD**：llm 若给 HOLD，解析器会自动转为 `WAIT`；避免“没持仓却 HOLD”的尴尬。
* **收盘前保护**：缺省 5 分钟内不再开新仓（可把阈值做成 `--no-new-positions-mins` 参数）。
* **自适应仓位**：R1 输出 `risk_units ∈ {0,1,2,3}`；按实时的“风险预算 R / 建议止损 / 当前价”换算成 `shares`，在高波动/高VIX时自然缩小。

